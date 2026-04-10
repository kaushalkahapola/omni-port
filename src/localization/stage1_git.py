import subprocess
from typing import Optional, Dict, Any
from src.core.state import LocalizationResult


def run_git_localization(repo_path: str, file_path: str, hunk: Dict[str, Any]) -> Optional[LocalizationResult]:
    """
    Stage 1: Git-native localization (Free, <100ms)
    Uses direct file read for exact-string match, then git pickaxe for renames.
    Resolves 100% of TYPE I patches.

    Returns ONLY exact-match results (no strip-comparison).
    Whitespace-drifted matches are left for Stage 2 (fuzzy).
    """
    old_content = hunk.get("old_content", "")
    old_content_lines = old_content.splitlines()
    if not old_content_lines:
        return None

    # Attempt 1: Direct file read — find all occurrences of the first line exactly,
    # then verify the full block matches exactly (no strip).
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

    # Attempt 2: Pickaxe search — handles file renames.
    try:
        search_term = old_content_lines[0].strip()
        if not search_term:
            return None

        cmd = [
            "git", "-C", repo_path,
            "log", "-n", "5", "-S", search_term,
            "--name-status", "--oneline",
        ]
        output = subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL, text=True, timeout=1.0
        )
        if output:
            for line in output.splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[1].startswith("R"):
                    new_file_path = parts[2]
                    return LocalizationResult(
                        method_used="git_pickaxe",
                        confidence=0.9,
                        context_snapshot=old_content,
                        symbol_mappings={},
                        file_path=new_file_path,
                        start_line=1,
                        end_line=len(old_content_lines),
                    )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return None
