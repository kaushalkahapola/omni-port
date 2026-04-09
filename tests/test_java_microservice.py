import json
import pytest
from src.tools.java_client import get_java_client

def test_java_microservice_ping():
    client = get_java_client()
    response = client.send_request("ping", {})
    assert response["status"] == "ok"
    assert response["message"] == "pong"

def test_java_microservice_gumtree():
    client = get_java_client()
    payload = {
        "repo_path": ".",
        "file_path": "nonexistent.java",
        "old_content": "public class Test {}"
    }
    response = client.send_request("gumtree_diff", payload)
    # The file doesn't exist, so we expect an error response, but we verify the routing works
    assert response["status"] == "error"
    assert "File not found" in response["message"]

