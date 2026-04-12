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
from src.tools.import_cleanup import cleanup_java_imports

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

        # NOTE: import cleanup is intentionally deferred to the end of the
        # fast_apply_agent node (after ALL hunks for the file are applied).
        # Running it here would prematurely remove imports that a later hunk
        # still needs to find as its old_string anchor.

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

        # Skip hunks with no file location at all, or new-file hunks (handled by Agent 6).
        if not loc_result.file_path or loc_result.method_used in ("failed", "new_file"):
            continue

        old_string, new_string = agent.build_claw_strings(hunk, loc_result)
        if not old_string:
            continue

        # Check verbatim existence before claiming.
        file_content = agent.read_target_file(loc_result.file_path)
        can_exact_match = file_content is not None and old_string in file_content

        if not can_exact_match:
            # Exact string not in file → leave unclaimed so Agent 4/5/6 can adapt it.
            # This applies even for high-confidence git localization: the file is right
            # but the content has drifted, so downstream LLM agents must handle it.
            continue

        # Claim only when we have a confirmed verbatim match.
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

    # ── Deferred import cleanup ───────────────────────────────────────────────
    # Run cleanup_java_imports ONCE per file AFTER all hunks have been applied,
    # not inside process_hunk after each individual hunk.
    #
    # Why: import-reorganisation patches often split across two hunks — one hunk
    # adds the import at the new location, a second removes it from the old one.
    # Running cleanup after hunk-1 removes the old copy prematurely, destroying
    # the anchor text that hunk-2 needs, causing it to fall through to agent 4/6
    # and fail synthesis.
    modified_files = {r["file_path"] for r in applied_hunks if r.get("file_path", "").endswith(".java")}
    for fp in modified_files:
        content = agent.read_target_file(fp)
        if content is not None:
            cleaned = cleanup_java_imports(content)
            if cleaned != content:
                agent.write_target_file(fp, cleaned)

    state["applied_hunks"] = applied_hunks
    state["failed_hunks"] = failed_hunks
    state["processed_hunk_indices"] = processed_indices
    state["retry_contexts"] = retry_contexts
    state["current_attempt"] = state.get("current_attempt", 1) + 1
    return state
