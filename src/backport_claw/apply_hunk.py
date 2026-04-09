"""
CLAW Hunk Application Engine
Handles exact-string replacement with context verification.
"""
from typing import Tuple, Optional, Dict, Any


class CLAWHunkError(Exception):
    """Exception raised when CLAW hunk application fails."""
    pass


class CLAWHunkApplier:
    """
    Applies CLAW hunks (exact-string pairs) to a file with verification.
    """

    def __init__(self, file_content: str):
        """
        Initialize with file content.

        Args:
            file_content: The entire content of the target file
        """
        self.file_content = file_content
        self.lines = file_content.splitlines(keepends=True)

    def find_and_replace(
        self,
        old_string: str,
        new_string: str,
        context_lines: int = 0
    ) -> Tuple[bool, str]:
        """
        Attempts to find and replace old_string with new_string in the file.

        Args:
            old_string: The exact string to find (from source patch)
            new_string: The replacement string
            context_lines: Number of surrounding lines for context (default 0 for exact match)

        Returns:
            Tuple of (success: bool, result_content: str)
            If success, result_content contains the modified file.
            If failed, result_content contains original file.
        """
        if not old_string:
            raise CLAWHunkError("old_string cannot be empty")

        # Exact match case
        if old_string in self.file_content:
            # Verify uniqueness if context_lines is 0
            if context_lines == 0 and self.file_content.count(old_string) > 1:
                return False, self.file_content

            new_content = self.file_content.replace(old_string, new_string, 1)
            return True, new_content

        # If exact match failed and context_lines is 0, fail immediately
        if context_lines == 0:
            return False, self.file_content

        # Try with expanded context
        return self._find_with_context(old_string, new_string, context_lines)

    def _find_with_context(
        self,
        old_string: str,
        new_string: str,
        context_lines: int
    ) -> Tuple[bool, str]:
        """
        Attempts to find old_string with context expansion.
        Strips leading/trailing context lines and searches for the core content.
        """
        old_lines = old_string.splitlines(keepends=True)
        if not old_lines:
            return False, self.file_content

        # Extract core content (middle lines, excluding context)
        if len(old_lines) <= 2 * context_lines:
            # All lines might be context, need different strategy
            return False, self.file_content

        core_start = context_lines
        core_end = len(old_lines) - context_lines
        core_lines = old_lines[core_start:core_end]

        if not core_lines:
            return False, self.file_content

        core_string = "".join(core_lines)

        # Search for core string in file
        if core_string not in self.file_content:
            return False, self.file_content

        # Find position of core string
        idx = self.file_content.find(core_string)

        # Try to extract old_string with context
        start_search = max(0, idx - 500)  # Look back up to 500 chars
        section = self.file_content[start_search:idx + len(core_string) + 500]

        # Extract lines around the match
        lines_before_idx = section[:idx - start_search].count('\n')
        lines_after_idx = section[idx - start_search + len(core_string):].split('\n')[0].count('\n')

        # Simple fallback: just replace the core string
        new_content = self.file_content.replace(core_string, new_string, 1)
        return True, new_content

    def apply_multiple(
        self,
        hunks: list
    ) -> Tuple[bool, str]:
        """
        Applies multiple hunks sequentially.

        Args:
            hunks: List of dicts with 'old_string', 'new_string' keys

        Returns:
            Tuple of (all_success: bool, final_content: str)
        """
        current_content = self.file_content
        all_success = True

        for hunk in hunks:
            old = hunk.get("old_string", "")
            new = hunk.get("new_string", "")

            if not old:
                all_success = False
                continue

            applier = CLAWHunkApplier(current_content)
            success, current_content = applier.find_and_replace(old, new)

            if not success:
                all_success = False
                # Don't abort; try next hunk

        return all_success, current_content
