import os
import re
from typing import List, Dict, Any
from collections import Counter
from src.core.state import BackportState, LocalizationResult
from src.localization.stage0_hierarchy import run_hierarchy_file_redirect
from src.localization.stage1_git import run_git_localization
from src.localization.stage2_fuzzy import run_fuzzy_localization
from src.localization.stage3_gumtree import run_gumtree_localization
from src.localization.stage4_javaparser import run_javaparser_localization
from src.localization.stage5_embedding import run_embedding_localization


# ──────────────────────────────────────────────────────────────────────────────
# Hunk Segregation
# ──────────────────────────────────────────────────────────────────────────────

_TEST_SOURCE_DIRS = (
    "/src/test/java/",
    "/src/internalClusterTest/java/",
    "/src/javaRestTest/java/",
    "/src/yamlRestTest/java/",
    "/src/integTest/java/",
    "/src/integrationTest/java/",
)

_AUTOGEN_PATTERNS = [
    r"lexer\.java$",
    r"parser\.java$",
    r"baselistener\.java$",
    r"listener\.java$",
    r"basevisitor\.java$",
    r"visitor\.java$",
    r"outerclass\.java$",
    r"pb\.java$",
    r"pborbuilder\.java$",
    r"grpc\.java$",
]


def _is_test_file(file_path: str) -> bool:
    p = (file_path or "").replace("\\", "/").lower()
    # Path-based detection
    if any(d in p for d in (d.lower() for d in _TEST_SOURCE_DIRS)):
        return True
    # File-name based: TestFoo.java or FooTest.java / FooTests.java / FooIT.java
    filename = os.path.basename(p)
    return (
        filename.endswith(("test.java", "tests.java", "it.java", "testcase.java"))
        or filename.startswith("test") and filename.endswith(".java")
    )


def _is_auto_generated_java_file(file_path: str) -> bool:
    normalized = (file_path or "").replace("\\", "/").lower()
    for pattern in _AUTOGEN_PATTERNS:
        if re.search(pattern, normalized):
            return True
    return False


def _is_auxiliary_hunk(hunk: Dict[str, Any]) -> bool:
    """
    Returns True if this hunk should bypass the LLM pipeline and go directly
    to the validation stage:
      - Non-Java files (config, XML, build files, etc.)
      - Test Java files
      - Auto-generated Java files (ANTLR, Protobuf, gRPC, etc.)
    """
    file_path = (hunk.get("file_path") or "").replace("\\", "/")
    if not file_path.lower().endswith(".java"):
        return True
    if _is_test_file(file_path):
        return True
    if _is_auto_generated_java_file(file_path):
        return True
    return False


def segregate_hunks(
    hunks: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Splits hunks into:
      - java_code_hunks: non-test, non-auto-generated Java source files → LLM pipeline
      - developer_aux_hunks: test files, non-Java files, auto-generated Java → direct apply

    Each auxiliary hunk gets a `hunk_text` field built from its raw unified diff
    lines (required for git-apply during validation), plus `file_operation` and
    `insertion_line` fields that the validator needs.
    """
    java_code_hunks: List[Dict[str, Any]] = []
    developer_aux_hunks: List[Dict[str, Any]] = []

    for hunk in hunks:
        if _is_auxiliary_hunk(hunk):
            aux = dict(hunk)
            # Build hunk_text for git-apply if not already present
            if not aux.get("hunk_text"):
                aux["hunk_text"] = _build_hunk_text(hunk)
            if not aux.get("file_operation"):
                aux["file_operation"] = "MODIFIED"
            if not aux.get("insertion_line"):
                aux["insertion_line"] = 0
            aux["intent_verified"] = True
            developer_aux_hunks.append(aux)
        else:
            java_code_hunks.append(hunk)

    return java_code_hunks, developer_aux_hunks


def _build_hunk_text(hunk: Dict[str, Any]) -> str:
    """
    Reconstruct a minimal unified diff hunk text from old_content / new_content
    fields when raw hunk_text is not already stored.
    """
    old_lines = (hunk.get("old_content") or "").splitlines(keepends=True)
    new_lines = (hunk.get("new_content") or "").splitlines(keepends=True)

    # Compute a simple diff: lines only in old → removed, only in new → added,
    # shared prefix/suffix lines → context.  For simple hunks this is accurate.
    old_set = set(old_lines)
    new_set = set(new_lines)

    body_lines = []
    for line in old_lines:
        if line not in new_set:
            body_lines.append(f"-{line}" if line.endswith("\n") else f"-{line}\n")
        else:
            body_lines.append(f" {line}" if line.endswith("\n") else f" {line}\n")
    for line in new_lines:
        if line not in old_set:
            body_lines.append(f"+{line}" if line.endswith("\n") else f"+{line}\n")

    src_count = sum(1 for l in body_lines if l.startswith(("-", " ")))
    tgt_count = sum(1 for l in body_lines if l.startswith(("+", " ")))
    header = f"@@ -1,{src_count} +1,{tgt_count} @@\n"
    return header + "".join(body_lines)


def _is_new_file_hunk(hunk: Dict[str, Any]) -> bool:
    """
    Fix G: Return True if this hunk represents a brand-new file creation.

    Since parse_unified_diff only stores file_path (the +++ b/ path) and not the
    --- a/ source path, we detect new-file hunks by the absence of old_content:
    a pure addition has no context lines and no removed lines, so old_content is
    empty (or only whitespace).

    We also require new_content to be non-trivial so that empty hunks don't
    accidentally get routed here.
    """
    old_content = (hunk.get("old_content") or "").strip()
    new_content = (hunk.get("new_content") or "").strip()
    file_path = (hunk.get("file_path") or "").strip()
    # Must have a target file, no old content, and non-empty new content
    return bool(file_path and not old_content and new_content)

    """
    Reconstruct a minimal unified diff hunk text from old_content / new_content
    fields when raw hunk_text is not already stored.
    """
    old_lines = (hunk.get("old_content") or "").splitlines(keepends=True)
    new_lines = (hunk.get("new_content") or "").splitlines(keepends=True)

    # Compute a simple diff: lines only in old → removed, only in new → added,
    # shared prefix/suffix lines → context.  For simple hunks this is accurate.
    old_set = set(old_lines)
    new_set = set(new_lines)

    body_lines = []
    for line in old_lines:
        if line not in new_set:
            body_lines.append(f"-{line}" if line.endswith("\n") else f"-{line}\n")
        else:
            body_lines.append(f" {line}" if line.endswith("\n") else f" {line}\n")
    for line in new_lines:
        if line not in old_set:
            body_lines.append(f"+{line}" if line.endswith("\n") else f"+{line}\n")

    src_count = sum(1 for l in body_lines if l.startswith(("-", " ")))
    tgt_count = sum(1 for l in body_lines if l.startswith(("+", " ")))
    header = f"@@ -1,{src_count} +1,{tgt_count} @@\n"
    return header + "".join(body_lines)

def _is_false_license_header_match(
    repo_path: str, canonical_file: str, res: LocalizationResult, hunk: Dict[str, Any]
) -> bool:
    """
    Returns True when a localization result landed on lines 1–N of a Java file
    that starts with a license comment block, but the hunk's old_content does not
    reference that license text.

    This false positive occurs when a localization stage (typically javaparser)
    can't find a distinctive anchor for a pure-addition hunk and falls back to
    the beginning of the file.  The result is useless and actively harmful:
    Agent 4 will embed new Java code inside the license comment.

    We read the first line of the target file directly to check.
    """
    if res.start_line != 1 or res.end_line > 30:
        return False
    old_content = hunk.get("old_content", "").lstrip()
    # If old_content itself starts with a license comment, this is legitimate.
    if old_content.startswith("/*"):
        return False
    # Read the first line of the actual target file to check for a license header.
    try:
        target_path = f"{repo_path}/{canonical_file}"
        with open(target_path, "r") as f:
            first_line = f.readline().lstrip()
        return first_line.startswith("/*")
    except (FileNotFoundError, IOError):
        return False


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

    def _accept(r: LocalizationResult) -> bool:
        return r is not None and not _is_false_license_header_match(repo_path, canonical_file, r, hunk)

    # Stage 1
    res = run_git_localization(repo_path, canonical_file, hunk)
    if _accept(res): return res

    # Stage 2
    res = run_fuzzy_localization(repo_path, canonical_file, hunk)
    if _accept(res): return res

    # Stage 3
    res = run_gumtree_localization(repo_path, canonical_file, hunk)
    if _accept(res): return res

    # Stage 4
    res = run_javaparser_localization(repo_path, canonical_file, hunk)
    if _accept(res): return res

    # Stage 5
    res = run_embedding_localization(repo_path, canonical_file, hunk)
    if _accept(res): return res

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

        # Re-localize outlier hunks AND entirely-failed hunks using the majority file.
        for i in indices:
            r = results[i]
            if r.file_path == majority_file and r.method_used != "failed":
                continue  # already correct

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

    Step 0: Filter mainline hunks to Java-code-only (strip test/non-Java/auto-gen).
      developer_aux_hunks is pre-populated by the caller from the target patch
      (the actual developer backport). Agent 1 must NOT overwrite it — those aux
      hunks must apply verbatim against the target branch, so they come from the
      developer's real backport, not from the mainline patch.

    Step 1: Execute the 5-stage hybrid localization per-hunk, per-file.
    Fix G: New-file hunks (empty old_content) skip the 5-stage pipeline entirely
    and receive a special LocalizationResult with method_used="new_file".

    Step 2: Apply an inter-hunk consistency check to correct outlier file
    assignments when multiple hunks from the same source file disagree.
    """
    repo_path = state["target_repo_path"]
    all_hunks = state.get("hunks", [])

    # Step 0: Filter mainline hunks — keep only Java production code hunks for the
    # LLM pipeline. developer_aux_hunks (from target_patch) are left untouched.
    java_code_hunks, _ = segregate_hunks(all_hunks)
    state["hunks"] = java_code_hunks
    # developer_aux_hunks pre-populated by caller; do NOT overwrite

    results: List[LocalizationResult] = []
    for hunk in java_code_hunks:
        file_path = hunk.get("file_path", "")
        if not file_path:
            results.append(LocalizationResult(
                method_used="failed",
                confidence=0.0,
                context_snapshot="",
                file_path="",
                start_line=0,
                end_line=0,
            ))
            continue

        # Fix G: new-file hunk — skip 5-stage pipeline, emit special result
        if _is_new_file_hunk(hunk):
            results.append(LocalizationResult(
                method_used="new_file",
                confidence=1.0,
                context_snapshot="",
                file_path=file_path,
                start_line=0,
                end_line=0,
            ))
            continue

        loc_result = localizer_pipeline(repo_path, file_path, hunk)
        results.append(loc_result)

    results = _apply_inter_hunk_consistency(repo_path, java_code_hunks, results)

    state["localization_results"] = results
    return state

    """
    Agent 1: Code Localizer

    Step 0: Filter mainline hunks to Java-code-only (strip test/non-Java/auto-gen).
      developer_aux_hunks is pre-populated by the caller from the target patch
      (the actual developer backport). Agent 1 must NOT overwrite it — those aux
      hunks must apply verbatim against the target branch, so they come from the
      developer's real backport, not from the mainline patch.

    Step 1: Execute the 5-stage hybrid localization per-hunk, per-file.

    Step 2: Apply an inter-hunk consistency check to correct outlier file
    assignments when multiple hunks from the same source file disagree.
    """
    repo_path = state["target_repo_path"]
    all_hunks = state.get("hunks", [])

    # Step 0: Filter mainline hunks — keep only Java production code hunks for the
    # LLM pipeline. developer_aux_hunks (from target_patch) are left untouched.
    java_code_hunks, _ = segregate_hunks(all_hunks)
    state["hunks"] = java_code_hunks
    # developer_aux_hunks pre-populated by caller; do NOT overwrite

    results: List[LocalizationResult] = []
    for hunk in java_code_hunks:
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

    results = _apply_inter_hunk_consistency(repo_path, java_code_hunks, results)

    state["localization_results"] = results
    return state
