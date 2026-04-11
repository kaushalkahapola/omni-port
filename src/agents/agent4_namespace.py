"""
Agent 4: Namespace Adapter (Balanced LLM)

Handles unclaimed hunks where the old_content is NOT found verbatim in the target file
(API drift between branches) or where import statements differ between versions.

Routing conditions (any one is sufficient):
  - javaparser/gumtree_ast localization with symbol_mappings detected
  - Hunk diff contains differing import statements
  - old_content does not exist verbatim in target file (API name / method signature drift)

Agent 3 already tried exact-string match for every hunk and only skipped those that
don't match. So reaching Agent 4 means the content has genuinely drifted.
"""

import re
from typing import Dict, List, Any, Optional, Set
from pathlib import Path
from difflib import SequenceMatcher
from pydantic import BaseModel, Field
from src.core.state import BackportState, LocalizationResult, PatchRetryContext
from src.core.llm_router import get_default_router, LLMTier


# ── Abstract-class helpers ────────────────────────────────────────────────────

def _is_abstract_class(file_content: str) -> bool:
    """Returns True if the target file defines an abstract class."""
    return bool(re.search(r'\babstract\s+class\b', file_content))


def _get_abstract_method_names(file_content: str) -> Set[str]:
    """Return method names declared abstract in the target file (regex fallback)."""
    names: Set[str] = set()
    for m in re.finditer(
        r'\babstract\b\s+[\w<>\[\]?,\s]+?\s+(\w+)\s*\(',
        file_content,
    ):
        names.add(m.group(1))
    return names


def _extract_method_name_from_old_content(old_content: str) -> Optional[str]:
    """Extract the primary method name from the hunk's old_content."""
    m = re.search(
        r'(?:public|protected|private)\s+(?:(?:synchronized|static|final|abstract)\s+)*'
        r'[\w<>\[\]?,\s]+?\s+(\w+)\s*\(',
        old_content,
    )
    if m:
        return m.group(1)
    return None


def _old_content_has_concrete_body(old_content: str) -> bool:
    """True if old_content contains a concrete method body (opening brace after params).

    A method with a body looks like:  ) { ...  or  ) throws Foo {
    An abstract declaration looks like: );  — no opening brace follows the signature.
    """
    return bool(re.search(r'\)\s*(?:throws\s+[\w\s,]+)?\s*\{', old_content))


def _query_method_modifiers_from_service(
    repo_path: str,
    file_path: str,
    method_names: List[str],
) -> Optional[Dict[str, Any]]:
    """
    Ask the Java microservice for precise modifier info on the given methods.

    Returns the 'methods' dict on success, or None if the service is unavailable
    or the file cannot be parsed.  Callers must handle None gracefully.
    """
    try:
        from src.tools.java_http_client import javaparser_method_modifiers
        result = javaparser_method_modifiers(repo_path, file_path, method_names)
        if result.get("status") == "ok":
            return result.get("methods", {})
    except Exception:
        pass
    return None


def _should_skip_abstract_hunk(
    method_name: str,
    old_content: str,
    service_method_info: Optional[Dict[str, Any]],
    regex_abstract_methods: Set[str],
) -> bool:
    """
    Determine whether a hunk targeting an abstract method should be skipped.

    A hunk must be SKIPPED when:
      - The method is abstract in the target file AND
      - old_content has a CONCRETE BODY (i.e. mainline kept it concrete in a more
        specialised class, but the target moved it to an abstract base — the concrete
        implementation lives in a subclass, not here).

    A hunk must NOT be skipped when:
      - The method is abstract in the target BUT old_content is also an abstract
        declaration (e.g. visibility change: protected abstract → public abstract).
        That change applies to the abstract declaration itself.
      - The method is CONCRETE in the target (normal adaptation path).

    Priority: JavaParser service info > regex heuristics.
    """
    # --- primary: JavaParser service result ---
    if service_method_info is not None:
        info = service_method_info.get(method_name)
        if info is None:
            # Method not found in target file at all — don't skip (let LLM handle it).
            return False
        method_is_abstract_in_target = info.get("is_abstract", False)
        if not method_is_abstract_in_target:
            return False  # concrete in target → normal adaptation
        # Method is abstract in target. Skip only if old_content has a body.
        return _old_content_has_concrete_body(old_content)

    # --- fallback: regex heuristics ---
    if method_name not in regex_abstract_methods:
        return False  # not abstract in target → normal
    return _old_content_has_concrete_body(old_content)


def _compute_hunk_diff(hunk: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compute which lines are added, removed, and unchanged between old_content and
    new_content. Returns a dict with:
      - lines_removed: list of lines only in old_content
      - lines_added:   list of lines only in new_content
      - is_pure_add:   True when nothing is removed
      - is_pure_remove: True when nothing is added
      - imports_removed: import lines being removed
      - imports_added:   import lines being added
    """
    old_lines = [l for l in hunk.get("old_content", "").splitlines() if l.strip()]
    new_lines = [l for l in hunk.get("new_content", "").splitlines() if l.strip()]
    old_set = set(old_lines)
    new_set = set(new_lines)
    removed = [l for l in old_lines if l not in new_set]
    added = [l for l in new_lines if l not in old_set]
    return {
        "lines_removed": removed,
        "lines_added": added,
        "is_pure_add": len(removed) == 0 and len(added) > 0,
        "is_pure_remove": len(added) == 0 and len(removed) > 0,
        "imports_removed": [l.strip() for l in removed if l.strip().startswith("import ")],
        "imports_added": [l.strip() for l in added if l.strip().startswith("import ")],
    }


# ── Output model ──────────────────────────────────────────────────────────────

class NamespaceAdaptationOutput(BaseModel):
    adapted_old_content: str = Field(
        description="old_content rewritten to match the exact code found in the TARGET file (verbatim match required)"
    )
    adapted_new_content: str = Field(
        description=(
            "The replacement code for the target file. "
            "MUST use field/method/type names from the TARGET file (e.g. from context_snapshot), "
            "NOT the mainline names from original old_content or new_content. "
            "Empty ONLY for PURE REMOVAL hunks."
        )
    )
    imports_added: List[str] = Field(
        description="Fully-qualified import statements to add to the target file"
    )
    imports_removed: List[str] = Field(
        description="Fully-qualified import statements to remove from the target file"
    )
    notes: str = Field(description="Brief explanation of adaptations made")
    success: bool = Field(description="Whether adaptation succeeded")
    error_message: Optional[str] = Field(default=None)


# ── Routing helpers ───────────────────────────────────────────────────────────

# Methods whose localization evidence may carry symbol_mappings.
_NAMESPACE_METHODS = {"javaparser", "gumtree_ast"}


def _has_import_changes(hunk: Dict[str, Any]) -> bool:
    """Returns True when the hunk itself changes import statements."""
    old_imports = {
        l.strip()
        for l in hunk.get("old_content", "").splitlines()
        if l.strip().startswith("import ")
    }
    new_imports = {
        l.strip()
        for l in hunk.get("new_content", "").splitlines()
        if l.strip().startswith("import ")
    }
    return old_imports != new_imports


def _should_namespace_adapt(
    hunk: Dict[str, Any],
    loc_result: LocalizationResult,
    file_content: Optional[str] = None,
) -> bool:
    """
    True when the hunk needs namespace/import adaptation:
      - Localization found symbol renames via javaparser/gumtree_ast, OR
      - The hunk diff itself contains differing import statements, OR
      - old_content is not found verbatim in target file (API / method name drift).

    The third condition is the key addition: Agent 3 already verified verbatim
    existence for every localized hunk, so an unclaimed hunk with no exact match
    has drifted and needs LLM-based adaptation regardless of localization method.
    """
    has_symbol_mappings = bool(loc_result.symbol_mappings)
    method_with_mappings = loc_result.method_used in _NAMESPACE_METHODS and has_symbol_mappings

    if method_with_mappings or _has_import_changes(hunk):
        return True

    # API drift: old_content not found verbatim in target file.
    if file_content is not None:
        old_content = hunk.get("old_content", "").rstrip("\n")
        if old_content and old_content not in file_content:
            return True

    return False


# ── LLM-backed adaptation ─────────────────────────────────────────────────────

def _adapt_with_llm(
    hunk: Dict[str, Any],
    loc_result: LocalizationResult,
    pre_region_context: str = "",
    post_region_context: str = "",
    same_file_applied_hunks: Optional[List[Dict[str, Any]]] = None,
    abstract_methods_in_target: Optional[Set[str]] = None,
) -> NamespaceAdaptationOutput:
    """
    Calls the Balanced LLM to rewrite imports and symbol references.
    """
    mappings_text = "\n".join(
        f"  - {orig} -> {target}"
        for orig, target in loc_result.symbol_mappings.items()
    ) or "  (none detected by localization; infer from diff context)"

    pre_context_section = ""
    if pre_region_context.strip():
        pre_context_section = f"""
Code immediately BEFORE the localized region in the target file (already exists — do NOT remove unless it is part of the block being replaced):
```java
{pre_region_context}
```
"""

    post_context_section = ""
    if post_region_context.strip():
        post_context_section = f"""
Code immediately AFTER the replaced region in the target file (already exists — do NOT recreate):
```java
{post_region_context}
```
"""

    # Precompute what is actually added/removed so the LLM cannot confuse context
    # lines with removed lines (a common failure for pure-add hunks).
    diff = _compute_hunk_diff(hunk)
    if diff["is_pure_add"]:
        hunk_type = "PURE ADDITION (nothing is removed; lines are only inserted)"
    elif diff["is_pure_remove"]:
        hunk_type = "PURE REMOVAL (nothing is added; lines are only deleted)"
    else:
        hunk_type = "REPLACEMENT (some lines removed AND some lines added)"

    removed_summary = "\n".join(f"  - {l}" for l in diff["lines_removed"]) or "  (none)"
    added_summary = "\n".join(f"  + {l}" for l in diff["lines_added"]) or "  (none)"
    imports_removed_summary = "\n".join(f"  - {l}" for l in diff["imports_removed"]) or "  (none)"
    imports_added_summary = "\n".join(f"  + {l}" for l in diff["imports_added"]) or "  (none)"

    pure_add_note = ""
    if diff["is_pure_add"]:
        pure_add_note = """
IMPORTANT — THIS IS A PURE ADDITION:
- adapted_old_content must be a MINIMAL string that marks the insertion point —
  typically the one or two lines immediately BEFORE where the new code is inserted.
  It must exist verbatim in the target file.
- adapted_new_content must be adapted_old_content PLUS the new lines being added
  (translated to target-branch API style if needed).
- Do NOT remove or omit any existing code; nothing is deleted by this hunk.
"""

    # Build same-file context section to prevent referencing already-deleted symbols.
    same_file_section = ""
    if same_file_applied_hunks:
        parts = []
        for h in same_file_applied_hunks:
            diff_h = _compute_hunk_diff(h)
            removed = "\n".join(f"  - {l}" for l in diff_h["lines_removed"])
            added = "\n".join(f"  + {l}" for l in diff_h["lines_added"])
            if removed or added:
                parts.append(f"Removed lines:\n{removed or '  (none)'}\nAdded lines:\n{added or '  (none)'}")
        if parts:
            same_file_section = (
                "\n⚠️  Other changes ALREADY APPLIED to this same file (Agent 3 fast-apply):\n"
                "Your adapted_new_content MUST be compatible with these. "
                "Do NOT call methods, use fields, or reference symbols that appear in 'Removed lines' below — "
                "they no longer exist in the file.\n\n"
                + "\n\n".join(parts)
                + "\n"
            )

    # Build abstract-class context note.
    abstract_class_note = ""
    if abstract_methods_in_target:
        method_name = _extract_method_name_from_old_content(hunk.get("old_content", ""))
        abstract_class_note = f"""
⚠️  TARGET FILE IS AN ABSTRACT CLASS. The following methods are declared abstract there
(they have NO body; concrete implementations live in subclasses):
  {', '.join(sorted(abstract_methods_in_target))}

Rules:
- NEVER produce a concrete body (return statement, method implementation) for any of
  these abstract methods in adapted_new_content. Abstract declarations must stay abstract.
- If this hunk's only change is a VISIBILITY modifier (e.g. protected→private) on an
  abstract method, it CANNOT apply here — abstract methods must remain overridable.
  In that case output adapted_old_content = adapted_new_content (no change to the line).
- Modifier changes like adding `synchronized` CAN apply to CONCRETE methods only.
  Check the target context: if the method has a body `{{ ... }}` it is concrete; if it
  ends with `;` it is abstract.
"""

    prompt = f"""You are Agent 4 (Namespace Adapter) for OmniPort, a Java patch backporting system.
A patch hunk must be adapted to a different codebase version where symbol names and imports differ.
{same_file_section}{abstract_class_note}
Known symbol renames from localization analysis:
{mappings_text}

─── Hunk change analysis ────────────────────────────────────────────────────────
Hunk type: {hunk_type}

Lines being REMOVED (not in new_content):
{removed_summary}

Lines being ADDED (not in old_content):
{added_summary}

Import changes:
  Imports removed: {imports_removed_summary}
  Imports added:   {imports_added_summary}
─────────────────────────────────────────────────────────────────────────────────
{pure_add_note}
Original hunk — old_content (context + removed lines from the source branch):
```java
{hunk.get("old_content", "")}
```

Original hunk — new_content (context + added lines from the source branch):
```java
{hunk.get("new_content", "")}
```

Target file context — localized region (the code found at the matching position in the target branch):
```java
{loc_result.context_snapshot}
```
{pre_context_section}{post_context_section}
Task:
1. Produce adapted_old_content: the exact code that exists in the TARGET FILE that
   corresponds to what the patch removes (or the insertion point for pure additions).
   It MUST be found verbatim in the target file. Use ALL context sections above
   (before, localized region, after) to determine the full extent of the code to replace.
   IMPORTANT: the localized region may only be a PARTIAL view of the block being changed —
   check the "before" context for code that also needs to be included in adapted_old_content.
2. Produce adapted_new_content: the replacement code in target-branch style.
   Translate the ADDED lines (see "Lines being ADDED" above) into the target API.
   For pure additions, this is adapted_old_content + the new lines.
3. Fix any import statements so they match the target codebase.
4. List any imports that must be added or removed from the target file.

Key rules:
- adapted_old_content MUST exist verbatim in the target file (across the before/localized/after context shown above).
- Only remove lines that appear in "Lines being REMOVED" above — do not remove context lines.
- Preserve the same logical change as the original patch (do not alter program logic).
- If the API has changed (e.g. builder.startObject → ob.xContentObject), use the
  target-branch API style in both adapted_old_content and adapted_new_content.
- CRITICAL — empty adapted_new_content rules (read carefully, hunk-type-specific):
  * PURE REMOVAL hunk: adapted_new_content MUST be empty — the code is being deleted.
  * REPLACEMENT or PURE ADDITION hunk: adapted_new_content MUST NOT be empty.
    For REPLACEMENT hunks, produce the replacement code adapted to the target API.
    Never output an empty adapted_new_content for a REPLACEMENT hunk — doing so
    would delete a method/block that must instead be modified.
- When the original hunk simplifies a method (e.g. removes a version/feature check,
  removes an if/else branch, or inlines a conditional), apply the SAME simplification
  to the target's equivalent method.
  Adapt the inner body to the target-branch API (different field/method names) but
  DO NOT delete the method — produce the simplified method as adapted_new_content.
- API ADAPTATION RULE — field/variable names in adapted_new_content:
  * NEW names (appear in new_content but NOT in old_content): keep them as-is in
    adapted_new_content. The patch is introducing this name; do not substitute it.
  * EXISTING names (appear in BOTH old_content and new_content, i.e. preserved by
    the patch): if the "Target file context" uses a DIFFERENT name for the same
    concept, use the target's name. Example — mainline preserves `fooMap` in both
    old and new, but target shows `barMap` → adapted_new_content must use `barMap`.
    Never copy a mainline field name when the target clearly uses a different name
    for the same field.

Return adapted_old_content and adapted_new_content as valid Java code snippets.
"""

    router = get_default_router()
    balanced_model = router.get_model(LLMTier.BALANCED)
    structured_llm = balanced_model.with_structured_output(NamespaceAdaptationOutput)

    try:
        result: NamespaceAdaptationOutput = structured_llm.invoke(prompt)
        return result
    except Exception as e:
        return NamespaceAdaptationOutput(
            adapted_old_content=hunk.get("old_content", ""),
            adapted_new_content=hunk.get("new_content", ""),
            imports_added=[],
            imports_removed=[],
            notes="",
            success=False,
            error_message=str(e),
        )


# ── LangGraph node ────────────────────────────────────────────────────────────

def namespace_adapter_agent(state: BackportState) -> BackportState:
    """
    LangGraph node: Namespace Adapter.

    Processes unclaimed hunks where the removed code has drifted in the target
    branch (import changes, symbol renames, API changes). Skips hunks already
    claimed by Agent 3. Records adapted hunks for Agent 6 to verify and apply.
    """
    repo_path = state["target_repo_path"]
    hunks = state.get("hunks", [])
    loc_results = state.get("localization_results", [])
    processed_indices: List[int] = list(state.get("processed_hunk_indices", []))
    adapted_hunks: List[Dict[str, Any]] = list(state.get("adapted_hunks", []))
    failed_hunks: List[Dict[str, Any]] = list(state.get("failed_hunks", []))
    retry_contexts: List[PatchRetryContext] = list(state.get("retry_contexts", []))
    tokens_used: int = state.get("tokens_used", 0)

    structural_escalation_indices: List[int] = list(state.get("structural_escalation_indices", []))

    for i, hunk in enumerate(hunks):
        if i in processed_indices:
            continue
        if i >= len(loc_results):
            break

        loc_result = loc_results[i]

        # Skip only completely failed localization (no file found at all).
        if loc_result.method_used == "failed" or not loc_result.file_path:
            continue

        # Read target file for exact-match check and context.
        file_content: Optional[str] = None
        target_path = Path(repo_path) / loc_result.file_path
        if target_path.exists():
            try:
                file_content = target_path.read_text(encoding="utf-8")
            except (IOError, UnicodeDecodeError):
                pass

        # ── Idempotency guard ─────────────────────────────────────────────────
        # Pure-addition hunks where every substantial added line is already
        # present verbatim in the target file are no-ops on this branch
        # (e.g. a constant backported earlier, or a branch where the constant
        # was never removed). Sending such hunks to the LLM causes it to
        # hallucinate duplicate declarations. Claim and skip.
        if file_content:
            diff_pre = _compute_hunk_diff(hunk)
            if diff_pre["is_pure_add"]:
                substantial = [l for l in diff_pre["lines_added"] if len(l.strip()) > 20]
                if substantial and all(l.strip() in file_content for l in substantial):
                    processed_indices.append(i)
                    continue

        if not _should_namespace_adapt(hunk, loc_result, file_content):
            continue

        # ── Abstract-class guard ──────────────────────────────────────────────
        # When the target file is an abstract class, some hunks from the mainline
        # try to modify methods that are ABSTRACT in the target.  Two sub-cases:
        #
        #   SKIP: old_content has a concrete body (the class hierarchy diverged —
        #         mainline kept it concrete in a specialized class, but in the target
        #         branch it was pulled up as abstract into the base; the concrete
        #         impl now lives in a subclass, not here).
        #
        #   KEEP: old_content is itself an abstract declaration (e.g. changing
        #         protected abstract → public abstract).  The change targets the
        #         declaration itself and is valid in the abstract class.
        #
        # We first try the JavaParser microservice for precise modifier info; if it
        # is unavailable we fall back to regex + body-detection heuristics.
        abstract_methods_in_target: Set[str] = set()
        if file_content and _is_abstract_class(file_content) and loc_result.file_path:
            abstract_methods_in_target = _get_abstract_method_names(file_content)
            method_name = _extract_method_name_from_old_content(hunk.get("old_content", ""))
            if method_name:
                # Query the microservice once per hunk (cheap — single file parse,
                # cached by Spring on repeated calls to same file).
                service_info = _query_method_modifiers_from_service(
                    repo_path, loc_result.file_path, [method_name]
                )
                if _should_skip_abstract_hunk(
                    method_name,
                    hunk.get("old_content", ""),
                    service_info,
                    abstract_methods_in_target,
                ):
                    # Silently claim and skip — change does not belong in the
                    # abstract base class on this branch.
                    processed_indices.append(i)
                    continue

        # Claim this hunk.
        processed_indices.append(i)

        # Extract lines before and after the localized region so the LLM
        # has full context. Pre-region is critical ONLY for git_pickaxe results,
        # where the sliding-window may have matched only the inner lines of a
        # larger block (e.g. the outer setup loop was structurally different
        # between branches). For fuzzy/embedding/gumtree/javaparser the
        # context_snapshot is already the correctly-sized window; passing
        # pre_region_context there causes the LLM to over-extend
        # adapted_old_content into surrounding code it should not touch.
        pre_region_context = ""
        post_region_context = ""
        if file_content and loc_result.start_line > 0:
            lines = file_content.splitlines(keepends=True)

            # Always include post-region so the LLM knows what already exists
            # after the replaced block (prevents it from re-emitting that code).
            if loc_result.end_line > 0:
                post_start = loc_result.end_line  # end_line is 1-indexed; next line index
                post_end = min(len(lines), post_start + 20)
                post_region_context = "".join(lines[post_start:post_end])

            # Pre-region context only for git_pickaxe: its window may be too narrow.
            if loc_result.method_used == "git_pickaxe":
                pre_start = max(0, loc_result.start_line - 21)
                pre_end = loc_result.start_line - 1
                pre_region_context = "".join(lines[pre_start:pre_end])

        # Collect other hunks on the same file that were already fast-applied by Agent 3,
        # so the LLM knows which symbols/methods have been removed and can't be referenced.
        current_file = loc_result.file_path
        same_file_applied: List[Dict[str, Any]] = [
            hunks[j]
            for j in processed_indices
            if j != i and j < len(hunks) and hunks[j].get("file_path") == current_file
        ]

        output = _adapt_with_llm(
            hunk, loc_result, pre_region_context, post_region_context,
            same_file_applied,
            abstract_methods_in_target=abstract_methods_in_target or None,
        )

        diff = _compute_hunk_diff(hunk)

        # Safety guard 1: if the LLM returned an empty adapted_new_content for a
        # non-pure-removal hunk, it misidentified the change as a deletion.
        # Safety guard 2: if adapted_new_content is suspiciously similar to the
        # mainline new_content (ratio > 0.85) for a substantial non-import hunk AND
        # contains identifiers not found in the target file, the LLM copied the
        # mainline body verbatim instead of adapting to the target API.
        # In both cases, escalate to Agent 5 (structural refactor).
        should_escalate = False
        if output.success and not output.adapted_new_content.strip() and not diff["is_pure_remove"]:
            should_escalate = True
        elif output.success and not diff["imports_added"] and not diff["imports_removed"] and file_content:
            norm_adapted = " ".join(output.adapted_new_content.split())
            norm_new = " ".join(hunk.get("new_content", "").split())
            if len(norm_adapted) > 100 and SequenceMatcher(None, norm_adapted, norm_new).ratio() > 0.85:
                # Additional check: are there mainline-specific identifiers (>6 chars)
                # in adapted_new_content that don't appear in the target file? If so,
                # the LLM used mainline API names without adapting to the target.
                adapted_ids = set(re.findall(r'\b[a-zA-Z][a-zA-Z0-9]{5,}\b', output.adapted_new_content))
                if any(aid not in file_content for aid in adapted_ids):
                    should_escalate = True

        if should_escalate:
            structural_escalation_indices.append(i)
            # Un-claim so Agent 5 can process it.
            processed_indices.remove(i)
            continue

        if output.success:
            adapted_hunks.append({
                **hunk,
                "old_content": output.adapted_old_content,
                "new_content": output.adapted_new_content,
                "imports_added": output.imports_added,
                "imports_removed": output.imports_removed,
                "adapted": True,
                "loc_index": i,
            })
        else:
            failed_hunks.append({**hunk, "error": output.error_message})
            retry_contexts.append(
                PatchRetryContext(
                    error_type="namespace_adaptation_failed",
                    error_message=output.error_message or "LLM adaptation failed",
                    attempt_count=state.get("current_attempt", 1),
                    suggested_action="structural_refactor",
                )
            )

    state["adapted_hunks"] = adapted_hunks
    state["failed_hunks"] = failed_hunks
    state["processed_hunk_indices"] = processed_indices
    state["structural_escalation_indices"] = structural_escalation_indices
    state["retry_contexts"] = retry_contexts
    state["tokens_used"] = tokens_used
    state["current_attempt"] = state.get("current_attempt", 1) + 1
    return state
