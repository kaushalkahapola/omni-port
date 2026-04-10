import re
from typing import Optional, Dict, Any, List
from src.core.state import LocalizationResult
from src.tools.java_http_client import javaparser_resolve

# Java keywords to exclude from symbol extraction
_JAVA_KEYWORDS = frozenset({
    "abstract", "assert", "boolean", "break", "byte", "case", "catch", "char",
    "class", "const", "continue", "default", "do", "double", "else", "enum",
    "extends", "final", "finally", "float", "for", "goto", "if", "implements",
    "import", "instanceof", "int", "interface", "long", "native", "new",
    "package", "private", "protected", "public", "return", "short", "static",
    "strictfp", "super", "switch", "synchronized", "this", "throw", "throws",
    "transient", "try", "void", "volatile", "while", "true", "false", "null",
    "var", "record", "sealed", "permits", "yield",
})


def _extract_java_symbols(content: str) -> List[str]:
    """
    Extract meaningful Java identifiers from content, including dotted qualified
    names (e.g., com.example.Foo.method) that naive split misses.
    """
    # Match dotted qualified names and plain identifiers
    tokens = re.findall(r'[a-zA-Z_][a-zA-Z0-9_.]*', content)
    seen = set()
    result = []
    for token in tokens:
        # Strip trailing dots (can happen at end of qualified name)
        token = token.rstrip(".")
        if not token or token in seen:
            continue
        # Skip short tokens and pure keywords
        parts = token.split(".")
        base = parts[-1]  # last segment (e.g., "method" from "com.example.Foo.method")
        if len(base) <= 2 or base.lower() in _JAVA_KEYWORDS:
            continue
        seen.add(token)
        result.append(token)
        # Also add dotted sub-segments if they look like class/package names
        if len(parts) > 1:
            for part in parts:
                if len(part) > 2 and part not in seen and part.lower() not in _JAVA_KEYWORDS:
                    seen.add(part)
                    result.append(part)
    return result


def run_javaparser_localization(repo_path: str, file_path: str, hunk: Dict[str, Any]) -> Optional[LocalizationResult]:
    """
    Stage 4: Symbol resolution via JavaParser (<500ms per query)
    Resolves TYPE III/IV patches.
    Calls the Java microservice via HTTP (Spring Boot on port 8080).
    """
    old_content = hunk.get("old_content", "")

    symbols = _extract_java_symbols(old_content)
    if not symbols:
        return None

    response = javaparser_resolve(repo_path, file_path, symbols)

    if response.get("status") == "ok":
        symbol_mappings = response.get("symbol_mappings", {})
        start_line = response.get("start_line", 1)
        end_line = response.get("end_line", max(1, len(old_content.splitlines())))
        context_snapshot = response.get("context_snapshot", old_content)
        confidence = float(response.get("confidence", 0.80))

        return LocalizationResult(
            method_used="javaparser",
            confidence=confidence,
            context_snapshot=context_snapshot,
            symbol_mappings=symbol_mappings,
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
        )

    return None
