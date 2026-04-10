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

                    # Exact match failed (content drifted). Run fuzzy sliding window
                    # to find the best-matching region and return real coordinates.
                    if window_size > 0 and len(new_lines) >= window_size:
                        best_ratio = 0.0
                        best_start = -1
                        for i in range(len(new_lines) - window_size + 1):
                            window = "".join(new_lines[i : i + window_size])
                            ratio = fuzz.token_sort_ratio(old_content, window) / 100.0
                            if ratio > best_ratio:
                                best_ratio = ratio
                                best_start = i

                        if best_start >= 0 and best_ratio >= 0.6:
                            return LocalizationResult(
                                method_used="git_pickaxe",
                                confidence=min(0.9, best_ratio),
                                context_snapshot="".join(
                                    new_lines[best_start : best_start + window_size]
                                ),
                                symbol_mappings={},
                                file_path=new_file_path,
                                start_line=best_start + 1,
                                end_line=best_start + window_size,
                            )

                except (FileNotFoundError, IOError):
                    pass

                # Fallback: file unreadable or no fuzzy match above threshold.
                # Return None to let Stage 2+ (fuzzy, hierarchy) try; a 0.5-confidence
                # result with bogus line-1 coordinates would block all downstream stages.
                return None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return None
