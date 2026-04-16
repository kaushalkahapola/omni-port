#!/usr/bin/env python3
import argparse
import subprocess
import os
import json


IGNORED_MODULES = {"web-console", "distribution", "docs", "examples"}


def _find_maven_module(repo: str, rel_path: str) -> str:
    """Find the nearest parent directory (from file path upward) that contains pom.xml."""
    head = rel_path
    while head:
        head, _ = os.path.split(head)
        if os.path.exists(os.path.join(repo, head, "pom.xml")):
            return head
    return ""


def _parse_changed_entries(output: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for line in output.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        status = parts[0].strip()
        file_path = parts[1].strip()
        if not status or not file_path:
            continue
        entries.append((status, file_path))
    return entries

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="Path to the git repository")
    parser.add_argument("--commit", help="Commit hash to analyze")
    parser.add_argument(
        "--worktree",
        action="store_true",
        help="Analyze current worktree diff instead of a commit",
    )
    parser.add_argument("--files-json", help="JSON array of [status, filepath] pairs (skips git)")
    args = parser.parse_args()

    if not args.commit and not args.worktree and not args.files_json:
        print(json.dumps({"modified": [], "added": [], "source_modules": [], "all_modules": []}))
        return

    modified_tests = set()
    added_tests = set()
    source_modules = set()
    all_modules = set()

    if args.files_json:
        # Use patch-derived entries directly — no git needed
        try:
            raw_entries = json.loads(args.files_json)
            entries: list[tuple[str, str]] = []
            for item in raw_entries:
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
            output = subprocess.check_output(cmd, cwd=args.repo, text=True)
        except subprocess.CalledProcessError:
            print(json.dumps({"modified": [], "added": [], "source_modules": [], "all_modules": []}))
            return

        entries = _parse_changed_entries(output)

    for status, f in entries:
        module_path = _find_maven_module(args.repo, f)
        if not module_path:
            continue
        if module_path in IGNORED_MODULES:
            continue
        all_modules.add(module_path)

        if f.endswith(".java") and "src/main/java/" in f:
            source_modules.add(module_path)

        if not f.endswith("Test.java"):
            continue

        if "src/test/java/" in f:
            try:
                class_path = f.split("src/test/java/")[1]
                class_name = class_path.replace("/", ".").replace(".java", "")
                target = f"{module_path}:{class_name}"
                if status == "A":
                    added_tests.add(target)
                else:
                    modified_tests.add(target)
            except Exception:
                continue

    result = {
        "modified": sorted(list(modified_tests)),
        "added": sorted(list(added_tests)),
        "source_modules": sorted(list(source_modules)),
        "all_modules": sorted(list(all_modules)),
    }
    print(json.dumps(result))

if __name__ == "__main__":
    main()