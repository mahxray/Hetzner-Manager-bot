"""SQLite owns per-user projects; Fernet protects tokens at rest."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet


class Store:
    def __init__(self, directory: str, key: str):
        self.cipher = Fernet(key.encode())
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / "projects.db"
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, owner INTEGER NOT NULL, name TEXT NOT NULL,
                token BLOB NOT NULL, UNIQUE(owner, name))""")
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def add(self, owner: int, name: str, token: str) -> str:
        if not name.strip() or len(name) > 60:
            raise ValueError("نام پروژه باید بین ۱ تا ۶۰ حرف باشد.")
        project_id = uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO projects VALUES (?, ?, ?, ?)",
                       (project_id, owner, name.strip(), self.cipher.encrypt(token.encode())))
        return project_id

    def list(self, owner: int):
        with self.connect() as db:
            return db.execute("SELECT id, name FROM projects WHERE owner=? ORDER BY name",
                              (owner,)).fetchall()

    def get(self, owner: int, project_id: str):
        with self.connect() as db:
            row = db.execute("SELECT name, token FROM projects WHERE owner=? AND id=?",
                             (owner, project_id)).fetchone()
        if row is None:
            raise ValueError("پروژه پیدا نشد یا اجازهٔ دسترسی ندارید.")
        return row[0], self.cipher.decrypt(row[1]).decode()

    def remove(self, owner: int, project_id: str):
        with self.connect() as db:
            db.execute("DELETE FROM projects WHERE owner=? AND id=?", (owner, project_id))
