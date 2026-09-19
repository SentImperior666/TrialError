"""The file queue — the only thing that crosses the trust boundary.

Design §2.2/§2.3 and ruling **L-A1 = (ii-a)**: one directory, bind-mounted
into the research container as ``/workspace/webfetch`` and into the sidecar as
``/queue``. The two containers share **no network**. A manifest goes one way,
a result and its bytes come back, and nothing else is possible — there is no
listening socket to send a crafted request to and no server parsing
agent-supplied structures beyond one strict JSON schema.

Layout (the lane-0 §4 offload design's shape, implemented here first)::

    <queue>/
      pending/<job_id>.json                 manifest, from the research side
      claimed/<job_id>.claim                the exclusion token: O_EXCL, one winner
      claimed/<worker_id>/<job_id>.json     the winner's copy of the manifest
      claimed/<worker_id>/<job_id>.heartbeat
      done/.partial/<job_id>/               staging; never visible as a result
      done/<job_id>/{result.json, body.bin|repo.tar}
      failed/<job_id>/result.json           a refusal is a *result*, not a retry
      proposals.jsonl                       hosts an agent would like approved
      audit.jsonl                           the sidecar's in-queue audit copy
      sidecar.heartbeat                     liveness for doctor and te-status

Three invariants everything else rests on:

**Publication is atomic.** Results are assembled under ``done/.partial/`` and
become visible by a single directory rename. A reader that sees
``done/<job>/`` sees a complete, verified set of files, whatever happened to
the writer in the middle. ``.partial`` lives *inside* ``done/`` so the rename
never crosses a filesystem, and no job id may begin with ``.`` so no id can
ever name it.

**Claiming is exclusive.** A worker takes a job by *exclusively creating*
``claimed/<job>.claim`` (``O_CREAT|O_EXCL``) and only then renaming the
manifest into its own directory. Two workers racing produce exactly one
winner and one ``None`` — the loser never sees the manifest at all.

The exclusive create is not decoration. A rename alone would be enough on
POSIX, where ``rename(2)`` is atomic *and* exclusive with respect to the
source path; it is **not** enough on Windows, where ``MoveFileExW`` renames
the file a thread already has open, so several threads that each opened the
source before any of them moved it all succeed — passing the one file along a
chain of destinations and each believing it won. This was found by the
concurrency test below rather than reasoned about in advance, and the note is
here because the failure is silent: the filesystem ends up consistent, and
only the count of claims is wrong. Production runs one single-threaded
sidecar on Linux, so nothing depended on it; the local development mode of
design §5 runs the same loop on Windows, so something eventually would have.

**Every structure is validated with an exact key set.** Unknown keys are
``manifest_invalid``, not "ignored for forward compatibility". The manifest
is the one structure an untrusted-by-assumption process (design §1: the
attacker is a prompt-injected agent *inside the research container*) hands to
the process with egress; the ability to smuggle an extra field into it is the
ability to grow the protocol. A field the sidecar does not know about is a
field a future sidecar might, so it is refused today.

Both sides import this module and neither imports the other's runtime.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from trialerror.util.atomic import atomic_write_bytes, atomic_write_text
from trialerror.util.timeutil import now, now_dt, parse as parse_ts
from trialerror.webfetch import (
    MANIFEST_SCHEMA,
    REASONS,
    RESULT_SCHEMA,
    WebFetchError,
    WebFetchRefused,
)

__all__ = [
    "ID_RE",
    "PENDING_DIRNAME",
    "CLAIMED_DIRNAME",
    "DONE_DIRNAME",
    "FAILED_DIRNAME",
    "PARTIAL_DIRNAME",
    "CLAIM_LOCK_SUFFIX",
    "RESULT_FILENAME",
    "BODY_FILENAME",
    "REPO_FILENAME",
    "PAYLOAD_FILENAMES",
    "PROPOSALS_FILENAME",
    "AUDIT_COPY_FILENAME",
    "SIDECAR_HEARTBEAT_FILENAME",
    "MANIFEST_KEYS",
    "RESULT_KEYS",
    "HEADERS_SUBSET_KEYS",
    "ORIGINS",
    "KINDS",
    "OUTCOMES",
    "CONTENT_CLASSES",
    "ROBOTS_VERDICTS",
    "STATE_ABSENT",
    "STATE_PENDING",
    "STATE_CLAIMED",
    "STATE_DONE",
    "STATE_FAILED",
    "ProtocolError",
    "AlreadyPublished",
    "Manifest",
    "Claim",
    "SubmitResult",
    "Queue",
    "check_id",
    "validate_manifest",
    "validate_result",
    "new_result",
    "append_jsonl",
]

PENDING_DIRNAME = "pending"
CLAIMED_DIRNAME = "claimed"
DONE_DIRNAME = "done"
FAILED_DIRNAME = "failed"
PARTIAL_DIRNAME = ".partial"

RESULT_FILENAME = "result.json"
BODY_FILENAME = "body.bin"
REPO_FILENAME = "repo.tar"
#: The only names a published result directory may contain besides
#: ``result.json``. The research side reads *these fixed names only* (design
#: §8, "the queue dir is writable by both sides"): a compromised sidecar
#: cannot get an arbitrarily-named file read by naming it in the result.
PAYLOAD_FILENAMES: frozenset[str] = frozenset({BODY_FILENAME, REPO_FILENAME})

#: Suffix of the per-job exclusion token in ``claimed/``. Not a manifest and
#: not in a worker subdirectory, so nothing that globs for either sees it.
CLAIM_LOCK_SUFFIX = ".claim"

PROPOSALS_FILENAME = "proposals.jsonl"
AUDIT_COPY_FILENAME = "audit.jsonl"
SIDECAR_HEARTBEAT_FILENAME = "sidecar.heartbeat"

STATE_ABSENT = "absent"
STATE_PENDING = "pending"
STATE_CLAIMED = "claimed"
STATE_DONE = "done"
STATE_FAILED = "failed"

#: Design §2.2: "ids validated against ``^[A-Za-z0-9._-]+$`` before any path
#: use". Two strengthenings of that floor, both about paths rather than
#: syntax: a length bound (a 4 KiB "id" is a filesystem problem, not an id),
#: and no leading ``.`` — which keeps ``.``, ``..`` and ``.partial`` out of
#: the namespace in one rule, so an id can never name the staging directory
#: or escape a queue subdirectory.
ID_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$")

#: Exact key set of ``pending/<job_id>.json`` (design §2.3). Every key is
#: required; the ones that may be null are marked in the validator below.
MANIFEST_KEYS: tuple[str, ...] = (
    "schema",
    "job_id",
    "fetch_id",
    "launch_id",
    "program_id",
    "url",
    "origin",
    "list_ref",
    "kind",
    "conditional",
    "robots_override_ruling",
    "created_ts",
)

#: Exact key set of ``done/<job_id>/result.json`` (design §2.3).
RESULT_KEYS: tuple[str, ...] = (
    "schema",
    "job_id",
    "fetch_id",
    "launch_id",
    "url",
    "url_norm",
    "final_url",
    "redirect_chain",
    "fetched_ts",
    "elapsed_ms",
    "http_status",
    "content_type",
    "content_class",
    "bytes",
    "content_sha256",
    "resolved_ips",
    "bytes_out",
    "headers_subset",
    "robots",
    "policy",
    "outcome",
    "reason",
    "sidecar_version",
    "git",
)

#: Fixed header keys copied into a result (design §4 T3, "metadata as data"):
#: a closed set, each value ASCII-filtered and length-capped by the fetcher.
#: A header the remote invents cannot create a key here.
HEADERS_SUBSET_KEYS: tuple[str, ...] = (
    "etag",
    "last-modified",
    "content-language",
    "x-robots-tag",
    "cache-control",
    "date",
    "server",
)

ORIGINS: frozenset[str] = frozenset({"operator_list", "agent"})
KINDS: frozenset[str] = frozenset({"page", "pdf", "git"})
OUTCOMES: frozenset[str] = frozenset({"fetched", "unchanged", "refused"})
CONTENT_CLASSES: frozenset[str] = frozenset({"html", "pdf", "text", "git"})
ROBOTS_VERDICTS: frozenset[str] = frozenset({"allow", "disallow", "unavailable", "n/a"})

_POLICY_VERDICTS = frozenset({"allow", "refused"})
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_RESULT_BYTES = 256 * 1024


class ProtocolError(WebFetchError):
    """The local API was used wrongly (a bad id, a payload name that is not
    in :data:`PAYLOAD_FILENAMES`, a queue root that is not a directory).

    Distinct from :class:`~trialerror.webfetch.WebFetchRefused` on purpose:
    a refusal is a *result* that gets recorded and reported; a
    ``ProtocolError`` is a bug in the caller.
    """


class AlreadyPublished(ProtocolError):
    """``done/<job_id>/`` already existed at publish time.

    Not a failure: the lane-0 protocol's ``publish`` has the same rule
    ("EEXIST = already published: verify remote sha, discard local"). It is
    raised rather than swallowed so the caller decides whether the two
    results agree.
    """


# --------------------------------------------------------------------------
# ids and validation
# --------------------------------------------------------------------------


def check_id(value: object, *, what: str = "id") -> str:
    """Return ``value`` if it is a usable path component; raise otherwise."""
    if not isinstance(value, str) or not ID_RE.match(value):
        raise ProtocolError(
            f"{what} {value!r} is not a safe path component "
            f"(expected {ID_RE.pattern}, no leading dot)"
        )
    return value


def _refuse_manifest(detail: str, **context: object) -> WebFetchRefused:
    return WebFetchRefused("manifest_invalid", detail, **context)


def _require_exact_keys(obj: object, keys: tuple[str, ...], *, what: str) -> dict:
    if not isinstance(obj, dict):
        raise _refuse_manifest(f"{what}: expected a JSON object, got {type(obj).__name__}")
    present = set(obj)
    expected = set(keys)
    unknown = sorted(present - expected)
    missing = sorted(expected - present)
    if unknown:
        raise _refuse_manifest(
            f"{what}: unknown key(s) {unknown} — the schema is closed; a field this "
            "side does not know is a field it must not silently accept"
        )
    if missing:
        raise _refuse_manifest(f"{what}: missing required key(s) {missing}")
    return dict(obj)


def _require_str(obj: dict, key: str, *, what: str, nullable: bool = False) -> str | None:
    value = obj[key]
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise _refuse_manifest(f"{what}:{key} must be a string")
    return value


def _require_int(obj: dict, key: str, *, what: str, nullable: bool = False) -> int | None:
    value = obj[key]
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _refuse_manifest(f"{what}:{key} must be an integer")
    if value < 0:
        raise _refuse_manifest(f"{what}:{key} must not be negative")
    return value


@dataclass(frozen=True)
class Manifest:
    """A validated ``pending/<job_id>.json`` (design §2.3).

    Deliberately *not* a free-form dict once it has crossed the boundary:
    the sidecar branches on ``kind`` and ``origin``, uses ``job_id`` and
    ``fetch_id`` as path components, and copies ``launch_id`` into the audit
    trail. Every one of those is a place where "whatever the JSON said" is
    the wrong contract.
    """

    schema: int
    job_id: str
    fetch_id: str
    launch_id: str
    program_id: str | None
    url: str
    origin: str
    list_ref: str | None
    kind: str
    conditional: Mapping[str, str | None]
    robots_override_ruling: str | None
    created_ts: str

    @property
    def etag(self) -> str | None:
        return self.conditional.get("etag")

    @property
    def last_modified(self) -> str | None:
        return self.conditional.get("last_modified")

    @property
    def effective_origin(self) -> str:
        """The origin the **sidecar** acts on, which is not always the label.

        ``origin`` is written by the research side, and the research side is
        the untrusted half of this boundary (design §1: the modelled attacker
        is a prompt-injected agent that can run ``trialerror`` unprompted).
        ``operator_list`` is the privileged value — it keeps the query string
        and it is exempt from the ``agent_daily`` cap — so if it were simply
        a word the manifest's writer chose, neither control would bind the
        attacker they were written for.

        It is not. ``operator_list`` means "this URL came off a list a human
        delivered", and a delivered list has a ``list_ref``. A manifest that
        claims the privileged origin without one is downgraded here, at the
        trust boundary, before ``resolve_target`` or any counter sees it.

        This does not make the origin unforgeable — the research side can
        still put *some* string in ``list_ref``, and the sidecar cannot see
        the deliveries directory to check it. What it does is take away the
        free case: the label now has to be accompanied by a claim the
        enqueue path validates (``webfetch add`` requires ``--list-ref`` to
        name a readable file, and ``batch`` derives it from the list's
        sha256), and the default the CLI hands an agent is ``agent``.
        """
        if self.origin == "operator_list" and not self.list_ref:
            return "agent"
        return self.origin

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "job_id": self.job_id,
            "fetch_id": self.fetch_id,
            "launch_id": self.launch_id,
            "program_id": self.program_id,
            "url": self.url,
            "origin": self.origin,
            "list_ref": self.list_ref,
            "kind": self.kind,
            "conditional": {
                "etag": self.conditional.get("etag"),
                "last_modified": self.conditional.get("last_modified"),
            },
            "robots_override_ruling": self.robots_override_ruling,
            "created_ts": self.created_ts,
        }

    @classmethod
    def build(
        cls,
        *,
        job_id: str,
        fetch_id: str,
        launch_id: str,
        url: str,
        kind: str = "page",
        # The unprivileged value is the default: `operator_list` keeps the
        # query string and skips the agent cap, and a constructor that hands
        # that out for free is the shape the two controls exist to prevent.
        origin: str = "agent",
        program_id: str | None = None,
        list_ref: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        robots_override_ruling: str | None = None,
        created_ts: str | None = None,
    ) -> "Manifest":
        """Construct a manifest, validating it exactly as a reader would.

        The enqueue side builds manifests through here so a malformed one
        fails on the *writing* side, where the operator can see the CLI
        error, rather than 45 seconds later in a container with no console.
        """
        return validate_manifest(
            {
                "schema": MANIFEST_SCHEMA,
                "job_id": job_id,
                "fetch_id": fetch_id,
                "launch_id": launch_id,
                "program_id": program_id,
                "url": url,
                "origin": origin,
                "list_ref": list_ref,
                "kind": kind,
                "conditional": {"etag": etag, "last_modified": last_modified},
                "robots_override_ruling": robots_override_ruling,
                "created_ts": created_ts or now(),
            }
        )


def validate_manifest(obj: object) -> Manifest:
    """Validate a manifest structure. Raises ``manifest_invalid`` on anything
    unexpected — unknown keys included."""
    what = "manifest"
    data = _require_exact_keys(obj, MANIFEST_KEYS, what=what)

    if data["schema"] != MANIFEST_SCHEMA:
        raise _refuse_manifest(
            f"{what}:schema is {data['schema']!r}, this build speaks {MANIFEST_SCHEMA}"
        )
    for key in ("job_id", "fetch_id", "launch_id"):
        value = _require_str(data, key, what=what)
        try:
            check_id(value, what=f"{what}:{key}")
        except ProtocolError as exc:
            raise _refuse_manifest(str(exc)) from exc
    program_id = _require_str(data, "program_id", what=what, nullable=True)
    if program_id is not None:
        try:
            check_id(program_id, what=f"{what}:program_id")
        except ProtocolError as exc:
            raise _refuse_manifest(str(exc)) from exc

    url = _require_str(data, "url", what=what)
    assert url is not None
    if not url or len(url) > 8192:
        raise _refuse_manifest(f"{what}:url is empty or absurdly long ({len(url)} bytes)")

    if data["origin"] not in ORIGINS:
        raise _refuse_manifest(f"{what}:origin must be one of {sorted(ORIGINS)}")
    if data["kind"] not in KINDS:
        raise _refuse_manifest(f"{what}:kind must be one of {sorted(KINDS)}")

    list_ref = _require_str(data, "list_ref", what=what, nullable=True)
    ruling = _require_str(data, "robots_override_ruling", what=what, nullable=True)
    created_ts = _require_str(data, "created_ts", what=what)
    assert created_ts is not None
    try:
        parse_ts(created_ts)
    except ValueError as exc:
        raise _refuse_manifest(f"{what}:created_ts is not an ISO-8601 timestamp") from exc

    conditional = data["conditional"]
    cond = _require_exact_keys(conditional, ("etag", "last_modified"), what=f"{what}:conditional")
    for key in ("etag", "last_modified"):
        value = cond[key]
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > 256:
            raise _refuse_manifest(f"{what}:conditional.{key} must be a string of ≤ 256 chars")

    return Manifest(
        schema=MANIFEST_SCHEMA,
        job_id=data["job_id"],
        fetch_id=data["fetch_id"],
        launch_id=data["launch_id"],
        program_id=program_id,
        url=url,
        origin=data["origin"],
        list_ref=list_ref,
        kind=data["kind"],
        conditional={"etag": cond["etag"], "last_modified": cond["last_modified"]},
        robots_override_ruling=ruling,
        created_ts=created_ts,
    )


def validate_result(obj: object) -> dict:
    """Validate a ``result.json`` structure and return it as a plain dict.

    Applied on **both** sides: the sidecar validates what it is about to
    publish (so a bug there fails in the sidecar's own log rather than
    silently poisoning a provenance record), and the research handler
    validates what it reads (so a compromised sidecar cannot hand the
    database a shape nobody checked).
    """
    what = "result"
    data = _require_exact_keys(obj, RESULT_KEYS, what=what)

    if data["schema"] != RESULT_SCHEMA:
        raise _refuse_manifest(
            f"{what}:schema is {data['schema']!r}, this build speaks {RESULT_SCHEMA}"
        )
    for key in ("job_id", "fetch_id", "launch_id"):
        value = _require_str(data, key, what=what)
        try:
            check_id(value, what=f"{what}:{key}")
        except ProtocolError as exc:
            raise _refuse_manifest(str(exc)) from exc

    for key in ("url", "url_norm", "fetched_ts", "sidecar_version"):
        _require_str(data, key, what=what)
    for key in ("final_url", "content_type", "content_class", "content_sha256"):
        _require_str(data, key, what=what, nullable=True)
    for key in ("elapsed_ms", "bytes", "bytes_out"):
        _require_int(data, key, what=what)
    _require_int(data, "http_status", what=what, nullable=True)

    if data["content_class"] is not None and data["content_class"] not in CONTENT_CLASSES:
        raise _refuse_manifest(f"{what}:content_class must be one of {sorted(CONTENT_CLASSES)}")
    if data["outcome"] not in OUTCOMES:
        raise _refuse_manifest(f"{what}:outcome must be one of {sorted(OUTCOMES)}")

    reason = data["reason"]
    if reason is not None:
        if not isinstance(reason, str) or reason not in REASONS:
            raise _refuse_manifest(
                f"{what}:reason {reason!r} is not in the closed vocabulary "
                "(trialerror.webfetch.REASONS)"
            )
    if data["outcome"] == "refused" and reason is None:
        raise _refuse_manifest(f"{what}: outcome 'refused' requires a reason")
    if data["outcome"] != "refused" and reason is not None:
        raise _refuse_manifest(f"{what}: reason is only meaningful with outcome 'refused'")

    for key in ("redirect_chain", "resolved_ips"):
        value = data[key]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise _refuse_manifest(f"{what}:{key} must be a list of strings")
    if len(data["redirect_chain"]) > 32:
        raise _refuse_manifest(f"{what}:redirect_chain is implausibly long")

    headers = _require_exact_keys(
        data["headers_subset"], HEADERS_SUBSET_KEYS, what=f"{what}:headers_subset"
    )
    for key, value in headers.items():
        if value is None:
            continue
        if not isinstance(value, str):
            raise _refuse_manifest(f"{what}:headers_subset.{key} must be a string or null")
        if len(value) > 1024:
            raise _refuse_manifest(f"{what}:headers_subset.{key} exceeds 1 KiB")

    robots = _require_exact_keys(
        data["robots"], ("fetched", "verdict", "crawl_delay_s"), what=f"{what}:robots"
    )
    if not isinstance(robots["fetched"], bool):
        raise _refuse_manifest(f"{what}:robots.fetched must be a boolean")
    if robots["verdict"] not in ROBOTS_VERDICTS:
        raise _refuse_manifest(f"{what}:robots.verdict must be one of {sorted(ROBOTS_VERDICTS)}")
    if isinstance(robots["crawl_delay_s"], bool) or not isinstance(
        robots["crawl_delay_s"], (int, float)
    ):
        raise _refuse_manifest(f"{what}:robots.crawl_delay_s must be a number")

    policy = _require_exact_keys(
        data["policy"], ("verdict", "host_rule", "query_stripped"), what=f"{what}:policy"
    )
    if policy["verdict"] not in _POLICY_VERDICTS:
        raise _refuse_manifest(f"{what}:policy.verdict must be one of {sorted(_POLICY_VERDICTS)}")
    if policy["host_rule"] is not None and not isinstance(policy["host_rule"], str):
        raise _refuse_manifest(f"{what}:policy.host_rule must be a string or null")
    if not isinstance(policy["query_stripped"], bool):
        raise _refuse_manifest(f"{what}:policy.query_stripped must be a boolean")

    git = data["git"]
    if git is not None:
        git_obj = _require_exact_keys(git, ("head", "ref", "path"), what=f"{what}:git")
        for key, value in git_obj.items():
            if value is not None and not isinstance(value, str):
                raise _refuse_manifest(f"{what}:git.{key} must be a string or null")

    return data


def new_result(
    *,
    manifest: Manifest,
    outcome: str,
    url_norm: str,
    final_url: str | None = None,
    fetched_ts: str | None = None,
    elapsed_ms: int = 0,
    http_status: int | None = None,
    content_type: str | None = None,
    content_class: str | None = None,
    payload_bytes: int = 0,
    content_sha256: str | None = None,
    resolved_ips: Iterable[str] = (),
    redirect_chain: Iterable[str] = (),
    bytes_out: int = 0,
    headers_subset: Mapping[str, str | None] | None = None,
    robots: Mapping[str, object] | None = None,
    policy: Mapping[str, object] | None = None,
    reason: str | None = None,
    sidecar_version: str | None = None,
    git: Mapping[str, str | None] | None = None,
) -> dict:
    """Build a validated ``result.json`` body from the fields a fetch knows.

    Every default here is the "nothing happened" value, so a refusal issued
    before a socket was ever opened still produces a complete, schema-valid
    provenance record rather than a sparse one.
    """
    from trialerror.webfetch import SIDECAR_VERSION

    headers = {key: None for key in HEADERS_SUBSET_KEYS}
    for key, value in (headers_subset or {}).items():
        lowered = key.lower()
        if lowered in headers:
            headers[lowered] = value

    robots_block = {"fetched": False, "verdict": "n/a", "crawl_delay_s": 0}
    robots_block.update(dict(robots or {}))
    policy_block: dict[str, object] = {
        "verdict": "refused" if outcome == "refused" else "allow",
        "host_rule": None,
        "query_stripped": False,
    }
    policy_block.update(dict(policy or {}))

    return validate_result(
        {
            "schema": RESULT_SCHEMA,
            "job_id": manifest.job_id,
            "fetch_id": manifest.fetch_id,
            "launch_id": manifest.launch_id,
            "url": manifest.url,
            "url_norm": url_norm,
            "final_url": final_url,
            "redirect_chain": list(redirect_chain),
            "fetched_ts": fetched_ts or now(),
            "elapsed_ms": int(elapsed_ms),
            "http_status": http_status,
            "content_type": content_type,
            "content_class": content_class,
            "bytes": int(payload_bytes),
            "content_sha256": content_sha256,
            "resolved_ips": list(resolved_ips),
            "bytes_out": int(bytes_out),
            "headers_subset": headers,
            "robots": robots_block,
            "policy": policy_block,
            "outcome": outcome,
            "reason": reason,
            "sidecar_version": sidecar_version or SIDECAR_VERSION,
            "git": dict(git) if git is not None else None,
        }
    )


def append_jsonl(path: str | Path, record: Mapping[str, Any]) -> None:
    """Append one JSON line.

    A single small ``write`` in append mode is atomic enough for the audit
    and proposal trails: the sidecar is single-threaded (design §4 T4), the
    lines are far below any pipe/write boundary, and the alternative
    (rewrite-and-rename) would lose concurrent appends rather than
    interleave them safely.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, ensure_ascii=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")


# --------------------------------------------------------------------------
# the queue
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    """One job, claimed by one worker.

    ``manifest`` is ``None`` exactly when ``error`` is set: the manifest was
    claimed (so no other worker will pick it up and hit the same wall) but
    did not validate. The sidecar's job then is to publish a ``refused``
    result with reason ``manifest_invalid`` — a bad manifest is still an
    event that must be recorded and audited, not a file quietly left to rot.
    """

    job_id: str
    worker_id: str
    manifest: Manifest | None
    manifest_path: Path
    heartbeat_path: Path
    lock_path: Path
    error: WebFetchRefused | None = None

    @property
    def ok(self) -> bool:
        return self.manifest is not None


@dataclass(frozen=True)
class SubmitResult:
    """What :meth:`Queue.submit` did (design §2.2 step 2: a manifest already
    present in *any* state means nothing new is written)."""

    job_id: str
    state: str
    wrote: bool
    path: Path


class Queue:
    """The shared directory, from either side.

    ``_now_fn`` is the clock seam used by heartbeat, reclaim and expiry
    tests — the same ``_time_fn`` convention the existing rate limiter uses.
    Production callers never pass it.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        _now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(root)
        self._now_fn = _now_fn or now_dt

    # -- layout ----------------------------------------------------------
    @property
    def pending_dir(self) -> Path:
        return self.root / PENDING_DIRNAME

    @property
    def claimed_dir(self) -> Path:
        return self.root / CLAIMED_DIRNAME

    @property
    def done_dir(self) -> Path:
        return self.root / DONE_DIRNAME

    @property
    def failed_dir(self) -> Path:
        return self.root / FAILED_DIRNAME

    @property
    def partial_dir(self) -> Path:
        """Staging for publication. Inside ``done/`` so the publish rename
        never crosses a filesystem boundary and can therefore be atomic."""
        return self.done_dir / PARTIAL_DIRNAME

    @property
    def proposals_path(self) -> Path:
        return self.root / PROPOSALS_FILENAME

    @property
    def audit_copy_path(self) -> Path:
        return self.root / AUDIT_COPY_FILENAME

    @property
    def heartbeat_path(self) -> Path:
        return self.root / SIDECAR_HEARTBEAT_FILENAME

    def ensure_layout(self) -> "Queue":
        for directory in (
            self.pending_dir,
            self.claimed_dir,
            self.done_dir,
            self.partial_dir,
            self.failed_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def pending_path(self, job_id: str) -> Path:
        return self.pending_dir / f"{check_id(job_id, what='job_id')}.json"

    def done_path(self, job_id: str) -> Path:
        return self.done_dir / check_id(job_id, what="job_id")

    def failed_path(self, job_id: str) -> Path:
        return self.failed_dir / check_id(job_id, what="job_id")

    def claimed_paths(self, job_id: str) -> list[Path]:
        """Every ``claimed/<worker>/<job>.json`` for this job (normally 0 or 1)."""
        check_id(job_id, what="job_id")
        if not self.claimed_dir.is_dir():
            return []
        return sorted(self.claimed_dir.glob(f"*/{job_id}.json"))

    # -- research side ---------------------------------------------------
    def state_of(self, job_id: str) -> str:
        """Where this job is, checked terminal-first.

        ``done/.partial/<job>`` is deliberately invisible here: it is not
        ``done/<job>``, and no id may begin with ``.``, so a half-written
        result can never be mistaken for a finished one.
        """
        check_id(job_id, what="job_id")
        if (self.done_path(job_id) / RESULT_FILENAME).is_file():
            return STATE_DONE
        if (self.failed_path(job_id) / RESULT_FILENAME).is_file():
            return STATE_FAILED
        if self.claimed_paths(job_id):
            return STATE_CLAIMED
        if self.pending_path(job_id).is_file():
            return STATE_PENDING
        return STATE_ABSENT

    def submit(self, manifest: Manifest) -> SubmitResult:
        """Write ``pending/<job>.json`` unless the job exists in any state.

        Idempotent by design (§2.2 step 2): the ``web_fetch`` handler is
        re-entered every jobs-worker cycle while it waits, and each re-entry
        must be a no-op rather than a second fetch of the same URL.

        The existence check and the write are not one atomic step. They do
        not need to be: the research side has a single jobs worker, and two
        submissions of the same job would carry byte-identical content
        anyway (the manifest is a function of the job).
        """
        self.ensure_layout()
        job_id = check_id(manifest.job_id, what="job_id")
        state = self.state_of(job_id)
        path = self.pending_path(job_id)
        if state != STATE_ABSENT:
            return SubmitResult(job_id=job_id, state=state, wrote=False, path=path)
        atomic_write_text(path, json.dumps(manifest.to_dict(), sort_keys=True, indent=2) + "\n")
        return SubmitResult(job_id=job_id, state=STATE_PENDING, wrote=True, path=path)

    def read_manifest(self, job_id: str) -> Manifest:
        """Read the manifest wherever it currently lives."""
        for path in (self.pending_path(job_id), *self.claimed_paths(job_id)):
            if path.is_file():
                return self._read_manifest_file(path)
        raise ProtocolError(f"no manifest for job {job_id!r} in {self.root}")

    def read_result(self, job_id: str) -> tuple[str, dict]:
        """Return ``(state, result)`` for a terminal job."""
        for state, directory in (
            (STATE_DONE, self.done_path(job_id)),
            (STATE_FAILED, self.failed_path(job_id)),
        ):
            path = directory / RESULT_FILENAME
            if path.is_file():
                return state, self._read_json(path, _MAX_RESULT_BYTES, what="result", loader=validate_result)
        raise ProtocolError(f"job {job_id!r} has no result in {self.root}")

    def payload_path(self, job_id: str, filename: str) -> Path:
        """A path inside ``done/<job>/`` for one of the fixed payload names."""
        if filename not in PAYLOAD_FILENAMES:
            raise ProtocolError(
                f"{filename!r} is not a payload name; the research side reads only "
                f"{sorted(PAYLOAD_FILENAMES)}"
            )
        return self.done_path(job_id) / filename

    def verify_payload(self, job_id: str, result: Mapping[str, Any]) -> Path | None:
        """Re-check a published payload's size and sha256 against its result.

        The research handler calls this before copying anything into the
        corpus (design §2.2 step 2). A mismatch is a *logic* failure there —
        it means the two sides disagree about what was fetched, and the
        conservative reading is that neither number can be trusted.
        """
        if result.get("outcome") == "refused" or not result.get("content_sha256"):
            return None
        filename = REPO_FILENAME if result.get("content_class") == "git" else BODY_FILENAME
        path = self.payload_path(job_id, filename)
        if not path.is_file():
            raise ProtocolError(f"published result for {job_id!r} has no {filename}")
        size = path.stat().st_size
        if size != int(result["bytes"]):
            raise ProtocolError(
                f"{job_id}: {filename} is {size} bytes, result.json says {result['bytes']}"
            )
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != result["content_sha256"]:
            raise ProtocolError(f"{job_id}: {filename} sha256 does not match result.json")
        return path

    def sweep(self, job_id: str) -> bool:
        """Delete ``done/<job>/`` once the research side has ingested it."""
        directory = self.done_path(job_id)
        if not directory.is_dir():
            return False
        shutil.rmtree(directory, ignore_errors=True)
        return not directory.exists()

    def expired_pending(self, expires_after_s: float) -> list[str]:
        """Pending job ids whose manifest is older than ``expires_after_s``.

        Design §2.2: such a manifest means the sidecar is not running, and
        the handler turns it into a *visible* logic failure rather than
        waiting forever.
        """
        cutoff = self._now_fn() - timedelta(seconds=float(expires_after_s))
        stale: list[str] = []
        for path in self._pending_files():
            job_id = path.stem
            created = self._created_at(path)
            if created is not None and created < cutoff:
                stale.append(job_id)
        return sorted(stale)

    def _created_at(self, path: Path) -> datetime | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return parse_ts(str(data["created_ts"]))
        except (OSError, ValueError, KeyError, TypeError):
            # A manifest we cannot read still ages: fall back to the file's
            # own mtime so an unparseable file cannot pin the queue open.
            try:
                return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                return None

    # -- sidecar side ----------------------------------------------------
    def _pending_files(self) -> list[Path]:
        if not self.pending_dir.is_dir():
            return []
        return sorted(p for p in self.pending_dir.glob("*.json") if ID_RE.match(p.stem))

    def pending_job_ids(self) -> list[str]:
        return [p.stem for p in self._pending_files()]

    def claim_next(self, worker_id: str) -> Claim | None:
        """Claim the oldest claimable pending job, or return ``None``.

        Ordering is by file name. Fetch ids are ULID-prefixed, so name order
        is creation order — with no directory-mtime scan and no second stat.
        """
        for path in self._pending_files():
            claim = self.claim(path.stem, worker_id)
            if claim is not None:
                return claim
        return None

    def claim_lock_path(self, job_id: str) -> Path:
        """The per-job exclusion token. One namespace for all workers — that
        is what makes ``O_EXCL`` mean "exactly one of you"."""
        return self.claimed_dir / f"{check_id(job_id, what='job_id')}{CLAIM_LOCK_SUFFIX}"

    def claim(self, job_id: str, worker_id: str) -> Claim | None:
        """Take exclusive ownership of a job, or return ``None``.

        Two steps, in this order and no other: an exclusive *create* decides
        who won (``O_CREAT|O_EXCL`` is genuinely exclusive on both platforms;
        a bare rename is not — see the module docstring), and only the winner
        then moves the manifest. The loser never opens the manifest, so there
        is no window in which two workers act on one job.
        """
        check_id(job_id, what="job_id")
        check_id(worker_id, what="worker_id")
        self.claimed_dir.mkdir(parents=True, exist_ok=True)
        lock = self.claim_lock_path(job_id)
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return None
        except OSError:
            return None
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(f"{worker_id}\n")
        except OSError:  # pragma: no cover - defensive
            lock.unlink(missing_ok=True)
            return None

        source = self.pending_path(job_id)
        target_dir = self.claimed_dir / worker_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{job_id}.json"
        try:
            os.replace(source, target)
        except OSError:
            # The lock was ours but the manifest is gone — settled by someone
            # else between the listing and now. Release the lock so the job is
            # not parked behind a token nobody is using.
            lock.unlink(missing_ok=True)
            return None

        heartbeat = target_dir / f"{job_id}.heartbeat"
        self._write_heartbeat(heartbeat)
        common = {
            "job_id": job_id,
            "worker_id": worker_id,
            "manifest_path": target,
            "heartbeat_path": heartbeat,
            "lock_path": lock,
        }
        try:
            manifest = self._read_manifest_file(target)
        except WebFetchRefused as exc:
            return Claim(manifest=None, error=exc, **common)
        if manifest.job_id != job_id:
            return Claim(
                manifest=None,
                error=_refuse_manifest(
                    f"manifest:job_id is {manifest.job_id!r} but the file is named {job_id!r}"
                ),
                **common,
            )
        return Claim(manifest=manifest, **common)

    def heartbeat(self, claim: Claim) -> None:
        """Refresh a claim's heartbeat — call it around anything slow."""
        self._write_heartbeat(claim.heartbeat_path)

    def _write_heartbeat(self, path: Path) -> None:
        atomic_write_text(path, self._iso_now() + "\n")

    def touch_sidecar_heartbeat(self, extra: Mapping[str, Any] | None = None) -> None:
        """Write ``sidecar.heartbeat`` — the file doctor and ``te-status.sh``
        read to decide whether the sidecar is alive (design §3.4)."""
        record = {"ts": self._iso_now(), "pending": len(self._pending_files())}
        if extra:
            record.update(dict(extra))
        atomic_write_text(self.heartbeat_path, json.dumps(record, sort_keys=True) + "\n")

    def sidecar_heartbeat_age_s(self) -> float | None:
        """Seconds since the sidecar last said it was alive; ``None`` if it
        never has."""
        path = self.heartbeat_path
        if not path.is_file():
            return None
        stamp: datetime | None = None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            stamp = parse_ts(str(record["ts"]))
        except (OSError, ValueError, KeyError, TypeError):
            try:
                stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                return None
        return max(0.0, (self._now_fn() - stamp).total_seconds())

    def reclaim(self, max_age_s: float) -> list[str]:
        """Return claims whose heartbeat is older than ``max_age_s`` to
        ``pending/``.

        The sidecar is single-threaded and restarts clean, so this is a
        recovery path for a killed container rather than a routine one: a
        job claimed by a process that no longer exists must become
        claimable again, or it waits out the 24 h manifest expiry for
        nothing.
        """
        cutoff = self._now_fn() - timedelta(seconds=float(max_age_s))
        reclaimed: list[str] = []
        if not self.claimed_dir.is_dir():
            return reclaimed
        for manifest_path in sorted(self.claimed_dir.glob("*/*.json")):
            job_id = manifest_path.stem
            if not ID_RE.match(job_id):
                continue
            heartbeat = manifest_path.with_suffix(".heartbeat")
            stamp = self._heartbeat_stamp(heartbeat) or self._file_stamp(manifest_path)
            if stamp is not None and stamp >= cutoff:
                continue
            target = self.pending_path(job_id)
            if target.exists() or self.done_path(job_id).exists() or self.failed_path(job_id).exists():
                # Already superseded elsewhere; just drop the stale claim.
                manifest_path.unlink(missing_ok=True)
                heartbeat.unlink(missing_ok=True)
                self.claim_lock_path(job_id).unlink(missing_ok=True)
                continue
            self.pending_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(manifest_path, target)
            except OSError:
                continue
            heartbeat.unlink(missing_ok=True)
            self.claim_lock_path(job_id).unlink(missing_ok=True)
            reclaimed.append(job_id)

        # A worker killed between taking the token and moving the manifest
        # leaves a token with nothing behind it. Without this sweep the job
        # would sit in pending/ forever, claimable by nobody.
        for lock in sorted(self.claimed_dir.glob(f"*{CLAIM_LOCK_SUFFIX}")):
            job_id = lock.name[: -len(CLAIM_LOCK_SUFFIX)]
            if not ID_RE.match(job_id) or self.claimed_paths(job_id):
                continue
            stamp = self._file_stamp(lock)
            if stamp is not None and stamp >= cutoff:
                continue
            lock.unlink(missing_ok=True)
        return sorted(reclaimed)

    def _heartbeat_stamp(self, path: Path) -> datetime | None:
        if not path.is_file():
            return None
        try:
            return parse_ts(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return self._file_stamp(path)

    @staticmethod
    def _file_stamp(path: Path) -> datetime | None:
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return None

    # -- publication -----------------------------------------------------
    def publish(
        self,
        claim: Claim,
        result: Mapping[str, Any],
        payloads: Mapping[str, bytes | Path] | None = None,
    ) -> Path:
        """Publish a successful result into ``done/<job>/`` atomically."""
        return self._publish(claim, result, payloads, self.done_path(claim.job_id))

    def fail(self, claim: Claim, result: Mapping[str, Any]) -> Path:
        """Publish a refusal into ``failed/<job>/``.

        A refusal carries no payload: the bytes either never arrived or are
        exactly what policy said must not cross. It is published with the
        same atomic rename so the research side never reads half a refusal.
        """
        return self._publish(claim, result, None, self.failed_path(claim.job_id))

    def _publish(
        self,
        claim: Claim,
        result: Mapping[str, Any],
        payloads: Mapping[str, bytes | Path] | None,
        target: Path,
    ) -> Path:
        validated = validate_result(result)
        if validated["job_id"] != claim.job_id:
            raise ProtocolError(
                f"result job_id {validated['job_id']!r} does not match the claim {claim.job_id!r}"
            )
        for name in (payloads or {}):
            if name not in PAYLOAD_FILENAMES:
                raise ProtocolError(
                    f"payload {name!r} is not one of {sorted(PAYLOAD_FILENAMES)}"
                )

        self.ensure_layout()
        staging = self.partial_dir / claim.job_id
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)

        for name, payload in (payloads or {}).items():
            destination = staging / name
            if isinstance(payload, (bytes, bytearray)):
                atomic_write_bytes(destination, bytes(payload))
            else:
                shutil.copyfile(Path(payload), destination)
        atomic_write_text(
            staging / RESULT_FILENAME, json.dumps(validated, sort_keys=True, indent=2) + "\n"
        )

        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(staging, ignore_errors=True)
            raise AlreadyPublished(f"{target} already exists")
        try:
            os.rename(staging, target)
        except OSError as exc:
            if target.exists():
                shutil.rmtree(staging, ignore_errors=True)
                raise AlreadyPublished(f"{target} already exists") from exc
            raise
        self.release(claim)
        return target

    def release(self, claim: Claim) -> None:
        """Drop a claim's files, exclusion token last. Safe to call twice."""
        claim.manifest_path.unlink(missing_ok=True)
        claim.heartbeat_path.unlink(missing_ok=True)
        claim.lock_path.unlink(missing_ok=True)

    def unclaim(self, claim: Claim) -> bool:
        """Put a claimed job back in ``pending/`` without settling it.

        For the case where the *worker* is unable to proceed — an unreadable
        policy, a shutdown mid-job — rather than the job being refusable.
        The job must stay claimable; abandoning it silently would leave the
        research side waiting out the 24 h manifest expiry for a fetch that
        was never attempted.
        """
        if not claim.manifest_path.is_file():
            return False
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        target = self.pending_path(claim.job_id)
        try:
            os.replace(claim.manifest_path, target)
        except OSError:
            return False
        claim.heartbeat_path.unlink(missing_ok=True)
        claim.lock_path.unlink(missing_ok=True)
        return True

    # -- housekeeping ----------------------------------------------------
    def append_proposal(self, record: Mapping[str, Any]) -> None:
        """Record a host an agent would like approved (design §3.3).

        Writing here is the *only* thing the fetch path may do about an
        unapproved host. Approval is a human command on the host machine.
        """
        append_jsonl(self.proposals_path, {"ts": self._iso_now(), **dict(record)})

    def append_audit(self, record: Mapping[str, Any]) -> None:
        """Append the in-queue copy of an audit line.

        The authoritative copy lives in the sidecar's ``/audit`` mount,
        which the research container cannot see (design §3.4). This copy
        exists so the in-container doctor check has something to read; the
        host check is the one that counts.
        """
        append_jsonl(self.audit_copy_path, {"ts": self._iso_now(), **dict(record)})

    def disk_usage_bytes(self) -> int:
        """Total bytes under the queue root — the ``queue_disk_cap`` input."""
        total = 0
        for path in self.root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
        return total

    def iter_results(self) -> Iterator[tuple[str, str, dict]]:
        """Yield ``(state, job_id, result)`` for every terminal job."""
        for state, directory in ((STATE_DONE, self.done_dir), (STATE_FAILED, self.failed_dir)):
            if not directory.is_dir():
                continue
            for child in sorted(directory.iterdir()):
                if not child.is_dir() or not ID_RE.match(child.name):
                    continue
                path = child / RESULT_FILENAME
                if not path.is_file():
                    continue
                try:
                    yield state, child.name, self._read_json(
                        path, _MAX_RESULT_BYTES, what="result", loader=validate_result
                    )
                except WebFetchRefused:
                    continue

    # -- io --------------------------------------------------------------
    def _iso_now(self) -> str:
        moment = self._now_fn()
        return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"

    def _read_manifest_file(self, path: Path) -> Manifest:
        return self._read_json(path, _MAX_MANIFEST_BYTES, what="manifest", loader=validate_manifest)

    @staticmethod
    def _read_json(path: Path, max_bytes: int, *, what: str, loader: Callable[[object], Any]) -> Any:
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise _refuse_manifest(f"{what}: cannot stat {path.name}: {exc}") from exc
        if size > max_bytes:
            raise _refuse_manifest(
                f"{what}: {path.name} is {size} bytes, cap {max_bytes} — refused unread"
            )
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise _refuse_manifest(f"{what}: cannot read {path.name}: {exc}") from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise _refuse_manifest(f"{what}: {path.name} is not valid JSON: {exc}") from exc
        return loader(data)
