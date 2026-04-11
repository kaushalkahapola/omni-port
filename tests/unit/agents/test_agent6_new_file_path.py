"""
Unit tests for Fix G (new-file hunk path):
  - _is_new_file_hunk correctly identifies pure-addition hunks
  - _synthesize_new_file produces correct SynthesizedHunk
  - _apply_synthesized_hunks creates file when old_string == ""
"""
import os
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from src.agents.agent1_localizer import _is_new_file_hunk
from src.agents.agent6_synthesizer import HunkSynthesizer
from src.agents.agent7_validator import _apply_synthesized_hunks


# ── Fix G: _is_new_file_hunk ─────────────────────────────────────────────────

class TestIsNewFileHunk:
    def test_detects_new_file_hunk(self):
        hunk = {
            "file_path": "src/main/java/io/crate/Foo.java",
            "old_content": "",
            "new_content": "public class Foo { public void bar() {} }\n",
        }
        assert _is_new_file_hunk(hunk)

    def test_rejects_hunk_with_old_content(self):
        hunk = {
            "file_path": "src/main/java/io/crate/Foo.java",
            "old_content": "public class Foo {}",
            "new_content": "public class Foo { void bar() {} }",
        }
        assert not _is_new_file_hunk(hunk)

    def test_rejects_hunk_with_no_new_content(self):
        hunk = {
            "file_path": "src/main/java/io/crate/Foo.java",
            "old_content": "",
            "new_content": "",
        }
        assert not _is_new_file_hunk(hunk)

    def test_rejects_hunk_with_no_file_path(self):
        hunk = {
            "file_path": "",
            "old_content": "",
            "new_content": "public class Foo {}",
        }
        assert not _is_new_file_hunk(hunk)

    def test_rejects_whitespace_only_old_content(self):
        """Whitespace in old_content should be treated as empty (strip)."""
        hunk = {
            "file_path": "src/Foo.java",
            "old_content": "   \n\t  ",
            "new_content": "public class Foo {}",
        }
        assert _is_new_file_hunk(hunk)


# ── Fix G: _synthesize_new_file ───────────────────────────────────────────────

class TestSynthesizeNewFile:
    @pytest.fixture
    def synthesizer(self, tmp_path):
        return HunkSynthesizer(str(tmp_path))

    def test_new_file_hunk_produces_empty_old_string(self, synthesizer):
        hunk = {
            "file_path": "src/Foo.java",
            "old_content": "",
            "new_content": "public class Foo { public void bar() {} }",
        }
        result = synthesizer._synthesize_new_file(hunk, "src/Foo.java")
        assert result.old_string == ""
        assert "public class Foo" in result.new_string
        assert result.verified is True
        assert result.confidence == 1.0

    def test_new_file_hunk_empty_new_content_gives_unverified(self, synthesizer):
        hunk = {"old_content": "", "new_content": ""}
        result = synthesizer._synthesize_new_file(hunk, "src/Empty.java")
        assert result.verified is False


# ── Fix G: _apply_synthesized_hunks (new-file creation) ──────────────────────

class TestApplySynthesizedHunksNewFile:
    def test_creates_new_file_when_old_string_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            new_content = "public class AllLikeOperator { public void eval() {} }"
            hunks = [{
                "file_path": "src/AllLikeOperator.java",
                "old_string": "",
                "new_string": new_content,
                "verified": True,
                "confidence": 1.0,
            }]
            result = _apply_synthesized_hunks(tmp, hunks)
            assert result["success"]
            created = os.path.join(tmp, "src", "AllLikeOperator.java")
            assert os.path.exists(created)
            with open(created) as f:
                assert "AllLikeOperator" in f.read()

    def test_skips_creation_if_file_already_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "src"), exist_ok=True)
            existing = os.path.join(tmp, "src", "Existing.java")
            with open(existing, "w") as f:
                f.write("original content\n")

            hunks = [{
                "file_path": "src/Existing.java",
                "old_string": "",
                "new_string": "new content",
                "verified": True,
                "confidence": 1.0,
            }]
            result = _apply_synthesized_hunks(tmp, hunks)
            assert result["success"]
            # File should be unchanged (not overwritten)
            with open(existing) as f:
                assert f.read() == "original content\n"

    def test_creates_nested_directory_for_new_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            hunks = [{
                "file_path": "a/b/c/New.java",
                "old_string": "",
                "new_string": "class New {}",
                "verified": True,
                "confidence": 1.0,
            }]
            result = _apply_synthesized_hunks(tmp, hunks)
            assert result["success"]
            assert os.path.exists(os.path.join(tmp, "a", "b", "c", "New.java"))
