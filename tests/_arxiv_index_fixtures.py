"""Shared synthetic-fixture helpers for the ``trialerror.arxiv_index`` test
suite (mirrors ``tests/_ingest_fixtures.py``/``tests/_litapi_fixtures.py``'s
own "small shared helper module, not a conftest fixture" convention --
these are used by several test files with different parametrizations, not
one autouse fixture)."""

from __future__ import annotations

import json
import struct
import zipfile
from pathlib import Path
from typing import Any


def deterministic_vector(seed: int, dims: int) -> list[float]:
    """A cheap, deterministic, non-random unit-ish vector -- distinct
    ``seed``s produce distinguishable vectors so nearest-neighbor ordering
    in a synthetic fixture is meaningful (not coincidental collisions),
    without pulling in ``trialerror.arxiv_index.encoder.FakeQueryEncoder``'s
    sha256 machinery (that IS separately exercised by its own test file --
    this helper is intentionally simpler/faster for building N-row zip
    fixtures)."""
    raw = [((seed * 7 + i * 13) % 101) / 50.0 - 1.0 for i in range(dims)]
    norm = sum(v * v for v in raw) ** 0.5 or 1.0
    return [v / norm for v in raw]


def make_record(i: int, *, dims: int = 8, id_prefix: str = "9999") -> dict[str, Any]:
    return {
        "id": f"{id_prefix}.{i:05d}",
        "title": f"Synthetic Paper {i}",
        "abstract": f"This is a synthetic abstract for fixture paper number {i}.",
        "categories": "cs.AI cs.LG",
        "authors": "A. Fixture, B. Synthetic",
        "update_date": "2026-01-01",
        "doi": None,
        "journal-ref": None,
        "embedding": deterministic_vector(i, dims),
    }


def write_records_zip(zip_path: Path, members: dict[str, list[dict[str, Any]]]) -> Path:
    """``members`` = ``{member_name: [record, ...]}`` -- each record
    written as one JSON-line (the ASSUMED ``.jsonl`` shape, see
    ``trialerror/arxiv_index/__init__.py``'s module docstring)."""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for member_name, records in members.items():
            body = "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"
            zf.writestr(member_name, body)
    return zip_path


def write_small_fixture_zip(zip_path: Path, *, n: int = 12, dims: int = 8, member_name: str = "shard-0000.jsonl") -> Path:
    records = [make_record(i, dims=dims) for i in range(n)]
    return write_records_zip(zip_path, {member_name: records})


def make_csv_dat_records(n: int, *, dims: int = 8, id_prefix: str = "9999", journal_every: int = 3) -> list[dict[str, Any]]:
    """``n`` synthetic ``(index, id, journal, vector)`` rows for the REAL
    Kaggle zip's own confirmed csv+dat layout (``trialerror.arxiv_index.ingest``
    module docstring: ``papers.csv`` header ``index,id,journal`` + a
    ``vectors.dat`` member of raw concatenated float32 rows, row ``i``
    aligned to csv row ``i``). Every ``journal_every``-th row gets a
    non-empty journal tag, the rest empty (round-trips to
    ``journal_ref IS NULL`` -- see
    ``trialerror.arxiv_index.ingest._flush_batch_csv_dat``), mirroring the real
    file's own sample (``0,0704.0001,arxiv`` -- most rows are journal-less
    per the confirmed schema.org description)."""
    out = []
    for i in range(n):
        journal = "arxiv" if journal_every and i % journal_every == 0 else ""
        out.append(
            {
                "index": i,
                "id": f"{id_prefix}.{i:05d}",
                "journal": journal,
                "vector": deterministic_vector(i, dims),
            }
        )
    return out


def write_csv_dat_fixture_zip(
    zip_path: Path,
    *,
    n: int = 24,
    dims: int = 8,
    id_prefix: str = "9999",
    csv_member: str = "papers.csv",
    dat_member: str = "vectors.dat",
    truncate_dat_bytes: int = 0,
    extra_dat_rows: int = 0,
    records: list[dict[str, Any]] | None = None,
) -> tuple[Path, list[dict[str, Any]]]:
    """Build a synthetic csv+dat-layout zip mirroring the REAL Kaggle
    zip's confirmed shape -- see :func:`make_csv_dat_records` and
    ``trialerror/arxiv_index/ingest.py``'s module docstring. Returns
    ``(zip_path, records)`` so a test can assert ingested rows against the
    SAME known vectors this fixture wrote (byte-exact correctness check).

    ``records``: pass a pre-built list (e.g. from :func:`make_csv_dat_records`)
    to control exact content (missing id, bad header, etc.) instead of the
    default auto-generated set.

    ``truncate_dat_bytes``: chop this many bytes off the END of the dat
    member's CONTENT before writing it into the zip -- constructs a
    legitimately-CRC-correct-for-its-own-truncated-content zip member (a
    genuinely CORRUPT zip would be caught by ``zipfile``'s own CRC check
    first, not by ``ingest.py``'s own short-read guard -- this keeps the
    two failure modes distinct for tests).

    ``extra_dat_rows``: append this many EXTRA fully-formed (but unclaimed
    by any csv row) vector rows to the dat content -- proves the row-COUNT
    integrity assertion fires even when every individual byte read stays
    exact (a byte-aligned but row-count-mismatched dat member).
    """
    if records is None:
        records = make_csv_dat_records(n, dims=dims, id_prefix=id_prefix)

    csv_lines = ["index,id,journal"]
    for rec in records:
        csv_lines.append(f"{rec['index']},{rec['id']},{rec['journal']}")
    csv_body = "\r\n".join(csv_lines) + "\r\n"

    dat_body = b"".join(struct.pack(f"<{dims}f", *rec["vector"]) for rec in records)
    if extra_dat_rows:
        base_n = len(records)
        dat_body += b"".join(
            struct.pack(f"<{dims}f", *deterministic_vector(base_n + j, dims)) for j in range(extra_dat_rows)
        )
    if truncate_dat_bytes:
        dat_body = dat_body[: len(dat_body) - truncate_dat_bytes]

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(csv_member, csv_body)
        zf.writestr(dat_member, dat_body)
    return zip_path, records
