import re
from typing import Dict, Any
from src.core.state import BackportState, PatchClassification, PatchType
from src.core.llm_router import get_default_router, LLMTier
from src.tools.preprocessor import is_auto_generated_file


def classify_patch(state: BackportState) -> BackportState:
    """
    Agent 2: Patch Classifier
    Classifies the patch/hunks (TYPE I-V), assigns confidence, and estimates token budgets.
    Uses the original patch and the localization evidence from Agent 1.

    Auto-generated file detection runs BEFORE the LLM call to avoid wasting tokens.
    Classifier output is for observability/metrics only — downstream agents do NOT
    read state["classification"] for routing decisions.
    """
    patch_content = state["patch_content"]
    localization_results = state.get("localization_results", [])

    # Pre-LLM check: skip expensive classification for auto-generated files.
    if is_auto_generated_file(patch_content):
        state["classification"] = PatchClassification(
            patch_type=PatchType.TYPE_I,
            confidence=1.0,
            token_budget_estimate=0,
            reasoning="File is auto-generated; skipping LLM classification.",
            is_auto_generated=True,
        )
        return state

    # Format localization evidence for the prompt.
    evidence_lines = [
        "Localization Evidence (how each hunk was found in the target branch):"
    ]
    for i, res in enumerate(localization_results):
        evidence_lines.append(
            f"  Hunk {i + 1}: found in {res.file_path} "
            f"via '{res.method_used}' with confidence {res.confidence:.2f}."
        )
    evidence_str = "\n".join(evidence_lines)

    router = get_default_router()
    fast_model = router.get_model(LLMTier.FAST)
    structured_llm = fast_model.with_structured_output(PatchClassification)

    prompt = f"""You are Agent 2 (Patch Classifier) for the OmniPort backporting system.
Analyze the following git unified diff alongside the localization evidence.
The complexity of a backport is defined by how the patch applies to the target branch.

Classification Types:
- TYPE_I: Exact match string replacement, trivial backport (often found via 'git_exact').
- TYPE_II: Whitespace/formatting drift, requires fuzzy matching (often found via 'fuzzy' or 'git_pickaxe').
- TYPE_III: Structural changes, renamed methods, namespace changes (often found via 'gumtree_ast' or 'javaparser').
- TYPE_IV: Deep structural refactoring, significant API divergence.
- TYPE_V: Complete redesign, fundamentally different implementation (often found via 'embedding' fallback).
- MIXED: Different hunks have very different complexities.

Assess the confidence (0.0 to 1.0) and estimate the token budget required to resolve this patch.

Original Patch:
{patch_content}

{evidence_str}
"""

    classification_result = structured_llm.invoke(prompt)

    # Track token usage if the model returns usage metadata.
    usage = getattr(classification_result, "usage_metadata", None) or {}
    tokens_used = state.get("tokens_used", 0) + usage.get("total_tokens", 0)
    state["tokens_used"] = tokens_used

    state["classification"] = classification_result
    return state
