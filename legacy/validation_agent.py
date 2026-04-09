"""
Agent 4: The Verifier (Validation Loop)

H-MABS Phase 4 — "Prove Red, Make Green" Loop
"""

import json
import os
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from agents.hunk_generator import _extract_hunk_block
from agents.validation_tools import ValidationToolkit, classify_test_failure_signal
from state import AdaptedHunk, AgentState
from utils.llm_provider import get_llm

# Test-suite compatibility shim for legacy patch target.
ChatGoogleGenerativeAI = None

_SYNTHESIZE_TEST_SYSTEM = """You are an expert Java test engineer.
Your task is to write a single JUnit test method that specifically triggers a vulnerability."""

_SYNTHESIZE_TEST_USER = """\
## Root Cause & Fix Logic
Root Cause: {root_cause}
Fix Logic: {fix_logic}

Write a JUnit test that proves this root cause exists (it should FAIL before the fix is applied, and PASS after).
Output ONLY the unified diff block starting with @@ showing the addition of this test to an appropriate test class.
"""

_ANALYZE_FAILURE_SYSTEM = """You are an expert Java developer reviewing a backport validation failure.
Analyze the error and provide a concise, actionable diagnosis.

Focus on:
- Root cause (missing API, signature mismatch, logic error, etc.)
- Specific files/methods involved
- Clear fix suggestion for hunk regeneration

Keep response under 3 sentences. Be direct and technical."""

_ANALYZE_FAILURE_USER = """\
## Step: {step_name}
## Error:
{error_output}

Provide concise diagnosis and fix suggestion:"""

MAX_VALIDATION_ATTEMPTS = 3


def _classify_apply_failure(raw_output: str) -> tuple[str, list[str], list[dict]]:
    """
    Classify patch-application failures into coarse categories and extract file hints.
    Returns: (category, retry_files, diagnostics)
    """
    text = str(raw_output or "")
    text_lower = text.lower()

    diagnostics: list[dict] = []
    retry_files: set[str] = set()
    category = "unknown"

    file_patterns = [
        r"error:\s+patch failed:\s+([^:\n]+):\d+",
        r"error:\s+([^:\n]+):\s+patch does not apply",
        r"error:\s+([^:\n]+):\s+No such file or directory",
        r"new file\s+([^\s]+)\s+depends on old contents",
    ]
    for pattern in file_patterns:
        for match in re.findall(pattern, text, flags=re.IGNORECASE):
            p = (match or "").strip().replace("\\", "/")
            if p and p != "dev/null":
                retry_files.add(p)

    if "no such file or directory" in text_lower:
        category = "path_or_file_operation"
        diagnostics.append(
            {
                "error_type": "missing_file",
                "suggested_action": "remap",
            }
        )
    if "depends on old contents" in text_lower:
        category = "path_or_file_operation"
        diagnostics.append(
            {
                "error_type": "rename_or_add_mismatch",
                "suggested_action": "remap",
            }
        )
    if "dev/null: does not exist in index" in text_lower:
        category = "path_or_file_operation"
        diagnostics.append(
            {
                "error_type": "invalid_add_delete_semantics",
                "suggested_action": "remap",
            }
        )
    if "malformed patch" in text_lower:
        if category == "unknown":
            category = "context_mismatch"
        diagnostics.append(
            {
                "error_type": "malformed_patch",
                "suggested_action": "regenerate_hunk",
            }
        )
    if "patch does not apply" in text_lower and category == "unknown":
        category = "context_mismatch"
        diagnostics.append(
            {
                "error_type": "context_mismatch",
                "suggested_action": "regenerate_hunk",
            }
        )

    if category == "unknown":
        category = "unknown"

    return category, sorted(retry_files), diagnostics


def _classify_build_failure(
    raw_output: str, target_repo_path: str = "", effective_hunks: list[AdaptedHunk] = []
) -> tuple[str, list[str], list[dict], str, list[str]]:
    """Deterministic build failure parser for retry routing and concise diagnostics."""
    text = str(raw_output or "")
    text_lower = text.lower()

    retry_files: set[str] = set()
    retry_hunk_ids: set[str] = set()
    diagnostics: list[dict] = []

    # Filter out common noise and non-project files
    def is_relevant_file(path: str) -> bool:
        p = path.lower()
        if "test" in p or "fixtures" in p or "build/generated" in p:
            return False
        if not p.endswith(".java"):
            return False
        # If we have a project-specific identifier, great.
        # Otherwise, if it's a .java file in a likely source tree, include it.
        return (
            "src/main/java" in p
            or "org/elasticsearch" in p
            or "io/crate" in p
            or "org/apache/hbase" in p
            or "src/java" in p
        )

    # Map compiler errors to files and lines
    # maven format: [ERROR] /path/to/File.java:[line,col] error message
    # gradle/javac format: /path/to/File.java:line: error message
    error_locations = []
    for line in text.splitlines():
        if ": error:" in line or "error: " in line:
            # Try to extract file and line number
            match = re.search(
                r"([^:\s]+\.java):(?:\[(\d+),\d+\][ ]?:?|(\d+):)",
                line,
            )
            if match:
                file_path = match.group(1).replace("\\", "/")
                # Normalize path if it starts with /repo/
                if file_path.startswith("/repo/"):
                    file_path = file_path[len("/repo/") :]

                if is_relevant_file(file_path):
                    line_num = int(match.group(2) or match.group(3) or 0)
                    retry_files.add(file_path)
                    error_locations.append((file_path, line_num))

    # Failure Attribution: Map error locations to specific hunks using (mainline_file, hunk_index)
    for f_path, l_num in error_locations:
        for hunk in effective_hunks:
            target_f = str(hunk.get("target_file") or "").replace("\\", "/")
            if target_f == f_path:
                # Check if the error line is within the hunk's range
                h_start = int(hunk.get("insertion_line") or 0)
                h_text = str(hunk.get("hunk_text") or "")
                h_len = len(h_text.splitlines())
                if h_start - 5 <= l_num <= h_start + h_len + 5:
                    m_file = hunk.get("mainline_file", "")
                    h_idx = hunk.get("hunk_index", 0)
                    if m_file:
                        retry_hunk_ids.add(f"{m_file}:{h_idx}")

    file_hit_patterns = [
        r"(/repo/[^:\n]+\.java):\[(\d+),\d+\][ ]?:?",
        r"(/repo/[^:\n]+\.java):\d+:",
        r"([^:\s]+\.java):\[(\d+),\d+\][ ]?:?",
        r"(x-pack/[^:\n]+\.java):\d+:",
        r"error:\s+patch failed:\s+([^:\n]+):\d+",
    ]
    for pat in file_hit_patterns:
        for m in re.findall(pat, text, flags=re.IGNORECASE):
            p = m[0] if isinstance(m, tuple) else m
            p = (p or "").strip().replace("\\", "/")
            if p.startswith("/repo/"):
                p = p[len("/repo/") :]
            if p and is_relevant_file(p):
                retry_files.add(p)

    if (
        "not a statement" in text_lower
        or "class, interface, enum, or record expected" in text_lower
    ):
        diagnostics.append(
            {
                "error_type": "java_syntax_or_patch_artifact",
                "suggested_action": "regenerate_hunk_with_structural_guardrails",
            }
        )
        reason = "Java syntax errors detected after patch application (likely malformed hunk output)."
        return (
            "context_mismatch",
            sorted(retry_files),
            diagnostics,
            reason,
            sorted(retry_hunk_ids),
        )

    if (
        "cannot find symbol" in text_lower
        or "method" in text_lower
        and "cannot be applied" in text_lower
    ):
        # Capture concrete javac error lines but filter out Java stdlib and common identifiers
        concrete_errors = []
        noise_identifiers = {
            "symbol",
            "location",
            "variable",
            "class",
            "method",
            "package",
            "type",
        }

        for ln in text.splitlines():
            ll = ln.strip()
            # Handle both Maven [ERROR] and raw Gradle/javac output
            if ("error: cannot find symbol" in ll or "error: method" in ll) and len(
                concrete_errors
            ) < 10:
                # Extract the symbol name to see if it's actually relevant
                sym_match = re.search(
                    r"symbol:\s+(?:variable|method|class)\s+(\w+)",
                    text[text.find(ll) : text.find(ll) + 500],
                )
                if sym_match:
                    sym = sym_match.group(1)
                    if sym.lower() not in noise_identifiers and len(sym) > 3:
                        concrete_errors.append(ll)
                else:
                    concrete_errors.append(ll)

        diagnostics.append(
            {
                "error_type": "api_or_signature_mismatch",
                "suggested_action": "replan_and_regenerate_with_api_compatibility",
                "compiler_errors": concrete_errors,
            }
        )
        reason = "API/signature mismatch in generated patch against target branch."
        if concrete_errors:
            # Surface more errors to the agent
            reason += " Compiler errors: " + " | ".join(concrete_errors[:5])
        return (
            "context_mismatch",
            sorted(retry_files),
            diagnostics,
            reason,
            sorted(retry_hunk_ids),
        )

    diagnostics.append(
        {
            "error_type": "unknown_build_failure",
            "suggested_action": "inspect_build_log_and_retry",
        }
    )
    reason = (
        "Build failed; deterministic parser could not identify a narrower category."
    )
    return "unknown", sorted(retry_files), diagnostics, reason, sorted(retry_hunk_ids)


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


def _extract_structured_failure_context(
    build_error: str,
    hunk_apply_error: str,
    effective_hunks: list,
) -> dict:
    """
    Produce a structured failure context dict for the rulebook.
    Stored in validation_results["structured_failure"] and
    passed to the planning agent as validation_error_context_structured.
    """
    text = str(build_error or hunk_apply_error or "")
    # Extract failed file + line from javac/maven output.
    # Maven format: file.java:[line,col] error:   (space before error)
    # javac format: file.java:line: error:        (colon immediately after line)
    file_line_pattern = re.compile(
        r"([a-zA-Z0-9_/.-]+\.java):(?:\[(\d+),\d+\][ ]?:?|(\d+):)"
    )
    failed_locations = []
    for m in file_line_pattern.finditer(text):
        line_num = int(m.group(2) or m.group(3) or 0)
        file_path = str(m.group(1) or "")
        if file_path.startswith("/repo/"):
            file_path = file_path[len("/repo/") :]
        failed_locations.append(
            {
                "file": file_path,
                "line": line_num,
            }
        )

    # Extract "cannot find symbol" details
    symbol_errors = []
    for m in re.finditer(r"symbol:\s+(variable|method|class)\s+(\w+)", text):
        symbol_errors.append(
            {
                "kind": m.group(1),
                "name": m.group(2),
            }
        )

    # Extract "cannot be applied to given types" — signature mismatch
    sig_errors = []
    for m in re.finditer(
        r"method (\w+)\([^)]*\) in class (\S+) cannot be applied",
        text,
    ):
        sig_errors.append(
            {
                "method": m.group(1),
                "class": m.group(2),
            }
        )

    # Map errors to hunks
    failed_hunk_targets = []
    for loc in failed_locations:
        loc_file = str(loc.get("file") or "")
        loc_line = int(loc.get("line") or 0)
        for hunk in effective_hunks:
            target_f = str(hunk.get("target_file") or "").replace("\\", "/")
            if target_f.endswith(loc_file) or loc_file.endswith(target_f):
                failed_hunk_targets.append(
                    {
                        "target_file": hunk.get("target_file"),
                        "mainline_file": hunk.get("mainline_file"),
                        "error_line": loc_line,
                    }
                )
                break

    primary_f = ""
    if failed_locations:
        primary_f = str(failed_locations[0].get("file") or "")

    primary_s = ""
    if symbol_errors:
        primary_s = str(symbol_errors[0].get("name") or "")

    res_locations = (
        failed_locations[:20] if len(failed_locations) > 20 else failed_locations
    )
    res_symbols = symbol_errors[:20] if len(symbol_errors) > 20 else symbol_errors
    res_sigs = sig_errors[:20] if len(sig_errors) > 20 else sig_errors
    res_hunks = (
        failed_hunk_targets[:20]
        if len(failed_hunk_targets) > 20
        else failed_hunk_targets
    )

    return {
        "raw_error": str(text[:10000]),
        "failed_locations": list(res_locations),
        "symbol_errors": list(res_symbols),
        "signature_errors": list(res_sigs),
        "failed_hunk_targets": list(res_hunks),
        "primary_failed_file": str(primary_f),
        "primary_failed_symbol": str(primary_s),
        "symbol_to_file_candidates": _build_symbol_to_file_candidates(
            text,
            effective_hunks,
        ),
        "method_mismatch_details": _extract_method_mismatch_details(text),
    }


def _build_symbol_to_file_candidates(
    raw_error: str,
    effective_hunks: list,
) -> list[dict[str, Any]]:
    text = str(raw_error or "")
    symbols = []
    for m in re.finditer(r"symbol:\s+(variable|method|class)\s+(\w+)", text):
        symbols.append({"kind": m.group(1), "name": m.group(2)})
    if not symbols:
        return []

    files = {
        str((h or {}).get("target_file") or "").replace("\\", "/")
        for h in (effective_hunks or [])
        if str((h or {}).get("target_file") or "").strip()
    }
    out = []
    for s in symbols[:25]:
        out.append(
            {
                "symbol": str(s.get("name") or ""),
                "kind": str(s.get("kind") or ""),
                "candidate_files": sorted(files),
                "source": "compiler_error_plus_effective_hunks",
            }
        )
    return out


def _extract_method_mismatch_details(raw_error: str) -> list[dict[str, Any]]:
    text = str(raw_error or "")
    out = []
    for m in re.finditer(
        r"method\s+(\w+)\(([^)]*)\)\s+in class\s+(\S+)\s+cannot be applied",
        text,
    ):
        out.append(
            {
                "method": str(m.group(1) or ""),
                "arg_types_in_error": str(m.group(2) or ""),
                "class": str(m.group(3) or ""),
                "source": "compiler_cannot_be_applied",
            }
        )
    return out


def _detect_type_v_retry_scope(
    generation_checklist: list[dict],
) -> tuple[bool, list[str]]:
    files: set[str] = set()
    found = False
    for item in generation_checklist or []:
        exec_types = [
            str(t).strip().upper()
            for t in ((item or {}).get("execution_types") or [])
            if str(t).strip()
        ]
        if "TYPE_V" in exec_types:
            found = True
            mainline_file = str((item or {}).get("mainline_file") or "").strip()
            target_file = str((item or {}).get("target_file") or "").strip()
            if mainline_file:
                files.add(mainline_file)
            if target_file:
                files.add(target_file)
    return found, sorted(files)


async def _analyze_failure(step_name: str, error_output: str, state: AgentState) -> str:
    """Uses LLM to evaluate and explain a validation failure."""
    # Truncate long errors to save tokens
    if len(error_output) > 3000:
        error_output = (
            error_output[:1500] + "\n...[TRUNCATED]...\n" + error_output[-1500:]
        )

    prompt = _ANALYZE_FAILURE_USER.format(
        step_name=step_name, error_output=error_output
    )

    llm = get_llm(
        temperature=0,
        provider=os.getenv("LLM_PROVIDER"),
        model=os.getenv("LLM_MODEL", "gemini-2.0-flash"),
    )

    try:
        response = await llm.ainvoke(
            [
                SystemMessage(content=_ANALYZE_FAILURE_SYSTEM),
                HumanMessage(content=prompt),
            ]
        )
        return response.content if hasattr(response, "content") else str(response)
    except Exception as e:
        return f"Analysis failed: {str(e)}"


async def _synthesize_target_test(
    state: AgentState, toolkit: ValidationToolkit
) -> list[AdaptedHunk]:
    """Phase 6: Test Synthesis"""
    print("  Agent 4: No test hunks provided. Synthesizing a vulnerability test...")
    blueprint = state.get("semantic_blueprint") or {}
    root_cause = blueprint.get("root_cause_hypothesis", "")
    fix_logic = blueprint.get("fix_logic", "")

    prompt = _SYNTHESIZE_TEST_USER.format(root_cause=root_cause, fix_logic=fix_logic)

    llm = get_llm(temperature=0)

    try:
        response = await llm.ainvoke(
            [
                SystemMessage(content=_SYNTHESIZE_TEST_SYSTEM),
                HumanMessage(content=prompt),
            ]
        )
        raw_content = (
            response.content if hasattr(response, "content") else str(response)
        )
        print(f"  Agent 4: Synthesized test (raw):\n{raw_content}")
        adapted_hunk_text = _extract_hunk_block(raw_content)
        print(f"  Agent 4: Extracted hunk text:\n{adapted_hunk_text}")

        if adapted_hunk_text:
            target_file = "src/test/java/SynthesizedVulnTest.java"  # Fallback
            lines = adapted_hunk_text.splitlines()
            for line in lines:
                if line.startswith("+++ b/"):
                    target_file = line[6:].strip()
                    break

            # Repair header to avoid "corrupt patch" errors
            adapted_hunk_text = _repair_hunk_header(adapted_hunk_text)

            # Dry run test synthesis
            dr = toolkit.apply_hunk_dry_run(target_file, adapted_hunk_text)

            hunk: AdaptedHunk = {
                "target_file": target_file,
                "mainline_file": "synthesized",
                "hunk_text": adapted_hunk_text,
                "insertion_line": 1,
                "intent_verified": True,
            }
            return [hunk]
    except Exception as e:
        print(f"  Agent 4: Test synthesis failed: {e}")

    return []


def _extract_test_classes(test_hunks: list[AdaptedHunk]) -> list[str]:
    """Helper to extract class names from file paths."""
    classes = []
    for h in test_hunks:
        f = h.get("target_file", "")
        if f.endswith(".java"):
            # e.g., src/test/java/org/example/MyTest.java -> org.example.MyTest
            parts = f.replace("\\", "/").split("/")
            if "java" in parts:
                idx = parts.index("java")
                cls_path = parts[idx + 1 :]
                cls_name = ".".join(cls_path).replace(".java", "")
                if cls_name not in classes:
                    classes.append(cls_name)
    return classes


def _repair_hunk_header(hunk_text: str) -> str:
    """
    Recalculates the line counts in a unified diff hunk header (@@ line)
    based on the actual lines present in the hunk body.
    """
    if not hunk_text:
        return hunk_text

    lines = hunk_text.splitlines()
    if not lines or not lines[0].startswith("@@"):
        return hunk_text

    header = lines[0]
    # Parse existing header: @@ -a,b +c,d @@  <optional context>
    m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)", header)
    if not m:
        return hunk_text

    start_src = int(m.group(1))
    start_tgt = int(m.group(3))
    ctx = m.group(5)

    count_src = 0
    count_tgt = 0

    for line in lines[1:]:
        if line.startswith(" "):
            count_src += 1
            count_tgt += 1
        elif line.startswith("-"):
            count_src += 1
        elif line.startswith("+"):
            count_tgt += 1

    new_header = f"@@ -{start_src if count_src > 0 else 0},{count_src} +{start_tgt},{count_tgt} @@{ctx}"
    return "\n".join([new_header] + lines[1:]) + "\n"


async def validation_agent(state: AgentState, config) -> dict:
    """
    Agent 4: The Verifier.
    Implements the 6-Phase Prove Red, Make Green loop.
    """
    print("Agent 4 (Validation): Starting 'Prove Red, Make Green' loop...")

    target_repo_path = state.get("target_repo_path")
    if not target_repo_path:
        return {
            "messages": [
                HumanMessage(content="Validation Agent Error: No target_repo_path.")
            ]
        }

    toolkit = ValidationToolkit(target_repo_path)

    # 3.0 Setup
    code_hunks = state.get("adapted_code_hunks", [])
    if not code_hunks:
        # Preserve best-so-far candidate hunks from generation attempts so
        # validation does not fail as empty_generation when a retry path dropped
        # final emission despite having a valid prior candidate.
        code_hunks = state.get("best_effort_adapted_code_hunks", []) or []
    test_hunks = state.get("adapted_test_hunks", [])
    generation_checklist = state.get("generation_checklist", []) or []
    type_v_present, type_v_files = _detect_type_v_retry_scope(generation_checklist)
    developer_aux_hunks = state.get("developer_auxiliary_hunks", [])
    attempts = state.get("validation_attempts", 0)
    blueprint = state.get("semantic_blueprint", {})
    compile_only = state.get("compile_only", False)
    evaluation_full_workflow = state.get("evaluation_full_workflow", False)
    phase_0_test_targets = state.get("phase_0_test_targets")
    phase_0_baseline_test_result = state.get("phase_0_baseline_test_result")
    # Optional runtime toggles for constrained runs.
    skip_compilation_checks = state.get("skip_compilation_checks", False)
    apply_only_validation = state.get("apply_only_validation", False)
    # When recovery applied edits directly on disk (via apply_edit tool), skip
    # hunk application — code files are already modified. Developer aux hunks
    # (test files) still need to be applied.
    recovery_applied_directly = bool(state.get("recovery_applied_directly", False))
    effective_code_hunks = list(code_hunks) + list(developer_aux_hunks)

    # Build rename map for cross-class test transition matching
    # (e.g. SupervisorTest -> LagStatsTest when a test file is renamed in the patch)
    test_rename_map: dict = toolkit.build_test_rename_map_from_aux_hunks(
        developer_aux_hunks
    )
    force_type_v_latch = bool(state.get("force_type_v_until_success") or False)
    force_type_v_reason = str(state.get("force_type_v_reason") or "").strip()
    force_type_v_retry_files = list(state.get("force_type_v_retry_files") or [])

    if attempts >= MAX_VALIDATION_ATTEMPTS:
        print(
            f"  Agent 4: Max validation attempts ({MAX_VALIDATION_ATTEMPTS}) reached. Failing."
        )
        return {"validation_passed": False}

    if not code_hunks and not test_hunks:
        if recovery_applied_directly:
            # Code files already modified on disk by recovery agent.
            # developer_aux_hunks (test files) may still need applying.
            print(
                "  Agent 4: Recovery applied directly — code files on disk. "
                "Proceeding to build/test (developer aux hunks applied if present)."
            )
        else:
            msg = (
                "Validation aborted: Phase 3 produced no adapted code/test hunks. "
                "Regenerate hunks with target-grounded anchors."
            )
            print(f"  Agent 4: {msg}")
            return {
                "validation_passed": False,
                "validation_attempts": attempts + 1,
                "validation_error_context": msg,
                "validation_failure_category": "empty_generation",
                "validation_retry_files": [],
                "validation_repeated_patch_detected": bool(
                    state.get("validation_repeated_patch_detected", False)
                ),
                "validation_repeated_plan_detected": bool(
                    state.get("validation_repeated_plan_detected", False)
                ),
                "force_type_v_until_success": force_type_v_latch,
                "force_type_v_reason": force_type_v_reason,
                "force_type_v_retry_files": force_type_v_retry_files,
            }

    incomplete_generation_items = []
    for item in generation_checklist:
        status = str(item.get("status", "")).strip().lower()
        if status == "failed" or status == "in_progress" or status == "pending":
            incomplete_generation_items.append(item)
            continue
        if status == "success":
            required_steps = set(item.get("todo_steps") or [])
            completed_steps = set(item.get("completed_steps") or [])
            if required_steps and not required_steps.issubset(completed_steps):
                item = dict(item)
                item["reason"] = "incomplete_todo_steps"
                item["missing_steps"] = sorted(required_steps - completed_steps)
                incomplete_generation_items.append(item)

    if incomplete_generation_items and not evaluation_full_workflow:
        retry_hunks = sorted(
            {
                int(item.get("hunk_index"))
                for item in incomplete_generation_items
                if isinstance(item.get("hunk_index"), int)
            }
        )
        failed_stages = [
            str(item.get("reason") or "").strip()
            for item in incomplete_generation_items
            if str(item.get("reason") or "").strip()
        ]
        failed_stage = failed_stages[0] if failed_stages else "generation_incomplete"

        retry_files = sorted(
            {
                str(item.get("mainline_file") or "").strip()
                for item in incomplete_generation_items
                if item.get("mainline_file")
            }
            | {
                str(item.get("target_file") or "").strip()
                for item in incomplete_generation_items
                if item.get("target_file")
            }
        )
        msg = (
            "Validation deferred: generator checklist has incomplete hunk tasks. "
            "Route back to planning/generation before running build/tests. "
            f"Incomplete hunks: {json.dumps(incomplete_generation_items, default=str)[:1500]}"
        )
        print(f"  Agent 4: {msg}")
        return {
            "validation_passed": False,
            "validation_attempts": attempts + 1,
            "validation_error_context": msg,
            "validation_failure_category": "context_mismatch",
            "validation_retry_files": retry_files,
            "validation_retry_hunks": retry_hunks,
            "validation_failed_stage": failed_stage,
            "force_type_v_until_success": force_type_v_latch,
            "force_type_v_reason": force_type_v_reason,
            "force_type_v_retry_files": force_type_v_retry_files,
            # Preserve prior validation step results (e.g. failed compile/build) so
            # graph routing (stagnation + build-failure escalation) still sees them
            # after checklist-short-circuit returns.
            "validation_results": dict(state.get("validation_results") or {}),
        }
    elif incomplete_generation_items and evaluation_full_workflow:
        print(
            "  Agent 4: Generator checklist incomplete, but evaluation_full_workflow "
            "is enabled; continuing to build/tests for concrete failure diagnostics."
        )

    # Ensure clean repo
    toolkit.restore_repo_state()

    # Trace Initialization
    unique_code_files = list(
        set(
            [h.get("target_file") for h in effective_code_hunks if h.get("target_file")]
        )
    )
    unique_test_files = list(
        set([h.get("target_file") for h in test_hunks if h.get("target_file")])
    )

    validation_results = {}

    trace = [
        "# Validation Trace",
        "",
        "## Blueprint Summary",
        f"- **Root Cause**: {blueprint.get('root_cause_hypothesis', 'N/A')}",
        f"- **Fix Logic**: {blueprint.get('fix_logic', 'N/A')}",
        f"- **Dependent APIs**: {blueprint.get('dependent_apis', [])}",
        "",
        "## Hunk Segregation",
        f"- Code files: {len(unique_code_files)}",
        f"- Test files: {len(unique_test_files)}",
        f"- Developer auxiliary hunks: {len(developer_aux_hunks)}",
        "",
        "## Agent Tool Steps",
        "",
    ]

    def log_step(tool_name, params, output):
        trace.append(
            f"  - `Agent calls {tool_name}` with `{json.dumps(params, default=str)}`"
        )
        # Truncate long outputs for trace readability
        out_str = str(output)
        if len(out_str) > 1000:
            out_str = out_str[:1000] + "... [TRUNCATED]"
        trace.append(f"  - `Tool: {tool_name}` -> {out_str}")

    if evaluation_full_workflow:
        if recovery_applied_directly:
            print(
                "  Agent 4: Evaluation full-workflow mode (recovery direct-apply — "
                "skip code hunk application, apply dev-aux only, then build + tests)..."
            )
            # Code files already on disk. Only apply developer_aux_hunks (test files).
            aux_hunks_to_apply = list(developer_aux_hunks) + list(test_hunks)
            if aux_hunks_to_apply:
                res = toolkit.apply_adapted_hunks(aux_hunks_to_apply, [])
                log_step(
                    "apply_adapted_hunks (dev-aux only)",
                    {"aux_count": len(aux_hunks_to_apply)},
                    res,
                )
                validation_results["hunk_application"] = {
                    "success": res["success"],
                    "raw": res["output"],
                }
                if not res["success"]:
                    analysis = await _analyze_failure(
                        "Dev-Aux Hunk Application", res["output"], state
                    )
                    failure_category, retry_files, diagnostics = _classify_apply_failure(
                        res.get("output", "")
                    )
                    validation_results["hunk_application"]["agent_evaluation"] = analysis
                    validation_results["hunk_application"]["error_context"] = analysis
                    trace.append(
                        f"\n**Dev-Aux Hunk Application FAILED**\n\n{analysis}"
                    )
                    toolkit.write_trace("\n".join(trace), "validation_trace.md")
                    toolkit.restore_repo_state()
                    return {
                        "validation_passed": False,
                        "validation_attempts": attempts + 1,
                        "validation_error_context": f"Dev-aux hunk application failed: {analysis}",
                        "validation_results": validation_results,
                        "regeneration_hint": analysis,
                        "validation_failure_category": failure_category,
                        "validation_retry_files": retry_files,
                        "force_type_v_until_success": force_type_v_latch,
                        "force_type_v_reason": force_type_v_reason,
                        "force_type_v_retry_files": force_type_v_retry_files,
                    }
            else:
                validation_results["hunk_application"] = {
                    "success": True,
                    "raw": "Skipped — recovery applied code files directly; no dev-aux hunks.",
                }
        else:
            print(
                "  Agent 4: Evaluation full-workflow mode (apply -> build -> relevant tests)..."
            )
            res = toolkit.apply_adapted_hunks(effective_code_hunks, test_hunks)
            log_step(
                "apply_adapted_hunks",
                {
                    "code_count": len(code_hunks),
                    "developer_aux_count": len(developer_aux_hunks),
                    "effective_code_count": len(effective_code_hunks),
                    "test_count": len(test_hunks),
                },
                res,
            )

            validation_results["hunk_application"] = {
                "success": res["success"],
                "raw": res["output"],
            }

            if not res["success"]:
                analysis = await _analyze_failure("Hunk Application", res["output"], state)
                failure_category, retry_files, diagnostics = _classify_apply_failure(
                    res.get("output", "")
                )
                validation_results["hunk_application"]["agent_evaluation"] = analysis
                validation_results["hunk_application"]["error_context"] = analysis
                validation_results["hunk_application"]["diagnostics"] = {
                    "category": failure_category,
                    "retry_files": retry_files,
                    "issues": diagnostics,
                }
                trace.append(
                    f"\n**Final Status: HUNK APPLICATION FAILED**\n\n**Agent Analysis:**\n{analysis}"
                )
                toolkit.write_trace("\n".join(trace), "validation_trace.md")
                toolkit.restore_repo_state()
                return {
                    "validation_passed": False,
                    "validation_attempts": attempts + 1,
                    "validation_error_context": f"Hunk application failed: {analysis}",
                    "validation_results": validation_results,
                    "regeneration_hint": analysis,
                    "validation_failure_category": failure_category,
                    "validation_retry_files": retry_files,
                    "force_type_v_until_success": force_type_v_latch,
                    "force_type_v_reason": force_type_v_reason,
                    "force_type_v_retry_files": force_type_v_retry_files,
                }

        build_res = toolkit.run_build_script()
        log_step("run_build_script", {}, build_res)
        validation_results["build"] = {
            "success": build_res.get("success", False),
            "raw": build_res.get("output", ""),
        }
        if not build_res.get("success", False):
            failure_category, retry_files, diagnostics, analysis, retry_hunks = (
                _classify_build_failure(
                    build_res.get("output", ""), target_repo_path, effective_code_hunks
                )
            )

            structured_failure = _extract_structured_failure_context(
                build_error=build_res.get("output", ""),
                hunk_apply_error="",
                effective_hunks=effective_code_hunks,
            )
            validation_results["structured_failure"] = structured_failure

            if type_v_present and type_v_files:
                retry_files = sorted(set(retry_files) | set(type_v_files))
            sticky_type_v = bool(force_type_v_latch or type_v_present)
            sticky_scope_files = sorted(
                set(force_type_v_retry_files) | set(type_v_files)
            )
            retry_files = sorted(set(retry_files) | set(sticky_scope_files))
            sticky_reason = (
                force_type_v_reason or "build_context_mismatch_type_v"
                if sticky_type_v
                else ""
            )
            validation_results["build"]["agent_evaluation"] = analysis
            validation_results["build"]["error_context"] = analysis
            validation_results["build"]["diagnostics"] = {
                "category": failure_category,
                "retry_files": retry_files,
                "retry_hunks": retry_hunks,
                "issues": diagnostics,
            }
            trace.append(
                f"\n**Final Status: BUILD FAILED**\n\n**Agent Analysis:**\n{analysis}"
            )
            toolkit.write_trace("\n".join(trace), "validation_trace.md")
            toolkit.restore_repo_state()
            return {
                "validation_passed": False,
                "validation_attempts": attempts + 1,
                "validation_error_context": f"Build failed: {analysis}",
                "validation_error_context_structured": structured_failure,
                "validation_results": validation_results,
                "regeneration_hint": analysis,
                "validation_failure_category": failure_category,
                "validation_retry_files": retry_files,
                "validation_retry_hunks": retry_hunks,
                "force_type_v_until_success": sticky_type_v,
                "force_type_v_reason": sticky_reason,
                "force_type_v_retry_files": sticky_scope_files,
            }

        inferred_project = os.path.basename(target_repo_path).strip().lower()
        test_targets = phase_0_test_targets or toolkit.detect_relevant_test_targets(
            project=inferred_project
        )
        test_res = toolkit.run_relevant_tests(
            project=inferred_project, target_info=test_targets
        )
        log_step("run_relevant_tests", {"targets": test_targets}, test_res)
        transition_eval = toolkit.evaluate_test_state_transition(
            phase_0_baseline_test_result, test_res, rename_map=test_rename_map or None
        )
        transition_summary = _format_transition_summary(transition_eval)
        log_step(
            "evaluate_test_state_transition",
            {
                "baseline_available": bool(phase_0_baseline_test_result),
                "baseline_mode": (phase_0_baseline_test_result or {}).get(
                    "mode", "none"
                ),
            },
            transition_eval,
        )

        validation_results["test_target_detection"] = {
            "success": True,
            "raw": test_targets,
        }
        validation_results["tests"] = {
            "success": test_res.get("success", False),
            "raw": test_res.get("output", ""),
            "mode": test_res.get("mode", "unknown"),
            "test_state": test_res.get("test_state", {}),
            "state_transition": transition_eval,
        }

        if test_res.get("compile_error", False):
            analysis = await _analyze_failure(
                "Relevant Tests (Compile Error)", test_res.get("output", ""), state
            )
            signal = classify_test_failure_signal(
                output_text=test_res.get("output", ""),
                transition_reason=transition_eval.get("reason", ""),
                success=test_res.get("success"),
                compile_error=bool(test_res.get("compile_error", False)),
            )
            validation_results["tests"]["agent_evaluation"] = analysis
            validation_results["tests"]["error_context"] = analysis
            validation_results["tests"]["diagnostics"] = signal
            trace.append(
                f"\n**Final Status: TEST EXECUTION FAILED (COMPILE ERROR)**\n\n**Agent Analysis:**\n{analysis}"
            )
            toolkit.write_trace("\n".join(trace), "validation_trace.md")
            toolkit.restore_repo_state()
            return {
                "validation_passed": False,
                "validation_attempts": attempts + 1,
                "validation_error_context": f"Tests failed to execute: {analysis}",
                "validation_results": validation_results,
                "regeneration_hint": analysis,
                "validation_failure_category": signal.get(
                    "category", "context_mismatch"
                ),
                "validation_infrastructure_failure": bool(
                    signal.get("infrastructure_failure", False)
                ),
                "validation_infrastructure_inconclusive": bool(
                    signal.get("infrastructure_inconclusive", False)
                ),
            }

        if not transition_eval.get("valid_backport_signal", False):
            analysis = (
                "Relevant-test state transition check failed. "
                f"Transition summary: {transition_summary}"
            )
            signal = classify_test_failure_signal(
                output_text=test_res.get("output", ""),
                transition_reason=transition_eval.get("reason", ""),
                success=test_res.get("success"),
                compile_error=bool(test_res.get("compile_error", False)),
            )
            validation_results["tests"]["agent_evaluation"] = analysis
            validation_results["tests"]["error_context"] = analysis
            validation_results["tests"]["diagnostics"] = signal
            trace.append(
                "\n**Final Status: TEST STATE TRANSITION FAILED**\n\n"
                f"**Transition Summary:**\n{transition_summary}\n\n"
                f"**Transition Evaluation:**\n{json.dumps(transition_eval, default=str)}"
            )
            toolkit.write_trace("\n".join(trace), "validation_trace.md")
            toolkit.restore_repo_state()
            retry_payload = {
                "validation_passed": False,
                "validation_attempts": attempts + 1,
                "validation_error_context": analysis,
                "validation_results": validation_results,
                "regeneration_hint": analysis,
                "validation_failure_category": signal.get(
                    "category", "context_mismatch"
                ),
                "validation_infrastructure_failure": bool(
                    signal.get("infrastructure_failure", False)
                ),
                "validation_infrastructure_inconclusive": bool(
                    signal.get("infrastructure_inconclusive", False)
                ),
            }
            if (
                type_v_present
                and type_v_files
                and not bool(signal.get("infrastructure_failure", False))
            ):
                retry_payload["validation_failure_category"] = "context_mismatch"
                retry_payload["validation_retry_files"] = type_v_files
                retry_payload["validation_retry_hunks"] = []
                retry_payload["validation_failed_stage"] = "generation_contract_failed"
                retry_payload["force_type_v_until_success"] = True
                retry_payload["force_type_v_reason"] = (
                    force_type_v_reason or "type_v_transition_retry"
                )
                retry_payload["force_type_v_retry_files"] = type_v_files
            return retry_payload

        validation_results["tests"]["agent_evaluation"] = (
            "Relevant-test transition check passed: no pass->fail regressions and "
            "at least one fail->pass or newly passing test observed. "
            f"Transition summary: {transition_summary}"
        )

        trace.append(
            "\n**Final Status: VALIDATION PASSED (FULL EVALUATION WORKFLOW)**\n\n"
            f"**Transition Summary:**\n{transition_summary}"
        )
        toolkit.write_trace("\n".join(trace), "validation_trace.md")
        toolkit.restore_repo_state()
        return {
            "validation_passed": True,
            "validation_attempts": attempts + 1,
            "validation_error_context": "",
            "validation_results": validation_results,
            "phase_0_transition_evaluation": transition_eval,
            "force_type_v_until_success": False,
            "force_type_v_reason": "",
            "force_type_v_retry_files": [],
        }

    if apply_only_validation:
        print("  Agent 4: Apply-only mode - checking patch application only...")
        res = toolkit.apply_adapted_hunks(effective_code_hunks, test_hunks)
        log_step(
            "apply_adapted_hunks",
            {"code_count": len(effective_code_hunks), "test_count": len(test_hunks)},
            res,
        )

        validation_results["hunk_application"] = {
            "success": res["success"],
            "raw": res["output"],
        }

        if not res["success"]:
            analysis = await _analyze_failure("Hunk Application", res["output"], state)
            failure_category, retry_files, diagnostics = _classify_apply_failure(
                res.get("output", "")
            )
            validation_results["hunk_application"]["agent_evaluation"] = analysis
            validation_results["hunk_application"]["error_context"] = analysis
            validation_results["hunk_application"]["diagnostics"] = {
                "category": failure_category,
                "retry_files": retry_files,
                "issues": diagnostics,
            }
            trace.append(
                f"\n**Final Status: HUNK APPLICATION FAILED**\n\n**Agent Analysis:**\n{analysis}"
            )
            toolkit.write_trace("\n".join(trace), "validation_trace.md")
            return {
                "validation_passed": False,
                "validation_attempts": attempts + 1,
                "validation_error_context": f"Hunk application failed: {analysis}",
                "validation_results": validation_results,
                "regeneration_hint": analysis,
                "validation_failure_category": failure_category,
                "validation_retry_files": retry_files,
            }

        validation_results["hunk_application"]["agent_evaluation"] = (
            "Hunks applied successfully to all target files."
        )
        validation_results["compilation"] = {
            "success": True,
            "raw": "Skipped by apply-only validation mode.",
            "agent_evaluation": "Compilation step intentionally skipped.",
        }
        validation_results["spotbugs"] = {
            "success": True,
            "raw": "Skipped by apply-only validation mode.",
            "agent_evaluation": "SpotBugs step intentionally skipped.",
        }
        trace.append("\n**Final Status: VALIDATION PASSED (APPLY-ONLY MODE)**")
        trace.append(
            "\n**Note:** Compilation, tests, and static-analysis phases are disabled."
        )
        toolkit.write_trace("\n".join(trace), "validation_trace.md")
        return {
            "validation_passed": True,
            "validation_attempts": attempts + 1,
            "validation_error_context": "",
            "validation_results": validation_results,
        }

    # Compile-Only Mode (Streamlined - Apply patch and compile changed files only)
    if compile_only:
        print(
            "  Agent 4: Streamlined mode - applying patch and compiling changed files only..."
        )
        res = toolkit.apply_adapted_hunks(effective_code_hunks, test_hunks)
        log_step(
            "apply_adapted_hunks",
            {"code_count": len(effective_code_hunks), "test_count": len(test_hunks)},
            res,
        )

        validation_results["hunk_application"] = {
            "success": res["success"],
            "raw": res["output"],
        }

        if not res["success"]:
            # Use LLM to analyze hunk application failure
            analysis = await _analyze_failure("Hunk Application", res["output"], state)
            failure_category, retry_files, diagnostics = _classify_apply_failure(
                res.get("output", "")
            )
            validation_results["hunk_application"]["agent_evaluation"] = analysis
            validation_results["hunk_application"]["error_context"] = analysis
            validation_results["hunk_application"]["diagnostics"] = {
                "category": failure_category,
                "retry_files": retry_files,
                "issues": diagnostics,
            }
            trace.append(
                f"\n**Final Status: HUNK APPLICATION FAILED**\n\n**Agent Analysis:**\n{analysis}"
            )
            toolkit.write_trace("\n".join(trace), "validation_trace.md")
            return {
                "validation_passed": False,
                "validation_attempts": attempts + 1,
                "validation_error_context": f"Hunk application failed: {analysis}",
                "validation_results": validation_results,
                "regeneration_hint": analysis,  # For hunk generator to use
                "validation_failure_category": failure_category,
                "validation_retry_files": retry_files,
            }
        else:
            validation_results["hunk_application"]["agent_evaluation"] = (
                "Hunks applied successfully to all target files."
            )

        if skip_compilation_checks:
            validation_results["compilation"] = {
                "success": True,
                "raw": "Skipped by configuration for environment-constrained runs.",
                "agent_evaluation": "Compilation step intentionally skipped.",
            }
            validation_results["spotbugs"] = {
                "success": True,
                "raw": "Skipped by configuration for environment-constrained runs.",
                "agent_evaluation": "Static analysis step intentionally skipped.",
            }
            trace.append(
                "\n**Final Status: VALIDATION PASSED (COMPILATION/SPOTBUGS SKIPPED)**"
            )
            trace.append(
                "\n**Note:** Compilation and static-analysis phases are disabled in this run mode."
            )
            passed = True
        else:
            applied_files = res.get("applied_files", [])
            build_res = toolkit.compile_files(applied_files)
            log_step("compile_files", {"files": applied_files}, build_res)

            validation_results["compilation"] = {
                "success": build_res.get("success"),
                "raw": build_res.get("output", ""),
            }

            if not build_res.get("success"):
                # Failure Attribution: Map error locations to specific hunks
                failure_category, retry_files, diagnostics, analysis, retry_hunks = (
                    _classify_build_failure(
                        build_res.get("output", ""),
                        target_repo_path,
                        effective_code_hunks,
                    )
                )

                structured_failure = _extract_structured_failure_context(
                    build_error=build_res.get("output", ""),
                    hunk_apply_error="",
                    effective_hunks=effective_code_hunks,
                )
                validation_results["structured_failure"] = structured_failure

                validation_results["compilation"]["agent_evaluation"] = analysis
                validation_results["compilation"]["error_context"] = analysis
                validation_results["compilation"]["diagnostics"] = {
                    "category": failure_category,
                    "retry_files": retry_files,
                    "retry_hunks": retry_hunks,
                }
                trace.append(
                    f"\n**Final Status: COMPILATION FAILED**\n\n**Agent Analysis:**\n{analysis}"
                )
                toolkit.write_trace("\n".join(trace), "validation_trace.md")
                return {
                    "validation_passed": False,
                    "validation_attempts": attempts + 1,
                    "validation_error_context": f"Compilation failed: {analysis}",
                    "validation_error_context_structured": structured_failure,
                    "validation_results": validation_results,
                    "regeneration_hint": analysis,  # For hunk generator to use
                    "validation_retry_files": retry_files,
                    "validation_retry_hunks": retry_hunks,
                }
            else:
                validation_results["compilation"]["agent_evaluation"] = (
                    "All modified files compiled successfully."
                )

            # Run SpotBugs after successful compile
            print("  Agent 4: Running SpotBugs validation...")
            modules = build_res.get("modules", [])
            classes_paths = toolkit.get_module_class_paths(modules)

            # Use target_files for Maven SpotBugs optimization
            spotbugs_res = toolkit.run_spotbugs(
                compiled_classes_paths=classes_paths,
                source_path=os.path.join(
                    state["target_repo_path"], "src", "main", "java"
                ),
                target_files=unique_code_files + unique_test_files,
            )
            log_step("run_spotbugs", {"paths": classes_paths}, spotbugs_res)

            passed = spotbugs_res.get("success", True)
            spotbugs_output = spotbugs_res.get("output", "")

            validation_results["spotbugs"] = {
                "success": passed,
                "raw": spotbugs_output,
                "agent_evaluation": "",
            }

            if not passed:
                # Use LLM to analyze SpotBugs findings (only send relevant findings, not full output)
                analysis = await _analyze_failure("SpotBugs", spotbugs_output, state)
                validation_results["spotbugs"]["agent_evaluation"] = analysis
                validation_results["spotbugs"]["error_context"] = analysis
                trace.append(
                    f"\n**Final Status: STATIC VALIDATION FAILED**\n\n**Agent Analysis:**\n{analysis}"
                )
            else:
                validation_results["spotbugs"]["agent_evaluation"] = (
                    "No high-severity bugs detected. Code passes static analysis."
                )
                trace.append(f"\n**Final Status: {'VALIDATION PASSED'}**")

        toolkit.write_trace("\n".join(trace), "validation_trace.md")

        return {
            "validation_passed": passed,
            "validation_attempts": attempts + 1,
            "validation_error_context": spotbugs_res.get("output", "")
            if not passed
            else "",
            "validation_results": validation_results,
        }

    # Standard "Prove Red, Make Green" Loop
    # 3.1 Phase 6: Test Synthesis (Moved to start if missing)
    if not test_hunks:
        print("  Agent 4: No test hunks found. Synthesizing...")
        synthesized = await _synthesize_target_test(state, toolkit)
        log_step(
            "synthesize_target_test", {}, "Hunks generated" if synthesized else "Failed"
        )

        validation_results["test_synthesis"] = {
            "success": bool(synthesized),
            "raw": "Hunks generated"
            if synthesized
            else "Failed to synthesize a viable test case.",
        }

        if not synthesized:
            error_msg = "Failed to synthesize a viable test case."
            analysis = await _analyze_failure("Test Synthesis", error_msg, state)
            validation_results["test_synthesis"]["agent_evaluation"] = analysis
            validation_results["test_synthesis"]["error_context"] = analysis
            trace.append(
                f"\n**Final Status: TEST SYNTHESIS FAILED**\n\n**Agent Analysis:**\n{analysis}"
            )
            toolkit.write_trace("\n".join(trace), "validation_trace.md")
            return {
                "validation_passed": False,
                "validation_attempts": attempts + 1,
                "validation_error_context": analysis,
                "validation_results": validation_results,
                "regeneration_hint": analysis,
            }
        test_hunks = synthesized
    else:
        validation_results["test_synthesis"] = {
            "success": True,
            "agent_evaluation": "Test hunks provided from previous phase.",
        }

    test_classes = _extract_test_classes(test_hunks)

    # 3.2 Phase 1: Proof of Vulnerability
    print("  Agent 4: Phase 1 — Proof of Vulnerability (Applying tests only)...")
    res1 = toolkit.apply_adapted_hunks(code_hunks=[], test_hunks=test_hunks)
    log_step("apply_adapted_hunks (tests only)", {"count": len(test_hunks)}, res1)

    validation_results["test_application"] = {
        "success": res1["success"],
        "raw": res1["output"],
    }

    if not res1["success"]:
        analysis = await _analyze_failure("Test Application", res1["output"], state)
        validation_results["test_application"]["agent_evaluation"] = analysis
        validation_results["test_application"]["error_context"] = analysis
        trace.append(
            f"\n**Final Status: TEST APPLICATION FAILED**\n\n**Agent Analysis:**\n{analysis}"
        )
        toolkit.write_trace("\n".join(trace), "validation_trace.md")
        toolkit.restore_repo_state()
        return {
            "validation_passed": False,
            "validation_attempts": attempts + 1,
            "validation_error_context": analysis,
            "adapted_test_hunks": test_hunks,
            "validation_results": validation_results,
            "regeneration_hint": analysis,
        }
    else:
        validation_results["test_application"]["agent_evaluation"] = (
            "Test hunks applied successfully."
        )

    # 3.3 Phase 2: Failure Confirmation
    print("  Agent 4: Phase 2 — Failure Confirmation (Running targeted tests)...")
    res2 = toolkit.run_targeted_tests(test_classes)
    log_step("run_targeted_tests (pre-fix)", {"classes": test_classes}, res2)

    # In Phase 2, SUCCESS (tests passing) is actually a FAILURE for the backport process
    # because the test should fail before the fix.
    validation_results["failure_confirmation"] = {
        "success": not res2["success"] and not res2["compile_error"],
        "raw": res2["output"],
    }

    if res2["compile_error"]:
        analysis = await _analyze_failure(
            "Failure Confirmation (Compile)", res2["output"], state
        )
        compile_context = (
            f"Compilation error during failure confirmation: {res2.get('output', '')}\n"
            f"Analysis: {analysis}"
        )
        validation_results["failure_confirmation"]["agent_evaluation"] = analysis
        validation_results["failure_confirmation"]["error_context"] = analysis
        trace.append(
            f"\n**Final Status: COMPILATION FAILED (PRE-FIX)**\n\n**Agent Analysis:**\n{analysis}"
        )
        toolkit.write_trace("\n".join(trace), "validation_trace.md")
        toolkit.restore_repo_state()
        return {
            "validation_passed": False,
            "validation_attempts": attempts + 1,
            "validation_error_context": compile_context,
            "adapted_test_hunks": test_hunks,
            "validation_results": validation_results,
            "regeneration_hint": analysis,
        }

    if res2["success"]:
        analysis = (
            "Test passed unexpectedly BEFORE the fix was applied. "
            "The synthesized test does not reproduce the issue and must be rewritten "
            "to properly trigger the vulnerability condition."
        )
        validation_results["failure_confirmation"]["agent_evaluation"] = analysis
        validation_results["failure_confirmation"]["error_context"] = analysis
        trace.append(
            f"\n**Final Status: INVALID TEST (PASSED UNEXPECTEDLY)**\n\n**Agent Analysis:**\n{analysis}"
        )
        toolkit.write_trace("\n".join(trace), "validation_trace.md")
        toolkit.restore_repo_state()
        return {
            "validation_passed": False,
            "validation_attempts": attempts + 1,
            "validation_error_context": analysis,
            "adapted_test_hunks": test_hunks,
            "validation_results": validation_results,
            "regeneration_hint": analysis,
        }

    validation_results["failure_confirmation"]["agent_evaluation"] = (
        "Tests failed as expected before fix - vulnerability confirmed."
    )
    trace.append("**Failure Confirmed** (Tests failed as expected).\n")

    # 3.4 Phase 3: Patch Application
    print("  Agent 4: Phase 3 — Patch Application (Applying code fix)...")
    toolkit.restore_repo_state()  # Cleanup buggy tests before full applied
    res3 = toolkit.apply_adapted_hunks(
        code_hunks=effective_code_hunks, test_hunks=test_hunks
    )
    log_step(
        "apply_adapted_hunks (full)",
        {"code_count": len(effective_code_hunks), "test_count": len(test_hunks)},
        res3,
    )

    validation_results["patch_application"] = {
        "success": res3["success"],
        "raw": res3["output"],
    }

    if not res3["success"]:
        analysis = await _analyze_failure("Patch Application", res3["output"], state)
        validation_results["patch_application"]["agent_evaluation"] = analysis
        validation_results["patch_application"]["error_context"] = analysis
        trace.append(
            f"\n**Final Status: PATCH APPLICATION FAILED**\n\n**Agent Analysis:**\n{analysis}"
        )
        toolkit.write_trace("\n".join(trace), "validation_trace.md")
        toolkit.restore_repo_state()
        return {
            "validation_passed": False,
            "validation_attempts": attempts + 1,
            "validation_error_context": analysis,
            "adapted_test_hunks": test_hunks,
            "validation_results": validation_results,
            "regeneration_hint": analysis,
        }
    else:
        validation_results["patch_application"]["agent_evaluation"] = (
            "Code and test hunks applied successfully."
        )

    # 3.5 Phase 4: Targeted Verification
    print("  Agent 4: Phase 4 — Targeted Verification (Making it Green)...")
    res4 = toolkit.run_targeted_tests(test_classes)
    log_step("run_targeted_tests (post-fix)", {"classes": test_classes}, res4)

    validation_results["targeted_verification"] = {
        "success": res4["success"],
        "raw": res4["output"],
    }

    if not res4["success"]:
        analysis = await _analyze_failure(
            "Targeted Verification", res4["output"], state
        )
        failure_context = f"tests failed or compile aborted: {analysis}"
        validation_results["targeted_verification"]["agent_evaluation"] = analysis
        validation_results["targeted_verification"]["error_context"] = failure_context
        trace.append(
            f"\n**Final Status: VERIFICATION FAILED**\n\n**Agent Analysis:**\n{analysis}"
        )
        toolkit.write_trace("\n".join(trace), "validation_trace.md")
        toolkit.restore_repo_state()
        return {
            "validation_passed": False,
            "validation_attempts": attempts + 1,
            "validation_error_context": failure_context,
            "adapted_test_hunks": test_hunks,
            "validation_results": validation_results,
            "regeneration_hint": analysis,
        }
    else:
        validation_results["targeted_verification"]["agent_evaluation"] = (
            "All tests passed after fix application."
        )

    trace.append("**Verification Successful** (Tests passed!).\n")

    # 3.6 Phase 5: AST/Static Validation
    print("  Agent 4: Phase 5 — AST/Static Validation (Running SpotBugs)...")
    # Recompile all files to ensure all classes are available for SpotBugs
    build_res_full = toolkit.compile_files(unique_code_files + unique_test_files)

    validation_results["full_compilation"] = {
        "success": build_res_full.get("success"),
        "raw": build_res_full.get("output", ""),
    }

    if not build_res_full.get("success"):
        analysis = await _analyze_failure(
            "Full Compilation", build_res_full.get("output", ""), state
        )
        validation_results["full_compilation"]["agent_evaluation"] = analysis
        validation_results["full_compilation"]["error_context"] = analysis
        trace.append(
            f"\n**Final Status: COMPILATION FAILED (PRE-SPOTBUGS)**\n\n**Agent Analysis:**\n{analysis}"
        )
        toolkit.write_trace("\n".join(trace), "validation_trace.md")
        toolkit.restore_repo_state()
        return {
            "validation_passed": False,
            "validation_attempts": attempts + 1,
            "validation_error_context": analysis,
            "adapted_test_hunks": test_hunks,
            "validation_results": validation_results,
            "regeneration_hint": analysis,
        }
    else:
        validation_results["full_compilation"]["agent_evaluation"] = (
            "Full project compilation successful."
        )

    modules = build_res_full.get("modules", [])
    classes_paths = toolkit.get_module_class_paths(modules)

    spotbugs_res = toolkit.run_spotbugs(
        compiled_classes_paths=classes_paths,
        source_path=os.path.join(target_repo_path, "src", "main", "java"),
    )
    log_step("run_spotbugs", {"paths": classes_paths}, spotbugs_res)

    passed = spotbugs_res.get("success", True)
    validation_results["spotbugs"] = {"success": passed, "raw": str(spotbugs_res)}

    if not passed:
        analysis = await _analyze_failure("SpotBugs", str(spotbugs_res), state)
        validation_results["spotbugs"]["agent_evaluation"] = analysis
        validation_results["spotbugs"]["error_context"] = analysis
        trace.append(
            f"\n**Final Status: STATIC VALIDATION FAILED**\n\n**Agent Analysis:**\n{analysis}"
        )
        toolkit.write_trace("\n".join(trace), "validation_trace.md")
        toolkit.restore_repo_state()
        return {
            "validation_passed": False,
            "validation_attempts": attempts + 1,
            "validation_error_context": analysis,
            "adapted_test_hunks": test_hunks,
            "validation_results": validation_results,
            "regeneration_hint": analysis,
        }
    else:
        validation_results["spotbugs"]["agent_evaluation"] = (
            "No high-severity bugs detected. Code passes static analysis."
        )

    trace.append("**Static Validation Successful** (No high-severity bugs found).\n")

    print("  Agent 4: 'Prove Red, Make Green' loop complete! Validation PASSED.")
    trace.append("\n**Final Status: VALIDATION PASSED**")
    toolkit.write_trace("\n".join(trace), "validation_trace.md")

    return {
        "validation_passed": True,
        "validation_attempts": attempts + 1,
        "adapted_test_hunks": test_hunks,
        "validation_results": validation_results,
        "messages": [HumanMessage(content="Validation passed successfully.")],
    }
