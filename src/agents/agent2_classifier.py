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
    """
    patch_content = state["patch_content"]
    localization_results = state.get("localization_results", [])

    # Format localization evidence
    evidence_str = (
        "Localization Evidence (How each hunk was found in the target branch):\n"
    )
    for i, res in enumerate(localization_results):
        evidence_str += f"Hunk {i + 1}: Found in {res.file_path} via method '{res.method_used}' with confidence {res.confidence:.2f}.\n"

    # LLM classification
    router = get_default_router()
    fast_model = router.get_model(LLMTier.FAST)

    # We use LangChain's structured output
    structured_llm = fast_model.with_structured_output(PatchClassification)

    prompt = f"""You are Agent 2 (Patch Classifier) for the OmniPort backporting system.
Analyze the following git unified diff alongside the localization evidence. 
The complexity of a backport is defined by how the patch applies to the target branch.

Classification Types:
- TYPE_I: Exact match string replacement, trivial backport (often found via 'git_exact').
- TYPE_II: Whitespace/formatting drift, requires fuzzy matching (often found via 'fuzzy' or 'git_pickaxe').
- TYPE_III: Structural changes, renamed methods, namespace changes (often found via 'gumtree' or 'javaparser').
- TYPE_IV: Deep structural refactoring, significant API divergence.
- TYPE_V: Complete redesign, fundamentally different implementation (often found via 'embedding' fallback).
- MIXED: Different hunks have very different complexities.

Assess the confidence (0.0 to 1.0) and estimate the token budget required to resolve this patch.

Original Patch:
{patch_content}

{evidence_str}
"""

    classification_result = structured_llm.invoke(prompt)

    is_auto_generated = is_auto_generated_file(patch_content)
    if is_auto_generated:
        classification_result.is_auto_generated = True

    state["classification"] = classification_result

    return state
