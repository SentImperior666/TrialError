"""``trialerror dashboard export`` -- the static self-contained snapshot writer.
Proves the two build-time transforms (CSS inlining, JSON data embedding)
land correctly and that the result is genuinely self-contained (no
external ``dashboard.css`` reference left behind)."""

from __future__ import annotations

import json
import re

from trialerror.dashboard import export as dashboard_export
from trialerror.stores.store import open_store
from tests._store_fixtures import populate_one_of_everything

_DATA_TAG_RE = re.compile(r'<script id="dashboard-data" type="application/json">(.*?)</script>', re.S)


def test_export_snapshot_is_self_contained_and_embeds_panels(program_root, platform_root, tmp_path):
    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    store.close()

    out_path = tmp_path / "snapshot.html"
    result_path = dashboard_export.export_snapshot(
        out_path=out_path, program_root=program_root, platform_root=platform_root
    )
    assert result_path == out_path
    assert out_path.is_file()
    html = out_path.read_text(encoding="utf-8")

    # single-file portable: the external stylesheet <link> is gone, its
    # content is inlined instead.
    assert 'href="dashboard.css"' not in html
    assert "<style>" in html
    assert "--live: #3FE07A" in html  # a real HALIDE token rule from dashboard.css landed inline

    # the static snapshot still carries the new HALIDE shell -- the rail,
    # every panel's data-panel hook, and the ext-panel injection points --
    # not just the old MINIMAL-FUNCTIONAL scaffold this build replaced.
    assert 'data-role="rail"' in html
    for panel_name in (
        "home", "search", "evidence", "lexicon", "dossier", "course",
        "rooms", "feed", "determinations", "console",
    ):
        assert f'data-panel="{panel_name}"' in html
    assert 'data-role="rail-ext-KNOW"' in html
    assert 'data-role="rail-ext-RUN"' in html
    assert 'id="dashboard-data"' in html

    m = _DATA_TAG_RE.search(html)
    assert m is not None, "embedded #dashboard-data script tag not found"
    raw = m.group(1)

    # </script>-safety: no literal "<" survives inside the embedded JSON
    # body (every one was escaped to < at embed time).
    assert "<" not in raw

    payload = json.loads(raw)
    assert payload["meta"]["program_root"] == str(program_root)
    assert payload["meta"]["snapshot"] is True
    assert set(payload["panels"]) == {
        "session", "budget", "jobs", "gates", "corpus", "doctor",
        "feed", "rooms", "determinations", "dossier", "lexicon", "course", "since_you_left",
    }
    assert payload["panels"]["session"]["open_session"]["session_id"] == ids["session"]
    assert payload["panels"]["doctor"]["status"] == "never_run"  # run_doctor=False (default)


def test_export_snapshot_run_doctor_populates_doctor_panel(program_root, platform_root, tmp_path):
    store = open_store(program_root, platform_root=platform_root)
    populate_one_of_everything(store)
    store.close()

    out_path = tmp_path / "snapshot_with_doctor.html"
    dashboard_export.export_snapshot(
        out_path=out_path,
        program_root=program_root,
        platform_root=platform_root,
        repo_root=tmp_path,  # scope the license_audit vendored/ scan to an empty tmp dir
        run_doctor=True,
    )
    html = out_path.read_text(encoding="utf-8")
    payload = json.loads(_DATA_TAG_RE.search(html).group(1))
    assert payload["panels"]["doctor"]["status"] == "ok"
    assert payload["panels"]["doctor"]["last_run"]["summary"]["total"] > 0


def test_export_snapshot_creates_parent_directories(program_root, platform_root, tmp_path):
    store = open_store(program_root, platform_root=platform_root)
    populate_one_of_everything(store)
    store.close()

    out_path = tmp_path / "nested" / "dir" / "snapshot.html"
    result_path = dashboard_export.export_snapshot(
        out_path=out_path, program_root=program_root, platform_root=platform_root
    )
    assert result_path.is_file()


def test_build_snapshot_html_with_no_program_root(tmp_path):
    """A program-agnostic export (no --program-root) still produces a
    valid snapshot -- every store-backed panel reports not_initialized
    rather than the exporter crashing."""
    html = dashboard_export.build_snapshot_html(program_root=None, platform_root=tmp_path / "platform")
    payload = json.loads(_DATA_TAG_RE.search(html).group(1))
    assert payload["meta"]["program_root"] is None
    assert payload["panels"]["session"]["status"] == "not_initialized"
    assert payload["panels"]["budget"]["status"] == "not_initialized"
    assert payload["meta"]["ext_panels"] == []
    assert "ext" not in payload["panels"]


def test_export_snapshot_has_no_write_token_and_every_write_button_disabled(program_root, platform_root, tmp_path):
    """Design constraint (Stage 3, build-v2dash-writes): a static snapshot
    is read-only by definition -- it must never carry a write token (only
    ``trialerror.dashboard.serve``'s ``_serve_index`` injects one, at live
    request time; ``export.py`` never runs through that code path at all),
    and every write control's ``disabled`` attribute in the served markup
    must therefore be the honest DEFAULT the raw HTML source ships with,
    not something a client script has to remember to enforce."""
    store = open_store(program_root, platform_root=platform_root)
    populate_one_of_everything(store)
    store.close()

    out_path = tmp_path / "snapshot_readonly.html"
    dashboard_export.export_snapshot(out_path=out_path, program_root=program_root, platform_root=platform_root)
    html = out_path.read_text(encoding="utf-8")

    # The actual INJECTED tag (always carries a real content="<hex>" value,
    # written only by trialerror.dashboard.serve._serve_index) must be absent --
    # not just the bare substring, which also (legitimately) appears in this
    # page's own explanatory JS comments about the mechanism, with no
    # content="..." attribute at all.
    assert re.search(r'<meta\s+name="dashboard-write-token"\s+content="[0-9a-f]+">', html) is None

    for data_role in ("feed-transmit-btn", "room-turn-btn"):
        m = re.search(rf'<[^>]*data-role="{data_role}"[^>]*>', html)
        assert m is not None, f"missing markup for data-role={data_role!r}"
        assert "disabled" in m.group(0), f"data-role={data_role!r} is not disabled in the raw export markup"


def test_export_snapshot_embeds_extension_panels(program_root, platform_root, tmp_path):
    """trialerror.dashboard.ext (C-0070): a program with a trialerror_ext/panels/
    extension gets it embedded in the static snapshot the SAME way the live
    server serves it -- one build path, never two."""
    store = open_store(program_root, platform_root=platform_root)
    populate_one_of_everything(store)
    store.close()

    panel_dir = program_root / "trialerror_ext" / "panels" / "fixture"
    panel_dir.mkdir(parents=True)
    (panel_dir / "panel.toml").write_text(
        '[panel]\ntitle = "Fixture"\nnav_group = "KNOW"\norder = 1\n', encoding="utf-8"
    )
    (panel_dir / "builder.py").write_text(
        "def build_panel(rostore, program_root):\n    return {'status': 'ok', 'value': 42}\n",
        encoding="utf-8",
    )

    out_path = tmp_path / "snapshot_ext.html"
    dashboard_export.export_snapshot(out_path=out_path, program_root=program_root, platform_root=platform_root)
    html = out_path.read_text(encoding="utf-8")
    payload = json.loads(_DATA_TAG_RE.search(html).group(1))

    assert payload["panels"]["ext"]["fixture"] == {"status": "ok", "value": 42}
    assert payload["meta"]["ext_panels"] == [
        {"name": "fixture", "manifest_status": "ok", "title": "Fixture", "nav_group": "KNOW", "order": 1, "description": "", "min_schema": []}
    ]
