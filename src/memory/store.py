"""Small tenant-scoped temporal fact store for local validation.

This is an optional development component, not a replacement for the planned
Graphiti/Neo4j integration.  It stores only explicit facts with provenance and
validity windows.  Every read and write requires a tenant id and applies the
tenant predicate in SQL, so a same-named subject in another tenant is isolated.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
import sqlite3
import threading
from pathlib import Path
from typing import Iterable
from uuid import uuid4


def _utc(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class MemoryFact:
    fact_id: str
    tenant_id: str
    subject: str
    predicate: str
    object: str
    valid_from: datetime
    valid_to: datetime | None
    source: str


class MemoryStore(ABC):
    """Backend contract for objective, time-bounded facts."""

    @abstractmethod
    def write_fact(
        self,
        tenant_id: str,
        subject: str,
        predicate: str,
        object: str,
        *,
        source: str,
        valid_from: datetime | str | None = None,
    ) -> MemoryFact:
        raise NotImplementedError

    @abstractmethod
    def recall(
        self,
        tenant_id: str,
        subject: str,
        *,
        as_of: datetime | str | None = None,
        predicate: str | None = None,
    ) -> list[MemoryFact]:
        raise NotImplementedError

    @abstractmethod
    def retire(
        self,
        tenant_id: str,
        fact_id: str,
        *,
        retired_at: datetime | str | None = None,
    ) -> MemoryFact:
        raise NotImplementedError


class SqliteMemoryStore(MemoryStore):
    """SQLite implementation intended for local/demo use and focused tests."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_facts (
                fact_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object_value TEXT NOT NULL,
                valid_from TEXT NOT NULL,
                valid_to TEXT,
                source TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_facts_scope "
            "ON memory_facts (tenant_id, subject, valid_from)"
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _require_tenant(tenant_id: str) -> str:
        tenant_id = str(tenant_id or "").strip()
        if not tenant_id:
            raise ValueError("tenant_id is required")
        return tenant_id

    @staticmethod
    def _require_text(value: str, field: str) -> str:
        value = str(value or "").strip()
        if not value:
            raise ValueError(f"{field} is required")
        return value

    @staticmethod
    def _fact(row: sqlite3.Row) -> MemoryFact:
        return MemoryFact(
            fact_id=row["fact_id"],
            tenant_id=row["tenant_id"],
            subject=row["subject"],
            predicate=row["predicate"],
            object=row["object_value"],
            valid_from=_utc(row["valid_from"]),
            valid_to=_utc(row["valid_to"]) if row["valid_to"] else None,
            source=row["source"],
        )

    def write_fact(
        self,
        tenant_id: str,
        subject: str,
        predicate: str,
        object: str,
        *,
        source: str,
        valid_from: datetime | str | None = None,
    ) -> MemoryFact:
        tenant_id = self._require_tenant(tenant_id)
        subject = self._require_text(subject, "subject")
        predicate = self._require_text(predicate, "predicate")
        object = self._require_text(object, "object")
        source = self._require_text(source, "source")
        fact_id = str(uuid4())
        effective_from = _utc(valid_from)
        effective_iso = _iso(effective_from)
        with self._lock:
            # Keep intervals non-overlapping even when a fact is backfilled:
            # shorten the immediately preceding fact and bound the new fact
            # by the next known fact in the same tenant/subject/predicate.
            self._conn.execute("BEGIN")
            try:
                duplicate = self._conn.execute(
                    "SELECT 1 FROM memory_facts "
                    "WHERE tenant_id=? AND subject=? AND predicate=? AND valid_from=?",
                    (tenant_id, subject, predicate, effective_iso),
                ).fetchone()
                if duplicate is not None:
                    raise ValueError("同一租户、主体、谓词和生效时间的事实已存在")
                previous = self._conn.execute(
                    "SELECT fact_id, valid_to FROM memory_facts "
                    "WHERE tenant_id=? AND subject=? AND predicate=? AND valid_from<? "
                    "ORDER BY valid_from DESC LIMIT 1",
                    (tenant_id, subject, predicate, effective_iso),
                ).fetchone()
                next_row = self._conn.execute(
                    "SELECT valid_from FROM memory_facts "
                    "WHERE tenant_id=? AND subject=? AND predicate=? AND valid_from>? "
                    "ORDER BY valid_from ASC LIMIT 1",
                    (tenant_id, subject, predicate, effective_iso),
                ).fetchone()
                next_valid = next_row["valid_from"] if next_row else None
                if previous is not None and (
                    previous["valid_to"] is None or previous["valid_to"] > effective_iso
                ):
                    self._conn.execute(
                        "UPDATE memory_facts SET valid_to=? WHERE tenant_id=? AND fact_id=?",
                        (effective_iso, tenant_id, previous["fact_id"]),
                    )
                self._conn.execute(
                    "INSERT INTO memory_facts "
                    "(fact_id, tenant_id, subject, predicate, object_value, valid_from, valid_to, source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (fact_id, tenant_id, subject, predicate, object, effective_iso, next_valid, source),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            row = self._conn.execute(
                "SELECT * FROM memory_facts WHERE tenant_id=? AND fact_id=?",
                (tenant_id, fact_id),
            ).fetchone()
        return self._fact(row)

    # Friendly alias for callers that use "add" terminology.
    add_fact = write_fact

    def recall(
        self,
        tenant_id: str,
        subject: str,
        *,
        as_of: datetime | str | None = None,
        predicate: str | None = None,
    ) -> list[MemoryFact]:
        tenant_id = self._require_tenant(tenant_id)
        subject = self._require_text(subject, "subject")
        point = _iso(_utc(as_of))
        sql = (
            "SELECT * FROM memory_facts "
            "WHERE tenant_id=? AND subject=? AND valid_from<=? "
            "AND (valid_to IS NULL OR valid_to>?)"
        )
        args: list[str] = [tenant_id, subject, point, point]
        if predicate is not None:
            predicate = self._require_text(predicate, "predicate")
            sql += " AND predicate=?"
            args.append(predicate)
        sql += " ORDER BY valid_from DESC, fact_id"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._fact(row) for row in rows]

    def retire(
        self,
        tenant_id: str,
        fact_id: str,
        *,
        retired_at: datetime | str | None = None,
    ) -> MemoryFact:
        tenant_id = self._require_tenant(tenant_id)
        fact_id = self._require_text(fact_id, "fact_id")
        retired = _utc(retired_at)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_facts WHERE tenant_id=? AND fact_id=?",
                (tenant_id, fact_id),
            ).fetchone()
            if row is None:
                raise KeyError("memory fact not found")
            valid_from = _utc(row["valid_from"])
            if retired <= valid_from:
                raise ValueError("retired_at 必须晚于 valid_from")
            if row["valid_to"] is None or _utc(row["valid_to"]) > retired:
                self._conn.execute(
                    "UPDATE memory_facts SET valid_to=? WHERE tenant_id=? AND fact_id=?",
                    (_iso(retired), tenant_id, fact_id),
                )
                self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM memory_facts WHERE tenant_id=? AND fact_id=?",
                (tenant_id, fact_id),
            ).fetchone()
        return self._fact(row)


__all__ = ["MemoryFact", "MemoryStore", "SqliteMemoryStore"]
