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


def javaparser_method_modifiers(
    repo_path: str, file_path: str, method_names: list
) -> dict:
    """
    Return modifier info for methods in a Java file.

    Response on success:
      {
        "status": "ok",
        "methods": {
          "<name>": {
            "visibility":        "public" | "protected" | "private" | "package-private",
            "modifiers":         ["public", "abstract", ...],
            "is_abstract":       true | false,
            "has_body":          true | false,
            "is_class_abstract": true | false,
            "declaring_class":   "<ClassName>"
          }, ...
        }
      }

    Returns {"status": "error", ...} when the service is unreachable or the file
    cannot be parsed.  Callers should treat a non-"ok" status as unavailable and
    fall back to regex heuristics.
    """
    return _post("/api/javaparser/method-modifiers", {
        "repo_path": repo_path,
        "file_path": file_path,
        "method_names": method_names,
    })


def japicmp_compare(old_jar_path: str, new_jar_path: str) -> dict:
    return _post("/api/japicmp/compare", {
        "old_jar_path": old_jar_path,
        "new_jar_path": new_jar_path,
    })


def javaparser_parse_snippet(code_snippet: str, context_class: str = "") -> dict:
    """
    Fix C: Validate that a Java code snippet is syntactically valid.

    Sends the snippet to the Java microservice's /api/javaparser/parse-snippet
    endpoint which tries to parse it as a class body declaration, then as a
    statement, then as a compilation unit.

    Response on success:
      {"status": "ok", "errors": []}
    Response on parse failure:
      {"status": "parse_error", "errors": ["<message>", ...]}
    Response when service unavailable (treated as pass — non-blocking gate):
      {"status": "error", "message": "..."}

    Callers should treat any non-"parse_error" status as "gate passes" so a
    temporarily unavailable microservice never blocks synthesis.
    """
    return _post("/api/javaparser/parse-snippet", {
        "code": code_snippet,
        "context_class": context_class,
    })
