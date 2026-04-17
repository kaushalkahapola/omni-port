#!/usr/bin/env python3
import argparse
import subprocess
import sys
import os
import json

def find_gradle_module(repo, filepath):
    """
    Finds the Gradle module path (e.g. :core or :connect:runtime) for a given file.
    Walks up the directory tree looking for build.gradle.
    """
    current_dir = os.path.dirname(filepath)
    
    while current_dir:
        # Check if build.gradle exists in this directory
        build_gradle_path = os.path.join(repo, current_dir, "build.gradle")
        
        if os.path.exists(build_gradle_path):
            # Found it!
            # Convert the relative directory path to Gradle project path
            # Ensure we use forward slashes for Gradle path logic
            normalized_dir = current_dir.replace("\\", "/")
            return ":" + normalized_dir.replace("/", ":")
            
        # Move up one level
        parent = os.path.dirname(current_dir)
        if parent == current_dir: # Safety check
            break
        current_dir = parent
        
    # Check root if we haven't found anything yet
    if os.path.exists(os.path.join(repo, "build.gradle")):
        return ":"
        
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
            "/src/test/" in filepath and
            filepath.endswith(".java") and
            (filename.endswith("Tests.java") or filename.endswith("Test.java") or filename.endswith("IT.java"))
        )
        if not is_test_file:
            continue

        module_path = find_gradle_module(args.repo, filepath)
        if not module_path:
            continue

        if module_path == ":":
            module_path = ""

        test_target = ""
        try:
            if "/src/test/java/" in filepath:
                rel_path = filepath.split("/src/test/java/")[1]
                class_name = rel_path.replace("/", ".").replace("\\", ".").rsplit(".", 1)[0]
                task_name = "test"
                test_target = f"{module_path}:{task_name} --tests {class_name}"
            else:
                test_target = f"{module_path}:test"
        except IndexError:
            test_target = f"{module_path}:test"

        if test_target:
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