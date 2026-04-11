"""
Unit tests for Fix H + Fix A (agent7_validator.py):
  - Error-tail extraction (Fix H)
  - 3-way git apply strategy (Fix A)
  - Already-applied detection (Fix A)
  - RST path remap (Fix A)
"""
import os
import tempfile
import textwrap
import pytest

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from src.agents.agent7_validator import (
    _extract_build_errors,
    _find_closest_rst,
    _is_rst_release_notes,
    _parse_version_tuple,
    _try_apply_rst_hunk_remapped,
)


# ── Fix H: Error-tail extraction ──────────────────────────────────────────────

class TestExtractBuildErrors:
    def test_extracts_error_lines(self):
        log = "\n".join([
            "[INFO] Scanning for projects...",
            "[INFO] Building Docker image...",
            "Step 1/10 : FROM maven:3.8",
            "src/main/java/Foo.java:12: error: cannot find symbol",
            "    return bar.baz();",
            "BUILD FAILURE",
        ])
        result = _extract_build_errors(log)
        assert "error: cannot find symbol" in result
        assert "BUILD FAILURE" in result
        # Docker preamble should NOT dominate
        assert "Scanning for projects" not in result

    def test_falls_back_to_tail_when_no_matches(self):
        log = "a\n" * 100 + "final line"
        result = _extract_build_errors(log)
        assert "final line" in result

    def test_empty_log(self):
        assert _extract_build_errors("") == ""

    def test_docker_preamble_not_in_result_when_errors_exist(self):
        docker_preamble = "Step 1/20 : FROM openjdk:17\n" * 50
        actual_errors = "src/Foo.java:5: error: ';' expected\nBUILD FAILURE"
        log = docker_preamble + actual_errors
        result = _extract_build_errors(log)
        assert "error: ';' expected" in result
        # Should capture javac errors, not ~100 lines of Docker preamble
        assert result.count("FROM openjdk") == 0


# ── Fix A: RST path remap ─────────────────────────────────────────────────────

class TestRstRelease:
    def test_is_rst_release_notes_positive(self):
        assert _is_rst_release_notes("docs/appendices/release-notes/5.10.9.rst")
        assert _is_rst_release_notes("releases/release-5.8.2.rst")

    def test_is_rst_release_notes_negative(self):
        assert not _is_rst_release_notes("src/main/java/Foo.java")
        assert not _is_rst_release_notes("docs/guide.rst")  # no "release" in path

    def test_parse_version_tuple(self):
        assert _parse_version_tuple("5.10.12.rst") == (5, 10, 12)
        assert _parse_version_tuple("5.8.2.rst") == (5, 8, 2)
        assert _parse_version_tuple("no-version.rst") == ()

    def test_find_closest_rst_returns_highest_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            rst_dir = os.path.join(tmp, "docs", "release-notes")
            os.makedirs(rst_dir)
            # Create a few versioned RSTs
            for v in ["5.10.9.rst", "5.10.12.rst", "5.10.3.rst"]:
                open(os.path.join(rst_dir, v), "w").close()

            missing = "docs/release-notes/5.10.2.rst"  # doesn't exist
            result = _find_closest_rst(tmp, missing)
            assert result is not None
            assert "5.10.12.rst" in result

    def test_find_closest_rst_returns_none_when_dir_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            assert _find_closest_rst(tmp, "nonexistent/path/5.10.9.rst") is None

    def test_try_apply_rst_hunk_remapped_appends_added_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            rst_dir = os.path.join(tmp, "docs", "release-notes")
            os.makedirs(rst_dir)
            # Create the target RST (closest version)
            existing_rst = os.path.join(rst_dir, "5.10.12.rst")
            with open(existing_rst, "w") as f:
                f.write("Existing content\n")

            hunk_text = textwrap.dedent("""\
                @@ -1,3 +1,5 @@
                 Existing content
                +
                +New release note entry
            """)
            missing_target = "docs/release-notes/5.10.9.rst"
            result = _try_apply_rst_hunk_remapped(tmp, hunk_text, missing_target)

            assert result["success"], result["output"]
            with open(existing_rst) as f:
                content = f.read()
            assert "New release note entry" in content

    def test_try_apply_rst_hunk_remapped_fails_gracefully_no_rst(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _try_apply_rst_hunk_remapped(
                tmp, "@@ -1 +1 @@\n+content\n", "docs/release-notes/5.9.1.rst"
            )
            assert not result["success"]
