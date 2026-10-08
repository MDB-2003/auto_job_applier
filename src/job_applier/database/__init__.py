"""Read repository and domain unit-of-work contracts.

Domain writes require an internal capability, never provided to worker callers.
This is an API boundary, not a sandbox against malicious local Python code.
"""
from contextlib import AbstractContextManager
from typing import Protocol, TypeVar
from job_applier.models import Record

T = TypeVar('T', bound=Record)
_DOMAIN_WRITE = object()


class Repository(Protocol):
    def get(self, model: type[T], record_id: str) -> T: ...
    def list(self, model: type[T], *, limit: int = 100, offset: int = 0, **filters: object) -> list[T]: ...
    def count(self, model: type[T], **filters: object) -> int: ...
    def add(self, record: Record) -> None: ...
    def update(self, record: Record) -> None: ...


class Database(Protocol):
    def transaction(self, *, capability: object = None) -> AbstractContextManager[Repository]: ...


class NotFoundError(LookupError): pass
class ConflictError(ValueError): pass
