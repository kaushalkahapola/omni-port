"""
HunkRouter — per-hunk routing node for the LangGraph state machine.

Reads localization_results and determines the primary processing path:
  - git_exact / fuzzy_text + confidence ≥ 0.85  → fast_apply
  - javaparser or import-heavy hunks             → namespace_adapter
  - gumtree_ast or confidence < 0.6              → structural_refactor
  - mixed                                        → namespace_adapter (safe default)

Because agents 3–5 already filter by processed_hunk_indices, this router only
selects the FIRST agent in the chain for the majority of hunks. Agents not
selected first still run and handle their eligible hunks.

The routing decision is stored in state["routing_decision"] so it can be used
by graph.py's conditional edge to pick the entry point.
"""

from typing import Dict
from src.core.state import BackportState, LocalizationResult


def route_hunks(state: BackportState) -> Dict:
    """
    Analyze localization results and return a routing_decision string:
      "fast_apply"        — majority of hunks are high-confidence exact matches
      "namespace_adapter" — majority need import/symbol remapping
      "structural_refactor" — majority need deep structural analysis
    """
    results: list[LocalizationResult] = state.get("localization_results", [])

    if not results:
        return {"routing_decision": "structural_refactor"}

    fast_count = sum(
        1 for r in results
        if r.method_used in ("git_exact", "fuzzy_text") and r.confidence >= 0.85
    )
    namespace_count = sum(
        1 for r in results
        if r.method_used == "javaparser"
    )
    structural_count = sum(
        1 for r in results
        if r.method_used == "gumtree_ast" or r.confidence < 0.6
    )

    total = len(results)

    # Choose entry point based on which category dominates
    if fast_count == total:
        decision = "fast_apply"
    elif structural_count > total // 2:
        decision = "structural_refactor"
    elif namespace_count > 0:
        decision = "namespace_adapter"
    elif fast_count > 0:
        decision = "fast_apply"
    else:
        decision = "structural_refactor"

    return {"routing_decision": decision}


def select_entry_agent(state: BackportState) -> str:
    """
    Conditional edge function: returns the name of the first processing agent
    based on routing_decision set by route_hunks().
    """
    return state.get("routing_decision", "structural_refactor")
