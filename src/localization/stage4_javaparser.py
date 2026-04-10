from typing import Optional, Dict, Any
from src.core.state import LocalizationResult
from src.tools.java_client import get_java_client

def run_javaparser_localization(repo_path: str, file_path: str, hunk: Dict[str, Any]) -> Optional[LocalizationResult]:
    """
    Stage 4: Symbol resolution via JavaParser (<500ms per query)
    Resolves TYPE III/IV patches.
    Calls the Java Microservice API for CombinedTypeSolver logic.
    """
    client = get_java_client()
    old_content = hunk.get("old_content", "")
    
    # Extract naive symbols to send to JavaParser
    symbols = [word for word in old_content.split() if word.isalnum()]
    
    payload = {
        "repo_path": repo_path,
        "file_path": file_path,
        "symbols_to_resolve": symbols
    }
    
    response = client.send_request("javaparser_resolve", payload)
    
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
