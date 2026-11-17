"""Versioned local SQLite schema. No network or provider calls."""
import datetime as dt
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import time


MIGRATIONS = Path(__file__).resolve().parent / 'migrations'
SCHEMA_VERSION = max((int(p.name[:3]) for p in MIGRATIONS.glob('[0-9][0-9][0-9]_*.sql')),default=0)
LOGICAL_TABLES = ('source_events','conversation_turns','utterances','memory_items',
                  'memory_sources','action_events','api_usage')


class Connection:
    """A short transaction on its own connection; settings apply to every reader/writer."""
    def __init__(self,path,*,readonly=False):
        target=Path(path).absolute().as_uri()+'?mode=ro' if readonly else path
        self.db=sqlite3.connect(target,timeout=5,uri=readonly)
        try:
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('PRAGMA busy_timeout=5000')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA recursive_triggers=ON')
            self.db.execute('PRAGMA secure_delete=ON')
        except BaseException:
            self.db.close();raise

    def __enter__(self):return self.db

    def __exit__(self,kind,*_):
        try:
            if kind:self.db.rollback()
            else:self.db.commit()
        finally:self.db.close()


def statements(script):
    statement=''
    for line in script.splitlines(keepends=True):
        statement+=line
        if sqlite3.complete_statement(statement):
            yield statement;statement=''
    if statement.strip():raise ValueError('INCOMPLETE_MIGRATION')


class Database:
    def __init__(self,path,*,migrations=MIGRATIONS):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        flags=os.O_RDWR|os.O_NOFOLLOW
        try:fd=os.open(self.path,flags|os.O_CREAT|os.O_EXCL,0o660)
        except FileExistsError:fd=os.open(self.path,flags)
        try:
            if os.fstat(fd).st_uid==os.geteuid():os.fchmod(fd,0o660)
            if os.fstat(fd).st_mode & 0o007:raise ValueError('DATABASE_MUST_NOT_BE_WORLD_ACCESSIBLE')
        finally:os.close(fd)
        self.migrate(Path(migrations))

    @classmethod
    def existing(cls,path,*,readonly=True):
        """Open an existing schema for inspection/backup, without creating or migrating it."""
        instance=cls.__new__(cls);instance.path=Path(path);instance.readonly=readonly
        fd=os.open(instance.path,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            if os.fstat(fd).st_mode & 0o007:raise ValueError('DATABASE_MUST_NOT_BE_WORLD_ACCESSIBLE')
        finally:os.close(fd)
        return instance

    def connect(self):return Connection(self.path,readonly=getattr(self,'readonly',False))

    def migrate(self,directory):
        files=sorted(directory.glob('[0-9][0-9][0-9]_*.sql'))
        if not files:raise ValueError('MIGRATIONS_MISSING')
        with self.connect() as db:
            # SQLite may return BUSY immediately while another connection changes
            # the journal mode, even with busy_timeout. Bound only this local setup retry.
            deadline=time.monotonic()+5
            while True:
                try:
                    if db.execute('PRAGMA journal_mode=WAL').fetchone()[0]!='wal':raise ValueError('WAL_REQUIRED')
                    break
                except sqlite3.OperationalError as exc:
                    if (getattr(exc,'sqlite_errorcode',0)&255) not in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED) or time.monotonic()>=deadline:raise
                    time.sleep(.025)
            # Recheck the version only after acquiring the single writer lock.
            db.execute('BEGIN IMMEDIATE')
            current_version=db.execute('PRAGMA user_version').fetchone()[0]
            db.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY,name TEXT NOT NULL,sha256 TEXT NOT NULL,applied_at REAL NOT NULL)')
            applied={v:(n,h) for v,n,h in db.execute('SELECT version,name,sha256 FROM schema_migrations')}
            available={int(p.name[:3]) for p in files}
            if set(applied)-available or current_version>max(available):raise ValueError('DATABASE_SCHEMA_NEWER_THAN_SOURCE')
            if current_version!=max(applied,default=0):raise ValueError('DATABASE_MIGRATION_VERSION_MISMATCH')
            if sorted(available)!=list(range(1,len(files)+1)):raise ValueError('MIGRATION_SEQUENCE_INVALID')
            for p in files:
                version=int(p.name[:3]);script=p.read_text();digest=hashlib.sha256(script.encode()).hexdigest()
                if version in applied:
                    if applied[version]!=(p.name,digest):raise ValueError('APPLIED_MIGRATION_CHANGED')
                    continue
                for statement in statements(script):db.execute(statement)
                db.execute('INSERT INTO schema_migrations VALUES(?,?,?,?)',(version,p.name,digest,time.time()))
            db.execute('PRAGMA user_version='+str(max(available)))
            if db.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('MIGRATION_FOREIGN_KEY_FAILURE')

    def status(self):
        with self.connect() as db:
            result={name:db.execute('PRAGMA '+name).fetchone()[0]
                    for name in ('user_version','journal_mode','foreign_keys','busy_timeout','synchronous')}
            present={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            result['tables']={name:db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] if name in present else None for name in LOGICAL_TABLES}
            result['foreign_key_errors']=len(db.execute('PRAGMA foreign_key_check').fetchall())
            result['integrity_check']=[row[0] for row in db.execute('PRAGMA integrity_check')]
            result['migration_versions']=[v for (v,) in db.execute('SELECT version FROM schema_migrations ORDER BY version')] if 'schema_migrations' in present else []
        return result

    def backup(self,destination):
        destination=Path(destination)
        destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.close(fd)
        try:
            # Online backup includes committed WAL pages and leaves source journaling alone.
            with self.connect() as source, closing(sqlite3.connect(destination)) as target:
                source.backup(target,pages=128,sleep=.01)
                if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('BACKUP_INTEGRITY_FAILED')
                if target.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('BACKUP_FOREIGN_KEY_FAILURE')
        except BaseException:
            # Preserve the failed copy for diagnosis; never overwrite an earlier backup.
            raise
        return {'file':destination.name,'bytes':destination.stat().st_size,
                'sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),
                'created_at':dt.datetime.now(dt.timezone.utc).isoformat()}
