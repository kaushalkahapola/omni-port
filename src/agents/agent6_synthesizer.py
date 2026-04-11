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

Fixes in this version:
  C — JavaParser parsability gate on new_string (reject garbage before it reaches disk)
  B — conf=0 structural fallback (full-file rewrite when localization failed entirely)
  G — New-file hunk path (old_string="" → create file with new_string content)
  D — AST-guided sub-hunk splitting on synthesis_no_match
"""

import re
import logging
from typing import Dict, List, Any, Optional, Tuple
from pathlib import Path
from pydantic import BaseModel, Field
from rapidfuzz import fuzz
from src.core.state import BackportState, LocalizationResult, PatchRetryContext

logger = logging.getLogger(__name__)

# Matches Java member-declaration lvalues:  [modifiers] Type name =
# Captures the declared name so we can detect duplicates introduced by new_string.
_JAVA_DECL_NAME_RE = re.compile(
    r"(?:public|private|protected|static|final|volatile|transient"
    r"|synchronized|abstract|native|strictfp)(?:\s+(?:public|private|protected"
    r"|static|final|volatile|transient|synchronized|abstract|native|strictfp))*"
    r"\s+[\w<>\[\],\s]+?\s+(\w+)\s*(?:=|[{(])",
    re.MULTILINE,
)


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


def _has_adjacent_duplicate_imports(code: str) -> bool:
    """
    Fix C: Return True if the code has adjacent identical import lines.
    This is a structural de-dup check beyond the declared-name check.
    """
    lines = code.splitlines()
    import_lines = [l.strip() for l in lines if l.strip().startswith("import ")]
    for i in range(1, len(import_lines)):
        if import_lines[i] == import_lines[i - 1]:
            return True
def _strip_java_comments(code: str) -> str:
    # Remove // comments
    code = re.sub(r'//.*', '', code)
    # Remove /* */ comments
    code = re.sub(r'/\*.*?\*/', '', code, flags=re.DOTALL)
    # Remove string literals to be safe
    code = re.sub(r'"(?:\\.|[^"\\])*"', '""', code)
    return code

def _has_undeclared_variables(file_content: str, old_string: str, new_string: str) -> Tuple[bool, str]:
    """
    Fix C Extended: Simple but valuable check to detect LLM hallucinations.
    Ensures that any new lower-camel-case identifiers (variables/methods) introduced
    in new_string actually exist in the surrounding file context or are locally declared.
    """
    clean_new = _strip_java_comments(new_string)
    clean_old = _strip_java_comments(old_string)
    
    tokens = set(re.findall(r'\b[a-z][a-zA-Z0-9_]*\b', clean_new))
    old_tokens = set(re.findall(r'\b[a-z][a-zA-Z0-9_]*\b', clean_old))
    introduced_tokens = tokens - old_tokens
    
    java_keywords = {"abstract", "assert", "boolean", "break", "byte", "case", "catch", "char",
        "class", "const", "continue", "default", "do", "double", "else", "enum",
        "extends", "final", "finally", "float", "for", "goto", "if", "implements",
        "import", "instanceof", "int", "interface", "long", "native", "new",
        "package", "private", "protected", "public", "return", "short", "static",
        "strictfp", "super", "switch", "synchronized", "this", "throw", "throws",
        "transient", "try", "void", "volatile", "while", "true", "false", "null",
        "var", "record", "sealed", "permits", "yield"}
        
    to_check = {t for t in introduced_tokens if len(t) > 2 and t not in java_keywords}
    
    surrounding_context = file_content.replace(old_string, "", 1)
    surrounding_tokens = set(re.findall(r'\b[a-z][a-zA-Z0-9_]*\b', surrounding_context))
    
    missing_from_file = to_check - surrounding_tokens
    
    for token in missing_from_file:
        # Check if it's locally declared in new_string (e.g., "Type token = " or "Type token;")
        decl_pattern = rf'(?:\w+(?:<.*>)?(?:\[\])*)\s+{token}\s*(?:[=;),])'
        if not re.search(decl_pattern, new_string):
            return True, token
            
    return False, ""



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

    # ── Fix C: Parsability gate ───────────────────────────────────────────────

    def _verify_new_string_parses(
        self,
        file_content: str,
        old_string: str,
        new_string: str,
        file_path: str,
    ) -> bool:
        """
        Fix C: Validate that replacing old_string with new_string produces parseable Java.

        Checks:
        1. Adjacent duplicate import lines (fast, no network)
        2. JavaParser parse-snippet endpoint (network, gracefully degraded)

        Returns True if the hunk passes all checks (gate passes), False to reject.
        Non-Java files always return True (gate is Java-only).
        """
        if not file_path.endswith(".java"):
            return True

        # Fast check: adjacent duplicate imports
        if _has_adjacent_duplicate_imports(new_string):
            logger.debug("[Fix C] Rejecting hunk for %s: adjacent duplicate imports in new_string", file_path)
            return False

        # Semantic fast check: Undeclared variables
        has_undeclared, missing_token = _has_undeclared_variables(file_content, old_string, new_string)
        if has_undeclared:
            logger.debug("[Fix C] Rejecting hunk for %s: undeclared variable/symbol '%s' in new_string", file_path, missing_token)
            return False

        # Network check: simulate replacement and parse the FULL modified result to catch broken boundaries
        try:
            from src.tools.java_http_client import javaparser_parse_snippet
            modified_file = file_content.replace(old_string, new_string, 1)
            result = javaparser_parse_snippet(modified_file, context_class="")
            status = result.get("status", "error")
            if status == "parse_error":
                errors = result.get("errors", [])
                logger.debug("[Fix C] Rejecting hunk for %s: parse_error — %s", file_path, errors[:1])
                return False
            # "ok" → passes; "error" (service unavailable) → treat as pass (non-blocking)
        except Exception as e:
            logger.debug("[Fix C] parse_snippet call failed for %s — treating as pass: %s", file_path, e)

        return True

    # ── Fix D: AST-guided sub-hunk splitting ─────────────────────────────────

    def _split_hunk_by_ast(
        self,
        hunk: Dict[str, Any],
        file_content: str,
        file_path: str,
        loc_result: "LocalizationResult",
    ) -> List[SynthesizedHunk]:
        """
        Fix D: When a single hunk covers multiple methods (mainline refactored N→N+1 methods),
        attempt to split it at method/class boundaries using GumTree, then synthesize each
        sub-hunk independently.

        Returns a list of verified SynthesizedHunks (may be empty if splitting fails or
        the GumTree service is unavailable).
        """
        old_content = hunk.get("old_content", "")
        new_content = hunk.get("new_content", "")
        if not old_content or not new_content:
            return []

        try:
            from src.tools.java_http_client import gumtree_diff
            gumtree_result = gumtree_diff(str(self.repo_path), file_path, old_content)
            if gumtree_result.get("status") == "error" or not gumtree_result.get("edits"):
                return []

            # Extract method boundary line numbers from GumTree edits
            # Look for MethodDeclaration nodes to find split points
            edits = gumtree_result.get("edits", [])
            method_boundaries: List[int] = []
            for edit in edits:
                node_type = edit.get("nodeType", "")
                if "Method" in node_type or "Constructor" in node_type:
                    src_line = edit.get("srcPos", {}).get("line", 0)
                    if src_line > 0:
                        method_boundaries.append(src_line)

            if not method_boundaries or len(method_boundaries) < 2:
                return []

            # Sort and de-dup split points
            method_boundaries = sorted(set(method_boundaries))

            # Split old_content at method boundaries
            old_lines = old_content.splitlines(keepends=True)
            new_lines = new_content.splitlines(keepends=True)

            # Create sub-hunks by splitting old_content at method boundary lines
            split_points = [0] + [b - 1 for b in method_boundaries[1:]] + [len(old_lines)]
            results: List[SynthesizedHunk] = []

            for i in range(len(split_points) - 1):
                start = split_points[i]
                end = split_points[i + 1]
                sub_old = "".join(old_lines[start:end]).rstrip("\n")
                # Corresponding new lines — heuristic: same proportional slice
                # (approximate; GumTree edits would give exact mapping but we keep it simple)
                new_start = int(start * len(new_lines) / max(len(old_lines), 1))
                new_end = int(end * len(new_lines) / max(len(old_lines), 1))
                sub_new = "".join(new_lines[new_start:new_end]).rstrip("\n")

                if not sub_old:
                    continue

                # Try to synthesize this sub-hunk
                sub_hunk = dict(hunk)
                sub_hunk["old_content"] = sub_old
                sub_hunk["new_content"] = sub_new

                result = self.synthesize_hunk(sub_hunk, loc_result, file_content)
                if result.verified:
                    results.append(result)

            return results

        except Exception as e:
            logger.debug("[Fix D] AST split failed for %s: %s", file_path, e)
            return []

    # ── Fix G: New-file synthesis ─────────────────────────────────────────────

    def _synthesize_new_file(
        self,
        hunk: Dict[str, Any],
        file_path: str,
    ) -> SynthesizedHunk:
        """
        Fix G: Synthesize a hunk for a brand-new file.

        old_string = "" (empty sentinel meaning "create this file")
        new_string = the full file content from new_content
        verified = True (no existence check needed — file is being created)
        """
        new_string = (hunk.get("new_content") or "").rstrip("\n")
        if not new_string:
            return SynthesizedHunk(
                file_path=file_path,
                old_string="",
                new_string="",
                confidence=0.0,
                context_lines_included=0,
                verified=False,
            )
        return SynthesizedHunk(
            file_path=file_path,
            old_string="",
            new_string=new_string,
            confidence=1.0,
            context_lines_included=0,
            verified=True,
        )

    # ── Core synthesis ────────────────────────────────────────────────────────

    def synthesize_hunk(
        self,
        hunk: Dict[str, Any],
        loc_result: "LocalizationResult",
        file_content: Optional[str] = None,
    ) -> SynthesizedHunk:
        """
        Synthesizes a CLAW hunk pair and verifies old_string exists in the target file.

        Fix G: If loc_result.method_used == "new_file", delegates to _synthesize_new_file.

        Context expansion strategy:
          - Try the raw old_content first (0 extra context lines).
          - If not found, expand the WINDOW that is read from the TARGET FILE
            around the localized position, keeping new_string unchanged.
            (Adding file context to old_string makes it unique; new_string stays
            as the pure replacement — the surrounding context is re-inserted
            verbatim by CLAW when it replaces old_string with new_string.)
          - Fix D: If all context expansions fail, attempt AST-guided sub-hunk splitting.
        """
        file_path = loc_result.file_path

        # Fix G: new-file fast path
        if loc_result.method_used == "new_file":
            return self._synthesize_new_file(hunk, file_path)

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
            if _new_string_introduces_duplicates(file_content, old_string, new_string):
                return SynthesizedHunk(
                    file_path=file_path,
                    old_string=old_string,
                    new_string=new_string,
                    confidence=0.0,
                    context_lines_included=0,
                    verified=False,
                )
            # Fix C: parsability gate
            if not self._verify_new_string_parses(file_content, old_string, new_string, file_path):
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

                if _new_string_introduces_duplicates(file_content, expanded_old, expanded_new):
                    return SynthesizedHunk(
                        file_path=file_path,
                        old_string=expanded_old,
                        new_string=expanded_new,
                        confidence=0.0,
                        context_lines_included=context_lines,
                        verified=False,
                    )
                # Fix C: parsability gate on expanded new_string
                if not self._verify_new_string_parses(file_content, expanded_old, expanded_new, file_path):
                    return SynthesizedHunk(
                        file_path=file_path,
                        old_string=expanded_old,
                        new_string=expanded_new,
                        confidence=0.0,
                        context_lines_included=context_lines,
                        verified=False,
                    )
                return SynthesizedHunk(
                    file_path=file_path,
                    old_string=expanded_old,
                    new_string=expanded_new,
                    confidence=confidence,
                    context_lines_included=context_lines,
                    verified=True,
                )

        # Fix D: AST-guided sub-hunk splitting as last resort before full failure.
        # Only attempt if the hunk covers multiple lines (worth splitting).
        old_line_count = len((old_string or "").splitlines())
        if old_line_count >= 5 and file_path.endswith(".java"):
            split_results = self._split_hunk_by_ast(hunk, file_content, file_path, loc_result)
            if split_results:
                # Return the first successful split result.
                # The caller (synthesize_batch) will handle multi-result returns.
                # For now, store extras in the hunk's metadata for the batch method.
                hunk["_ast_split_results"] = [r.model_dump() for r in split_results[1:]]
                return split_results[0]

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
        loc_results: List["LocalizationResult"],
        loc_index_override: Optional[List[int]] = None,
    ) -> SynthesizerOutput:
        """
        Synthesizes a list of hunks.

        loc_index_override: when set, hunk[k] maps to loc_results[loc_index_override[k]].
        Otherwise, assumes 1-to-1 correspondence.

        Fix D: Handles AST-split results stored in hunk["_ast_split_results"] — these
        are additional verified hunks produced when a single hunk was split at method
        boundaries. They are added to the synthesized output automatically.
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
                # Fix D: also add any extra split-results stored in the hunk dict
                for extra in hunk.get("_ast_split_results", []):
                    synthesized.append(SynthesizedHunk(**extra))
            else:
                failed.append(hunk)

        return SynthesizerOutput(
            synthesized_hunks=synthesized,
            failed_hunks=failed,
            success=len(failed) == 0,
            error_message=None if not failed else f"{len(failed)} hunk(s) failed verification",
        )


# ── Fix B: conf=0 structural fallback helpers ─────────────────────────────────

def rewrite_hunk_against_full_file(
    hunk: Dict[str, Any],
    file_content: str,
    file_path: str,
    llm_client: Any,
) -> Optional[SynthesizedHunk]:
    """
    Fix B: When all 5 localization stages fail (conf=0, method_used="failed"),
    invoke the Reasoning LLM with the full target file + the mainline hunk and ask it
    to produce a CLAW old/new pair targeting the goal of the hunk.

    Returns a verified SynthesizedHunk if successful, None otherwise.

    The LLM is asked to return its result in the format:
        OLD_STRING_START
        <exact string from file>
        OLD_STRING_END
        NEW_STRING_START
        <replacement string>
        NEW_STRING_END
    """
    old_content = hunk.get("old_content", "")
    new_content = hunk.get("new_content", "")

    prompt = f"""You are Agent 5 (Structural Refactor) for OmniPort, a Java patch backporting system.

A hunk from a mainline patch could not be localized in the target file — all 5 localization
stages failed. Your task is to identify WHERE in the target file this change should be made,
and produce an exact CLAW old/new string pair.

Target file path: {file_path}

Target file content:
```java
{file_content[:8000]}
```

Mainline hunk — code being REMOVED from mainline:
```java
{old_content}
```

Mainline hunk — code being ADDED in mainline:
```java
{new_content}
```

The target file may use different class names, method names, or have different structure
than the mainline. Your job is to:
1. Find the section in the TARGET FILE that corresponds semantically to the mainline old code
2. Produce the equivalent new code adapted to the target file's actual names and structure
3. Return EXACTLY in this format (no other text):

OLD_STRING_START
<copy the exact bytes from the target file that need replacing>
OLD_STRING_END
NEW_STRING_START
<your replacement code using target file's actual names>
NEW_STRING_END

CRITICAL: OLD_STRING must be an exact verbatim substring of the target file content above.
If you cannot identify a clear match, return empty OLD_STRING_START/END blocks.
"""

    try:
        response = llm_client.invoke(prompt)
        text = response.content if hasattr(response, "content") else str(response)

        # Parse the delimited format
        old_match = re.search(
            r"OLD_STRING_START\n(.*?)\nOLD_STRING_END",
            text, re.DOTALL
        )
        new_match = re.search(
            r"NEW_STRING_START\n(.*?)\nNEW_STRING_END",
            text, re.DOTALL
        )

        if not old_match or not new_match:
            return None

        old_string = old_match.group(1).strip()
        new_string = new_match.group(1).strip()

        if not old_string or old_string not in file_content:
            return None

        # Verify and return
        count = file_content.count(old_string)
        confidence = 1.0 if count == 1 else 0.9

        return SynthesizedHunk(
            file_path=file_path,
            old_string=old_string,
            new_string=new_string,
            confidence=confidence,
            context_lines_included=0,
            verified=True,
        )

    except Exception as e:
        logger.debug("[Fix B] rewrite_hunk_against_full_file failed for %s: %s", file_path, e)
        return None


# ── LangGraph node ────────────────────────────────────────────────────────────

def hunk_synthesizer_agent(state: BackportState) -> BackportState:
    """
    LangGraph node: Hunk Synthesizer.

    Synthesizes CLAW-compatible exact-string pairs for:
      - adapted_hunks   (Agent 4 output)
      - refactored_hunks (Agent 5 output)
      - unprocessed raw hunks (fallback for hunks that no specialist claimed)

    applied_hunks (Agent 3 output) are already on disk and are excluded.

    Fix B: conf=0 hunks (method_used="failed") now attempt a full-file structural
    rewrite via the Reasoning LLM before being dropped as synthesis_skipped_no_localization.
    Fix G: hunks with method_used="new_file" go directly to _synthesize_new_file.
    """
    repo_path = state["target_repo_path"]
    synthesizer = HunkSynthesizer(repo_path)

    hunks = state.get("hunks", [])
    loc_results = state.get("localization_results", [])
    processed_indices = set(state.get("processed_hunk_indices", []))
    retry_contexts: List[PatchRetryContext] = list(state.get("retry_contexts", []))

    # Fix B: obtain LLM client for conf=0 fallback (lazy — only used when needed)
    _llm_client_cache: List[Any] = []

    def _get_llm_client() -> Optional[Any]:
        if _llm_client_cache:
            return _llm_client_cache[0]
        try:
            from src.core.llm_router import get_default_router, LLMTier
            client = get_default_router().get_model(LLMTier.REASONING)
            _llm_client_cache.append(client)
            return client
        except Exception as e:
            logger.debug("[Fix B] Could not get LLM client: %s", e)
            _llm_client_cache.append(None)
            return None

    # ── Build synthesis batches ───────────────────────────────────────────────

    # Initialize accumulator lists.
    all_synthesized: List[Dict[str, Any]] = []
    all_failed: List[Dict[str, Any]] = []

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
            if loc_result.method_used == "new_file":
                # Fix G: new-file hunks — synthesize directly without localization
                result = synthesizer._synthesize_new_file(h, loc_result.file_path)
                if result.verified:
                    all_synthesized.append(result.model_dump())
                else:
                    all_failed.append(h)
            elif loc_result.method_used != "failed" and loc_result.file_path:
                passthrough.append((i, h))
            else:
                # Before dropping: check for pure-addition hunk (empty old_content).
                hunk_file = h.get("file_path") or h.get("target_file") or ""
                old_content = h.get("old_content", "")
                if not old_content and hunk_file:
                    result = synthesizer.synthesize_pure_addition(h, hunk_file)
                    if result.verified:
                        all_synthesized.append(result.model_dump())
                        continue

                # Fix B: conf=0 fallback — attempt full-file structural rewrite
                hunk_file = loc_result.file_path or hunk_file
                if hunk_file:
                    file_content = synthesizer.read_file(hunk_file)
                    if file_content:
                        llm_client = _get_llm_client()
                        if llm_client:
                            logger.debug("[Fix B] Attempting full-file rewrite for %s (conf=0)", hunk_file)
                            fix_b_result = rewrite_hunk_against_full_file(h, file_content, hunk_file, llm_client)
                            if fix_b_result and fix_b_result.verified:
                                logger.debug("[Fix B] Full-file rewrite succeeded for %s", hunk_file)
                                all_synthesized.append(fix_b_result.model_dump())
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
