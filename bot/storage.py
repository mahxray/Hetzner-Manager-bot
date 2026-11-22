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
            db.execute("CREATE TABLE IF NOT EXISTS price_preferences (owner INTEGER PRIMARY KEY, currency TEXT NOT NULL CHECK(currency IN ('EUR','IRR')))")
            db.execute("""CREATE TABLE IF NOT EXISTS ip_changes (
                project TEXT NOT NULL, server INTEGER NOT NULL, owner INTEGER NOT NULL,
                family TEXT NOT NULL, old_ip INTEGER, new_ip INTEGER,
                server_was_running INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(project, server))""")
            columns = {row[1] for row in db.execute('PRAGMA table_info(ip_changes)')}
            if 'server_was_running' not in columns:
                db.execute('ALTER TABLE ip_changes ADD COLUMN server_was_running INTEGER NOT NULL DEFAULT 0')
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

    def backup(self, destination, owner):
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(self.path, timeout=10)
        output = sqlite3.connect(target, timeout=10)
        try:
            source.backup(output)
        finally:
            output.close()
            source.close()
        with sqlite3.connect(target) as db:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('DELETE FROM project_grants WHERE granted_by<>? OR project NOT IN (SELECT id FROM projects WHERE owner=?)',
                       (owner, owner))
            db.execute('DELETE FROM ip_changes')
            db.execute('DELETE FROM admins WHERE owner<>?', (owner,))
            db.execute('DELETE FROM projects WHERE owner<>?', (owner,))
            db.execute('DELETE FROM preferences WHERE owner<>?', (owner,))
            db.execute('DELETE FROM price_preferences WHERE owner<>?', (owner,))
        if os.name != "nt":
            target.chmod(0o600)

    def validate_backup(self, source_path, owner):
        source = sqlite3.connect(f"file:{Path(source_path).resolve().as_posix()}?mode=ro", uri=True)
        try:
            if source.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise ValueError(tr('فایل پشتیبان سالم نیست.'))
            tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {'projects', 'preferences', 'price_preferences', 'ip_changes', 'admins', 'project_grants'}
            if not required <= tables:
                raise ValueError(tr('این فایل، پشتیبان سازگار بات نیست.'))
            projects = source.execute('SELECT id, owner, name, token FROM projects').fetchall()
            for _, row_owner, name, token in projects:
                if not isinstance(row_owner, int) or not isinstance(name, str) or not name or not isinstance(token, bytes):
                    raise ValueError(tr('اطلاعات پروژه در فایل پشتیبان معتبر نیست.'))
                self.cipher.decrypt(token)
            owners = [row[1] for row in projects]
            owners += [row[0] for row in source.execute('SELECT owner FROM preferences')]
            owners += [row[0] for row in source.execute('SELECT owner FROM price_preferences')]
            owners += [row[2] for row in source.execute('SELECT project, server, owner FROM ip_changes')]
            owners += [row[0] for row in source.execute('SELECT owner FROM admins')]
            owners += [row[2] for row in source.execute('SELECT project, admin, granted_by FROM project_grants')]
            if any(value != owner for value in owners):
                raise ValueError(tr('پشتیبان متعلق به مالک دیگری است.'))
            return {table: source.execute(f'SELECT * FROM "{table}"').fetchall()
                    for table in required}
        except (sqlite3.Error, TypeError) as error:
            raise ValueError(tr('فایل پشتیبان قابل خواندن یا معتبر نیست.')) from error
        finally:
            source.close()

    def restore(self, source_path, owner):
        tables = self.validate_backup(source_path, owner)
        with self.connect() as db:
            if db.execute('SELECT 1 FROM ip_changes WHERE owner=? LIMIT 1', (owner,)).fetchone():
                raise ValueError(tr('ابتدا تغییر IP ناتمام را بازیابی کنید؛ سپس پشتیبان را برگردانید.'))
            db.execute('DELETE FROM project_grants WHERE granted_by=? OR project IN (SELECT id FROM projects WHERE owner=?)',
                       (owner, owner))
            db.execute('DELETE FROM ip_changes WHERE owner=? OR project IN (SELECT id FROM projects WHERE owner=?)',
                       (owner, owner))
            db.execute('DELETE FROM admins WHERE owner=?', (owner,))
            db.execute('DELETE FROM projects WHERE owner=?', (owner,))
            db.execute('DELETE FROM preferences WHERE owner=?', (owner,))
            db.execute('DELETE FROM price_preferences WHERE owner=?', (owner,))
            for table in ('projects', 'preferences', 'price_preferences', 'ip_changes', 'admins', 'project_grants'):
                if tables[table]:
                    columns = len(tables[table][0])
                    db.executemany(f'INSERT INTO "{table}" VALUES ({",".join("?" for _ in range(columns))})',
                                   tables[table])

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

    def display_currency(self, owner):
        with self.connect() as db:
            row = db.execute('SELECT currency FROM price_preferences WHERE owner=?', (owner,)).fetchone()
        return row[0] if row else None

    def set_display_currency(self, owner, currency):
        if currency not in {'EUR', 'IRR'}:
            raise ValueError('Unsupported display currency')
        with self.connect() as db:
            db.execute('INSERT INTO price_preferences VALUES (?, ?) ON CONFLICT(owner) DO UPDATE SET currency=excluded.currency',
                       (owner, currency))

    def begin_ip_change(self, owner, project, server, family, old_ip, new_ip, server_was_running=False):
        with self.connect() as db:
            db.execute('INSERT INTO ip_changes (project, server, owner, family, old_ip, new_ip, server_was_running) VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (project, server, owner, family, old_ip, new_ip, bool(server_was_running)))

    def ip_changes(self):
        with self.connect() as db:
            return db.execute('SELECT owner, project, server, family, old_ip, new_ip, server_was_running FROM ip_changes').fetchall()

    def finish_ip_change(self, project, server):
        with self.connect() as db:
            db.execute('DELETE FROM ip_changes WHERE project=? AND server=?', (project, server))
