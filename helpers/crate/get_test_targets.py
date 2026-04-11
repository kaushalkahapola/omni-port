#!/usr/bin/env python3
"""
Extract Maven test targets for CrateDB modified test files.
"""

import sys
import os
import subprocess
import json
import argparse
import xml.etree.ElementTree as ET


def find_module_for_file(file_path, repo_dir):
    """Find the Maven module containing the given file."""
    # Navigate up from the file to find pom.xml
    current_dir = os.path.dirname(os.path.join(repo_dir, file_path))
    
    while current_dir.startswith(repo_dir):
        pom_path = os.path.join(current_dir, 'pom.xml')
        if os.path.exists(pom_path):
            # Found a module with pom.xml
            # Get relative path from repo root
            module_path = os.path.relpath(current_dir, repo_dir)
            if module_path == '.':
                return None  # Root module
            return module_path
        
        parent_dir = os.path.dirname(current_dir)
        if parent_dir == current_dir:
            break
        current_dir = parent_dir
    
    return None


def extract_test_class_name(file_path):
    """Extract the test class name from the Java file path."""
    # Get filename without .java extension
    filename = os.path.basename(file_path)
    if filename.endswith('.java'):
        return filename[:-5]
    return filename


def get_modified_test_files(repo_dir, commit_sha=None, use_worktree=False):
    """Get modified test files from commit or worktree."""
    try:
        if use_worktree:
            cmd = "git diff --name-only --diff-filter=M"
        else:
            cmd = f"git diff-tree --no-commit-id --name-only --diff-filter=M -r {commit_sha}"
        
        result = subprocess.run(
            cmd,
            shell=True, cwd=repo_dir, capture_output=True, text=True, check=True
        )
        all_modified = [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]
        
        # Filter for test files
        test_files = []
        for f in all_modified:
            if 'test' in f.lower() and f.endswith('.java'):
                test_files.append(f)
        
        return test_files
    except:
        return []


def get_added_test_files(repo_dir, commit_sha=None, use_worktree=False):
    """Get added test files from commit or worktree."""
    try:
        if use_worktree:
            cmd = "git diff --name-only --diff-filter=A"
        else:
            cmd = f"git diff-tree --no-commit-id --name-only --diff-filter=A -r {commit_sha}"
            
        result = subprocess.run(
            cmd,
            shell=True, cwd=repo_dir, capture_output=True, text=True, check=True
        )
        all_added = [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]
        
        # Filter for test files
        test_files = []
        for f in all_added:
            if 'test' in f.lower() and f.endswith('.java'):
                test_files.append(f)
        
        return test_files
    except:
        return []


def main():
    parser = argparse.ArgumentParser(description="Extract Maven test targets")
    parser.add_argument("--repo", required=True, help="Path to the repository")
    parser.add_argument("--commit", help="Commit SHA to examine")
    parser.add_argument("--worktree", action="store_true", help="Analyze uncommitted changes")
    args = parser.parse_args()

    if not args.commit and not args.worktree:
        print("Usage: get_test_targets.py --repo <repo_dir> [--commit <commit_sha> | --worktree]", file=sys.stderr)
        sys.exit(1)
    
    repo_dir = args.repo
    
    # Get modified and added test files
    modified_test_files = get_modified_test_files(repo_dir, commit_sha=args.commit, use_worktree=args.worktree)
    added_test_files = get_added_test_files(repo_dir, commit_sha=args.commit, use_worktree=args.worktree)
    
    modified_targets = []
    added_targets = []
    source_modules = set()
    all_modules = set()
    
    # Process modified test files
    for test_file in modified_test_files:
        module = find_module_for_file(test_file, repo_dir)
        test_class = extract_test_class_name(test_file)
        
        if module:
            all_modules.add(module)
            target = f"{module}:{test_class}"
            modified_targets.append(target)
            
    # Process added test files
    for test_file in added_test_files:
        module = find_module_for_file(test_file, repo_dir)
        test_class = extract_test_class_name(test_file)
        
        if module:
            all_modules.add(module)
            target = f"{module}:{test_class}"
            added_targets.append(target)

    # Simple source module detection (heuristic)
    try:
        if args.worktree:
            cmd = "git diff --name-only"
        else:
            cmd = f"git diff-tree --no-commit-id --name-only -r {args.commit}"
            
        result = subprocess.run(
            cmd,
            shell=True, cwd=repo_dir, capture_output=True, text=True, check=True
        )
        all_changed = [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]
        for f in all_changed:
            if f.endswith(".java") and "src/main/java/" in f:
                mod = find_module_for_file(f, repo_dir)
                if mod:
                    source_modules.add(mod)
                    all_modules.add(mod)
    except:
        pass
    
    # Return JSON format
    result = {
        "modified": sorted(list(modified_targets)),
        "added": sorted(list(added_targets)),
        "source_modules": sorted(list(source_modules)),
        "all_modules": sorted(list(all_modules))
    }
    
    print(json.dumps(result))


if __name__ == '__main__':
    main()
