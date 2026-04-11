import re
from typing import List, Dict, Any

def parse_unified_diff(diff_text: str) -> List[Dict[str, Any]]:
    """
    Parses a unified diff into a list of hunks.
    Extracts the old_content (lines removed or retained) which is the source anchor
    we use for searching the target repository.

    Detects new-file hunks (--- /dev/null) and sets is_new_file=True.
    Detects deleted-file hunks (+++ /dev/null) and sets is_del_file=True.
    Detects renamed-file hunks (rename from / rename to lines) and sets is_rename=True
    with old_file_path populated.  For pure renames (100% similarity, no @@ section)
    a sentinel hunk with empty old/new_content is emitted so downstream agents can
    execute the filesystem rename even when the content is unchanged.
    """
    hunks = []
    current_file = None
    current_hunk = None
    is_new_file = False
    is_del_file = False
    is_rename = False
    rename_from_path = None
    rename_to_path = None
    # True once we see "rename from/to" but before any @@ hunk — used to emit a
    # sentinel for pure renames that have no @@ body at all.
    pending_pure_rename = False

    def _emit_pure_rename_sentinel():
        """Emit a sentinel hunk for a rename that had no @@ content section."""
        if pending_pure_rename and rename_from_path and rename_to_path:
            hunks.append({
                "file_path": rename_to_path,
                "old_file_path": rename_from_path,
                "old_content": "",
                "new_content": "",
                "is_new_file": False,
                "is_del_file": False,
                "is_rename": True,
                "file_operation": "RENAMED",
            })

    lines = diff_text.splitlines()
    for line in lines:
        if line.startswith("diff --git"):
            # Before resetting, flush any pending pure-rename sentinel.
            _emit_pure_rename_sentinel()

            if current_hunk:
                hunks.append(current_hunk)
                current_hunk = None

            # Reset per-file state.
            current_file = None
            is_new_file = False
            is_del_file = False
            is_rename = False
            rename_from_path = None
            rename_to_path = None
            pending_pure_rename = False

        elif line.startswith("rename from "):
            rename_from_path = line[len("rename from "):].strip()
            is_rename = True
            pending_pure_rename = True  # may be cancelled when we see @@

        elif line.startswith("rename to "):
            rename_to_path = line[len("rename to "):].strip()
            # Use the rename-to path as the canonical target file path.
            current_file = rename_to_path

        elif line.startswith("--- "):
            src = line[4:].split("\t")[0].strip()
            # Strip a/ prefix
            if src.startswith("a/"):
                src = src[2:]
            if src == "/dev/null" or src == "dev/null":
                is_new_file = True
            else:
                is_new_file = False
                # For non-rename diffs, --- gives us the source file.
                # For rename diffs, current_file is already set from "rename to".
                if not is_rename:
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
                # +++ always wins as the canonical target (overrides --- for non-null paths).
                current_file = tgt

        elif line.startswith("@@ "):
            # A content hunk is present — this is NOT a pure rename.
            pending_pure_rename = False

            if current_hunk:
                hunks.append(current_hunk)

            file_op = "MODIFIED"
            if is_new_file:
                file_op = "ADDED"
            elif is_del_file:
                file_op = "DELETED"
            elif is_rename:
                file_op = "RENAMED"

            current_hunk = {
                "file_path": current_file,
                "old_file_path": rename_from_path if is_rename else None,
                "old_content": "",
                "new_content": "",
                "is_new_file": is_new_file,
                "is_del_file": is_del_file,
                "is_rename": is_rename,
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

    # Flush any pending state at end-of-input.
    _emit_pure_rename_sentinel()
    if current_hunk:
        hunks.append(current_hunk)

    return hunks
