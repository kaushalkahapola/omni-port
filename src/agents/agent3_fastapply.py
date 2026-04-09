"""
Agent 3: Fast-Apply Agent (No LLM)

Routed when localization method is git_exact and confidence is high (>0.85).
Uses deterministic git apply or CLAW exact-string replacement.
Performs no LLM-based reasoning.
If CLAW pre-validation fails, marks hunk for relocalizer.
"""

from typing import List, Dict, Any, Tuple, Optional
from pathlib import Path
from src.core.state import BackportState, LocalizationResult, PatchRetryContext
from src.backport_claw.apply_hunk import CLAWHunkApplier, CLAWHunkError


class FastApplyAgent:
    """
    Applies patches with git_exact localization and high confidence.
    Deterministic, no LLM reasoning.
    """

    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path)

    def should_route_to_fast_apply(self, loc_result: LocalizationResult) -> bool:
        """
        Checks if a localization result qualifies for fast apply.
        Returns True if:
        - method_used is 'git_exact'
        - confidence > 0.85
        """
        return (
            loc_result.method_used == "git_exact"
            and loc_result.confidence > 0.85
        )

    def read_target_file(self, file_path: str) -> Optional[str]:
        """
        Reads the target file from disk.
        Returns None if file doesn't exist.
        """
        target = self.repo_path / file_path
        if not target.exists():
            return None

        try:
            with open(target, "r", encoding="utf-8") as f:
                return f.read()
        except (IOError, UnicodeDecodeError):
            return None

    def write_target_file(self, file_path: str, content: str) -> bool:
        """
        Writes modified content back to the target file.
        Returns True on success, False on failure.
        """
        target = self.repo_path / file_path
        target.parent.mkdir(parents=True, exist_ok=True)

        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
            return True
        except (IOError, UnicodeDecodeError):
            return False

    def build_claw_strings(
        self,
        hunk: Dict[str, Any],
        loc_result: LocalizationResult
    ) -> Tuple[str, str]:
        """
        Builds CLAW old_string and new_string from hunk and localization result.

        old_string: Lines that should be removed (context + removal lines)
        new_string: Lines that should be added (context + addition lines)

        Both are extracted from the unified diff hunk.
        """
        old_content = hunk.get("old_content", "").rstrip("\n")
        new_content = hunk.get("new_content", "").rstrip("\n")

        return old_content, new_content

    def apply_hunk_to_file(
        self,
        file_path: str,
        old_string: str,
        new_string: str
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Applies a single hunk to a file using CLAW.

        Returns:
            Tuple of (success: bool, modified_content: str, error_message: Optional[str])
        """
        file_content = self.read_target_file(file_path)
        if file_content is None:
            return False, "", f"File not found: {file_path}"

        if not old_string:
            return False, "", "Empty old_string; cannot apply hunk"

        try:
            applier = CLAWHunkApplier(file_content)

            # Try exact match first
            success, result = applier.find_and_replace(old_string, new_string, context_lines=0)

            if success:
                return True, result, None

            # If exact match failed, try with expanded context (±5 lines)
            success, result = applier.find_and_replace(old_string, new_string, context_lines=5)

            if success:
                return True, result, None

            return False, "", f"Could not locate hunk in {file_path}"

        except CLAWHunkError as e:
            return False, "", str(e)

    def finalize_hunk_application(
        self,
        file_path: str,
        modified_content: str
    ) -> bool:
        """
        Writes the modified content back to the target file.
        """
        return self.write_target_file(file_path, modified_content)

    def process_hunk(
        self,
        hunk: Dict[str, Any],
        loc_result: LocalizationResult
    ) -> Dict[str, Any]:
        """
        Processes a single hunk through fast apply.

        Returns:
            Dict with keys:
            - applied: bool
            - file_path: str
            - error: Optional[str]
            - modified_content: Optional[str] (for testing)
        """
        file_path = loc_result.file_path

        # Build CLAW strings
        old_string, new_string = self.build_claw_strings(hunk, loc_result)

        # Apply to file
        success, modified_content, error = self.apply_hunk_to_file(
            file_path, old_string, new_string
        )

        if not success:
            return {
                "applied": False,
                "file_path": file_path,
                "error": error,
                "modified_content": None
            }

        # Write back to disk
        write_success = self.finalize_hunk_application(file_path, modified_content)

        if not write_success:
            return {
                "applied": False,
                "file_path": file_path,
                "error": f"Failed to write modified content to {file_path}",
                "modified_content": None
            }

        return {
            "applied": True,
            "file_path": file_path,
            "error": None,
            "modified_content": modified_content
        }


def fast_apply_agent(state: BackportState) -> BackportState:
    """
    LangGraph node: Fast-Apply Agent.

    Reads hunks and localization results from state.
    Applies hunks with git_exact and high confidence.
    Updates state with applied hunks.
    Marks failed hunks with retry context.
    """
    repo_path = state["target_repo_path"]
    agent = FastApplyAgent(repo_path)

    hunks = state.get("hunks", [])
    loc_results = state.get("localization_results", [])

    # Map hunks to localization results (1:1 correspondence expected)
    applied_hunks = []
    failed_hunks = []
    retry_contexts = state.get("retry_contexts", [])

    for i, hunk in enumerate(hunks):
        if i >= len(loc_results):
            break

        loc_result = loc_results[i]

        # Check if this hunk qualifies for fast apply
        if not agent.should_route_to_fast_apply(loc_result):
            # Not eligible for fast apply; skip
            continue

        # Process the hunk
        result = agent.process_hunk(hunk, loc_result)

        if result["applied"]:
            applied_hunks.append(result)
        else:
            failed_hunks.append(result)

            # Add retry context
            error_msg = result.get("error", "Unknown error")
            retry_contexts.append(
                PatchRetryContext(
                    error_type="apply_failure_context_mismatch",
                    error_message=error_msg,
                    attempt_count=state.get("current_attempt", 1),
                    suggested_action="relocalize"
                )
            )

    # Update state
    state["applied_hunks"] = applied_hunks
    state["failed_hunks"] = failed_hunks
    state["retry_contexts"] = retry_contexts
    state["current_attempt"] = state.get("current_attempt", 1) + 1

    return state
