#!/usr/bin/env python3
"""
Extract Maven test targets for Hadoop modified/added test files.

Output JSON schema:
  {
    "modified": ["module:fully.qualified.TestClass", ...],
    "added":    ["module:fully.qualified.TestClass", ...],
    "source_modules": ["hadoop-common-project/hadoop-common", ...],
    "all_modules":    ["hadoop-common-project/hadoop-common", ...]
  }
"""
import argparse
import json
import os
import subprocess

BLACKLIST_MODULES = [
    "hadoop-yarn-project/hadoop-yarn/hadoop-yarn-applications/hadoop-yarn-applications-catalog/hadoop-yarn-applications-catalog-webapp",
    "hadoop-yarn-project/hadoop-yarn/hadoop-yarn-applications/hadoop-yarn-applications-catalog/hadoop-yarn-applications-catalog-docker",
]

TEST_SUFFIXES = ("Test.java", "Tests.java", "IT.java")


def _is_blacklisted(module_path: str) -> bool:
    return any(module_path == b or module_path.startswith(b + "/") for b in BLACKLIST_MODULES)


def _find_maven_module(repo: str, rel_path: str) -> str:
    current = os.path.dirname(rel_path)
    while current:
        if os.path.exists(os.path.join(repo, current, "pom.xml")):
            return "" if _is_blacklisted(current) else current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return ""


def _is_test_file(rel_path: str) -> bool:
    if "/src/test/java/" not in rel_path:
        return False
    name = os.path.basename(rel_path)
    return (name.startswith("Test") and name.endswith(".java")) or any(rel_path.endswith(s) for s in TEST_SUFFIXES)


def _is_main_source(rel_path: str) -> bool:
    return "/src/main/java/" in rel_path and rel_path.endswith(".java")


def _extract_class(rel_path: str) -> str:
    if "/src/test/java/" not in rel_path:
        return ""
    return rel_path.split("/src/test/java/")[1].replace("/", ".").replace(".java", "")


def _parse_entries(output: str) -> list:
    entries = []
    for line in output.strip().splitlines():
        parts = line.split("\t")
        status = parts[0].strip() if parts else ""
        if status.startswith("R") or status.startswith("C"):
            filepath = parts[2].strip() if len(parts) >= 3 else ""
        else:
            filepath = parts[1].strip() if len(parts) >= 2 else ""
        if status and filepath:
            entries.append((status[0], filepath))
    return entries


def main():
    parser = argparse.ArgumentParser(description="Extract Maven test targets for Hadoop")
    parser.add_argument("--repo", required=True)
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
            entries = [(str(i[0]), str(i[1])) if isinstance(i, (list, tuple)) and len(i) == 2
                       else ("M", str(i)) for i in raw]
        except Exception:
            entries = []
    else:
        cmd = (["git", "diff", "--name-status", "--diff-filter=AMRT"] if args.worktree
               else ["git", "diff-tree", "--no-commit-id", "--name-status", "-r", args.commit])
        try:
            output = subprocess.check_output(cmd, cwd=args.repo, text=True, timeout=60)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            print(json.dumps(empty))
            return
        entries = _parse_entries(output)

    modified_tests: set = set()
    added_tests: set = set()
    source_modules: set = set()
    all_modules: set = set()

    for status, f in entries:
        module = _find_maven_module(args.repo, f)
        if not module:
            continue
        all_modules.add(module)

        if _is_main_source(f):
            source_modules.add(module)

        if not _is_test_file(f):
            continue

        cls = _extract_class(f)
        if not cls:
            continue

        target = f"{module}:{cls}"
        if status == "A":
            added_tests.add(target)
        else:
            modified_tests.add(target)

    print(json.dumps({
        "modified": sorted(modified_tests),
        "added": sorted(added_tests),
        "source_modules": sorted(source_modules),
        "all_modules": sorted(all_modules),
    }))


if __name__ == "__main__":
    main()
