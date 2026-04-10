"""
Comprehensive tests for Agents 4, 5, and 6:
- Agent 4: Namespace Adapter
- Agent 5: Structural Refactor
- Agent 6: Hunk Synthesizer
"""

import pytest
import tempfile
from pathlib import Path
from src.agents.agent4_namespace import (
    namespace_adapter_agent, _has_import_changes, _should_namespace_adapt,
)
from src.agents.agent5_structural import structural_refactor_agent
from src.agents.agent6_synthesizer import (
    HunkSynthesizer, hunk_synthesizer_agent, SynthesizedHunk
)
from src.core.state import BackportState, LocalizationResult


# ============================================================================
# Tests for Agent 4: Namespace Adapter routing helpers
# ============================================================================


class TestNamespaceAdapterRouting:
    """Tests for the namespace adapter routing logic."""

    def test_has_import_changes_detects_addition(self):
        """Test that a new import is detected."""
        hunk = {
            "old_content": "import java.util.List;\n",
            "new_content": "import java.util.List;\nimport java.util.Map;\n",
        }
        assert _has_import_changes(hunk)

    def test_has_import_changes_no_change(self):
        """Test that identical imports return False."""
        hunk = {
            "old_content": "import java.util.List;\nreturn value;\n",
            "new_content": "import java.util.List;\nreturn other;\n",
        }
        assert not _has_import_changes(hunk)

    def test_should_namespace_adapt_import_change(self):
        """Test routing when import changes detected."""
        hunk = {
            "old_content": "import java.util.List;\n",
            "new_content": "import java.util.List;\nimport java.util.Map;\n",
        }
        loc = LocalizationResult(
            method_used="fuzzy", confidence=0.88, context_snapshot="",
            file_path="Test.java", start_line=1, end_line=1,
        )
        assert _should_namespace_adapt(hunk, loc)

    def test_should_namespace_adapt_api_drift(self):
        """Test routing when old_content not found in target file (API drift)."""
        hunk = {
            "old_content": "builder.startObject(COMPILATIONS_HISTORY);",
            "new_content": "builder.startObject(CACHE_EVICTIONS_HISTORY);",
        }
        loc = LocalizationResult(
            method_used="fuzzy", confidence=0.88, context_snapshot="",
            file_path="Test.java", start_line=10, end_line=10,
        )
        # file_content uses different API than old_content
        file_content = "ob.xContentObject(COMPILATIONS_HISTORY, cacheEvictionsHistory);\n"
        assert _should_namespace_adapt(hunk, loc, file_content)

    def test_should_namespace_adapt_exact_match_no_import(self):
        """Test NOT routing when old_content found verbatim and no import changes."""
        hunk = {
            "old_content": "return value;",
            "new_content": "return processValue(value);",
        }
        loc = LocalizationResult(
            method_used="fuzzy", confidence=0.88, context_snapshot="",
            file_path="Test.java", start_line=5, end_line=5,
        )
        file_content = "public void method() {\n    return value;\n}\n"
        assert not _should_namespace_adapt(hunk, loc, file_content)


class TestNamespaceAdapterNode:
    """Tests for the namespace_adapter_agent LangGraph node."""

    def test_namespace_adapter_node_with_mappings(self):
        """Test the node processes hunks with symbol mappings."""
        state: BackportState = {
            "patch_content": "",
            "target_repo_path": "/dummy",
            "target_branch": "main",
            "worktree_path": None,
            "clean_state": True,
            "classification": None,
            "localization_results": [
                LocalizationResult(
                    method_used="gumtree_ast",
                    confidence=0.8,
                    context_snapshot="",
                    file_path="Test.java",
                    start_line=1,
                    end_line=1,
                    symbol_mappings={"OldClass": "NewClass"}
                )
            ],
            "hunks": [
                {
                    "file_path": "Test.java",
                    "old_content": "OldClass obj = new OldClass();",
                    "new_content": "OldClass obj = new OldClass(arg);"
                }
            ],
            "retry_contexts": [],
            "current_attempt": 1,
            "max_retries": 3,
            "tokens_used": 0,
            "wall_clock_time": 0.0,
            "status": "pending"
        }

        result_state = namespace_adapter_agent(state)

        assert len(result_state["adapted_hunks"]) == 1

    def test_namespace_adapter_node_skips_no_mappings(self):
        """Test the node skips hunks without symbol mappings."""
        state: BackportState = {
            "patch_content": "",
            "target_repo_path": "/dummy",
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
                    "old_content": "return value;",
                    "new_content": "return processValue(value);"
                }
            ],
            "retry_contexts": [],
            "current_attempt": 1,
            "max_retries": 3,
            "tokens_used": 0,
            "wall_clock_time": 0.0,
            "status": "pending"
        }

        result_state = namespace_adapter_agent(state)

        # Should skip (no symbol mappings)
        assert len(result_state.get("adapted_hunks", [])) == 0


# ============================================================================
# Tests for Agent 5: Structural Refactor
# ============================================================================


class TestStructuralRefactor:
    """Tests for the Structural Refactor agent."""

    def test_parse_gumtree_edits_insert(self):
        """Test parsing Insert operations from GumTree script."""
        script = "Insert 123 MethodDeclaration public void test() {}"
        refactor = StructuralRefactor("/dummy/path")

        edits = refactor.parse_gumtree_edits(script)

        assert len(edits) == 1
        assert edits[0].operation == EditType.INSERT
        assert edits[0].node_type == "MethodDeclaration"

    def test_parse_gumtree_edits_delete(self):
        """Test parsing Delete operations."""
        script = "Delete 456 oldMethodCode"
        refactor = StructuralRefactor("/dummy/path")

        edits = refactor.parse_gumtree_edits(script)

        assert len(edits) == 1
        assert edits[0].operation == EditType.DELETE

    def test_parse_gumtree_edits_update(self):
        """Test parsing Update operations."""
        script = "Update 789 modifierList public"
        refactor = StructuralRefactor("/dummy/path")

        edits = refactor.parse_gumtree_edits(script)

        assert len(edits) == 1
        assert edits[0].operation == EditType.UPDATE

    def test_parse_gumtree_edits_move(self):
        """Test parsing Move operations."""
        script = "Move 321 456 789"
        refactor = StructuralRefactor("/dummy/path")

        edits = refactor.parse_gumtree_edits(script)

        assert len(edits) == 1
        assert edits[0].operation == EditType.MOVE

    def test_parse_gumtree_edits_multiple(self):
        """Test parsing multiple mixed operations."""
        script = """
Insert 1 MethodDeclaration method1()
Delete 2 oldMethod()
Update 3 name newName
Move 4 5 6
"""
        refactor = StructuralRefactor("/dummy/path")

        edits = refactor.parse_gumtree_edits(script)

        assert len(edits) == 4
        assert edits[0].operation == EditType.INSERT
        assert edits[1].operation == EditType.DELETE
        assert edits[2].operation == EditType.UPDATE
        assert edits[3].operation == EditType.MOVE

    def test_summarize_structural_changes(self):
        """Test summarizing structural changes."""
        edits = [
            GumTreeEdit(operation=EditType.INSERT, node_type="Method", old_code=None, new_code="new"),
            GumTreeEdit(operation=EditType.DELETE, node_type="Field", old_code="old", new_code=None),
            GumTreeEdit(operation=EditType.UPDATE, node_type="Type", old_code="old", new_code="new"),
            GumTreeEdit(operation=EditType.MOVE, node_type="Class", old_code="old", new_code="new"),
            GumTreeEdit(operation=EditType.INSERT, node_type="Method", old_code=None, new_code="new"),
        ]

        refactor = StructuralRefactor("/dummy/path")
        summary = refactor.summarize_structural_changes(edits)

        assert "Inserts: 2" in summary
        assert "Deletes: 1" in summary
        assert "Updates: 1" in summary
        assert "Moves: 1" in summary

    def test_refactor_hunk_without_llm(self):
        """Test refactoring without LLM client (fallback)."""
        refactor = StructuralRefactor("/dummy/path", llm_client=None)

        hunk = {
            "old_content": "public void oldMethod() {}",
            "new_content": "public void newMethod() {}"
        }

        loc_result = LocalizationResult(
            method_used="gumtree_ast",
            confidence=0.4,
            context_snapshot="class Test {}",
            file_path="Test.java",
            start_line=1,
            end_line=1
        )

        output = refactor.refactor_hunk(hunk, loc_result)

        assert not output.success
        assert output.semantic_equivalence == 0.0


class TestStructuralRefactorNode:
    """Tests for the structural_refactor_agent LangGraph node."""

    def test_structural_refactor_node_skips_high_confidence(self):
        """Test the node skips high-confidence hunks."""
        state: BackportState = {
            "patch_content": "",
            "target_repo_path": "/dummy",
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
                    "old_content": "return old;",
                    "new_content": "return new;"
                }
            ],
            "retry_contexts": [],
            "current_attempt": 1,
            "max_retries": 3,
            "tokens_used": 0,
            "wall_clock_time": 0.0,
            "status": "pending"
        }

        result_state = structural_refactor_agent(state)

        # Should skip (high confidence, not gumtree)
        assert len(result_state.get("refactored_hunks", [])) == 0

    def test_structural_refactor_node_processes_low_confidence(self):
        """Test the node processes low-confidence hunks."""
        state: BackportState = {
            "patch_content": "",
            "target_repo_path": "/dummy",
            "target_branch": "main",
            "worktree_path": None,
            "clean_state": True,
            "classification": None,
            "localization_results": [
                LocalizationResult(
                    method_used="fuzzy_text",
                    confidence=0.3,
                    context_snapshot="",
                    file_path="Test.java",
                    start_line=1,
                    end_line=1
                )
            ],
            "hunks": [
                {
                    "old_content": "public void method() {}",
                    "new_content": "public void method(int arg) {}"
                }
            ],
            "retry_contexts": [],
            "current_attempt": 1,
            "max_retries": 3,
            "tokens_used": 0,
            "wall_clock_time": 0.0,
            "status": "pending"
        }

        result_state = structural_refactor_agent(state)

        # Should process (low confidence)
        # Will fail because no LLM client, but should try
        assert "refactored_hunks" in result_state or "retry_contexts" in result_state


# ============================================================================
# Tests for Agent 6: Hunk Synthesizer
# ============================================================================


class TestHunkSynthesizer:
    """Tests for the Hunk Synthesizer agent."""

    @pytest.fixture
    def temp_repo(self):
        """Create a temporary repository for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir)
            java_file = repo_path / "TestClass.java"
            java_file.write_text("""public class TestClass {
    public void method1() {
        System.out.println("test");
    }

    public void method2() {
        return;
    }
}
""")
            yield repo_path

    def test_read_file_exists(self, temp_repo):
        """Test reading an existing file."""
        synthesizer = HunkSynthesizer(str(temp_repo))
        content = synthesizer.read_file("TestClass.java")

        assert content is not None
        assert "TestClass" in content

    def test_read_file_not_found(self, temp_repo):
        """Test reading a non-existent file."""
        synthesizer = HunkSynthesizer(str(temp_repo))
        content = synthesizer.read_file("NonExistent.java")

        assert content is None

    def test_extract_lines_with_context(self, temp_repo):
        """Test extracting lines with context."""
        synthesizer = HunkSynthesizer(str(temp_repo))
        file_content = synthesizer.read_file("TestClass.java")

        # Extract lines 2-2 with 1 line context
        result = synthesizer.extract_lines_with_context(
            file_content, 2, 2, context_lines=1
        )

        assert "public void method1()" in result

    def test_verify_old_string_exists_exact(self, temp_repo):
        """Test verifying an exact string match."""
        synthesizer = HunkSynthesizer(str(temp_repo))
        file_content = synthesizer.read_file("TestClass.java")

        old_string = '    public void method1() {'
        verified, confidence = synthesizer.verify_old_string_exists(file_content, old_string)

        assert verified
        assert confidence == 1.0

    def test_verify_old_string_not_found(self, temp_repo):
        """Test verifying a non-existent string."""
        synthesizer = HunkSynthesizer(str(temp_repo))
        file_content = synthesizer.read_file("TestClass.java")

        old_string = "nonexistent code"
        verified, confidence = synthesizer.verify_old_string_exists(file_content, old_string)

        assert not verified
        assert confidence == 0.0

    def test_verify_old_string_duplicate(self, temp_repo):
        """Test verifying a string that appears multiple times."""
        synthesizer = HunkSynthesizer(str(temp_repo))
        file_content = synthesizer.read_file("TestClass.java")

        # "public void" appears twice
        old_string = "public void"
        verified, confidence = synthesizer.verify_old_string_exists(file_content, old_string)

        assert verified
        assert confidence == 0.9  # Not unique

    def test_synthesize_hunk_exact_match(self, temp_repo):
        """Test synthesizing a hunk with exact match."""
        synthesizer = HunkSynthesizer(str(temp_repo))

        hunk = {
            "file_path": "TestClass.java",
            "old_content": '    public void method1() {',
            "new_content": '    public void method1() {\n        // Modified'
        }

        loc_result = LocalizationResult(
            method_used="git_exact",
            confidence=0.95,
            context_snapshot="",
            file_path="TestClass.java",
            start_line=2,
            end_line=2
        )

        result = synthesizer.synthesize_hunk(hunk, loc_result)

        assert result.verified
        assert result.confidence == 1.0
        assert result.context_lines_included == 0

    def test_synthesize_hunk_no_match(self, temp_repo):
        """Test synthesizing a hunk with no match."""
        synthesizer = HunkSynthesizer(str(temp_repo))

        hunk = {
            "file_path": "TestClass.java",
            "old_content": "IMPOSSIBLE_UNIQUE_MARKER_XYZ_12345_NOTFOUND",
            "new_content": "replacement"
        }

        loc_result = LocalizationResult(
            method_used="fuzzy_text",
            confidence=0.5,
            context_snapshot="",
            file_path="TestClass.java",
            start_line=100,
            end_line=100
        )

        result = synthesizer.synthesize_hunk(hunk, loc_result)

        assert not result.verified
        assert result.confidence == 0.0

    def test_synthesize_batch(self, temp_repo):
        """Test synthesizing multiple hunks."""
        synthesizer = HunkSynthesizer(str(temp_repo))

        hunks = [
            {
                "file_path": "TestClass.java",
                "old_content": '    public void method1() {',
                "new_content": '    public void method1(int x) {'
            },
            {
                "file_path": "TestClass.java",
                "old_content": "IMPOSSIBLE_MARKER_NOT_IN_FILE_XYZ",
                "new_content": "replacement"
            }
        ]

        loc_results = [
            LocalizationResult(
                method_used="git_exact",
                confidence=0.95,
                context_snapshot="",
                file_path="TestClass.java",
                start_line=2,
                end_line=2
            ),
            LocalizationResult(
                method_used="fuzzy_text",
                confidence=0.4,
                context_snapshot="",
                file_path="TestClass.java",
                start_line=100,
                end_line=100
            )
        ]

        output = synthesizer.synthesize_batch(hunks, loc_results)

        assert len(output.synthesized_hunks) == 1
        assert len(output.failed_hunks) == 1
        assert not output.success


class TestHunkSynthesizerNode:
    """Tests for the hunk_synthesizer_agent LangGraph node."""

    @pytest.fixture
    def temp_repo(self):
        """Create a temporary repository for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir)
            java_file = repo_path / "Test.java"
            java_file.write_text("""public class Test {
    public void test() {
        System.out.println("test");
    }
}
""")
            yield repo_path

    def test_hunk_synthesizer_node_success(self, temp_repo):
        """Test the node synthesizes hunks successfully."""
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
                    "old_content": '    public void test() {',
                    "new_content": '    public void test() {\n        // Modified'
                }
            ],
            "applied_hunks": [],
            "adapted_hunks": [],
            "refactored_hunks": [],
            "retry_contexts": [],
            "current_attempt": 1,
            "max_retries": 3,
            "tokens_used": 0,
            "wall_clock_time": 0.0,
            "status": "pending"
        }

        result_state = hunk_synthesizer_agent(state)

        assert len(result_state["synthesized_hunks"]) == 1
        assert result_state["synthesis_status"] == "success"

    def test_hunk_synthesizer_node_partial_failure(self, temp_repo):
        """Test the node handles partial failures."""
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
                ),
                LocalizationResult(
                    method_used="fuzzy_text",
                    confidence=0.5,
                    context_snapshot="",
                    file_path="Test.java",
                    start_line=100,
                    end_line=100
                )
            ],
            "hunks": [
                {
                    "file_path": "Test.java",
                    "old_content": '    public void test() {',
                    "new_content": '    public void test() {\n        // Success'
                },
                {
                    "file_path": "Test.java",
                    "old_content": "IMPOSSIBLE_UNIQUE_MARKER_ABC_NOT_FOUND",
                    "new_content": "replacement"
                }
            ],
            "applied_hunks": [],
            "adapted_hunks": [],
            "refactored_hunks": [],
            "retry_contexts": [],
            "current_attempt": 1,
            "max_retries": 3,
            "tokens_used": 0,
            "wall_clock_time": 0.0,
            "status": "pending"
        }

        result_state = hunk_synthesizer_agent(state)

        assert len(result_state["synthesized_hunks"]) == 1
        assert result_state["synthesis_status"] == "partial"
        assert len(result_state["retry_contexts"]) >= 1
