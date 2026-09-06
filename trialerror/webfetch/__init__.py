"""``trialerror.webfetch`` — web-page → corpus ingestion, split across a
trust boundary.

Design of record: ``docs/reviews/LANE_A_WEB_INGESTION_DESIGN.md`` (frozen).
Two processes, deliberately:

- a **fetch-only sidecar** with open egress that never sees the corpus or any
  secret. It resolves, validates, connects and reads bytes; it sniffs magic
  numbers and nothing more (design §1 P1). This package's
  :mod:`~trialerror.webfetch.urlcheck`, :mod:`~trialerror.webfetch.policy`,
  :mod:`~trialerror.webfetch.netguard`, :mod:`~trialerror.webfetch.robots`,
  :mod:`~trialerror.webfetch.fetcher`, :mod:`~trialerror.webfetch.gitfetch`
  and :mod:`~trialerror.webfetch.sidecar` modules run *there*.
- the **research container**, which has no egress, parses the hostile bytes
  and owns every database. Its handlers (a later build step) run *here*.

The only thing that crosses between them is a directory:
:mod:`~trialerror.webfetch.protocol` is the file queue (design §2.2/§2.3,
transport ruling L-A1 = "(ii-a) file queue over the shared bind mount").
Both sides import ``protocol``; neither side imports the other's runtime.

**Everything in this package refuses with a reason from one closed
vocabulary** (:data:`REASONS`). A refusal is a *result*, not an exception to
paper over: it is recorded in ``result.json``, audited, and — where a human
could lawfully fix it — surfaced as a request for the operator. Adding a new
reason string means adding it here first; :func:`check_reason` is called on
every construction of :class:`WebFetchRefused` so a typo fails loudly in the
sidecar rather than silently in a database column.
"""

from __future__ import annotations

__all__ = [
    "REASONS",
    "HUMAN_FIXABLE_REASONS",
    "SSRF_CLASS_REASONS",
    "MANIFEST_SCHEMA",
    "RESULT_SCHEMA",
    "SIDECAR_VERSION",
    "WebFetchError",
    "WebFetchRefused",
    "WebFetchHandlerError",
    "check_reason",
]

#: Manifest/result schema versions (design §2.3). Both sides write and
#: verify these; an unrecognized value is ``manifest_invalid``.
MANIFEST_SCHEMA = 1
RESULT_SCHEMA = 1

#: Stamped into every ``result.json`` so a provenance record says which
#: fetcher produced it (design §2.3 ``sidecar_version``).
SIDECAR_VERSION = "webfetch-sidecar/1"

#: The closed refusal vocabulary, verbatim from design §2.3. Nothing in this
#: package may report a reason outside this set.
REASONS: frozenset[str] = frozenset(
    {
        # --- URL shape (urlcheck, pre-DNS: no socket is ever opened) ---
        "scheme_not_allowed",
        "userinfo_present",
        "port_not_allowed",
        "ip_literal",
        "host_syntax",
        "host_not_allowed",
        "host_reserved_suffix",
        "url_too_long",
        "query_too_long",
        # --- name resolution and address policy (netguard) ---
        "dns_failed",
        "ip_private",
        "ipv6_unsupported",
        # --- redirects ---
        "redirect_limit",
        "redirect_downgrade",
        "redirect_off_allowlist",
        # --- politeness / lawfulness ---
        "robots_disallow",
        "robots_unavailable",
        "tdm_optout",
        # --- transport and payload ---
        "timeout",
        "too_large",
        "decompress_bomb",
        "content_type_disallowed",
        "magic_mismatch",
        "http_error",
        "paywalled",
        "bot_challenge",
        "needs_render",
        # --- caps ---
        "daily_cap",
        "host_cap",
        "agent_cap",
        "disk_cap",
        # --- queue / protocol ---
        "manifest_invalid",
        "sidecar_unavailable",
        # --- git ---
        "git_url_shape",
        "git_too_large",
    }
)

#: Refusals a *human* can lawfully resolve by delivering a saved copy
#: through the existing operator-delivery path (design §5, settlement
#: table). These become a ``wanted`` request row on the research side; the
#: rest are audit rows only. No bypass is ever attempted for any of them.
HUMAN_FIXABLE_REASONS: frozenset[str] = frozenset(
    {
        "paywalled",
        "bot_challenge",
        "robots_disallow",
        "host_not_allowed",
        "needs_render",
        "tdm_optout",
        "http_error",
    }
)

#: Refusals that, when they appear at all, are evidence that something
#: inside the research container tried to reach somewhere it should not
#: (design §3.4: the doctor check WARNs on any of these). Grouped here so
#: the doctor and the status script share one definition.
SSRF_CLASS_REASONS: frozenset[str] = frozenset(
    {
        "ip_private",
        "ip_literal",
        "host_reserved_suffix",
        "redirect_off_allowlist",
        "redirect_downgrade",
        "url_too_long",
        "query_too_long",
        "manifest_invalid",
    }
)


class WebFetchError(Exception):
    """Base class for every error this package raises."""


def check_reason(reason: str) -> str:
    """Return ``reason`` if it is in :data:`REASONS`; raise otherwise.

    Deliberately strict: a reason string is a machine-readable value that
    ends up in a database column, an audit line, a doctor verdict and an
    operator-facing table. A typo that only shows up as an unmatched
    ``WHERE reason = ...`` months later is exactly the class of bug a closed
    vocabulary exists to prevent.
    """
    if reason not in REASONS:
        raise WebFetchError(
            f"refusal reason {reason!r} is not in the closed vocabulary "
            f"(trialerror.webfetch.REASONS); add it to the design's §2.3 list first"
        )
    return reason


class WebFetchRefused(WebFetchError):
    """A refusal carrying a machine reason from :data:`REASONS`.

    ``detail`` is free text for the audit line and the operator; it must
    never be relied on by code (branch on ``reason``). ``context`` carries
    structured extras a caller wants recorded (the offending host, the
    redirect hop index, the cap that was hit).
    """

    def __init__(self, reason: str, detail: str = "", **context: object) -> None:
        self.reason = check_reason(reason)
        self.detail = detail
        self.context: dict[str, object] = dict(context)
        super().__init__(f"{self.reason}: {detail}" if detail else self.reason)

    def to_dict(self) -> dict:
        d: dict = {"reason": self.reason}
        if self.detail:
            d["detail"] = self.detail
        if self.context:
            d["context"] = dict(self.context)
        return d


class WebFetchHandlerError(WebFetchError):
    """A caller's request to the research-side handlers was wrong.

    An unbooked launch id, a fetch id that does not exist, a URL that has no
    live row to refresh — the class of thing a CLI turns into an error
    envelope, distinct from :class:`WebFetchRefused` (which is a *result*
    about a page) and from a job failure (which is the ledger's business).

    **It lives here rather than in ``webfetch/handlers.py``, and that is not
    tidiness.** ``trialerror.jobs.registry.discover_and_register_handlers``
    *reloads* every ``<subsystem>.handlers`` module, once per claimed job, so
    the registry can never go stale. A reload re-executes the module and
    rebinds every class it defines to a NEW class object — after which an
    ``except WebFetchHandlerError`` clause holding the pre-reload class stops
    catching anything, silently, in a caller that looks correct. The package
    module is imported once and never reloaded, so a class defined here keeps
    one identity for the life of the process. (``trialerror.ingest.handlers``
    never hit this because it raises ``RuntimeError`` and defines no classes
    of its own.)
    """
