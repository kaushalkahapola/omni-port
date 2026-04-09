"""
Agent 6: Hunk Synthesizer (Balanced LLM)

Produces CLAW-compatible exact-string old_string/new_string pairs.
Verifies old_string exists verbatim in target file.
If verification fails, expands context (±5 lines) and retries.

This is a critical safety gate before hunk application.
"""

from typing import Dict, List, Any, Optional, Tuple
from pathlib import Path
from pydantic import BaseModel, Field
from src.core.state import BackportState, LocalizationResult, PatchRetryContext


class SynthesizedHunk(BaseModel):
    """A CLAW-compatible hunk with exact-string pairs."""
    file_path: str = Field(description="Target file path")
    old_string: str = Field(description="Exact string to find (with context)")
    new_string: str = Field(description="Exact replacement string (with context)")
    confidence: float = Field(description="Confidence in exact match (0-1)")
    context_lines_included: int = Field(description="Number of context lines on each side")
    verified: bool = Field(description="Whether old_string was verified in target")


class SynthesizerOutput(BaseModel):
    """Output from hunk synthesis."""
    synthesized_hunks: List[SynthesizedHunk] = Field(description="Generated CLAW hunks")
    failed_hunks: List[Dict[str, Any]] = Field(description="Hunks that couldn't be synthesized")
    success: bool = Field(description="Overall synthesis success")
    error_message: Optional[str] = Field(description="Error if synthesis failed")


class HunkSynthesizer:
    """
    Synthesizes CLAW-compatible exact-string hunks.
    Verifies they exist in the target before application.
    """

    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path)

    def read_file(self, file_path: str) -> Optional[str]:
        """Reads file content from target repo."""
        target = self.repo_path / file_path
        if not target.exists():
            return None

        try:
            return target.read_text(encoding="utf-8")
        except (IOError, UnicodeDecodeError):
            return None

    def extract_lines_with_context(
        self,
        file_content: str,
        start_line: int,
        end_line: int,
        context_lines: int = 5
    ) -> str:
        """
        Extracts lines with surrounding context.

        Args:
            file_content: Full file content
            start_line: Start line (1-indexed)
            end_line: End line (1-indexed)
            context_lines: Number of lines to include before/after

        Returns:
            String with context
        """
        lines = file_content.splitlines(keepends=True)

        # Convert to 0-indexed
        start = max(0, start_line - 1 - context_lines)
        end = min(len(lines), end_line + context_lines)

        return "".join(lines[start:end])

    def verify_old_string_exists(
        self,
        file_content: str,
        old_string: str
    ) -> Tuple[bool, float]:
        """
        Verifies that old_string exists in the file.
        Returns (exists, confidence).

        Confidence factors:
        - 1.0: Exact match, unique in file
        - 0.9: Exact match, but not unique
        - 0.5: Fuzzy match
        - 0.0: Not found
        """
        if not old_string:
            return False, 0.0

        # Check exact match
        if old_string in file_content:
            count = file_content.count(old_string)
            if count == 1:
                return True, 1.0
            else:
                return True, 0.9

        # No exact match
        return False, 0.0

    def synthesize_hunk(
        self,
        hunk: Dict[str, Any],
        loc_result: LocalizationResult,
        file_content: Optional[str] = None
    ) -> SynthesizedHunk:
        """
        Synthesizes a CLAW hunk from adapted/refactored hunk and localization result.

        Args:
            hunk: The hunk (possibly adapted/refactored)
            loc_result: Localization result with target location info
            file_content: Optional pre-read file content (for testing)

        Returns:
            SynthesizedHunk with verification status
        """
        file_path = loc_result.file_path
        old_string = hunk.get("old_content", "").rstrip("\n")
        new_string = hunk.get("new_content", "").rstrip("\n")

        if not file_content:
            file_content = self.read_file(file_path)

        if not file_content:
            return SynthesizedHunk(
                file_path=file_path,
                old_string="",
                new_string="",
                confidence=0.0,
                context_lines_included=0,
                verified=False
            )

        # Try exact match first
        verified, confidence = self.verify_old_string_exists(file_content, old_string)

        if verified:
            return SynthesizedHunk(
                file_path=file_path,
                old_string=old_string,
                new_string=new_string,
                confidence=confidence,
                context_lines_included=0,
                verified=True
            )

        # If exact match failed, try with expanded context
        for context_lines in [3, 5, 7, 10]:
            expanded_old = self.extract_lines_with_context(
                file_content,
                loc_result.start_line,
                loc_result.end_line,
                context_lines
            )

            verified, confidence = self.verify_old_string_exists(
                file_content, expanded_old
            )

            if verified:
                # Also expand new_string to match context
                expanded_new = self.extract_lines_with_context(
                    new_string,
                    0,
                    len(new_string.splitlines()),
                    context_lines
                )

                return SynthesizedHunk(
                    file_path=file_path,
                    old_string=expanded_old,
                    new_string=expanded_new,
                    confidence=confidence,
                    context_lines_included=context_lines,
                    verified=True
                )

        # All attempts failed
        return SynthesizedHunk(
            file_path=file_path,
            old_string=old_string,
            new_string=new_string,
            confidence=0.0,
            context_lines_included=0,
            verified=False
        )

    def synthesize_batch(
        self,
        hunks: List[Dict[str, Any]],
        loc_results: List[LocalizationResult]
    ) -> SynthesizerOutput:
        """
        Synthesizes multiple hunks.

        Args:
            hunks: List of hunks to synthesize
            loc_results: Corresponding localization results

        Returns:
            SynthesizerOutput with synthesized and failed hunks
        """
        synthesized = []
        failed = []

        for i, hunk in enumerate(hunks):
            if i >= len(loc_results):
                failed.append(hunk)
                continue

            loc_result = loc_results[i]
            synthesized_hunk = self.synthesize_hunk(hunk, loc_result)

            if synthesized_hunk.verified:
                synthesized.append(synthesized_hunk)
            else:
                failed.append(hunk)

        return SynthesizerOutput(
            synthesized_hunks=synthesized,
            failed_hunks=failed,
            success=len(failed) == 0,
            error_message=None if len(failed) == 0 else f"{len(failed)} hunks failed synthesis"
        )


def hunk_synthesizer_agent(state: BackportState) -> BackportState:
    """
    LangGraph node: Hunk Synthesizer.

    Takes hunks that have been adapted/refactored and produces
    CLAW-compatible exact-string pairs with verification.
    This is the final safety gate before application.
    """
    repo_path = state["target_repo_path"]
    synthesizer = HunkSynthesizer(repo_path)

    # Collect all processed hunks
    all_hunks = []
    all_hunks.extend(state.get("applied_hunks", []))  # From fast-apply
    all_hunks.extend(state.get("adapted_hunks", []))   # From namespace adapter
    all_hunks.extend(state.get("refactored_hunks", [])) # From structural refactor

    # Use original hunks if nothing was processed
    if not all_hunks:
        all_hunks = state.get("hunks", [])

    loc_results = state.get("localization_results", [])

    # Synthesize all hunks
    output = synthesizer.synthesize_batch(all_hunks, loc_results)

    synthesized_hunks = [h.model_dump() for h in output.synthesized_hunks]
    retry_contexts = state.get("retry_contexts", [])

    # Add retry contexts for failed hunks
    for failed_hunk in output.failed_hunks:
        retry_contexts.append(
            PatchRetryContext(
                error_type="synthesis_failed_no_match",
                error_message="Could not find exact string in target file",
                attempt_count=state.get("current_attempt", 1),
                suggested_action="relocalize"
            )
        )

    state["synthesized_hunks"] = synthesized_hunks
    state["synthesis_status"] = "success" if output.success else "partial"
    state["retry_contexts"] = retry_contexts
    state["current_attempt"] = state.get("current_attempt", 1) + 1

    return state
