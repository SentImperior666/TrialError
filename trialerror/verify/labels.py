"""The contracrow label taxonomy, loaded from the vendored module. Design
Section 8.2: "each evidence chunk scored against the hypothesis with the
paper-qa contracrow prompt (vendored, Apache-2.0)."

Thin wrapper over ``vendored/paper-qa/contracrow.py`` (verbatim-extracted
prompt/label constants — see that file's own docstring and
``vendored/VENDORED.md`` for the manifest row), loaded by file path exactly
like ``trialerror/ingest/sanitizer.py`` loads its own vendored module: no
``sys.path`` mutation (a top-level module literally named ``contracrow``
has no business on this process's import path), and ``sys.dont_write_bytecode``
held for the duration of the import so this build never leaves a
``vendored/paper-qa/__pycache__/*.pyc`` behind (the pre-existing
``trialerror.util.checks.check_license_audit`` gap ``trialerror/memory/merge.py``'s
own TRIALERROR-DEV-NOTE already flags for a nested ``.pyc`` one level inside a
vendored item's directory — out of this build's lane, not re-flagged here,
only avoided).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

__all__ = ["CONTRACROW_LABELS", "CONTRACROW_QA_PROMPT", "label_index", "label_polarity"]

_VENDORED_PATH = Path(__file__).resolve().parents[2] / "vendored" / "paper-qa" / "contracrow.py"


def _load_vendored_contracrow_module():
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location("_trialerror_vendored_paperqa_contracrow", _VENDORED_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = prev
    return module


_contracrow = _load_vendored_contracrow_module()

CONTRACROW_LABELS: tuple[str, ...] = _contracrow.CONTRACROW_LABELS
CONTRACROW_QA_PROMPT: str = _contracrow.CONTRACROW_QA_PROMPT
label_index = _contracrow.label_index
label_polarity = _contracrow.label_polarity
