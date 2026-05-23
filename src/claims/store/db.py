"""Connection helper around stdlib sqlite3.

Centralizes the PRAGMAs every connection needs (foreign keys on,
row factory for dict-like access). Callers get a Connection and
manage transactions however they like — no implicit auto-commit
games."""

import sqlite3
from sqlite3 import Connection


def connect(path: str) -> Connection:
    """Open a SQLite connection with the project's defaults.

    `path` may be a filesystem path or `":memory:"` for tests.
    """
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn
