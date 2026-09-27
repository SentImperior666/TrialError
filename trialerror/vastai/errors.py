"""Every exception the vast.ai OCR executor raises, by settlement class.

Settlement classes (``trialerror.offload.settle``; design section 11.1):

* **R** -- :class:`JobRefused` and its subclasses. The claim goes back unrun,
  no attempt is burned, the job is skipped for the rest of the worker run.
  :class:`EgressRefused` (the document may not leave), :class:`VastSpendRefused`
  (a cap would be crossed), :class:`VastPlanRefused` (no offer, an API error, a
  failed create), :class:`StackMismatch` (the rented stack is not DEV's; every
  later job of the run is refused too).
* **F** -- :class:`HostFailure`: the backend destroys the lease and rents a
  fresh host, excluding that machine.
* **E** -- anything else, including :class:`VastError` itself.
* **X** -- ``trialerror.vastai.lease.LeaseExpired``, a ``KeyboardInterrupt``.

:class:`VastConfigError` stops the worker before it claims anything.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from trialerror.offload.settle import ClaimReturned

__all__ = [
    "REASON_CODES",
    "REDACTED",
    "redact_secrets",
    "VastError",
    "VastConfigError",
    "VastApiError",
    "VastKeyMissing",
    "OfferUnavailable",
    "HostFailure",
    "JobRefused",
    "EgressRefused",
    "VastSpendRefused",
    "VastPlanRefused",
    "StackMismatch",
]

#: The reason codes of a returned claim: the ledger's ``refused`` rows, the run
#: summary's ``refused`` bucket and the docs use exactly these (lane contract,
#: section 3).
REASON_CODES: tuple[str, ...] = (
    "approval-missing",
    "approval-invalid",
    "approval-expired",
    "approval-future",
    "approval-unsealed",
    "tier-not-allowed",
    "tier-missing",
    "document-too-large",
    "cap-job",
    "cap-run",
    "cap-approval",
    "cap-ttl",
    "credit-low",
    "no-offer",
    "api-error",
    "create-failed",
    "key-missing",
    "stack-mismatch",
    "shm-too-small",
    "hosts-exhausted",
    "vastai-disabled-for-run",
    "stage-not-served",
)


#: What a redacted credential reads as.
REDACTED = "[redacted]"

#: ``Bearer <anything>`` and ``api_key=<anything>`` (any case; ``api-key``,
#: ``apikey``, ``:`` too): credential shapes redacted from every error text,
#: whatever the key.
_SECRET_SHAPES = (
    re.compile(r"(?i)\b(bearer)(\s+)(?!\[redacted\])[^\s\"',;)\]}]+"),
    re.compile(r"(?i)\b(api[_-]?key)(\"?\s*[=:]\s*\"?)(?!\[redacted\])[^\s&\"',;)\]}]+"),
)


def redact_secrets(text: Any, *secrets: str | None) -> str:
    """``text`` with every one of ``secrets`` (a key; values shorter than 8
    characters are ignored) and every ``Bearer <anything>`` /
    ``api_key=<anything>`` replaced by :data:`REDACTED`. Redact BEFORE
    truncating, so that no prefix of a key survives a cut."""
    out = str(text)
    for secret in secrets:
        if secret and len(str(secret)) >= 8:
            out = out.replace(str(secret), REDACTED)
    for shape in _SECRET_SHAPES:
        out = shape.sub(lambda m: m.group(1) + m.group(2) + REDACTED, out)
    return out


class VastError(RuntimeError):
    """Base of the executor's own errors. ``next_actions`` says what would
    change the answer, most useful first. The message never carries a
    ``Bearer`` token or an ``api_key=`` value (:func:`redact_secrets`)."""

    def __init__(self, message: str, *, next_actions: Iterable[str] = ()) -> None:
        super().__init__(redact_secrets(message))
        self.next_actions = [redact_secrets(a) for a in next_actions]


class VastConfigError(VastError, ValueError):
    """The DEV toml's vast.ai settings are refused, by key name. The worker
    does not start. Also a ``ValueError``: the ONE config error of the package,
    which :mod:`trialerror.vastai.tiers` re-exports where the public copy's
    embedding backend defines its own ``VastConfigError(ValueError)``."""


class VastApiError(VastError):
    """A vast.ai REST call failed. ``status`` is the HTTP status when there
    was one. Messages never carry the API key."""

    def __init__(self, message: str, *, status: int | None = None, next_actions: Iterable[str] = ()) -> None:
        super().__init__(message, next_actions=next_actions)
        self.status = status


class VastKeyMissing(VastApiError):
    """The key file is not configured, not readable or empty. The message
    names the PATH only, never the contents."""


class OfferUnavailable(VastApiError):
    """The offer was taken between search and create (vast.ai ``no_such_ask``).
    Nothing was rented; the next ranked offer may be tried."""

    def __init__(self, message: str, *, offer_id: Any, status: int | None = None) -> None:
        super().__init__(message, status=status)
        self.offer_id = offer_id


class HostFailure(VastError):
    """F class: this rented host cannot finish the job (ssh never accepted,
    bootstrap failed, the instance died, a transfer hash mismatched twice).
    The backend destroys the lease and rents a fresh host that excludes
    ``machine_id``."""

    def __init__(
        self,
        message: str,
        *,
        machine_id: Any = None,
        instance_id: Any = None,
        next_actions: Iterable[str] = (),
    ) -> None:
        super().__init__(message, next_actions=next_actions)
        self.machine_id = machine_id
        self.instance_id = instance_id


class JobRefused(ClaimReturned):
    """R class: hand the claim back unrun. ``reason_code`` must be one of
    :data:`REASON_CODES` -- a misspelt code is a programming error and raises
    ``ValueError`` at the raise site rather than reaching the ledger."""

    #: The subclass's own default for ``disable_executor``.
    disables_executor: bool = False

    def __init__(
        self,
        reason_code: str,
        message: str,
        *,
        next_actions: Iterable[str] = (),
        details: dict[str, Any] | None = None,
        disable_executor: bool | None = None,
    ) -> None:
        if reason_code not in REASON_CODES:
            raise ValueError(f"unknown vast.ai refusal reason code {reason_code!r}; use one of {REASON_CODES}")
        # A refusal text reaches the ledger, the run summary and the log: it
        # never carries a Bearer token or an api_key= value.
        super().__init__(
            reason_code,
            redact_secrets(message),
            next_actions=[redact_secrets(a) for a in next_actions],
            disable_executor=self.disables_executor if disable_executor is None else disable_executor,
            details=details,
        )


class EgressRefused(JobRefused):
    """The document may not leave DEV (policy, approval, size)."""


class VastSpendRefused(JobRefused):
    """A spend cap (job, run, approval, TTL, account credit) would be crossed."""


class VastPlanRefused(JobRefused):
    """Nothing could be rented: no admissible offer, an API error, a failed
    create, hosts exhausted."""


class StackMismatch(JobRefused):
    """The rented stack is not DEV's (marker version, model files, canary).
    Another host would not fix it, so vast.ai use stops for the worker run."""

    disables_executor = True
