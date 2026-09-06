from datetime import datetime, timezone

import pytest

from src.memory import SqliteMemoryStore


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def test_temporal_recall_and_retire_are_tenant_scoped(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    try:
        old = store.write_fact(
            "TENANT-A", "USER-1", "lives_at", "旧地址",
            source="ticket:T-1", valid_from=dt("2026-01-01T00:00:00+00:00"),
        )
        current = store.write_fact(
            "TENANT-A", "USER-1", "lives_at", "新地址",
            source="ticket:T-2", valid_from=dt("2026-06-01T00:00:00+00:00"),
        )
        other = store.write_fact(
            "TENANT-B", "USER-1", "lives_at", "租户B地址",
            source="ticket:B-1", valid_from=dt("2026-01-01T00:00:00+00:00"),
        )

        assert [f.object for f in store.recall("TENANT-A", "USER-1", as_of=dt("2026-02-01T00:00:00+00:00"))] == ["旧地址"]
        assert [f.object for f in store.recall("TENANT-A", "USER-1", as_of=dt("2026-07-01T00:00:00+00:00"))] == ["新地址"]
        assert [f.object for f in store.recall("TENANT-B", "USER-1", as_of=dt("2026-07-01T00:00:00+00:00"))] == ["租户B地址"]
        assert store.recall("TENANT-A", "USER-1", predicate="status") == []

        retired = store.retire("TENANT-A", current.fact_id, retired_at=dt("2026-08-01T00:00:00+00:00"))
        assert retired.valid_to == dt("2026-08-01T00:00:00+00:00")
        assert store.recall("TENANT-A", "USER-1", as_of=dt("2026-09-01T00:00:00+00:00")) == []
        assert store.recall("TENANT-B", "USER-1", as_of=dt("2026-09-01T00:00:00+00:00"))[0].fact_id == other.fact_id
        assert old.tenant_id == "TENANT-A"
    finally:
        store.close()


def test_missing_tenant_or_fact_fails_closed(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    try:
        with pytest.raises(ValueError):
            store.recall("", "USER-1")
        with pytest.raises(ValueError):
            store.write_fact("TENANT-A", "USER-1", "status", "", source="test")
        with pytest.raises(KeyError):
            store.retire("TENANT-A", "missing")
        fact = store.write_fact("TENANT-A", "USER-1", "status", "delivered", source="test",
                                valid_from=dt("2026-06-01T00:00:00+00:00"))
        with pytest.raises(ValueError, match="retired_at"):
            store.retire("TENANT-A", fact.fact_id, retired_at=dt("2026-05-01T00:00:00+00:00"))
        with pytest.raises(ValueError, match="retired_at"):
            store.retire("TENANT-A", fact.fact_id, retired_at=dt("2026-06-01T00:00:00+00:00"))
    finally:
        store.close()


def test_backfilled_fact_splits_existing_interval(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    try:
        old = store.write_fact("TENANT-A", "USER-1", "status", "old", source="t1",
                               valid_from=dt("2026-01-01T00:00:00+00:00"))
        current = store.write_fact("TENANT-A", "USER-1", "status", "current", source="t2",
                                   valid_from=dt("2026-06-01T00:00:00+00:00"))
        inserted = store.write_fact("TENANT-A", "USER-1", "status", "backfilled", source="t3",
                                    valid_from=dt("2026-03-01T00:00:00+00:00"))
        assert store.recall("TENANT-A", "USER-1", as_of=dt("2026-02-01T00:00:00+00:00"))[0].fact_id == old.fact_id
        assert store.recall("TENANT-A", "USER-1", as_of=dt("2026-04-01T00:00:00+00:00"))[0].fact_id == inserted.fact_id
        assert store.recall("TENANT-A", "USER-1", as_of=dt("2026-07-01T00:00:00+00:00"))[0].fact_id == current.fact_id
        assert inserted.valid_to == dt("2026-06-01T00:00:00+00:00")
    finally:
        store.close()
