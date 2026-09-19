"""M0's own doctor check: ``license_audit``.

Design Section 12 (M0 acceptance criteria): "``trialerror doctor --license-audit``
fails on a headerless vendored fixture." Design Section 13 (flag F4
mitigation): "every vendored file lands under ``vendored/<item>/`` with a
header (upstream URL, commit, license, verified-by, date) + ``VENDORED.md``
manifest; ``trialerror doctor --license-audit`` fails on headerless vendored
files."

Header convention (defined here, documented in full in ``vendored/VENDORED.md``):
every vendored file's first ``HEADER_SCAN_LINES`` lines must contain five
``key: value`` lines — ``upstream``, ``commit``, ``license``, ``verified-by``,
``date`` — in any comment style (``#``, ``//``, ``<!--``, ...; the scan
strips leading comment punctuation before matching keys, so the same five
lines work verbatim in a ``.py``, ``.md``, or ``.ts`` file).
"""

from __future__ import annotations

from pathlib import Path

from trialerror.util.doctor import CheckResult, DoctorContext, register_check

__all__ = ["REQUIRED_HEADER_FIELDS", "HEADER_SCAN_LINES", "check_license_audit"]

REQUIRED_HEADER_FIELDS: tuple[str, ...] = (
    "upstream",
    "commit",
    "license",
    "verified-by",
    "date",
)
HEADER_SCAN_LINES = 40
_EXCLUDED_NAMES = {"VENDORED.md", ".gitkeep", "__pycache__"}
_MANIFEST_NAME = "VENDORED.md"


def _header_fields_present(text: str) -> set[str]:
    found: set[str] = set()
    for line in text.splitlines()[:HEADER_SCAN_LINES]:
        stripped = line.strip().lstrip("#/*<!-").strip("->").strip()
        if ":" not in stripped:
            continue
        key = stripped.split(":", 1)[0].strip().lower()
        if key in REQUIRED_HEADER_FIELDS:
            found.add(key)
    return found


def _file_has_full_header(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False
    return _header_fields_present(text) >= set(REQUIRED_HEADER_FIELDS)


@register_check("license_audit", category="license")
def check_license_audit(ctx: DoctorContext) -> CheckResult:
    root = ctx.resolve_vendored_root()

    if not root.exists():
        return CheckResult(
            name="license_audit",
            category="license",
            status="pass",
            message="no vendored/ directory present; nothing to audit",
            details={"root": str(root)},
        )

    item_dirs = sorted(p for p in root.iterdir() if p.is_dir() and p.name not in _EXCLUDED_NAMES)
    offenders: list[str] = []
    scanned = 0
    for item_dir in item_dirs:
        for f in item_dir.rglob("*"):
            if f.is_dir() or f.name in _EXCLUDED_NAMES:
                continue
            # TRIALERROR-DEV-NOTE (M15, INTEGRATION_NOTES.md item 4 / M11 flag
            # chip task_2fe5d707): `rglob` still DESCENDS into a directory
            # named `__pycache__` even though the directory itself is
            # skipped above (the `f.is_dir()` branch only ever fires for
            # the directory entry itself, never for the compiled files
            # inside it) -- so an `.pyc`/`.pyo` byte-compile artifact under
            # any vendored item's own `__pycache__/` was being scanned as
            # if it were source, and (having no header) always failed the
            # audit. Any importable vendored `.py` file picks up a
            # `__pycache__` sibling the first time it's imported, so this
            # was a live false-positive, not just a theoretical one --
            # skip every path with a `__pycache__` component anywhere in
            # its parts, not just a bare `__pycache__` leaf name.
            if "__pycache__" in f.relative_to(item_dir).parts:
                continue
            scanned += 1
            if not _file_has_full_header(f):
                offenders.append(str(f.relative_to(root)))

    manifest_path = root / _MANIFEST_NAME
    manifest_missing = item_dirs and not manifest_path.is_file()

    status = "fail" if offenders or manifest_missing else "pass"
    parts = []
    if offenders:
        parts.append(f"{len(offenders)} vendored file(s) missing the required header")
    if manifest_missing:
        parts.append(f"{_MANIFEST_NAME} manifest missing at {manifest_path}")
    message = "; ".join(parts) if parts else f"license audit clean ({scanned} file(s) scanned)"

    return CheckResult(
        name="license_audit",
        category="license",
        status=status,
        message=message,
        details={
            "root": str(root),
            "items_scanned": [p.name for p in item_dirs],
            "files_scanned": scanned,
            "offenders": offenders,
            "manifest_present": not manifest_missing,
            "required_fields": list(REQUIRED_HEADER_FIELDS),
        },
    )
