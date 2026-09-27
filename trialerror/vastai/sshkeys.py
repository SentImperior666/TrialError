"""Is the configured ssh identity registered with the vast.ai ACCOUNT?

**Why this exists.** Neither this lane nor the public embedding lane it was
ported from sends a public key to vast.ai. Both create calls carry exactly
``client_id / image / disk / label / onstart / runtype="ssh"`` and nothing
else; neither attaches a key afterwards, and both connect to the proxy host
(``sshN.vast.ai``) the instance record names. Both therefore depend on
vast.ai putting the ACCOUNT's registered keys on a new instance [FACT, the
code of both lanes at the same point; the embedding lane's rentals of
2026-09-18 authenticated exactly this way].

When the account does not carry the key, the refusal arrives inside a paid
rental: canary C4 of 2026-09-19 created instance 51649387, waited for it to
report ``running``, and got ``Permission denied (publickey)`` on the first
probe -- 313 s and $0.0309 after the create. This module makes that answer a
FREE read taken BEFORE any create: list the account's registered keys,
fingerprint the configured identity's PUBLIC half, compare.

**What it reads.** The ``.pub`` file beside the configured identity, and
nothing else. The private half is opened only by ``ssh``, never by
TrialError; a fingerprint is a hash, so no key text ever reaches a log, an
envelope or the ledger.

**Fail-closed only on a positive absence.** The account's key list is read
through one endpoint constant. If the endpoint or the payload shape is not
one this module knows, the answer is ``"unknown"`` and the caller carries on
as before -- a guess about an endpoint must never be able to stop a run that
would otherwise work. Only a key list that was read AND does not hold the
identity's fingerprint refuses.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "SSH_KEYS_PATH",
    "SSH_KEYS_FALLBACK_PATH",
    "KEY_LIST_FIELDS",
    "KEY_TEXT_FIELDS",
    "CONSOLE_PAGE",
    "SshKeyCheck",
    "fingerprint",
    "public_half_path",
    "public_keys_in_payload",
    "check_ssh_key",
]

#: The account's ssh-key list. ONE constant [assumption, unconfirmed live:
#: the vast.ai CLI's ``show ssh-keys``; settle it with the custodian's free
#: read of ``trialerror vastai plan``, whose result block names the state].
SSH_KEYS_PATH = "/ssh/"

#: The older shape: the user object carries the key text in a field.
SSH_KEYS_FALLBACK_PATH = "/users/current/"

#: Where a list of keys may sit in a payload that is not itself a list.
KEY_LIST_FIELDS: tuple[str, ...] = ("ssh_keys", "results", "keys", "data")

#: Where one key's text may sit in an item (or in the user object).
KEY_TEXT_FIELDS: tuple[str, ...] = ("ssh_key", "public_key", "key", "ssh_key_text")

#: Named in every refusal, so the operator knows where to go.
CONSOLE_PAGE = "the vast.ai console, Account -> Keys -> SSH Keys"


def public_half_path(identity_path: Path | str) -> Path:
    """``<identity>.pub`` -- the only key file TrialError ever opens."""
    return Path(str(identity_path) + ".pub")


def fingerprint(public_key_text: Any) -> str | None:
    """The OpenSSH ``SHA256:`` fingerprint of one ``ssh-... AAAA... comment``
    line, or ``None`` when the line is not a public key. The comment and the
    algorithm name are ignored: the fingerprint is of the blob alone, so the
    same key registered under another label still matches."""
    if not isinstance(public_key_text, str):
        return None
    for field in public_key_text.strip().split():
        if not field.startswith("AAAA"):
            continue
        try:
            blob = base64.b64decode(field, validate=True)
        except (ValueError, TypeError):
            continue
        if not blob:
            continue
        return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return None


def _texts(item: Any) -> Iterable[str]:
    if isinstance(item, str):
        yield item
        return
    if isinstance(item, Mapping):
        for field in KEY_TEXT_FIELDS:
            value = item.get(field)
            if isinstance(value, str) and value.strip():
                yield value


def public_keys_in_payload(payload: Any) -> list[str] | None:
    """Every public-key line in a payload of one of the shapes above, or
    ``None`` when the payload is not a shape this module knows. An empty
    list is an ANSWER (an account with no registered key), not an unknown."""
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        items: Any = list(payload)
    elif isinstance(payload, Mapping):
        items = next((payload[f] for f in KEY_LIST_FIELDS if isinstance(payload.get(f), list)), None)
        if items is None:
            # The user-object shape: one key (or a list) under a text field.
            found = [t for t in _texts(payload)]
            if not found:
                return None
            items = found
    else:
        return None
    out: list[str] = []
    for item in items:
        out.extend(t for t in _texts(item))
    return out


@dataclass(frozen=True)
class SshKeyCheck:
    """What a free read of the account's keys says about the configured
    identity. ``state`` is ``"registered"``, ``"not-registered"`` or
    ``"unknown"``; ``detail`` says why, naming no key text."""

    state: str
    detail: str
    fingerprint: str | None = None
    registered_count: int | None = None

    @property
    def refuses(self) -> bool:
        """Only a positive absence stops a rental."""
        return self.state == "not-registered"

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "detail": self.detail,
            "fingerprint": self.fingerprint,
            "registered_count": self.registered_count,
        }

    def message(self) -> str:
        """The refusal text: what was compared, and where to fix it."""
        return (
            f"the ssh identity's public half ({self.fingerprint}) is not among the "
            f"{self.registered_count} ssh key(s) registered with the vast.ai account -- "
            f"vast.ai puts only ACCOUNT keys on a new instance, so every rental would refuse it "
            f"after it is already billing. Register it in {CONSOLE_PAGE}."
        )

    def next_actions(self) -> list[str]:
        return [
            f"register the public half of [vastai] ssh_identity_path in {CONSOLE_PAGE}",
            "or point [vastai] ssh_identity_path at the private half of a key that is registered there",
        ]


def check_ssh_key(client: Any, identity_path: Path | str | None) -> SshKeyCheck:
    """Compare the configured identity's public half with the account's
    registered keys. One free GET; never raises for an API or file problem
    (the answer is then ``"unknown"``)."""
    if identity_path is None:
        return SshKeyCheck("unknown", "[vastai] ssh_identity_path is not set")
    pub = public_half_path(identity_path)
    try:
        text = pub.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return SshKeyCheck("unknown", f"no public half beside the identity at {pub} ({type(exc).__name__})")
    mine = fingerprint(text)
    del text
    if mine is None:
        return SshKeyCheck("unknown", f"{pub} does not hold an openssh public key line")
    try:
        registered = client.ssh_keys()
    except Exception as exc:  # noqa: BLE001 - a pre-flight never fails a run it cannot judge
        return SshKeyCheck("unknown", f"the account's ssh keys could not be read ({type(exc).__name__})", mine)
    if registered is None:
        return SshKeyCheck("unknown", f"vast.ai {SSH_KEYS_PATH} answered a shape this version does not read", mine)
    prints = {fp for fp in (fingerprint(k) for k in registered) if fp}
    if mine in prints:
        return SshKeyCheck("registered", "the identity's public half is registered with the account", mine, len(prints))
    return SshKeyCheck("not-registered", "the account's key list does not hold this fingerprint", mine, len(prints))
