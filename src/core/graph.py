"""
OmniPort LangGraph State Machine

Wires agents 0–7 into a pipeline with HunkRouter-driven conditional entry.

Pipeline structure:

  code_localizer (+ hunk segregation)
    → patch_classifier
      → hunk_router (sets routing_decision)
        ┌── fast_apply          (git_exact / fuzzy_text, high confidence)
        ├── namespace_adapter   (javaparser / import changes)
        └── structural_refactor (gumtree_ast / low confidence)
          → hunk_synthesizer → validator → END

HunkRouter chooses the FIRST processing agent based on the dominant
localization method. Agents not selected first still run after and process
their own eligible hunks (via processed_hunk_indices filtering).

Agent 1 (code_localizer) also segregates the input hunks:
  - state["hunks"]              → Java code hunks → LLM pipeline
  - state["developer_aux_hunks"] → test/non-Java/auto-gen → direct apply in validator

The Send-API per-hunk parallel fan-out is a Phase 3 enhancement; this graph
handles the conditional-entry baseline.
"""

from langgraph.graph import StateGraph, START, END

from src.core.state import BackportState
from src.agents.agent1_localizer import localize_hunks
from src.agents.agent2_classifier import classify_patch
from src.agents.agent3_fastapply import fast_apply_agent
from src.agents.agent4_namespace import namespace_adapter_agent
from src.agents.agent5_structural import structural_refactor_agent
from src.agents.agent6_synthesizer import hunk_synthesizer_agent
from src.agents.agent7_validator import run_validation
from src.agents.hunk_router import route_hunks, select_entry_agent


def build_graph():
    """
    Builds and compiles the OmniPort LangGraph state machine.
    Returns a compiled CompiledGraph ready for .invoke() / .stream().
    """
    graph = StateGraph(BackportState)

    # ── Nodes ─────────────────────────────────────────────────────────────────
    graph.add_node("code_localizer", localize_hunks)
    graph.add_node("patch_classifier", classify_patch)
    graph.add_node("hunk_router", route_hunks)
    graph.add_node("fast_apply", fast_apply_agent)
    graph.add_node("namespace_adapter", namespace_adapter_agent)
    graph.add_node("structural_refactor", structural_refactor_agent)
    graph.add_node("hunk_synthesizer", hunk_synthesizer_agent)
    graph.add_node("validator", run_validation)

    # ── Edges ──────────────────────────────────────────────────────────────────
    graph.add_edge(START, "code_localizer")
    graph.add_edge("code_localizer", "patch_classifier")
    graph.add_edge("patch_classifier", "hunk_router")

    # Conditional entry: HunkRouter picks the first processing agent
    graph.add_conditional_edges(
        "hunk_router",
        select_entry_agent,
        {
            "fast_apply": "fast_apply",
            "namespace_adapter": "namespace_adapter",
            "structural_refactor": "structural_refactor",
        },
    )

    # After fast_apply, remaining hunks flow to namespace_adapter then structural
    graph.add_edge("fast_apply", "namespace_adapter")
    graph.add_edge("namespace_adapter", "structural_refactor")
    graph.add_edge("structural_refactor", "hunk_synthesizer")
    graph.add_edge("hunk_synthesizer", "validator")
    graph.add_edge("validator", END)

    return graph.compile()


# Module-level compiled graph — import and call .invoke(initial_state).
app = build_graph()
