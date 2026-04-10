"""
Agent 3: Fast-Apply Agent (No LLM)

Routed when localization method is 'git_exact' or 'git_pickaxe' and confidence > 0.85.
Uses deterministic CLAW exact-string replacement.
Performs no LLM-based reasoning.
If CLAW pre-validation fails, marks hunk for re-localization via retry context.
"""

from typing import List, Dict, Any, Tuple, Optional
from pathlib import Path
from src.core.state import BackportState, LocalizationResult, PatchRetryContext
from src.backport_claw.apply_hunk import CLAWHunkApplier, CLAWHunkError

# Methods that qualify for deterministic fast-apply.
_FAST_APPLY_METHODS = {"git_exact", "git_pickaxe"}
_FAST_APPLY_MIN_CONFIDENCE = 0.85


class FastApplyAgent:
    """
    Applies patches with git_exact/git_pickaxe localization and high confidence.
    Deterministic, no LLM reasoning.
    """

    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path)

    def should_route_to_fast_apply(self, loc_result: LocalizationResult) -> bool:
        return (
            loc_result.method_used in _FAST_APPLY_METHODS
            and loc_result.confidence >= _FAST_APPLY_MIN_CONFIDENCE
        )

    def read_target_file(self, file_path: str) -> Optional[str]:
        target = self.repo_path / file_path
        if not target.exists():
            return None
        try:
            return target.read_text(encoding="utf-8")
        except (IOError, UnicodeDecodeError):
            return None

    def write_target_file(self, file_path: str, content: str) -> bool:
        target = self.repo_path / file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_text(content, encoding="utf-8")
            return True
        except (IOError, UnicodeDecodeError):
            return False

    def build_claw_strings(
        self,
        hunk: Dict[str, Any],
        loc_result: LocalizationResult,
    ) -> Tuple[str, str]:
        """
        Builds CLAW old_string / new_string from hunk content.
        The patch_parser strips +/- markers, so old_content and new_content
        are plain code lines ready for exact-string matching.
        """
        old_content = hunk.get("old_content", "").rstrip("\n")
        new_content = hunk.get("new_content", "").rstrip("\n")
        return old_content, new_content

    def apply_hunk_to_file(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Returns (success, modified_content, error_message).
        """
        file_content = self.read_target_file(file_path)
        if file_content is None:
            return False, "", f"File not found: {file_path}"
        if not old_string:
            return False, "", "Empty old_string; cannot apply hunk"

        try:
            applier = CLAWHunkApplier(file_content)

            # Exact match first (context_lines=0).
            success, result = applier.find_and_replace(old_string, new_string, context_lines=0)
            if success:
                return True, result, None

            # Expand context up to ±5 lines on failure.
            success, result = applier.find_and_replace(old_string, new_string, context_lines=5)
            if success:
                return True, result, None

            return False, "", f"Could not locate hunk in {file_path}"
        except CLAWHunkError as e:
            return False, "", str(e)

    def process_hunk(
        self,
        hunk: Dict[str, Any],
        loc_result: LocalizationResult,
    ) -> Dict[str, Any]:
        file_path = loc_result.file_path
        old_string, new_string = self.build_claw_strings(hunk, loc_result)

        success, modified_content, error = self.apply_hunk_to_file(
            file_path, old_string, new_string
        )
        if not success:
            return {"applied": False, "file_path": file_path, "error": error}

        write_ok = self.write_target_file(file_path, modified_content)
        if not write_ok:
            return {
                "applied": False,
                "file_path": file_path,
                "error": f"Failed to write modified content to {file_path}",
            }

        return {"applied": True, "file_path": file_path, "error": None}


def fast_apply_agent(state: BackportState) -> BackportState:
    """
    LangGraph node: Fast-Apply Agent.

    Processes hunks whose localization method is git_exact or git_pickaxe with
    confidence >= 0.85. Writes them directly to disk (no LLM, no synthesis step
    needed). Records their indices in processed_hunk_indices so later agents skip them.
    """
    repo_path = state["target_repo_path"]
    agent = FastApplyAgent(repo_path)

    hunks = state.get("hunks", [])
    loc_results = state.get("localization_results", [])
    processed_indices: List[int] = list(state.get("processed_hunk_indices", []))
    applied_hunks: List[Dict[str, Any]] = list(state.get("applied_hunks", []))
    failed_hunks: List[Dict[str, Any]] = list(state.get("failed_hunks", []))
    retry_contexts: List[PatchRetryContext] = list(state.get("retry_contexts", []))

    for i, hunk in enumerate(hunks):
        if i in processed_indices:
            continue
        if i >= len(loc_results):
            break

        loc_result = loc_results[i]
        if not agent.should_route_to_fast_apply(loc_result):
            continue

        # Claim this hunk regardless of success so no other agent tries it.
        processed_indices.append(i)

        result = agent.process_hunk(hunk, loc_result)
        if result["applied"]:
            applied_hunks.append(result)
        else:
            failed_hunks.append({**hunk, "error": result["error"]})
            retry_contexts.append(
                PatchRetryContext(
                    error_type="apply_failure_context_mismatch",
                    error_message=result.get("error", "Unknown error"),
                    attempt_count=state.get("current_attempt", 1),
                    suggested_action="relocalize",
                )
            )

    state["applied_hunks"] = applied_hunks
    state["failed_hunks"] = failed_hunks
    state["processed_hunk_indices"] = processed_indices
    state["retry_contexts"] = retry_contexts
    state["current_attempt"] = state.get("current_attempt", 1) + 1
    return state
