#!/usr/bin/env python3
"""
Extract Gradle test targets for OpenSearch SQL modified/added test files.

Output JSON schema:
  {
    "modified": [":module:task --tests ClassName", ...],
    "added":    [":module:task --tests ClassName", ...],
    "source_modules": ["module", ...],
    "all_modules":    ["module", ...]
  }
"""
import argparse
import json
import os
import subprocess
import sys

TEST_SUFFIXES = ("Test.java", "Tests.java", "TestCase.java")
# IT (integration test) files require a running cluster — exclude from targeted runs
IT_SUFFIXES = ("IT.java",)


def _find_gradle_module(repo: str, rel_path: str) -> str:
    """Walk up looking for build.gradle; return Gradle-style :module path."""
    head = os.path.dirname(rel_path)
    while head:
        if os.path.exists(os.path.join(repo, head, "build.gradle")):
            return ":" + head.replace("/", ":")
        parent = os.path.dirname(head)
        if parent == head:
            break
        head = parent
    if os.path.exists(os.path.join(repo, "build.gradle")):
        return ""
    return ""


def _is_test_file(rel_path: str) -> bool:
    return any(rel_path.endswith(s) for s in TEST_SUFFIXES)


def _is_it_file(rel_path: str) -> bool:
    return any(rel_path.endswith(s) for s in IT_SUFFIXES)


def _is_main_source(rel_path: str) -> bool:
    return "/src/main/java/" in rel_path and rel_path.endswith(".java")


def _extract_target(module_path: str, rel_path: str) -> str:
    """Convert a file path to a Gradle test target string."""
    if "/java/" not in rel_path:
        return f"{module_path}:test"

    pre_java, class_part = rel_path.split("/java/", 1)
    class_name = class_part.replace("/", ".").replace(".java", "")

    # Determine task from source set directory name (e.g. src/test, src/integTest)
    if "/src/" in pre_java:
        task_name = pre_java.split("/src/")[-1]
    else:
        task_name = "test"

    # IT classes in src/test usually run under integTest task
    if task_name == "test" and rel_path.endswith("IT.java"):
        task_name = "integTest"

    return f"{module_path}:{task_name} --tests {class_name}"


def _parse_status_output(output: str) -> list[tuple[str, str]]:
    entries = []
    for line in output.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            status, path = parts[0].strip(), parts[1].strip()
            if status and path:
                entries.append((status, path))
    return entries


def main():
    parser = argparse.ArgumentParser(description="Extract Gradle test targets for OpenSearch SQL")
    parser.add_argument("--repo", required=True, help="Path to the git repository")
    parser.add_argument("--commit", help="Commit hash to analyze")
    parser.add_argument("--worktree", action="store_true", help="Analyze current worktree diff")
    parser.add_argument("--files-json", help="JSON array of [status, filepath] pairs (skips git)")
    args = parser.parse_args()

    empty = {"modified": [], "added": [], "source_modules": [], "all_modules": []}

    if not args.commit and not args.worktree and not args.files_json:
        print(json.dumps(empty))
        return

    if args.files_json:
        try:
            raw = json.loads(args.files_json)
            entries = []
            for item in raw:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    entries.append((str(item[0]), str(item[1])))
                else:
                    entries.append(("M", str(item)))
        except Exception:
            entries = []
    else:
        if args.worktree:
            cmd = ["git", "diff", "--name-status", "--diff-filter=AMRT"]
        else:
            cmd = ["git", "diff-tree", "--no-commit-id", "--name-status", "-r", args.commit]
        try:
            output = subprocess.check_output(cmd, cwd=args.repo, text=True, timeout=60)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            print(json.dumps(empty))
            return
        entries = _parse_status_output(output)

    modified_tests: set[str] = set()
    added_tests: set[str] = set()
    source_modules: set[str] = set()
    all_modules: set[str] = set()
    it_modules: set[str] = set()  # modules whose only test changes are ITs

    for status, f in entries:
        module = _find_gradle_module(args.repo, f)
        module_key = module.lstrip(":").replace(":", "/") if module else ""

        if module_key:
            all_modules.add(module_key)

        if _is_main_source(f) and module_key:
            source_modules.add(module_key)

        if _is_it_file(f):
            # Track IT-only modules — if no unit tests are found we'll fall back
            # to running source_modules unit tests instead.
            if module_key:
                it_modules.add(module_key)
            continue

        if not _is_test_file(f):
            continue

        target = _extract_target(module, f)
        if status == "A":
            added_tests.add(target)
        else:
            modified_tests.add(target)

    # If only IT files were touched (no runnable unit test targets), fall back to
    # running the unit tests of the source modules touched by the patch.
    # This gives us meaningful pass/fail signal even when the actual test changes
    # are cluster-integration tests that can't run standalone.
    if not modified_tests and not added_tests and source_modules:
        for mod in source_modules:
            gradle_mod = ":" + mod.replace("/", ":")
            modified_tests.add(f"{gradle_mod}:test")

    result = {
        "modified": sorted(modified_tests),
        "added": sorted(added_tests),
        "source_modules": sorted(source_modules),
        "all_modules": sorted(all_modules),
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
