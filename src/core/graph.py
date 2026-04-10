"""
OmniPort LangGraph State Machine

Wires agents 0–6 into a linear pipeline with conditional terminal routing.
HunkRouter logic is embedded in each agent's node function (agents filter their
own eligible hunks using processed_hunk_indices), keeping the graph simple while
preserving the correct precedence:

  Agent 3 (git_exact / git_pickaxe, high confidence)
    → Agent 4 (import/namespace changes)
      → Agent 5 (gumtree_ast or low confidence)
        → Agent 6 (synthesize remaining + adapted + refactored)

The Send-API parallel fan-out described in the architecture plan is a Phase 3
enhancement; this graph handles the sequential-pipeline baseline.
"""

from langgraph.graph import StateGraph, START, END

from src.core.state import BackportState
from src.agents.agent1_localizer import localize_hunks
from src.agents.agent2_classifier import classify_patch
from src.agents.agent3_fastapply import fast_apply_agent
from src.agents.agent4_namespace import namespace_adapter_agent
from src.agents.agent5_structural import structural_refactor_agent
from src.agents.agent6_synthesizer import hunk_synthesizer_agent


def _should_continue_after_synthesis(state: BackportState) -> str:
    """
    Conditional edge after Agent 6.
    If synthesis fully succeeded, we're done (Agent 7 not yet implemented).
    On partial/failed synthesis, still terminate gracefully — retry loop
    (Agent 7) will be wired here in Phase 2.
    """
    return END


def build_graph():
    """
    Builds and compiles the OmniPort LangGraph state machine.
    Returns a compiled CompiledGraph ready for .invoke() / .stream().
    """
    graph = StateGraph(BackportState)

    # ── Nodes ─────────────────────────────────────────────────────────────────
    graph.add_node("code_localizer", localize_hunks)
    graph.add_node("patch_classifier", classify_patch)
    graph.add_node("fast_apply", fast_apply_agent)
    graph.add_node("namespace_adapter", namespace_adapter_agent)
    graph.add_node("structural_refactor", structural_refactor_agent)
    graph.add_node("hunk_synthesizer", hunk_synthesizer_agent)

    # ── Edges ──────────────────────────────────────────────────────────────────
    # Linear pipeline: each agent passes full state to the next.
    graph.add_edge(START, "code_localizer")
    graph.add_edge("code_localizer", "patch_classifier")
    graph.add_edge("patch_classifier", "fast_apply")
    graph.add_edge("fast_apply", "namespace_adapter")
    graph.add_edge("namespace_adapter", "structural_refactor")
    graph.add_edge("structural_refactor", "hunk_synthesizer")
    graph.add_edge("hunk_synthesizer", END)

    return graph.compile()


# Module-level compiled graph — import and call .invoke(initial_state).
app = build_graph()
