"""``vec_chunks`` factory: sqlite-vec real path (skipped if the extension
package isn't importable in this environment) plus the pure-stdlib
fallback path, forced via the ``_force_fallback`` test seam so it's
exercised even on a machine that DOES have sqlite-vec installed — "tests
pass on a machine without the extension" per the build brief.
"""

from __future__ import annotations

import sqlite3

import pytest

from trialerror.stores.vecindex import (
    VecBackend,
    deserialize_vector_fallback,
    ensure_vec_table,
    serialize_vector_fallback,
    try_load_sqlite_vec,
    vec_table_name,
)


def _sqlite_vec_available() -> bool:
    conn = sqlite3.connect(":memory:")
    try:
        return try_load_sqlite_vec(conn)
    finally:
        conn.close()


def test_vec_table_name_is_identifier_safe():
    assert vec_table_name("qwen3-embedding-4b") == "vec_chunks__qwen3_embedding_4b"
    assert vec_table_name("weird key!!") == "vec_chunks__weird_key__"


def test_registry_bookkeeping_records_backend_and_dims():
    conn = sqlite3.connect(":memory:")
    ensure_vec_table(conn, "model-a", 16, _force_fallback=True)
    row = conn.execute("SELECT * FROM vec_index_registry WHERE model_key = 'model-a'").fetchone()
    assert row[1] == "vec_chunks__model_a"  # table_name
    assert row[2] == 16  # dims
    assert row[3] == "fallback"  # backend


def test_ensure_vec_table_is_idempotent():
    conn = sqlite3.connect(":memory:")
    b1 = ensure_vec_table(conn, "model-a", 8, _force_fallback=True)
    b2 = ensure_vec_table(conn, "model-a", 8, _force_fallback=True)
    assert b1 == b2 == VecBackend.FALLBACK
    count = conn.execute("SELECT COUNT(*) FROM vec_index_registry").fetchone()[0]
    assert count == 1  # re-registering the same model_key updates, doesn't duplicate


def test_fallback_backend_forced_even_with_extension_available():
    """Proves the fallback code path works on its own merits, independent
    of whether this particular environment happens to have sqlite-vec."""
    conn = sqlite3.connect(":memory:")
    backend = ensure_vec_table(conn, "any-model", 4, _force_fallback=True)
    assert backend == VecBackend.FALLBACK
    table = vec_table_name("any-model")
    blob = serialize_vector_fallback([0.5, -0.25, 1.0, 0.0])
    conn.execute(
        f"INSERT INTO {table}(chunk_id, model_key, dims, vector) VALUES (?,?,?,?)",
        ("CHK-1", "any-model", 4, blob),
    )
    conn.commit()
    row = conn.execute(f"SELECT vector FROM {table} WHERE chunk_id = ?", ("CHK-1",)).fetchone()
    values = deserialize_vector_fallback(row[0])
    assert values == pytest.approx([0.5, -0.25, 1.0, 0.0])


def test_try_load_sqlite_vec_never_raises_when_package_missing(monkeypatch):
    """The probe must degrade to False, never propagate an exception, even
    if the import machinery misbehaves."""
    import builtins

    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "sqlite_vec":
            raise ImportError("simulated: sqlite_vec not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    conn = sqlite3.connect(":memory:")
    assert try_load_sqlite_vec(conn) is False


@pytest.mark.skipif(not _sqlite_vec_available(), reason="sqlite-vec extension not installed in this environment")
def test_real_sqlite_vec_backend_creates_working_vec0_table(monkeypatch):
    monkeypatch.setenv("TRIALERROR_VEC_BACKEND", "sqlite_vec")  # B.4a: vec0 is opt-in now
    conn = sqlite3.connect(":memory:")
    backend = ensure_vec_table(conn, "qwen3-4b", 8)
    assert backend == VecBackend.SQLITE_VEC

    import sqlite_vec

    table = vec_table_name("qwen3-4b")
    conn.execute(
        f"INSERT INTO {table}(chunk_id, vector) VALUES (?, ?)",
        ("CHK-1", sqlite_vec.serialize_float32([0.1] * 8)),
    )
    rows = conn.execute(f"SELECT chunk_id FROM {table}").fetchall()
    assert [r[0] for r in rows] == ["CHK-1"]
