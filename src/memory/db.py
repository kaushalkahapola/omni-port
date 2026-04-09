import sqlite3
import os
from contextlib import contextmanager
from typing import List, Dict, Any


class MemoryDB:
    """
    Manages SQLite database for PatchLesson schema as part of the Memory Manager.
    """

    def __init__(self, db_path: str = "omniport_memory.db"):
        self.db_path = os.path.abspath(db_path)
        self._initialize_schema()

    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.commit()
            conn.close()

    def _initialize_schema(self):
        schema = """
        CREATE TABLE IF NOT EXISTS PatchLesson (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_name TEXT NOT NULL,
            source_version TEXT,
            target_version TEXT,
            patch_type TEXT,
            original_symbol TEXT,
            new_symbol TEXT,
            description TEXT,
            success_count INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'NOW'))
        );
        
        CREATE INDEX IF NOT EXISTS idx_repo_symbols on PatchLesson(repo_name, original_symbol);
        """
        with self.get_connection() as conn:
            conn.executescript(schema)

    def insert_lesson(self, lesson_data: Dict[str, Any]) -> int:
        """
        Inserts a consolidated lesson learned into the database.
        """
        query = """
        INSERT INTO PatchLesson (repo_name, source_version, target_version, 
                                 patch_type, original_symbol, new_symbol, description)
        VALUES (:repo_name, :source_version, :target_version, :patch_type, 
                :original_symbol, :new_symbol, :description)
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, lesson_data)
            return cursor.lastrowid

    def get_lessons_for_repo(
        self, repo_name: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM PatchLesson WHERE repo_name = ? ORDER BY created_at DESC LIMIT ?"
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, (repo_name, limit))
            return [dict(row) for row in cursor.fetchall()]


# Default singleton instance
db = MemoryDB()
