from typing import Optional, Dict, Any
from rapidfuzz import fuzz
from src.core.state import LocalizationResult

import re

def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//.*$", "", text, flags=re.MULTILINE)
    return text

def _normalize(text: str) -> str:
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())

def run_fuzzy_localization(repo_path: str, file_path: str, hunk: Dict[str, Any]) -> Optional[LocalizationResult]:
    """
    Stage 2: Fuzzy text matching (<1s per file)
    Uses RapidFuzz ratio (0.75 threshold) for whitespace/reformatting drift.
    Strips comments to ensure code changes are prioritized and prevent generic
    comment blocks from dominating the ratio.
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

        old_norm = _normalize(old_content)
        old_code_only = _normalize(_strip_comments(old_content))
        has_code = len(old_code_only) > 5
        
        # Simple sliding window
        for i in range(len(lines) - window_size + 1):
            window_text = "".join(lines[i:i+window_size])
            window_norm = _normalize(window_text)
            
            ratio = fuzz.ratio(old_norm, window_norm) / 100.0
            
            # Require the semantic code to match well to avoid comment spoofing
            if has_code:
                window_code_only = _normalize(_strip_comments(window_text))
                code_ratio = fuzz.ratio(old_code_only, window_code_only) / 100.0
                ratio = min(ratio, code_ratio + 0.1)
            
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
