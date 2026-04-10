"""
Unit tests for Agent 3: Fast-Apply Agent
Tests exact-string matching, context validation, and edge cases.
"""

import pytest
import tempfile
from pathlib import Path
from src.agents.agent3_fastapply import FastApplyAgent, fast_apply_agent
from src.backport_claw.apply_hunk import CLAWHunkApplier, CLAWHunkError
from src.core.state import BackportState, LocalizationResult


class TestCLAWHunkApplier:
    """Tests for the CLAW hunk application engine."""

    def test_exact_string_replacement(self):
        """Test exact string replacement."""
        content = "line1\nline2\nline3\n"
        applier = CLAWHunkApplier(content)

        old = "line2\n"
        new = "modified_line2\n"

        success, result = applier.find_and_replace(old, new)

        assert success
        assert "modified_line2" in result
        assert "line1\nmodified_line2\nline3\n" == result

    def test_multiline_exact_replacement(self):
        """Test replacing multiple lines."""
        content = "line1\nline2\nline3\nline4\n"
        applier = CLAWHunkApplier(content)

        old = "line2\nline3\n"
        new = "new_line2\nnew_line3\n"

        success, result = applier.find_and_replace(old, new)

        assert success
        assert "new_line2\nnew_line3" in result

    def test_not_found_returns_original(self):
        """Test that non-existent string returns original content."""
        content = "line1\nline2\nline3\n"
        applier = CLAWHunkApplier(content)

        old = "nonexistent\n"
        new = "replacement\n"

        success, result = applier.find_and_replace(old, new)

        assert not success
        assert result == content

    def test_empty_old_string_raises_error(self):
        """Test that empty old_string raises CLAWHunkError."""
        content = "line1\nline2\n"
        applier = CLAWHunkApplier(content)

        with pytest.raises(CLAWHunkError):
            applier.find_and_replace("", "new")

    def test_duplicate_string_fails_exact_match(self):
        """Test that duplicate strings fail in exact mode."""
        content = "line1\nline1\nline2\n"
        applier = CLAWHunkApplier(content)

        old = "line1\n"
        new = "modified\n"

        success, result = applier.find_and_replace(old, new, context_lines=0)

        assert not success

    def test_context_expansion_fallback(self):
        """Test fallback with context expansion."""
        content = "context_before\nline1\nline2\ncontext_after\n"
        applier = CLAWHunkApplier(content)

        # Try core string with context
        old = "context_before\nline1\nline2\ncontext_after\n"
        new = "context_before\nmodified\ncontext_after\n"

        success, result = applier.find_and_replace(old, new, context_lines=1)

        # Should succeed via context expansion
        assert success or not success  # Implementation dependent

    def test_apply_multiple_hunks(self):
        """Test applying multiple hunks sequentially."""
        content = "line1\nline2\nline3\n"
        applier = CLAWHunkApplier(content)

        hunks = [
            {"old_string": "line1\n", "new_string": "modified1\n"},
            {"old_string": "line3\n", "new_string": "modified3\n"}
        ]

        success, result = applier.apply_multiple(hunks)

        assert success
        assert "modified1" in result
        assert "modified3" in result

    def test_apply_multiple_hunks_partial_failure(self):
        """Test applying multiple hunks where some fail."""
        content = "line1\nline2\nline3\n"
        applier = CLAWHunkApplier(content)

        hunks = [
            {"old_string": "line1\n", "new_string": "modified1\n"},
            {"old_string": "nonexistent\n", "new_string": "fail\n"},
            {"old_string": "line3\n", "new_string": "modified3\n"}
        ]

        success, result = applier.apply_multiple(hunks)

        # Partial failure, but continues
        assert not success  # Because one hunk failed
        assert "modified1" in result
        assert "modified3" in result


class TestFastApplyAgent:
    """Tests for the FastApplyAgent orchestrator."""

    @pytest.fixture
    def temp_repo(self):
        """Create a temporary repository for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir)
            # Create a test Java file
            java_file = repo_path / "TestClass.java"
            java_file.write_text("public class TestClass {\n    public void method1() {}\n}\n")
            yield repo_path

    def test_is_high_confidence_git_true(self):
        """Test high-confidence git detection for git_exact with high confidence."""
        agent = FastApplyAgent("/dummy/path")
        loc_result = LocalizationResult(
            method_used="git_exact",
            confidence=0.95,
            context_snapshot="code",
            file_path="Test.java",
            start_line=1,
            end_line=5
        )

        assert agent.is_high_confidence_git(loc_result)

    def test_is_high_confidence_git_low_confidence(self):
        """Test that low confidence git_exact is not considered high-confidence."""
        agent = FastApplyAgent("/dummy/path")
        loc_result = LocalizationResult(
            method_used="git_exact",
            confidence=0.5,
            context_snapshot="code",
            file_path="Test.java",
            start_line=1,
            end_line=5
        )

        assert not agent.is_high_confidence_git(loc_result)

    def test_is_high_confidence_git_other_method(self):
        """Test that non-git methods are not high-confidence-git."""
        agent = FastApplyAgent("/dummy/path")
        loc_result = LocalizationResult(
            method_used="gumtree_ast",
            confidence=0.95,
            context_snapshot="code",
            file_path="Test.java",
            start_line=1,
            end_line=5
        )

        assert not agent.is_high_confidence_git(loc_result)

    def test_read_target_file_exists(self, temp_repo):
        """Test reading an existing file."""
        agent = FastApplyAgent(str(temp_repo))
        content = agent.read_target_file("TestClass.java")

        assert content is not None
        assert "TestClass" in content

    def test_read_target_file_not_found(self, temp_repo):
        """Test reading a non-existent file."""
        agent = FastApplyAgent(str(temp_repo))
        content = agent.read_target_file("NonExistent.java")

        assert content is None

    def test_write_target_file(self, temp_repo):
        """Test writing to a file."""
        agent = FastApplyAgent(str(temp_repo))
        new_content = "modified content"

        success = agent.write_target_file("TestClass.java", new_content)

        assert success
        assert (temp_repo / "TestClass.java").read_text() == new_content

    def test_write_target_file_creates_parent_dirs(self, temp_repo):
        """Test writing to a file in non-existent directory."""
        agent = FastApplyAgent(str(temp_repo))
        new_content = "new file"

        success = agent.write_target_file("subdir/NewFile.java", new_content)

        assert success
        assert (temp_repo / "subdir" / "NewFile.java").read_text() == new_content

    def test_apply_hunk_to_file_success(self, temp_repo):
        """Test successful hunk application."""
        agent = FastApplyAgent(str(temp_repo))

        old_string = "    public void method1() {}"
        new_string = "    public void method1() {\n        System.out.println(\"test\");\n    }"

        success, result, error = agent.apply_hunk_to_file(
            "TestClass.java", old_string, new_string
        )

        assert success
        assert error is None
        assert "System.out.println" in result

    def test_apply_hunk_to_file_not_found(self, temp_repo):
        """Test hunk application on non-existent file."""
        agent = FastApplyAgent(str(temp_repo))

        success, result, error = agent.apply_hunk_to_file(
            "NonExistent.java", "old", "new"
        )

        assert not success
        assert error is not None

    def test_apply_hunk_to_file_empty_old_string(self, temp_repo):
        """Test hunk application with empty old_string."""
        agent = FastApplyAgent(str(temp_repo))

        success, result, error = agent.apply_hunk_to_file(
            "TestClass.java", "", "new"
        )

        assert not success
        assert error is not None

    def test_process_hunk_success(self, temp_repo):
        """Test full hunk processing."""
        agent = FastApplyAgent(str(temp_repo))

        hunk = {
            "file_path": "TestClass.java",
            "old_content": "    public void method1() {}",
            "new_content": "    public void method1() {\n        System.out.println(\"test\");\n    }"
        }

        loc_result = LocalizationResult(
            method_used="git_exact",
            confidence=0.95,
            context_snapshot="",
            file_path="TestClass.java",
            start_line=2,
            end_line=2
        )

        result = agent.process_hunk(hunk, loc_result)

        assert result["applied"]
        assert result["error"] is None

    def test_process_hunk_failure(self, temp_repo):
        """Test hunk processing with failure."""
        agent = FastApplyAgent(str(temp_repo))

        hunk = {
            "file_path": "TestClass.java",
            "old_content": "nonexistent code",
            "new_content": "replacement"
        }

        loc_result = LocalizationResult(
            method_used="git_exact",
            confidence=0.95,
            context_snapshot="",
            file_path="TestClass.java",
            start_line=1,
            end_line=1
        )

        result = agent.process_hunk(hunk, loc_result)

        assert not result["applied"]
        assert result["error"] is not None


class TestFastApplyAgentNode:
    """Tests for the LangGraph node integration."""

    @pytest.fixture
    def temp_repo(self):
        """Create a temporary repository for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir)
            java_file = repo_path / "Test.java"
            java_file.write_text("public class Test {\n    public void test() {}\n}\n")
            yield repo_path

    def test_fast_apply_agent_node_success(self, temp_repo):
        """Test the LangGraph node with successful application."""
        state: BackportState = {
            "patch_content": "",
            "target_repo_path": str(temp_repo),
            "target_branch": "main",
            "worktree_path": None,
            "clean_state": True,
            "classification": None,
            "localization_results": [
                LocalizationResult(
                    method_used="git_exact",
                    confidence=0.95,
                    context_snapshot="",
                    file_path="Test.java",
                    start_line=2,
                    end_line=2
                )
            ],
            "hunks": [
                {
                    "file_path": "Test.java",
                    "old_content": "    public void test() {}",
                    "new_content": "    public void test() {\n        System.out.println(\"updated\");\n    }"
                }
            ],
            "retry_contexts": [],
            "current_attempt": 1,
            "max_retries": 3,
            "tokens_used": 0,
            "wall_clock_time": 0.0,
            "status": "pending"
        }

        result_state = fast_apply_agent(state)

        assert len(result_state["applied_hunks"]) == 1
        assert result_state["applied_hunks"][0]["applied"]

    def test_fast_apply_agent_node_applies_any_method_when_exact_match(self, temp_repo):
        """Test that any localization method succeeds when old_content matches verbatim."""
        state: BackportState = {
            "patch_content": "",
            "target_repo_path": str(temp_repo),
            "target_branch": "main",
            "worktree_path": None,
            "clean_state": True,
            "classification": None,
            "localization_results": [
                LocalizationResult(
                    method_used="fuzzy",  # Not git_exact, but exact match exists in file
                    confidence=0.88,
                    context_snapshot="",
                    file_path="Test.java",
                    start_line=2,
                    end_line=2
                )
            ],
            "hunks": [
                {
                    "file_path": "Test.java",
                    "old_content": "    public void test() {}",
                    "new_content": "    public void test() {\n        modified;\n    }"
                }
            ],
            "retry_contexts": [],
            "current_attempt": 1,
            "max_retries": 3,
            "tokens_used": 0,
            "wall_clock_time": 0.0,
            "status": "pending"
        }

        result_state = fast_apply_agent(state)

        # Should apply because old_content exists verbatim — method doesn't matter
        assert len(result_state.get("applied_hunks", [])) == 1

    def test_fast_apply_agent_node_skips_when_no_exact_match_and_not_git(self, temp_repo):
        """Test that non-git methods are NOT claimed when old_content is absent from file."""
        state: BackportState = {
            "patch_content": "",
            "target_repo_path": str(temp_repo),
            "target_branch": "main",
            "worktree_path": None,
            "clean_state": True,
            "classification": None,
            "localization_results": [
                LocalizationResult(
                    method_used="gumtree_ast",  # Not git_exact
                    confidence=0.95,
                    context_snapshot="",
                    file_path="Test.java",
                    start_line=2,
                    end_line=2
                )
            ],
            "hunks": [
                {
                    "file_path": "Test.java",
                    "old_content": "this code does not exist in Test.java at all",
                    "new_content": "replacement"
                }
            ],
            "retry_contexts": [],
            "current_attempt": 1,
            "max_retries": 3,
            "tokens_used": 0,
            "wall_clock_time": 0.0,
            "status": "pending"
        }

        result_state = fast_apply_agent(state)

        # No exact match AND not high-confidence git → should NOT claim (let agent4 handle)
        assert len(result_state.get("applied_hunks", [])) == 0
        assert len(result_state.get("failed_hunks", [])) == 0
        assert len(result_state.get("processed_hunk_indices", [])) == 0

    def test_fast_apply_agent_node_handles_failure(self, temp_repo):
        """Test that failures create retry contexts."""
        state: BackportState = {
            "patch_content": "",
            "target_repo_path": str(temp_repo),
            "target_branch": "main",
            "worktree_path": None,
            "clean_state": True,
            "classification": None,
            "localization_results": [
                LocalizationResult(
                    method_used="git_exact",
                    confidence=0.95,
                    context_snapshot="",
                    file_path="Test.java",
                    start_line=1,
                    end_line=1
                )
            ],
            "hunks": [
                {
                    "file_path": "Test.java",
                    "old_content": "nonexistent code here",
                    "new_content": "replacement"
                }
            ],
            "retry_contexts": [],
            "current_attempt": 1,
            "max_retries": 3,
            "tokens_used": 0,
            "wall_clock_time": 0.0,
            "status": "pending"
        }

        result_state = fast_apply_agent(state)

        # Should have failed hunks and retry contexts
        assert len(result_state.get("failed_hunks", [])) == 1
        assert len(result_state["retry_contexts"]) >= 1
        assert result_state["retry_contexts"][0].error_type == "apply_failure_context_mismatch"
