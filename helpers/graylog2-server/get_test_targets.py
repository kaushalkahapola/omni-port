#!/usr/bin/env python3
import argparse
import subprocess
import sys
import os
import json

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="Path to the git repository")
    parser.add_argument("--commit", help="Commit hash to analyze")
    parser.add_argument("--worktree", action="store_true", help="Analyze current worktree changes instead of a specific commit")
    args = parser.parse_args()

    if not args.commit and not args.worktree:
        print(json.dumps({"modified": [], "added": []}))
        return

    # 1. Get list of changed files with status
    if args.worktree:
        # Get staged and unstaged changes
        cmd = ["git", "status", "--porcelain"]
    else:
        cmd = ["git", "diff-tree", "--no-commit-id", "--name-status", "-r", args.commit]

    try:
        output = subprocess.check_output(cmd, cwd=args.repo, text=True)
    except subprocess.CalledProcessError:
        print(json.dumps({"modified": [], "added": []}))
        return

    modified_tests = set()
    added_tests = set()

    # 2. Analyze changes
    for line in output.strip().splitlines():
        if args.worktree:
            # git status --porcelain output: XY filename
            if len(line) < 4:
                continue
            status_line = line[:2]
            f = line[3:].strip()
            
            # Treat 'A', 'M', 'R', 'C' as relevant
            if 'A' in status_line:
                status = 'A'
            elif 'M' in status_line or 'R' in status_line or 'C' in status_line:
                status = 'M'
            elif '?' in status_line: # Untracked
                status = 'A'
            else:
                continue
        else:
            parts = line.split('\t', 1)
            if len(parts) != 2:
                continue
            status = parts[0]
            f = parts[1]

        # Strict filtering: Only process Test files
        if not f.endswith("Test.java"):
            continue
            
        # Find the Maven module for this file by walking up the tree
        head = f
        module_path = ""
        while head:
            head, tail = os.path.split(head)
            if os.path.exists(os.path.join(args.repo, head, "pom.xml")):
                if head == "":
                    module_path = "" # Root
                else:
                    module_path = head
                break
        
        # If no module found, skip
        if module_path == "" and not os.path.exists(os.path.join(args.repo, "pom.xml")):
             continue

        # Skip ignored modules
        if module_path in ["graylog2-web-interface"]: 
            continue

        # Extract class name
        # Pattern: [module]/src/test/java/[package]/[Class]Test.java
        if "src/test/java/" in f:
            try:
                class_part = f.split("src/test/java/")[1]
                class_name = class_part.replace("/", ".").replace(".java", "")
                
                # Target format: module:class
                target = f"{module_path}:{class_name}" if module_path else class_name
                
                if status == 'A' or status.startswith('A'):
                    added_tests.add(target)
                else:
                    modified_tests.add(target)
            except:
                continue

    # 3. Output JSON
    result = {
        "modified": sorted(list(modified_tests)),
        "added": sorted(list(added_tests))
    }
    print(json.dumps(result))

if __name__ == "__main__":
    main()
