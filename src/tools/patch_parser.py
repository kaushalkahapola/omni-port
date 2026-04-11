import re
from typing import List, Dict, Any

def parse_unified_diff(diff_text: str) -> List[Dict[str, Any]]:
    """
    Parses a unified diff into a list of hunks.
    Extracts the old_content (lines removed or retained) which is the source anchor
    we use for searching the target repository.

    Detects new-file hunks (--- /dev/null) and sets is_new_file=True.
    Detects deleted-file hunks (+++ /dev/null) and sets is_del_file=True.
    """
    hunks = []
    current_file = None
    current_hunk = None
    is_new_file = False
    is_del_file = False

    lines = diff_text.splitlines()
    for line in lines:
        if line.startswith("diff --git"):
            # reset file state
            current_file = None
            is_new_file = False
            is_del_file = False
        elif line.startswith("--- "):
            src = line[4:].split("\t")[0].strip()
            # Strip a/ prefix
            if src.startswith("a/"):
                src = src[2:]
            if src == "/dev/null" or src == "dev/null":
                is_new_file = True
            else:
                is_new_file = False
                current_file = src
        elif line.startswith("+++ "):
            tgt = line[4:].split("\t")[0].strip()
            # Strip b/ prefix
            if tgt.startswith("b/"):
                tgt = tgt[2:]
            if tgt == "/dev/null" or tgt == "dev/null":
                is_del_file = True
            else:
                is_del_file = False
                current_file = tgt  # prefer +++ path as the canonical target
        elif line.startswith("@@ "):
            if current_hunk:
                hunks.append(current_hunk)

            file_op = "MODIFIED"
            if is_new_file:
                file_op = "ADDED"
            elif is_del_file:
                file_op = "DELETED"

            # Start new hunk
            current_hunk = {
                "file_path": current_file,
                "old_content": "",
                "new_content": "",
                "is_new_file": is_new_file,
                "is_del_file": is_del_file,
                "file_operation": file_op,
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
