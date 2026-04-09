import re
from typing import List, Dict, Any

def parse_unified_diff(diff_text: str) -> List[Dict[str, Any]]:
    """
    Parses a unified diff into a list of hunks.
    Extracts the old_content (lines removed or retained) which is the source anchor
    we use for searching the target repository.
    """
    hunks = []
    current_file = None
    current_hunk = None
    
    lines = diff_text.splitlines()
    for line in lines:
        if line.startswith("--- a/"):
            current_file = line[6:]
        elif line.startswith("+++ b/"):
            # Update target file if it was a rename
            current_file = line[6:]
        elif line.startswith("diff --git"):
            # reset file
            current_file = None
        elif line.startswith("@@ "):
            if current_hunk:
                hunks.append(current_hunk)
            
            # Start new hunk
            current_hunk = {
                "file_path": current_file,
                "old_content": "",
                "new_content": ""
            }
        elif current_hunk is not None:
            if line.startswith("-"):
                current_hunk["old_content"] += line[1:] + "\n"
            elif line.startswith("+"):
                current_hunk["new_content"] += line[1:] + "\n"
            elif line.startswith(" "):
                current_hunk["old_content"] += line[1:] + "\n"
                current_hunk["new_content"] += line[1:] + "\n"
                
    if current_hunk:
        hunks.append(current_hunk)
        
    return hunks
