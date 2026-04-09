import os
import sys
import subprocess

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.core.state import BackportState
from src.localization.stage1_git import run_git_localization
from src.localization.stage2_fuzzy import run_fuzzy_localization
from src.localization.stage3_gumtree import run_gumtree_localization
from src.localization.stage4_javaparser import run_javaparser_localization
from src.localization.stage5_embedding import run_embedding_localization
from src.tools.patch_parser import parse_unified_diff

def test_hunk(repo_path, file_path, hunk):
    print(f"    Stage 1: Git", flush=True)
    res = run_git_localization(repo_path, file_path, hunk)
    if res: return res
    print(f"    Stage 2: Fuzzy", flush=True)
    res = run_fuzzy_localization(repo_path, file_path, hunk)
    if res: return res
    print(f"    Stage 3: Gumtree", flush=True)
    res = run_gumtree_localization(repo_path, file_path, hunk)
    if res: return res
    print(f"    Stage 4: JavaParser", flush=True)
    res = run_javaparser_localization(repo_path, file_path, hunk)
    if res: return res
    print(f"    Stage 5: Embedding", flush=True)
    res = run_embedding_localization(repo_path, file_path, hunk)
    return res

def run_evaluation():
    repo_path = "repos/elasticsearch"
    commit_hash = "da51c8ccbf0171abefb2978810efd6db0f6f743e"
    cmd = ["git", "-C", repo_path, "show", commit_hash]
    patch_content = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
    from src.tools.patch_parser import parse_unified_diff
    hunks = parse_unified_diff(patch_content)
    java_hunks = [h for h in hunks if h["file_path"] and h["file_path"].endswith(".java")]
    
    for i, h in enumerate(java_hunks):
        print(f"Hunk {i+1}/{len(java_hunks)} in {h['file_path']}", flush=True)
        res = test_hunk(repo_path, h['file_path'], h)
        if res:
            print(f"  -> SUCCESS: {res.method_used}")
        else:
            print(f"  -> FAILED")

if __name__ == "__main__":
    run_evaluation()
