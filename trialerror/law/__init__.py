"""``trialerror.law`` — user rulings as versioned law. Design Section 2 (subsystem
table, row D): "Rulings, digest lockstep, pin verification, foreign-entry
diff." Design Section 12 (M4 row): "append+digest atomic, hash chain, pin
verify, foreign diff, rendered LAW_DIGEST.md."

Generalizes origin-project's corrections ledger + LAW_DIGEST (REQUIREMENTS Sec 1.1):
rulings carry ``(id, ts, verbatim quote, standing clauses, supersedes,
domains)``; the digest regenerates AT EVERY version bump in lockstep with a
recorded content hash; a stale-pin spawn is refused by the machinery, not
by prompt discipline.

Public surface
--------------
- :func:`append_ruling` — the ONE way to add law (ruling + digest bump,
  atomic; see ``trialerror.law.service`` module docstring for the transaction
  mechanics).
- :func:`lookup_rulings` — filtered read over the ledger.
- :func:`get_current_digest` / :func:`current_pin` — the latest digest row
  / its pin string.
- :func:`render_current_digest_to_disk` — recovery: re-flush the current
  digest's rendered file from ops.db truth without bumping a version.
- :func:`verify_pin` — freshness + chain-integrity check. **THE integration
  point other modules call**: M3's ``plugin/hooks/spawn_gate.py`` (spawn
  gate) and M6's session boot/close logic both call this with the pin they
  are holding (typically ``session.boot_pin_version``) and translate a
  ``PinVerifyResult(valid=False, ...)`` into their own refusal. See its
  docstring in ``trialerror/law/service.py`` for the full contract — if M3 has
  not landed yet when this module ships, THIS is the function signature it
  builds against.
- :func:`diff_foreign` — rulings appended since a given pin (other-session
  appends).
- :func:`verify_chain` / :class:`ChainVerifyResult` — the tamper-evidence
  primitive ``verify_pin`` and ``trialerror.law.checks`` both build on.
- :func:`format_pin` / :func:`parse_pin` — the ``'vNN@YYYY-MM-DD'`` pin
  string shape shared by every entry point above.

``trialerror/law/checks.py`` registers this module's ``trialerror doctor`` checks
(``law_digest_lockstep``, ``law_chain_integrity``, ``law_pin_format``) by
the same auto-discovery convention every other subsystem uses — importing
it is not required for the checks to run under ``trialerror doctor``, only for
calling them directly.
"""

from __future__ import annotations

from trialerror.law.chain import (
    GENESIS_HASH,
    ChainVerifyResult,
    canonical_ruling_repr,
    compute_ledger_hash,
    verify_chain,
)
from trialerror.law.digest import digest_sha256, render_digest
from trialerror.law.service import (
    RENDERED_PATH,
    AppendResult,
    PinVerifyResult,
    RenderResult,
    append_ruling,
    current_pin,
    diff_foreign,
    format_pin,
    get_current_digest,
    lookup_rulings,
    parse_pin,
    render_current_digest_to_disk,
    verify_pin,
)

__all__ = [
    "RENDERED_PATH",
    "GENESIS_HASH",
    "ChainVerifyResult",
    "canonical_ruling_repr",
    "compute_ledger_hash",
    "verify_chain",
    "render_digest",
    "digest_sha256",
    "AppendResult",
    "append_ruling",
    "lookup_rulings",
    "get_current_digest",
    "current_pin",
    "RenderResult",
    "render_current_digest_to_disk",
    "PinVerifyResult",
    "verify_pin",
    "diff_foreign",
    "format_pin",
    "parse_pin",
]
