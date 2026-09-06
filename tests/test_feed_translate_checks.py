"""lane-b-translator: the translator's doctor checks
(:mod:`trialerror.feed_translate.checks`).

``feed_translation_failures`` is the check that makes fail-closed
OBSERVABLE. A withheld translation is invisible in the dashboard on
purpose -- without a number in doctor, a translator that is failing every
post looks exactly like a translator nobody has run.
"""

from __future__ import annotations

import pytest

from trialerror.feed_translate.api import build_translation_envelope, store_translation
from trialerror.feed_translate.gate import run_translation_gate
from trialerror.util.doctor import (
    DoctorContext,
    clear_registry,
    discover_and_register_checks,
    registered_checks,
    run_checks,
)

from tests._feed_translate_fixtures import DENSE_POST, FAITHFUL_TRANSLATION, UNFAITHFUL_TRANSLATION, build_feed


@pytest.fixture()
def registry():
    clear_registry()
    discover_and_register_checks()
    yield
    clear_registry()


def _run(ctx: DoctorContext, name: str):
    return {r.name: r for r in run_checks(ctx, only=[name])}[name]


def _ctx(program_root, platform_root) -> DoctorContext:
    return DoctorContext(program_root=program_root, platform_root=platform_root)


def _translate(store, post_id: str, body: str) -> None:
    envelope = build_translation_envelope(store, post_id=post_id)
    gate = run_translation_gate(None, post_id=post_id, original_body=DENSE_POST, translation_body=body)
    store_translation(store, envelope=envelope, body=body, gate=gate.as_row())


def test_both_checks_are_auto_discovered(registry):
    names = registered_checks()
    assert "feed_translation_failures" in names
    assert "feed_translations_stale" in names


def test_failures_check_passes_when_nothing_was_withheld(registry, store, program_root, platform_root):
    feed = build_feed(store)
    _translate(store, feed["post_ids"][0], FAITHFUL_TRANSLATION)
    store.close()
    result = _run(_ctx(program_root, platform_root), "feed_translation_failures")
    assert result.status == "pass"
    assert result.details["count"] == 0


def test_failures_check_warns_and_names_the_withheld_rows(registry, store, program_root, platform_root):
    feed = build_feed(store)
    _translate(store, feed["post_ids"][0], UNFAITHFUL_TRANSLATION)
    store.close()
    result = _run(_ctx(program_root, platform_root), "feed_translation_failures")
    # warn, never fail: the guard WORKING is not a broken program
    assert result.status == "warn"
    assert result.details["count"] == 1
    assert result.details["post_ids"] == [feed["post_ids"][0]]
    assert "withheld" in result.message


def test_stale_check_warns_for_a_translation_from_an_older_version(registry, store, program_root, platform_root):
    feed = build_feed(store)
    envelope = build_translation_envelope(store, post_id=feed["post_ids"][0], translator_version="0")
    store_translation(store, envelope=envelope, body=FAITHFUL_TRANSLATION, gate={"gate_status": "pass"})
    store.close()
    result = _run(_ctx(program_root, platform_root), "feed_translations_stale")
    assert result.status == "warn"
    assert result.details["count"] == 1


def test_both_checks_skip_cleanly_on_an_uninitialized_program(registry, tmp_path):
    ctx = _ctx(tmp_path / "nothing-here", tmp_path / "no-platform")
    assert _run(ctx, "feed_translation_failures").status == "skip"
    assert _run(ctx, "feed_translations_stale").status == "skip"
