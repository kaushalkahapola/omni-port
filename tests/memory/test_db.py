import pytest
import sqlite3
import os
from src.memory.db import MemoryDB


@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "test_memory.db"
    db = MemoryDB(str(db_file))
    yield db
    if db_file.exists():
        os.remove(db_file)


def test_insert_and_retrieve_lesson(test_db):
    lesson = {
        "repo_name": "elasticsearch",
        "source_version": "7.x",
        "target_version": "6.x",
        "patch_type": "TYPE_III",
        "original_symbol": "ActionListener.wrap()",
        "new_symbol": "ActionListener.toBiConsumer()",
        "description": "API signature changed in 6.8 for async wrappers",
    }

    lesson_id = test_db.insert_lesson(lesson)
    assert lesson_id > 0

    lessons = test_db.get_lessons_for_repo("elasticsearch")
    assert len(lessons) == 1
    retrieved = lessons[0]

    assert retrieved["original_symbol"] == "ActionListener.wrap()"
    assert retrieved["new_symbol"] == "ActionListener.toBiConsumer()"
    assert retrieved["patch_type"] == "TYPE_III"
    assert retrieved["success_count"] == 1


def test_retrieve_empty_repo(test_db):
    lessons = test_db.get_lessons_for_repo("non_existent_repo")
    assert len(lessons) == 0


def test_ordering_and_limit(test_db):
    import time

    for i in range(10):
        test_db.insert_lesson(
            {
                "repo_name": "kibana",
                "source_version": "v1",
                "target_version": "v2",
                "patch_type": "TYPE_I",
                "original_symbol": f"sym_{i}",
                "new_symbol": "new_sym",
                "description": "test",
            }
        )
        time.sleep(0.01)  # Ensure timestamps are distinctly different

    lessons = test_db.get_lessons_for_repo("kibana", limit=5)
    assert len(lessons) == 5
    # Since they are ordered by created_at DESC, the most recent should be sym_9
    assert lessons[0]["original_symbol"] == "sym_9"
    assert lessons[4]["original_symbol"] == "sym_5"
