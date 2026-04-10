"""
Agent 6: Hunk Synthesizer (Balanced LLM)

Produces CLAW-compatible exact-string old_string/new_string pairs.
Verifies old_string exists verbatim in the target file.
If verification fails, expands context (±3/5/7/10 lines) and retries.

Input sources:
  - adapted_hunks  (from Agent 4 — namespace-adapted, not yet on disk)
  - refactored_hunks (from Agent 5 — structurally refactored, not yet on disk)
  - unprocessed raw hunks (indices NOT in processed_hunk_indices, as a fallback)

NOTE: applied_hunks from Agent 3 are already written to disk and must NOT be
re-synthesized or re-applied here.
"""

from typing import Dict, List, Any, Optional, Tuple
from pathlib import Path
from pydantic import BaseModel, Field
from src.core.state import BackportState, LocalizationResult, PatchRetryContext


# ── Data models ───────────────────────────────────────────────────────────────

class SynthesizedHunk(BaseModel):
    file_path: str
    old_string: str = Field(description="Exact string to find (verified present in target file)")
    new_string: str = Field(description="Exact replacement string")
    confidence: float
    context_lines_included: int
    verified: bool


class SynthesizerOutput(BaseModel):
    synthesized_hunks: List[SynthesizedHunk]
    failed_hunks: List[Dict[str, Any]]
    success: bool
    error_message: Optional[str]


# ── Core class ────────────────────────────────────────────────────────────────

class HunkSynthesizer:
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path)

    def read_file(self, file_path: str) -> Optional[str]:
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
        context_lines: int = 5,
    ) -> str:
        """
        Extracts [start_line..end_line] (1-indexed) plus context_lines on each side
        from file_content. Returns the slice as a string.
        """
        lines = file_content.splitlines(keepends=True)
        start = max(0, start_line - 1 - context_lines)
        end = min(len(lines), end_line + context_lines)
        return "".join(lines[start:end])

    def verify_old_string_exists(
        self,
        file_content: str,
        old_string: str,
    ) -> Tuple[bool, float]:
        """
        Returns (exists, confidence).
        1.0 → unique exact match; 0.9 → exact match but not unique; 0.0 → not found.
        """
        if not old_string:
            return False, 0.0
        if old_string in file_content:
            count = file_content.count(old_string)
            return True, 1.0 if count == 1 else 0.9
        return False, 0.0

    def synthesize_hunk(
        self,
        hunk: Dict[str, Any],
        loc_result: LocalizationResult,
        file_content: Optional[str] = None,
    ) -> SynthesizedHunk:
        """
        Synthesizes a CLAW hunk pair and verifies old_string exists in the target file.

        Context expansion strategy:
          - Try the raw old_content first (0 extra context lines).
          - If not found, expand the WINDOW that is read from the TARGET FILE
            around the localized position, keeping new_string unchanged.
            (Adding file context to old_string makes it unique; new_string stays
            as the pure replacement — the surrounding context is re-inserted
            verbatim by CLAW when it replaces old_string with new_string.)
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
                verified=False,
            )

        # Attempt 0: raw old_string.
        verified, confidence = self.verify_old_string_exists(file_content, old_string)
        if verified:
            return SynthesizedHunk(
                file_path=file_path,
                old_string=old_string,
                new_string=new_string,
                confidence=confidence,
                context_lines_included=0,
                verified=True,
            )

        # Attempts 1-4: expand context from the TARGET FILE around the localized region.
        # new_string does NOT change — CLAW replaces only the old_string portion.
        for context_lines in [3, 5, 7, 10]:
            expanded_old = self.extract_lines_with_context(
                file_content,
                loc_result.start_line,
                loc_result.end_line,
                context_lines,
            )
            verified, confidence = self.verify_old_string_exists(file_content, expanded_old)
            if verified:
                # Build the expanded new_string: same surrounding context lines
                # from the file, but with the core replacement swapped in.
                context_before = self.extract_lines_with_context(
                    file_content,
                    loc_result.start_line,
                    loc_result.start_line - 1,  # yields only the prefix lines
                    context_lines,
                )
                context_after = self.extract_lines_with_context(
                    file_content,
                    loc_result.end_line + 1,
                    loc_result.end_line,  # yields only the suffix lines
                    context_lines,
                )
                expanded_new = context_before + new_string + "\n" + context_after

                return SynthesizedHunk(
                    file_path=file_path,
                    old_string=expanded_old,
                    new_string=expanded_new,
                    confidence=confidence,
                    context_lines_included=context_lines,
                    verified=True,
                )

        return SynthesizedHunk(
            file_path=file_path,
            old_string=old_string,
            new_string=new_string,
            confidence=0.0,
            context_lines_included=0,
            verified=False,
        )

    def synthesize_batch(
        self,
        hunks: List[Dict[str, Any]],
        loc_results: List[LocalizationResult],
        loc_index_override: Optional[List[int]] = None,
    ) -> SynthesizerOutput:
        """
        Synthesizes a list of hunks.

        loc_index_override: when set, hunk[k] maps to loc_results[loc_index_override[k]].
        Otherwise, assumes 1-to-1 correspondence.
        """
        synthesized = []
        failed = []

        for k, hunk in enumerate(hunks):
            loc_idx = loc_index_override[k] if loc_index_override else k
            if loc_idx >= len(loc_results):
                failed.append(hunk)
                continue

            loc_result = loc_results[loc_idx]
            result = self.synthesize_hunk(hunk, loc_result)

            if result.verified:
                synthesized.append(result)
            else:
                failed.append(hunk)

        return SynthesizerOutput(
            synthesized_hunks=synthesized,
            failed_hunks=failed,
            success=len(failed) == 0,
            error_message=None if not failed else f"{len(failed)} hunk(s) failed verification",
        )


# ── LangGraph node ────────────────────────────────────────────────────────────

def hunk_synthesizer_agent(state: BackportState) -> BackportState:
    """
    LangGraph node: Hunk Synthesizer.

    Synthesizes CLAW-compatible exact-string pairs for:
      - adapted_hunks   (Agent 4 output)
      - refactored_hunks (Agent 5 output)
      - unprocessed raw hunks (fallback for hunks that no specialist claimed)

    applied_hunks (Agent 3 output) are already on disk and are excluded.
    """
    repo_path = state["target_repo_path"]
    synthesizer = HunkSynthesizer(repo_path)

    hunks = state.get("hunks", [])
    loc_results = state.get("localization_results", [])
    processed_indices = set(state.get("processed_hunk_indices", []))
    retry_contexts: List[PatchRetryContext] = list(state.get("retry_contexts", []))

    # ── Build synthesis batches ───────────────────────────────────────────────

    # 1. Adapted hunks (Agent 4).
    adapted = state.get("adapted_hunks", [])
    adapted_loc_indices = [h.get("loc_index", i) for i, h in enumerate(adapted)]

    # 2. Refactored hunks (Agent 5).
    refactored = state.get("refactored_hunks", [])
    refactored_loc_indices = [h.get("loc_index", i) for i, h in enumerate(refactored)]

    # 3. Unprocessed raw hunks: indices not claimed by any prior agent.
    passthrough = [
        (i, h) for i, h in enumerate(hunks)
        if i not in processed_indices and i < len(loc_results)
    ]
    passthrough_hunks = [h for _, h in passthrough]
    passthrough_loc_indices = [i for i, _ in passthrough]

    # ── Synthesize each batch ─────────────────────────────────────────────────

    all_synthesized: List[Dict[str, Any]] = []
    all_failed: List[Dict[str, Any]] = []

    for batch, loc_idx_list, label in [
        (adapted, adapted_loc_indices, "adapted"),
        (refactored, refactored_loc_indices, "refactored"),
        (passthrough_hunks, passthrough_loc_indices, "passthrough"),
    ]:
        if not batch:
            continue
        output = synthesizer.synthesize_batch(batch, loc_results, loc_index_override=loc_idx_list)
        all_synthesized.extend(h.model_dump() for h in output.synthesized_hunks)
        all_failed.extend(output.failed_hunks)

    # ── Retry contexts for failures ───────────────────────────────────────────
    for _ in all_failed:
        retry_contexts.append(
            PatchRetryContext(
                error_type="synthesis_failed_no_match",
                error_message="Could not verify exact string in target file",
                attempt_count=state.get("current_attempt", 1),
                suggested_action="relocalize",
            )
        )

    synthesis_status = (
        "success" if not all_failed
        else ("partial" if all_synthesized else "failed")
    )

    state["synthesized_hunks"] = all_synthesized
    state["failed_hunks"] = list(state.get("failed_hunks", [])) + all_failed
    state["synthesis_status"] = synthesis_status
    state["retry_contexts"] = retry_contexts
    state["current_attempt"] = state.get("current_attempt", 1) + 1
    return state
