"""
Phase 0: Optimistic Fast-Path Patching Node

Before spinning up the expensive 4-agent LLM pipeline, Retrofit first tries
a direct `git apply --check` on the mainline patch against the target repo.
If it applies cleanly *and* builds/tests pass, we exit immediately without
burning any LLM tokens.

This logic was previously embedded inside `reasoning_agent.py`. It is now
a standalone pre-graph node so the graph can route on `fast_path_success`.
"""

import os
import json
import subprocess
from langchain_core.messages import HumanMessage
from state import AgentState
from utils.patch_analyzer import PatchAnalyzer
from utils.patch_complexity import classify_patch_complexity
from agents.validation_tools import ValidationToolkit


def _format_transition_summary(transition_eval: dict) -> str:
    if not transition_eval:
        return "No transition evaluation available."

    fail_to_pass = transition_eval.get("fail_to_pass", []) or []
    newly_passing = transition_eval.get("newly_passing", []) or []
    pass_to_fail = transition_eval.get("pass_to_fail", []) or []
    reason = transition_eval.get("reason", "Unknown reason.")

    return (
        f"reason={reason}; "
        f"fail->pass({len(fail_to_pass)}): {fail_to_pass}; "
        f"newly_passing({len(newly_passing)}): {newly_passing}; "
        f"pass->fail({len(pass_to_fail)}): {pass_to_fail}"
    )


def _phase0_cache_dir() -> str:
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    default_dir = os.path.join(root_dir, "evaluate", "full_run", "phase0_cache")
    return os.getenv("PHASE0_CACHE_DIR", default_dir)


def _phase0_cache_file(project: str, backport_commit: str, original_commit: str) -> str:
    safe_project = (project or "unknown").strip().lower() or "unknown"
    safe_backport = (backport_commit or "unknown").strip() or "unknown"
    safe_original = (original_commit or "unknown").strip() or "unknown"
    filename = f"{safe_project}_{safe_backport[:12]}_{safe_original[:12]}.json"
    return os.path.join(_phase0_cache_dir(), filename)


def _load_phase0_cache(
    project: str, backport_commit: str, original_commit: str
) -> dict | None:
    cache_file = _phase0_cache_file(project, backport_commit, original_commit)
    if not os.path.exists(cache_file):
        return None
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _is_phase0_cache_reusable(cached: dict | None) -> tuple[bool, str]:
    payload = cached or {}
    baseline = payload.get("phase_0_baseline_test_result") or {}
    transition = payload.get("phase_0_transition_evaluation") or {}

    baseline_mode = (baseline.get("mode") or "").strip().lower()
    baseline_total = int(
        (baseline.get("test_state") or {}).get("summary", {}).get("total", 0) or 0
    )
    transition_reason = (transition.get("reason") or "").strip().lower()

    if baseline_mode == "baseline-apply-failed":
        return False, "baseline-apply-failed"
    if baseline_total == 0 and "no fail-to-pass or newly passing" in transition_reason:
        return False, "empty-baseline-and-empty-transition"
    if "inconclusive: relevant target tests were not observed" in transition_reason:
        return False, "inconclusive-target-tests-not-observed"
    return True, "ok"


def _save_phase0_cache(
    project: str, backport_commit: str, original_commit: str, payload: dict
) -> None:
    cache_dir = _phase0_cache_dir()
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = _phase0_cache_file(project, backport_commit, original_commit)
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
    except Exception as e:
        print(f"Phase 0: Warning - failed to write cache file: {e}")


async def phase_0_optimistic(state: AgentState, config) -> dict:
    """
    Phase 0: The Fast-Path Direct Application Node.

    Responsibilities:
      1. Load and parse the mainline patch diff.
      2. Initialise the EnsembleRetriever (and handle experiment-mode checkout).
      3. Run `git apply --check` via ReasoningToolkit.check_patch_applicability().
      4. Set `fast_path_success` = True/False in state.
         - True  -> graph routes directly to END (no LLM agents needed).
         - False -> graph routes into the full 4-agent pipeline.

    On fast-path success this node also populates `patch_analysis` and
    `implementation_plan` (a simplified direct-apply plan) so the downstream
    validation agent has something to work with if needed.
    """
    print("Phase 0 (Optimistic): Checking direct patch applicability...")

    # ------------------------------------------------------------------
    # 1. Load the patch diff
    # ------------------------------------------------------------------
    patch_path = state.get("patch_path")
    if not patch_path or not os.path.exists(patch_path):
        return {
            "messages": [
                HumanMessage(content="Error: Patch file not found in Phase 0")
            ],
            "fast_path_success": False,
        }

    with open(patch_path, "r", encoding="utf-8") as f:
        diff_text = f.read()

    analyzer = PatchAnalyzer()
    all_changes = analyzer.analyze(diff_text, with_test_changes=True)
    # For agentic phases (1-4), evaluate only non-test Java code changes.
    java_code_changes = [
        c for c in all_changes if c.file_path.endswith(".java") and not c.is_test_file
    ]

    # ------------------------------------------------------------------
    # 2. Handle experiment mode checkout
    # ------------------------------------------------------------------
    target_repo_path = state.get("target_repo_path")
    experiment_mode = state.get("experiment_mode", False)
    backport_commit = state.get("backport_commit")
    original_commit = state.get("original_commit")
    project_name = os.path.basename(target_repo_path or "").strip().lower() or "unknown"
    use_phase0_cache = state.get("use_phase_0_cache", True)

    validation_toolkit = ValidationToolkit(target_repo_path)

    complexity_info = classify_patch_complexity(
        patch_diff=diff_text,
        target_repo_path=target_repo_path,
        with_test_changes=state.get("with_test_changes", False),
    )
    patch_complexity = str(complexity_info.get("complexity") or "REWRITE")
    complexity_reason = str(complexity_info.get("reason") or "unknown")
    complexity_details = complexity_info.get("details") or {}
    relevant_changed_files = [c.file_path for c in all_changes if c.file_path]
    test_targets = validation_toolkit.detect_relevant_test_targets_from_changed_files(
        relevant_changed_files, project=project_name
    )

    if use_phase0_cache and experiment_mode and backport_commit and original_commit:
        cached = _load_phase0_cache(project_name, backport_commit, original_commit)
        if cached:
            reusable, reason = _is_phase0_cache_reusable(cached)
            if not reusable:
                print(
                    f"Phase 0: Ignoring stale cache (reason={reason}); recomputing Phase 0."
                )
                cached = None
        if cached:
            cached_transition = cached.get("phase_0_transition_evaluation", {})
            transition_summary = _format_transition_summary(cached_transition)
            print("Phase 0: Using cached Phase 0 results.")
            return {
                "messages": [
                    HumanMessage(
                        content=(
                            "Phase 0: Loaded cached results. "
                            f"Transition summary: {transition_summary}"
                        )
                    )
                ],
                "patch_analysis": java_code_changes,
                "patch_diff": diff_text,
                "fast_path_success": bool(cached.get("fast_path_success", False)),
                "phase_0_test_targets": cached.get(
                    "phase_0_test_targets", test_targets
                ),
                "phase_0_baseline_test_result": cached.get(
                    "phase_0_baseline_test_result", {}
                ),
                "phase_0_post_patch_test_result": cached.get(
                    "phase_0_post_patch_test_result", {}
                ),
                "phase_0_transition_evaluation": cached_transition,
                "patch_complexity": patch_complexity,
                "complexity_reason": complexity_reason,
                "complexity_details": complexity_details,
            }

    if experiment_mode and backport_commit:
        print(f"Phase 0: Experiment mode — checking out parent of {backport_commit}...")
        try:
            subprocess.run(
                ["git", "checkout", f"{backport_commit}^"],
                cwd=target_repo_path,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            print(f"Phase 0: Checkout failed: {e.stderr}")
            return {
                "messages": [
                    HumanMessage(content=f"Phase 0 checkout error: {e.stderr}")
                ],
                "fast_path_success": False,
                "patch_analysis": java_code_changes,
                "patch_diff": diff_text,
            }

    phase0_baseline_test_result = {
        "success": True,
        "compile_error": False,
        "output": "Baseline step skipped: no developer test hunks.",
        "failed_tests": [],
        "mode": "baseline-skip",
        "targets": test_targets,
        "test_state": {
            "xml_reports": [],
            "target_classes": [],
            "test_cases": {},
            "classes": {},
            "summary": {"passed": 0, "failed": 0, "skipped": 0, "total": 0},
        },
    }

    # ------------------------------------------------------------------
    # 3. Attempt direct patch application check
    # ------------------------------------------------------------------
    try:
        check_result = subprocess.run(
            ["git", "apply", "--check", patch_path],
            cwd=target_repo_path,
            capture_output=True,
            text=True,
        )
        is_applicable = check_result.returncode == 0
        output = (
            check_result.stdout
            if is_applicable
            else (check_result.stderr or check_result.stdout)
        )
    except Exception as e:
        is_applicable = False
        output = str(e)

    if is_applicable:
        print(
            "Phase 0: git apply --check SUCCEEDED. Attempting actual patch application and test run."
        )

        # Actually apply the patch
        patch_apply_result = None
        try:
            patch_apply_result = subprocess.run(
                ["git", "apply", patch_path],
                capture_output=True,
                text=True,
                cwd=target_repo_path,
            )
        except Exception as e:
            print(f"Phase 0: Exception during git apply: {e}")
            return {
                "messages": [
                    HumanMessage(content=f"Phase 0: Exception during git apply: {e}")
                ],
                "patch_analysis": java_code_changes,
                "patch_diff": diff_text,
                "fast_path_success": False,
                "patch_complexity": patch_complexity,
                "complexity_reason": complexity_reason,
                "complexity_details": complexity_details,
            }

        if patch_apply_result is None or patch_apply_result.returncode != 0:
            print(
                f"Phase 0: git apply failed: {patch_apply_result.stderr if patch_apply_result else 'Unknown error'}"
            )
            return {
                "messages": [
                    HumanMessage(
                        content=f"Phase 0: git apply failed: {patch_apply_result.stderr if patch_apply_result else 'Unknown error'}"
                    )
                ],
                "patch_analysis": java_code_changes,
                "patch_diff": diff_text,
                "fast_path_success": False,
                "patch_complexity": patch_complexity,
                "complexity_reason": complexity_reason,
                "complexity_details": complexity_details,
            }

        # Build and run relevant tests against the fully applied mainline patch.
        build_result = validation_toolkit.run_build_script()
        if not build_result.get("success"):
            print("Phase 0: Build failed after patch apply. Rolling back.")
            validation_toolkit.restore_repo_state()
            transition_eval = {
                "valid_backport_signal": False,
                "reason": "Invalid: Build failed after patch apply.",
                "fail_to_pass": [],
                "pass_to_fail": [],
                "newly_passing": [],
            }

            if (
                use_phase0_cache
                and experiment_mode
                and backport_commit
                and original_commit
            ):
                _save_phase0_cache(
                    project_name,
                    backport_commit,
                    original_commit,
                    {
                        "fast_path_success": False,
                        "phase_0_test_targets": test_targets,
                        "phase_0_baseline_test_result": phase0_baseline_test_result,
                        "phase_0_post_patch_test_result": {},
                        "phase_0_transition_evaluation": transition_eval,
                        "phase_0_build_result": build_result,
                    },
                )

            return {
                "messages": [
                    HumanMessage(
                        content=(
                            "Phase 0: Build failed after full patch apply. "
                            f"Output: {build_result.get('output', '')[:500]}"
                        )
                    )
                ],
                "patch_analysis": java_code_changes,
                "patch_diff": diff_text,
                "fast_path_success": False,
                "phase_0_test_targets": test_targets,
                "phase_0_baseline_test_result": phase0_baseline_test_result,
                "phase_0_post_patch_test_result": {},
                "phase_0_transition_evaluation": transition_eval,
                "patch_complexity": patch_complexity,
                "complexity_reason": complexity_reason,
                "complexity_details": complexity_details,
            }

        test_result = validation_toolkit.run_relevant_tests(
            project=project_name, target_info=test_targets
        )
        transition_eval = validation_toolkit.evaluate_test_state_transition(
            phase0_baseline_test_result,
            test_result,
            rename_map=None,
        )
        transition_summary = _format_transition_summary(transition_eval)
        print(f"Phase 0: Transition summary -> {transition_summary}")

        fast_path_success = not test_result.get("compile_error", False) and bool(
            transition_eval.get("valid_backport_signal", False)
        )

        if use_phase0_cache and experiment_mode and backport_commit and original_commit:
            _save_phase0_cache(
                project_name,
                backport_commit,
                original_commit,
                {
                    "fast_path_success": fast_path_success,
                    "phase_0_test_targets": test_targets,
                    "phase_0_baseline_test_result": phase0_baseline_test_result,
                    "phase_0_post_patch_test_result": test_result,
                    "phase_0_transition_evaluation": transition_eval,
                    "phase_0_build_result": build_result,
                },
            )

        if fast_path_success:
            print(
                "Phase 0: Transition-based test validation passed. Fast-path success."
            )
            # Build a simplified implementation plan for the validation agent
            from utils.models import (
                Step,
                CompatibilityAnalysis,
                FileMapping,
                ImplementationPlan,
            )

            mappings = [
                FileMapping(
                    source_file=change.file_path,
                    target_file=change.file_path,
                    confidence=1.0,
                    reasoning="Phase 0 Optimistic Patch — git apply and tests passed.",
                )
                for change in java_code_changes
            ]
            steps = [
                Step(
                    step_id=idx + 1,
                    action="apply_patch_hunk",
                    file_path=change.file_path,
                    description="Apply patch directly — clean git apply.",
                    start_line=None,
                    end_line=None,
                    target_context=None,
                )
                for idx, change in enumerate(java_code_changes)
            ]
            plan = ImplementationPlan(
                patch_intent="Direct Backport (Phase 0 Fast-Path)",
                file_mappings=mappings,
                compatibility_analysis=CompatibilityAnalysis(
                    java_version_differences="Unknown (Optimistic)",
                    missing_dependencies=[],
                    refactoring_notes="None",
                ),
                steps=steps,
            )

            return {
                "messages": [
                    HumanMessage(
                        content=(
                            "Phase 0: Fast-path success. Skipping LLM agents. "
                            f"Transition summary: {transition_summary}"
                        )
                    )
                ],
                "patch_analysis": java_code_changes,
                "patch_diff": diff_text,
                "fast_path_success": True,
                "implementation_plan": plan.dict(),
                "phase_0_test_targets": test_targets,
                "phase_0_baseline_test_result": phase0_baseline_test_result,
                "phase_0_post_patch_test_result": test_result,
                "phase_0_transition_evaluation": transition_eval,
                "patch_complexity": patch_complexity,
                "complexity_reason": complexity_reason,
                "complexity_details": complexity_details,
            }
        else:
            print("Phase 0: Transition-based test validation failed. Rolling back.")
            validation_toolkit.restore_repo_state()
            return {
                "messages": [
                    HumanMessage(
                        content=(
                            "Phase 0: Transition validation failed after patch. "
                            f"Transition summary: {transition_summary}"
                        )
                    )
                ],
                "patch_analysis": java_code_changes,
                "patch_diff": diff_text,
                "fast_path_success": False,
                "phase_0_test_targets": test_targets,
                "phase_0_baseline_test_result": phase0_baseline_test_result,
                "phase_0_post_patch_test_result": test_result,
                "phase_0_transition_evaluation": transition_eval,
            }
    else:
        hint = output[:200]
        print(
            f"Phase 0: git apply --check FAILED. Escalating to full pipeline.\n  Hint: {hint}"
        )
        if use_phase0_cache and experiment_mode and backport_commit and original_commit:
            _save_phase0_cache(
                project_name,
                backport_commit,
                original_commit,
                {
                    "fast_path_success": False,
                    "phase_0_test_targets": test_targets,
                    "phase_0_baseline_test_result": phase0_baseline_test_result,
                    "phase_0_post_patch_test_result": {},
                    "phase_0_transition_evaluation": {
                        "valid_backport_signal": False,
                        "reason": f"Invalid: git apply --check failed. {hint}",
                        "fail_to_pass": [],
                        "pass_to_fail": [],
                        "newly_passing": [],
                    },
                },
            )
        return {
            "messages": [HumanMessage(content=f"Phase 0 failed. Reason: {hint}")],
            "patch_analysis": java_code_changes,
            "patch_diff": diff_text,
            "fast_path_success": False,
            "phase_0_test_targets": test_targets,
            "phase_0_baseline_test_result": phase0_baseline_test_result,
            "patch_complexity": patch_complexity,
            "complexity_reason": complexity_reason,
            "complexity_details": complexity_details,
        }
