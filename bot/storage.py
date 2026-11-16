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
            db.execute("""CREATE TABLE IF NOT EXISTS admins (
                owner INTEGER NOT NULL, admin INTEGER NOT NULL, name TEXT NOT NULL,
                PRIMARY KEY(owner, admin))""")
            db.execute("""CREATE TABLE IF NOT EXISTS project_grants (
                project TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                admin INTEGER NOT NULL, granted_by INTEGER NOT NULL,
                PRIMARY KEY(project, admin),
                FOREIGN KEY(granted_by, admin) REFERENCES admins(owner, admin) ON DELETE CASCADE)""")
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute('PRAGMA foreign_keys=ON')
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
            return db.execute("""SELECT p.id, p.name FROM projects p WHERE p.owner=? OR EXISTS (
                SELECT 1 FROM project_grants g JOIN admins a ON a.owner=g.granted_by AND a.admin=g.admin
                WHERE g.project=p.id AND g.admin=? AND g.granted_by=p.owner) ORDER BY p.name""",
                              (owner, owner)).fetchall()

    def owned_projects(self, owner):
        with self.connect() as db:
            return db.execute('SELECT id, name FROM projects WHERE owner=? ORDER BY name', (owner,)).fetchall()

    def granted_projects(self, actor, grantors=None):
        with self.connect() as db:
            rows = db.execute("""SELECT p.id, p.name, p.owner FROM projects p JOIN project_grants g ON g.project=p.id
                JOIN admins a ON a.owner=g.granted_by AND a.admin=g.admin
                WHERE g.admin=? AND g.granted_by=p.owner ORDER BY p.name""", (actor,)).fetchall()
        return [(pid, name) for pid, name, owner in rows if grantors is None or owner in grantors]

    def has_grant(self, actor, project):
        with self.connect() as db:
            return db.execute("""SELECT 1 FROM project_grants g JOIN projects p ON p.id=g.project
                JOIN admins a ON a.owner=g.granted_by AND a.admin=g.admin
                WHERE g.admin=? AND g.project=? AND g.granted_by=p.owner""", (actor, project)).fetchone() is not None

    def is_owner(self, owner, project):
        with self.connect() as db:
            return db.execute('SELECT 1 FROM projects WHERE owner=? AND id=?', (owner, project)).fetchone() is not None

    def project_owner(self, project):
        with self.connect() as db:
            row = db.execute('SELECT owner FROM projects WHERE id=?', (project,)).fetchone()
        if row is None:
            raise ValueError(tr('پروژه پیدا نشد یا اجازهٔ دسترسی ندارید.'))
        return row[0]

    def get(self, owner: int, project_id: str):
        with self.connect() as db:
            row = db.execute("""SELECT p.name, p.token FROM projects p WHERE p.id=? AND (p.owner=? OR EXISTS (
                SELECT 1 FROM project_grants g JOIN admins a ON a.owner=g.granted_by AND a.admin=g.admin
                WHERE g.project=p.id AND g.admin=? AND g.granted_by=p.owner))""",
                             (project_id, owner, owner)).fetchone()
        if row is None:
            raise ValueError(tr('پروژه پیدا نشد یا اجازهٔ دسترسی ندارید.'))
        return row[0], self.cipher.decrypt(row[1]).decode()

    def remove(self, owner: int, project_id: str):
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM projects WHERE owner=? AND id=?', (owner, project_id)).fetchone():
                return
            if db.execute('SELECT 1 FROM ip_changes WHERE project=?', (project_id,)).fetchone():
                raise ValueError(tr('ابتدا تغییر IP ناتمام این پروژه را بررسی کنید.'))
            db.execute("DELETE FROM projects WHERE owner=? AND id=?", (owner, project_id))

    def admin_ids(self, owners=None):
        with self.connect() as db:
            return {admin for owner, admin in db.execute('SELECT owner, admin FROM admins')
                    if owners is None or owner in owners}

    def list_admins(self, owner):
        with self.connect() as db:
            return db.execute('SELECT admin, name FROM admins WHERE owner=? ORDER BY name, admin', (owner,)).fetchall()

    def add_admin(self, owner, admin, name):
        if not 0 < admin <= 2**63-1 or admin == owner or not name.strip() or len(name) > 60:
            raise ValueError(tr('شناسه یا نام ادمین معتبر نیست.'))
        with self.connect() as db:
            db.execute('INSERT INTO admins VALUES (?, ?, ?)', (owner, admin, name.strip()))

    def assigned_projects(self, owner, admin):
        with self.connect() as db:
            return {row[0] for row in db.execute("""SELECT g.project FROM project_grants g
                JOIN projects p ON p.id=g.project WHERE g.granted_by=? AND g.admin=? AND p.owner=?""",
                                              (owner, admin, owner))}

    def replace_grants(self, owner, admin, projects):
        projects = set(projects)
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM admins WHERE owner=? AND admin=?', (owner, admin)).fetchone():
                raise ValueError(tr('ادمین پیدا نشد یا اجازهٔ مدیریت آن را ندارید.'))
            owned = {row[0] for row in db.execute('SELECT id FROM projects WHERE owner=?', (owner,))}
            if not projects <= owned:
                raise ValueError(tr('فقط پروژه‌های متعلق به خودتان را می‌توانید اختصاص دهید.'))
            db.execute('DELETE FROM project_grants WHERE granted_by=? AND admin=?', (owner, admin))
            db.executemany('INSERT INTO project_grants VALUES (?, ?, ?)', [(pid, admin, owner) for pid in projects])

    def remove_admin(self, owner, admin):
        with self.connect() as db:
            db.execute('DELETE FROM admins WHERE owner=? AND admin=?', (owner, admin))

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
