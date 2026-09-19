"""``trialerror.memory.render`` — markdown front-matter render/parse and the
export/import file layer."""

from __future__ import annotations

import pytest

from trialerror.memory.api import put_item
from trialerror.memory.render import (
    INDEX_FILENAME,
    export_memory,
    import_memory,
    parse_item_markdown,
    render_item_markdown,
    slug_for_key,
)
from tests._memory_fixtures import make_account


def test_slug_for_key_basic():
    assert slug_for_key("origin-project-orchestrator-working-rules") == "origin-project-orchestrator-working-rules"
    assert slug_for_key("Hello World!!") == "hello-world"
    assert slug_for_key("  spaced  out  ") == "spaced-out"


def test_slug_for_key_falls_back_for_all_punctuation():
    assert slug_for_key("!!!") == "item"


def test_render_parse_round_trip_exact():
    row = {
        "memory_item_id": "MEM-01J000000000000000000000",
        "key": "some-topic",
        "tier": "L1",
        "kind": "lesson",
        "body": "line one\nline two\n\nline four after a blank line",
        "l0_abstract": "a short abstract",
        "updated_ts": "2026-08-29T00:00:00.000Z",
        "account_id": "ACC-01J000000000000000000000",
        "status": "active",
    }
    text = render_item_markdown(row)
    parsed = parse_item_markdown(text)
    for field in ("memory_item_id", "key", "tier", "kind", "account_id", "updated_ts", "status", "l0_abstract"):
        assert parsed[field] == row[field], field
    assert parsed["body"] == row["body"]


def test_render_parse_round_trip_empty_abstract_and_body():
    row = {
        "memory_item_id": "MEM-x", "key": "k", "tier": "L0", "kind": "rule", "body": "",
        "l0_abstract": None, "updated_ts": "2026-01-01T00:00:00.000Z", "account_id": "ACC-x", "status": "active",
    }
    parsed = parse_item_markdown(render_item_markdown(row))
    assert parsed["body"] == ""
    assert parsed["l0_abstract"] is None


def test_render_collapses_newlines_in_l0_abstract():
    row = {
        "memory_item_id": "MEM-x", "key": "k", "tier": "L0", "kind": "rule", "body": "b",
        "l0_abstract": "line one\nline two", "updated_ts": "t", "account_id": "ACC-x", "status": "active",
    }
    text = render_item_markdown(row)
    assert "l0_abstract: line one line two" in text
    parsed = parse_item_markdown(text)
    assert parsed["l0_abstract"] == "line one line two"


def test_parse_item_markdown_refuses_malformed_file():
    with pytest.raises(ValueError):
        parse_item_markdown("just some random markdown\nwith no marker at all\n")
    with pytest.raises(ValueError):
        parse_item_markdown("<!-- trialerror-memory-item\nkey: x\n-->\nno blank line before body")


def test_export_memory_writes_one_file_per_item_plus_index(store, tmp_path):
    account_id = make_account(store)
    put_item(store, key="topic-one", tier="L0", kind="rule", body="body one", account_id=account_id, l0_abstract="abstract one")
    put_item(store, key="topic-two", tier="L1", kind="fact", body="body two", account_id=account_id)

    out_dir = tmp_path / "memory"
    result = export_memory(store, out_dir=out_dir)

    assert result["count"] == 2
    assert (out_dir / INDEX_FILENAME).is_file()
    assert (out_dir / "topic-one.md").is_file()
    assert (out_dir / "topic-two.md").is_file()

    index_text = (out_dir / INDEX_FILENAME).read_text(encoding="utf-8")
    assert "topic-one" in index_text
    assert "topic-two" in index_text
    assert "abstract one" in index_text


def test_export_memory_is_byte_stable_on_unchanged_store(store, tmp_path):
    account_id = make_account(store)
    put_item(store, key="stable", tier="L0", kind="rule", body="unchanging", account_id=account_id)
    out_dir = tmp_path / "memory"
    export_memory(store, out_dir=out_dir, ts="2026-08-29T00:00:00.000Z")
    first = (out_dir / "stable.md").read_bytes()
    export_memory(store, out_dir=out_dir, ts="2026-08-29T00:00:00.000Z")
    second = (out_dir / "stable.md").read_bytes()
    assert first == second


def test_import_memory_skips_the_index_file(store, tmp_path):
    account_id = make_account(store)
    put_item(store, key="real-item", tier="L0", kind="rule", body="content", account_id=account_id)
    out_dir = tmp_path / "memory"
    export_memory(store, out_dir=out_dir)

    # a second, empty store imports cleanly -- the index file must not be
    # misparsed as a foreign memory item.
    from trialerror.stores.store import open_store

    other_root = tmp_path / "program_other"
    other_root.mkdir()
    other_store = open_store(other_root, platform_root=store.platform_root)
    try:
        result = import_memory(other_store, in_dir=out_dir)
        assert result.imported  # the real item landed
        assert len(result.imported) == 1
    finally:
        other_store.close()


def test_import_memory_refuses_missing_dir_gracefully_via_glob(store, tmp_path):
    empty_dir = tmp_path / "nothing_here"
    empty_dir.mkdir()
    result = import_memory(store, in_dir=empty_dir)
    assert result.imported == []
    assert result.conflicts == []
