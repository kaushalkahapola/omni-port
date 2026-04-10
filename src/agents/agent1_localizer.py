from typing import List, Dict, Any
from src.core.state import BackportState, LocalizationResult
from src.localization.stage0_hierarchy import run_hierarchy_file_redirect
from src.localization.stage1_git import run_git_localization
from src.localization.stage2_fuzzy import run_fuzzy_localization
from src.localization.stage3_gumtree import run_gumtree_localization
from src.localization.stage4_javaparser import run_javaparser_localization
from src.localization.stage5_embedding import run_embedding_localization

def localizer_pipeline(repo_path: str, file_path: str, hunk: Dict[str, Any]) -> LocalizationResult:
    """
    The 5-stage hybrid code localization pipeline.
    Ordered by computational cost and strictness.

    Stage 0 (pre-pass): Class hierarchy file redirect — if the hunk's methods
    are defined in a parent/abstract class in the target branch, find that file
    first before running Stages 1–5 on the wrong file.
    """
    # Stage 0: Hierarchy-aware file redirect (graceful: falls back to file_path
    # if Java microservice is unavailable or no redirect is needed)
    canonical_file = run_hierarchy_file_redirect(repo_path, file_path, hunk) or file_path

    # Stage 1
    res = run_git_localization(repo_path, canonical_file, hunk)
    if res: return res

    # Stage 2
    res = run_fuzzy_localization(repo_path, canonical_file, hunk)
    if res: return res

    # Stage 3
    res = run_gumtree_localization(repo_path, canonical_file, hunk)
    if res: return res

    # Stage 4
    res = run_javaparser_localization(repo_path, canonical_file, hunk)
    if res: return res

    # Stage 5
    res = run_embedding_localization(repo_path, canonical_file, hunk)
    if res: return res

    # Fallback failure
    return LocalizationResult(
        method_used="failed",
        confidence=0.0,
        context_snapshot="",
        file_path=canonical_file,
        start_line=0,
        end_line=0
    )

def localize_hunks(state: BackportState) -> BackportState:
    """
    Agent 1: Code Localizer
    Executes the 5-stage hybrid localization per-hunk, per-file.
    Outputs LocalizationResult.
    """
    repo_path = state["target_repo_path"]
    
    results = []
    for hunk in state.get("hunks", []):
        file_path = hunk.get("file_path", "")
        if file_path:
            loc_result = localizer_pipeline(repo_path, file_path, hunk)
            results.append(loc_result)
            
    state["localization_results"] = results
    return state
