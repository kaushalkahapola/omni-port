import json
import pytest
import os
from src.tools.java_client import get_java_client

def test_javaparser_resolve(tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    
    file_path = repo_path / "Test.java"
    file_path.write_text("public class Test {\n  public void doSomething() {}\n}\n")
    
    client = get_java_client()
    payload = {
        "repo_path": str(repo_path),
        "file_path": "Test.java",
        "symbols_to_resolve": ["doSomething"]
    }
    response = client.send_request("javaparser_resolve", payload)
    
    assert response["status"] == "ok"
    assert "mappings" in response
    assert "doSomething" in response["mappings"]
    assert "void doSomething()" in response["mappings"]["doSomething"]

