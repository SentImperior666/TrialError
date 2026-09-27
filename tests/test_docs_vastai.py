"""vast.ai round 2, lane L3: the documentation moves with the code.

* The command catalog's ``vastai`` row names exactly the verbs the parser
  registers, and only flags that exist (the ``offload`` row's pattern, in
  ``tests/test_docs_fb8b_small.py``).
* ``docs/USER_SETUP.md`` section 1b lists every key of ``CONFIG_KEYS`` with its
  default, on one line: a key added to the config without a row fails here.
* The operator guide carries the section, the reason codes and the settlement
  names the code uses.
* No name of another programme and no local path in the new documentation or
  in the vast.ai files.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_DOCS = _REPO / "docs"
_GUIDE_HEADING = "## OCR on a rented GPU (vast.ai)"
_SETUP_HEADING = "## 1b. Renting the OCR GPU"


def _read(name: str) -> str:
    path = _DOCS / name
    if not path.is_file():
        pytest.skip(f"{name} not present in this tree")
    return path.read_text(encoding="utf-8")


def _section(name: str, heading: str) -> str:
    """From ``heading`` to the next top-level heading (subsections included)."""
    text = _read(name)
    assert heading in text, f"{name} has no {heading!r} section"
    return heading + text.split(heading, 1)[1].split("\n## ", 1)[0]


# ---------------------------------------------------------------------------
# the command catalog's `vastai` row
# ---------------------------------------------------------------------------
def _row() -> str:
    rows = [line for line in _read("OPERATOR_GUIDE.md").splitlines() if line.startswith("| `vastai` |")]
    assert rows, "OPERATOR_GUIDE.md's command table has no `vastai` row"
    return rows[0]  # the command table's row comes before the doctor catalog's


def _group():
    from trialerror.cli import build_parser

    parser = build_parser()
    groups = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)][0]
    sub = groups.choices["vastai"]
    return [a for a in sub._actions if isinstance(a, argparse._SubParsersAction)][0].choices


def test_the_vastai_row_names_every_registered_verb_and_nothing_else():
    registered = set(_group())
    catalogued = set(re.findall(r"`([a-z][a-z0-9-]*)`", _row().split("|")[2]))
    assert registered == catalogued, (sorted(registered - catalogued), sorted(catalogued - registered))


def test_every_flag_the_vastai_row_names_is_a_real_flag():
    real = {o for p in _group().values() for a in p._actions for o in a.option_strings}
    named = set(re.findall(r"(--[a-z][a-z0-9-]*)", _row()))
    assert named, "the vastai row names no flags at all -- has its shape changed?"
    assert not (named - real), f"flags the vastai parsers do not have: {sorted(named - real)}"
    assert "--backend-config-root" in named


# ---------------------------------------------------------------------------
# USER_SETUP.md 1b: every key and its default
# ---------------------------------------------------------------------------
def test_every_config_key_is_in_user_setup_with_its_default():
    from trialerror.vastai.config import CONFIG_KEYS

    section = _section("USER_SETUP.md", _SETUP_HEADING)
    missing = []
    for key in CONFIG_KEYS:
        lines = [line for line in section.splitlines() if f"`{key.qualified}`" in line]
        if not any(key.default_text in line for line in lines):
            missing.append(f"{key.qualified} = {key.default_text}")
    assert not missing, f"USER_SETUP.md section 1b lacks these keys or defaults: {missing}"


def test_user_setup_names_the_operator_steps():
    section = _section("USER_SETUP.md", _SETUP_HEADING)
    for words in ("trialerror vastai lock-deps", "trialerror vastai approve-ocr", "trialerror vastai plan",
                  "trialerror vastai reap", "Never use the queue key", "listed documents only",
                  "The first live run is a separate act", "record_offsite_ocr_events"):
        assert words in section, words


# ---------------------------------------------------------------------------
# the guide's section
# ---------------------------------------------------------------------------
def test_the_guide_lists_every_reason_code_and_the_settlement_names():
    from trialerror.vastai.errors import REASON_CODES

    section = _section("OPERATOR_GUIDE.md", _GUIDE_HEADING)
    missing = [code for code in REASON_CODES if f"`{code}`" not in section]
    assert not missing, f"reason codes the guide does not list: {missing}"
    for words in ("nothing leaves by default", "`refused`", "`when_refused`", "`--stages ocr`", "sequential",
                  "vast.ai's invoice is authoritative", "every 15 minutes", "`offload_ocr_offsite`",
                  "What remains where"):
        assert words.lower() in section.lower(), words


def test_the_guide_names_the_ledger_kinds_the_code_writes():
    from trialerror.vastai.ledger import LEDGER_KINDS

    section = _section("OPERATOR_GUIDE.md", _GUIDE_HEADING)
    assert not [k for k in LEDGER_KINDS if f"`{k}`" not in section]


def test_the_design_says_it_is_implemented_on_fakes():
    text = _read("VASTAI_OCR_DESIGN.md")
    assert "implemented in round 2 on fakes; verification is round 3" in text
    assert "## 18 · Round-2 implementation notes" in text


# ---------------------------------------------------------------------------
# no other programme's name, no local path
# ---------------------------------------------------------------------------
#: Assembled from pieces so this file does not carry the words it looks for.
_FORBIDDEN = re.compile(
    "|".join(["ai" + "if", "univ" + "estal", "table" + "top", "sentim" + "perior", "ge" + "66"]), re.IGNORECASE
)
_LOCAL_PATH = re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+(?!<)|/home/(?!you\b|<)[a-z]")


def _new_texts() -> dict[str, str]:
    texts = {
        "OPERATOR_GUIDE.md#vastai": _section("OPERATOR_GUIDE.md", _GUIDE_HEADING) + "\n" + _row(),
        "USER_SETUP.md#1b": _section("USER_SETUP.md", _SETUP_HEADING),
        "VASTAI_OCR_DESIGN.md": _read("VASTAI_OCR_DESIGN.md"),
        # round 3: the embedding lane's design, ported with its line 88 generalised (V2-F06)
        "VASTAI_EMBED_DESIGN.md": _read("VASTAI_EMBED_DESIGN.md"),
    }
    files = [_REPO / "trialerror" / "cli" / "vastai.py", _REPO / "trialerror" / "offload" / "stage.py"]
    files += sorted((_REPO / "trialerror" / "vastai").rglob("*"))
    files += sorted((_REPO / "tests").glob("test_vastai_*.py")) + sorted((_REPO / "tests").glob("test_docs_vastai*.py"))
    files += sorted((_REPO / "tests").glob("test_offload_stage_license*.py"))
    for path in files:
        if path.is_file() and path.suffix in {".py", ".txt", ".sh", ".sha256", ".md", ".toml", ".json"}:
            texts[path.relative_to(_REPO).as_posix()] = path.read_text(encoding="utf-8", errors="replace")
    return texts


def test_no_other_programme_is_named_and_no_local_path_appears():
    hits = []
    for name, text in _new_texts().items():
        for pattern in (_FORBIDDEN, _LOCAL_PATH):
            for m in pattern.finditer(text):
                hits.append(f"{name}:{text.count(chr(10), 0, m.start()) + 1}: {m.group(0)!r}")
    assert not hits, hits


# ---------------------------------------------------------------------------
# round 4, decision (b): the embedding design names its source in the custodian's words
# ---------------------------------------------------------------------------
_EMBED_SOURCE = (
    "Source: the operator's local `embeddings_local/embed_backend.py` and its",
    "`results/qwen3-4b.json` (a backend bake-off measured on that laptop's GPU).",
)


def test_the_embed_design_names_its_source_in_the_custodians_words():
    text = _read("VASTAI_EMBED_DESIGN.md")
    lines = text.splitlines()
    at = [i for i, line in enumerate(lines) if line == _EMBED_SOURCE[0]]
    assert len(at) == 1 and lines[at[0] + 1] == _EMBED_SOURCE[1], at
    # assembled from pieces, like _FORBIDDEN, so this file does not carry the words it looks for
    for gone in ("origin" + "-project", "WKP" + "-061"):
        assert gone not in text, gone
