"""The source investigator (lane SI part B): vet cited works before any of
them reaches a human's request queue.

A research programme that asks a human to obtain books and papers needs to
know, per cited work: does the cited identifier resolve to the work the
citation describes, is the work already held (or already asked for), who cites
it, did a later review consolidate it, did its authors publish a fuller
treatment since. :func:`run_investigation` answers those mechanically, through
the EXISTING providers (:class:`~trialerror.litapi.client.LitApiClient` over
OpenAlex and Semantic Scholar) and writes one JSON dossier per seed plus a
``source_dossier`` row; :func:`record_verdict` records the reader's verdict on
a dossier under a fixed set of refusals; :func:`render_list` turns a judged
list into the lines a human acts on. The CLI (``trialerror lit investigate
run|verdict|render``, ``trialerror/cli/lit.py``) only wires these.

What this module never does: fetch a full text, call a model, or fill a
dossier field from anything but a provider response, the local arXiv index,
the store, or the seed as given.

**The evidence cache.** Every provider call made during a run goes through
``source_evidence`` (knowledge schema v15) first: each provider is wrapped in
a :class:`CachedProvider`, so the client's own orchestration (first success,
per-provider outcomes, reconciliation) runs unchanged while each provider's
answer to each call is kept -- keyed by the subject the call was about, the
provider, the kind of call and its exact parameters. An answer is served from
the cache unless its outcome is ``rate_limited``, ``transport_unreachable`` or
``http_error`` (those are re-asked, and the new answer retires the old row);
``--no-cache`` asks every provider again and records the answers the same way.

**The call cap.** ``max_calls_per_seed`` counts CLIENT-level calls issued for a
seed -- ``lookup_doi``, ``lookup_arxiv``, ``search``, the two ``get_citations``,
``get_references``, ``get_author_works`` -- whether the cache or a provider
answered them, so a dossier is the same whatever the cache held. The default
plan is at most seven calls; the default cap is eight.
"""

from __future__ import annotations

import csv
import functools
import hashlib
import inspect
import json
import re
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from trialerror.litapi.citeparse import CitedWork, isbn_to_13, parse_citation, parse_seed_list, text_without_identifiers
from trialerror.litapi.client import AuthorWorksResult, LitApiClient, outcome_word
from trialerror.litapi.config import InvestigateConfig
from trialerror.litapi.errors import (
    AllProvidersFailedError,
    LitApiError,
    ProviderConfigError,
    ProviderNotFoundError,
    ProviderUnsupportedOperationError,
)
from trialerror.litapi.match import MatchResult, best_title_match, match_citation, title_similarity
from trialerror.litapi.models import (
    CitationEdge,
    CitationsPage,
    WorkRecord,
    arxiv_to_doi,
    normalize_arxiv_id,
    normalize_doi,
    normalize_title,
    provider_extra,
)
from trialerror.stores.store import Store
from trialerror.stores.writer import insert, require_xid_targets, update
from trialerror.util.atomic import atomic_write_bytes
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now, now_dt

__all__ = [
    "STAGE_VERSION",
    "DOSSIER_VERSION",
    "MECHANICAL_STATES",
    "VERDICTS",
    "NEVER_FROM_CACHE",
    "InvestigateError",
    "Seed",
    "load_seeds",
    "cited_subject_key",
    "record_subject_key",
    "seed_slug",
    "EvidenceCache",
    "CachedProvider",
    "HeldMatch",
    "HeldIndex",
    "held_match",
    "load_delivered_manifest",
    "arxiv_neighbour_search",
    "run_investigation",
    "record_verdict",
    "render_list",
]

#: Written into every dossier and ``source_dossier`` row; bump when what a
#: run writes changes meaning.
STAGE_VERSION = "source-investigator-1"
DOSSIER_VERSION = 1

#: In precedence order: a seed is ``held`` before anything else, and ``open``
#: only when nothing else applies.
MECHANICAL_STATES: tuple[str, ...] = ("held", "wrong_identifier", "retry", "need_info", "open")
VERDICTS: tuple[str, ...] = ("REQUEST", "REQUEST-AS-FOUNDATIONAL", "SUBSTITUTE-WITH", "HELD", "DROP", "NEED-INFO")
FOUNDATIONAL_REASONS: tuple[str, ...] = ("own-text", "no-consolidator")

#: Provider outcomes never served from the evidence cache: they say "could
#: not ask", not "asked and answered", so they are re-asked.
NEVER_FROM_CACHE = frozenset({"rate_limited", "transport_unreachable", "http_error"})

#: ``source.request_state`` values whose text is held, and the two that mean
#: an ask is already open. ``rejected``/``failed`` are neither and never match.
HELD_TEXT_STATES = frozenset({"delivered", "verifying", "archived", "indexed"})
OPEN_REQUEST_STATES = frozenset({"wanted", "requested"})

SEARCH_LIMIT = 5
AUTHOR_POSITIONS = 3
ARXIV_NEIGHBOURS_K = 10
MANIFEST_COLUMNS: tuple[str, ...] = ("path", "title", "doi", "isbn")

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CACHED_METHODS: tuple[str, ...] = (
    "get_by_doi", "get_by_arxiv", "search", "get_citations", "get_references", "get_author_works",
)
_NOTHING_SENT: dict[str, Any] = {
    "attempts": 0, "waited_s": 0.0, "last_status": None, "retry_after_s": None, "request_sent": False,
}
_RANK = {"exact": 0, "probable": 1, "mismatch": 2, "none": 3}


class InvestigateError(Exception):
    """A refusal. ``code`` is the envelope's error code and ``details`` its
    structured detail; nothing has been written when one is raised."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = dict(details) if details else {}


def _current_year() -> int:
    """This year, UTC. A module function so a test can pin it."""
    return now_dt().year


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _dossier_bytes(dossier: Mapping[str, Any]) -> bytes:
    return (json.dumps(dossier, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# seeds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Seed:
    """One cited work to investigate, as the seeds file gave it. ``line`` is
    the 1-based line of the seeds file and ``position`` the seed's index
    within that line (``0`` for a ``seed_raw`` line), so a rendered list keeps
    the order the list was written in."""

    list_id: str
    row_id: str
    seed_raw: str
    question: str | None = None
    literature: Any = None
    line: int = 0
    position: int = 0


def load_seeds(path: Path | str) -> tuple[list[Seed], list[str]]:
    """Read a seeds JSONL file: one object per line, ``{"list_id", "row_id",
    "seed_raw", "question", "literature"}``, or ``"seeds_raw"`` in place of
    ``"seed_raw"`` for a whole row (split by
    :func:`~trialerror.litapi.citeparse.parse_seed_list`).

    The whole file is validated before anything is investigated: any bad line
    raises :class:`InvestigateError` (``seeds_file_invalid``) naming every
    problem. ``list_id``/``row_id`` become directory names, so they must be
    plain ids (letters, digits, ``.``, ``_``, ``-``; starting alphanumeric).
    A seed repeated under the same list and row is investigated once; the
    repeats are returned as warnings."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InvestigateError("seeds_file_invalid", f"cannot read {path}: {exc}") from exc
    seeds: list[Seed] = []
    problems: list[str] = []
    warnings: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError as exc:
            problems.append(f"line {number}: not JSON ({exc})")
            continue
        if not isinstance(obj, dict):
            problems.append(f"line {number}: not a JSON object")
            continue
        ids_ok = True
        for key in ("list_id", "row_id"):
            value = obj.get(key)
            if not isinstance(value, str) or not _SAFE_ID_RE.match(value):
                problems.append(
                    f"line {number}: {key} must be a plain id (letters, digits, '.', '_', '-'; "
                    f"starting with a letter or digit), got {value!r}"
                )
                ids_ok = False
        question = obj.get("question")
        if question is not None and not isinstance(question, str):
            problems.append(f"line {number}: question must be a string or absent")
            continue
        has_one, has_row = "seed_raw" in obj, "seeds_raw" in obj
        if has_one == has_row:
            problems.append(f"line {number}: give exactly one of seed_raw or seeds_raw")
            continue
        raw_value = obj.get("seed_raw" if has_one else "seeds_raw")
        if not isinstance(raw_value, str) or not raw_value.strip():
            problems.append(f"line {number}: {'seed_raw' if has_one else 'seeds_raw'} must be a non-empty string")
            continue
        if not ids_ok:
            continue
        raws = [raw_value.strip()] if has_one else [cited.raw for cited in parse_seed_list(raw_value)]
        if not raws:
            problems.append(f"line {number}: seeds_raw holds no citation")
            continue
        for position, raw in enumerate(raws):
            key = (obj["list_id"], obj["row_id"], raw)
            if key in seen:
                warnings.append(f"line {number}: seed {raw!r} repeats one already given for {key[0]}/{key[1]}; skipped")
                continue
            seen.add(key)
            seeds.append(
                Seed(
                    list_id=obj["list_id"], row_id=obj["row_id"], seed_raw=raw, question=question,
                    literature=obj.get("literature"), line=number, position=position,
                )
            )
    if problems:
        raise InvestigateError(
            "seeds_file_invalid", f"{path}: {len(problems)} problem(s); nothing was investigated",
            details={"problems": problems},
        )
    if not seeds:
        raise InvestigateError("seeds_file_invalid", f"{path}: no seeds", details={"problems": ["no seeds"]})
    return seeds, warnings


# ---------------------------------------------------------------------------
# subject keys and slugs
# ---------------------------------------------------------------------------


def _title_key(title: str | None, year: int | None) -> str:
    return f"title:{normalize_title(title) or ''}|{year if isinstance(year, int) else ''}"


def cited_subject_key(cited: CitedWork) -> str:
    """The key a seed's resolve calls are cached under: its normalised DOI,
    else ``arxiv:<id>``, else ``isbn:<isbn13>``, else ``title:<normalised
    title>|<year>`` (the quoted title, or the citation's prose without its
    identifiers)."""
    if cited.doi:
        return cited.doi
    if cited.arxiv_id:
        return f"arxiv:{cited.arxiv_id}"
    if cited.isbn:
        return f"isbn:{cited.isbn}"
    return _title_key(cited.title_hint or text_without_identifiers(cited.raw), cited.year)


def record_subject_key(record: WorkRecord) -> str:
    """The key a resolved work's gather calls are cached under -- shared by
    every seed that resolves to the same work, whatever its citation said."""
    doi = normalize_doi(record.doi)
    if doi:
        return doi
    arxiv_id = normalize_arxiv_id(record.arxiv_id)
    if arxiv_id:
        return f"arxiv:{arxiv_id}"
    return _title_key(record.title, record.year)


def seed_slug(seed_raw: str) -> str:
    """A filesystem-safe, deterministic file stem for one seed: a readable
    prefix plus a short hash of the exact seed text."""
    base = re.sub(r"[^a-z0-9]+", "-", seed_raw.casefold()).strip("-")[:48].strip("-") or "seed"
    return f"{base}-{hashlib.sha256(seed_raw.encode('utf-8')).hexdigest()[:10]}"


def _gather_identifier(record: WorkRecord) -> str | None:
    """What the gather calls ask about: the DOI, else arXiv's own DOI for an
    arXiv id (both providers resolve a DOI), else a provider-native id (served
    by the provider that issued it; the other answers not-found)."""
    doi = normalize_doi(record.doi)
    if doi:
        return doi
    arxiv_doi = arxiv_to_doi(record.arxiv_id)
    if arxiv_doi:
        return arxiv_doi
    return record.external_ids.get("openalex") or record.external_ids.get("semanticscholar") or None


def _store_path(path: Path, program_root: Path) -> str:
    """Relative to the program root when under it (so the store survives the
    root moving), else absolute."""
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(Path(program_root).resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _load_path(stored: str, program_root: Path) -> Path:
    candidate = Path(stored)
    return candidate if candidate.is_absolute() else Path(program_root) / candidate


# ---------------------------------------------------------------------------
# (de)serialising provider answers
# ---------------------------------------------------------------------------


def _record_from_dict(d: Mapping[str, Any]) -> WorkRecord:
    return WorkRecord(
        title=d.get("title"), doi=d.get("doi"), arxiv_id=d.get("arxiv_id"), authors=list(d.get("authors") or []),
        year=d.get("year"), venue=d.get("venue"), abstract=d.get("abstract"), citation_count=d.get("citation_count"),
        oa_pdf_url=d.get("oa_pdf_url"), url=d.get("url"), external_ids=dict(d.get("external_ids") or {}),
        providers=list(d.get("providers") or []), other=dict(d.get("other") or {}),
    )


def _edge_from_dict(d: Mapping[str, Any]) -> CitationEdge:
    return CitationEdge(
        title=d.get("title"), doi=d.get("doi"), arxiv_id=d.get("arxiv_id"), year=d.get("year"),
        authors=list(d.get("authors") or []), external_ids=dict(d.get("external_ids") or {}),
        work_type=d.get("work_type"), citation_count=d.get("citation_count"),
    )


def _page_from_dict(d: Mapping[str, Any]) -> CitationsPage:
    items = [_edge_from_dict(i) for i in d.get("items") or []]
    return CitationsPage(
        items=items, provider=d.get("provider") or "", offset=int(d.get("offset") or 0),
        limit=int(d.get("limit") or len(items)), total=d.get("total"), has_more=bool(d.get("has_more")),
    )


def _encode_answer(value: Any, exc: LitApiError | None) -> dict[str, Any]:
    if exc is not None:
        return {
            "error": {
                "class": type(exc).__name__, "message": str(exc),
                "status_code": getattr(exc, "status_code", None),
            }
        }
    if value is None:
        return {"value": None}
    if isinstance(value, WorkRecord):
        return {"value": value.to_dict()}
    if isinstance(value, CitationsPage):
        page = value.to_dict()
        page.pop("provider_outcomes", None)  # the client's, not the provider's
        return {"value": page}
    if isinstance(value, list):
        return {"value": [v.to_dict() for v in value]}
    raise TypeError(f"cannot cache a provider answer of type {type(value).__name__}")


def _replay_answer(method: str, payload: Mapping[str, Any], provider_name: str) -> Any:
    """A cached answer handed back exactly as the provider gave it: the value
    rebuilt, or the recorded refusal raised again with its own message."""
    error = payload.get("error")
    if error:
        message = str(error.get("message") or "")
        cls = error.get("class")
        if cls == "ProviderNotFoundError":
            raise ProviderNotFoundError(message, provider=provider_name)
        if cls == "ProviderUnsupportedOperationError":
            raise ProviderUnsupportedOperationError(message, provider=provider_name)
        if cls == "ProviderConfigError":
            raise ProviderConfigError(message)
        raise LitApiError(message)
    value = payload.get("value")
    if method in ("get_by_doi", "get_by_arxiv"):
        return None if value is None else _record_from_dict(value)
    if method in ("search", "get_author_works"):
        return [_record_from_dict(v) for v in value or []]
    return _page_from_dict(value or {})


def _answer_found(method: str, value: Any) -> bool:
    """The same "found" the client reports per provider: a record for a
    lookup, a non-empty list for a search or author works, and any page for a
    listing (an empty page is an answer)."""
    if method in ("get_by_doi", "get_by_arxiv"):
        return value is not None
    if method in ("search", "get_author_works"):
        return bool(value)
    return True


def _kind_for(method: str, params: Mapping[str, Any]) -> str:
    if method in ("get_by_doi", "get_by_arxiv"):
        return "record"
    if method == "search":
        return "search"
    if method == "get_citations":
        return "citing_reviews" if params.get("work_type") == "review" else "citing"
    if method == "get_references":
        return "references"
    return "author_works"


def _fallback_subject(method: str, params: Mapping[str, Any]) -> str:
    """Only for a call made outside :meth:`EvidenceCache.subject` (never the
    case inside a run): the call's own identifier."""
    if method == "get_by_doi":
        return normalize_doi(str(params.get("doi") or "")) or ""
    if method == "get_by_arxiv":
        return f"arxiv:{normalize_arxiv_id(str(params.get('arxiv_id') or '')) or ''}"
    if method == "search":
        return _title_key(str(params.get("query") or ""), None)
    if method == "get_author_works":
        return f"author:{params.get('author_id')}"
    identifier = str(params.get("identifier") or "")
    return normalize_doi(identifier) if "/" in identifier else f"id:{identifier}"


def _bound_params(method: Callable[..., Any], args: tuple, kwargs: dict) -> dict[str, Any]:
    try:
        bound = inspect.signature(method).bind(*args, **kwargs)
    except (TypeError, ValueError):
        return {"args": list(args), **kwargs}
    bound.apply_defaults()
    return dict(bound.arguments)


def _provider_id(provider_name: str, method: str, value: Any, params: Mapping[str, Any]) -> str | None:
    if method in ("get_by_doi", "get_by_arxiv") and isinstance(value, WorkRecord):
        native = value.external_ids.get(provider_name)
        return str(native) if native else None
    if method == "get_author_works" and params.get("author_id") is not None:
        return str(params["author_id"])
    return None


# ---------------------------------------------------------------------------
# the evidence cache
# ---------------------------------------------------------------------------


@dataclass
class ProviderCall:
    """One provider method invocation during a run, as the cache saw it.
    ``cache`` is ``hit`` (served from ``source_evidence``), ``miss`` (asked,
    and recorded) or ``bypass`` (asked under ``--no-cache``, and recorded).
    ``value`` is the answer itself, kept in memory only."""

    provider: str
    method: str
    kind: str
    subject_key: str
    params: dict[str, Any]
    outcome: str
    cache: str
    evidence_id: str
    fetched_ts: str
    value: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider, "method": self.method, "kind": self.kind, "params": dict(self.params),
            "outcome": self.outcome, "cache": self.cache, "evidence_id": self.evidence_id,
            "fetched_ts": self.fetched_ts,
        }


class EvidenceCache:
    """``source_evidence`` as a read-through cache for one run. Every answer
    a provider gives is written (the live row for that call retired first,
    web_fetch's refresh order); an answer is served instead of asking when
    caching is enabled and its outcome is not in :data:`NEVER_FROM_CACHE`."""

    def __init__(self, store: Store, *, launch_id: str, enabled: bool = True):
        self.store = store
        self.launch_id = launch_id
        self.enabled = enabled
        self.log: list[ProviderCall] = []
        self._subject: str | None = None

    @contextmanager
    def subject(self, key: str) -> Iterator[None]:
        """Every call made inside is cached under ``key``."""
        previous = self._subject
        self._subject = key
        try:
            yield
        finally:
            self._subject = previous

    @property
    def hits(self) -> int:
        return sum(1 for c in self.log if c.cache == "hit")

    @property
    def misses(self) -> int:
        return sum(1 for c in self.log if c.cache != "hit")

    def _live(self, subject_key: str, provider: str, kind: str, params_json: str):
        return self.store.knowledge.execute(
            "SELECT * FROM source_evidence WHERE subject_key = ? AND provider = ? AND kind = ? "
            "AND params_json = ? AND superseded_by IS NULL",
            (subject_key, provider, kind, params_json),
        ).fetchone()

    def _write(
        self, *, subject_key: str, provider: str, kind: str, params_json: str, outcome: str,
        payload: Mapping[str, Any], provider_id: str | None,
    ) -> tuple[str, str]:
        evidence_id = new_id("SEVD")
        fetched_ts = now()
        live = self._live(subject_key, provider, kind, params_json)
        if live is not None:
            update(
                self.store, "source_evidence", pk_column="evidence_id", pk_value=live["evidence_id"],
                changes={"superseded_by": evidence_id},
            )
        insert(
            self.store,
            "source_evidence",
            {
                "evidence_id": evidence_id, "subject_key": subject_key, "provider": provider,
                "provider_id": provider_id, "kind": kind, "params_json": params_json, "outcome": outcome,
                "payload_json": _canonical_json(payload), "fetched_ts": fetched_ts,
                "created_by_launch": self.launch_id,
            },
        )
        return evidence_id, fetched_ts

    def invoke(self, proxy: "CachedProvider", method: str, inner: Callable[..., Any], args: tuple, kwargs: dict) -> Any:
        params = _bound_params(inner, args, kwargs)
        kind = _kind_for(method, params)
        subject_key = self._subject or _fallback_subject(method, params)
        params_json = _canonical_json({"method": method, **params})
        live = self._live(subject_key, proxy.name, kind, params_json)
        if self.enabled and live is not None and live["outcome"] not in NEVER_FROM_CACHE:
            proxy.last_request_stats = dict(_NOTHING_SENT)
            payload = json.loads(live["payload_json"]) if live["payload_json"] else {"value": None}
            call = ProviderCall(
                provider=proxy.name, method=method, kind=kind, subject_key=subject_key, params=params,
                outcome=live["outcome"], cache="hit", evidence_id=live["evidence_id"], fetched_ts=live["fetched_ts"],
            )
            self.log.append(call)
            value = _replay_answer(method, payload, proxy.name)  # raises a recorded refusal
            call.value = value
            return value

        value: Any = None
        exc: LitApiError | None = None
        try:
            value = inner(*args, **kwargs)
        except LitApiError as caught:
            exc = caught
        finally:
            proxy.last_request_stats = dict(getattr(proxy.inner, "last_request_stats", None) or {})
        outcome = outcome_word(exc, _answer_found(method, value))
        evidence_id, fetched_ts = self._write(
            subject_key=subject_key, provider=proxy.name, kind=kind, params_json=params_json, outcome=outcome,
            payload=_encode_answer(value, exc), provider_id=_provider_id(proxy.name, method, value, params),
        )
        self.log.append(
            ProviderCall(
                provider=proxy.name, method=method, kind=kind, subject_key=subject_key, params=params,
                outcome=outcome, cache="miss" if self.enabled else "bypass", evidence_id=evidence_id,
                fetched_ts=fetched_ts, value=value,
            )
        )
        if exc is not None:
            raise exc
        return value


class CachedProvider:
    """A provider seen through an :class:`EvidenceCache`. It carries exactly
    the methods the wrapped provider has (so the client still records a
    missing one as ``unsupported``), each with the wrapped method's own
    signature (so the client's keyword check still sees what it accepts), and
    the attributes the client reads (``name``, ``search_scope``,
    ``last_request_stats``, ``_api_key``)."""

    def __init__(self, inner: Any, cache: EvidenceCache):
        self.inner = inner
        self.cache = cache
        self.name = inner.name
        self.search_scope = getattr(inner, "search_scope", "unspecified")
        self._api_key = getattr(inner, "_api_key", None)
        self.last_request_stats: dict[str, Any] = {}
        for method in _CACHED_METHODS:
            inner_method = getattr(inner, method, None)
            if inner_method is not None:
                setattr(self, method, self._wrap(method, inner_method))

    def _wrap(self, method: str, inner_method: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(inner_method)
        def call(*args: Any, **kwargs: Any) -> Any:
            return self.cache.invoke(self, method, inner_method, args, kwargs)

        return call


# ---------------------------------------------------------------------------
# the held check
# ---------------------------------------------------------------------------


@dataclass
class HeldMatch:
    """A cited work found among what the programme already holds or has
    already asked for. ``match_on`` is the key that matched (``doi``,
    ``arxiv``, ``isbn``, ``title``) -- or ``open-request`` when the matched
    ``source`` row is still ``wanted``/``requested`` (a duplicate ask, not a
    held text), in which case ``matched_key`` still names the key. ``form`` is
    ``exact``, or ``probable`` for a title that only reached the similarity
    floor. ``origin`` is ``store`` or ``manifest`` (a delivered-manifest hit,
    which reports its ``path``)."""

    match_on: str
    matched_key: str
    form: str
    source_id: str | None = None
    request_state: str | None = None
    title: str | None = None
    year: int | None = None
    title_similarity: float | None = None
    path: str | None = None
    origin: str = "store"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id, "match_on": self.match_on, "matched_key": self.matched_key,
            "form": self.form, "request_state": self.request_state, "title": self.title, "year": self.year,
            "title_similarity": self.title_similarity, "path": self.path, "origin": self.origin,
        }


_KEY_ORDER = {"doi": 0, "arxiv": 1, "isbn": 2, "title": 3}


class HeldIndex:
    """The ``source`` rows a held check can match (those in a held-text or an
    open-request state), indexed by normalised DOI, arXiv id, ISBN-13 and
    title, plus the optional delivered manifest. Built once per run."""

    def __init__(self, rows: Sequence[Mapping[str, Any]], manifest: Sequence[Mapping[str, Any]] | None = None):
        self.rows: list[dict[str, Any]] = []
        self.by_doi: dict[str, list[dict[str, Any]]] = {}
        self.by_arxiv: dict[str, list[dict[str, Any]]] = {}
        self.by_isbn: dict[str, list[dict[str, Any]]] = {}
        self.by_title: dict[str, list[dict[str, Any]]] = {}
        for order, raw in enumerate(rows):
            state = raw.get("request_state")
            if state not in HELD_TEXT_STATES and state not in OPEN_REQUEST_STATES:
                continue
            row = dict(raw)
            row["_order"] = order
            row["_title_norm"] = normalize_title(row.get("title"))
            self.rows.append(row)
            doi = normalize_doi(row.get("doi"))
            if doi:
                self.by_doi.setdefault(doi, []).append(row)
            arxiv_id = normalize_arxiv_id(row.get("arxiv_id"))
            if arxiv_id:
                self.by_arxiv.setdefault(arxiv_id, []).append(row)
            isbn = isbn_to_13(str(row["isbn"])) if row.get("isbn") else None
            if isbn:
                self.by_isbn.setdefault(isbn, []).append(row)
            if row["_title_norm"]:
                self.by_title.setdefault(row["_title_norm"], []).append(row)
        self.manifest: list[dict[str, Any]] = [dict(m) for m in manifest or []]

    @classmethod
    def from_store(cls, store: Store, manifest: Sequence[Mapping[str, Any]] | None = None) -> "HeldIndex":
        rows = store.knowledge.execute(
            "SELECT source_id, title, year, doi, arxiv_id, isbn, request_state FROM source "
            "ORDER BY registered_ts, source_id"
        ).fetchall()
        return cls([dict(r) for r in rows], manifest)


def _years_ok(candidate: Any, years: Sequence[int], tolerance: int) -> bool:
    return isinstance(candidate, int) and any(abs(candidate - y) <= tolerance for y in years)


def held_match(
    store: Store | None,
    cited: CitedWork,
    record: WorkRecord | None,
    *,
    title_floor: float = 0.90,
    year_tolerance: int = 1,
    manifest: Sequence[Mapping[str, Any]] | None = None,
    index: HeldIndex | None = None,
) -> HeldMatch | None:
    """Is the cited work already held, or already asked for?

    Matched on the normalised DOI, the arXiv id, the ISBN-13, then the
    normalised title with the year within ``year_tolerance`` (``exact`` on
    equal titles, ``probable`` when the title only reaches ``title_floor``).
    Identifiers and titles are taken from ``cited`` and, when given, from the
    resolved ``record``. The caller passes only what it trusts: a record that
    is not the cited work (a ``mismatch``) is not passed, and neither is a
    cited identifier that resolved to another work -- a mistyped DOI that
    happens to equal a held source's DOI names that source, not the cited
    work.

    A ``source`` row whose text is held (``delivered``/``verifying``/
    ``archived``/``indexed``) wins; then a delivered-manifest hit (same keys;
    the manifest has no year, so its title match is not year-checked); then a
    row still ``wanted``/``requested``, reported with ``match_on =
    "open-request"``. ``rejected``/``failed`` rows never match."""
    if index is None:
        if store is None:
            raise ValueError("held_match needs a store or an index")
        index = HeldIndex.from_store(store, manifest)
    dois = {d for d in (normalize_doi(cited.doi), normalize_doi(record.doi) if record else None) if d}
    arxivs = {a for a in (normalize_arxiv_id(cited.arxiv_id), normalize_arxiv_id(record.arxiv_id) if record else None) if a}
    isbns = {i for i in (cited.isbn,) if i}
    titles = [t for t in (cited.title_hint, record.title if record else None) if normalize_title(t)]
    years = [y for y in (cited.year, cited.first_pub_year, record.year if record else None) if isinstance(y, int)]

    found: list[tuple[tuple, HeldMatch]] = []

    def add(row: Mapping[str, Any], key: str, form: str, similarity: float | None) -> None:
        state = row.get("request_state")
        open_request = state in OPEN_REQUEST_STATES
        match = HeldMatch(
            match_on="open-request" if open_request else key, matched_key=key, form=form,
            source_id=row.get("source_id"), request_state=state, title=row.get("title"), year=row.get("year"),
            title_similarity=None if similarity is None else round(similarity, 4),
        )
        rank = (1 if open_request else 0, _KEY_ORDER[key], 0 if form == "exact" else 1, -(similarity or 1.0), row["_order"])
        found.append((rank, match))

    for doi in dois:
        for row in index.by_doi.get(doi, []):
            add(row, "doi", "exact", None)
    for arxiv_id in arxivs:
        for row in index.by_arxiv.get(arxiv_id, []):
            add(row, "arxiv", "exact", None)
    for isbn in isbns:
        for row in index.by_isbn.get(isbn, []):
            add(row, "isbn", "exact", None)
    if years and titles:
        exact_title_rows: set[str] = set()
        for title in titles:
            for row in index.by_title.get(normalize_title(title) or "", []):
                if _years_ok(row.get("year"), years, year_tolerance):
                    add(row, "title", "exact", 1.0)
                    exact_title_rows.add(row["source_id"])
        for row in index.rows:
            if row["source_id"] in exact_title_rows or not _years_ok(row.get("year"), years, year_tolerance):
                continue
            best = max((title_similarity(t, row.get("title")) for t in titles), default=0.0)
            if best >= title_floor:
                add(row, "title", "probable", best)

    held_text = sorted((f for f in found if f[0][0] == 0), key=lambda f: f[0])
    if held_text:
        return held_text[0][1]
    manifest_hit = _manifest_match(index.manifest, dois, isbns, titles, title_floor)
    if manifest_hit is not None:
        return manifest_hit
    open_asks = sorted((f for f in found if f[0][0] == 1), key=lambda f: f[0])
    return open_asks[0][1] if open_asks else None


def _manifest_match(
    manifest: Sequence[Mapping[str, Any]], dois: set[str], isbns: set[str], titles: Sequence[str], title_floor: float,
) -> HeldMatch | None:
    best: tuple[tuple, HeldMatch] | None = None
    for order, entry in enumerate(manifest):
        candidates: list[tuple[tuple, HeldMatch]] = []
        if entry.get("doi") and entry["doi"] in dois:
            candidates.append(((0, 0, order), HeldMatch("doi", "doi", "exact")))
        if entry.get("isbn") and entry["isbn"] in isbns:
            candidates.append(((2, 0, order), HeldMatch("isbn", "isbn", "exact")))
        if entry.get("title_norm") and titles:
            if any(normalize_title(t) == entry["title_norm"] for t in titles):
                candidates.append(((3, 0, order), HeldMatch("title", "title", "exact", title_similarity=1.0)))
            else:
                similarity = max(title_similarity(t, entry.get("title")) for t in titles)
                if similarity >= title_floor:
                    candidates.append(
                        ((3, 1, order), HeldMatch("title", "title", "probable", title_similarity=round(similarity, 4)))
                    )
        for rank, match in candidates:
            match.origin = "manifest"
            match.path = entry.get("path")
            match.title = entry.get("title") or None
            if best is None or rank < best[0]:
                best = (rank, match)
    return best[1] if best else None


def load_delivered_manifest(path: Path | str) -> list[dict[str, Any]]:
    """A delivered-files manifest: a TSV whose header names ``path``,
    ``title``, ``doi`` and ``isbn`` (any order, other columns ignored). Each
    row needs a ``path``; the other three may be empty. An ISBN that fails its
    checksum is ignored for that row. Refused whole (``manifest_invalid``) when
    the header or a row is unusable."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InvestigateError("manifest_invalid", f"cannot read {path}: {exc}") from exc
    reader = csv.reader(text.splitlines(), delimiter="\t")
    try:
        header = [h.strip().lower() for h in next(reader)]
    except StopIteration:
        raise InvestigateError("manifest_invalid", f"{path}: empty; a header row is required") from None
    missing = [c for c in MANIFEST_COLUMNS if c not in header]
    if missing:
        raise InvestigateError(
            "manifest_invalid", f"{path}: header lacks {', '.join(missing)} (needs {', '.join(MANIFEST_COLUMNS)})",
        )
    position = {c: header.index(c) for c in MANIFEST_COLUMNS}
    entries: list[dict[str, Any]] = []
    problems: list[str] = []
    for number, cells in enumerate(reader, start=2):
        if not any(c.strip() for c in cells):
            continue
        values = {name: (cells[i].strip() if i < len(cells) else "") for name, i in position.items()}
        if not values["path"]:
            problems.append(f"line {number}: no path")
            continue
        entries.append(
            {
                "path": values["path"], "title": values["title"] or None, "title_norm": normalize_title(values["title"]),
                "doi": normalize_doi(values["doi"]), "isbn": isbn_to_13(values["isbn"]) if values["isbn"] else None,
            }
        )
    if problems:
        raise InvestigateError("manifest_invalid", f"{path}: {len(problems)} problem(s)", details={"problems": problems})
    return entries


# ---------------------------------------------------------------------------
# one seed
# ---------------------------------------------------------------------------


@dataclass
class _RunContext:
    store: Store
    client: LitApiClient
    cache: EvidenceCache
    index: HeldIndex
    config: InvestigateConfig
    thresholds: dict[str, Any]
    max_calls: int
    year: int
    launch_id: str


def _retryable_outcome(outcome: Mapping[str, Any]) -> bool:
    word = outcome.get("outcome")
    if word in ("rate_limited", "transport_unreachable"):
        return True
    status = outcome.get("status_code")
    return word == "http_error" and isinstance(status, int) and status >= 500


def _iter_outcomes(provider_outcomes: Mapping[str, Any]) -> Iterator[tuple[str, Mapping[str, Any]]]:
    """Both shapes the client reports: ``{provider: outcome}`` and, for
    author works, ``{provider: [outcome, ...]}``."""
    for provider, value in (provider_outcomes or {}).items():
        for outcome in value if isinstance(value, list) else [value]:
            if isinstance(outcome, Mapping):
                yield provider, outcome


def _compact_record(record: WorkRecord) -> dict[str, Any]:
    work_type = None
    for provider in record.providers:
        work_type = provider_extra(record, provider, "work_type")
        if work_type:
            break
    return {
        "title": record.title, "doi": record.doi, "arxiv_id": record.arxiv_id, "year": record.year,
        "authors": list(record.authors), "venue": record.venue, "citation_count": record.citation_count,
        "work_type": work_type, "external_ids": dict(record.external_ids), "providers": list(record.providers),
    }


def _truncate_words(text: str | None, max_words: int) -> tuple[str | None, bool]:
    if not text:
        return None, False
    words = text.split()
    if len(words) <= max_words:
        return " ".join(words), False
    return " ".join(words[:max_words]), True


def _without_referenced_works(other: Mapping[str, Any]) -> dict[str, Any]:
    """``other`` minus OpenAlex's ``referenced_works`` id list (the dossier's
    ``references`` carries the works themselves), in either shape."""
    out: dict[str, Any] = {}
    for key, value in (other or {}).items():
        if key == "referenced_works":
            continue
        if isinstance(value, dict) and "referenced_works" in value:
            value = {k: v for k, v in value.items() if k != "referenced_works"}
        out[key] = value
    return out


def _dossier_record(record: WorkRecord, max_words: int) -> dict[str, Any]:
    d = record.to_dict()
    d["abstract"], _ = _truncate_words(record.abstract, max_words)
    d["other"] = _without_referenced_works(d.get("other") or {})
    return d


class _SeedRun:
    """The per-seed call budget and the record of every call made."""

    def __init__(self, ctx: _RunContext):
        self.ctx = ctx
        self.calls_used = 0

    def call(self, name: str, params: dict[str, Any], fn: Callable[[], Any]) -> tuple[dict[str, Any], Any] | None:
        """Make one client-level call, unless the seed's cap is spent (then
        ``None``). The entry carries the client's per-provider outcomes and
        every provider invocation behind them (cache hit or not)."""
        if self.calls_used >= self.ctx.max_calls:
            return None
        self.calls_used += 1
        start = len(self.ctx.cache.log)
        entry: dict[str, Any] = {"call": name, "params": dict(params)}
        result: Any = None
        try:
            result = fn()
        except AllProvidersFailedError as exc:
            entry["error"] = str(exc)
            entry["provider_outcomes"] = dict(exc.details.get("provider_outcomes") or {})
        else:
            outcomes = getattr(result, "provider_outcomes", None)
            entry["provider_outcomes"] = dict(outcomes or {})
        entry["provider_calls"] = [c.to_dict() for c in self.ctx.cache.log[start:]]
        entry["_log"] = self.ctx.cache.log[start:]
        return entry, result


def _strip_private(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in e.items() if not k.startswith("_")} for e in entries]


def _abstract_provider(record: WorkRecord, log: Sequence[ProviderCall]) -> str | None:
    """Which provider's answer the (longest-wins) merged abstract came from."""
    if not record.abstract:
        return None
    key = record.identity_key()
    for call in log:
        values = call.value if isinstance(call.value, list) else [call.value]
        for value in values:
            if isinstance(value, WorkRecord) and value.abstract == record.abstract and value.identity_key() == key:
                return call.provider
    return None


def _first_pub_year(cited: CitedWork, record: WorkRecord | None) -> tuple[int | None, str | None]:
    """The earliest publication year the evidence gives: a reprint pair's
    first year, else the year the citation gives, else the resolved record's."""
    if isinstance(cited.first_pub_year, int):
        return cited.first_pub_year, "reprint_pair"
    if isinstance(cited.year, int):
        return cited.year, "cited_year"
    if record is not None and isinstance(record.year, int):
        return record.year, "record_year"
    return None, None


def _investigate_seed(seed: Seed, ctx: _RunContext) -> dict[str, Any]:
    cfg = ctx.config
    cited = parse_citation(seed.seed_raw)
    run = _SeedRun(ctx)
    seed_key = cited_subject_key(cited)
    tried: list[dict[str, Any]] = []
    resolve_skipped: list[dict[str, Any]] = []
    best: tuple[WorkRecord, MatchResult, str, list[ProviderCall]] | None = None
    mismatches: list[tuple[WorkRecord, MatchResult, str]] = []
    search_match: MatchResult | None = None
    retryable = False

    with ctx.cache.subject(seed_key):
        for name, identifier, via, lookup in (
            ("lookup_doi", cited.doi, "doi", ctx.client.lookup_doi),
            ("lookup_arxiv", cited.arxiv_id, "arxiv", ctx.client.lookup_arxiv),
        ):
            if not identifier or (best is not None and best[1].resolution in ("exact", "probable")):
                continue
            params = {"doi": identifier} if via == "doi" else {"arxiv_id": identifier}
            made = run.call(name, params, functools.partial(lookup, identifier))
            if made is None:
                resolve_skipped.append({"call": name, "params": params, "reason": "max_calls_per_seed"})
                continue
            entry, result = made
            tried.append(entry)
            retryable = retryable or any(_retryable_outcome(o) for _, o in _iter_outcomes(entry["provider_outcomes"]))
            if result is None:
                continue
            match = match_citation(
                cited, result.record, title_floor=cfg.title_floor, year_tolerance=cfg.year_tolerance, via=via,
            )
            entry["record"] = _compact_record(result.record)
            entry["match"] = match.to_dict()
            if match.resolution == "mismatch":
                mismatches.append((result.record, match, via))
            if best is None or _RANK[match.resolution] < _RANK[best[1].resolution]:
                best = (result.record, match, via, entry["_log"])

        if not any("match" in entry for entry in tried):
            query = cited.title_hint or text_without_identifiers(cited.raw)
            if query:
                params = {"query": query, "limit": SEARCH_LIMIT}
                made = run.call("search", params, functools.partial(ctx.client.search, query, limit=SEARCH_LIMIT))
                if made is None:
                    resolve_skipped.append({"call": "search", "params": params, "reason": "max_calls_per_seed"})
                else:
                    entry, result = made
                    tried.append(entry)
                    retryable = retryable or any(
                        _retryable_outcome(o) for _, o in _iter_outcomes(entry["provider_outcomes"])
                    )
                    if result is not None:
                        record, match = best_title_match(
                            cited, result.records, title_floor=cfg.title_floor, year_tolerance=cfg.year_tolerance,
                        )
                        entry["candidates"] = [_compact_record(r) for r in result.records]
                        entry["match"] = match.to_dict()
                        search_match = match
                        if record is not None:
                            best = (record, match, "title", entry["_log"])

    if best is not None and best[1].resolution in ("exact", "probable"):
        record, match, via, resolve_log = best
        status = match.resolution
    elif mismatches:
        record, match, via = mismatches[0]
        resolve_log = []
        status = "mismatch"
    else:
        record, match, via, resolve_log = None, search_match, None, []
        status = "retry" if retryable else "none"
    resolved = status in ("exact", "probable")

    # the held check trusts only what did not resolve to another work
    distrusted = {v for _, _, v in mismatches}
    held_cited = replace(
        cited,
        doi=None if "doi" in distrusted else cited.doi,
        arxiv_id=None if "arxiv" in distrusted else cited.arxiv_id,
    )
    held = held_match(
        None, held_cited, record if resolved else None, title_floor=cfg.title_floor,
        year_tolerance=cfg.year_tolerance, index=ctx.index,
    )

    if held is not None:
        state = "held"
    elif status == "mismatch":
        state = "wrong_identifier"
    elif status == "retry":
        state = "retry"
    elif status == "none":
        state = "need_info"
    else:
        state = "open"

    evidence: dict[str, Any] = {
        "abstract": None, "abstract_provider": None, "abstract_truncated": False, "work_type": None,
        "cited_by_count": None, "counts_by_year": [], "citing": [], "citing_reviews": [], "references": [],
        "author_works": [], "oa_pdf_url": None, "arxiv_neighbours": None, "calls": [], "skipped": [],
    }
    if resolved:
        assert record is not None
        evidence["abstract"], evidence["abstract_truncated"] = _truncate_words(record.abstract, cfg.abstract_max_words)
        evidence["abstract_provider"] = _abstract_provider(record, resolve_log)
        evidence["work_type"] = provider_extra(record, "openalex", "work_type") or provider_extra(
            record, "semanticscholar", "work_type"
        )
        evidence["cited_by_count"] = record.citation_count
        evidence["counts_by_year"] = list(provider_extra(record, "openalex", "counts_by_year") or [])
        evidence["oa_pdf_url"] = record.oa_pdf_url
    evidence_incomplete = False
    if resolved and held is None:
        assert record is not None
        evidence_incomplete = _gather(run, record, evidence)

    first_pub_year, first_pub_year_from = _first_pub_year(cited, record if resolved else None)
    subject_key = record_subject_key(record) if resolved and record is not None else seed_key
    return {
        "dossier_version": DOSSIER_VERSION,
        "list_id": seed.list_id,
        "row_id": seed.row_id,
        "seed_raw": seed.seed_raw,
        "question": seed.question,
        "literature": seed.literature,
        "input_position": {"line": seed.line, "seed": seed.position},
        "subject_key": subject_key,
        "parsed": cited.to_dict(),
        "resolution": {
            "status": status,
            "via": via,
            "record": _dossier_record(record, cfg.abstract_max_words) if record is not None else None,
            "match": match.to_dict() if match is not None else None,
            "tried": _strip_private(tried),
            "skipped": resolve_skipped,
        },
        "held": held.to_dict() if held is not None else None,
        "evidence": evidence,
        "flags": {
            "foundational": first_pub_year is not None and first_pub_year < cfg.foundational_before,
            "first_pub_year": first_pub_year,
            "first_pub_year_from": first_pub_year_from,
            "no_abstract": (evidence["abstract"] is None) if resolved else None,
            "wrong_identifier": bool(mismatches),
            "call_cap_reached": any(
                s.get("reason") == "max_calls_per_seed" for s in resolve_skipped + evidence["skipped"]
            ),
            "evidence_incomplete": evidence_incomplete,
        },
        "mechanical_state": state,
        "thresholds": dict(ctx.thresholds),
        "calls_used": run.calls_used,
        "stage_version": STAGE_VERSION,
        "created_by_launch": ctx.launch_id,
        "ts": now(),
    }


def _gather(run: _SeedRun, record: WorkRecord, evidence: dict[str, Any]) -> bool:
    """The four gather calls for a resolved, not-held work. Returns whether
    any of them went unanswered for a reason worth asking again."""
    ctx = run.ctx
    cfg = ctx.config
    identifier = _gather_identifier(record)
    incomplete = False
    since_year = ctx.year - cfg.author_works_years
    plan: list[tuple[str, str, dict[str, Any], Callable[[], Any]]] = []
    if identifier is not None:
        plan += [
            (
                "citing", "get_citations",
                {"identifier": identifier, "sort": "cited_by_count:desc", "limit": cfg.citing_limit},
                functools.partial(ctx.client.get_citations, identifier, sort="cited_by_count:desc", limit=cfg.citing_limit),
            ),
            (
                "citing_reviews", "get_citations",
                {"identifier": identifier, "work_type": "review", "limit": cfg.review_limit},
                functools.partial(ctx.client.get_citations, identifier, work_type="review", limit=cfg.review_limit),
            ),
            (
                "references", "get_references", {"identifier": identifier, "limit": cfg.references_limit},
                functools.partial(ctx.client.get_references, identifier, limit=cfg.references_limit),
            ),
        ]
    else:
        for slot, name in (("citing", "get_citations"), ("citing_reviews", "get_citations"), ("references", "get_references")):
            evidence["skipped"].append({"call": name, "params": {}, "reason": "no identifier to ask about", "slot": slot})
    plan.append(
        (
            "author_works", "get_author_works",
            {"authors": AUTHOR_POSITIONS, "since_year": since_year, "limit": cfg.author_works_limit},
            functools.partial(
                ctx.client.get_author_works, record, authors=AUTHOR_POSITIONS, since_year=since_year,
                limit=cfg.author_works_limit,
            ),
        )
    )
    with ctx.cache.subject(record_subject_key(record)):
        for slot, name, params, fn in plan:
            made = run.call(name, params, fn)
            if made is None:
                evidence["skipped"].append({"call": name, "params": params, "reason": "max_calls_per_seed", "slot": slot})
                continue
            entry, result = made
            entry["slot"] = slot
            evidence["calls"].append({k: v for k, v in entry.items() if not k.startswith("_")})
            if slot == "author_works":
                incomplete = _fill_author_works(result, evidence) or incomplete
                continue
            if result is None:
                incomplete = incomplete or any(
                    _retryable_outcome(o) for _, o in _iter_outcomes(entry["provider_outcomes"])
                )
                continue
            limit = {"citing": cfg.citing_limit, "citing_reviews": cfg.review_limit, "references": cfg.references_limit}[slot]
            evidence[slot] = [edge.to_dict() for edge in result.items[:limit]]
    return incomplete


def _fill_author_works(result: AuthorWorksResult | None, evidence: dict[str, Any]) -> bool:
    incomplete = False
    for position in (result.authors if result is not None else []):
        if position.provider is None and any(_retryable_outcome(o) for o in position.provider_outcomes.values()):
            incomplete = True
        evidence["author_works"].append(
            {
                "position": position.position, "provider": position.provider, "author_id": position.author_id,
                "works": [_compact_record(r) for r in position.records],
            }
        )
    return incomplete


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


def _dossier_row(store: Store, list_id: str, row_id: str, seed_raw: str):
    return store.knowledge.execute(
        "SELECT * FROM source_dossier WHERE list_id = ? AND row_id = ? AND seed_raw = ?", (list_id, row_id, seed_raw),
    ).fetchone()


def _write_dossier(path: Path, dossier: Mapping[str, Any]) -> str:
    data = _dossier_bytes(dossier)
    atomic_write_bytes(path, data)
    return hashlib.sha256(data).hexdigest()


def _upsert_dossier_row(
    store: Store, dossier: Mapping[str, Any], path: Path, sha256: str, existing, launch_id: str,
) -> str:
    held = dossier.get("held") or {}
    fields = {
        "subject_key": dossier.get("subject_key"),
        "resolution": dossier["resolution"]["status"],
        "held_source_id": held.get("source_id") if held.get("origin") == "store" else None,
        "mechanical_state": dossier["mechanical_state"],
        "dossier_path": _store_path(path, store.program_root),
        "dossier_sha256": sha256,
        "stage_version": STAGE_VERSION,
        "created_by_launch": launch_id,
        "created_ts": dossier["ts"],
    }
    if existing is not None:
        update(store, "source_dossier", pk_column="dossier_id", pk_value=existing["dossier_id"], changes=fields)
        return existing["dossier_id"]
    dossier_id = new_id("DOSS")
    insert(
        store, "source_dossier",
        {"dossier_id": dossier_id, "list_id": dossier["list_id"], "row_id": dossier["row_id"],
         "seed_raw": dossier["seed_raw"], **fields},
    )
    return dossier_id


def arxiv_neighbour_search(
    conn: Any, encoder: Any, *, k: int = ARXIV_NEIGHBOURS_K, config: Mapping[str, Any] | None = None,
) -> Callable[[list[str]], list[list[dict[str, Any]]]]:
    """The ``--arxiv-neighbours`` search over an open local arXiv index: all
    the questions encoded, then ONE
    :func:`~trialerror.arxiv_index.query.semantic_search_many` pass, ``k``
    neighbours each, in question order."""
    from trialerror.arxiv_index.query import semantic_search_many

    def search(questions: list[str]) -> list[list[dict[str, Any]]]:
        batch = getattr(encoder, "encode_queries", None)
        vectors = [list(v) for v in batch(questions)] if callable(batch) else [encoder.encode_query(q) for q in questions]
        result = semantic_search_many(conn, vectors, k=k, config=config)
        return [[r.to_dict() for r in hits] for hits in result["results"]]

    return search


def run_investigation(
    store: Store,
    providers: Sequence[Any],
    seeds: Sequence[Seed],
    *,
    config: InvestigateConfig,
    out_dir: Path | str,
    launch_id: str,
    resume: bool = False,
    use_cache: bool = True,
    max_calls_per_seed: int | None = None,
    manifest: Sequence[Mapping[str, Any]] | None = None,
    neighbours: Callable[[list[str]], list[list[dict[str, Any]]]] | None = None,
    current_year: int | None = None,
) -> dict[str, Any]:
    """Investigate ``seeds`` through ``providers`` (each wrapped in a
    :class:`CachedProvider`), writing ``<out_dir>/<list_id>/<row_id>/<seed
    slug>.json`` and upserting ``source_dossier`` per seed.

    Per seed, in order, stopping at the call cap: parse; resolve (the DOI,
    then the arXiv id, then -- only when no identifier returned a record -- a
    search on the quoted title or the citation's prose, ``limit=5``, judged by
    :func:`~trialerror.litapi.match.best_title_match`); match; the held check;
    and, when resolved (``exact``/``probable``) and not held, the four gather
    calls. ``mechanical_state``, by precedence: ``held`` (a held check hit),
    ``wrong_identifier`` (resolution ``mismatch``), ``retry`` (no record, and
    some resolve call was rate-limited, unreachable or answered 5xx),
    ``need_info`` (no record), else ``open``.

    A seed whose dossier row carries a verdict is never re-investigated (its
    judged dossier is left as it is); with ``resume`` a seed whose dossier
    exists in a state other than ``retry`` is skipped too.

    Raises :class:`InvestigateError` (config or cap invalid) and the store's
    ``XidTargetMissingError`` (unknown launch) before any call is made."""
    problems = config.problems()
    if problems:
        raise InvestigateError("config_invalid", "; ".join(problems), details={"problems": problems})
    max_calls = config.max_calls_per_seed if max_calls_per_seed is None else int(max_calls_per_seed)
    if max_calls < 1:
        raise InvestigateError("max_calls_invalid", f"--max-calls-per-seed must be >= 1, got {max_calls}")
    unsafe = sorted({v for s in seeds for v in (s.list_id, s.row_id) if not _SAFE_ID_RE.match(str(v))})
    if unsafe:
        # they become directory names under out_dir
        raise InvestigateError("seeds_invalid", f"list/row ids must be plain ids, got {unsafe!r}", details={"ids": unsafe})
    require_xid_targets(store, "source_dossier", {"created_by_launch": launch_id})

    out_path = Path(out_dir).resolve()
    thresholds = {**config.to_dict(), "max_calls_per_seed": max_calls}
    cache = EvidenceCache(store, launch_id=launch_id, enabled=use_cache)
    ctx = _RunContext(
        store=store, client=LitApiClient([CachedProvider(p, cache) for p in providers]), cache=cache,
        index=HeldIndex.from_store(store, manifest), config=config, thresholds=thresholds, max_calls=max_calls,
        year=current_year if current_year is not None else _current_year(), launch_id=launch_id,
    )

    states: Counter[str] = Counter({s: 0 for s in MECHANICAL_STATES})
    skipped = {"resume": 0, "verdict_recorded": 0}
    tally: dict[str, Counter[str]] = {}
    calls_used = 0
    evidence_incomplete = 0
    investigated = 0
    written: list[tuple[Path, dict[str, Any]]] = []  # kept only for the neighbours pass
    summaries: list[dict[str, Any]] = []
    warnings: list[str] = []

    for seed in seeds:
        existing = _dossier_row(store, seed.list_id, seed.row_id, seed.seed_raw)
        if existing is not None and existing["verdict"] is not None:
            skipped["verdict_recorded"] += 1
            continue
        if (
            resume and existing is not None and existing["mechanical_state"] != "retry"
            and _load_path(existing["dossier_path"], store.program_root).is_file()
        ):
            skipped["resume"] += 1
            continue
        dossier = _investigate_seed(seed, ctx)
        path = out_path / seed.list_id / seed.row_id / f"{seed_slug(seed.seed_raw)}.json"
        sha256 = _write_dossier(path, dossier)
        _upsert_dossier_row(store, dossier, path, sha256, existing, launch_id)
        investigated += 1
        if neighbours is not None:
            written.append((path, dossier))
        states[dossier["mechanical_state"]] += 1
        calls_used += dossier["calls_used"]
        if dossier["flags"]["evidence_incomplete"]:
            evidence_incomplete += 1
        for entry in dossier["resolution"]["tried"] + dossier["evidence"]["calls"]:
            for provider, outcome in _iter_outcomes(entry.get("provider_outcomes") or {}):
                tally.setdefault(provider, Counter())[str(outcome.get("outcome"))] += 1
        summaries.append(
            {
                "list_id": seed.list_id, "row_id": seed.row_id, "seed_raw": seed.seed_raw,
                "mechanical_state": dossier["mechanical_state"], "resolution": dossier["resolution"]["status"],
                "dossier_path": str(path),
            }
        )

    neighbours_report: dict[str, Any] = {"requested": neighbours is not None, "ran": False}
    if neighbours is not None and written:
        neighbours_report.update(_neighbours_pass(store, written, neighbours, warnings))

    return {
        "seeds": len(seeds),
        "investigated": investigated,
        "skipped": skipped,
        "states": dict(states),
        "calls_used": calls_used,
        "cache": {"enabled": use_cache, "hits": cache.hits, "misses": cache.misses},
        "providers": {p: dict(c) for p, c in sorted(tally.items())},
        "thresholds": thresholds,
        "evidence_incomplete": evidence_incomplete,
        "arxiv_neighbours": neighbours_report,
        "out_dir": str(out_path),
        "dossiers": summaries,
        "warnings": warnings,
    }


def _neighbours_pass(
    store: Store,
    written: list[tuple[Path, dict[str, Any]]],
    neighbours: Callable[[list[str]], list[list[dict[str, Any]]]],
    warnings: list[str],
) -> dict[str, Any]:
    """One search over the distinct questions of the dossiers this run wrote
    (at most ``MAX_BATCH_QUERIES``), attached to each of those dossiers by its
    question; each touched file is rewritten and its row's sha updated."""
    from trialerror.arxiv_index.query import MAX_BATCH_QUERIES

    questions = list(dict.fromkeys(d.get("question") for _, d in written if d.get("question")))
    over = questions[MAX_BATCH_QUERIES:]
    questions = questions[:MAX_BATCH_QUERIES]
    if over:
        warnings.append(
            f"arxiv neighbours: {len(over)} question(s) past the first {MAX_BATCH_QUERIES} were not searched"
        )
    if not questions:
        return {"ran": False, "questions": 0}
    try:
        results = neighbours(questions)
    except Exception as exc:  # noqa: BLE001 - the provider work is done; a failed extra pass must not lose it
        warnings.append(f"arxiv neighbours: the search failed ({type(exc).__name__}: {exc}); none attached")
        return {"ran": False, "questions": len(questions), "error": str(exc)}
    by_question = dict(zip(questions, results))
    for path, dossier in written:
        hits = by_question.get(dossier.get("question"))
        if hits is None:
            continue
        dossier["evidence"]["arxiv_neighbours"] = hits
        sha256 = _write_dossier(path, dossier)
        row = _dossier_row(store, dossier["list_id"], dossier["row_id"], dossier["seed_raw"])
        update(store, "source_dossier", pk_column="dossier_id", pk_value=row["dossier_id"],
               changes={"dossier_sha256": sha256})
    return {"ran": True, "questions": len(questions), "questions_not_searched": len(over)}


# ---------------------------------------------------------------------------
# verdicts
# ---------------------------------------------------------------------------


_EVIDENCE_LISTS = ("citing", "citing_reviews", "references", "author_works", "arxiv_neighbours")


def _evidence_identifiers(evidence: Mapping[str, Any]) -> dict[str, set[str]]:
    """Every DOI, arXiv id and ISBN named in the dossier's evidence lists."""
    found: dict[str, set[str]] = {"doi": set(), "arxiv": set(), "isbn": set()}

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if key == "doi" and isinstance(value, str):
                    doi = normalize_doi(value)
                    if doi:
                        found["doi"].add(doi)
                        if doi.startswith("10.48550/arxiv."):
                            found["arxiv"].add(doi[len("10.48550/arxiv."):])
                elif key == "arxiv_id" and isinstance(value, str):
                    arxiv_id = normalize_arxiv_id(value)
                    if arxiv_id:
                        found["arxiv"].add(arxiv_id)
                elif key == "isbn" and isinstance(value, str):
                    isbn = isbn_to_13(value)
                    if isbn:
                        found["isbn"].add(isbn)
                else:
                    visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    for key in _EVIDENCE_LISTS:
        visit((evidence or {}).get(key))
    return found


def _normalise_substitute(kind: str, value: str) -> str | None:
    if kind == "doi":
        return normalize_doi(value)
    if kind == "arxiv":
        return normalize_arxiv_id(value)
    if kind == "isbn":
        return isbn_to_13(value)
    raise ValueError(f"substitute kind must be doi, arxiv or isbn, got {kind!r}")


def _read_dossier(path: Path) -> tuple[bytes, dict[str, Any]]:
    if not path.is_file():
        raise InvestigateError("dossier_not_found", f"no such dossier file: {path}")
    data = path.read_bytes()
    try:
        dossier = json.loads(data)
    except ValueError as exc:
        raise InvestigateError("dossier_invalid", f"{path}: not JSON ({exc})") from exc
    if not isinstance(dossier, dict) or dossier.get("dossier_version") != DOSSIER_VERSION:
        raise InvestigateError("dossier_invalid", f"{path}: not a version-{DOSSIER_VERSION} dossier")
    for key in ("list_id", "row_id", "seed_raw"):
        if not isinstance(dossier.get(key), str):
            raise InvestigateError("dossier_invalid", f"{path}: no {key}")
    return data, dossier


def record_verdict(
    store: Store,
    dossier_path: Path | str,
    *,
    verdict: str,
    launch_id: str,
    substitute: tuple[str, str] | None = None,
    reason_code: str | None = None,
    detail: Mapping[str, Any] | None = None,
    supersede: bool = False,
) -> dict[str, Any]:
    """Record ``verdict`` on one dossier: the ``source_dossier`` row's four
    verdict columns and the dossier file's ``verdict`` block, together.

    Refused, with nothing written (:class:`InvestigateError`):
    ``REQUEST``/``REQUEST-AS-FOUNDATIONAL`` when the resolution is not
    ``exact``/``probable``; ``REQUEST`` when the dossier is flagged
    foundational; ``REQUEST-AS-FOUNDATIONAL`` without ``founds``,
    ``consolidator`` and ``foundational_reason`` in
    :data:`FOUNDATIONAL_REASONS` in the detail; ``SUBSTITUTE-WITH`` without
    exactly one substitute, or with one that appears nowhere in the dossier's
    evidence lists; a substitute on any other verdict; ``HELD`` without a
    ``held`` block; a second verdict unless ``supersede`` (the earlier then
    moves into the detail's ``history``). Also refused: a dossier file that is
    not the one registered for its row, or that changed since it was written.
    The launch is checked before the file is touched, and the file is put back
    if the row cannot be written."""
    if verdict not in VERDICTS:
        raise InvestigateError("verdict_unknown", f"verdict must be one of {', '.join(VERDICTS)}, got {verdict!r}")
    path = Path(dossier_path)
    original, dossier = _read_dossier(path)
    row = _dossier_row(store, dossier["list_id"], dossier["row_id"], dossier["seed_raw"])
    if row is None:
        raise InvestigateError(
            "dossier_not_registered",
            f"{path}: no source_dossier row for list {dossier['list_id']!r}, row {dossier['row_id']!r} and this seed",
        )
    registered = _load_path(row["dossier_path"], store.program_root)
    if registered.resolve() != path.resolve():
        raise InvestigateError(
            "dossier_path_mismatch", f"{path} is not the dossier registered for this seed ({registered})",
            details={"registered_path": str(registered)},
        )
    if hashlib.sha256(original).hexdigest() != row["dossier_sha256"]:
        raise InvestigateError(
            "dossier_changed",
            f"{path} changed since it was written (sha256 differs from the registered one); re-run the "
            "investigation for this seed rather than editing its dossier",
        )

    detail_out: dict[str, Any] = dict(detail or {})
    reserved = sorted({"history", "substitute"} & set(detail_out))
    if reserved:
        raise InvestigateError("detail_invalid", f"the detail may not set {', '.join(reserved)} (recorded by this verb)")
    if reason_code is not None:
        if detail_out.get("reason_code") not in (None, reason_code):
            raise InvestigateError(
                "detail_invalid",
                f"--reason-code {reason_code!r} disagrees with the detail's reason_code {detail_out['reason_code']!r}",
            )
        detail_out["reason_code"] = reason_code

    if row["verdict"] is not None and not supersede:
        raise InvestigateError(
            "verdict_exists",
            f"this dossier already carries verdict {row['verdict']} ({row['verdict_ts']}); pass --supersede to "
            "replace it (the earlier one is kept in the detail's history)",
            details={"verdict": row["verdict"], "verdict_by_launch": row["verdict_by_launch"]},
        )

    resolution = row["resolution"]
    flags = dossier.get("flags") or {}
    if verdict in ("REQUEST", "REQUEST-AS-FOUNDATIONAL") and resolution not in ("exact", "probable"):
        raise InvestigateError(
            "resolution_not_requestable",
            f"{verdict} needs a resolution of exact or probable; this dossier's is {resolution!r}",
            details={"resolution": resolution},
        )
    if verdict == "REQUEST" and flags.get("foundational"):
        raise InvestigateError(
            "foundational_needs_request_as_foundational",
            f"this work is flagged foundational (first published {flags.get('first_pub_year')}); request it with "
            "REQUEST-AS-FOUNDATIONAL and say what it founds and why a consolidating work will not do",
        )
    if verdict == "REQUEST-AS-FOUNDATIONAL":
        missing = []
        if not detail_out.get("founds"):
            missing.append("founds")
        reason = detail_out.get("foundational_reason")
        if reason not in FOUNDATIONAL_REASONS:
            missing.append(f"foundational_reason (one of {', '.join(FOUNDATIONAL_REASONS)})")
        if "consolidator" not in detail_out or (not detail_out.get("consolidator") and reason != "no-consolidator"):
            missing.append("consolidator")
        if missing:
            raise InvestigateError(
                "foundational_detail_incomplete",
                f"REQUEST-AS-FOUNDATIONAL needs {', '.join(missing)} in the detail",
                details={"missing": missing},
            )
    if verdict == "SUBSTITUTE-WITH":
        if substitute is None:
            raise InvestigateError(
                "substitute_required", "SUBSTITUTE-WITH needs one of --substitute-doi/--substitute-arxiv/--substitute-isbn",
            )
        kind, value = substitute
        normalised = _normalise_substitute(kind, value)
        present = _evidence_identifiers(dossier.get("evidence") or {})[kind]
        if not normalised or normalised not in present:
            raise InvestigateError(
                "substitute_not_in_evidence",
                f"substitute {kind} {value!r} appears nowhere in this dossier's evidence "
                f"({', '.join(_EVIDENCE_LISTS)}); a substitute must be a work the investigation found",
                details={"kind": kind, "value": value},
            )
        detail_out["substitute"] = {"kind": kind, "id": normalised}
    elif substitute is not None:
        raise InvestigateError("substitute_not_allowed", f"a substitute is only recorded with SUBSTITUTE-WITH, not {verdict}")
    if verdict == "HELD" and not dossier.get("held"):
        raise InvestigateError("held_block_missing", "HELD needs a dossier whose held check matched (its held block is empty)")

    require_xid_targets(store, "source_dossier", {"verdict_by_launch": launch_id})

    history: list[dict[str, Any]] = []
    if row["verdict"] is not None:
        previous = json.loads(row["verdict_detail_json"] or "{}")
        history = list(previous.pop("history", None) or [])
        history.append(
            {"verdict": row["verdict"], "detail": previous, "by_launch": row["verdict_by_launch"], "ts": row["verdict_ts"]}
        )
    detail_out["history"] = history
    ts = now()
    dossier["verdict"] = {"verdict": verdict, "detail": detail_out, "by_launch": launch_id, "ts": ts}
    data = _dossier_bytes(dossier)
    atomic_write_bytes(path, data)
    try:
        update(
            store, "source_dossier", pk_column="dossier_id", pk_value=row["dossier_id"],
            changes={
                "verdict": verdict, "verdict_detail_json": _canonical_json(detail_out), "verdict_by_launch": launch_id,
                "verdict_ts": ts, "dossier_sha256": hashlib.sha256(data).hexdigest(),
            },
        )
    except Exception:
        atomic_write_bytes(path, original)
        raise
    return {
        "dossier_id": row["dossier_id"], "list_id": dossier["list_id"], "row_id": dossier["row_id"],
        "seed_raw": dossier["seed_raw"], "verdict": verdict, "detail": detail_out, "by_launch": launch_id,
        "ts": ts, "superseded": row["verdict"] if row["verdict"] is not None else None, "dossier_path": str(path),
    }


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


def _natural_key(text: str) -> list[Any]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", text)]


def _authors_text(authors: Any) -> str:
    if isinstance(authors, str):
        names = [a.strip() for a in re.split(r",|;| and ", authors) if a.strip()]
    else:
        names = [str(a) for a in authors or [] if a]
    if not names:
        return "(no authors)"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} & {names[1]}"
    return f"{names[0]} et al."


def _year_text(year: Any) -> str:
    if isinstance(year, int):
        return str(year)
    if isinstance(year, str) and re.match(r"^\d{4}", year):
        return year[:4]
    return "n.d."


def _work_line(record: Mapping[str, Any], parsed: Mapping[str, Any], oa_pdf_url: Any) -> tuple[str, str]:
    doi = record.get("doi")
    if doi:
        ident = f"doi:{doi}"
    elif parsed.get("isbn"):
        ident = f"isbn:{parsed['isbn']}"
    elif record.get("arxiv_id"):
        ident = f"arXiv:{record['arxiv_id']}"
    else:
        ident = "(no identifier)"
    oa = "open" if (oa_pdf_url or record.get("arxiv_id")) else "paywalled"
    text = f"{_authors_text(record.get('authors'))}, {record.get('title') or '(no title)'}, {_year_text(record.get('year'))}, {ident}"
    return text, oa


def _substitute_entry(evidence: Mapping[str, Any], substitute: Mapping[str, Any]) -> dict[str, Any] | None:
    kind, wanted = substitute.get("kind"), substitute.get("id")
    candidates: list[Mapping[str, Any]] = []
    for key in ("citing", "citing_reviews", "references", "arxiv_neighbours"):
        candidates.extend(e for e in evidence.get(key) or [] if isinstance(e, Mapping))
    for position in evidence.get("author_works") or []:
        candidates.extend(w for w in position.get("works") or [] if isinstance(w, Mapping))
    for entry in candidates:
        if kind == "doi" and normalize_doi(entry.get("doi")) == wanted:
            return dict(entry)
        if kind == "arxiv" and (
            normalize_arxiv_id(entry.get("arxiv_id")) == wanted or normalize_doi(entry.get("doi")) == arxiv_to_doi(wanted)
        ):
            return dict(entry)
    return None


def _seed_line(dossier: Mapping[str, Any], verdict: str, detail: Mapping[str, Any]) -> str | None:
    record = (dossier.get("resolution") or {}).get("record") or {}
    parsed = dossier.get("parsed") or {}
    evidence = dossier.get("evidence") or {}
    why = detail.get("why") or "(no reason given)"
    if verdict == "REQUEST":
        text, oa = _work_line(record, parsed, evidence.get("oa_pdf_url"))
        return f"  FETCH {text} · {oa} · WHY: {why}"
    if verdict == "REQUEST-AS-FOUNDATIONAL":
        text, oa = _work_line(record, parsed, evidence.get("oa_pdf_url"))
        founds = detail.get("founds")
        founds_text = ", ".join(str(f) for f in founds) if isinstance(founds, list) else str(founds)
        return f"  FETCH-FOUNDATIONAL {text} · {oa} · founds: {founds_text} · reason: {detail.get('foundational_reason')}"
    if verdict == "SUBSTITUTE-WITH":
        substitute = detail.get("substitute") or {}
        entry = _substitute_entry(evidence, substitute) or {}
        record_like = {
            "authors": entry.get("authors"), "title": entry.get("title"),
            "year": entry.get("year") if entry.get("year") is not None else entry.get("published"),
            "doi": substitute.get("id") if substitute.get("kind") == "doi" else entry.get("doi"),
            "arxiv_id": substitute.get("id") if substitute.get("kind") == "arxiv" else entry.get("arxiv_id"),
        }
        parsed_like = {"isbn": substitute.get("id")} if substitute.get("kind") == "isbn" else {}
        text, oa = _work_line(record_like, parsed_like, None)
        return f"  FETCH {text} · {oa} · WHY: {why}"
    if verdict == "NEED-INFO":
        return f"  ASK {detail.get('question') or '(no question recorded)'}"
    return None


def render_list(store: Store, list_id: str, *, fmt: str = "lines") -> dict[str, Any]:
    """The operator lines for one list: one line per row, one indented line
    per seed whose verdict is ``REQUEST`` (``FETCH ...``),
    ``REQUEST-AS-FOUNDATIONAL`` (``FETCH-FOUNDATIONAL ...``),
    ``SUBSTITUTE-WITH`` (``FETCH`` the substitute) or ``NEED-INFO`` (``ASK
    ...``), and a trailing ``(held n · dropped n · substituted n)`` count
    (``· unjudged n`` added only when some seed has no verdict yet).

    Refused whole (``render_refused``, every offending seed named) when any row
    holds a seed whose resolution is ``mismatch`` and that has not been ruled
    ``DROP`` or ``SUBSTITUTE-WITH``, or a seed whose resolution is
    ``none``/``retry`` with an empty ``tried`` list -- a wrong identifier must
    not reach a human's fetch list, and neither may a seed nobody actually
    looked up. A ruled mismatch cannot leak: ``DROP`` prints nothing and
    ``SUBSTITUTE-WITH`` prints the substitute, never the cited identifier."""
    if fmt not in ("lines", "json"):
        raise InvestigateError("format_unknown", f"--format must be lines or json, got {fmt!r}")
    rows = [
        dict(r) for r in store.knowledge.execute(
            "SELECT * FROM source_dossier WHERE list_id = ?", (list_id,)
        ).fetchall()
    ]
    if not rows:
        raise InvestigateError("list_not_found", f"no dossiers recorded for list {list_id!r}")

    missing: list[str] = []
    loaded: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row in rows:
        path = _load_path(row["dossier_path"], store.program_root)
        try:
            dossier = json.loads(path.read_bytes())
        except (OSError, ValueError):
            missing.append(str(path))
            continue
        loaded.append((row, dossier))
    if missing:
        raise InvestigateError(
            "dossier_missing", f"{len(missing)} dossier file(s) of list {list_id!r} could not be read",
            details={"paths": missing},
        )

    by_row: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for row, dossier in loaded:
        by_row.setdefault(row["row_id"], []).append((row, dossier))
    ordered_rows = sorted(by_row, key=_natural_key)
    for row_id in ordered_rows:
        by_row[row_id].sort(
            key=lambda pair: (
                (pair[1].get("input_position") or {}).get("line", 0),
                (pair[1].get("input_position") or {}).get("seed", 0),
                pair[0]["seed_raw"],
            )
        )

    refused: list[dict[str, Any]] = []
    for row_id in ordered_rows:
        offenders = []
        for row, dossier in by_row[row_id]:
            tried = (dossier.get("resolution") or {}).get("tried") or []
            if row["resolution"] == "mismatch" and row["verdict"] not in ("DROP", "SUBSTITUTE-WITH"):
                reason = "resolution mismatch: the cited identifier resolves to another work, and no DROP or SUBSTITUTE-WITH verdict rules it out"
            elif row["resolution"] in ("none", "retry") and not tried:
                reason = f"resolution {row['resolution']} with nothing tried"
            else:
                continue
            offenders.append({"seed_raw": row["seed_raw"], "resolution": row["resolution"], "reason": reason,
                              "dossier_path": row["dossier_path"]})
        if offenders:
            refused.append({"row_id": row_id, "seeds": offenders})
    if refused:
        named = "; ".join(f"{r['row_id']}: {s['seed_raw']!r}" for r in refused for s in r["seeds"])
        raise InvestigateError(
            "render_refused", f"list {list_id!r} holds seed(s) that must not reach a fetch list: {named}",
            details={"rows": refused},
        )

    counts = {"held": 0, "dropped": 0, "substituted": 0, "unjudged": 0, "fetch": 0, "ask": 0}
    lines: list[str] = []
    rows_out: list[dict[str, Any]] = []
    for row_id in ordered_rows:
        pairs = by_row[row_id]
        question = next((d.get("question") for _, d in pairs if d.get("question")), None)
        lines.append(f"{row_id} · {question or '(no question)'}")
        row_counts = {"held": 0, "dropped": 0, "substituted": 0, "unjudged": 0}
        seeds_out = []
        for row, dossier in pairs:
            verdict = row["verdict"]
            detail = json.loads(row["verdict_detail_json"]) if row["verdict_detail_json"] else {}
            line = _seed_line(dossier, verdict, detail) if verdict else None
            if verdict is None:
                row_counts["unjudged"] += 1
            elif verdict == "HELD":
                row_counts["held"] += 1
            elif verdict == "DROP":
                row_counts["dropped"] += 1
            elif verdict == "SUBSTITUTE-WITH":
                row_counts["substituted"] += 1
            if line is not None:
                lines.append(line)
                counts["ask" if verdict == "NEED-INFO" else "fetch"] += 1
            seeds_out.append(
                {"seed_raw": row["seed_raw"], "verdict": verdict, "mechanical_state": row["mechanical_state"],
                 "resolution": row["resolution"], "line": line.strip() if line else None,
                 "dossier_path": row["dossier_path"]}
            )
        for key, value in row_counts.items():
            counts[key] += value
        rows_out.append({"row_id": row_id, "question": question, "seeds": seeds_out, "counts": row_counts})
    trailing = f"held {counts['held']} · dropped {counts['dropped']} · substituted {counts['substituted']}"
    if counts["unjudged"]:
        trailing += f" · unjudged {counts['unjudged']}"
    lines.append(f"({trailing})")

    if fmt == "json":
        return {"list_id": list_id, "format": "json", "rows": rows_out, "counts": counts, "lines": lines}
    return {"list_id": list_id, "format": "lines", "lines": lines, "text": "\n".join(lines), "counts": counts}
