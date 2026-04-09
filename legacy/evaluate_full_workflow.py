"""
Full Workflow Evaluation Script (Phase 0-4) for Multiple Projects (Druid, CrateDB).

This evaluator differs from evaluate/pipeline/evaluate_full_pipeline.py:
- Uses the full workflow (does NOT skip phase 0).
- Runs build + relevant tests in phase 0 and phase 4.
- Restricts agentic adaptation to Java code hunks from mainline.patch only.
- Applies developer backport hunks only in validation (phase 4), and only for
  test files, non-Java files, and auto-generated Java files.
"""

import argparse
import asyncio
import csv
import difflib
import io
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime
from typing import Any

from unidiff import PatchSet

# Add src to path
sys.path.append(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
)

from dotenv import load_dotenv

from agents.context_analyzer import context_analyzer_node
from agents.structural_locator import structural_locator_node
from graph import app
from utils.patch_analyzer import PatchAnalyzer
from utils.patch_complexity import classify_patch_complexity
from utils.token_counter import (
    aggregate_usage_from_messages,
    has_tiktoken,
    resolve_model_name,
)

load_dotenv()


def configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
DATASET_PATH = os.path.join(BASE_DIR, "datasets", "all_projects_final.csv")
REPOS_DIR = os.path.join(BASE_DIR, "temp_repo_storage")
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")
PHASE0_CACHE_DIR = os.path.join(os.path.dirname(__file__), "phase0_cache")

TARGET_PROJECTS = ["elasticsearch", "crate"]
MAX_PATCHES_PER_PROJECT = 10

RUN_MODE_FULL = "full"
RUN_MODE_PHASE1 = "phase1"
RUN_MODE_PHASE2 = "phase2"


def _new_run_id() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S%f")


def ensure_dirs() -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    os.makedirs(PHASE0_CACHE_DIR, exist_ok=True)
    for project in TARGET_PROJECTS:
        os.makedirs(os.path.join(RESULTS_DIR, project), exist_ok=True)


def _patch_results_dir(project: str, patch_id: str) -> str:
    return os.path.join(RESULTS_DIR, project, patch_id)


def _phase_output_file(
    project: str, patch_id: str, phase_name: str, agent_name: str
) -> str:
    return os.path.join(
        _patch_results_dir(project, patch_id), f"{phase_name}_{agent_name}.json"
    )


def _save_generated_patch_artifacts(
    project: str,
    patch_id: str,
    agent_only_patch_diff: str,
    final_effective_patch_diff: str,
) -> dict[str, str]:
    """
    Persist generated patch artifacts as standalone files for later inspection.
    """
    out_dir = _patch_results_dir(project, patch_id)
    os.makedirs(out_dir, exist_ok=True)

    agent_only_path = os.path.join(out_dir, "generated_patch_agent_only.patch")
    final_effective_path = os.path.join(
        out_dir, "generated_patch_final_effective.patch"
    )

    with open(agent_only_path, "w", encoding="utf-8") as f:
        f.write((agent_only_patch_diff or "").strip() + "\n")

    with open(final_effective_path, "w", encoding="utf-8") as f:
        f.write((final_effective_patch_diff or "").strip() + "\n")

    return {
        "agent_only": agent_only_path,
        "final_effective": final_effective_path,
    }


def is_phase_processed(
    project: str, patch_id: str, phase_name: str, agent_name: str
) -> bool:
    return os.path.exists(_phase_output_file(project, patch_id, phase_name, agent_name))


def _phase0_cache_file(project: str, backport_commit: str, original_commit: str) -> str:
    safe_project = (project or "unknown").strip().lower() or "unknown"
    safe_backport = (backport_commit or "unknown").strip() or "unknown"
    safe_original = (original_commit or "unknown").strip() or "unknown"
    filename = f"{safe_project}_{safe_backport[:12]}_{safe_original[:12]}.json"
    return os.path.join(PHASE0_CACHE_DIR, filename)


def _load_phase0_cache(
    project: str, backport_commit: str, original_commit: str
) -> dict[str, Any] | None:
    path = _phase0_cache_file(project, backport_commit, original_commit)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _is_phase0_cache_reusable(cache_payload: dict[str, Any] | None) -> tuple[bool, str]:
    """
    Reject stale/poisoned Phase 0 cache entries that cannot provide meaningful
    transition comparison data.
    """
    payload = cache_payload or {}
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


def run_cmd(cmd, cwd, env=None, timeout=None):
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
        return True, result.stdout
    except subprocess.CalledProcessError as e:
        return False, e.stderr or e.stdout
    except subprocess.TimeoutExpired:
        return False, f"Command timed out after {timeout} seconds"
    except Exception as e:
        return False, str(e)


def _is_test_file(file_path: str) -> bool:
    lower_path = (file_path or "").lower()
    return "test" in lower_path or lower_path.endswith("test.java")


def _is_java_code_file(file_path: str) -> bool:
    p = (file_path or "").lower()
    return p.endswith(".java") and not _is_test_file(p)


def _is_auto_generated_java_file(file_path: str) -> bool:
    """
    Detect if a Java file is auto-generated and should be skipped from agent modification.
    These files should not be regenerated by agents as they are generated from source files.

    Patterns detected:
    - ANTLR-generated files: *Lexer.java, *Parser.java (contain generated token/parsing code)
    - Protobuf-generated files: *OuterClass.java, *Pb.java, *PbOrBuilder.java
    - gRPC-generated files: *Grpc.java
    - Java code generator marker files: "// This file is automatically generated"

    Args:
        file_path: Path to the file

    Returns:
        True if the file appears to be auto-generated
    """
    if not file_path:
        return False

    normalized = file_path.lower()

    # ANTLR-generated file patterns (lexer and parser)
    # Patterns must be lowercase since normalized is lowercase
    antlr_patterns = [
        r"lexer\.java$",  # *Lexer.java (ANTLR lexer)
        r"parser\.java$",  # *Parser.java (ANTLR parser)
        r"baselistener\.java$",  # *BaseListener.java (ANTLR listener base)
        r"listener\.java$",  # *Listener.java (ANTLR listener interface, but be careful)
        r"basevisitor\.java$",  # *BaseVisitor.java (ANTLR visitor base)
        r"visitor\.java$",  # *Visitor.java (ANTLR visitor interface, but be careful)
    ]

    # Protobuf-generated file patterns
    protobuf_patterns = [
        r"outerclass\.java$",  # ProtobufClass$OuterClass.java
        r"pb\.java$",  # Proto.java (simplified proto naming)
        r"pborbuilder\.java$",  # Proto$..PbOrBuilder.java
    ]

    # gRPC-generated file patterns
    grpc_patterns = [
        r"grpc\.java$",  # *Grpc.java (gRPC service)
    ]

    all_patterns = antlr_patterns + protobuf_patterns + grpc_patterns

    for pattern in all_patterns:
        try:
            if re.search(pattern, normalized):
                return True
        except Exception:
            pass

    return False


def _is_non_java_hunk_in_java_file(file_path: str, hunk_text: str) -> bool:
    """
    Detect if a hunk within a Java file contains non-Java code content
    (SQL, YAML, JSON, XML, config strings, etc.).

    Args:
        file_path: Path to the file
        hunk_text: The hunk text (including @@ header lines)

    Returns:
        True if the file is a .java file AND the hunk contains non-code patterns
    """
    # Only apply to Java files (not test files)
    if not _is_java_code_file(file_path):
        return False

    # Extract lines from hunk, removing @@ headers and context markers
    lines = []
    for line in (hunk_text or "").split("\n"):
        stripped = line.strip()
        # Skip hunk headers (@@ ...) and empty lines
        if stripped.startswith("@@") or not stripped:
            continue
        # Remove diff markers (+/-) for content analysis
        if line and line[0] in ("+", "-"):
            lines.append(line[1:].strip())
        else:
            lines.append(line.strip())

    if not lines:
        return False

    content = "\n".join(lines)

    java_code_line_count = 0
    non_code_signal_count = 0

    java_like_rx = [
        r"\b(class|interface|enum|record)\b",
        r"\b(public|private|protected|static|final|abstract|synchronized|native)\b",
        r"\b(if|else|for|while|switch|case|default|return|throw|try|catch|finally|new)\b",
        r"\b[A-Za-z_][A-Za-z0-9_]*\s*\(",
        r"[{};]$",
    ]

    for raw in lines:
        s = (raw or "").strip()
        if not s:
            continue
        if re.match(r"^(case\s+[^:]+|default)\s*:\s*$", s):
            java_code_line_count += 1
            continue
        if any(re.search(rx, s) for rx in java_like_rx):
            java_code_line_count += 1

    # Patterns for non-Java content (regex patterns)
    patterns = [
        # SQL patterns (SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER)
        r"(?i)\b(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER)\s+(INTO|FROM|TABLE|DATABASE|VIEW|INDEX)",
        r"(?i)sql\s*=\s*[\"']",
        r"(?i)WHERE\s+\w+\s*[=<>]",
        r"(?i)\bJOIN\b",
        r"(?i)\bGROUP\s+BY\b",
        r"(?i)\bORDER\s+BY\b",
        # YAML patterns (key: value syntax)
        r"^\s*[a-zA-Z_][a-zA-Z0-9_\-]{1,30}:\s+\S",  # key: value
        r"^---$|^\.\.\.$",  # YAML document markers
        r"^!\w+\s",  # YAML tags
        # JSON patterns
        r'"\s*:\s*["{\\[\d\-]',  # "key": value pattern
        r"{\s*\"[^\"]+\"\s*:",
        # XML patterns
        r"<\?xml",
        r"<[a-zA-Z]+\s+[a-zA-Z:_][a-zA-Z0-9:_\-]*\s*=\s*[\"']",
        # Multi-line string/comment markers
        r'"""\s*$|""".+$',  # Triple quotes
        r"\/\*\*.*\*\/",  # Large block comments
    ]

    for pattern in patterns:
        try:
            m = re.search(pattern, content, re.MULTILINE)
            if not m:
                continue
            # Avoid false positives for Java switch labels like `case 0:` and `default:`.
            if pattern == r"^\s*[a-zA-Z_][a-zA-Z0-9_\-]{1,30}:\s+\S":
                ms = (m.group(0) or "").strip()
                if re.match(r"^(case\s+[^:]+|default)\s*:\s*$", ms):
                    continue
            non_code_signal_count += 1
        except Exception:
            # If regex is invalid, skip it
            pass

    if non_code_signal_count == 0:
        return False
    if java_code_line_count >= 2 and non_code_signal_count <= 1:
        return False
    return True


def setup_repos(mainline_commit, backport_commit, mainline_repo_path, target_repo_path):
    success, output = run_cmd(
        ["git", "checkout", mainline_commit],
        cwd=mainline_repo_path,
        timeout=300,
    )
    if not success:
        return False, f"Failed to checkout mainline commit {mainline_commit}: {output}"

    success, output = run_cmd(
        ["git", "checkout", f"{backport_commit}^"],
        cwd=target_repo_path,
        timeout=300,
    )
    if not success:
        return False, f"Failed to checkout target commit {backport_commit}^: {output}"

    return True, "Repos set up successfully"


def generate_mainline_patch(mainline_commit, mainline_repo_path):
    patch_path = os.path.join(RESULTS_DIR, "temp_mainline.patch")

    success, output = run_cmd(
        ["git", "format-patch", "-1", mainline_commit, "--stdout"],
        cwd=mainline_repo_path,
        timeout=300,
    )

    if not success:
        return None, f"Failed to generate patch: {output}"

    try:
        with open(patch_path, "w", encoding="utf-8") as f:
            f.write(output)
        return patch_path, output
    except Exception as e:
        return None, f"Failed to write patch file: {str(e)}"


def generate_developer_backport_patch(backport_commit, target_repo_path):
    success, output = run_cmd(
        ["git", "show", "--format=", "--no-color", backport_commit],
        cwd=target_repo_path,
        timeout=300,
    )
    if not success:
        return (
            None,
            f"Failed to generate developer backport patch for {backport_commit}: {output}",
        )
    return output, None


def _prepare_pipeline_inputs(
    project: str,
    patch_id: str,
    mainline_commit: str,
    backport_commit: str,
    mainline_repo_path: str,
    target_repo_path: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Shared setup for full/phase1/phase2 modes:
    - repo checkout
    - mainline patch generation
    - developer backport patch capture
    - Java-only patch analysis
    - auxiliary hunk extraction
    """
    success, output = setup_repos(
        mainline_commit,
        backport_commit,
        mainline_repo_path,
        target_repo_path,
    )
    if not success:
        return None, output

    patch_path, patch_output = generate_mainline_patch(
        mainline_commit, mainline_repo_path
    )
    if not patch_path:
        return None, patch_output

    developer_patch_diff, patch_err = generate_developer_backport_patch(
        backport_commit, target_repo_path
    )
    if patch_err:
        return None, patch_err

    project_dir = _patch_results_dir(project, patch_id)
    os.makedirs(project_dir, exist_ok=True)

    mainline_patch_file = os.path.join(project_dir, "mainline.patch")
    with open(mainline_patch_file, "w", encoding="utf-8") as f:
        f.write(patch_output)

    analyzer = PatchAnalyzer()
    full_patch_analysis = analyzer.analyze(patch_output, with_test_changes=True)
    java_only_patch_analysis = [
        fc for fc in full_patch_analysis if _is_java_code_file(fc.file_path)
    ]
    pair_consistency = _compute_pair_consistency(
        patch_output,
        developer_patch_diff,
        analyzer,
    )
    developer_aux_hunks = _build_auxiliary_hunks_from_developer_patch(
        developer_patch_diff
    )

    # Build agent-eligible patch (excludes test files, non-Java files, auto-generated files, non-code content)
    agent_eligible_patch = _build_agent_eligible_patch(patch_output)

    return (
        {
            "patch_path": patch_path,
            "patch_output": patch_output,
            "agent_eligible_patch": agent_eligible_patch,
            "developer_patch_diff": developer_patch_diff,
            "java_only_patch_analysis": java_only_patch_analysis,
            "pair_consistency": pair_consistency,
            "developer_aux_hunks": developer_aux_hunks,
        },
        None,
    )


def _build_auxiliary_hunks_from_developer_patch(
    patch_diff: str,
) -> list[dict[str, Any]]:
    """
    Build hunks for developer-owned changes that agentic system should not generate:
    - all test file hunks
    - all non-Java file hunks
    - auto-generated Java file hunks

    These hunks are applied directly during validation phase without agent modification.
    """
    if not patch_diff.strip():
        return []

    patch_set = PatchSet(io.StringIO(patch_diff))
    hunks: list[dict[str, Any]] = []

    def _norm_path(path: str | None) -> str:
        p = (path or "").strip().replace("\\", "/").lstrip("/")
        while p.startswith("a/") or p.startswith("b/"):
            p = p[2:]
        if p == "dev/null":
            return ""
        return p

    for patched_file in patch_set:
        file_path = _norm_path(patched_file.path)
        is_test_file_check = _is_test_file(file_path)
        is_non_java_file = not file_path.lower().endswith(".java")
        is_auto_generated = _is_auto_generated_java_file(file_path)

        # Process all files; we'll filter hunks per-file
        source_path = _norm_path(getattr(patched_file, "source_file", None))
        target_path = (
            _norm_path(getattr(patched_file, "target_file", None)) or file_path
        )

        # IMPORTANT: classify add/delete BEFORE rename.
        # Some diff parsers may expose source/target path differences for added files,
        # and we must preserve true create/delete semantics for validation apply.
        if patched_file.is_added_file:
            op = "ADDED"
        elif patched_file.is_removed_file:
            op = "DELETED"
        elif patched_file.is_rename or (
            source_path and target_path and source_path != target_path
        ):
            op = "RENAMED"
        else:
            op = "MODIFIED"

        # Process hunks in this file
        for hunk in patched_file:
            lines = [
                f"@@ -{hunk.source_start},{hunk.source_length} +{hunk.target_start},{hunk.target_length} @@\n"
            ]
            for line in hunk:
                if line.is_added:
                    lines.append(f"+{line.value}")
                elif line.is_removed:
                    lines.append(f"-{line.value}")
                else:
                    lines.append(f" {line.value}")

            hunk_text = "".join(lines)
            if not hunk_text.endswith("\n"):
                hunk_text += "\n"

            # Determine if this hunk should be auxiliary (skip from agent generation)
            is_auxiliary = (
                is_test_file_check  # Test files always auxiliary
                or is_non_java_file  # Non-Java files always auxiliary
                or is_auto_generated  # Auto-generated Java files always auxiliary
            )

            # Only add to auxiliary hunks if it should be skipped from agents
            if is_auxiliary:
                hunks.append(
                    {
                        "target_file": target_path,
                        "mainline_file": target_path,
                        "hunk_text": hunk_text,
                        "insertion_line": hunk.target_start,
                        "intent_verified": True,
                        "file_operation": op,
                        "old_target_file": source_path or None,
                    }
                )

        # Preserve pure structural file-operations with no hunks.
        # These are ALWAYS auxiliary (they should apply as-is during validation)
        if len(list(patched_file)) == 0:
            hunks.append(
                {
                    "target_file": target_path,
                    "mainline_file": target_path,
                    "hunk_text": "",
                    "insertion_line": 0,
                    "intent_verified": True,
                    "file_operation": op,
                    "old_target_file": source_path or None,
                }
            )

    return hunks


def _build_agent_eligible_patch(
    patch_diff: str,
) -> str:
    """
    Build a patch containing only hunks that agents should process.

    Filters out:
    - All test file hunks
    - All non-Java file hunks
    - All auto-generated Java file hunks

    Returns a valid patch diff that agents can safely modify.

    Args:
        patch_diff: Full patch diff

    Returns:
        Filtered patch containing only agent-eligible hunks
    """
    if not patch_diff.strip():
        return ""

    patch_set = PatchSet(io.StringIO(patch_diff))
    output_lines = []

    def _norm_path(path: str | None) -> str:
        p = (path or "").strip().replace("\\", "/")
        p = p.lstrip("/")
        while p.startswith("a/") or p.startswith("b/"):
            p = p[2:]
        if p == "dev/null":
            return ""
        return p

    for patched_file in patch_set:
        file_path = _norm_path(patched_file.path)

        # Skip non-agent-eligible files
        is_test_file = _is_test_file(file_path)
        is_non_java_file = not file_path.lower().endswith(".java")
        is_auto_generated = _is_auto_generated_java_file(file_path)

        if is_test_file or is_non_java_file or is_auto_generated:
            continue

        # Keep all hunks in eligible Java files.
        eligible_hunks = []
        for hunk in patched_file:
            lines = [
                f"@@ -{hunk.source_start},{hunk.source_length} +{hunk.target_start},{hunk.target_length} @@\n"
            ]
            for line in hunk:
                if line.is_added:
                    lines.append(f"+{line.value}")
                elif line.is_removed:
                    lines.append(f"-{line.value}")
                else:
                    lines.append(f" {line.value}")

            hunk_text = "".join(lines)
            if not hunk_text.endswith("\n"):
                hunk_text += "\n"

            eligible_hunks.append(hunk)

        # If the file has eligible hunks, include it in the output patch
        if eligible_hunks:
            # Write the file header
            source_file_raw = (
                getattr(patched_file, "source_file", None) or patched_file.path
            )
            target_file_raw = (
                getattr(patched_file, "target_file", None) or patched_file.path
            )
            source_file = _norm_path(source_file_raw)
            target_file = _norm_path(target_file_raw)

            diff_source = source_file or target_file
            diff_target = target_file or source_file

            output_lines.append(f"diff --git a/{diff_source} b/{diff_target}\n")

            # Write file operation markers
            if patched_file.is_added_file:
                output_lines.append("new file mode 100644\n")
            if patched_file.is_removed_file:
                output_lines.append("deleted file mode 100644\n")

            # Write index line (simplified)
            output_lines.append(f"index 0000000..0000000 100644\n")

            # Write --- and +++ lines
            src = "/dev/null" if patched_file.is_added_file else f"a/{source_file}"
            tgt = "/dev/null" if patched_file.is_removed_file else f"b/{target_file}"
            output_lines.append(f"--- {src}\n")
            output_lines.append(f"+++ {tgt}\n")

            # Write hunks
            for hunk in eligible_hunks:
                lines = [
                    f"@@ -{hunk.source_start},{hunk.source_length} +{hunk.target_start},{hunk.target_length} @@\n"
                ]
                for line in hunk:
                    if line.is_added:
                        lines.append(f"+{line.value}")
                    elif line.is_removed:
                        lines.append(f"-{line.value}")
                    else:
                        lines.append(f" {line.value}")

                output_lines.extend(lines)

    return "".join(output_lines)


def _extract_hunks_by_file_from_patch(
    patch_diff: str,
    analyzer: PatchAnalyzer,
    code_only: bool = True,
) -> dict[str, list[str]]:
    if not patch_diff:
        return {}

    hunks_by_file = analyzer.extract_raw_hunks(patch_diff, with_test_changes=False)
    if code_only:
        hunks_by_file = {
            file: hunks
            for file, hunks in hunks_by_file.items()
            if _is_java_code_file(file)
        }
    return hunks_by_file


def _collect_java_code_files_from_patch(
    patch_diff: str,
    analyzer: PatchAnalyzer,
) -> set[str]:
    hunks_by_file = _extract_hunks_by_file_from_patch(
        patch_diff,
        analyzer,
        code_only=True,
    )
    return set(hunks_by_file.keys())


def _compute_pair_consistency(
    mainline_patch_diff: str,
    developer_patch_diff: str,
    analyzer: PatchAnalyzer,
) -> dict[str, Any]:
    mainline_java = sorted(
        _collect_java_code_files_from_patch(mainline_patch_diff, analyzer)
    )
    developer_java = sorted(
        _collect_java_code_files_from_patch(developer_patch_diff, analyzer)
    )
    overlap = sorted(set(mainline_java) & set(developer_java))

    ratio = 1.0
    if mainline_java:
        ratio = len(overlap) / len(mainline_java)

    pair_mismatch = bool(mainline_java) and ratio < 0.5
    reason = "mainline_backport_scope_mismatch" if pair_mismatch else "scope_overlap_ok"

    return {
        "mainline_java_files": mainline_java,
        "developer_java_files": developer_java,
        "overlap_java_files": overlap,
        "overlap_ratio_mainline": ratio,
        "pair_mismatch": pair_mismatch,
        "reason": reason,
    }


def _build_hunk_comparison_markdown(
    developer_patch_diff: str,
    generated_patch_diff: str,
    analyzer: PatchAnalyzer,
    heading: str = "Hunk-by-Hunk Comparison",
):
    developer_hunks_by_file = analyzer.extract_raw_hunks(
        developer_patch_diff, with_test_changes=False
    )
    developer_hunks_by_file = {
        file: hunks
        for file, hunks in developer_hunks_by_file.items()
        if _is_java_code_file(file)
    }

    generated_hunks_by_file = _extract_hunks_by_file_from_patch(
        generated_patch_diff,
        analyzer,
        code_only=True,
    )

    all_files = set(generated_hunks_by_file.keys()) | set(
        developer_hunks_by_file.keys()
    )

    markdown = f"## {heading}\n\n"
    if not all_files:
        return (
            markdown
            + "No Java code hunks found in either developer or generated patch.\n\n"
        )

    for file in sorted(all_files):
        gen_hunks = generated_hunks_by_file.get(file, [])
        dev_hunks = developer_hunks_by_file.get(file, [])
        max_hunks = max(len(gen_hunks), len(dev_hunks))

        markdown += f"### {file}\n\n"
        markdown += f"- Developer hunks: {len(dev_hunks)}\n"
        markdown += f"- Generated hunks: {len(gen_hunks)}\n\n"

        for i in range(max_hunks):
            gen = gen_hunks[i] if i < len(gen_hunks) else "*No hunk*"
            dev = dev_hunks[i] if i < len(dev_hunks) else "*No hunk*"

            dev_lines = dev.splitlines(keepends=True)
            gen_lines = gen.splitlines(keepends=True)
            delta = "".join(
                difflib.unified_diff(
                    dev_lines,
                    gen_lines,
                    fromfile="developer",
                    tofile="generated",
                    lineterm="",
                )
            )
            if not delta:
                delta = "(No textual difference)\n"

            markdown += f"#### Hunk {i + 1}\n\n"
            markdown += "Developer\n"
            markdown += f"```diff\n{dev}\n```\n\n"
            markdown += "Generated\n"
            markdown += f"```diff\n{gen}\n```\n\n"
            markdown += "Developer -> Generated (Unified Diff)\n"
            markdown += f"```diff\n{delta}\n```\n\n"

        markdown += "\n"

    return markdown


def _normalize_hunk_header_for_operation(hunk_text: str, file_operation: str) -> str:
    if not hunk_text:
        return hunk_text

    lines = hunk_text.splitlines()
    if not lines:
        return hunk_text

    header = lines[0]
    m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$", header)
    if not m:
        return hunk_text

    old_start = int(m.group(1))
    old_len = int(m.group(2) or "1")
    new_start = int(m.group(3))
    new_len = int(m.group(4) or "1")
    suffix = m.group(5)

    op = (file_operation or "MODIFIED").upper()
    if op == "ADDED":
        old_start, old_len = 0, 0
        if new_start <= 0:
            new_start = 1
    elif op == "DELETED":
        new_start, new_len = 0, 0
        if old_start <= 0:
            old_start = 1

    lines[0] = f"@@ -{old_start},{old_len} +{new_start},{new_len} @@{suffix}"
    normalized = "\n".join(lines)
    if hunk_text.endswith("\n"):
        normalized += "\n"
    return normalized


def _build_generated_patch_from_hunks(adapted_code_hunks: list[dict[str, Any]]) -> str:
    """
    Build a complete unified diff string from adapted_code_hunks.

    Supports two hunk_text formats:
    1. Full git diff (starts with 'diff --git ...') - produced by the new file_editor
       architecture. These are passed through directly.
    2. Bare hunk text (starts with '@@') - produced by the legacy hunk_generator.
       These are wrapped with diff --git / --- / +++ headers as before.
    """

    def _norm(path: str | None) -> str:
        p = (path or "").strip().replace("\\", "/").lstrip("/")
        while p.startswith("a/") or p.startswith("b/"):
            p = p[2:]
        if p == "dev/null":
            return ""
        return p

    def _is_full_git_diff(hunk_text: str) -> bool:
        return bool(hunk_text and hunk_text.lstrip().startswith("diff --git"))

    # Full git-diff payloads (file_editor architecture) are already complete.
    # Keep them as-is and only wrap legacy bare-@@ hunks.
    full_diff_parts: list[str] = []
    full_diff_targets: set[str] = set()
    legacy_hunks: list[dict[str, Any]] = []

    for hunk in adapted_code_hunks or []:
        hunk_text = hunk.get("hunk_text", "")
        if _is_full_git_diff(hunk_text):
            ht = (hunk_text or "").rstrip("\n")
            if ht:
                full_diff_parts.append(ht)
            tf = _norm(hunk.get("target_file"))
            if tf:
                full_diff_targets.add(tf)
        else:
            legacy_hunks.append(hunk)

    # --- Legacy path: bare @@ hunks (hunk_generator architecture) ---
    hunks_by_file: dict[str, dict[str, Any]] = {}
    file_order: list[str] = []

    for hunk in legacy_hunks:
        target_file = _norm(hunk.get("target_file"))
        if not target_file:
            continue
        if target_file in full_diff_targets:
            # File already covered by a complete git diff payload.
            continue

        if target_file not in hunks_by_file:
            file_operation = (hunk.get("file_operation") or "MODIFIED").upper()
            old_target_file = _norm(
                hunk.get("old_target_file") or hunk.get("mainline_file")
            )
            hunks_by_file[target_file] = {
                "file_operation": file_operation,
                "old_target_file": old_target_file,
                "hunks": [],
            }
            file_order.append(target_file)

        existing_old = hunks_by_file[target_file].get("old_target_file") or ""
        incoming_old = _norm(hunk.get("old_target_file") or hunk.get("mainline_file"))
        if not existing_old and incoming_old:
            hunks_by_file[target_file]["old_target_file"] = incoming_old

        hunks_by_file[target_file]["hunks"].append(hunk.get("hunk_text", ""))

    lines: list[str] = []
    for target_file in file_order:
        payload = hunks_by_file[target_file]
        op = (payload.get("file_operation") or "MODIFIED").upper()
        old_target_file = _norm(payload.get("old_target_file")) or target_file

        if op == "RENAMED" and old_target_file and old_target_file != target_file:
            lines.append(f"diff --git a/{old_target_file} b/{target_file}")
            lines.append(f"rename from {old_target_file}")
            lines.append(f"rename to {target_file}")
            lines.append(f"--- a/{old_target_file}")
            lines.append(f"+++ b/{target_file}")
        else:
            lines.append(f"diff --git a/{target_file} b/{target_file}")
            if op == "ADDED":
                lines.append("--- /dev/null")
                lines.append(f"+++ b/{target_file}")
            elif op == "DELETED":
                lines.append(f"--- a/{target_file}")
                lines.append("+++ /dev/null")
            else:
                lines.append(f"--- a/{target_file}")
                lines.append(f"+++ b/{target_file}")

        if op == "MODIFIED":
            has_full_create_hunk = any(
                (h or "").lstrip().startswith("@@ -0,0 +")
                for h in payload.get("hunks", [])
            )
            if has_full_create_hunk:
                lines[-2:] = ["--- /dev/null", f"+++ b/{target_file}"]
                op = "ADDED"

        if op == "ADDED" and lines[-2] != "--- /dev/null":
            lines.append("--- /dev/null")
            lines.append(f"+++ b/{target_file}")
        elif op == "DELETED" and lines[-1] != "+++ /dev/null":
            lines.append(f"--- a/{target_file}")
            lines.append("+++ /dev/null")

        for hunk_text in payload.get("hunks", []):
            if not hunk_text:
                continue
            normalized_hunk = _normalize_hunk_header_for_operation(hunk_text, op)
            if not hunk_text.endswith("\n"):
                lines.append(normalized_hunk)
            else:
                lines.append(normalized_hunk.rstrip("\n"))

    all_parts = []
    if full_diff_parts:
        all_parts.append("\n".join(full_diff_parts).rstrip("\n"))
    if lines:
        all_parts.append("\n".join(lines).rstrip("\n"))

    if not all_parts:
        return ""
    return "\n".join(all_parts) + "\n"


def _get_java_code_changed_files(commit, repo_path):
    success, output = run_cmd(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit],
        cwd=repo_path,
        timeout=300,
    )
    if not success:
        return None, output

    files = [line.strip() for line in output.splitlines() if line.strip()]
    files = [f for f in files if _is_java_code_file(f)]
    return sorted(set(files)), None


def _get_blob_id_from_commit(repo_path, commit, rel_path):
    success, output = run_cmd(
        ["git", "rev-parse", f"{commit}:{rel_path}"],
        cwd=repo_path,
        timeout=300,
    )
    if not success:
        return None
    return output.strip() or None


def _get_blob_id_from_index(repo_path, rel_path, env):
    success, output = run_cmd(
        ["git", "rev-parse", f":{rel_path}"],
        cwd=repo_path,
        env=env,
        timeout=300,
    )
    if not success:
        return None
    return output.strip() or None


def _get_blob_content(repo_path, blob_id):
    if not blob_id:
        return None

    success, output = run_cmd(
        ["git", "cat-file", "-p", blob_id],
        cwd=repo_path,
        timeout=300,
    )
    if not success:
        return None
    return output


def _normalize_content_for_code_line_compare(content: str | None) -> list[str] | None:
    if content is None:
        return None

    normalized_lines = []
    for line in content.splitlines():
        normalized_lines.append(re.sub(r"\s+", "", line))
    return normalized_lines


def _apply_patch_with_temp_index(repo_path, patch_file_path, env):
    attempts = [
        [
            "git",
            "apply",
            "--cached",
            "--recount",
            "--whitespace=nowarn",
            patch_file_path,
        ],
        [
            "git",
            "apply",
            "--cached",
            "--recount",
            "--ignore-space-change",
            "--ignore-whitespace",
            "--whitespace=nowarn",
            patch_file_path,
        ],
    ]

    errors = []
    for cmd in attempts:
        success, output = run_cmd(cmd, cwd=repo_path, env=env, timeout=300)
        if success:
            return True, ""
        errors.append(output)

    return False, "\n\n".join(errors)


def compare_generated_with_developer_patch(
    adapted_code_hunks,
    developer_patch_diff,
    backport_commit,
    target_repo_path,
    compare_files_scope: list[str] | None = None,
):
    code_only_hunks = [
        h
        for h in (adapted_code_hunks or [])
        if _is_java_code_file((h or {}).get("target_file", ""))
    ]
    generated_patch_diff = _build_generated_patch_from_hunks(code_only_hunks)
    generated_files = sorted(
        {
            h.get("target_file")
            for h in code_only_hunks
            if h.get("target_file") and _is_java_code_file(h.get("target_file"))
        }
    )

    developer_files, files_err = _get_java_code_changed_files(
        backport_commit, target_repo_path
    )
    if files_err:
        return {
            "exact_developer_patch": False,
            "comparison_method": "file_state",
            "error": f"Failed to get changed files for {backport_commit}: {files_err}",
            "generated_patch_diff": generated_patch_diff,
        }

    if compare_files_scope:
        files_to_compare = sorted(
            {
                str(p).replace("\\", "/").strip().lstrip("/")
                for p in compare_files_scope
                if str(p).strip()
            }
        )
    else:
        files_to_compare = sorted(set((developer_files or []) + generated_files))

    with tempfile.NamedTemporaryFile(
        prefix="git_index_", suffix=".tmp", delete=False
    ) as index_file:
        temp_index_path = index_file.name

    with tempfile.NamedTemporaryFile(
        prefix="generated_eval_",
        suffix=".patch",
        delete=False,
        mode="w",
        encoding="utf-8",
    ) as patch_file:
        patch_file.write(generated_patch_diff)
        generated_patch_file = patch_file.name

    index_env = dict(os.environ)
    index_env["GIT_INDEX_FILE"] = temp_index_path

    try:
        success, output = run_cmd(
            ["git", "read-tree", f"{backport_commit}^"],
            cwd=target_repo_path,
            env=index_env,
            timeout=300,
        )
        if not success:
            return {
                "exact_developer_patch": False,
                "comparison_method": "file_state",
                "error": f"Failed to seed temporary index from {backport_commit}^: {output}",
                "generated_patch_diff": generated_patch_diff,
            }

        if generated_patch_diff.strip():
            apply_ok, apply_err = _apply_patch_with_temp_index(
                target_repo_path, generated_patch_file, index_env
            )
            if not apply_ok:
                return {
                    "exact_developer_patch": False,
                    "comparison_method": "file_state",
                    "error": f"Failed to apply generated patch in temporary index: {apply_err}",
                    "generated_patch_diff": generated_patch_diff,
                }

        mismatched_files = []
        for rel_path in files_to_compare:
            developer_blob = _get_blob_id_from_commit(
                target_repo_path, backport_commit, rel_path
            )
            generated_blob = _get_blob_id_from_index(
                target_repo_path, rel_path, index_env
            )
            if developer_blob == generated_blob:
                continue

            developer_content = _get_blob_content(target_repo_path, developer_blob)
            generated_content = _get_blob_content(target_repo_path, generated_blob)

            developer_normalized = _normalize_content_for_code_line_compare(
                developer_content
            )
            generated_normalized = _normalize_content_for_code_line_compare(
                generated_content
            )

            if developer_normalized != generated_normalized:
                mismatched_files.append(rel_path)

        return {
            "exact_developer_patch": len(mismatched_files) == 0,
            "comparison_method": "file_state",
            "comparison_normalization": "line_by_line_whitespace_insensitive",
            "compared_files": files_to_compare,
            "mismatched_files": mismatched_files,
            "developer_files": developer_files,
            "generated_files": generated_files,
            "generated_patch_diff": generated_patch_diff,
        }
    finally:
        try:
            os.remove(temp_index_path)
        except Exception:
            pass
        try:
            os.remove(generated_patch_file)
        except Exception:
            pass


def save_agent_state(project, patch_id, phase_name, state_dict, agent_name=None):
    project_dir = os.path.join(RESULTS_DIR, project, patch_id)
    os.makedirs(project_dir, exist_ok=True)

    output_file = os.path.join(
        project_dir,
        f"{phase_name}_{agent_name}.json" if agent_name else f"{phase_name}_state.json",
    )

    serializable_state = {}
    for key, value in state_dict.items():
        try:
            json.dumps(value)
            serializable_state[key] = value
        except (TypeError, ValueError):
            serializable_state[key] = str(value)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(serializable_state, f, indent=2, default=str)

    return output_file


def load_agent_state(
    project: str, patch_id: str, phase_name: str, agent_name: str
) -> dict[str, Any] | None:
    output_file = _phase_output_file(project, patch_id, phase_name, agent_name)
    if not os.path.exists(output_file):
        return None

    try:
        with open(output_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_pipeline_log(project, patch_id, phase_name, log_content, run_id: str = ""):
    project_dir = os.path.join(RESULTS_DIR, project, patch_id)
    os.makedirs(project_dir, exist_ok=True)

    log_file = os.path.join(project_dir, f"{phase_name}_log.md")

    prefix = f"run_id={run_id}\n\n" if str(run_id or "").strip() else ""

    with open(log_file, "w", encoding="utf-8") as f:
        f.write(prefix + str(log_content or ""))

    return log_file


def _extract_transition_eval_from_outputs(
    phase_outputs: dict[str, Any],
) -> dict[str, Any] | None:
    phase0_eval = (
        phase_outputs.get("phase0", {})
        .get("phase_0_optimistic", {})
        .get("outputs", {})
        .get("phase_0_transition_evaluation")
    )
    if phase0_eval:
        return phase0_eval

    phase4_eval = (
        phase_outputs.get("phase4_validation", {})
        .get("validation", {})
        .get("outputs", {})
        .get("phase_0_transition_evaluation")
    )
    if phase4_eval:
        return phase4_eval

    return (
        phase_outputs.get("phase4_validation", {})
        .get("validation", {})
        .get("outputs", {})
        .get("validation_results", {})
        .get("tests", {})
        .get("state_transition")
    )


def _extract_touched_test_classes(
    phase_outputs: dict[str, Any], phase0_cache: dict[str, Any] | None = None
) -> list[str]:
    phase0_targets = (
        phase_outputs.get("phase0", {})
        .get("phase_0_optimistic", {})
        .get("outputs", {})
        .get("phase_0_test_targets", {})
    )
    if not phase0_targets and phase0_cache:
        phase0_targets = phase0_cache.get("phase_0_test_targets", {})

    targets = list((phase0_targets or {}).get("test_targets") or [])
    classes = []
    for item in targets:
        if ":" in item:
            classes.append(item.split(":", 1)[1].strip())
        elif item:
            classes.append(str(item).strip())

    return sorted({c for c in classes if c})


def _extract_baseline_and_patched_test_results(
    phase_outputs: dict[str, Any], phase0_cache: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    phase0_outputs = (
        phase_outputs.get("phase0", {}).get("phase_0_optimistic", {}).get("outputs", {})
    )

    baseline_result = (
        phase0_outputs.get("phase_0_baseline_test_result")
        or (phase0_cache or {}).get("phase_0_baseline_test_result")
        or {}
    )

    phase4_tests = (
        phase_outputs.get("phase4_validation", {})
        .get("validation", {})
        .get("outputs", {})
        .get("validation_results", {})
        .get("tests", {})
    )

    if (phase4_tests or {}).get("test_state"):
        patched_result = {"test_state": (phase4_tests or {}).get("test_state") or {}}
    else:
        patched_result = (
            phase0_outputs.get("phase_0_post_patch_test_result")
            or (phase0_cache or {}).get("phase_0_post_patch_test_result")
            or {}
        )

    return baseline_result, patched_result


def _build_touched_test_state_markdown(
    touched_classes: list[str],
    baseline_result: dict[str, Any],
    patched_result: dict[str, Any],
) -> str:
    if not touched_classes:
        return "- Touched tests (from patch): []\n"

    baseline_state = (baseline_result or {}).get("test_state") or {}
    patched_state = (patched_result or {}).get("test_state") or {}

    baseline_cases = baseline_state.get("test_cases") or {}
    patched_cases = patched_state.get("test_cases") or {}
    baseline_classes = baseline_state.get("classes") or {}
    patched_classes = patched_state.get("classes") or {}
    patched_missing = not patched_cases and not patched_classes

    lines = [f"- Touched tests (from patch): {touched_classes}"]
    for cls in touched_classes:
        class_case_keys = sorted(
            {
                key
                for key in set(baseline_cases.keys()) | set(patched_cases.keys())
                if key.startswith(f"{cls}#")
            }
        )
        if class_case_keys:
            for key in class_case_keys:
                old_status = baseline_cases.get(key, "absent")
                new_status = patched_cases.get(
                    key, "unknown" if patched_missing else "absent"
                )
                lines.append(f"  - {key}: baseline={old_status}, patched={new_status}")
        else:
            old_status = baseline_classes.get(cls, "absent")
            new_status = patched_classes.get(
                cls, "unknown" if patched_missing else "absent"
            )
            lines.append(f"  - {cls}: baseline={old_status}, patched={new_status}")

    return "\n".join(lines) + "\n"


def _build_transition_summary_markdown(
    transition_eval: dict[str, Any] | None,
    source_label: str,
    phase_outputs: dict[str, Any] | None = None,
    phase0_cache: dict[str, Any] | None = None,
) -> str:
    if not transition_eval:
        return "# Transition Summary\n\nNo transition evaluation available.\n"

    fail_to_pass = transition_eval.get("fail_to_pass", []) or []
    newly_passing = transition_eval.get("newly_passing", []) or []
    pass_to_fail = transition_eval.get("pass_to_fail", []) or []
    reason = transition_eval.get("reason", "Unknown reason.")
    valid = bool(transition_eval.get("valid_backport_signal", False))
    phase_outputs = phase_outputs or {}

    touched_classes = _extract_touched_test_classes(phase_outputs, phase0_cache)
    baseline_result, patched_result = _extract_baseline_and_patched_test_results(
        phase_outputs, phase0_cache
    )
    touched_state_markdown = _build_touched_test_state_markdown(
        touched_classes, baseline_result, patched_result
    )

    return (
        "# Transition Summary\n\n"
        f"- Source: {source_label}\n"
        f"- Valid backport signal: {valid}\n"
        f"- Reason: {reason}\n"
        f"- fail->pass ({len(fail_to_pass)}): {fail_to_pass}\n"
        f"- newly passing ({len(newly_passing)}): {newly_passing}\n"
        f"- pass->fail ({len(pass_to_fail)}): {pass_to_fail}\n"
        "\n## Touched Test States\n"
        f"{touched_state_markdown}"
    )


def _extract_recovery_outputs(phase_outputs: dict[str, Any]) -> dict[str, Any]:
    primary = (
        phase_outputs.get("phase_recovery_agent", {})
        .get("recovery_agent", {})
        .get("outputs", {})
    )
    if primary:
        return primary
    return (
        phase_outputs.get("recovery_agent", {})
        .get("recovery_agent", {})
        .get("outputs", {})
    )


def _extract_validation_outputs(phase_outputs: dict[str, Any]) -> dict[str, Any]:
    return (
        phase_outputs.get("phase4_validation", {})
        .get("validation", {})
        .get("outputs", {})
    )


def _extract_file_editor_outputs(phase_outputs: dict[str, Any]) -> dict[str, Any]:
    return (
        phase_outputs.get("phase3_hunk_generator", {})
        .get("hunk_generator", {})
        .get("outputs", {})
    )


def _build_recovery_intelligence_report(
    phase_outputs: dict[str, Any],
) -> dict[str, Any]:
    ro = _extract_recovery_outputs(phase_outputs)
    vo = _extract_validation_outputs(phase_outputs)
    fo = _extract_file_editor_outputs(phase_outputs)

    brief = ro.get("recovery_brief") if isinstance(ro, dict) else {}
    obligations = ro.get("recovery_obligations") if isinstance(ro, dict) else []
    decisions = ro.get("recovery_decisions") if isinstance(ro, dict) else []
    strategy_history = (
        ro.get("recovery_strategy_history") if isinstance(ro, dict) else []
    )
    scope_files = ro.get("recovery_scope_files") if isinstance(ro, dict) else []

    diag = brief.get("diagnosis") if isinstance(brief, dict) else {}
    drift_type = str((diag or {}).get("kind") or "")

    obligations_total = len(obligations) if isinstance(obligations, list) else 0
    obligations_covered = 0
    if isinstance(decisions, list):
        obligations_covered = sum(
            1
            for d in decisions
            if isinstance(d, dict)
            and str(d.get("status") or "").strip().lower()
            in {"edited", "verified_no_change", "blocked"}
        )

    side_files = []
    if isinstance(brief, dict):
        rb = brief.get("rulebook_decision") or {}
        if isinstance(rb, dict):
            side_files = list(rb.get("additional_files") or [])

    retries_to_success = int(vo.get("validation_attempts") or 0)
    status = str(ro.get("recovery_agent_status") or "")
    stagnation_break_reason = ""
    if status == "no_fix_found":
        stagnation_break_reason = str(ro.get("recovery_agent_summary") or "")

    recovery_tokens = ro.get("token_usage") if isinstance(ro, dict) else {}

    edited_files = {
        str((e or {}).get("target_file") or "")
        for e in (fo.get("adapted_file_edits") or [])
        if isinstance(e, dict)
    }
    side_file_edits_count = len([f for f in side_files if str(f) in edited_files])

    return {
        "recovered_drift_type": drift_type,
        "connected_files_discovered": sorted(
            {str(f) for f in (scope_files or []) if str(f).strip()}
        ),
        "side_files_suggested": [str(f) for f in side_files if str(f).strip()],
        "side_file_edits_count": int(side_file_edits_count),
        "obligation_coverage": {
            "total": int(obligations_total),
            "covered": int(obligations_covered),
            "ratio": (float(obligations_covered) / float(obligations_total))
            if obligations_total
            else 1.0,
        },
        "retries_to_success": retries_to_success,
        "stagnation_break_reason": stagnation_break_reason,
        "strategy_history": [
            str(s) for s in (strategy_history or []) if str(s).strip()
        ],
        "recovery_token_usage": recovery_tokens
        if isinstance(recovery_tokens, dict)
        else {},
    }


async def run_full_pipeline(
    project,
    patch_id,
    mainline_commit,
    backport_commit,
    mainline_repo_path,
    target_repo_path,
    run_mode: str = RUN_MODE_FULL,
    force_phase: bool = False,
):
    run_id = _new_run_id()
    results = {
        "run_id": run_id,
        "project": project,
        "patch_id": patch_id,
        "mainline_commit": mainline_commit,
        "backport_commit": backport_commit,
        "run_mode": run_mode,
        "timestamp": datetime.now().isoformat(),
        "phases": {},
    }

    patch_dir = _patch_results_dir(project, patch_id)
    os.makedirs(patch_dir, exist_ok=True)
    runtime_log_path = os.path.join(patch_dir, "log.txt")
    runtime_tokens_path = os.path.join(patch_dir, "tokens.txt")
    token_totals = {
        "run_id": run_id,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "messages_with_usage": 0,
        "estimated_messages": 0,
        "tokenizer": {
            "model": resolve_model_name(),
            "library": "tiktoken",
            "available": has_tiktoken(),
        },
        "by_node": {},
    }

    class _Tee:
        def __init__(self, *streams):
            self.streams = streams

        def write(self, data):
            for s in self.streams:
                try:
                    s.write(data)
                except Exception:
                    pass
            return len(data)

        def flush(self):
            for s in self.streams:
                try:
                    s.flush()
                except Exception:
                    pass

    def _estimate_tokens_from_text(text: str) -> int:
        # Coarse fallback: ~4 chars/token for English/code mix.
        t = str(text or "")
        if not t:
            return 0
        return max(1, len(t) // 4)

    def _add_tokens(
        node_name: str,
        input_tokens: int,
        output_tokens: int,
        *,
        estimated: bool,
        source: str,
    ) -> None:
        in_t = int(input_tokens or 0)
        out_t = int(output_tokens or 0)
        if in_t == 0 and out_t == 0:
            return

        token_totals["input_tokens"] += in_t
        token_totals["output_tokens"] += out_t
        token_totals["total_tokens"] = (
            token_totals["input_tokens"] + token_totals["output_tokens"]
        )
        if estimated:
            token_totals["estimated_messages"] += 1
        else:
            token_totals["messages_with_usage"] += 1

        node_bucket = token_totals["by_node"].setdefault(
            node_name,
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "estimated_events": 0,
                "exact_events": 0,
                "sources": [],
            },
        )
        node_bucket["input_tokens"] += in_t
        node_bucket["output_tokens"] += out_t
        node_bucket["total_tokens"] = (
            node_bucket["input_tokens"] + node_bucket["output_tokens"]
        )
        if estimated:
            node_bucket["estimated_events"] += 1
        else:
            node_bucket["exact_events"] += 1
        node_bucket["sources"].append(source)

    def _consume_usage(node_name: str, usage: dict | None, source: str) -> bool:
        if not isinstance(usage, dict) or not usage:
            return False
        in_t = usage.get("input_tokens", usage.get("prompt_tokens", 0))
        out_t = usage.get("output_tokens", usage.get("completion_tokens", 0))
        total = usage.get("total_tokens")
        if total and (not in_t and not out_t):
            # Split unknown direction 50/50 as fallback.
            in_t = int(total) // 2
            out_t = int(total) - int(in_t)
        _add_tokens(
            node_name, int(in_t or 0), int(out_t or 0), estimated=False, source=source
        )
        return bool(in_t or out_t or total)

    def _collect_tokens_from_node_output(
        node_name: str, node_output: dict[str, Any]
    ) -> None:
        exact_hit = False

        msgs = node_output.get("messages", []) if isinstance(node_output, dict) else []
        agg = aggregate_usage_from_messages(msgs)
        if agg.get("input_tokens") or agg.get("output_tokens"):
            if _consume_usage(
                node_name,
                {
                    "input_tokens": agg.get("input_tokens", 0),
                    "output_tokens": agg.get("output_tokens", 0),
                    "total_tokens": agg.get("total_tokens", 0),
                },
                "msg.aggregate_usage_from_messages",
            ):
                exact_hit = True
        elif _consume_usage(
            node_name, node_output.get("token_usage"), "node.token_usage"
        ):
            exact_hit = True

        if not exact_hit:
            for m in msgs:
                md = getattr(m, "response_metadata", {}) or {}
                if isinstance(md, dict):
                    if _consume_usage(
                        node_name,
                        md.get("token_usage"),
                        "msg.response_metadata.token_usage",
                    ):
                        exact_hit = True
                    if _consume_usage(
                        node_name,
                        md.get("usage"),
                        "msg.response_metadata.usage",
                    ):
                        exact_hit = True

                um = getattr(m, "usage_metadata", {}) or {}
                if _consume_usage(node_name, um, "msg.usage_metadata"):
                    exact_hit = True

        if not exact_hit and msgs:
            in_chars = 0
            out_chars = 0
            for m in msgs:
                txt = getattr(m, "content", "")
                s = txt if isinstance(txt, str) else str(txt)
                if getattr(m, "type", "") == "ai":
                    out_chars += len(s)
                else:
                    in_chars += len(s)

            _add_tokens(
                node_name,
                _estimate_tokens_from_text("x" * in_chars),
                _estimate_tokens_from_text("x" * out_chars),
                estimated=True,
                source="estimated_from_message_chars",
            )

    def _append_runtime_log(message: str) -> None:
        with open(runtime_log_path, "a", encoding="utf-8") as lf:
            lf.write(message.rstrip("\n") + "\n")

    # Reset per-patch runtime log at start.
    with open(runtime_log_path, "w", encoding="utf-8") as lf:
        lf.write(
            f"run_id={run_id}\n"
            f"patch_id={patch_id}\n"
            f"project={project}\n"
            f"mainline_commit={mainline_commit}\n"
            f"backport_commit={backport_commit}\n"
            f"mode={run_mode}\n"
            f"started_at={datetime.now().isoformat()}\n"
            "----------------------------------------\n"
        )

    _orig_stdout = sys.stdout
    _orig_stderr = sys.stderr
    _log_stream = open(runtime_log_path, "a", encoding="utf-8")
    sys.stdout = _Tee(_orig_stdout, _log_stream)
    sys.stderr = _Tee(_orig_stderr, _log_stream)

    try:
        if run_mode not in {RUN_MODE_FULL, RUN_MODE_PHASE1, RUN_MODE_PHASE2}:
            results["status"] = "failed"
            results["error"] = f"Unsupported run mode: {run_mode}"
            return results

        print(
            f"\n[{project}/{patch_id}] Setting up repositories for mode={run_mode}..."
        )
        prepared_inputs, prepare_err = _prepare_pipeline_inputs(
            project=project,
            patch_id=patch_id,
            mainline_commit=mainline_commit,
            backport_commit=backport_commit,
            mainline_repo_path=mainline_repo_path,
            target_repo_path=target_repo_path,
        )
        if prepare_err:
            results["setup_error"] = prepare_err
            results["status"] = "failed"
            return results

        patch_path = prepared_inputs["patch_path"]
        patch_output = prepared_inputs["patch_output"]
        agent_eligible_patch = prepared_inputs.get("agent_eligible_patch", patch_output)
        developer_patch_diff = prepared_inputs["developer_patch_diff"]
        java_only_patch_analysis = prepared_inputs["java_only_patch_analysis"]
        pair_consistency = prepared_inputs.get("pair_consistency") or {}
        developer_aux_hunks = prepared_inputs["developer_aux_hunks"]
        analyzer = PatchAnalyzer()

        save_pipeline_log(
            project,
            patch_id,
            "phase0",
            f"# Phase 0 Inputs\n\n"
            f"- Mainline commit: {mainline_commit}\n"
            f"- Backport commit: {backport_commit}\n"
            f"- Java-only files for agentic phases: {len(java_only_patch_analysis)}\n"
            f"- Developer auxiliary hunks (test + non-Java): {len(developer_aux_hunks)}\n\n"
            f"## Commit Pair Consistency\n"
            f"- Pair mismatch: {pair_consistency.get('pair_mismatch', False)}\n"
            f"- Reason: {pair_consistency.get('reason', 'unknown')}\n"
            f"- Mainline Java files: {pair_consistency.get('mainline_java_files', [])}\n"
            f"- Developer Java files: {pair_consistency.get('developer_java_files', [])}\n"
            f"- Overlap Java files: {pair_consistency.get('overlap_java_files', [])}\n"
            f"- Overlap ratio (mainline): {pair_consistency.get('overlap_ratio_mainline', 0)}\n\n"
            f"## Mainline Patch\n```diff\n{patch_output}\n```\n",
            run_id=run_id,
        )

        base_inputs = {
            "messages": ["Start"],
            "patch_path": patch_path,
            "patch_diff": agent_eligible_patch,
            "patch_analysis": java_only_patch_analysis,
            "target_repo_path": target_repo_path,
            "mainline_repo_path": mainline_repo_path,
            "experiment_mode": True,
            "backport_commit": backport_commit,
            "original_commit": mainline_commit,
            "skip_phase_0": False,
            "compile_only": False,
            "apply_only_validation": False,
            "skip_compilation_checks": False,
            "evaluation_full_workflow": True,
            "with_test_changes": False,
            "developer_auxiliary_hunks": developer_aux_hunks,
            "pair_consistency": pair_consistency,
            "use_phase_0_cache": True,
        }

        # Always pre-compute deterministic complexity so cached Phase 0 runs do not
        # lose routing context and accidentally default into REWRITE.
        try:
            complexity_info = classify_patch_complexity(
                patch_diff=agent_eligible_patch,
                target_repo_path=target_repo_path,
                with_test_changes=False,
            )
            base_inputs["patch_complexity"] = str(
                complexity_info.get("complexity") or "REWRITE"
            )
            base_inputs["complexity_reason"] = str(
                complexity_info.get("reason") or "unknown"
            )
            base_inputs["complexity_details"] = complexity_info.get("details") or {}
        except Exception as e:
            print(
                f"[{project}/{patch_id}] Warning: complexity preclassification failed: {e}"
            )
            base_inputs["patch_complexity"] = "REWRITE"
            base_inputs["complexity_reason"] = "preclassification_error"
            base_inputs["complexity_details"] = {}

        if run_mode == RUN_MODE_PHASE1:
            _append_runtime_log("Running mode=phase1")
            phase_name = "phase1_context_analyzer"
            agent_name = "context_analyzer"
            if not force_phase and is_phase_processed(
                project, patch_id, phase_name, agent_name
            ):
                print(
                    f"[{project}/{patch_id}] Skipping {run_mode}: phase output already exists"
                )
                results["status"] = "skipped"
                results["skip_reason"] = f"{phase_name}_{agent_name} already processed"
                return results

            print(f"[{project}/{patch_id}] Running {phase_name} only...")
            phase1_result = await context_analyzer_node(base_inputs, config={})
            _collect_tokens_from_node_output("context_analyzer", phase1_result)
            _append_runtime_log("Completed node=context_analyzer")
            phase1_outputs = {k: v for k, v in phase1_result.items() if k != "messages"}
            save_agent_state(project, patch_id, phase_name, phase1_outputs, agent_name)
            results["phases"] = {
                phase_name: {
                    agent_name: {
                        "node": agent_name,
                        "outputs": phase1_outputs,
                    }
                }
            }
            results["status"] = "completed"
            mode_results_file = os.path.join(
                RESULTS_DIR, project, patch_id, f"{run_mode}_results.json"
            )
            with open(mode_results_file, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, default=str)
            print(f"[{project}/{patch_id}] {run_mode} completed")
            _append_runtime_log("Mode phase1 completed")
            return results

        if run_mode == RUN_MODE_PHASE2:
            _append_runtime_log("Running mode=phase2")
            phase_name = "phase2_structural_locator"
            agent_name = "structural_locator"
            if not force_phase and is_phase_processed(
                project, patch_id, phase_name, agent_name
            ):
                print(
                    f"[{project}/{patch_id}] Skipping {run_mode}: phase output already exists"
                )
                results["status"] = "skipped"
                results["skip_reason"] = f"{phase_name}_{agent_name} already processed"
                return results

            phase1_state = load_agent_state(
                project,
                patch_id,
                "phase1_context_analyzer",
                "context_analyzer",
            )
            semantic_blueprint = (phase1_state or {}).get("semantic_blueprint")

            if not semantic_blueprint:
                pipeline_results_path = os.path.join(
                    _patch_results_dir(project, patch_id), "pipeline_results.json"
                )
                if os.path.exists(pipeline_results_path):
                    try:
                        with open(pipeline_results_path, "r", encoding="utf-8") as f:
                            pipeline_results = json.load(f)
                        semantic_blueprint = (
                            pipeline_results.get("phases", {})
                            .get("phase1_context_analyzer", {})
                            .get("context_analyzer", {})
                            .get("outputs", {})
                            .get("semantic_blueprint")
                        )
                    except Exception:
                        semantic_blueprint = None

            if not semantic_blueprint:
                results["status"] = "failed"
                results["error"] = (
                    "Missing semantic_blueprint for phase2-only run. "
                    "Run phase1 first or provide prior phase1 output."
                )
                return results

            phase2_inputs = dict(base_inputs)
            phase2_inputs["semantic_blueprint"] = semantic_blueprint
            print(f"[{project}/{patch_id}] Running {phase_name} only...")
            phase2_result = await structural_locator_node(phase2_inputs, config={})
            _collect_tokens_from_node_output("structural_locator", phase2_result)
            _append_runtime_log("Completed node=structural_locator")
            phase2_outputs = {k: v for k, v in phase2_result.items() if k != "messages"}
            save_agent_state(project, patch_id, phase_name, phase2_outputs, agent_name)
            results["phases"] = {
                phase_name: {
                    agent_name: {
                        "node": agent_name,
                        "outputs": phase2_outputs,
                    }
                }
            }
            results["status"] = "completed"
            mode_results_file = os.path.join(
                RESULTS_DIR, project, patch_id, f"{run_mode}_results.json"
            )
            with open(mode_results_file, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, default=str)
            print(f"[{project}/{patch_id}] {run_mode} completed")
            _append_runtime_log("Mode phase2 completed")
            return results

        inputs = dict(base_inputs)

        phase0_cache = _load_phase0_cache(project, backport_commit, mainline_commit)
        phase0_cache_transition = None
        if phase0_cache:
            reusable, reuse_reason = _is_phase0_cache_reusable(phase0_cache)
            if not reusable:
                print(
                    f"[{project}/{patch_id}] Ignoring stale Phase 0 cache "
                    f"(reason={reuse_reason}). Running fresh Phase 0."
                )
                save_pipeline_log(
                    project,
                    patch_id,
                    "phase0_cache_reuse",
                    "# Phase 0 Cache Reuse\n\n"
                    f"- Cache file: {_phase0_cache_file(project, backport_commit, mainline_commit)}\n"
                    "- Decision: ignored\n"
                    f"- Reason: {reuse_reason}\n",
                    run_id=run_id,
                )
                phase0_cache = None

        if phase0_cache:
            print(
                f"[{project}/{patch_id}] Found cached Phase 0 results. Skipping Phase 0 and reusing baseline test state."
            )
            inputs["skip_phase_0"] = True
            inputs["phase_0_test_targets"] = phase0_cache.get(
                "phase_0_test_targets", {}
            )
            inputs["phase_0_baseline_test_result"] = phase0_cache.get(
                "phase_0_baseline_test_result", {}
            )
            phase0_cache_transition = phase0_cache.get("phase_0_transition_evaluation")
            phase0_cached_success = bool(phase0_cache.get("fast_path_success", False))
            save_pipeline_log(
                project,
                patch_id,
                "phase0_cache_reuse",
                "# Phase 0 Cache Reuse\n\n"
                f"- Cache file: {_phase0_cache_file(project, backport_commit, mainline_commit)}\n"
                f"- Reused targets: {phase0_cache.get('phase_0_test_targets', {})}\n"
                f"- Reused baseline mode: {(phase0_cache.get('phase_0_baseline_test_result') or {}).get('mode', 'unknown')}\n",
                run_id=run_id,
            )

            if phase0_cached_success:
                _append_runtime_log(
                    "Phase0 cache fast-path success; skipping agentic workflow"
                )
                print(
                    f"[{project}/{patch_id}] Cached Phase 0 fast-path success. Skipping agentic workflow."
                )
                save_pipeline_log(
                    project,
                    patch_id,
                    "transition_summary",
                    _build_transition_summary_markdown(
                        phase0_cache_transition,
                        "phase0_cache",
                        phase_outputs={},
                        phase0_cache=phase0_cache,
                    ),
                    run_id=run_id,
                )

                results["phases"] = {
                    "phase0_cache": {
                        "phase_0_optimistic": {
                            "outputs": {
                                "fast_path_success": True,
                                "phase_0_test_targets": phase0_cache.get(
                                    "phase_0_test_targets", {}
                                ),
                                "phase_0_baseline_test_result": phase0_cache.get(
                                    "phase_0_baseline_test_result", {}
                                ),
                                "phase_0_post_patch_test_result": phase0_cache.get(
                                    "phase_0_post_patch_test_result", {}
                                ),
                                "phase_0_transition_evaluation": phase0_cache_transition,
                            }
                        }
                    }
                }
                results["status"] = "completed"
                results["cache_decision"] = {
                    "phase0_cache_used": True,
                    "phase0_fast_path_success": True,
                    "agentic_workflow_skipped": True,
                }
                results["exact_developer_patch"] = None
                results["developer_patch_comparison"] = {
                    "skipped": True,
                    "reason": "Skipped: cached Phase 0 fast-path success.",
                }

                results_file = os.path.join(
                    RESULTS_DIR, project, patch_id, "pipeline_results.json"
                )
                with open(results_file, "w", encoding="utf-8") as f:
                    json.dump(results, f, indent=2, default=str)

                print(
                    f"[{project}/{patch_id}] Completed from cache without agentic workflow."
                )
                _append_runtime_log("Completed from cache")
                return results

        print(f"[{project}/{patch_id}] Running full workflow graph...")
        _append_runtime_log("Running full workflow graph")
        phase_outputs = defaultdict(dict)
        current_phase = None
        latest_adapted_code_hunks = []

        async for output in app.astream(inputs):
            for node_name, node_output in output.items():
                print(f"  Completed: {node_name}")
                _append_runtime_log(f"Completed node={node_name}")
                if isinstance(node_output, dict):
                    _collect_tokens_from_node_output(node_name, node_output)

                if (
                    isinstance(node_output, dict)
                    and "adapted_code_hunks" in node_output
                ):
                    latest_adapted_code_hunks = (
                        node_output.get("adapted_code_hunks") or []
                    )

                if node_name in ["phase_0_optimistic"]:
                    current_phase = "phase0"
                elif node_name in ["context_analyzer"]:
                    current_phase = "phase1_context_analyzer"
                elif node_name in ["structural_locator"]:
                    current_phase = "phase2_structural_locator"
                elif node_name in ["planning_agent"]:
                    current_phase = "phase2_5_planning_agent"
                elif node_name in ["hunk_generator"]:
                    current_phase = "phase3_hunk_generator"
                elif node_name in ["validation"]:
                    current_phase = "phase4_validation"
                else:
                    current_phase = node_name

                phase_data = {
                    "node": node_name,
                    "outputs": {},
                }

                skip_keys = {"messages"}
                for key, value in node_output.items():
                    if key not in skip_keys:
                        phase_data["outputs"][key] = value

                if current_phase:
                    save_agent_state(
                        project,
                        patch_id,
                        current_phase,
                        phase_data["outputs"],
                        node_name,
                    )
                    phase_outputs[current_phase][node_name] = phase_data

        # Full comparison input: Java hunks generated by agents + developer-owned auxiliary hunks.
        final_adapted_code_hunks = latest_adapted_code_hunks + (
            developer_aux_hunks or []
        )

        comparison = compare_generated_with_developer_patch(
            adapted_code_hunks=final_adapted_code_hunks,
            developer_patch_diff=developer_patch_diff,
            backport_commit=backport_commit,
            target_repo_path=target_repo_path,
            compare_files_scope=pair_consistency.get("mainline_java_files", []),
        )
        results["pair_consistency"] = pair_consistency
        results["exact_developer_patch"] = comparison["exact_developer_patch"]
        results["developer_patch_comparison"] = {
            "comparison_method": comparison.get("comparison_method", "file_state"),
            "compared_files": comparison.get("compared_files", []),
            "mismatched_files": comparison.get("mismatched_files", []),
            "developer_files": comparison.get("developer_files", []),
            "generated_files": comparison.get("generated_files", []),
            "pair_consistency": pair_consistency,
            "error": comparison.get("error"),
        }

        agent_only_generated_patch_diff = _build_generated_patch_from_hunks(
            [
                h
                for h in (latest_adapted_code_hunks or [])
                if _is_java_code_file((h or {}).get("target_file", ""))
            ]
        )
        final_generated_patch_diff = comparison.get("generated_patch_diff", "")

        generated_patch_files = _save_generated_patch_artifacts(
            project=project,
            patch_id=patch_id,
            agent_only_patch_diff=agent_only_generated_patch_diff,
            final_effective_patch_diff=final_generated_patch_diff,
        )
        results["generated_patch_files"] = generated_patch_files

        agent_hunk_markdown = _build_hunk_comparison_markdown(
            developer_patch_diff=developer_patch_diff,
            generated_patch_diff=agent_only_generated_patch_diff,
            analyzer=analyzer,
            heading="Agent-Only Hunk Comparison (code files)",
        )

        final_hunk_markdown = ""
        if (agent_only_generated_patch_diff or "").strip() != (
            final_generated_patch_diff or ""
        ).strip():
            final_hunk_markdown = _build_hunk_comparison_markdown(
                developer_patch_diff=developer_patch_diff,
                generated_patch_diff=final_generated_patch_diff,
                analyzer=analyzer,
                heading="Final Effective Hunk Comparison (agent + developer aux, code files)",
            )

        save_pipeline_log(
            project,
            patch_id,
            "post_pipeline_developer_patch_compare",
            "# Post-Pipeline Developer Patch Comparison\n\n"
            f"**Exact Developer Patch (code-only)**: {comparison['exact_developer_patch']}\n\n"
            f"**Comparison Method**: {comparison.get('comparison_method', 'file_state')}\n\n"
            "## Commit Pair Consistency\n"
            f"- Pair mismatch: {pair_consistency.get('pair_mismatch', False)}\n"
            f"- Reason: {pair_consistency.get('reason', 'unknown')}\n"
            f"- Mainline Java files: {pair_consistency.get('mainline_java_files', [])}\n"
            f"- Developer Java files: {pair_consistency.get('developer_java_files', [])}\n"
            f"- Overlap Java files: {pair_consistency.get('overlap_java_files', [])}\n"
            f"- Overlap ratio (mainline): {pair_consistency.get('overlap_ratio_mainline', 0)}\n"
            f"- Compare files scope used: {pair_consistency.get('mainline_java_files', [])}\n\n"
            "## File State Comparison\n"
            f"- Compared files: {comparison.get('compared_files', [])}\n"
            f"- Mismatched files: {comparison.get('mismatched_files', [])}\n"
            f"- Error: {comparison.get('error')}\n\n"
            "## Comparison Scope\n"
            "- Agent-only patch: code hunks produced by Agent 3\n"
            "- Final effective patch: agent code hunks + developer auxiliary hunks (still code-only for this report)\n\n"
            + agent_hunk_markdown
            + (final_hunk_markdown if final_hunk_markdown else "")
            + "\n## Full Generated Patch (Agent-Only, code-only)\n"
            f"```diff\n{agent_only_generated_patch_diff}\n```\n"
            "\n## Full Generated Patch (Final Effective, code-only)\n"
            f"```diff\n{final_generated_patch_diff}\n```\n"
            "## Full Developer Backport Patch (full commit diff)\n"
            f"```diff\n{developer_patch_diff}\n```\n",
            run_id=run_id,
        )

        transition_eval = _extract_transition_eval_from_outputs(dict(phase_outputs))
        transition_source = "phase_outputs"
        if not transition_eval and phase0_cache_transition:
            transition_eval = phase0_cache_transition
            transition_source = "phase0_cache"

        # Output the full trace of file edits across retries
        trace_lines = ["# Full Trace of Agentic File Edits"]
        try:
            p3 = (
                phase_outputs.get("phase3_hunk_generator", {})
                .get("hunk_generator", {})
                .get("outputs", {})
            )
            history = p3.get("all_adapted_file_edits", [])
            trajectory = p3.get("agent_trajectory_edits", [])

            for attempt, edits in enumerate(history, start=1):
                trace_lines.append(f"\n## Attempt #{attempt}")

                # Check for agent tool calls in this attempt
                try:
                    if len(trajectory) >= attempt:
                        attempt_tool_calls = trajectory[attempt - 1]
                        if attempt_tool_calls:
                            trace_lines.append("\n### ReAct Agent Actions")
                            for tc in attempt_tool_calls:
                                trace_lines.append(
                                    f"- **{tc.get('target_file')}**: Called `{tc.get('tool')}`"
                                )
                                trace_lines.append(
                                    "```json\n"
                                    + json.dumps(tc.get("args", {}), indent=2)
                                    + "\n```"
                                )
                except Exception as eval_e:
                    pass

                trace_lines.append("\n### Final Output Diff")
                for edit in edits:
                    target = edit.get("target_file", "unknown")
                    edit_type = edit.get("edit_type", "unknown")
                    old_str = edit.get("old_string", "")
                    new_str = edit.get("new_string", "")
                    trace_lines.append(f"**{target}** [{edit_type}]")
                    if old_str == "<line-based ReAct agent diff>":
                        trace_lines.append(f"```diff\n{new_str}\n```")
                    else:
                        trace_lines.append(
                            f"```java\n// --- OLD ---\n{old_str}\n// --- NEW ---\n{new_str}\n```"
                        )
            if not history:
                trace_lines.append("\nNo edit history found in phase 3 outputs.")
        except Exception as e:
            trace_lines.append(f"\nError generating trace: {e}")

        save_pipeline_log(
            project,
            patch_id,
            "full_trace",
            "\n".join(trace_lines),
            run_id=run_id,
        )

        save_pipeline_log(
            project,
            patch_id,
            "transition_summary",
            _build_transition_summary_markdown(
                transition_eval,
                transition_source,
                phase_outputs=dict(phase_outputs),
                phase0_cache=phase0_cache,
            ),
            run_id=run_id,
        )

        recovery_intelligence = _build_recovery_intelligence_report(dict(phase_outputs))
        results["recovery_intelligence"] = recovery_intelligence
        save_pipeline_log(
            project,
            patch_id,
            "recovery_intelligence",
            "# Recovery Intelligence\n\n"
            + json.dumps(recovery_intelligence, indent=2, default=str),
            run_id=run_id,
        )

        results["phases"] = dict(phase_outputs)
        results["status"] = "completed"

        results_file = os.path.join(
            RESULTS_DIR, project, patch_id, "pipeline_results.json"
        )
        with open(results_file, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, default=str)

        print(f"[{project}/{patch_id}] Full workflow completed!")
        _append_runtime_log("Workflow completed successfully")
        return results

    except Exception as e:
        _append_runtime_log(f"Workflow failed: {e}")
        results["error"] = str(e)
        results["status"] = "failed"
        print(f"[{project}/{patch_id}] Workflow failed: {e}")
        return results
    finally:
        try:
            sys.stdout = _orig_stdout
            sys.stderr = _orig_stderr
            _log_stream.close()
        except Exception:
            pass
        token_totals["finished_at"] = datetime.now().isoformat()
        with open(runtime_tokens_path, "w", encoding="utf-8") as tf:
            json.dump(token_totals, tf, indent=2)


def is_patch_processed(
    project: str,
    patch_id: str,
    phase_name: str | None = None,
    agent_name: str | None = None,
) -> bool:
    if phase_name and agent_name:
        return is_phase_processed(project, patch_id, phase_name, agent_name)

    patch_results_dir = _patch_results_dir(project, patch_id)
    results_file = os.path.join(patch_results_dir, "pipeline_results.json")
    if not os.path.exists(results_file):
        return False

    # Treat legacy summaries with no run_mode as full runs.
    try:
        with open(results_file, "r", encoding="utf-8") as f:
            payload = json.load(f)
        run_mode = str(payload.get("run_mode", RUN_MODE_FULL)).strip().lower()
        status = str(payload.get("status", "")).strip().lower()
        return run_mode == RUN_MODE_FULL and status in {"", "completed"}
    except Exception:
        return True


async def main():
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Evaluate full/partial retrofit workflow"
    )
    parser.add_argument(
        "--mode",
        choices=[RUN_MODE_FULL, RUN_MODE_PHASE1, RUN_MODE_PHASE2],
        default=RUN_MODE_FULL,
        help="Execution mode: full pipeline, phase1 only, or phase2 only.",
    )
    parser.add_argument(
        "--project",
        choices=TARGET_PROJECTS,
        default=None,
        help="Optional project filter.",
    )
    parser.add_argument(
        "--patch-id",
        default=None,
        help="Optional patch id filter (e.g. elasticsearch_734dd070).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force rerun for the selected mode even if outputs already exist.",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Skip git clean/reset before running each patch.",
    )
    parser.add_argument(
        "--phase2-reset",
        action="store_true",
        help="When mode=phase2, delete existing phase2 outputs before rerun.",
    )
    args = parser.parse_args()

    print("=" * 80)
    print(
        f"WORKFLOW EVALUATION MODE={args.mode.upper()} "
        f"(projects: {', '.join(TARGET_PROJECTS)})"
    )
    print("=" * 80)

    ensure_dirs()

    if not os.path.exists(DATASET_PATH):
        print(f"ERROR: Dataset not found at {DATASET_PATH}")
        return

    selected_projects = [args.project] if args.project else TARGET_PROJECTS

    data = []
    with open(DATASET_PATH, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["Project"] in selected_projects:
                data.append(row)

    print(f"\nFound {len(data)} total patches for target projects: {selected_projects}")

    if args.patch_id:
        data = [
            row
            for row in data
            if f"{row['Project']}_{row['Original Commit'][:8]}" == args.patch_id
        ]
        if not data:
            print(f"No dataset row matched --patch-id={args.patch_id}")
            return

    if MAX_PATCHES_PER_PROJECT and not args.patch_id:
        data_limited = defaultdict(list)
        for row in data:
            data_limited[row["Project"]].append(row)

        data = []
        for project in selected_projects:
            data.extend(data_limited[project][:MAX_PATCHES_PER_PROJECT])

    print(f"Processing {len(data)} patches")

    all_results = []
    skipped_patches = []
    for idx, row in enumerate(data, 1):
        project = row["Project"]
        mainline_commit = row["Original Commit"]
        backport_commit = row["Backport Commit"]
        patch_id = f"{project}_{mainline_commit[:8]}"

        if args.mode == RUN_MODE_FULL:
            already_processed = is_patch_processed(project, patch_id)
        elif args.mode == RUN_MODE_PHASE1:
            already_processed = is_patch_processed(
                project,
                patch_id,
                "phase1_context_analyzer",
                "context_analyzer",
            )
        else:
            already_processed = is_patch_processed(
                project,
                patch_id,
                "phase2_structural_locator",
                "structural_locator",
            )

        if already_processed and not args.force:
            print(
                f"\n[{idx}/{len(data)}] SKIPPING ({args.mode} already processed): {patch_id}"
            )
            skipped_patches.append(patch_id)
            continue

        print(f"\n[{idx}/{len(data)}] Processing: {patch_id}")

        mainline_repo_path = os.path.join(REPOS_DIR, project)
        target_repo_path = os.path.join(REPOS_DIR, project)

        if args.phase2_reset and args.mode == RUN_MODE_PHASE2:
            phase2_file = _phase_output_file(
                project,
                patch_id,
                "phase2_structural_locator",
                "structural_locator",
            )
            if os.path.exists(phase2_file):
                os.remove(phase2_file)
                print(
                    f"[{project}/{patch_id}] Removed prior phase2 output: {phase2_file}"
                )

        if not args.no_clean:
            print(f"[{project}/{patch_id}] Cleaning repositories...")
            for repo_path in [mainline_repo_path, target_repo_path]:
                if os.path.exists(repo_path):
                    subprocess.run(
                        ["git", "reset", "--hard", "HEAD"],
                        cwd=repo_path,
                        capture_output=True,
                    )
                    subprocess.run(
                        ["git", "clean", "-fd"], cwd=repo_path, capture_output=True
                    )

        if not os.path.exists(mainline_repo_path):
            print(f"  ERROR: Repository not found at {mainline_repo_path}")
            continue

        result = await run_full_pipeline(
            project,
            patch_id,
            mainline_commit,
            backport_commit,
            mainline_repo_path,
            target_repo_path,
            run_mode=args.mode,
            force_phase=args.force,
        )
        all_results.append(result)

        temp_patch_path = os.path.join(RESULTS_DIR, "temp_mainline.patch")
        if os.path.exists(temp_patch_path):
            try:
                os.remove(temp_patch_path)
            except Exception as e:
                print(f"  Warning: Could not remove temp patch file: {e}")

    summary_file = os.path.join(RESULTS_DIR, f"pipeline_summary_{args.mode}.json")
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, default=str)

    print("\n" + "=" * 80)
    print("EVALUATION SUMMARY")
    print("=" * 80)
    completed = sum(1 for r in all_results if r.get("status") == "completed")
    failed = sum(1 for r in all_results if r.get("status") == "failed")
    mode_skipped = sum(1 for r in all_results if r.get("status") == "skipped")
    print(f"Total Patches in Dataset: {len(data)}")
    print(f"Skipped (already processed): {len(skipped_patches)}")
    print(f"Newly Processed: {len(all_results)}")
    print(f"Completed: {completed}")
    print(f"Failed: {failed}")
    if mode_skipped:
        print(f"Mode-skipped during execution: {mode_skipped}")
    if skipped_patches:
        print(f"\nSkipped Patches: {', '.join(skipped_patches)}")
    print(f"Results Directory: {RESULTS_DIR}")
    print(f"Summary Report: {summary_file}")


if __name__ == "__main__":
    asyncio.run(main())
