"""How a backend hands a claimed job back UNRUN (settlement class R).

The DEV worker settles every claimed job one of four ways (the vast.ai OCR
design, section 11.1):

* **R** -- the job may not run here, now: the claim is handed back through
  the queue's ``return`` verb, no offload attempt is burned, the job lands in
  the run summary's ``refused`` bucket with its reason code, and it is skipped
  for the rest of the worker run. A backend says "R" by raising
  :class:`ClaimReturned` (or a subclass).
* **F** -- a rented host failed; the backend itself fails over.
* **E** -- any other exception: ``error.json``, one attempt burned (unchanged).
* **X** -- a ``KeyboardInterrupt`` (the lease watchdog's ``LeaseExpired``
  among them): the claim goes back unrun and the worker run ends loudly.

A policy "no" must not burn attempts: the queue side's attempt counter
measures GPU failures, and three burned attempts would abandon a document
for the wrong reason. That is why R is its own exception type rather than a
flavour of ``RuntimeError``: an ``except Exception`` that meant "the GPU run
failed" must not be the thing that catches it.
"""

from __future__ import annotations

from typing import Any, Iterable

__all__ = ["ClaimReturned"]


class ClaimReturned(Exception):
    """Hand this claim back unrun, burning no attempt.

    ``reason_code``      a short stable name (the vast.ai reason codes are
                         listed in :data:`trialerror.vastai.errors.REASON_CODES`)
    ``message``          one human sentence; it says what would change the
                         answer and, where it matters, that nothing was sent
    ``next_actions``     what an operator can do about it, most useful first
    ``disable_executor`` true when every later job of this worker run would
                         be refused for the same reason (a wrong remote stack,
                         a missing key): the backend then refuses them at
                         once, without any further call
    ``details``          structured numbers for the run summary and the ledger
    """

    def __init__(
        self,
        reason_code: str,
        message: str,
        *,
        next_actions: Iterable[str] = (),
        disable_executor: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = str(reason_code)
        self.message = str(message)
        self.next_actions = [str(a) for a in next_actions]
        self.disable_executor = bool(disable_executor)
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        """The shape the run summary's ``refused`` bucket records."""
        return {
            "reason_code": self.reason_code,
            "message": self.message,
            "next_actions": list(self.next_actions),
            "disable_executor": self.disable_executor,
            "details": dict(self.details),
        }

    def __str__(self) -> str:
        return f"[{self.reason_code}] {self.message}"
