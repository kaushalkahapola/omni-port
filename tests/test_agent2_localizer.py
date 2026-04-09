import pytest
from src.core.state import LocalizationResult, BackportState
from src.agents.agent2_localizer import localize_hunks
from src.localization.stage2_fuzzy import run_fuzzy_localization

def test_run_fuzzy_localization(tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    
    file_path = repo_path / "test.txt"
    # Notice we added an extra space to test fuzzy matching
    file_path.write_text("public void hello() {\n  System.out.println(\"world\");\n}\n")
    
    hunk = {"old_content": "public void hello() {\nSystem.out.println(\"world\");\n}"}
    
    result = run_fuzzy_localization(str(repo_path), "test.txt", hunk)
    
    assert result is not None
    assert result.method_used == "fuzzy"
    assert result.confidence >= 0.75
    assert result.start_line == 1

def test_localize_hunks_pipeline(tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    
    file_path = repo_path / "ExactMatch.java"
    file_path.write_text("class ExactMatch {\n  int x = 1;\n}\n")
    
    state = BackportState(
        patch_content="",
        target_repo_path=str(repo_path),
        target_branch="6.x",
        worktree_path=None,
        clean_state=True,
        classification=None,
        localization_results=[],
        hunks=[
            {
                "file_path": "ExactMatch.java",
                "old_content": "class ExactMatch {\n"
            }
        ],
        retry_contexts=[],
        current_attempt=1,
        max_retries=3,
        tokens_used=0,
        wall_clock_time=0.0,
        status="started"
    )
    
    new_state = localize_hunks(state)
    
    assert len(new_state["localization_results"]) == 1
    assert new_state["localization_results"][0].method_used == "git_exact"
    assert new_state["localization_results"][0].confidence == 1.0

