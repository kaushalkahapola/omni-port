"""
Agent 4: Namespace Adapter (Balanced LLM)

Routed when import/namespace changes are detected in localization evidence:
  - method_used is 'javaparser' or 'gumtree_ast' AND symbol_mappings is non-empty, OR
  - the hunk itself contains differing import statements.

Uses the Balanced LLM to rewrite imports and symbol references, guided by
symbol_mappings from the localization stage.
"""

from typing import Dict, List, Any, Optional
from pydantic import BaseModel, Field
from src.core.state import BackportState, LocalizationResult, PatchRetryContext
from src.core.llm_router import get_default_router, LLMTier


# ── Output model ──────────────────────────────────────────────────────────────

class NamespaceAdaptationOutput(BaseModel):
    adapted_old_content: str = Field(
        description="old_content with symbol renames and import fixes applied"
    )
    adapted_new_content: str = Field(
        description="new_content with symbol renames and import fixes applied"
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


def _should_namespace_adapt(hunk: Dict[str, Any], loc_result: LocalizationResult) -> bool:
    """
    True when the hunk needs namespace/import adaptation:
      - Localization found symbol renames via javaparser/gumtree_ast, OR
      - The hunk diff itself contains differing import statements.
    """
    has_symbol_mappings = bool(loc_result.symbol_mappings)
    method_with_mappings = loc_result.method_used in _NAMESPACE_METHODS and has_symbol_mappings
    return method_with_mappings or _has_import_changes(hunk)


# ── LLM-backed adaptation ─────────────────────────────────────────────────────

def _adapt_with_llm(
    hunk: Dict[str, Any],
    loc_result: LocalizationResult,
) -> NamespaceAdaptationOutput:
    """
    Calls the Balanced LLM to rewrite imports and symbol references.
    """
    mappings_text = "\n".join(
        f"  - {orig} -> {target}"
        for orig, target in loc_result.symbol_mappings.items()
    ) or "  (none detected by localization; infer from diff context)"

    prompt = f"""You are Agent 4 (Namespace Adapter) for OmniPort, a Java patch backporting system.
A patch hunk must be adapted to a different codebase version where symbol names and imports differ.

Known symbol renames from localization analysis:
{mappings_text}

Original hunk — old_content (lines being replaced in the target):
```java
{hunk.get("old_content", "")}
```

Original hunk — new_content (replacement to apply):
```java
{hunk.get("new_content", "")}
```

Target file context (surrounding code in the target branch):
```java
{loc_result.context_snapshot}
```

Task:
1. Apply the known symbol renames to both old_content and new_content.
2. Fix any import statements so they match the target codebase.
3. List any imports that must be added or removed from the target file.
4. Do NOT change logic — only rename symbols and fix namespacing.

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

    Processes hunks that require import/namespace adaptation.
    Skips hunks already claimed by Agent 3.
    Records adapted hunks for Agent 6 (Hunk Synthesizer) to verify and apply.
    """
    hunks = state.get("hunks", [])
    loc_results = state.get("localization_results", [])
    processed_indices: List[int] = list(state.get("processed_hunk_indices", []))
    adapted_hunks: List[Dict[str, Any]] = list(state.get("adapted_hunks", []))
    failed_hunks: List[Dict[str, Any]] = list(state.get("failed_hunks", []))
    retry_contexts: List[PatchRetryContext] = list(state.get("retry_contexts", []))
    tokens_used: int = state.get("tokens_used", 0)

    for i, hunk in enumerate(hunks):
        if i in processed_indices:
            continue
        if i >= len(loc_results):
            break

        loc_result = loc_results[i]
        if not _should_namespace_adapt(hunk, loc_result):
            continue

        # Claim this hunk.
        processed_indices.append(i)

        output = _adapt_with_llm(hunk, loc_result)

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
    state["retry_contexts"] = retry_contexts
    state["tokens_used"] = tokens_used
    state["current_attempt"] = state.get("current_attempt", 1) + 1
    return state
