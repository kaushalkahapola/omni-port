import os
import sys
import subprocess

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.core.state import BackportState
from src.agents.agent2_localizer import localize_hunks
from src.tools.patch_parser import parse_unified_diff

def run_evaluation():
    repo_path = "repos/elasticsearch"
    
    commits = {
        "TYPE-I": "da51c8ccbf0171abefb2978810efd6db0f6f743e",
    }

    for patch_type, commit_hash in commits.items():
        print(f"Evaluating {patch_type} Patch ({commit_hash})", flush=True)
        
        cmd = ["git", "-C", repo_path, "show", commit_hash]
        patch_content = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)

        hunks = parse_unified_diff(patch_content)
        java_hunks = [h for h in hunks if h["file_path"] and h["file_path"].endswith(".java")]
        
        state = BackportState(
            patch_content=patch_content,
            target_repo_path=repo_path,
            target_branch="master",
            worktree_path=None,
            clean_state=True,
            classification=None,
            localization_results=[],
            hunks=java_hunks,
            retry_contexts=[],
            current_attempt=1,
            max_retries=3,
            tokens_used=0,
            wall_clock_time=0.0,
            status="started"
        )
        
        print(f"Running Agent 2: Code Localizer ({len(java_hunks)} Java hunks)...", flush=True)
        state = localize_hunks(state)
        results = state["localization_results"]
        for i, res in enumerate(results):
            print(f"  Hunk {i+1} -> {res.file_path}", flush=True)
            print(f"    Method Used: {res.method_used} (Confidence: {res.confidence:.2f})", flush=True)
            print(f"    Lines: {res.start_line}-{res.end_line}", flush=True)

if __name__ == "__main__":
    run_evaluation()
