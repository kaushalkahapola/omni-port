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

import json
import re
from typing import Dict, List, Any, Optional, Tuple
from pathlib import Path
from pydantic import BaseModel, Field
from rapidfuzz import fuzz
from src.core.state import BackportState, LocalizationResult, PatchRetryContext

# Matches Java member-declaration lvalues:  [modifiers] Type name =
# Captures the declared name so we can detect duplicates introduced by new_string.
_JAVA_DECL_NAME_RE = re.compile(
    r"(?:public|private|protected|static|final|volatile|transient"
    r"|synchronized|abstract|native|strictfp)(?:\s+(?:public|private|protected"
    r"|static|final|volatile|transient|synchronized|abstract|native|strictfp))*"
    r"\s+[\w<>\[\],\s]+?\s+(\w+)\s*(?:=|[{(])",
    re.MULTILINE,
)

# Fix D: method boundary pattern — blank line followed by an access modifier.
_METHOD_BOUNDARY_RE = re.compile(
    r"\n\n(?=\s*(?:public|private|protected|static|final|abstract|synchronized|@Override)\b)",
)

# Fix C: Java keywords to exclude from variable-availability heuristic.
_JAVA_KEYWORDS = frozenset({
    "abstract", "assert", "boolean", "break", "byte", "case", "catch", "char",
    "class", "const", "continue", "default", "do", "double", "else", "enum",
    "extends", "final", "finally", "float", "for", "goto", "if", "implements",
    "import", "instanceof", "int", "interface", "long", "native", "new",
    "null", "package", "private", "protected", "public", "return", "short",
    "static", "strictfp", "super", "switch", "synchronized", "this", "throw",
    "throws", "transient", "try", "void", "volatile", "while", "true", "false",
    "var", "record", "sealed", "permits", "yield", "String", "Object",
    "Integer", "Long", "Double", "Float", "Boolean", "List", "Map", "Set",
    "Optional", "Collection", "Iterator", "Iterable", "Comparable",
})


def _declared_names(code: str) -> set:
    """Return the set of Java identifier names declared in *code*."""
    return {m.group(1) for m in _JAVA_DECL_NAME_RE.finditer(code)}


def _new_string_introduces_duplicates(
    file_content: str,
    old_string: str,
    new_string: str,
) -> bool:
    """
    Return True if *new_string* would introduce duplicate Java declarations.

    Two distinct failure modes are caught:

    1. Internal duplicates in new_string itself — the LLM echoed the same
       declaration twice (e.g. two ``public static final Version V_5_10_1``
       inside new_string).  These are always compile errors.

    2. Cross-file duplicates — new_string introduces a name that is NOT being
       replaced (not in old_string) but already exists elsewhere in the file
       (e.g. a constant that was already backported independently).
    """
    # ── Check 1: internal duplicates inside new_string ────────────────────────
    matches = list(_JAVA_DECL_NAME_RE.finditer(new_string))
    seen: set = set()
    for m in matches:
        name = m.group(1)
        if name in seen:
            return True
        seen.add(name)

    # ── Check 2: cross-file duplicates (new names already in the remainder) ───
    introduced = _declared_names(new_string) - _declared_names(old_string)
    if not introduced:
        return False
    remaining = file_content.replace(old_string, "", 1)
    existing = _declared_names(remaining)
    return bool(introduced & existing)


# ── Fix C: parsability gate ───────────────────────────────────────────────────

def _count_braces(text: str) -> Tuple[int, int]:
    """
    Count { and } in text, ignoring string literals and comments.
    Returns (open_count, close_count).
    """
    opens = closes = 0
    in_single_line_comment = False
    in_multi_line_comment = False
    in_string = False
    in_char = False
    i = 0
    while i < len(text):
        c = text[i]
        if in_single_line_comment:
            if c == "\n":
                in_single_line_comment = False
        elif in_multi_line_comment:
            if c == "*" and i + 1 < len(text) and text[i + 1] == "/":
                in_multi_line_comment = False
                i += 1
        elif in_string:
            if c == "\\" and i + 1 < len(text):
                i += 1  # skip escaped char
            elif c == '"':
                in_string = False
        elif in_char:
            if c == "\\" and i + 1 < len(text):
                i += 1
            elif c == "'":
                in_char = False
        else:
            if c == "/" and i + 1 < len(text):
                if text[i + 1] == "/":
                    in_single_line_comment = True
                    i += 1
                elif text[i + 1] == "*":
                    in_multi_line_comment = True
                    i += 1
            elif c == '"':
                in_string = True
            elif c == "'":
                in_char = True
            elif c == "{":
                opens += 1
            elif c == "}":
                closes += 1
        i += 1
    return opens, closes


def _has_duplicate_adjacent_imports(text: str) -> bool:
    """Return True if there are two identical adjacent import lines."""
    lines = text.splitlines()
    for i in range(len(lines) - 1):
        if lines[i].strip().startswith("import ") and lines[i].strip() == lines[i + 1].strip():
            return True
    return False


def _check_simulated_brace_balance(
    file_content: str,
    old_string: str,
    new_string: str,
) -> bool:
    """
    Return True if the simulated replacement keeps brace balance.
    Unbalanced braces in new_string relative to old_string are a strong signal
    that the LLM produced malformed code.

    Critical case: if old_string is a PARTIAL view of a method body (old_net > 0,
    meaning it opens more braces than it closes), then new_string must NOT close
    more braces than old_string did. If new_net < old_net, the LLM completed the
    full method body inside new_string while old_string only covers the beginning —
    this creates orphaned code after the replacement point (the body continuation
    that was not part of old_string remains verbatim, after the newly-inserted
    closing brace).
    """
    old_open, old_close = _count_braces(old_string)
    new_open, new_close = _count_braces(new_string)
    old_net = old_open - old_close
    new_net = new_open - new_close
    # Directional guard: if old_string is partially open (old_net > 0), new_string
    # must not close more braces than old_string. new_net < old_net means the LLM
    # emitted an extra closing brace, completing a body that was not fully in old_string.
    if old_net > 0 and new_net < old_net:
        return False
    # Allow at most 1 unit of tolerance for edge cases where context expansion
    # includes an outer class brace.
    return abs(new_net - old_net) <= 1


def _check_variable_availability(
    old_string: str,
    new_string: str,
    context_snapshot: str,
) -> bool:
    """
    Heuristic dataflow check: identifiers used in new_string should be available
    in the surrounding context (old_string + context_snapshot).

    Returns True (gate passes) when fewer than 3 identifiers in new_string are
    missing from the combined context.  The threshold prevents false positives
    on large new methods that introduce new local variable names.
    """
    # Extract lowercase-starting identifiers from new_string that look like
    # variable/field references (not declarations, keywords, or type names).
    new_ids = set(re.findall(r"\b([a-z][a-zA-Z0-9_]*)\b", new_string))
    new_ids -= _JAVA_KEYWORDS
    # Remove identifiers that are *declared* in new_string (they're introduced, not used)
    new_decl = set(re.findall(r"\b([a-z][a-zA-Z0-9_]*)\s*(?:=|;|\()", new_string))
    new_ids -= new_decl
    if not new_ids:
        return True

    combined_context = old_string + "\n" + context_snapshot
    context_ids = set(re.findall(r"\b([a-zA-Z][a-zA-Z0-9_]*)\b", combined_context))

    missing = new_ids - context_ids - _JAVA_KEYWORDS
    # Tolerate up to 2 missing identifiers (new helper methods, constants, etc.)
    return len(missing) < 3


def _validate_new_string(
    file_content: str,
    old_string: str,
    new_string: str,
    loc_result: LocalizationResult,
) -> Tuple[bool, str]:
    """
    Fix C: Combined parsability gate.
    Returns (passes, reason_if_rejected).

    Checks brace balance and duplicate imports only. Variable availability was
    removed: LLM-generated new_string routinely references identifiers from the
    wider file context that are absent from old_string/context_snapshot, causing
    too many false positives for structurally-refactored hunks.
    """
    if not new_string.strip():
        return False, "new_string is empty"
    if _has_duplicate_adjacent_imports(new_string):
        return False, "duplicate adjacent imports in new_string"
    if not _check_simulated_brace_balance(file_content, old_string, new_string):
        return False, "unbalanced braces in new_string relative to old_string"
    return True, ""


# ── Fix D: AST-guided sub-hunk splitting ──────────────────────────────────────

# Module-level accumulator for extra hunks produced by splitting.
_PENDING_EXTRA_HUNKS: List["SynthesizedHunk"] = []


def _split_at_boundaries(old_string: str, new_string: str) -> List[Tuple[str, str]]:
    """
    Split old_string and new_string at method boundaries.
    Returns a list of (old_chunk, new_chunk) pairs, one per method.
    """
    old_parts = re.split(_METHOD_BOUNDARY_RE, old_string)
    new_parts = re.split(_METHOD_BOUNDARY_RE, new_string)
    if len(old_parts) <= 1 or len(new_parts) <= 1:
        return []
    # Align: pair each old part with the corresponding new part if counts match,
    # otherwise fall back to one-to-one pairing of the common prefix.
    pairs = []
    for i in range(min(len(old_parts), len(new_parts))):
        op = old_parts[i].strip()
        np = new_parts[i].strip()
        if op and np:
            pairs.append((op, np))
    return pairs


def _try_split_hunk(
    hunk: Dict[str, Any],
    loc_result: LocalizationResult,
    file_content: str,
    synthesizer: "HunkSynthesizer",
) -> Optional["SynthesizedHunk"]:
    """
    Fix D: when synthesis fails for a large hunk, try splitting at method boundaries
    and synthesize each sub-hunk independently.  Returns the FIRST successful sub-hunk
    (head); remaining sub-hunks are appended to _PENDING_EXTRA_HUNKS.
    """
    global _PENDING_EXTRA_HUNKS
    old_string = hunk.get("old_content", "").rstrip("\n")
    new_string = hunk.get("new_content", "").rstrip("\n")

    # Only attempt splitting for large hunks (> 15 lines)
    if old_string.count("\n") < 15:
        return None

    pairs = _split_at_boundaries(old_string, new_string)
    if len(pairs) < 2:
        return None

    successful: List[SynthesizedHunk] = []
    file_path = loc_result.file_path

    for old_chunk, new_chunk in pairs:
        verified, confidence = synthesizer.verify_old_string_exists(file_content, old_chunk)
        if not verified:
            # Try with 2 context lines
            chunk_lines = old_chunk.splitlines()
            mid = max(0, len(chunk_lines) // 2)
            slice_content = synthesizer.extract_lines_with_context(
                file_content,
                loc_result.start_line + mid,
                loc_result.end_line,
                2,
            )
            verified, confidence = synthesizer.verify_old_string_exists(file_content, slice_content)
            if verified:
                old_chunk = slice_content

        if verified:
            gate_ok, _ = _validate_new_string(file_content, old_chunk, new_chunk, loc_result)
            if gate_ok and not _new_string_introduces_duplicates(file_content, old_chunk, new_chunk):
                successful.append(SynthesizedHunk(
                    file_path=file_path,
                    old_string=old_chunk,
                    new_string=new_chunk,
                    confidence=confidence * 0.8,  # penalise split hunks slightly
                    context_lines_included=0,
                    verified=True,
                ))

    if not successful:
        return None

    # Return head, stash the rest for the draining pass
    head = successful[0]
    _PENDING_EXTRA_HUNKS.extend(successful[1:])
    return head


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
        context_lines: int = 30,
    ) -> str:
        """
        Extracts [start_line..end_line] (1-indexed) plus context_lines on each side
        from file_content. Returns the slice as a string.
        """
        lines = file_content.splitlines(keepends=True)
        start = max(0, start_line - 1 - context_lines)
        end = min(len(lines), end_line + context_lines)
        return "".join(lines[start:end])

    def fuzzy_find_in_file(
        self,
        file_content: str,
        old_string: str,
        threshold: float = 0.85,
    ) -> Optional[str]:
        """
        Sliding-window fuzzy search through file_content for the best match to
        old_string. Returns the matched text slice if ratio >= threshold, else None.

        Used as a last-resort fallback when the localized line numbers are wrong
        (e.g. GumTree returned stub coordinates) and context expansion failed.
        """
        old_lines = old_string.splitlines()
        window_size = len(old_lines)
        if window_size == 0:
            return None

        file_lines = file_content.splitlines(keepends=True)
        if window_size > len(file_lines):
            return None

        best_ratio = 0.0
        best_start = -1

        for i in range(len(file_lines) - window_size + 1):
            window = "".join(file_lines[i : i + window_size])
            ratio = fuzz.token_sort_ratio(old_string, window) / 100.0
            if ratio > best_ratio:
                best_ratio = ratio
                best_start = i

        if best_ratio >= threshold and best_start >= 0:
            return "".join(file_lines[best_start : best_start + window_size])

        return None

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
        if verified and confidence >= 1.0:
            if _new_string_introduces_duplicates(file_content, old_string, new_string):
                return SynthesizedHunk(
                    file_path=file_path,
                    old_string=old_string,
                    new_string=new_string,
                    confidence=0.0,
                    context_lines_included=0,
                    verified=False,
                )
            # Fix C: parsability gate at attempt 0.
            gate_ok, gate_reason = _validate_new_string(file_content, old_string, new_string, loc_result)
            if not gate_ok:
                return SynthesizedHunk(
                    file_path=file_path,
                    old_string=old_string,
                    new_string=new_string,
                    confidence=0.0,
                    context_lines_included=0,
                    verified=False,
                )
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
        for context_lines in [3, 10, 20, 30]:
            expanded_old = self.extract_lines_with_context(
                file_content,
                loc_result.start_line,
                loc_result.end_line,
                context_lines,
            )
            verified, confidence = self.verify_old_string_exists(file_content, expanded_old)
            if verified and confidence >= 1.0:
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

                if _new_string_introduces_duplicates(file_content, expanded_old, expanded_new):
                    return SynthesizedHunk(
                        file_path=file_path,
                        old_string=expanded_old,
                        new_string=expanded_new,
                        confidence=0.0,
                        context_lines_included=context_lines,
                        verified=False,
                    )
                # Fix C: parsability gate at each context expansion.
                gate_ok, gate_reason = _validate_new_string(file_content, expanded_old, expanded_new, loc_result)
                if not gate_ok:
                    continue  # try next expansion level
                return SynthesizedHunk(
                    file_path=file_path,
                    old_string=expanded_old,
                    new_string=expanded_new,
                    confidence=confidence,
                    context_lines_included=context_lines,
                    verified=True,
                )

        # Fix D: try AST-guided sub-hunk splitting for large hunks.
        split_result = _try_split_hunk(hunk, loc_result, file_content, self)
        if split_result is not None:
            return split_result

        # Do NOT attempt fuzzy fallback for code hunks. If localization failed to find
        # an exact match via git/context expansion, fuzzy search often finds the WRONG
        # location in a diverged codebase (e.g. different API versions), leading to
        # corrupted synthesis. Better to fail cleanly and let retry logic handle it.
        # (Fuzzy is only safe for trivial hunks like test additions or docs.)

        return SynthesizedHunk(
            file_path=file_path,
            old_string=old_string,
            new_string=new_string,
            confidence=0.0,
            context_lines_included=0,
            verified=False,
        )

    def synthesize_pure_addition(
        self,
        hunk: Dict[str, Any],
        file_path: str,
    ) -> SynthesizedHunk:
        """
        For hunks with empty old_content (pure insertion of new method/block).
        Inserts before the final closing brace of the file (end of class body).
        Only used when localization failed entirely and old_content is empty.
        Confidence is 0.7 to flag for review — heuristic insertion point.
        """
        new_string = hunk.get("new_content", "").rstrip("\n")
        if not new_string:
            return SynthesizedHunk(
                file_path=file_path, old_string="", new_string="",
                confidence=0.0, context_lines_included=0, verified=False,
            )

        file_content = self.read_file(file_path)
        if not file_content:
            return SynthesizedHunk(
                file_path=file_path, old_string="", new_string="",
                confidence=0.0, context_lines_included=0, verified=False,
            )

        # Find the last line that is exactly `}` — the outermost class closing brace.
        lines = file_content.splitlines(keepends=True)
        for i in range(len(lines) - 1, -1, -1):
            stripped = lines[i].strip()
            if stripped == "}":
                # Use the closing brace + a few lines of context as the anchor
                # to ensure uniqueness (bare `}` may not be unique on its own).
                context_start = max(0, i - 2)
                anchor = "".join(lines[context_start : i + 1])
                if anchor in file_content:
                    expanded_new = "".join(lines[context_start:i]) + new_string + "\n" + lines[i]
                    return SynthesizedHunk(
                        file_path=file_path,
                        old_string=anchor,
                        new_string=expanded_new,
                        confidence=0.7,
                        context_lines_included=0,
                        verified=True,
                    )
                break

        return SynthesizedHunk(
            file_path=file_path, old_string="", new_string="",
            confidence=0.0, context_lines_included=0, verified=False,
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


# ── Fix B: structural fallback for conf=0 hunks ───────────────────────────────

def _structural_fallback_for_failed_loc(
    hunk: Dict[str, Any],
    file_path: str,
    repo_path: str,
) -> Optional[SynthesizedHunk]:
    """
    Fix B: when all 5 localization stages returned confidence=0, attempt a
    full-file structural rewrite via Agent 5's skeleton-rewrite entrypoint.
    Returns a SynthesizedHunk if successful, else None.
    """
    from src.agents.agent5_structural import StructuralRefactor
    from src.core.llm_router import get_default_router, LLMTier

    try:
        router = get_default_router()
        llm_client = router.get_model(LLMTier.REASONING)
    except Exception:
        return None

    synthesizer = HunkSynthesizer(repo_path)
    file_content = synthesizer.read_file(file_path)
    if not file_content:
        return None

    refactor = StructuralRefactor(repo_path, llm_client)
    output = refactor.rewrite_from_target_skeleton(hunk, file_content, file_path)
    if not output.success or not output.refactored_code.strip():
        return None

    # Parse JSON result: {"old_string": "...", "new_string": "..."}
    try:
        content = output.refactored_code.strip()
        if content.startswith("```"):
            content = "\n".join(content.splitlines()[1:])
            if content.endswith("```"):
                content = content[:-3].strip()
        data = json.loads(content)
        old_str = data.get("old_string", "").strip()
        new_str = data.get("new_string", "").strip()
    except (json.JSONDecodeError, AttributeError):
        return None

    if not old_str:
        return None

    # Verify the returned old_string actually exists in the file.
    if old_str not in file_content:
        return None

    # Apply parsability gate.
    stub_loc = LocalizationResult(
        method_used="structural_fallback",
        confidence=0.6,
        context_snapshot=old_str,
        file_path=file_path,
        start_line=0,
        end_line=0,
    )
    gate_ok, _ = _validate_new_string(file_content, old_str, new_str, stub_loc)
    if not gate_ok:
        return None
    if _new_string_introduces_duplicates(file_content, old_str, new_str):
        return None

    return SynthesizedHunk(
        file_path=file_path,
        old_string=old_str,
        new_string=new_str,
        confidence=0.6,
        context_lines_included=0,
        verified=True,
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
    global _PENDING_EXTRA_HUNKS

    repo_path = state["target_repo_path"]
    synthesizer = HunkSynthesizer(repo_path)

    hunks = state.get("hunks", [])
    loc_results = state.get("localization_results", [])
    processed_indices = set(state.get("processed_hunk_indices", []))
    retry_contexts: List[PatchRetryContext] = list(state.get("retry_contexts", []))

    # ── Build synthesis batches ───────────────────────────────────────────────

    # Initialize accumulator lists.
    all_synthesized: List[Dict[str, Any]] = []
    all_failed: List[Dict[str, Any]] = []

    # Reset the pending extra hunks accumulator for this invocation.
    _PENDING_EXTRA_HUNKS = []

    # 1. Adapted hunks (Agent 4).
    adapted = state.get("adapted_hunks", [])
    adapted_loc_indices = [h.get("loc_index", i) for i, h in enumerate(adapted)]

    # 2. Refactored hunks (Agent 5).
    refactored = state.get("refactored_hunks", [])
    refactored_loc_indices = [h.get("loc_index", i) for i, h in enumerate(refactored)]

    # 3. Unprocessed raw hunks: indices not claimed by any prior agent.
    passthrough = []
    for i, h in enumerate(hunks):
        if i not in processed_indices and i < len(loc_results):
            loc_result = loc_results[i]

            # Fix G: new-file hunk — create the file directly.
            if loc_result.method_used == "new_file":
                file_path = loc_result.file_path or h.get("file_path", "")
                new_content = h.get("new_content", "").rstrip("\n")
                if file_path and new_content:
                    abs_path = Path(repo_path) / file_path
                    try:
                        abs_path.parent.mkdir(parents=True, exist_ok=True)
                        abs_path.write_text(new_content, encoding="utf-8")
                        # Add a sentinel SynthesizedHunk with empty old_string
                        # so agent7 knows the file was created (skip CLAW replace).
                        all_synthesized.append(SynthesizedHunk(
                            file_path=file_path,
                            old_string="",
                            new_string=new_content,
                            confidence=1.0,
                            context_lines_included=0,
                            verified=True,
                        ).model_dump())
                    except Exception as e:
                        all_failed.append({**h, "error": f"new-file creation failed: {e}"})
                continue

            if loc_result.method_used != "failed" and loc_result.file_path:
                passthrough.append((i, h))
            else:
                # Before dropping: check if this is a pure-addition hunk (empty old_content).
                hunk_file = h.get("file_path") or h.get("target_file") or ""
                old_content = h.get("old_content", "")
                if not old_content and hunk_file:
                    result = synthesizer.synthesize_pure_addition(h, hunk_file)
                    if result.verified:
                        all_synthesized.append(result.model_dump())
                        continue

                # Fix B: structural fallback for conf=0 hunks.
                hunk_file = hunk_file or (loc_result.file_path if loc_result else "")
                if hunk_file:
                    fb_result = _structural_fallback_for_failed_loc(h, hunk_file, repo_path)
                    if fb_result is not None:
                        all_synthesized.append(fb_result.model_dump())
                        continue

                all_failed.append(h)
                retry_contexts.append(
                    PatchRetryContext(
                        error_type="synthesis_skipped_no_localization",
                        error_message="Hunk unclaimed; localization failed entirely",
                        attempt_count=state.get("current_attempt", 1),
                        suggested_action="manual_review",
                    )
                )

    passthrough_hunks = [h for _, h in passthrough]
    passthrough_loc_indices = [i for i, _ in passthrough]

    # ── Synthesize each batch ─────────────────────────────────────────────────

    for batch, loc_idx_list, label in [
        (adapted, adapted_loc_indices, "adapted"),
        (refactored, refactored_loc_indices, "refactored"),
        (passthrough_hunks, passthrough_loc_indices, "passthrough"),
    ]:
        if not batch:
            continue
        output = synthesizer.synthesize_batch(batch, loc_results, loc_index_override=loc_idx_list)
        all_synthesized.extend(h.model_dump() for h in output.synthesized_hunks)

        # For all failed hunks, try the structural fallback (Agent 5's skeleton
        # rewrite) as a last resort. For "adapted" hunks, Agent 4 stored the
        # original mainline old/new content so the LLM has accurate context.
        # For "passthrough" and "refactored" hunks, use the hunk content as-is.
        for failed_hunk in output.failed_hunks:
            file_path_for_fallback = None
            proxy = dict(failed_hunk)

            if label == "adapted":
                loc_idx = failed_hunk.get("loc_index")
                if loc_idx is not None and loc_idx < len(loc_results):
                    loc_r = loc_results[loc_idx]
                    file_path_for_fallback = loc_r.file_path
                    # Restore original mainline content so the LLM sees the full patch intent.
                    if failed_hunk.get("original_old_content"):
                        proxy["old_content"] = failed_hunk["original_old_content"]
                        proxy["new_content"] = failed_hunk.get("original_new_content", "")
            elif label == "refactored":
                loc_idx = failed_hunk.get("loc_index")
                if loc_idx is not None and loc_idx < len(loc_results):
                    file_path_for_fallback = loc_results[loc_idx].file_path
                if not file_path_for_fallback:
                    file_path_for_fallback = failed_hunk.get("file_path") or failed_hunk.get("target_file")
            else:  # passthrough
                file_path_for_fallback = failed_hunk.get("file_path") or failed_hunk.get("target_file")

            if file_path_for_fallback:
                fb = _structural_fallback_for_failed_loc(proxy, file_path_for_fallback, repo_path)
                if fb is not None:
                    all_synthesized.append(fb.model_dump())
                    continue

            all_failed.append(failed_hunk)

    # Fix D: drain any extra sub-hunks produced by the split path.
    if _PENDING_EXTRA_HUNKS:
        all_synthesized.extend(h.model_dump() for h in _PENDING_EXTRA_HUNKS)
        _PENDING_EXTRA_HUNKS = []

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
