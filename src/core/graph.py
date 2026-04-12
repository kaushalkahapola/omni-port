"""
OmniPort LangGraph State Machine

Wires agents 0–9 into a pipeline with HunkRouter-driven conditional entry
and a bounded fallback retry loop.

Pipeline structure:

  code_localizer (+ hunk segregation)
    → patch_classifier
      → hunk_router (sets routing_decision)
        ┌── fast_apply          (git_exact / fuzzy_text, high confidence)
        ├── namespace_adapter   (javaparser / import changes)
        └── structural_refactor (gumtree_ast / low confidence)
          → hunk_synthesizer
            → syntax_repair          ← Agent 8: pre-validate syntax, LLM-fix if broken
                ├── validator        (syntax clean/repaired → run build + tests)
                │     ├── END        (pass, infra error, or fallback_attempts ≥ 2)
                │     └── fallback_agent  ← Agent 9: description-driven re-application
                │           └── syntax_repair  (loop, max 2 fallback rounds)
                └── fallback_agent   (syntax still broken → skip validator, go straight to fallback)

HunkRouter chooses the FIRST processing agent based on the dominant
localization method. Agents not selected first still run after and process
their own eligible hunks (via processed_hunk_indices filtering).

Agent 1 (code_localizer) also segregates the input hunks:
  - state["hunks"]               → Java code hunks → LLM pipeline
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
from src.agents.agent8_syntax_repair import syntax_repair_agent
from src.agents.agent9_fallback import fallback_agent_node
from src.agents.hunk_router import route_hunks, select_entry_agent


def route_after_syntax_repair(state: BackportState) -> str:
    """
    Conditional edge from syntax_repair.

    If repair failed (syntax errors remain after LLM attempts), there is no
    point running the build — the compiler will just fail on broken Java.
    Go directly to the fallback agent instead.  Once all fallback attempts are
    exhausted, exit to END.

    Otherwise (status is "clean", "repaired", or "skipped") → validator.
    """
    if state.get("syntax_repair_status") == "failed":
        if state.get("fallback_attempts", 0) < 2:
            return "fallback_agent"
        return "end"
    return "validator"


def route_after_validation(state: BackportState) -> str:
    """
    Conditional edge from the validator.

    Routes to "fallback_agent" when:
      - validation failed (validation_passed is False), AND
      - the failure is not a transient infrastructure issue, AND
      - fewer than 2 fallback attempts have been made.

    Routes to "end" in all other cases (success, infra error, max retries).
    """
    if state.get("validation_passed", False):
        return "end"
    if state.get("validation_failure_category") == "infrastructure":
        return "end"
    if state.get("fallback_attempts", 0) >= 2:
        return "end"
    return "fallback_agent"


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
    graph.add_node("syntax_repair", syntax_repair_agent)    # Agent 8
    graph.add_node("validator", run_validation)
    graph.add_node("fallback_agent", fallback_agent_node)   # Agent 9

    # ── Edges ──────────────────────────────────────────────────────────────────
    graph.add_edge(START, "code_localizer")
    graph.add_edge("code_localizer", "patch_classifier")
    graph.add_edge("patch_classifier", "hunk_router")

    # Conditional entry: HunkRouter picks the first processing agent.
    graph.add_conditional_edges(
        "hunk_router",
        select_entry_agent,
        {
            "fast_apply": "fast_apply",
            "namespace_adapter": "namespace_adapter",
            "structural_refactor": "structural_refactor",
        },
    )

    # After fast_apply, remaining hunks flow to namespace_adapter then structural.
    graph.add_edge("fast_apply", "namespace_adapter")
    graph.add_edge("namespace_adapter", "structural_refactor")
    graph.add_edge("structural_refactor", "hunk_synthesizer")

    # Synthesizer feeds syntax_repair (Agent 8) before validation.
    graph.add_edge("hunk_synthesizer", "syntax_repair")

    # If syntax repair fixed (or found no) errors → validator.
    # If syntax repair failed (errors remain) → skip the build, go to fallback.
    graph.add_conditional_edges(
        "syntax_repair",
        route_after_syntax_repair,
        {
            "validator": "validator",
            "fallback_agent": "fallback_agent",
            "end": END,
        },
    )

    # Validator either exits or triggers the fallback loop.
    graph.add_conditional_edges(
        "validator",
        route_after_validation,
        {
            "fallback_agent": "fallback_agent",
            "end": END,
        },
    )

    # Fallback agent re-enters through syntax_repair; the same conditional
    # routing applies: fixed → validator, still broken → another fallback round.
    graph.add_edge("fallback_agent", "syntax_repair")

    return graph.compile()


def build_validator_fallback_graph():
    """
    Post-synthesis subgraph used by the shadow-run harness.

    The harness runs agents 1–6 manually, then Agent 8 (syntax_repair)
    separately (before pre-applying hunks to disk and capturing generated.patch).
    This subgraph handles the rest: validation and any fallback retries.

    Entry routing (conditional from START):
      syntax_repair_status == "failed"  → fallback_agent immediately
                                          (no point running the build on broken Java)
      otherwise                         → validator

    Fallback loop:
      fallback_agent → syntax_repair → [validator | fallback_agent | END]
                                         ↑ same route_after_syntax_repair logic
    """
    graph = StateGraph(BackportState)

    graph.add_node("validator", run_validation)
    graph.add_node("fallback_agent", fallback_agent_node)
    graph.add_node("syntax_repair", syntax_repair_agent)

    # Entry: honour the syntax_repair_status that Agent 8 set before the graph.
    graph.add_conditional_edges(
        START,
        route_after_syntax_repair,
        {
            "validator": "validator",
            "fallback_agent": "fallback_agent",
            "end": END,
        },
    )

    # Validator exits or triggers fallback.
    graph.add_conditional_edges(
        "validator",
        route_after_validation,
        {
            "fallback_agent": "fallback_agent",
            "end": END,
        },
    )

    # Fallback generates new hunks → syntax_repair checks them.
    graph.add_edge("fallback_agent", "syntax_repair")

    # Same routing as the main graph: fixed → validator, broken → another fallback/end.
    graph.add_conditional_edges(
        "syntax_repair",
        route_after_syntax_repair,
        {
            "validator": "validator",
            "fallback_agent": "fallback_agent",
            "end": END,
        },
    )

    return graph.compile()


# Module-level compiled graph — import and call .invoke(initial_state).
app = build_graph()
