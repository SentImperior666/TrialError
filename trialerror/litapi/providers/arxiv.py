"""arXiv ``Provider`` client (v3-acquisition build, C-0064 flags F1/F2
RESOLVED -- ``docs/EXTERNAL_API_FACTS.md``). arXiv is the simplest of the
four providers this package now ships: fully keyless, no account, no
signup -- the ONLY real constraint is the documented **1 request per 3
seconds** ToU rate limit (``info.arxiv.org/help/api/tou.html``, fetched
directly as a primary source per that doc's own Method note), which this
module enforces client-side as the provider's ``min_interval_s`` default
(:data:`trialerror.litapi.config._DEFAULT_MIN_INTERVAL_S` ``["arxiv"] == 3.0``)
-- a hard-enforced pacing gate via the SAME :class:`~trialerror.litapi.providers.base.RateLimiter`
every other provider uses, not a config suggestion a caller could
accidentally leave unset and go faster than the ToU allows.

Unlike OpenAlex/Semantic Scholar, the arXiv API responds with an **Atom
XML feed**, not JSON (``TransportResponse.json_body`` is ``None`` for
every real arXiv response -- this module parses ``response.text`` via
stdlib ``xml.etree.ElementTree``, the same dependency-free XML tooling
``trialerror.ingest.normalizers`` already uses for EPUB parsing).

TRIALERROR-DEV-NOTE (arXiv's documented "success" envelope for an unknown id,
not independently re-verified this session): a request for a
non-existent/malformed arXiv id returns HTTP **200**, not 404 -- the
response is a well-formed Atom feed containing exactly one ``<entry>``
whose ``<id>`` is ``http://arxiv.org/api/errors#...`` and whose
``<title>`` is literally ``"Error"``. This is long-standing, widely
documented arXiv API behavior (mirrored by every third-party arXiv client
this package's own mining docs surveyed, e.g. ``docs/mining/S2-scilit-2__arxiv-mcp-server.md``'s
"search is optional" client), but was not independently re-fetched
against a live malformed-id call in this session -- :data:`ArxivProvider`
detects this error-entry shape (:func:`_entry_is_error`) rather than
relying on the HTTP status code, which arXiv itself never varies for this
case.

TRIALERROR-DEV-NOTE (get_by_doi / get_citations, deliberate scope limits): the
arXiv API has no by-DOI lookup endpoint and no citation-graph endpoint at
all (confirmed by omission across every arXiv API doc surveyed by this
build and by ``docs/mining/S2-scilit-2__arxiv-mcp-server.md``'s own
observation that that project backs ITS citation-graph feature via a
separate Semantic Scholar call, not arXiv itself). :meth:`ArxivProvider.get_by_doi`
only succeeds for arXiv's own self-assigned ``10.48550/arXiv.<id>`` DOI
form (reversing :func:`trialerror.litapi.models.arxiv_to_doi`) -- any other DOI
raises :class:`~trialerror.litapi.errors.ProviderNotFoundError` immediately,
with zero network calls, rather than silently returning ``None``.
:meth:`ArxivProvider.get_citations` always raises
:class:`~trialerror.litapi.errors.ProviderUnsupportedOperationError` -- this
provider genuinely has nothing to offer that verb, ever, not just "no
record for this identifier".
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

from trialerror.litapi.config import ProviderApiConfig
from trialerror.litapi.errors import (
    LitApiError,
    ProviderNotFoundError,
    ProviderTransportError,
    ProviderUnsupportedOperationError,
)
from trialerror.litapi.models import CitationsPage, WorkRecord, normalize_arxiv_id, normalize_doi
from trialerror.litapi.providers.base import RateLimiter, get_with_retry, raise_for_transport_error
from trialerror.litapi.transport import ProviderTransport, TransportResponse

__all__ = ["ArxivProvider", "normalize_license_url"]

#: arXiv's Atom feed namespaces (standard Atom + arXiv's own extension
#: namespace for doi/journal_ref/comment/primary_category/affiliation).
_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_ARXIV_NS = "{http://arxiv.org/schemas/atom}"

_ARXIV_DOI_RE = re.compile(r"^10\.48550/arxiv\.(.+)$", re.IGNORECASE)

#: Version suffix on an arXiv id (``2101.00001v2``). OAI-PMH identifiers are
#: version-less, so :meth:`ArxivProvider.get_license` strips it.
_VERSION_SUFFIX_RE = re.compile(r"v\d+$", re.IGNORECASE)

#: Licence URL -> short token, matched on the url with its scheme and any
#: trailing slash stripped (lane FB-acq item 4). A short vocabulary of tokens
#: is what makes the recorded grant groupable and greppable; anything not in
#: this table is recorded AS ITSELF (stripped and lowercased) rather than
#: dropped, because an unknown licence is still a licence and inventing a
#: token for it would be worse than keeping the url.
#:
#: The three arXiv entries matter most and are the reason this exists at all:
#: ``arxiv-nonexclusive-distrib-1.0`` grants arXiv distribution rights and
#: grants the reader none, while the tier such a paper is registered under is
#: ``open``.
_LICENSE_URL_TOKENS: dict[str, str] = {
    "creativecommons.org/licenses/by/4.0": "cc-by-4.0",
    "creativecommons.org/licenses/by-sa/4.0": "cc-by-sa-4.0",
    "creativecommons.org/licenses/by-nc-sa/4.0": "cc-by-nc-sa-4.0",
    "creativecommons.org/licenses/by-nc-nd/4.0": "cc-by-nc-nd-4.0",
    "creativecommons.org/licenses/by/3.0": "cc-by-3.0",
    "creativecommons.org/licenses/by-nc-sa/3.0": "cc-by-nc-sa-3.0",
    "creativecommons.org/publicdomain/zero/1.0": "cc0-1.0",
    "arxiv.org/licenses/nonexclusive-distrib/1.0": "arxiv-nonexclusive-distrib-1.0",
    "arxiv.org/licenses/assumed-1991-2003": "arxiv-assumed-1991-2003",
}


def normalize_license_url(raw: str | None) -> str | None:
    """A licence url as a short token, or the stripped url itself when this
    build knows no token for it. ``None`` in, ``None`` out."""
    if not raw:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    for prefix in ("https://", "http://", "//"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    text = text.rstrip("/")
    return _LICENSE_URL_TOKENS.get(text, text) or None


def _doi_to_arxiv_id(doi: str | None) -> str | None:
    """Reverse of :func:`trialerror.litapi.models.arxiv_to_doi`: recover the
    arXiv id from arXiv's own self-assigned DOI form. Any DOI that isn't
    that exact synthesized form (a real journal DOI, a malformed string,
    ``None``) returns ``None`` -- arXiv's API has no way to resolve those."""
    normalized = normalize_doi(doi)
    if not normalized:
        return None
    m = _ARXIV_DOI_RE.match(normalized)
    if not m:
        return None
    return normalize_arxiv_id(m.group(1))


def _text(el: ET.Element, tag: str, *, ns: str = _ATOM_NS) -> str | None:
    child = el.find(f"{ns}{tag}")
    if child is None or child.text is None:
        return None
    collapsed = " ".join(child.text.split())  # arXiv titles/summaries wrap with embedded newlines
    return collapsed or None


def _entry_is_error(entry: ET.Element) -> bool:
    """See this module's docstring TRIALERROR-DEV-NOTE: arXiv signals
    "not found"/"malformed id" via a single error-shaped ``<entry>``
    inside an otherwise-normal HTTP 200 Atom feed, not via the HTTP
    status code."""
    id_text = _text(entry, "id") or ""
    return id_text.startswith("http://arxiv.org/api/errors")


def _parse_feed(text: str) -> list[ET.Element]:
    root = ET.fromstring(text)
    return root.findall(f"{_ATOM_NS}entry")


def _entry_to_record(entry: ET.Element) -> WorkRecord:
    id_url = _text(entry, "id")
    raw_id = id_url.rsplit("/abs/", 1)[-1] if id_url and "/abs/" in id_url else None
    arxiv_id = normalize_arxiv_id(raw_id)

    authors = [name for a in entry.findall(f"{_ATOM_NS}author") if (name := _text(a, "name"))]

    doi = _text(entry, "doi", ns=_ARXIV_NS)
    journal_ref = _text(entry, "journal_ref", ns=_ARXIV_NS)

    published = _text(entry, "published")
    year = int(published[:4]) if published and published[:4].isdigit() else None

    pdf_url = None
    for link in entry.findall(f"{_ATOM_NS}link"):
        if link.get("title") == "pdf" and link.get("href"):
            pdf_url = link.get("href")
            break
    if not pdf_url and arxiv_id:
        # arXiv's own stable, documented PDF URL convention -- a safe
        # fallback when the feed's own pdf <link> is (unexpectedly) absent.
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"

    # lane FB-acq item 4: two places a licence COULD appear in an entry -- an
    # arXiv-namespaced <arxiv:license> element, and a <link rel="license">.
    # Whether arXiv's Atom feed ever carries either could not be established
    # offline by this build; the parse is defensive, costs one find() per
    # entry, and is what makes the free case free (no OAI request needed).
    license_url = _text(entry, "license", ns=_ARXIV_NS)
    if not license_url:
        for link in entry.findall(f"{_ATOM_NS}link"):
            if link.get("rel") == "license" and link.get("href"):
                license_url = link.get("href")
                break

    other: dict = {}
    if journal_ref:
        other["journal_ref"] = journal_ref
    if license_url:
        other["license_url"] = license_url

    return WorkRecord(
        title=_text(entry, "title"),
        doi=normalize_doi(doi),
        arxiv_id=arxiv_id,
        authors=authors,
        year=year,
        venue=journal_ref,
        abstract=_text(entry, "summary"),
        citation_count=None,  # arXiv's own API carries no citation-count field
        oa_pdf_url=pdf_url,
        url=id_url,
        external_ids={},
        other=other,
    )


class ArxivProvider:
    name = "arxiv"
    #: FB-1 item F3: what this provider's `search` matches on.
    search_scope = "all fields (search_query=all:)"

    def __init__(
        self, transport: ProviderTransport, config: ProviderApiConfig, *,
        program_root=None, pacing_dir=None,
    ):
        # program_root accepted (unused) only to keep this provider's
        # constructor call-shape identical to OpenAlexProvider/
        # SemanticScholarProvider -- trialerror.litapi.client.build_default_providers
        # constructs every provider class the same way; arXiv has no API
        # key concept at all, so there is nothing to resolve here.
        self.transport = transport
        self.config = config
        # lane FB-acq item 2: ``pacing_dir`` turns the in-memory rate limiter
        # into a cross-INVOCATION one (every CLI call is a new process, so the
        # in-memory gate alone spaced nothing across a shell loop). ``None``
        # -- the default every test and library caller gets -- keeps exactly
        # today's in-process behaviour and writes no files anywhere.
        self._rate_limiter = RateLimiter(
            config.min_interval_s,
            stamp_path=(Path(pacing_dir) / f"{self.name}.json") if pacing_dir else None,
        )
        #: The last request's :func:`get_with_retry` stats (attempts, total
        #: backoff waited, last status, Retry-After, whether a request went out
        #: at all), reset per request and read by
        #: ``trialerror.litapi.client._provider_outcome``.
        self.last_request_stats: dict = {}

    #: arXiv answers Atom XML, never JSON, and
    #: ``UrllibTransport.get``'s own default is ``Accept: application/json``
    #: for every provider that sends no header of its own -- which is what an
    #: Atom endpoint is entitled to answer 406 to. The ``*/*`` tail keeps the
    #: header harmless whatever the host actually negotiates.
    _ACCEPT = "application/atom+xml, application/xml;q=0.9, */*;q=0.8"

    def _get(self, query: dict[str, str]) -> TransportResponse:
        url = f"{self.config.base_url}/query?{urlencode(query)}"
        self.last_request_stats = {}
        return get_with_retry(
            self.transport,
            url,
            provider=self.name,
            headers={"Accept": self._ACCEPT},
            timeout_s=self.config.timeout_s,
            rate_limiter=self._rate_limiter,
            retry_attempts=self.config.retry_attempts,
            retry_on_status=self.config.retry_on_status,
            max_total_wait_s=self.config.max_total_wait_s,
            stats=self.last_request_stats,
        )

    # -- Provider interface --------------------------------------------------

    def get_by_doi(self, doi: str) -> WorkRecord | None:
        """See this module's docstring TRIALERROR-DEV-NOTE: only succeeds for
        arXiv's own self-assigned DOI form; any other DOI is refused with
        zero network calls (arXiv has no by-DOI lookup at all)."""
        arxiv_id = _doi_to_arxiv_id(doi)
        if not arxiv_id:
            raise ProviderNotFoundError(
                f"arXiv: {doi!r} is not an arXiv-derived DOI (10.48550/arXiv.<id> form) -- "
                "arXiv's API has no by-DOI lookup endpoint",
                provider=self.name,
            )
        return self.get_by_arxiv(arxiv_id)

    def get_by_arxiv(self, arxiv_id: str) -> WorkRecord | None:
        normalized = normalize_arxiv_id(arxiv_id)
        if not normalized:
            return None
        response = self._get({"id_list": normalized})
        raise_for_transport_error(response, provider=self.name, context=f"get_by_arxiv({arxiv_id!r})")
        entries = _parse_feed(response.text)
        if not entries or _entry_is_error(entries[0]):
            raise ProviderNotFoundError(f"arXiv: no paper found for id {arxiv_id!r}", provider=self.name)
        return _entry_to_record(entries[0])

    def search(self, query: str, *, limit: int = 10) -> list[WorkRecord]:
        limit = max(1, min(limit, 100))
        response = self._get({"search_query": f"all:{query}", "start": "0", "max_results": str(limit)})
        raise_for_transport_error(response, provider=self.name, context=f"search({query!r})")
        entries = _parse_feed(response.text)
        records = [_entry_to_record(e) for e in entries if not _entry_is_error(e)]
        return records[:limit]

    # -- licence (beyond the Provider interface) ------------------------------

    def get_license(self, record_or_id) -> tuple[str | None, str]:
        """This paper's own licence GRANT, as ``(token_or_None, source)``.
        Never raises -- lane FB-acq item 4's whole point is that recording a
        grant must not be able to fail an acquisition.

        Two places are tried, cheapest first:

        1. The Atom entry already in hand. ``_entry_to_record`` stores any
           ``<arxiv:license>`` text or ``<link rel="license">`` href in
           ``record.other["license_url"]`` -> ``(token, "arxiv_atom")``.
        2. When ``[litapi.arxiv].license_lookup`` is true (the default), ONE
           OAI-PMH ``GetRecord`` request through the same limiter and retry
           policy as every other call this provider makes. Found ->
           ``(token, "arxiv_oai")``; a well-formed answer with no licence, or
           an OAI ``<error>`` -> ``(None, "none_reported")``; ANY failure --
           bad status, transport, malformed XML -> ``(None,
           "lookup_failed:<outcome>")`` in the same outcome words
           ``trialerror.ingest.acquire``'s legs use.

        ``(None, "not_attempted")`` when the lookup is switched off and the
        entry carried nothing.

        TRIALERROR-DEV-NOTE (ASSUMED, not verifiable without egress): the OAI-PMH
        endpoint (``[litapi.arxiv].oai_base_url``, default
        ``https://export.arxiv.org/oai2``), the ``oai:arXiv.org:<id>``
        identifier form, the ``metadataPrefix=arXiv`` name AND the response's
        own element names were NOT confirmed against a live call by this build,
        which has no network egress. They are arXiv's long-documented OAI
        interface as this repo's own mining notes describe it, not a re-fetched
        fact. The parse is deliberately namespace-agnostic (the first element
        whose tag ends in ``license``) so a namespace change alone cannot break
        it, and the two config keys an operator changes if the host or the path
        differs are ``[litapi.arxiv].oai_base_url`` and, to switch the request
        off entirely, ``[litapi.arxiv].license_lookup = false``.
        """
        record = record_or_id if not isinstance(record_or_id, str) else None
        arxiv_id = record.arxiv_id if record is not None else normalize_arxiv_id(record_or_id)

        if record is not None:
            token = normalize_license_url((record.other or {}).get("license_url"))
            if token:
                return token, "arxiv_atom"

        if not getattr(self.config, "license_lookup", True):
            return None, "not_attempted"
        if not arxiv_id:
            return None, "not_attempted"

        base = getattr(self.config, "oai_base_url", None)
        if not base:
            return None, "not_attempted"
        bare_id = _VERSION_SUFFIX_RE.sub("", arxiv_id)
        url = f"{base}?{urlencode({'verb': 'GetRecord', 'identifier': f'oai:arXiv.org:{bare_id}', 'metadataPrefix': 'arXiv'})}"
        try:
            self.last_request_stats = {}
            response = get_with_retry(
                self.transport,
                url,
                provider=self.name,
                headers={"Accept": "application/xml, text/xml;q=0.9, */*;q=0.8"},
                timeout_s=self.config.timeout_s,
                rate_limiter=self._rate_limiter,
                retry_attempts=self.config.retry_attempts,
                retry_on_status=self.config.retry_on_status,
                max_total_wait_s=self.config.max_total_wait_s,
                stats=self.last_request_stats,
            )
        except ProviderTransportError as exc:
            if exc.host is not None:
                return None, "lookup_failed:transport_unreachable"
            if exc.status_code == 429:
                return None, "lookup_failed:rate_limited"
            return None, "lookup_failed:http_error"
        except LitApiError:
            return None, "lookup_failed:provider_error"
        except Exception:  # noqa: BLE001 - deliberate: a grant lookup never fails an acquisition
            return None, "lookup_failed:provider_error"

        if not response.ok:
            if response.status_code == 429:
                return None, "lookup_failed:rate_limited"
            return None, "lookup_failed:http_error"
        try:
            root = ET.fromstring(response.text)
        except ET.ParseError:
            return None, "lookup_failed:provider_error"
        for element in root.iter():
            tag = str(element.tag)
            if tag.rsplit("}", 1)[-1] == "license" and element.text:
                token = normalize_license_url(element.text)
                if token:
                    return token, "arxiv_oai"
        return None, "none_reported"

    def get_citations(self, identifier: str, *, limit: int = 100, offset: int = 0) -> CitationsPage:
        """See this module's docstring TRIALERROR-DEV-NOTE: arXiv has no
        citation-graph endpoint at all, unconditionally."""
        raise ProviderUnsupportedOperationError(
            f"arXiv has no citation-graph endpoint (metadata/full-text API only) -- identifier={identifier!r}",
            provider=self.name,
        )
