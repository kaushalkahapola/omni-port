#!/usr/bin/env python3
"""
Full pipeline shadow run (Agents 1-6) — Parameterized version

Supports running any patches from any repo with any type via YAML config.

Usage:
  # Run all patches from patches.yaml
  python run_full_pipeline_shadow_v2.py

  # Run specific config file
  python run_full_pipeline_shadow_v2.py --config patches.yaml

  # Filter by repo only
  python run_full_pipeline_shadow_v2.py --repo elasticsearch

  # Filter by patch type
  python run_full_pipeline_shadow_v2.py --type TYPE-I

  # Filter by specific repo and type
  python run_full_pipeline_shadow_v2.py --repo elasticsearch --type TYPE-II

  # Limit to N patches total
  python run_full_pipeline_shadow_v2.py --count 5

For each patch, creates a folder under tests/shadow_run_results/<TYPE>_<sha>/ with:
  mainline.patch  — the original commit diff (what we're backporting FROM)
  target.patch    — the actual backport commit (ground truth to compare against)
  generated.patch — the diff we produced by running the full pipeline
  results.json    — per-agent timing, outputs, metrics, failure details
"""

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
from typing import Optional, List, Dict, Any

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.state import BackportState
from src.tools.patch_parser import parse_unified_diff
from src.agents.agent1_localizer import localize_hunks
from src.agents.agent2_classifier import classify_patch
from src.agents.agent3_fastapply import fast_apply_agent
from src.agents.agent4_namespace import namespace_adapter_agent
from src.agents.agent5_structural import structural_refactor_agent
from src.agents.agent6_synthesizer import hunk_synthesizer_agent

# ── Config ────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = Path(__file__).parent / "patches.yaml"
OUTPUT_DIR = Path(__file__).parent / "shadow_run_results"


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_config(config_path: Path) -> Dict[str, Any]:
    """Load YAML config with patch definitions."""
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def get_patches_from_config(
    config: Dict[str, Any],
    repo_filter: Optional[str] = None,
    type_filter: Optional[str] = None,
    count_limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Extract patches from config, with optional filters.
    Returns list of dicts with keys: type, original_commit, backport_commit, repo_path, description
    """
    patches = []

    for repo_name, repo_config in config.get("repos", {}).items():
        if repo_filter and repo_name != repo_filter:
            continue

        repo_path = repo_config.get("path")
        for patch in repo_config.get("patches", []):
            if type_filter and patch.get("type") != type_filter:
                continue

            patches.append({
                "repo": repo_name,
                "repo_path": repo_path,
                "type": patch.get("type", "UNKNOWN"),
                "original_commit": patch.get("original_commit"),
                "backport_commit": patch.get("backport_commit"),
                "description": patch.get("description", ""),
            })

            if count_limit and len(patches) >= count_limit:
                return patches

    return patches


def is_production_java_hunk(h: dict) -> bool:
    path = h.get("file_path", "")
    if not path.endswith(".java"):
        return False
    if "Test" in path or "Tests" in path:
        return False
    return True


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
    """Unstaged diff against the current HEAD (modifications not yet committed)."""
    result = subprocess.run(
        ["git", "-C", repo_path, "diff"],
        capture_output=True, text=True
    )
    return result.stdout


def make_serializable(obj):
    """Recursively convert Pydantic models to plain dicts for JSON serialisation."""
    if hasattr(obj, "model_dump"):
        return make_serializable(obj.model_dump())
    if isinstance(obj, dict):
        return {k: make_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [make_serializable(v) for v in obj]
    return obj


def run_agent(label: str, fn, state: BackportState, agent_records: dict) -> BackportState:
    """
    Runs a single agent node, records timing and any exception.
    If the agent raises, original state is preserved and the error is recorded.
    """
    print(f"    Running {label}...", end=" ", flush=True)
    start = time.time()
    error_msg = None
    try:
        state = fn(state)
        print(f"OK ({time.time() - start:.2f}s)")
    except Exception:
        error_msg = traceback.format_exc()
        print(f"ERROR ({time.time() - start:.2f}s)")
        print(f"      {error_msg.splitlines()[-1]}")

    agent_records[label] = {
        "elapsed_s": round(time.time() - start, 2),
        "error": error_msg,
    }
    return state


def apply_synthesized_hunks(repo_path: str, synthesized_hunks: list) -> list:
    """
    Applies Agent 6 synthesized hunks (old_string → new_string) to disk.
    Returns a list of error messages for hunks that could not be applied.
    """
    errors = []
    for sh in synthesized_hunks:
        file_path = sh.get("file_path", "")
        old_str = sh.get("old_string", "")
        new_str = sh.get("new_string", "")
        if not file_path or not old_str:
            errors.append(f"Empty file_path or old_string for hunk: {sh}")
            continue
        target = Path(repo_path) / file_path
        if not target.exists():
            errors.append(f"File not found: {file_path}")
            continue
        content = target.read_text(encoding="utf-8")
        if old_str in content:
            target.write_text(content.replace(old_str, new_str, 1), encoding="utf-8")
        else:
            errors.append(f"old_string not found in {file_path} (len={len(old_str)})")
    return errors


# ── Main processing ───────────────────────────────────────────────────────────

def process_patch(item: dict, run_ts: str) -> None:
    patch_type = item["type"]
    original_commit = item["original_commit"]
    backport_commit = item["backport_commit"]
    repo_path = item["repo_path"]
    repo_name = item["repo"]
    description = item.get("description", "")

    out_dir = OUTPUT_DIR / f"{patch_type}_{original_commit[:8]}"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*64}")
    print(f"  {patch_type}  |  repo={repo_name}  orig={original_commit[:12]}")
    if description:
        print(f"  {description}")
    print(f"  backport={backport_commit[:12]}")
    print(f"{'='*64}")

    results: dict = {
        "run_timestamp": run_ts,
        "repo": repo_name,
        "patch_type": patch_type,
        "original_commit": original_commit,
        "backport_commit": backport_commit,
        "description": description,
        "agents": {},
        "summary": {},
    }

    # ── 1. Capture reference patches ─────────────────────────────────────────

    mainline_patch = git_show(repo_path, original_commit)
    if not mainline_patch:
        print(f"  ERROR: Cannot retrieve commit {original_commit}")
        return
    (out_dir / "mainline.patch").write_text(mainline_patch, encoding="utf-8")
    print(f"  mainline.patch written ({len(mainline_patch)} bytes)")

    target_patch = git_show(repo_path, backport_commit)
    if target_patch:
        (out_dir / "target.patch").write_text(target_patch, encoding="utf-8")
        print(f"  target.patch written  ({len(target_patch)} bytes)")
    else:
        print(f"  WARNING: Could not retrieve backport commit {backport_commit}")

    # ── 2. Checkout target repo to state just before the backport ─────────────

    print(f"  Checking out {backport_commit}~1 ...")
    git_checkout(repo_path, f"{backport_commit}~1")

    # ── 3. Parse and filter hunks ─────────────────────────────────────────────

    all_hunks = parse_unified_diff(mainline_patch)
    java_hunks = [h for h in all_hunks if is_production_java_hunk(h)]
    print(f"  Parsed {len(all_hunks)} total hunks → {len(java_hunks)} Java production hunks")
    results["summary"]["total_hunks_in_patch"] = len(all_hunks)
    results["summary"]["java_production_hunks"] = len(java_hunks)

    # ── 4. Initialise state ───────────────────────────────────────────────────

    state = BackportState(
        patch_content=mainline_patch,
        target_repo_path=repo_path,
        target_branch=f"{backport_commit}~1",
        worktree_path=None,
        clean_state=True,
        classification=None,
        localization_results=[],
        hunks=java_hunks,
        applied_hunks=[],
        adapted_hunks=[],
        refactored_hunks=[],
        synthesized_hunks=[],
        failed_hunks=[],
        processed_hunk_indices=[],
        retry_contexts=[],
        current_attempt=1,
        max_retries=3,
        synthesis_status="",
        tokens_used=0,
        wall_clock_time=0.0,
        status="started",
    )

    # ── 5. Run pipeline ───────────────────────────────────────────────────────

    print("\n  Pipeline:")
    state = run_agent("agent1_localizer", localize_hunks, state, results["agents"])
    # Enrich localizer record with per-hunk detail
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

    # ── 6. Apply synthesized hunks to disk (Agent 3 already wrote its hunks) ─

    synth_errors = apply_synthesized_hunks(repo_path, synthesized)
    results["summary"]["synthesized_apply_errors"] = synth_errors
    if synth_errors:
        print(f"\n  WARNING: {len(synth_errors)} synthesized hunk(s) could not be applied:")
        for e in synth_errors:
            print(f"    - {e}")

    # ── 7. Capture generated patch (git diff of all modified files) ───────────

    generated_patch = git_diff(repo_path)
    (out_dir / "generated.patch").write_text(generated_patch, encoding="utf-8")
    print(f"\n  generated.patch written ({len(generated_patch)} bytes)")

    # ── 8. Build summary ──────────────────────────────────────────────────────

    results["summary"].update({
        "processed_hunk_indices": state.get("processed_hunk_indices", []),
        "tokens_used": state.get("tokens_used", 0),
        "synthesis_status": state.get("synthesis_status", ""),
        "applied_on_disk": len(state.get("applied_hunks", [])),
        "synthesized_applied": len(synthesized) - len(synth_errors),
        "failed_hunks_total": len(state.get("failed_hunks", [])),
        "retry_contexts": make_serializable(state.get("retry_contexts", [])),
        "failed_hunks": make_serializable(state.get("failed_hunks", [])),
        "generated_patch_bytes": len(generated_patch),
        "target_patch_bytes": len(target_patch) if target_patch else 0,
    })

    # ── 9. Save JSON ──────────────────────────────────────────────────────────

    (out_dir / "results.json").write_text(
        json.dumps(results, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"  results.json saved → {out_dir}")

    # ── 10. Print quick stats ─────────────────────────────────────────────────

    s = results["summary"]
    print(f"\n  ┌─ Result Summary ──────────────────────────────────────────")
    print(f"  │  Hunks in patch  : {s['java_production_hunks']}")
    print(f"  │  Applied (Ag3)   : {s['applied_on_disk']}")
    print(f"  │  Synthesized (Ag6): {s['synthesized_applied']} applied, {len(synth_errors)} errors")
    print(f"  │  Failed total     : {s['failed_hunks_total']}")
    print(f"  │  Tokens used      : {s['tokens_used']}")
    print(f"  │  Synthesis status : {s['synthesis_status']}")
    print(f"  └───────────────────────────────────────────────────────────")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run full pipeline shadow test on parameterized patches",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_full_pipeline_shadow_v2.py
  python run_full_pipeline_shadow_v2.py --config patches.yaml
  python run_full_pipeline_shadow_v2.py --repo elasticsearch --type TYPE-I
  python run_full_pipeline_shadow_v2.py --count 5
        """
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"YAML config file (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Filter by repository name (e.g., elasticsearch, crate)",
    )
    parser.add_argument(
        "--type",
        type=str,
        default=None,
        help="Filter by patch type (e.g., TYPE-I, TYPE-II, TYPE-III)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=None,
        help="Limit to N patches",
    )

    args = parser.parse_args()

    # Load config
    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR parsing config: {e}")
        sys.exit(1)

    # Extract patches with filters
    patches = get_patches_from_config(
        config,
        repo_filter=args.repo,
        type_filter=args.type,
        count_limit=args.count,
    )

    if not patches:
        print(f"No patches found matching filters (repo={args.repo}, type={args.type}, count={args.count})")
        sys.exit(0)

    run_ts = datetime.now().isoformat()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Shadow run started at {run_ts}")
    print(f"Processing {len(patches)} patch(es)")
    print(f"Results → {OUTPUT_DIR.resolve()}\n")

    for i, item in enumerate(patches, start=1):
        repo_path = item["repo_path"]
        if not os.path.exists(repo_path):
            print(f"\n[{i}/{len(patches)}] ERROR: Repo not found at {repo_path}")
            continue

        print(f"\n[{i}/{len(patches)}]", end="")
        try:
            process_patch(item, run_ts)
        except Exception:
            print(f"\n  FATAL ERROR for {item['type']}:")
            traceback.print_exc()
        finally:
            # Always restore repo between patches
            git_checkout(repo_path, "main")

    # Final restore
    print(f"\n{'='*64}")
    print(f"Shadow run complete. Results in {OUTPUT_DIR.resolve()}/")
    print(f"{'='*64}")


if __name__ == "__main__":
    main()
