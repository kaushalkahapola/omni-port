#!/usr/bin/env python3
"""
Enhanced test pipeline runner with Telegram notifications.

This script wraps the shadow v3 pipeline and sends test result summaries
to your Telegram group in real-time.

Usage:
  # Run all patches and send notifications
  python tests/run_with_telegram_notifications.py

  # Filter by repo
  python tests/run_with_telegram_notifications.py --repo elasticsearch

  # Limit count
  python tests/run_with_telegram_notifications.py --count 5

  # Disable notifications
  python tests/run_with_telegram_notifications.py --no-notifications
"""

import os
import sys
import json
import subprocess
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def run_test_pipeline(args: List[str]) -> bool:
    """Run the shadow v3 pipeline."""
    cmd = ["python3", "tests/run_full_pipeline_shadow_v3.py"] + args
    print(f"\n🚀 Running: {' '.join(cmd)}\n")

    result = subprocess.run(cmd, cwd=Path(__file__).parent.parent)
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(
        description="Run test pipeline with Telegram notifications"
    )
    # Patch filtering/source arguments (passed to shadow_v3)
    parser.add_argument("--mode", choices=["yaml", "dataset"], default="yaml",
                        help="Patch source: 'yaml' (default) or 'dataset'")
    parser.add_argument("--repo", help="Filter by repository name")
    parser.add_argument("--project", help="Filter by project name (alias for repo)")
    parser.add_argument("--type", help="Filter by patch type (TYPE-I … TYPE-V)")
    parser.add_argument("--commit", help="Filter by original commit SHA")
    parser.add_argument("--count", type=int, help="Limit number of patches")
    parser.add_argument("--config", type=Path, help="Path to patches.yaml")
    parser.add_argument("--dataset", type=Path, help="Path to CSV dataset")
    parser.add_argument("--skip-validation", action="store_true", help="Skip validation phase")

    # Wrapper-specific arguments
    parser.add_argument("--no-notifications", action="store_true", help="Disable individual patch notifications")

    args = parser.parse_args()

    # Build args for pipeline
    pipeline_args = []
    if args.mode:
        pipeline_args.extend(["--mode", args.mode])
    if args.repo:
        pipeline_args.extend(["--repo", args.repo])
    if args.project:
        pipeline_args.extend(["--project", args.project])
    if args.type:
        pipeline_args.extend(["--type", args.type])
    if args.commit:
        pipeline_args.extend(["--commit", args.commit])
    if args.count:
        pipeline_args.extend(["--count", str(args.count)])
    if args.config:
        pipeline_args.extend(["--config", str(args.config)])
    if args.dataset:
        pipeline_args.extend(["--dataset", str(args.dataset)])
    if args.skip_validation:
        pipeline_args.append("--skip-validation")
    if args.no_notifications:
        pipeline_args.append("--no-notifications")

    # Run pipeline
    print("=" * 60)
    print("🔄 Test Pipeline with Telegram Notifications")
    print("=" * 60)

    success = run_test_pipeline(pipeline_args)

    if not success:
        print("\n❌ Pipeline failed")
        return 1

    print("\n✅ Pipeline completed! Check Telegram for the updated SUMMARY.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
