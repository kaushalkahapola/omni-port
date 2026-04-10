from typing import Optional, Dict, Any
from src.core.state import LocalizationResult
from src.tools.java_client import get_java_client

def run_gumtree_localization(repo_path: str, file_path: str, hunk: Dict[str, Any]) -> Optional[LocalizationResult]:
    """
    Stage 3: AST structural matching via GumTree (~2-5s per file)
    Calls the Java Microservice API using JSON over stdin/stdout.
    """
    client = get_java_client()
    old_content = hunk.get("old_content", "")
    
    # Request the AST Diff from GumTree via Java
    payload = {
        "repo_path": repo_path,
        "file_path": file_path,
        "old_content": old_content
    }
    
    response = client.send_request("gumtree_diff", payload)
    
    if response.get("status") == "ok":
        # Use real values from the microservice response when available.
        symbol_mappings = response.get("symbol_mappings", {})
        start_line = response.get("start_line", 1)
        end_line = response.get("end_line", max(1, len(old_content.splitlines())))
        context_snapshot = response.get("context_snapshot", old_content)
        confidence = float(response.get("confidence", 0.85))

        return LocalizationResult(
            method_used="gumtree_ast",
            confidence=confidence,
            context_snapshot=context_snapshot,
            symbol_mappings=symbol_mappings,
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
        )
        
    return None
