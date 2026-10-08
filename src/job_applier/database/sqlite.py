"""SQLite adapter: bounded SQL queries and capability-scoped domain writes."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator, TypeVar
from job_applier.database import ConflictError, NotFoundError, _DOMAIN_WRITE
from job_applier.database.schema import TABLES, CURRENT_VERSION
from job_applier.models import Record, AuditEvent
from job_applier.utilities.serialization import decode, encode

T = TypeVar('T', bound=Record)


class SQLiteRepository:
    def __init__(self, connection: sqlite3.Connection, *, capability: object = None):
        self._connection = connection
        self._writable = capability is _DOMAIN_WRITE

    def get(self, model: type[T], record_id: str) -> T:
        table, _ = TABLES[model]
        row = self._connection.execute(f'SELECT payload FROM {table} WHERE id=?', (record_id,)).fetchone()
        if row is None: raise NotFoundError('record_not_found')
        return decode(model, row[0])

    def _where(self, model: type[T], filters: dict) -> tuple[str, list]:
        _, projections = TABLES[model]
        if set(filters) - set(model.__dataclass_fields__): raise ValueError('unknown_filter')
        expressions, parameters = [], []
        for name, value in filters.items():
            expr = name if name in ('id','revision', *projections) else f"json_extract(payload, '$.{name}')"
            expressions.append(expr + ' IS ?')
            parameters.append(value)
        return (' WHERE ' + ' AND '.join(expressions) if expressions else ''), parameters

    def list(self, model: type[T], *, limit: int = 100, offset: int = 0, **filters: object) -> list[T]:
        if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or offset < 0:
            raise ValueError('invalid_pagination')
        table, _ = TABLES[model]
        where, values = self._where(model, filters)
        rows = self._connection.execute(f'SELECT payload FROM {table}{where} ORDER BY rowid LIMIT ? OFFSET ?', (*values,limit,offset))
        return [decode(model,row[0]) for row in rows]

    def count(self, model: type[T], **filters: object) -> int:
        table, _ = TABLES[model]
        where, values = self._where(model, filters)
        return self._connection.execute(f'SELECT COUNT(*) FROM {table}{where}',values).fetchone()[0]

    def add(self, record: Record) -> None:
        if not self._writable: raise PermissionError('domain_service_required')
        table, projections = TABLES[type(record)]
        names = ('id','revision','payload',*projections)
        values = (record.id,record.revision,encode(record),*(getattr(record,f) for f in projections))
        try:
            self._connection.execute(f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",values)
        except sqlite3.IntegrityError as exc:
            raise ConflictError('duplicate_or_invalid_relationship') from exc

    def update(self, record: Record) -> None:
        if not self._writable: raise PermissionError('domain_service_required')
        if isinstance(record, AuditEvent): raise ConflictError('audit_append_only')
        table, projections = TABLES[type(record)]
        names = ('revision','payload',*projections)
        values = (record.revision,encode(record),*(getattr(record,f) for f in projections),record.id,record.revision-1)
        try:
            cursor = self._connection.execute(f"UPDATE {table} SET {','.join(f'{n}=?' for n in names)} WHERE id=? AND revision=?",values)
        except sqlite3.IntegrityError as exc:
            raise ConflictError('duplicate_or_invalid_relationship') from exc
        if cursor.rowcount != 1: raise ConflictError('stale_revision')


class SQLiteDatabase:
    def __init__(self, path: Path): self.path = Path(path)

    def _connect(self, *, writable: bool = False) -> sqlite3.Connection:
        target = self.path if writable else self.path.resolve().as_uri() + '?mode=ro'
        connection = sqlite3.connect(target, uri=not writable, timeout=10, isolation_level=None)
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA recursive_triggers=ON')
        connection.create_function('domain_write_allowed',0,lambda: int(writable))
        if not writable: connection.execute('PRAGMA query_only=ON')
        return connection

    def initialize(self) -> None:
        from job_applier.database.migrations import migrate
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect(writable=True)
        try: migrate(connection)
        finally: connection.close()

    @contextmanager
    def transaction(self, *, capability: object = None) -> Iterator[SQLiteRepository]:
        writable = capability is _DOMAIN_WRITE
        connection = self._connect(writable=writable)
        try:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version != CURRENT_VERSION: raise RuntimeError('database_migration_required')
            connection.execute('BEGIN IMMEDIATE' if writable else 'BEGIN')
            yield SQLiteRepository(connection,capability=capability)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally: connection.close()
