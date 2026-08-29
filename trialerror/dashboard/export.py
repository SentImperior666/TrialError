"""``trialerror dashboard export`` -- a self-contained, single-file static
snapshot of every panel, viewable offline (``file://``, no server, no
network access attempted at all).

Builds the SAME panel JSON the live server would (via
:func:`trialerror.dashboard.data.build_all_panels` -- one data-builder path,
never a second copy), then two build-time transforms turn the
MINIMAL-FUNCTIONAL ``static/dashboard.html`` template into one portable
file:

1. **CSS inlining.** The template's ``<link rel="stylesheet"
   href="dashboard.css">`` is replaced with an inline ``<style>`` block
   containing ``dashboard.css``'s own content verbatim. The *source*
   template stays "one external CSS file, no inline styles" (so a re-skin
   only ever touches ``dashboard.css``); only the EXPORT step inlines it,
   because a snapshot dropped anywhere on disk by ``--out`` cannot rely on
   a sibling file being copied alongside it.
2. **Data embedding.** The panel JSON is written into a
   ``<script id="dashboard-data" type="application/json">`` tag the page's
   own inline script already knows to look for (``tryStaticMode()``) --
   when present, the page renders it directly and never calls ``fetch()``
   or opens an ``EventSource``, regardless of how the file is later opened
   or served.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trialerror.dashboard.data import build_all_panels
from trialerror.dashboard.doctor_run import read_doctor_state, run_doctor_and_persist
from trialerror.dashboard.ext import build_all_ext_panels, list_ext_panels
from trialerror.dashboard.store_ro import open_store_ro
from trialerror.stores import paths as store_paths
from trialerror.util.timeutil import now

__all__ = ["build_snapshot_html", "export_snapshot"]

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
_CSS_LINK_TAG = '<link rel="stylesheet" href="dashboard.css">'


def _embed_json_safely(payload: dict[str, Any]) -> str:
    """A ``<script type="application/json">`` body must never contain the
    raw byte sequence ``</script`` (the HTML tokenizer does not know it is
    "inside JSON" -- it would end the script element early, exactly like
    it would inside a ``<script>`` tag). The standard-safe fix: escape
    every literal ``<`` in the JSON text as the JSON unicode escape
    ``\\u003c`` -- still byte-for-byte valid JSON (``JSON.parse`` decodes
    the escape back to ``<``), and now contains no ``<`` at all, so no
    tag-like sequence can ever appear."""
    raw = json.dumps(payload, ensure_ascii=False, default=str)
    return raw.replace("<", "\\u003c")


def build_snapshot_html(
    *,
    program_root: Path | str | None,
    platform_root: Path | str | None = None,
    repo_root: Path | str | None = None,
    run_doctor: bool = False,
) -> str:
    """Return the exported page's full HTML text (caller writes it)."""
    program_root_p = Path(program_root) if program_root else None
    platform_root_p = Path(platform_root) if platform_root else store_paths.platform_root()

    rostore = open_store_ro(program_root_p, platform_root=platform_root_p)
    try:
        if run_doctor:
            doctor_state = run_doctor_and_persist(
                repo_root=Path(repo_root) if repo_root else Path.cwd(),
                program_root=program_root_p,
                platform_root=platform_root_p,
            )
        else:
            doctor_state = read_doctor_state(program_root_p)
        panels = build_all_panels(rostore, doctor_state=doctor_state)
        # extension panels (trialerror.dashboard.ext, C-0070): SAME build path
        # the live server uses (trialerror.dashboard.serve.build_all) -- only
        # add the "ext" key when this program actually declares at least
        # one, matching the live server's own byte-for-byte-compatible
        # core-panel-set behavior.
        ext_panels = build_all_ext_panels(program_root_p, rostore)
        if ext_panels:
            panels["ext"] = ext_panels
    finally:
        rostore.close()

    meta = {
        "generated_ts": now(),
        "program_root": str(program_root_p) if program_root_p else None,
        "platform_root": str(platform_root_p),
        "snapshot": True,
        "ext_panels": list_ext_panels(program_root_p),
    }
    payload = {"meta": meta, "panels": panels}

    html = (STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")

    if _CSS_LINK_TAG not in html:
        raise RuntimeError(
            "trialerror/dashboard/static/dashboard.html no longer contains the expected "
            f"stylesheet <link> tag ({_CSS_LINK_TAG!r}) -- export.py's CSS-inlining "
            "marker is stale; update both together"
        )
    html = html.replace(_CSS_LINK_TAG, f"<style>\n{css}\n</style>", 1)

    if "</body>" not in html:
        raise RuntimeError("trialerror/dashboard/static/dashboard.html has no </body> tag to embed data before")
    data_tag = f'<script id="dashboard-data" type="application/json">{_embed_json_safely(payload)}</script>\n</body>'
    html = html.replace("</body>", data_tag, 1)

    return html


def export_snapshot(
    *,
    out_path: Path | str,
    program_root: Path | str | None,
    platform_root: Path | str | None = None,
    repo_root: Path | str | None = None,
    run_doctor: bool = False,
) -> Path:
    html = build_snapshot_html(
        program_root=program_root, platform_root=platform_root, repo_root=repo_root, run_doctor=run_doctor
    )
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out
