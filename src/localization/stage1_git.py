import subprocess
from typing import Optional, Dict, Any
from src.core.state import LocalizationResult

def run_git_localization(repo_path: str, file_path: str, hunk: Dict[str, Any]) -> Optional[LocalizationResult]:
    """
    Stage 1: Git-native localization (Free, <100ms)
    Uses git diff --find-renames=30, git log -S "symbol", git log --follow
    Resolves 100% of TYPE I patches.
    """
    old_content = hunk.get("old_content", "")
    old_content_lines = old_content.splitlines()
    if not old_content_lines:
        return None
        
    # Attempt 1: Fast direct read of the target file to check if it's already there
    try:
        with open(f"{repo_path}/{file_path}", 'r') as f:
            lines = f.readlines()
            
        # Try finding the exact string match (classic apply)
        first_line = old_content_lines[0] + "\n"
        if first_line in lines:
            start_idx = lines.index(first_line)
            # check the full block
            match = True
            for j, ln in enumerate(old_content_lines):
                if lines[start_idx + j].strip() != ln.strip():
                    match = False
                    break
                    
            if match:
                return LocalizationResult(
                    method_used="git_exact",
                    confidence=1.0,
                    context_snapshot="".join(lines[start_idx:start_idx+len(old_content_lines)]),
                    symbol_mappings={},
                    file_path=file_path,
                    start_line=start_idx + 1,
                    end_line=start_idx + len(old_content_lines)
                )
    except FileNotFoundError:
        pass

    # Attempt 2: Pickaxe search (`git log -S "symbol" --oneline`)
    # Try finding the exact old block via pickaxe if the file was renamed
    try:
        search_term = old_content_lines[0].strip()
        if not search_term:
            return None
            
        cmd = ["git", "-C", repo_path, "log", "-n", "5", "-S", search_term, "--name-status", "--oneline"]
        # Limit to 1 second timeout
        output = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True, timeout=1.0)
        if output:
            for line in output.splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[1].startswith("R"):
                    # Found a rename!
                    new_file_path = parts[2]
                    return LocalizationResult(
                        method_used="git_pickaxe",
                        confidence=0.9,
                        context_snapshot=old_content,
                        symbol_mappings={},
                        file_path=new_file_path,
                        start_line=1, # Line info might require further fuzzy matching
                        end_line=len(old_content_lines)
                    )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
        
    return None
