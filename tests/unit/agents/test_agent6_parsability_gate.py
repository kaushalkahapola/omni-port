"""
Unit tests for Fix C (agent6_synthesizer.py):
  - Parsability gate rejects unbalanced braces
  - Parsability gate rejects adjacent duplicate imports
  - Valid Java passes the gate
  - Gate is non-blocking when microservice is unavailable
"""
import os
import sys
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from src.agents.agent6_synthesizer import HunkSynthesizer, _has_adjacent_duplicate_imports


# ── Fix C: has_adjacent_duplicate_imports ─────────────────────────────────────

class TestAdjacentDuplicateImports:
    def test_detects_adjacent_duplicates(self):
        code = "import java.util.List;\nimport java.util.List;\nclass Foo {}"
        assert _has_adjacent_duplicate_imports(code)

    def test_accepts_non_adjacent_duplicates(self):
        # Duplicate but with something in between — not adjacent
        code = "import java.util.List;\nimport java.util.Map;\nimport java.util.List;"
        assert not _has_adjacent_duplicate_imports(code)

    def test_no_imports(self):
        assert not _has_adjacent_duplicate_imports("class Foo { void bar() {} }")

    def test_single_import(self):
        assert not _has_adjacent_duplicate_imports("import java.util.List;\nclass Foo {}")


# ── Fix C: _verify_new_string_parses ─────────────────────────────────────────

class TestParsabilityGate:
    @pytest.fixture
    def synthesizer(self, tmp_path):
        return HunkSynthesizer(str(tmp_path))

    def test_rejects_adjacent_duplicate_imports(self, synthesizer):
        """Gate should reject new_string with adjacent identical import lines."""
        new_string = "import java.util.List;\nimport java.util.List;\npublic void foo() {}"
        result = synthesizer._verify_new_string_parses("", "", new_string, "Foo.java")
        assert not result

    def test_non_java_file_always_passes(self, synthesizer):
        """Fix C gate is Java-only — other file types pass unconditionally."""
        new_string = "{{{{{{ this is garbage"
        result = synthesizer._verify_new_string_parses("", "", new_string, "config.xml")
        assert result

    def test_passes_when_microservice_unavailable(self, synthesizer):
        """
        If the JavaParser microservice can't be reached, the gate should be
        non-blocking (treat as pass) so we don't kill synthesis on infra issues.
        """
        new_string = "public void validMethod() { return; }"
        # Simulate microservice unreachable by patching the import
        with patch("src.tools.java_http_client.javaparser_parse_snippet",
                   return_value={"status": "error", "message": "not reachable"}):
            result = synthesizer._verify_new_string_parses("", "", new_string, "Foo.java")
        assert result  # gate must pass when service is unavailable

    def test_accepts_valid_method(self, synthesizer):
        """Valid Java method should pass without microservice (adjacent import check only)."""
        new_string = "public int compute(int x) { return x * 2; }"
        # With microservice mocked as "ok"
        with patch("src.tools.java_http_client.javaparser_parse_snippet",
                   return_value={"status": "ok", "errors": []}):
            result = synthesizer._verify_new_string_parses("", "", new_string, "Foo.java")
        assert result

    def test_rejects_parse_error_from_microservice(self, synthesizer):
        """If microservice returns parse_error, gate should reject."""
        new_string = "public void broken( { }"
        with patch("src.tools.java_http_client.javaparser_parse_snippet",
                   return_value={"status": "parse_error", "errors": ["Unbalanced brace"]}):
            result = synthesizer._verify_new_string_parses("", "", new_string, "Foo.java")
        assert not result
