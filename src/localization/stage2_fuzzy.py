from typing import Optional, Dict, Any
from rapidfuzz import fuzz
from src.core.state import LocalizationResult

def run_fuzzy_localization(repo_path: str, file_path: str, hunk: Dict[str, Any]) -> Optional[LocalizationResult]:
    """
    Stage 2: Fuzzy text matching (<1s per file)
    Uses RapidFuzz token_sort_ratio (0.75 threshold) for whitespace/reformatting drift.
    Resolves TYPE II patches.
    """
    try:
        with open(f"{repo_path}/{file_path}", 'r') as f:
            lines = f.readlines()
            
        old_content = hunk.get("old_content", "")
        old_content_lines = old_content.splitlines()
        
        if not old_content_lines:
            return None
            
        window_size = len(old_content_lines)
        best_ratio = 0.0
        best_start = -1
        
        # Simple sliding window
        for i in range(len(lines) - window_size + 1):
            window_text = "".join(lines[i:i+window_size])
            ratio = fuzz.token_sort_ratio(old_content, window_text) / 100.0
            
            if ratio > best_ratio:
                best_ratio = ratio
                best_start = i
                
        if best_ratio >= 0.75:
            return LocalizationResult(
                method_used="fuzzy",
                confidence=best_ratio,
                context_snapshot="".join(lines[best_start:best_start+window_size]),
                symbol_mappings={},
                file_path=file_path,
                start_line=best_start + 1,
                end_line=best_start + window_size
            )
            
    except FileNotFoundError:
        pass
        
    return None
