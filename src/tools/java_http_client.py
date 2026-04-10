"""
Synchronous HTTP client for the Java microservice (Spring Boot on port 8080).

Stages 3 and 4 use this instead of the stdin/stdout java_client.py, which is
incompatible with the Spring Boot HTTP server.
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)

JAVA_SERVICE_URL = os.getenv("JAVA_SERVICE_URL", "http://localhost:8080")
REQUEST_TIMEOUT = 30.0


def _post(endpoint: str, payload: dict) -> dict:
    """Make a synchronous HTTP POST to the Java microservice."""
    url = f"{JAVA_SERVICE_URL}{endpoint}"
    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.ConnectionError:
        logger.debug("Java microservice not reachable at %s (is it running?)", JAVA_SERVICE_URL)
        return {"status": "error", "message": f"Java microservice not reachable at {JAVA_SERVICE_URL}"}
    except requests.exceptions.Timeout:
        return {"status": "error", "message": f"Timeout calling {endpoint}"}
    except requests.exceptions.RequestException as e:
        return {"status": "error", "message": str(e)}


def gumtree_diff(repo_path: str, file_path: str, old_content: str) -> dict:
    return _post("/api/gumtree/diff", {
        "repo_path": repo_path,
        "file_path": file_path,
        "old_content": old_content,
    })


def javaparser_resolve(repo_path: str, file_path: str, symbols_to_resolve: list) -> dict:
    return _post("/api/javaparser/resolve", {
        "repo_path": repo_path,
        "file_path": file_path,
        "symbols_to_resolve": symbols_to_resolve,
    })


def javaparser_find_method(
    repo_path: str, source_file_path: str, method_names: list
) -> dict:
    return _post("/api/javaparser/find-method", {
        "repo_path": repo_path,
        "source_file_path": source_file_path,
        "method_names": method_names,
    })


def japicmp_compare(old_jar_path: str, new_jar_path: str) -> dict:
    return _post("/api/japicmp/compare", {
        "old_jar_path": old_jar_path,
        "new_jar_path": new_jar_path,
    })
