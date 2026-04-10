"""
Agent 3: Fast-Apply Agent (No LLM)

Strategy: for EVERY localized hunk, attempt exact-string match first.
- If old_content exists verbatim in the target file → apply directly, claim hunk.
- If not found and localization was high-confidence git → claim as failed (file changed).
- If not found and other method → do NOT claim; let Agent 4/5 handle adaptation.

This way, even fuzzy/embedding-localized hunks get fast-applied when the removed lines
are still present verbatim in the target (simple textual backport, no API drift).
"""

from typing import List, Dict, Any, Tuple, Optional
from pathlib import Path
from src.core.state import BackportState, LocalizationResult, PatchRetryContext
from src.backport_claw.apply_hunk import CLAWHunkApplier, CLAWHunkError

# High-confidence git methods: always claim the hunk even if exact match fails.
_FAST_APPLY_METHODS = {"git_exact", "git_pickaxe"}
_FAST_APPLY_MIN_CONFIDENCE = 0.85


class FastApplyAgent:
    """
    Applies patches using deterministic exact-string replacement.
    Tries ALL hunks with a valid file location; claims only those where the
    match succeeds (or where high-confidence git localization found the file).
    """

    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path)

    def is_high_confidence_git(self, loc_result: LocalizationResult) -> bool:
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

    For every localized hunk, checks if old_content exists verbatim in the target file.
    - Match found → claim, apply directly to disk (no LLM needed).
    - No match, high-confidence git → claim as failed (location correct but content changed).
    - No match, other method → skip (Agent 4/5 will handle adaptation).
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

        # Skip hunks with no file location at all.
        if not loc_result.file_path or loc_result.method_used == "failed":
            continue

        old_string, new_string = agent.build_claw_strings(hunk, loc_result)
        if not old_string:
            continue

        # Check verbatim existence before claiming.
        file_content = agent.read_target_file(loc_result.file_path)
        can_exact_match = file_content is not None and old_string in file_content

        if not can_exact_match and not agent.is_high_confidence_git(loc_result):
            # Exact string not in file and localization is imprecise →
            # leave unclaimed so Agent 4 (namespace) can adapt it.
            continue

        # Claim this hunk.
        processed_indices.append(i)

        if can_exact_match:
            result = agent.process_hunk(hunk, loc_result)
        else:
            # High-confidence git but exact string missing → content has genuinely changed.
            result = {
                "applied": False,
                "file_path": loc_result.file_path,
                "error": (
                    f"Exact match not found in {loc_result.file_path} despite "
                    f"high-confidence {loc_result.method_used} localization"
                ),
            }

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
