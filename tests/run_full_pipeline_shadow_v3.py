#!/usr/bin/env python3
"""
Full pipeline shadow run (Agents 1-7, including Validation) — v3

Extends v2 by running Agent 7 (Validation Loop) after synthesis:
  - Applies synthesized_hunks via CLAW + developer_aux_hunks via git-apply
  - Runs project build (helpers/{project}/run_build.sh or Maven/Gradle)
  - Runs targeted tests (helpers/{project}/run_tests.sh or Maven/Gradle)
  - Records build/test results in results.json

Usage:
  # Run all patches from patches.yaml
  python run_full_pipeline_shadow_v3.py

  # Filter by repo or type
  python run_full_pipeline_shadow_v3.py --repo elasticsearch --type TYPE-I

  # Run a single patch by commit
  python run_full_pipeline_shadow_v3.py --project elasticsearch --commit abc123def456

  # Limit patch count
  python run_full_pipeline_shadow_v3.py --count 3

  # Skip validation (run agents 1-6 only, same as v2)
  python run_full_pipeline_shadow_v3.py --skip-validation

  # Force re-run even if results folder already exists
  python run_full_pipeline_shadow_v3.py --force

For each patch, creates a folder under tests/shadow_run_results_v3/<project>/<TYPE>_<sha>/ with:
  mainline.patch    — original commit diff (what we're backporting FROM)
  target.patch      — actual backport commit (ground truth)
  generated.patch   — diff produced after agents 1-7 ran
  results.json      — per-agent timing, hunk counts, build/test results
"""

import csv
import os
import sys
import json
import yaml
import argparse
import subprocess
import time
import traceback
from pathlib import Path
from datetime import datetime
from typing import Any

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.state import BackportState
from src.tools.patch_parser import parse_unified_diff
from src.tools.build_systems import (
    detect_test_targets,
    run_build,
    run_tests,
    restore_repo_state,
)
from src.agents.agent1_localizer import localize_hunks
from src.agents.agent2_classifier import classify_patch
from src.agents.agent3_fastapply import fast_apply_agent
from src.agents.agent4_namespace import namespace_adapter_agent
from src.agents.agent5_structural import structural_refactor_agent
from src.agents.agent6_synthesizer import hunk_synthesizer_agent
from src.agents.agent7_validator import run_validation, run_phase0_baseline, _apply_synthesized_hunks
from src.agents.agent8_syntax_repair import syntax_repair_agent
from src.core.graph import build_validator_fallback_graph
from src.tools.notification_service import TelegramNotifier

# ── Config ────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = Path(__file__).parent / "patches.yaml"
DEFAULT_DATASET = Path(__file__).parent.parent / "dataset" / "all_projects_final.csv"
OUTPUT_DIR = Path(__file__).parent / "shadow_run_results_v3"

# Maps project names in the CSV to repo paths relative to the project root.
# Add entries here as new repos are cloned under repos/.
REPO_PATH_MAP: dict[str, str] = {
    "elasticsearch": "repos/elasticsearch",
    "crate": "repos/crate",
    "druid": "repos/druid",
    "hibernate-orm": "repos/hibernate-orm",
    "logstash": "repos/logstash",
    "sql": "repos/sql",
    "spring-framework": "repos/spring-framework",
    "hadoop": "repos/hadoop",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def get_patches_from_config(
    config: dict[str, Any],
    repo_filter: str | None = None,
    type_filter: str | None = None,
    count_limit: int | None = None,
    project_filter: str | None = None,
    commit_filter: str | None = None,
) -> list[dict[str, Any]]:
    patches = []
    for repo_name, repo_config in config.get("repos", {}).items():
        if repo_filter and repo_name != repo_filter:
            continue
        if project_filter and repo_name != project_filter:
            continue
        repo_path = repo_config.get("path")
        for patch in repo_config.get("patches", []):
            if type_filter and patch.get("type") != type_filter:
                continue
            original_commit = patch.get("original_commit", "")
            if commit_filter and not original_commit.startswith(commit_filter):
                continue
            patches.append({
                "repo": repo_name,
                "repo_path": repo_path,
                "type": patch.get("type", "UNKNOWN"),
                "original_commit": original_commit,
                "backport_commit": patch.get("backport_commit"),
                "description": patch.get("description", ""),
            })
            if count_limit and len(patches) >= count_limit:
                return patches
    return patches


def get_patches_from_csv(
    csv_path: Path,
    repo_filter: str | None = None,
    type_filter: str | None = None,
    count_limit: int | None = None,
    project_filter: str | None = None,
    commit_filter: str | None = None,
) -> list[dict[str, Any]]:
    """
    Load patches from dataset/all_projects_final.csv.

    CSV columns:
      Project, Original Version, Original Commit,
      Backport Version, Backport Commit, Backport Date, Type

    Skips projects that have no entry in REPO_PATH_MAP or whose repo
    directory does not exist on disk.
    """
    project_root = Path(__file__).parent.parent
    patches: list[dict[str, Any]] = []

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            project = (row.get("Project") or "").strip().lower()
            original_commit = (row.get("Original Commit") or "").strip()
            backport_commit = (row.get("Backport Commit") or "").strip()
            patch_type = (row.get("Type") or "").strip()

            if not project or not original_commit or not backport_commit:
                continue
            if repo_filter and project != repo_filter:
                continue
            if project_filter and project != project_filter:
                continue
            if type_filter and patch_type != type_filter:
                continue
            if commit_filter and not original_commit.startswith(commit_filter):
                continue

            rel_path = REPO_PATH_MAP.get(project)
            if rel_path is None:
                continue  # project not configured
            repo_path = str(project_root / rel_path)
            if not os.path.isdir(repo_path):
                continue  # repo not cloned

            patches.append({
                "repo": project,
                "repo_path": repo_path,
                "type": patch_type,
                "original_commit": original_commit,
                "backport_commit": backport_commit,
                "description": f"{row.get('Original Version','')} → {row.get('Backport Version','')}",
            })
            if count_limit and len(patches) >= count_limit:
                break

    return patches


def git_show(repo_path: str, ref: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", repo_path, "show", ref],
        capture_output=True, text=True
    )
    return result.stdout if result.returncode == 0 else None


def git_checkout(repo_path: str, ref: str) -> None:
    subprocess.run(
        ["git", "-C", repo_path, "checkout", "-f", ref],
        capture_output=True, text=True, check=False
    )


def git_diff(repo_path: str) -> str:
    # Stage all changes (including new files created by agent6) so `git diff --cached`
    # can show them. Without staging, newly created files are invisible to git diff.
    subprocess.run(
        ["git", "-C", repo_path, "add", "--", "."],
        capture_output=True, text=True
    )
    # Unstage build/test artefacts that Docker wrote into the repo during Phase-0
    # baseline / validation runs (e.g. build/all-test-results/TEST-*.xml).
    # These are NOT part of the backport patch and must not appear in generated.patch.
    for artifact_dir in ("build/", "target/"):
        subprocess.run(
            ["git", "-C", repo_path, "reset", "HEAD", "--", artifact_dir],
            capture_output=True, text=True
        )
    # git diff --cached shows the staged changes relative to HEAD.
    # This includes new files (with "new file mode"), modifications, and deletions.
    result = subprocess.run(
        ["git", "-C", repo_path, "diff", "--cached"],
        capture_output=True, text=True
    )
    return result.stdout


def _extract_production_diff_hunks(patch_text: str) -> str:
    """
    Split patch into per-file sections, drop test/aux/non-Java files (same
    criteria as _build_aux_hunks_from_target_patch), then normalise the
    remaining production-Java sections by removing commit headers and SHA-
    bearing index lines.  Returns a canonical string for exact comparison.
    """
    from src.agents.agent1_localizer import _is_test_file, _is_auto_generated_java_file

    def _norm_path(raw: str) -> str:
        p = raw.strip().replace("\\", "/")
        while p.startswith("a/") or p.startswith("b/"):
            p = p[2:]
        return "" if p in ("/dev/null", "dev/null") else p

    # Split on "diff --git" boundaries
    sections: list[str] = []
    buf: list[str] = []
    for line in (patch_text or "").splitlines(keepends=True):
        if line.startswith("diff --git ") and buf:
            sections.append("".join(buf))
            buf = []
        buf.append(line)
    if buf:
        sections.append("".join(buf))

    kept: list[str] = []
    for section in sections:
        tgt_path = ""
        for line in section.splitlines():
            if line.startswith("+++ "):
                tgt_path = _norm_path(line[4:].split("\t")[0])
                break
        if not tgt_path:
            continue
        # Skip test, non-Java, and auto-generated files — same as aux hunk logic
        if (not tgt_path.lower().endswith(".java")
                or _is_test_file(tgt_path)
                or _is_auto_generated_java_file(tgt_path)):
            continue

        # Normalise: drop index/mode lines that carry commit-specific SHAs
        norm_lines = []
        for line in section.splitlines():
            if line.startswith("index ") or line.startswith("old mode") or line.startswith("new mode"):
                continue
            norm_lines.append(line)
        kept.append("\n".join(norm_lines).strip())

    return "\n".join(kept).strip()


def patches_are_equivalent(generated: str, target: str) -> bool:
    """
    Return True when the generated patch produces the same production-Java code
    changes as the developer's target patch, ignoring test/aux file sections
    and commit metadata.
    """
    gen_hunks = _extract_production_diff_hunks(generated)
    tgt_hunks = _extract_production_diff_hunks(target)
    return bool(gen_hunks) and gen_hunks == tgt_hunks


KNOWN_TYPES = ["TYPE-I", "TYPE-II", "TYPE-III", "TYPE-IV", "TYPE-V"]

# Phase 0 baseline cache — keyed by (project, backport_commit).
# Stores the full test_state (per-case pass/fail) so we never re-run the
# expensive baseline Docker build+test for the same commit.
PHASE0_CACHE_DIR = Path(__file__).parent / "phase0_cache"


def _phase0_cache_path(project: str, backport_commit: str) -> Path:
    safe_project = (project or "unknown").strip().lower()
    key = f"{safe_project}_{backport_commit[:16]}.json"
    return PHASE0_CACHE_DIR / key


def load_phase0_cache(project: str, backport_commit: str) -> dict | None:
    path = _phase0_cache_path(project, backport_commit)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  phase0 cache: read error ({e}), ignoring cache")
        return None


def save_phase0_cache(project: str, backport_commit: str, baseline: dict) -> None:
    PHASE0_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _phase0_cache_path(project, backport_commit)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(baseline, f, indent=2, default=str)
        print(f"  phase0 cache: saved → {path.name}")
    except Exception as e:
        print(f"  phase0 cache: write error ({e})")


# ── Aux-hunk extraction from target patch ─────────────────────────────────────

def _extract_file_entries_from_patch(patch_text: str) -> list[tuple[str, str]]:
    """
    Extract (status, filepath) pairs from a unified diff patch.
    Status is one of: "M" (modified), "A" (added), "R" (renamed), "D" (deleted).
    Used to pass exact file-change information to per-project get_test_targets.py
    helpers so both phase 0 and validation detect identical test targets.
    """
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()

    src_path = ""
    tgt_path = ""
    is_added = False
    is_deleted = False
    is_rename = False

    def _norm(p: str) -> str:
        p = p.strip().replace("\\", "/")
        while p.startswith("a/") or p.startswith("b/"):
            p = p[2:]
        return "" if p in ("/dev/null", "dev/null") else p

    def _flush():
        nonlocal src_path, tgt_path, is_added, is_deleted, is_rename
        path = tgt_path or src_path
        if path and path not in seen:
            seen.add(path)
            if is_added:
                status = "A"
            elif is_deleted:
                status = "D"
            elif is_rename:
                status = "R"
            else:
                status = "M"
            entries.append((status, path))
        src_path = tgt_path = ""
        is_added = is_deleted = is_rename = False

    for line in (patch_text or "").splitlines():
        if line.startswith("diff --git "):
            _flush()
        elif line.startswith("new file mode"):
            is_added = True
        elif line.startswith("deleted file mode"):
            is_deleted = True
        elif line.startswith("rename from ") or line.startswith("rename to "):
            is_rename = True
        elif line.startswith("--- "):
            src_path = _norm(line[4:].split("\t")[0])
        elif line.startswith("+++ "):
            tgt_path = _norm(line[4:].split("\t")[0])

    _flush()
    return entries


def _extract_all_files_from_patch(patch_text: str) -> list[str]:
    """Backwards-compat wrapper — returns just the file paths."""
    return [path for _, path in _extract_file_entries_from_patch(patch_text)]


def _build_aux_hunks_from_target_patch(target_patch: str) -> list[dict]:
    """
    Parse the actual developer backport (target_patch) and extract all aux file
    patches (test Java, non-Java, auto-generated Java) with their REAL line-number
    offsets preserved.

    parse_unified_diff strips line numbers (it only stores old/new content for
    localization). We must parse the raw diff text directly so that git apply
    receives the correct @@ -N,M +N,M @@ headers and can locate context lines.

    Returns one entry per FILE (not per hunk). Each entry has:
      file_path, raw_patch (complete diff --git ... section), file_operation,
      old_target_file (for renames), intent_verified=True.
    Agent 7's _apply_developer_aux_hunks uses raw_patch when present.
    """
    from src.agents.agent1_localizer import (
        _is_test_file, _is_auto_generated_java_file
    )

    if not target_patch.strip():
        return []

    def _norm(p: str) -> str:
        p = p.strip().replace("\\", "/")
        while p.startswith("a/") or p.startswith("b/"):
            p = p[2:]
        return "" if p == "/dev/null" or p == "dev/null" else p

    result: list[dict] = []

    # Split into per-file sections on "diff --git" boundaries
    sections: list[str] = []
    buf: list[str] = []
    for line in target_patch.splitlines(keepends=True):
        if line.startswith("diff --git ") and buf:
            sections.append("".join(buf))
            buf = []
        buf.append(line)
    if buf:
        sections.append("".join(buf))

    for section in sections:
        if not section.strip():
            continue

        # Extract source/target file paths
        src_path = ""
        tgt_path = ""
        is_added = False
        is_deleted = False
        is_rename = False

        for line in section.splitlines():
            if line.startswith("--- "):
                src_path = _norm(line[4:].split("\t")[0])
            elif line.startswith("+++ "):
                tgt_path = _norm(line[4:].split("\t")[0])
            elif line.startswith("new file mode"):
                is_added = True
            elif line.startswith("deleted file mode"):
                is_deleted = True
            elif line.startswith("rename from ") or line.startswith("rename to "):
                is_rename = True
            if src_path and tgt_path:
                break

        file_path = tgt_path or src_path
        if not file_path:
            continue

        is_test = _is_test_file(file_path)
        is_non_java = not file_path.lower().endswith(".java")
        is_autogen = _is_auto_generated_java_file(file_path)
        if not (is_test or is_non_java or is_autogen):
            continue  # Java production code — handled by LLM pipeline

        if is_added:
            op = "ADDED"
        elif is_deleted:
            op = "DELETED"
        elif is_rename or (src_path and tgt_path and src_path != tgt_path):
            op = "RENAMED"
        else:
            op = "MODIFIED"

        result.append({
            "file_path": file_path,
            "raw_patch": section,        # complete section, correct line numbers
            "hunk_text": "",             # not used when raw_patch is present
            "file_operation": op,
            "insertion_line": 0,
            "intent_verified": True,
            "old_target_file": src_path if op == "RENAMED" else None,
        })

    return result


def update_summary_md(output_dir: Path, no_notifications: bool = False) -> None:
    """
    Scan all results.json files under output_dir and regenerate SUMMARY.md.

    Success criterion: fail_to_pass_count > 0 OR newly_passing_count > 0.

    Matrix layout:
      rows    = patch types  (TYPE-I … TYPE-V)
      columns = projects     (elasticsearch, crate, …)
      cells   = "pass/total" fraction, or "-" when no data
    """
    # ── Collect all results ───────────────────────────────────────────────────
    # {(project, patch_type): [True/False, ...]}  — True = backport success
    data: dict[tuple[str, str], list[bool]] = {}
    projects_seen: set[str] = set()
    types_seen: set[str] = set()

    for result_file in sorted(output_dir.glob("*/*/results.json")):
        try:
            with open(result_file, "r", encoding="utf-8") as f:
                r = json.load(f)
        except Exception:
            continue

        project = r.get("repo", "")
        patch_type = r.get("patch_type", "")
        skip = r.get("skip_validation", False)

        if not project or not patch_type or skip:
            continue

        success = r.get("summary", {}).get("backport_success")
        if success is None:
            continue  # validation was skipped for this entry

        projects_seen.add(project)
        types_seen.add(patch_type)
        data.setdefault((project, patch_type), []).append(bool(success))

    if not data:
        return

    # ── Order rows and columns ─────────────────────────────────────────────
    # Rows: known types first (in order), then any unexpected types sorted
    all_types = [t for t in KNOWN_TYPES if t in types_seen] + sorted(
        t for t in types_seen if t not in KNOWN_TYPES
    )
    all_projects = sorted(projects_seen)

    # ── Build markdown table ───────────────────────────────────────────────
    total_patches = sum(len(v) for v in data.values())
    total_passed = sum(sum(v) for v in data.values())

    col_width = max(len(p) for p in all_projects) if all_projects else 12
    col_width = max(col_width, 5)
    row_label_width = max(len(t) for t in all_types) if all_types else 8
    row_label_width = max(row_label_width, 8)

    def cell(project: str, patch_type: str) -> str:
        runs = data.get((project, patch_type))
        if not runs:
            return "-"
        passed = sum(runs)
        return f"{passed}/{len(runs)}"

    # Header
    header_cols = " | ".join(f"{p:^{col_width}}" for p in all_projects)
    sep_cols = " | ".join("-" * col_width for _ in all_projects)
    lines = [
        "# Backport CLAW — Shadow Run Results (v3)",
        "",
        f"**Last updated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"**Total:** {total_patches} patch(es) run, "
        f"**{total_passed} passed** "
        f"({100 * total_passed // total_patches if total_patches else 0}%)",
        "",
        "> **Success** = at least one fail→pass or newly-passing test observed.",
        "",
        f"| {'Type':{row_label_width}} | {header_cols} |",
        f"| {'-' * row_label_width} | {sep_cols} |",
    ]

    for patch_type in all_types:
        cells = " | ".join(f"{cell(p, patch_type):^{col_width}}" for p in all_projects)
        lines.append(f"| {patch_type:{row_label_width}} | {cells} |")

    lines += [
        "",
        "---",
        "",
        "## Per-patch detail",
        "",
        "| Project | Type | Commit | Success | Build | Tests (f→p / newly / p→f) | Details | Category |",
        "| ------- | ---- | ------ | :-----: | :---: | :------------------------: | ------- | -------- |",
    ]

    inconclusive: list[dict[str, str]] = []

    for result_file in sorted(output_dir.glob("*/*/results.json")):
        try:
            with open(result_file, "r", encoding="utf-8") as f:
                r = json.load(f)
        except Exception:
            continue
        if r.get("skip_validation", False):
            continue
        s = r.get("summary", {})
        project = r.get("repo", "?")
        patch_type = r.get("patch_type", "?")
        commit = r.get("original_commit", "?")[:10]
        success = s.get("backport_success")
        build_ok = s.get("build_success")
        test_ok = s.get("test_success")
        f2p = s.get("fail_to_pass_count", 0)
        newly = s.get("newly_passing_count", 0)
        p2f = s.get("pass_to_fail_count", 0)
        cat = s.get("validation_failure_category", "")
        exact_match = s.get("patch_exact_match", False)

        transition = s.get("test_transition", {})
        details = []
        if exact_match:
            details.append("patch=exact")
        if f2p > 0:
            details.append(f"F→P: {', '.join(transition.get('fail_to_pass', []))}")
        if newly > 0:
            details.append(f"Newly: {', '.join(transition.get('newly_passing', []))}")
        if p2f > 0:
            details.append(f"P→F: {', '.join(transition.get('pass_to_fail', []))}")
        details_str = "<br>".join(details) if details else "-"

        success_icon = "✓" if success else ("✗" if success is False else "-")
        build_icon = "✓" if build_ok else ("✗" if build_ok is False else "-")
        test_detail = f"{f2p} / {newly} / {p2f}" if (test_ok is not None) else "-"

        lines.append(
            f"| {project} | {patch_type} | `{commit}` | {success_icon} | "
            f"{build_icon} | {test_detail} | {details_str} | {cat} |"
        )

        # Collect patches where test reports were never found (inconclusive signal).
        reason = transition.get("reason", "")
        if not transition.get("valid_backport_signal") and "Inconclusive" in reason:
            inconclusive.append({
                "project": project,
                "type": patch_type,
                "original_commit": r.get("original_commit", "?"),
                "backport_commit": r.get("backport_commit", "?"),
                "reason": reason,
                "result_dir": str(result_file.parent.relative_to(output_dir)),
            })

    if inconclusive:
        lines += [
            "",
            "---",
            "",
            "## Inconclusive — no test reports found (retry candidates)",
            "",
            "> These patches ran but no XML reports were produced for the target test classes.",
            "> Re-run after fixing test-task selection or infrastructure issues.",
            "",
            "| Project | Type | Original Commit | Backport Commit | Reason | Result dir |",
            "| ------- | ---- | --------------- | --------------- | ------ | ---------- |",
        ]
        for inc in inconclusive:
            orig = inc["original_commit"][:10]
            bkpt = inc["backport_commit"][:10]
            reason = inc["reason"].replace("|", "\\|")
            lines.append(
                f"| {inc['project']} | {inc['type']} | `{orig}` | `{bkpt}` "
                f"| {reason} | `{inc['result_dir']}` |"
            )

    summary_path = output_dir / "SUMMARY.md"
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  SUMMARY.md updated → {summary_path}")

    # Send update to Telegram
    if not no_notifications:
        try:
            notifier = TelegramNotifier()
            if notifier.bot_token and notifier.chat_id:
                print(f"  [pipeline] Sending updated SUMMARY.md to Telegram...")
                notifier.send_markdown_file(summary_path)
        except Exception as e:
            print(f"  [pipeline] Failed to send Telegram notification: {e}")


def make_serializable(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return make_serializable(obj.model_dump())
    if isinstance(obj, dict):
        return {k: make_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [make_serializable(v) for v in obj]
    return obj


def run_agent(
    label: str, fn, state: BackportState, agent_records: dict
) -> BackportState:
    print(f"    [pipeline] Starting Agent: {label}...", end=" ", flush=True)
    start = time.time()
    error_msg = None
    try:
        state = fn(state)
        print(f"DONE ({time.time() - start:.2f}s)")
    except Exception:
        error_msg = traceback.format_exc()
        print(f"FAILED ({time.time() - start:.2f}s)")
        print(f"      [pipeline] Agent {label} error: {error_msg.splitlines()[-1]}")
    agent_records[label] = {
        "elapsed_s": round(time.time() - start, 2),
        "error": error_msg,
    }
    return state


# ── Main processing ───────────────────────────────────────────────────────────

def _patch_previously_failed(out_dir: Path) -> bool:
    """Return True if the patch's last run exists but did NOT succeed."""
    results_file = out_dir / "results.json"
    if not results_file.exists():
        return False
    try:
        with open(results_file, "r", encoding="utf-8") as f:
            r = json.load(f)
        success = r.get("summary", {}).get("backport_success")
        # None means validation was skipped — treat as not-failed
        return success is False
    except Exception:
        return False


def process_patch(
    item: dict[str, Any],
    run_ts: str,
    skip_validation: bool = False,
    force: bool = False,
    skip_test: bool = False,
    failed_only: bool = False,
    run_phase0: bool = False,
) -> None:
    patch_type = item["type"]
    original_commit = item["original_commit"]
    backport_commit = item["backport_commit"]
    repo_path = item["repo_path"]
    repo_name = item["repo"]
    description = item.get("description", "")

    out_dir = OUTPUT_DIR / repo_name / f"{patch_type}_{original_commit[:8]}"

    # Skip if results.json already exists in out_dir, unless --force is used
    results_file = out_dir / "results.json"
    if results_file.exists() and not force:
        print(f"\n  [pipeline] Skipping {patch_type} | repo={repo_name} | commit={original_commit[:12]} (already processed)")
        return

    # --failed: only re-run patches whose last result was a failure
    if failed_only and results_file.exists() and not _patch_previously_failed(out_dir):
        print(f"\n  [pipeline] Skipping {patch_type} | repo={repo_name} | commit={original_commit[:12]} (not a failed patch)")
        return

    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*64}")
    print(f"  [pipeline] Processing {patch_type} | repo={repo_name} | commit={original_commit[:12]}")
    if description:
        print(f"  [pipeline] Description: {description}")
    print(f"  [pipeline] Backport Target: {backport_commit[:12]}")
    suffix = "  [VALIDATION SKIPPED]" if skip_validation else ""
    print(f"{'='*64}{suffix}")

    results: dict[str, Any] = {
        "run_timestamp": run_ts,
        "repo": repo_name,
        "patch_type": patch_type,
        "original_commit": original_commit,
        "backport_commit": backport_commit,
        "description": description,
        "skip_validation": skip_validation,
        "skip_test": skip_test,
        "agents": {},
        "summary": {},
    }

    # ── 1. Capture reference patches ─────────────────────────────────────────

    print(f"  [pipeline] Capturing reference patches for {original_commit}")
    mainline_patch = git_show(repo_path, original_commit)
    if not mainline_patch:
        print(f"  [pipeline] ERROR: Cannot retrieve commit {original_commit}")
        return
    (out_dir / "mainline.patch").write_text(mainline_patch, encoding="utf-8")
    print(f"  [pipeline] mainline.patch saved to {out_dir}")

    target_patch = git_show(repo_path, backport_commit)
    if target_patch:
        (out_dir / "target.patch").write_text(target_patch, encoding="utf-8")
        print(f"  [pipeline] target.patch saved to {out_dir}")
    else:
        target_patch = ""
        print(f"  [pipeline] WARNING: Could not retrieve backport commit {backport_commit}")

    # ── 2. Checkout repo to the state just before the backport ───────────────

    print(f"  [pipeline] Preparing worktree: checking out {backport_commit}~1")
    git_checkout(repo_path, f"{backport_commit}~1")

    # ── 3. Parse hunks ───────────────────────────────────────────────────────
    # mainline_patch → ALL hunks (Agent 1 will filter to Java-code-only)
    # target_patch   → aux hunks only (test/non-Java/auto-gen from developer backport)
    #                  These apply verbatim against the target branch; they must NOT
    #                  come from mainline_patch because context lines differ.

    all_hunks = parse_unified_diff(mainline_patch)
    print(f"  Parsed {len(all_hunks)} total hunk(s) from mainline patch")
    results["summary"]["total_hunks_in_patch"] = len(all_hunks)

    developer_aux_hunks = _build_aux_hunks_from_target_patch(target_patch)
    print(f"  Extracted {len(developer_aux_hunks)} aux hunk(s) from target.patch")
    results["summary"]["developer_aux_hunks_from_target"] = len(developer_aux_hunks)

    # Extract ALL changed files from target.patch — used for consistent test target detection
    # in both phase 0 and validation so both steps run the exact same set of tests.
    target_patch_file_entries = _extract_file_entries_from_patch(target_patch)
    target_patch_files = [path for _, path in target_patch_file_entries]
    print(f"  Extracted {len(target_patch_file_entries)} changed file(s) from target.patch for test target detection")

    # ── 4. Initialise state ───────────────────────────────────────────────────

    state: BackportState = {
        "patch_content": mainline_patch,
        "target_repo_path": repo_path,
        "target_branch": f"{backport_commit}~1",
        "worktree_path": repo_path,       # shadow: use repo directly (no isolated worktree)
        "clean_state": True,
        "classification": None,
        "localization_results": [],
        "hunks": all_hunks,               # Agent 1 will filter to Java code hunks
        "developer_aux_hunks": developer_aux_hunks,  # from target_patch (verbatim apply)
        "applied_hunks": [],
        "adapted_hunks": [],
        "refactored_hunks": [],
        "synthesized_hunks": [],
        "failed_hunks": [],
        "processed_hunk_indices": [],
        "structural_escalation_indices": [],
        "retry_contexts": [],
        "current_attempt": 1,
        "max_retries": 3,
        "synthesis_status": "",
        "routing_decision": "",
        "validation_passed": False,
        "validation_attempts": 0,
        "validation_error_context": "",
        "validation_failure_category": "",
        "validation_retry_files": [],
        "validation_results": {},
        # Agent 8 — Syntax Repair
        "syntax_repair_status": "skipped",
        "syntax_repair_attempts": 0,
        "syntax_repair_log": [],
        # Agent 9 — Fallback
        "fallback_status": "not_run",
        "fallback_attempts": 0,
        "hunk_descriptions": [],
        "tokens_used": 0,
        "wall_clock_time": 0.0,
        "status": "started",
        # All files changed in the developer's target.patch — ensures phase 0 and
        # validation detect identical test targets regardless of worktree state.
        "target_patch_changed_files": target_patch_files,
        "target_patch_file_entries": target_patch_file_entries,
        "skip_test": skip_test,
    }

    # ── 5. Phase 0: Baseline test run ────────────────────────────────────────
    # Apply developer_aux_hunks, run tests, restore. The result is the baseline:
    # tests fail here because the Java code changes are not yet applied.
    # After the agent pipeline, fail→pass = agents correctly backported the code.

    if not skip_validation and not skip_test and (developer_aux_hunks or run_phase0):
        print("\n  [pipeline] Phase 0 (baseline):")
        project = os.path.basename(repo_path).strip().lower()

        # Check cache first — baseline is deterministic for a given backport_commit
        cached_baseline = load_phase0_cache(project, backport_commit)
        if cached_baseline:
            print(f"  [pipeline] phase0 cache: hit (mode={cached_baseline.get('mode')}) — skipping baseline run")
            baseline_result = cached_baseline
        else:
            print(f"  [pipeline] phase0 cache: miss — starting baseline run for {project}")
            # Use target_patch_file_entries so phase 0 detects the same test targets as validation.
            baseline_result = run_phase0_baseline(
                repo_path, project, developer_aux_hunks,
                target_patch_file_entries,
            )
            if not baseline_result.get("skipped"):
                save_phase0_cache(project, backport_commit, baseline_result)

        results["summary"]["phase0_baseline"] = {
            "mode": baseline_result.get("mode"),
            "skipped": baseline_result.get("skipped", False),
            "cached": cached_baseline is not None,
            "test_cases_total": len((baseline_result.get("test_state") or {}).get("test_cases") or {}),
        }
        state["validation_results"] = {"phase_0_baseline_test_result": baseline_result}
    else:
        print("\n  [pipeline] Phase 0: skipped (no aux hunks or --skip-validation or --skip-test)")

    # ── 6. Run agents 1–6 ────────────────────────────────────────────────────

    print("\n  Pipeline:")
    state = run_agent("agent1_localizer", localize_hunks, state, results["agents"])

    # Record segregation results
    java_hunk_count = len(state.get("hunks", []))
    aux_hunk_count = len(state.get("developer_aux_hunks", []))
    results["summary"]["java_production_hunks"] = java_hunk_count
    results["summary"]["developer_aux_hunks"] = aux_hunk_count
    print(f"    → {java_hunk_count} Java code hunk(s) | {aux_hunk_count} developer aux hunk(s)")

    results["agents"]["agent1_localizer"]["java_hunks"] = java_hunk_count
    results["agents"]["agent1_localizer"]["aux_hunks"] = aux_hunk_count
    results["agents"]["agent1_localizer"]["localization_results"] = [
        {
            "hunk_index": i,
            "file_path": r.file_path,
            "method_used": r.method_used,
            "confidence": round(r.confidence, 3),
            "start_line": r.start_line,
            "end_line": r.end_line,
            "symbol_mappings": r.symbol_mappings,
        }
        for i, r in enumerate(state.get("localization_results", []))
    ]

    state = run_agent("agent2_classifier", classify_patch, state, results["agents"])
    cls = state.get("classification")
    if cls:
        results["agents"]["agent2_classifier"]["classification"] = {
            "patch_type": str(cls.patch_type),
            "confidence": cls.confidence,
            "reasoning": cls.reasoning,
            "is_auto_generated": cls.is_auto_generated,
        }

    state = run_agent("agent3_fastapply", fast_apply_agent, state, results["agents"])
    results["agents"]["agent3_fastapply"]["applied_count"] = len(state.get("applied_hunks", []))
    results["agents"]["agent3_fastapply"]["applied_hunks"] = make_serializable(state.get("applied_hunks", []))

    state = run_agent("agent4_namespace", namespace_adapter_agent, state, results["agents"])
    results["agents"]["agent4_namespace"]["adapted_count"] = len(state.get("adapted_hunks", []))

    state = run_agent("agent5_structural", structural_refactor_agent, state, results["agents"])
    results["agents"]["agent5_structural"]["refactored_count"] = len(state.get("refactored_hunks", []))

    state = run_agent("agent6_synthesizer", hunk_synthesizer_agent, state, results["agents"])
    synthesized = state.get("synthesized_hunks", [])
    results["agents"]["agent6_synthesizer"]["synthesized_count"] = len(synthesized)
    results["agents"]["agent6_synthesizer"]["failed_count"] = len(state.get("failed_hunks", []))
    results["agents"]["agent6_synthesizer"]["synthesis_status"] = state.get("synthesis_status", "")
    results["agents"]["agent6_synthesizer"]["synthesized_hunks"] = make_serializable(synthesized)

    # ── Fix E: per-file atomic apply ─────────────────────────────────────────
    # If any hunk in a file failed synthesis, roll back ALL changes to that file
    # so the build does not see a half-migrated state that obscures the real error.
    failed_hunks_state = state.get("failed_hunks", [])
    if failed_hunks_state:
        failed_files = {
            (h.get("file_path") or h.get("target_file") or "").split("/")[-1]: (
                h.get("file_path") or h.get("target_file") or ""
            )
            for h in failed_hunks_state
        }
        # Identify files that also have SUCCESSFUL synthesized hunks — those must be
        # rolled back since they are now partial.
        affected_files = set()
        for fp_name, fp in failed_files.items():
            if fp:
                affected_files.add(fp)
        if affected_files:
            for fp in sorted(affected_files):
                if fp:
                    abs_fp = Path(repo_path) / fp
                    # Check if the file exists in HEAD (tracked). If not, it's a newly
                    # created file — delete it from disk instead of checking it out.
                    in_head = subprocess.run(
                        ["git", "-C", repo_path, "cat-file", "-e", f"HEAD:{fp}"],
                        capture_output=True, text=True,
                    )
                    if in_head.returncode != 0:
                        # New file not in HEAD — delete it to roll back agent6's creation.
                        if abs_fp.exists():
                            abs_fp.unlink()
                            print(f"  [pipeline] Fix E: deleted new file {fp} (not in HEAD)")
                        else:
                            print(f"  [pipeline] Fix E: new file {fp} already absent — skipping")
                    else:
                        r = subprocess.run(
                            ["git", "-C", repo_path, "checkout", "HEAD", "--", fp],
                            capture_output=True, text=True,
                        )
                        if r.returncode == 0:
                            print(f"  [pipeline] Fix E: rolled back partial changes to {fp}")
                        else:
                            print(f"  [pipeline] Fix E: could not roll back {fp}: {r.stderr[:200]}")
            # Remove synthesized hunks for affected files from state so agent7 doesn't try to re-apply.
            synthesized = [
                h for h in synthesized
                if (h.get("file_path") or "") not in affected_files
            ]
            state["synthesized_hunks"] = synthesized

    # ── 6.5. Agent 8: Syntax Repair (in-memory, must run BEFORE pre-apply) ──────
    # Applies synthesized_hunks to in-memory copies of each affected file and
    # calls the Java microservice parse-check endpoint. If syntax errors are
    # found, an LLM repairs the offending new_string(s). Must run here (before
    # hunks are written to disk) so it reads the unmodified disk state.

    state = run_agent("agent8_syntax_repair", syntax_repair_agent, state, results["agents"])
    synthesized = state.get("synthesized_hunks", [])
    results["agents"]["agent8_syntax_repair"].update({
        "syntax_repair_status": state.get("syntax_repair_status", ""),
        "syntax_repair_attempts": state.get("syntax_repair_attempts", 0),
        "syntax_repair_log": state.get("syntax_repair_log", []),
        "synthesized_count_after_repair": len(synthesized),
    })

    # ── 7. Apply synthesized hunks to disk, then capture the diff ────────────────
    # Only pre-apply if Agent 8 did NOT fail. If syntax repair failed, the hunks
    # are still broken — writing them to disk would give the build garbage input.
    # The graph will route to fallback_agent instead of validator in that case.
    syntax_ok = state.get("syntax_repair_status", "skipped") != "failed"
    if synthesized and syntax_ok:
        pre_apply_result = _apply_synthesized_hunks(repo_path, synthesized)
        state["synthesized_hunks_pre_applied"] = True
        if not pre_apply_result["success"]:
            print(f"  [pipeline] WARNING: pre-apply of synthesized hunks had errors: {pre_apply_result['output'][:200]}")
    elif synthesized and not syntax_ok:
        print("  [pipeline] Syntax repair FAILED — skipping pre-apply, routing to fallback via graph")
    generated_patch = git_diff(repo_path)
    (out_dir / "generated.patch").write_text(generated_patch, encoding="utf-8")
    print(f"\n  generated.patch written ({len(generated_patch)} bytes)")

    # ── 8. Validator + fallback cycle via graph ───────────────────────────────
    # build_validator_fallback_graph() wires:
    #   START → validator → [fallback_agent → syntax_repair → validator]* → END
    #
    # When Agent 8 already failed above, the graph is invoked with
    # syntax_repair_status="failed" so the first node (validator) is skipped and
    # control goes directly to fallback_agent — matching the main graph routing.
    # The graph handles up to 2 fallback rounds via state["fallback_attempts"].

    if not skip_validation:
        val_fb_graph = build_validator_fallback_graph()
        # build_validator_fallback_graph() uses a conditional edge from START:
        #   syntax_repair_status == "failed"  → fallback_agent  (no wasted build)
        #   otherwise                         → validator
        # The state["syntax_repair_status"] set by the Agent 8 run above drives
        # this decision automatically — no extra wiring needed here.

        print("\n  [pipeline] Starting validator + fallback graph (agents 7/8/9)...")
        _validator_run_count = 0

        for chunk in val_fb_graph.stream(state):
            for node_name, node_output in chunk.items():
                # Merge node output into accumulated state.
                state = {**state, **node_output}

                if node_name == "validator":
                    _validator_run_count += 1
                    _attempt = state.get("validation_attempts", _validator_run_count)
                    label = "agent7_validator" if _validator_run_count == 1 else f"agent7_validator_retry{_validator_run_count - 1}"
                    val_passed = state.get("validation_passed", False)
                    val_cat = state.get("validation_failure_category", "")
                    val_ctx = state.get("validation_error_context", "")
                    val_res = state.get("validation_results", {})
                    results["agents"][label] = {
                        "validation_passed": val_passed,
                        "validation_failure_category": val_cat,
                        "validation_error_context": val_ctx[:500] if val_ctx else "",
                        "validation_attempts": _attempt,
                        "retry_files": state.get("validation_retry_files", []),
                        "build_success": (val_res.get("build") or {}).get("success"),
                        "build_mode": (val_res.get("build") or {}).get("mode"),
                        "test_success": (val_res.get("tests") or {}).get("success"),
                        "test_mode": (val_res.get("tests") or {}).get("mode"),
                        "test_state": val_res.get("tests", {}).get("test_state"),
                        "test_state_transition": (val_res.get("tests") or {}).get("state_transition"),
                        "compile_errors": (val_res.get("build") or {}).get("compile_errors", []),
                    }
                    status_str = "PASSED ✓" if val_passed else f"FAILED — {val_cat}"
                    print(f"    [pipeline] {label}: {status_str}")
                    if val_ctx:
                        print(f"      {val_ctx[:200]}")
                    transition = (val_res.get("tests") or {}).get("state_transition") or {}
                    if transition:
                        f2p = transition.get("fail_to_pass", [])
                        p2f = transition.get("pass_to_fail", [])
                        newly = transition.get("newly_passing", [])
                        print(f"      Test transition: fail→pass={len(f2p)}, pass→fail={len(p2f)}, newly_passing={len(newly)}")

                elif node_name == "fallback_agent":
                    fb_attempt = state.get("fallback_attempts", 1)
                    label = f"agent9_fallback_{fb_attempt}"
                    results["agents"][label] = {
                        "fallback_status": state.get("fallback_status", ""),
                        "fallback_attempts": fb_attempt,
                        "synthesized_count": len(state.get("synthesized_hunks", [])),
                        "hunk_descriptions": make_serializable(state.get("hunk_descriptions", [])),
                    }
                    print(f"    [pipeline] {label}: status={state.get('fallback_status', '?')}, "
                          f"hunks={len(state.get('synthesized_hunks', []))}")

                elif node_name == "syntax_repair":
                    # syntax_repair inside the fallback loop
                    fb_attempt = state.get("fallback_attempts", 1)
                    label = f"agent8_syntax_repair_fallback_{fb_attempt}"
                    results["agents"][label] = {
                        "syntax_repair_status": state.get("syntax_repair_status", ""),
                        "syntax_repair_attempts": state.get("syntax_repair_attempts", 0),
                        "syntax_repair_log": state.get("syntax_repair_log", []),
                    }
                    print(f"    [pipeline] {label}: {state.get('syntax_repair_status', '?')}")

        # Use the final validator result for the summary (last validator run).
        val_passed = state.get("validation_passed", False)
        val_cat = state.get("validation_failure_category", "")
        val_ctx = state.get("validation_error_context", "")
        val_res = state.get("validation_results", {})
        transition = (val_res.get("tests") or {}).get("state_transition") or {}
        print(f"\n  Final validation: {'PASSED ✓' if val_passed else f'FAILED — {val_cat}'}")
        if transition:
            f2p = transition.get("fail_to_pass", [])
            p2f = transition.get("pass_to_fail", [])
            newly = transition.get("newly_passing", [])
            print(f"  Test transition: fail→pass={len(f2p)}, pass→fail={len(p2f)}, newly_passing={len(newly)}")
            print(f"  {transition.get('reason', '')}")

    else:
        results["agents"]["agent7_validator"] = {"skipped": True}

    # ── 8. Build summary ──────────────────────────────────────────────────────

    # Re-read synthesized from final state — fallback agent may have replaced hunks.
    synthesized = state.get("synthesized_hunks", [])

    val_res = state.get("validation_results", {})
    transition = (val_res.get("tests") or {}).get("state_transition") or {}

    results["summary"].update({
        "processed_hunk_indices": state.get("processed_hunk_indices", []),
        "tokens_used": state.get("tokens_used", 0),
        "llm_token_usage": state.get("llm_token_usage", {}),
        "synthesis_status": state.get("synthesis_status", ""),
        "applied_on_disk_ag3": len(state.get("applied_hunks", [])),
        "synthesized_count": len(synthesized),
        "failed_hunks_total": len(state.get("failed_hunks", [])),
        # Agent 8 / 9 outcomes
        "syntax_repair_status": state.get("syntax_repair_status", "skipped"),
        "fallback_status": state.get("fallback_status", "not_run"),
        "fallback_attempts": state.get("fallback_attempts", 0),
        "retry_contexts": make_serializable(state.get("retry_contexts", [])),
        "failed_hunks": make_serializable(state.get("failed_hunks", [])),
        "generated_patch_bytes": len(generated_patch),
        "target_patch_bytes": len(target_patch),
        "patch_exact_match": patches_are_equivalent(generated_patch, target_patch),
        # Validation outcomes
        "validation_passed": state.get("validation_passed", None) if not skip_validation else "skipped",
        "validation_failure_category": state.get("validation_failure_category", ""),
        "build_success": (val_res.get("build") or {}).get("success"),
        "test_success": (val_res.get("tests") or {}).get("success"),
        "test_mode": (val_res.get("tests") or {}).get("mode"),
        "test_transition": transition,
        "fail_to_pass_count": len(transition.get("fail_to_pass", [])),
        "newly_passing_count": len(transition.get("newly_passing", [])),
        "pass_to_fail_count": len(transition.get("pass_to_fail", [])),
        # Success = generated patch exactly matches developer's target patch,
        # OR at least one fail→pass / newly_passing test was observed.
        "backport_success": (
            patches_are_equivalent(generated_patch, target_patch)
            or len(transition.get("fail_to_pass", [])) > 0
            or len(transition.get("newly_passing", [])) > 0
        ) if not skip_validation else None,
    })

    # ── 10. Save JSON ─────────────────────────────────────────────────────────

    (out_dir / "results.json").write_text(
        json.dumps(results, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"  results.json saved → {out_dir}")

    # ── 11. Print summary ─────────────────────────────────────────────────────

    s = results["summary"]
    print(f"\n  ┌─ Result Summary ──────────────────────────────────────────")
    print(f"  │  Total hunks          : {s['total_hunks_in_patch']}")
    print(f"  │  Java code hunks      : {s['java_production_hunks']}")
    print(f"  │  Developer aux hunks  : {s['developer_aux_hunks']}")
    print(f"  │  Applied (Agent 3)    : {s['applied_on_disk_ag3']}")
    print(f"  │  Synthesized (Agent 6): {s['synthesized_count']}")
    print(f"  │  Failed total         : {s['failed_hunks_total']}")
    print(f"  │  Synthesis status     : {s['synthesis_status']}")
    print(f"  │  Syntax repair (Ag 8) : {s.get('syntax_repair_status', 'skipped')}")
    fb_status = s.get('fallback_status', 'not_run')
    fb_attempts = s.get('fallback_attempts', 0)
    if fb_attempts > 0:
        print(f"  │  Fallback    (Ag 9)  : {fb_status} ({fb_attempts} attempt(s))")
    exact = s.get("patch_exact_match")
    if exact is not None:
        print(f"  │  Patch exact match    : {'YES ✓' if exact else 'no'}")
    if not skip_validation:
        v_label = "PASSED ✓" if s.get("validation_passed") else f"FAILED ({s.get('validation_failure_category', '?')})"
        print(f"  │  Validation           : {v_label}")
        print(f"  │  Build                : {'OK' if s.get('build_success') else 'FAILED'}")
        if s.get("test_mode") and s.get("test_mode") != "skip":
            f2p = s.get("fail_to_pass_count", 0)
            p2f = s.get("pass_to_fail_count", 0)
            newly = s.get("newly_passing_count", 0)
            runner_ok = "OK" if s.get("test_success") else "PARTIAL"
            print(f"  │  Tests                : {runner_ok} (fail→pass={f2p}, newly={newly}, pass→fail={p2f})")
    print(f"  │  Tokens used          : {s['tokens_used']}")
    print(f"  └───────────────────────────────────────────────────────────")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Full pipeline shadow test v3 (Agents 1-7, with Validation)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_full_pipeline_shadow_v3.py
  python run_full_pipeline_shadow_v3.py --mode dataset --repo elasticsearch --type TYPE-I
  python run_full_pipeline_shadow_v3.py --mode dataset --count 10
  python run_full_pipeline_shadow_v3.py --repo elasticsearch --type TYPE-I
  python run_full_pipeline_shadow_v3.py --count 3
  python run_full_pipeline_shadow_v3.py --project elasticsearch --commit abc123def456
  python run_full_pipeline_shadow_v3.py --skip-validation
  python run_full_pipeline_shadow_v3.py --force
  python run_full_pipeline_shadow_v3.py --failed
  python run_full_pipeline_shadow_v3.py --phase0
        """,
    )
    parser.add_argument("--mode", choices=["yaml", "dataset"], default="yaml",
                        help="Patch source: 'yaml' (patches.yaml, default) or 'dataset' "
                             "(dataset/all_projects_final.csv)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="Path to patches.yaml (only used in yaml mode)")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET,
                        help="Path to CSV dataset (only used in dataset mode)")
    parser.add_argument("--repo", type=str, default=None,
                        help="Filter by repository name")
    parser.add_argument("--project", type=str, default=None,
                        help="Filter by project name")
    parser.add_argument("--type", type=str, default=None,
                        help="Filter by patch type (TYPE-I … TYPE-V)")
    parser.add_argument("--commit", type=str, default=None,
                        help="Filter by original commit SHA")
    parser.add_argument("--count", type=int, default=None,
                        help="Limit to N patches")
    parser.add_argument("--skip-validation", action="store_true",
                        help="Run agents 1-6 only (no build/test)")
    parser.add_argument("--skip-test", action="store_true",
                        help="Build only in validation agent (skip tests)")
    parser.add_argument("--force", action="store_true",
                        help="Force re-run even if results.json exists")
    parser.add_argument("--failed", action="store_true",
                        help="Only re-run patches whose last result was a failure (implies --force for those patches)")
    parser.add_argument("--phase0", action="store_true",
                        help="Force phase 0 baseline run even when there are no aux hunks")
    parser.add_argument("--no-notifications", action="store_true",
                        help="Disable Telegram notifications")

    args = parser.parse_args()

    # When --failed is set, count must be applied AFTER filtering to failed patches,
    # so we load everything first and trim later.
    load_count = None if args.failed else args.count

    if args.mode == "dataset":
        if not args.dataset.exists():
            print(f"ERROR: Dataset file not found: {args.dataset}")
            sys.exit(1)
        patches = get_patches_from_csv(
            args.dataset,
            repo_filter=args.repo,
            project_filter=args.project,
            type_filter=args.type,
            commit_filter=args.commit,
            count_limit=load_count,
        )
    else:
        try:
            config = load_config(args.config)
        except FileNotFoundError as e:
            print(f"ERROR: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"ERROR parsing config: {e}")
            sys.exit(1)
        patches = get_patches_from_config(
            config,
            repo_filter=args.repo,
            project_filter=args.project,
            type_filter=args.type,
            commit_filter=args.commit,
            count_limit=load_count,
        )

    # Apply count after failed-patch filtering
    if args.failed and args.count:
        patches = [
            p for p in patches
            if _patch_previously_failed(
                OUTPUT_DIR / p["repo"] / f"{p['type']}_{p['original_commit'][:8]}"
            )
        ][:args.count]

    if not patches:
        print(f"No patches found (mode={args.mode}, repo={args.repo}, type={args.type}, count={args.count})")
        sys.exit(0)

    run_ts = datetime.now().isoformat()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Shadow v3 run started at {run_ts}")
    print(f"Processing {len(patches)} patch(es){' [validation SKIPPED]' if args.skip_validation else ''}")
    print(f"Results → {OUTPUT_DIR.resolve()}\n")

    for i, item in enumerate(patches, start=1):
        repo_path = item["repo_path"]
        if not os.path.exists(repo_path):
            print(f"\n[{i}/{len(patches)}] ERROR: Repo not found at {repo_path}")
            continue
        print(f"\n[{i}/{len(patches)}]", end="")
        try:
            process_patch(
                item, run_ts,
                skip_validation=args.skip_validation,
                force=args.force or args.failed,
                skip_test=args.skip_test,
                failed_only=args.failed,
                run_phase0=args.phase0,
            )
        except Exception:
            print(f"\n  FATAL ERROR for {item['type']}:")
            traceback.print_exc()
        finally:
            git_checkout(repo_path, "main")
        update_summary_md(OUTPUT_DIR, no_notifications=args.no_notifications)

    print(f"\n{'='*64}")
    print(f"Shadow v3 run complete. Results in {OUTPUT_DIR.resolve()}/")
    print(f"{'='*64}")


if __name__ == "__main__":
    main()
