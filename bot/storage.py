"""SQLite owns per-user projects; Fernet protects tokens at rest."""
from .i18n import tr
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
            db.execute("CREATE TABLE IF NOT EXISTS preferences (owner INTEGER PRIMARY KEY, language TEXT NOT NULL CHECK(language IN ('fa','en')))")
            db.execute("""CREATE TABLE IF NOT EXISTS ip_changes (
                project TEXT NOT NULL, server INTEGER NOT NULL, owner INTEGER NOT NULL,
                family TEXT NOT NULL, old_ip INTEGER, new_ip INTEGER,
                PRIMARY KEY(project, server))""")
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
            raise ValueError(tr('نام پروژه باید بین ۱ تا ۶۰ حرف باشد.'))
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
            raise ValueError(tr('پروژه پیدا نشد یا اجازهٔ دسترسی ندارید.'))
        return row[0], self.cipher.decrypt(row[1]).decode()

    def remove(self, owner: int, project_id: str):
        with self.connect() as db:
            if db.execute('SELECT 1 FROM ip_changes WHERE owner=? AND project=?', (owner, project_id)).fetchone():
                raise ValueError(tr('ابتدا تغییر IP ناتمام این پروژه را بررسی کنید.'))
            db.execute("DELETE FROM projects WHERE owner=? AND id=?", (owner, project_id))

    def languages(self):
        with self.connect() as db:
            return dict(db.execute('SELECT owner, language FROM preferences').fetchall())

    def set_language(self, owner, language):
        if language not in {'fa', 'en'}:
            raise ValueError('Unsupported language')
        with self.connect() as db:
            db.execute('INSERT INTO preferences VALUES (?, ?) ON CONFLICT(owner) DO UPDATE SET language=excluded.language',
                       (owner, language))

    def begin_ip_change(self, owner, project, server, family, old_ip, new_ip):
        with self.connect() as db:
            db.execute('INSERT INTO ip_changes VALUES (?, ?, ?, ?, ?, ?)',
                       (project, server, owner, family, old_ip, new_ip))

    def ip_changes(self):
        with self.connect() as db:
            return db.execute('SELECT owner, project, server, family, old_ip, new_ip FROM ip_changes').fetchall()

    def finish_ip_change(self, project, server):
        with self.connect() as db:
            db.execute('DELETE FROM ip_changes WHERE project=? AND server=?', (project, server))
