#!/usr/bin/env python3
"""
Extract Gradle test targets for Elasticsearch modified/added test files.

Output JSON schema (matches druid/crate format expected by ValidationToolkit):
  {
    "modified": ["module:Full.Class.Name", ...],
    "added":    ["module:Full.Class.Name", ...],
    "source_modules": ["server", "x-pack/plugin/core", ...],
    "all_modules":    ["server", ...]
  }

The "module:ClassName" token is later converted to a Gradle task by run_tests.sh:
  :server:test --tests "org.elasticsearch.index.FooTests"
"""

import argparse
import json
import os
import subprocess
import sys


# Elasticsearch source-set paths that indicate test code.
TEST_SOURCE_SETS = (
    "/src/test/java/",
    "/src/internalClusterTest/java/",
    "/src/javaRestTest/java/",
    "/src/yamlRestTest/java/",
    "/src/integTest/java/",
    "/src/integrationTest/java/",
)

# Elasticsearch test-class name suffixes (used as a secondary signal).
TEST_CLASS_SUFFIXES = ("Tests.java", "Test.java", "IT.java", "TestCase.java")

# Gradle modules to ignore (top-level noise).
IGNORED_MODULES = {"benchmarks", "distribution", "docs", "qa"}


def _find_gradle_module(repo: str, rel_path: str) -> str:
    """
    Walk up the file path looking for a build.gradle or build.gradle.kts.
    Returns a POSIX-style relative path from the repo root (e.g. 'server',
    'x-pack/plugin/core').  Returns "" if none is found.
    """
    head = os.path.dirname(rel_path)
    while head:
        for build_file in ("build.gradle", "build.gradle.kts"):
            if os.path.exists(os.path.join(repo, head, build_file)):
                return head.replace("\\", "/")
        parent = os.path.dirname(head)
        if parent == head:
            break
        head = parent
    # Check root
    for build_file in ("build.gradle", "build.gradle.kts"):
        if os.path.exists(os.path.join(repo, build_file)):
            return ""
    return ""


def _is_test_file(rel_path: str) -> bool:
    """Return True if this file looks like a test source file."""
    for src_set in TEST_SOURCE_SETS:
        if src_set in rel_path:
            return True
    return any(rel_path.endswith(s) for s in TEST_CLASS_SUFFIXES)


def _is_main_source(rel_path: str) -> bool:
    return "/src/main/java/" in rel_path and rel_path.endswith(".java")


def _extract_class_name(rel_path: str) -> str:
    """
    Convert a repo-relative Java file path to a fully-qualified class name.
    Works for all test source sets.
    """
    # Find the /java/ separator closest to the package root.
    for src_set in TEST_SOURCE_SETS:
        if src_set in rel_path:
            class_part = rel_path.split(src_set, 1)[1]
            return class_part.replace("/", ".").replace("\\", ".").replace(".java", "")
    # Fallback: look for any /java/ segment.
    if "/java/" in rel_path:
        class_part = rel_path.split("/java/", 1)[1]
        return class_part.replace("/", ".").replace("\\", ".").replace(".java", "")
    return os.path.basename(rel_path).replace(".java", "")


def _parse_status_output(output: str) -> list[tuple[str, str]]:
    """Parse 'git diff --name-status' or 'git diff-tree --name-status' output."""
    entries: list[tuple[str, str]] = []
    for line in output.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            status, path = parts[0].strip(), parts[1].strip()
            if status and path:
                entries.append((status, path))
    return entries


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Detect Elasticsearch Gradle test targets from changed files."
    )
    parser.add_argument("--repo", required=True, help="Path to the git repository")
    parser.add_argument("--commit", help="Commit hash to analyse")
    parser.add_argument(
        "--worktree",
        action="store_true",
        help="Analyse the current worktree diff (HEAD vs working tree)",
    )
    args = parser.parse_args()

    empty = {"modified": [], "added": [], "source_modules": [], "all_modules": []}

    if not args.commit and not args.worktree:
        print(json.dumps(empty))
        return

    # 1. Get changed-file list.
    if args.worktree:
        cmd = ["git", "diff", "--name-status", "--diff-filter=AMRT"]
    else:
        cmd = ["git", "diff-tree", "--no-commit-id", "--name-status", "-r", args.commit]

    try:
        output = subprocess.check_output(cmd, cwd=args.repo, text=True, timeout=60)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print(json.dumps(empty))
        return

    modified_tests: set[str] = set()
    added_tests: set[str] = set()
    source_modules: set[str] = set()
    all_modules: set[str] = set()

    for status, f in _parse_status_output(output):
        module = _find_gradle_module(args.repo, f)

        # Skip ignored top-level modules.
        top_level = module.split("/")[0] if module else ""
        if top_level in IGNORED_MODULES:
            continue

        if module:
            all_modules.add(module)

        # Track touched source modules for module-level test fallback.
        if _is_main_source(f) and module:
            source_modules.add(module)

        if not _is_test_file(f):
            continue

        # Build "module:ClassName" token.
        class_name = _extract_class_name(f)
        target = f"{module}:{class_name}" if module else class_name

        if status == "A":
            added_tests.add(target)
        else:
            modified_tests.add(target)

    result = {
        "modified": sorted(modified_tests),
        "added": sorted(added_tests),
        "source_modules": sorted(source_modules),
        "all_modules": sorted(all_modules),
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()