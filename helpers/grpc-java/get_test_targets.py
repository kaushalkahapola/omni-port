#!/usr/bin/env python3
"""
Extract Gradle test targets for grpc-java.
"""
import sys
import os
import subprocess
import json

def get_modified_test_files(repo_dir, commit_sha):
    try:
        # Get list of modified files
        result = subprocess.run(
            f"git diff-tree --no-commit-id --name-only --diff-filter=M -r {commit_sha}",
            shell=True, cwd=repo_dir, capture_output=True, text=True, check=True
        )
        files = [f.strip() for f in result.stdout.splitlines() if f.strip()]
        # Filter for Java test files
        return [f for f in files if f.endswith(".java") and ("test" in f.lower() or "Test" in os.path.basename(f))]
    except:
        return []

def get_added_test_files(repo_dir, commit_sha):
    try:
        # Get list of added files
        result = subprocess.run(
            f"git diff-tree --no-commit-id --name-only --diff-filter=A -r {commit_sha}",
            shell=True, cwd=repo_dir, capture_output=True, text=True, check=True
        )
        files = [f.strip() for f in result.stdout.splitlines() if f.strip()]
        return [f for f in files if f.endswith(".java") and ("test" in f.lower() or "Test" in os.path.basename(f))]
    except:
        return []

def file_to_gradle_target(file_path):
    # Convert core/src/test/java/io/grpc/internal/MyTest.java -> core:MyTest
    # The run_tests.sh will then convert core:MyTest -> :grpc-core:test --tests "MyTest"
    parts = file_path.replace("\\", "/").split("/")
    if len(parts) > 1:
        module = parts[0]
        filename = os.path.basename(file_path)
        classname = filename.replace(".java", "")
        return f"{module}:{classname}"
    
    filename = os.path.basename(file_path)
    classname = filename.replace(".java", "")
    return classname

def main():
    repo_dir = None
    commit_sha = None
    use_worktree = False
    
    # Simple manually parsing to avoid complex argparse deps if minimal env
    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == "--repo":
            repo_dir = sys.argv[i+1]
            i += 2
        elif sys.argv[i] == "--commit":
            commit_sha = sys.argv[i+1]
            i += 2
        elif sys.argv[i] == "--worktree":
            use_worktree = True
            i += 1
        else:
            i += 1

    if not repo_dir:
        print(json.dumps({"modified": [], "added": [], "all_targets": "NONE"}))
        return

    modified_files = []
    added_files = []

    if commit_sha:
        modified_files = get_modified_test_files(repo_dir, commit_sha)
        added_files = get_added_test_files(repo_dir, commit_sha)
    elif use_worktree:
        try:
            # Check staged and unstaged changes
            res_m = subprocess.run(
                "git diff --name-only --diff-filter=M",
                shell=True, cwd=repo_dir, capture_output=True, text=True, check=True
            )
            res_a = subprocess.run(
                "git diff --name-only --diff-filter=A",
                shell=True, cwd=repo_dir, capture_output=True, text=True, check=True
            )
            # Also check against HEAD... wait, if strictly worktree changes:
            # But usually we stage changes before calling this.
            # Let's include staged changes too just in case.
            res_ms = subprocess.run(
                "git diff --cached --name-only --diff-filter=M",
                shell=True, cwd=repo_dir, capture_output=True, text=True, check=True
            )
            res_as = subprocess.run(
                "git diff --cached --name-only --diff-filter=A",
                shell=True, cwd=repo_dir, capture_output=True, text=True, check=True
            )
            
            m_files = set(res_m.stdout.splitlines() + res_ms.stdout.splitlines())
            a_files = set(res_a.stdout.splitlines() + res_as.stdout.splitlines())
            
            modified_files = [f.strip() for f in m_files if f.strip() and f.endswith(".java") and ("test" in f.lower() or "Test" in os.path.basename(f))]
            added_files = [f.strip() for f in a_files if f.strip() and f.endswith(".java") and ("test" in f.lower() or "Test" in os.path.basename(f))]
        except:
            pass

    modified_targets = [file_to_gradle_target(f) for f in modified_files]
    added_targets = [file_to_gradle_target(f) for f in added_files]
    
    # Combined string for legacy support
    all_targets_list = sorted(list(set(modified_targets + added_targets)))
    all_targets_str = " ".join(all_targets_list) if all_targets_list else "NONE"

    print(json.dumps({
        "modified": modified_targets,
        "added": added_targets,
        "all_targets": all_targets_str
    }))

if __name__ == "__main__":
    main()
