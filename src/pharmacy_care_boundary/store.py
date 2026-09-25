"""药店健康陪伴边界的 SQLite 基础存储。"""
import sqlite3
from pathlib import Path

from .domain import Record


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS records (
                record_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK(revision > 0),
                created_at TEXT NOT NULL
            )
        """)
        self.connection.commit()

    def add(self, record: Record) -> Record:
        value = record.stamped()
        with self.connection:
            self.connection.execute(
                "INSERT INTO records(record_id, owner_id, state, revision, created_at) VALUES(?,?,?,?,?)",
                (value.record_id, value.owner_id, value.state, value.revision, value.created_at),
            )
        return value

    def get(self, record_id: str) -> Record | None:
        row = self.connection.execute(
            "SELECT record_id, owner_id, state, revision, created_at FROM records WHERE record_id=?",
            (record_id,),
        ).fetchone()
        return Record(**dict(row)) if row else None
