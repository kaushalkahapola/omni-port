import os
import re
import subprocess
from typing import Any, Dict, List, Optional

from src.tools.java_http_client import javaparser_find_method


def _extract_method_names(content: str) -> List[str]:
    """
    Extract declared method names from Java source content.
    Matches: (public|protected|private) [modifiers] returnType methodName(
    """
    pattern = r'(?:public|protected|private)[^;{(]*?\s+([a-zA-Z_]\w*)\s*\('
    return list(set(re.findall(pattern, content)))


def _find_src_main_java(repo_path: str, file_path: str) -> Optional[str]:
    """
    Find the src/main/java root directory for the given file path.
    Handles multi-module layouts (e.g. server/src/main/java/...).
    Returns absolute path or None.
    """
    parts = file_path.replace("\\", "/").split("/")
    for i, part in enumerate(parts):
        if part == "src" and i + 2 < len(parts) and parts[i + 1] == "main" and parts[i + 2] == "java":
            src_rel = "/".join(parts[: i + 3])
            candidate = os.path.join(repo_path, src_rel)
            if os.path.isdir(candidate):
                return candidate
    return None


def _grep_method_definitions(
    repo_path: str, file_path: str, method_names: List[str]
) -> Optional[str]:
    """
    Grep the repo for Java method DECLARATIONS (not calls) matching `method_names`.
    Pattern: (public|protected|private) ... methodName (
    Returns the dominant file path (relative to repo_path) if different from file_path.

    CRITICAL: Only redirects for methods that are NOT declared in the source file.
    If a method exists in the source file, it stays there (no redirect for that method).
    This prevents TYPE-I/II patches from being wrongly redirected to files that happen
    to share the same method name.
    """
    src_root = _find_src_main_java(repo_path, file_path)
    if not src_root or not os.path.isdir(src_root):
        return None

    norm_source = os.path.normpath(file_path)
    abs_source = os.path.join(repo_path, file_path)

    # Step 1: determine which methods are NOT declared in the source file.
    # For methods that ARE in the source file, no redirect is needed.
    methods_to_redirect: List[str] = []
    for name in method_names:
        pattern = r"(public|protected|private)\b.*\b" + re.escape(name) + r"\b"
        try:
            result = subprocess.run(
                ["grep", "-E", "-l", pattern, abs_source],
                capture_output=True, text=True, timeout=2.0,
            )
            if not result.stdout.strip():
                # Method declaration not found in source file → candidate for redirect
                methods_to_redirect.append(name)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    if not methods_to_redirect:
        return None  # All methods are in the source file — no redirect needed

    # Step 2: find which other files declare the missing methods
    file_scores: Dict[str, int] = {}
    for name in methods_to_redirect:
        pattern = r"(public|protected|private)\b.*\b" + re.escape(name) + r"\b"
        try:
            result = subprocess.run(
                ["grep", "-rE", "--include=*.java", "-l", pattern, "."],
                capture_output=True, text=True, cwd=src_root, timeout=5.0,
            )
            for rel_to_src in result.stdout.strip().splitlines():
                abs_path = os.path.join(src_root, rel_to_src.lstrip("./"))
                rel_path = os.path.relpath(abs_path, repo_path)
                if os.path.normpath(rel_path) == norm_source:
                    continue
                file_scores[rel_path] = file_scores.get(rel_path, 0) + 1
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    if not file_scores:
        return None

    dominant = max(file_scores, key=file_scores.get)
    return dominant


def run_hierarchy_file_redirect(
    repo_path: str, file_path: str, hunk: Dict[str, Any]
) -> Optional[str]:
    """
    Stage 0: Class hierarchy file redirector.

    When the mainline patch modifies methods that live in a parent/abstract class
    (or a composed type) in the target branch, detects the correct target file.

    Strategy:
    1. Grep-based method declaration search (fast, no service needed, handles composition)
    2. Java service BFS (catches inheritance chains, generics, multi-module resolution)

    Returns the dominant target file path if it differs from `file_path`, else None.
    """
    old_content = hunk.get("old_content", "")
    method_names = _extract_method_names(old_content)
    if not method_names:
        return None

    # Strategy 1: Python grep for method declarations (handles composition, fast)
    grep_result = _grep_method_definitions(repo_path, file_path, method_names)
    if grep_result:
        return grep_result

    # Strategy 2: Java service BFS up inheritance chain (handles generics/modules)
    response = javaparser_find_method(repo_path, file_path, method_names)
    if response.get("status") != "ok":
        return None

    dominant = response.get("dominant_file_path", "")
    if dominant and os.path.normpath(dominant) != os.path.normpath(file_path):
        return dominant
    return None
