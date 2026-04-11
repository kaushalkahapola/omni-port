import os
import subprocess
from typing import Optional, Dict, Any
from rapidfuzz import fuzz
from src.core.state import LocalizationResult


def run_git_localization(repo_path: str, file_path: str, hunk: Dict[str, Any]) -> Optional[LocalizationResult]:
    """
    Stage 1: Git-native localization (Free, <100ms)
    Two sub-methods in order:
      1. Direct exact-string match against current file content
      2. File rename detection (git log --follow --diff-filter=R)

    Resolves 100% of TYPE I patches.

    Returns ONLY exact-match results (no strip-comparison).
    Whitespace-drifted matches are left for Stage 2 (fuzzy).

    NOTE: git log -S pickaxe search was trialled here but removed because
    it returns line numbers from historical diffs (stale), not the current
    file state. Hunks not found exactly here correctly fall through to
    Stage 2 (fuzzy) which finds the current location reliably.
    """
    old_content = hunk.get("old_content", "")
    old_content_lines = old_content.splitlines()
    if not old_content_lines:
        return None

    # ── Attempt 1: Direct file read — exact-string match ─────────────────────
    try:
        with open(f"{repo_path}/{file_path}", "r") as f:
            lines = f.readlines()

        first_line = old_content_lines[0] + "\n"
        # Scan ALL occurrences of the first line, not just the first.
        candidate_starts = [i for i, l in enumerate(lines) if l == first_line]

        for start_idx in candidate_starts:
            # Guard against running off the end of the file.
            if start_idx + len(old_content_lines) > len(lines):
                continue

            # Exact comparison — no stripping; whitespace must match.
            match = all(
                lines[start_idx + j] == (old_content_lines[j] + "\n")
                for j in range(len(old_content_lines))
            )
            if match:
                return LocalizationResult(
                    method_used="git_exact",
                    confidence=1.0,
                    context_snapshot="".join(lines[start_idx: start_idx + len(old_content_lines)]),
                    symbol_mappings={},
                    file_path=file_path,
                    start_line=start_idx + 1,
                    end_line=start_idx + len(old_content_lines),
                )
    except FileNotFoundError:
        pass

    # ── Attempt 2: Rename detection ───────────────────────────────────────────
    # Follow renames for THIS specific file path to find its new location.
    # Uses --follow --diff-filter=R so we only get renames of the exact file,
    # not unrelated renames from a global content search.
    try:
        cmd = [
            "git", "-C", repo_path,
            "log", "-n", "10", "--diff-filter=R",
            "--name-status", "--oneline", "--follow", "--", file_path,
        ]
        output = subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL, text=True, timeout=2.0
        )
        for line in output.splitlines():
            parts = line.split("\t")
            # --name-status lines for renames look like: "R100\told_path\tnew_path"
            if len(parts) == 3 and parts[0].startswith("R"):
                new_file_path = parts[2].strip()

                # Found the renamed file. Now locate the actual position within it.
                # Returning start_line=1 / context_snapshot=old_content (mainline) would
                # give Agent 4 wrong context and Agent 6 a bogus expansion anchor.
                try:
                    with open(f"{repo_path}/{new_file_path}", "r") as nf:
                        new_lines = nf.readlines()

                    window_size = len(old_content_lines)

                    # Attempt exact match first.
                    first_line = old_content_lines[0] + "\n"
                    candidate_starts = [i for i, l in enumerate(new_lines) if l == first_line]
                    for start_idx in candidate_starts:
                        if start_idx + window_size > len(new_lines):
                            continue
                        if all(
                            new_lines[start_idx + j] == (old_content_lines[j] + "\n")
                            for j in range(window_size)
                        ):
                            return LocalizationResult(
                                method_used="git_exact",
                                confidence=1.0,
                                context_snapshot="".join(
                                    new_lines[start_idx : start_idx + window_size]
                                ),
                                symbol_mappings={},
                                file_path=new_file_path,
                                start_line=start_idx + 1,
                                end_line=start_idx + window_size,
                            )

                    # Exact match failed. Return a sentinel result so the pipeline
                    # updates the canonical file path and continues to stage 2.
                    return LocalizationResult(
                        method_used="path_redirect",
                        confidence=0.0,
                        context_snapshot="",
                        symbol_mappings={},
                        file_path=new_file_path,
                        start_line=0,
                        end_line=0,
                    )

                except (FileNotFoundError, IOError):
                    pass

                # Fallback: file unreadable. Return None to let subsequent stages try on original path.
                return None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # ── Attempt 3: Content-based cross-repo search ────────────────────────────
    # If the source file doesn't exist in the target branch AND wasn't renamed
    # within this branch's history (e.g. the mainline split a class into a new
    # file that never existed in the target), grep the repo's Java source tree
    # for the most distinctive lines from old_content to find the real file.
    if not os.path.exists(f"{repo_path}/{file_path}"):
        result = _grep_for_content_in_repo(repo_path, file_path, old_content, old_content_lines)
        if result:
            return result

    return None


def _grep_for_content_in_repo(
    repo_path: str,
    original_file_path: str,
    old_content: str,
    old_content_lines: list,
) -> Optional[LocalizationResult]:
    """
    When the source file doesn't exist in the target repo at all, search all
    Java files in src/main/java for the most distinctive non-trivial lines from
    old_content. Returns the file with the best fuzzy match, or None.
    """
    # Find the src/main/java root relative to the original file path.
    parts = original_file_path.replace("\\", "/").split("/")
    src_root = None
    for i, part in enumerate(parts):
        if part == "src" and i + 2 < len(parts) and parts[i+1] == "main" and parts[i+2] == "java":
            src_root = os.path.join(repo_path, "/".join(parts[:i+3]))
            break
    if not src_root or not os.path.isdir(src_root):
        return None

    # Pick the most distinctive lines: non-trivial, non-boilerplate, not too short.
    _SKIP = {"", "{", "}", "};", "});", ");", "*/", "/*", "//"}
    candidate_lines = [
        l.strip() for l in old_content_lines
        if len(l.strip()) > 6 and l.strip() not in _SKIP
        and not l.strip().startswith("//")
        and not l.strip().startswith("*")
        and not l.strip().startswith("@")
        and l.strip() not in {"import", "package"}
    ]
    if not candidate_lines:
        return None

    # Prefer lines that are more unique (longer and less likely to be generic).
    # Sort by length descending, pick the top candidates for grepping.
    candidate_lines.sort(key=lambda l: len(l), reverse=True)

    # Grep for up to 5 distinctive lines and collect file scores.
    file_scores: Dict[str, int] = {}
    for probe in candidate_lines[:5]:
        try:
            result = subprocess.run(
                ["grep", "-rl", "--include=*.java", probe, "."],
                capture_output=True, text=True, cwd=src_root, timeout=5.0,
            )
            for rel in result.stdout.strip().splitlines():
                abs_path = os.path.join(src_root, rel.lstrip("./"))
                rel_path = os.path.relpath(abs_path, repo_path)
                file_scores[rel_path] = file_scores.get(rel_path, 0) + 1
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    if not file_scores:
        return None

    # Score each candidate file with a fuzzy sliding window over old_content.
    # Boost files that share the same package directory AND have a similar name
    # to the original file, so a generic signature match in a different file
    # doesn't beat the correct target.
    original_dir = os.path.dirname(original_file_path).replace("\\", "/")
    original_stem = os.path.basename(original_file_path).replace(".java", "").lower()
    window_size = len(old_content_lines)
    best_ratio = 0.0
    best_result = None

    # Sort by hit count descending; check top 5 candidates.
    top_files = sorted(file_scores, key=file_scores.get, reverse=True)[:5]

    # Always include same-package Java files even if not in the top grep hits.
    same_pkg_dir = os.path.join(repo_path, original_dir)
    if os.path.isdir(same_pkg_dir):
        for fname in os.listdir(same_pkg_dir):
            if fname.endswith(".java"):
                rel = os.path.join(original_dir, fname)
                if rel not in top_files:
                    top_files.append(rel)

    for rel_path in top_files:
        try:
            with open(os.path.join(repo_path, rel_path), "r") as f:
                new_lines = f.readlines()
        except (FileNotFoundError, IOError):
            continue

        if len(new_lines) < window_size:
            continue

        for i in range(len(new_lines) - window_size + 1):
            window = "".join(new_lines[i : i + window_size])
            ratio = fuzz.token_sort_ratio(old_content, window) / 100.0
            # Boost files in the same package directory.
            file_dir = os.path.dirname(rel_path).replace("\\", "/")
            if file_dir == original_dir:
                ratio = min(1.0, ratio + 0.15)
            # Boost files whose name is similar to the original file name.
            candidate_stem = os.path.basename(rel_path).replace(".java", "").lower()
            name_sim = fuzz.partial_ratio(original_stem, candidate_stem) / 100.0
            if name_sim >= 0.5:
                ratio = min(1.0, ratio + name_sim * 0.10)
            if ratio > best_ratio:
                best_ratio = ratio
                best_result = LocalizationResult(
                    method_used="path_redirect",
                    confidence=0.0,
                    context_snapshot="",
                    symbol_mappings={},
                    file_path=rel_path,
                    start_line=0,
                    end_line=0,
                )

    if best_result and best_ratio >= 0.5:
        return best_result

    return None
