"""
Java import deduplication utility.

When the namespace adapter synthesizes an import hunk, it works from a narrow
fuzzy-localized context window and may include imports that already exist in
the target file outside that window. After CLAW applies old_string → new_string,
those imports become duplicates.

cleanup_java_imports() removes duplicate import lines, preserving order and
keeping the first occurrence of each. It also removes any blank lines that
become consecutive (more than one blank) inside the import block.
"""

from pathlib import Path


def cleanup_java_imports(content: str) -> str:
    """
    Remove duplicate import statements from Java source content.

    Rules:
      - Exact duplicate import/import-static lines are removed (keeps first).
      - Comparison ignores leading/trailing whitespace per line (handles indentation drift).
      - Non-import lines are passed through unchanged.
      - Consecutive blank lines inside the import block are collapsed to one.
    """
    lines = content.splitlines(keepends=True)
    seen_imports: set[str] = set()
    result: list[str] = []
    prev_was_blank_in_import_block = False

    for line in lines:
        stripped = line.strip()

        is_import = stripped.startswith("import ") or stripped.startswith("import static ")

        if is_import:
            if stripped in seen_imports:
                # Skip exact duplicate import
                continue
            seen_imports.add(stripped)
            prev_was_blank_in_import_block = False
            result.append(line)
        elif not stripped and seen_imports:
            # Blank line inside/after import block: collapse consecutive blanks
            if prev_was_blank_in_import_block:
                continue
            prev_was_blank_in_import_block = True
            result.append(line)
        else:
            prev_was_blank_in_import_block = False
            result.append(line)

    return "".join(result)


def cleanup_java_file(file_path: str | Path) -> bool:
    """
    Apply import cleanup to a Java file on disk.
    Returns True if the file was modified, False if already clean or not found.
    """
    path = Path(file_path)
    if not path.exists() or not str(path).endswith(".java"):
        return False
    try:
        original = path.read_text(encoding="utf-8")
        cleaned = cleanup_java_imports(original)
        if cleaned != original:
            path.write_text(cleaned, encoding="utf-8")
            return True
        return False
    except OSError:
        return False
