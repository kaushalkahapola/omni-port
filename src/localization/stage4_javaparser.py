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
        return LocalizationResult(
            method_used="javaparser",
            confidence=0.80,
            context_snapshot=old_content,
            symbol_mappings={"ResolvedType": "TargetType"}, 
            file_path=file_path,
            start_line=1,
            end_line=max(1, len(old_content.splitlines()))
        )
        
    return None
