#!/usr/bin/env python3
import argparse
import subprocess
import sys
import os
import json

# Modules known to be problematic or requiring complex environments
BLACKLIST_MODULES = [
    "hbase-assembly",
    "hbase-archetypes",
]

def is_blacklisted(module_path):
    for bad in BLACKLIST_MODULES:
        if module_path == bad or module_path.startswith(bad + "/"):
            return True
    return False

def find_module_for_file(repo, filepath):
    """Find the Maven module (directory with pom.xml) for a given file."""
    current_dir = os.path.dirname(filepath) if filepath else ""
    
    while current_dir:
        pom_path = os.path.join(repo, current_dir, "pom.xml")
        if os.path.exists(pom_path):
            if not is_blacklisted(current_dir):
                return current_dir
            else:
                return None  # Blacklisted module
        parent = os.path.dirname(current_dir)
        if parent == current_dir:
            break
        current_dir = parent
    
    return None

def extract_test_class(filepath):
    """Extract the fully qualified test class name from a test file path."""
    if "/src/test/java/" not in filepath:
        return None
    
    try:
        class_part = filepath.split("/src/test/java/")[1]
        class_name = class_part.replace("/", ".").replace(".java", "")
        return class_name
    except:
        return None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="Path to the git repository")
    parser.add_argument("--commit", help="Commit hash to analyze")
    parser.add_argument("--files-json", help="JSON array of [status, filepath] pairs (skips git)")
    args = parser.parse_args()

    if not args.commit and not args.files_json:
        print(json.dumps({"modified": [], "added": []}))
        return

    modified_tests = set()
    added_tests = set()

    if args.files_json:
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
        cmd = ["git", "diff-tree", "--no-commit-id", "--name-status", "-r", args.commit]
        try:
            output = subprocess.check_output(cmd, cwd=args.repo, text=True)
        except subprocess.CalledProcessError:
            print(json.dumps({"modified": [], "added": []}))
            return

        entries = []
        for line in output.strip().splitlines():
            parts = line.split('\t')
            if not parts:
                continue
            status = parts[0]
            if status.startswith('R') or status.startswith('C'):
                filepath = parts[2] if len(parts) >= 3 else None
            else:
                filepath = parts[1] if len(parts) >= 2 else None
            if filepath:
                entries.append((status[0], filepath))

    for status, filepath in entries:
        filename = os.path.basename(filepath)
        is_test_file = (
            "/src/test/java/" in filepath and
            filepath.endswith(".java") and
            (filename.startswith("Test") or filename.endswith("Test.java") or
             filename.endswith("Tests.java") or filename.endswith("IT.java"))
        )
        if not is_test_file:
            continue

        module = find_module_for_file(args.repo, filepath)
        if not module:
            continue

        class_name = extract_test_class(filepath)
        test_target = f"{module}:{class_name}" if class_name else module

        if status == 'A':
            added_tests.add(test_target)
        else:
            modified_tests.add(test_target)

    result = {
        "modified": sorted(list(modified_tests)),
        "added": sorted(list(added_tests))
    }
    print(json.dumps(result))

if __name__ == "__main__":
    main()
