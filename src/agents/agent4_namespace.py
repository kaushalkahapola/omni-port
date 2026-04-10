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
from typing import Dict, List, Any, Optional
from pathlib import Path
from difflib import SequenceMatcher
from pydantic import BaseModel, Field
from src.core.state import BackportState, LocalizationResult, PatchRetryContext
from src.core.llm_router import get_default_router, LLMTier


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

    prompt = f"""You are Agent 4 (Namespace Adapter) for OmniPort, a Java patch backporting system.
A patch hunk must be adapted to a different codebase version where symbol names and imports differ.

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

        if not _should_namespace_adapt(hunk, loc_result, file_content):
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

        output = _adapt_with_llm(hunk, loc_result, pre_region_context, post_region_context)

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
