from typing import List, Dict, Any
from collections import Counter
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


def _apply_inter_hunk_consistency(
    repo_path: str,
    hunks: List[Dict[str, Any]],
    results: List[LocalizationResult],
) -> List[LocalizationResult]:
    """
    Inter-hunk consistency check: when multiple hunks from the same source file
    are localized, a majority vote among successful results overrides outliers.

    Example: hunks 0,1,2 all come from ExplainProfilePlan.java. Hunks 1 and 2
    correctly land on ExplainPlan.java; hunk 0 lands on RerouteRetryFailedPlan.java
    because its signature is too generic. The 2-of-3 majority (ExplainPlan.java)
    overrides hunk 0, and stages 1-5 are re-run with the corrected file path.
    """
    # Group hunk indices by their SOURCE file_path.
    source_groups: Dict[str, List[int]] = {}
    for i, hunk in enumerate(hunks):
        src = hunk.get("file_path", "")
        if src:
            source_groups.setdefault(src, []).append(i)

    for src_file, indices in source_groups.items():
        if len(indices) < 2:
            continue  # need at least 2 hunks to have a majority

        # Count target files among successful (non-failed) results.
        target_counts: Counter = Counter()
        for i in indices:
            r = results[i]
            if r.method_used != "failed" and r.file_path:
                target_counts[r.file_path] += 1

        if not target_counts:
            continue

        majority_file, majority_count = target_counts.most_common(1)[0]
        # Only override when majority is strict (> 50% of successful results).
        successful_count = sum(target_counts.values())
        if majority_count <= successful_count / 2:
            continue

        # Re-localize any outlier hunks using the majority file.
        for i in indices:
            r = results[i]
            if r.file_path == majority_file:
                continue  # already correct
            if r.method_used == "failed":
                continue  # failed entirely — don't guess

            hunk = hunks[i]
            new_result = localizer_pipeline(repo_path, majority_file, hunk)
            if new_result.method_used != "failed":
                results[i] = new_result
            else:
                # Re-localization on majority file also failed; keep original but
                # update file_path so downstream agents at least look in the right
                # file (they'll call Agent 4 which can handle the content drift).
                results[i] = LocalizationResult(
                    method_used=r.method_used,
                    confidence=min(r.confidence, 0.5),
                    context_snapshot=r.context_snapshot,
                    symbol_mappings=r.symbol_mappings,
                    file_path=majority_file,
                    start_line=r.start_line,
                    end_line=r.end_line,
                )

    return results


def localize_hunks(state: BackportState) -> BackportState:
    """
    Agent 1: Code Localizer
    Executes the 5-stage hybrid localization per-hunk, per-file, then applies
    an inter-hunk consistency check to correct outlier file assignments when
    multiple hunks from the same source file disagree on the target file.
    """
    repo_path = state["target_repo_path"]
    hunks = state.get("hunks", [])

    results: List[LocalizationResult] = []
    for hunk in hunks:
        file_path = hunk.get("file_path", "")
        if file_path:
            loc_result = localizer_pipeline(repo_path, file_path, hunk)
            results.append(loc_result)
        else:
            results.append(LocalizationResult(
                method_used="failed",
                confidence=0.0,
                context_snapshot="",
                file_path="",
                start_line=0,
                end_line=0,
            ))

    results = _apply_inter_hunk_consistency(repo_path, hunks, results)

    state["localization_results"] = results
    return state
