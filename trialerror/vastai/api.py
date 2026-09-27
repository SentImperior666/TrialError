"""The vast.ai REST client. Ported from the public TrialError copy's embedding
backend, with the OCR lane's host filters (the query body is built by
:func:`trialerror.vastai.pricing.offer_query`).

Endpoint shapes [spec: docs.vast.ai; instance fields partly unverified --
design section 15]::

    POST   /api/v0/bundles/            search offers   (body: filter JSON)
    PUT    /api/v0/asks/<offer_id>/    create instance -> {"success", "new_contract"}
    GET    /api/v1/instances/          list own instances (v0 answers HTTP 410) -> {"instances": [...]}
    DELETE /api/v0/instances/<id>/     destroy (falls back to /api/v1 on HTTP 410)
    GET    /api/v0/users/current/      the account; ``credit`` when the API exposes it

The recorded drifts stay as the public copy found them: listing on
``/api/v1`` because v0 answers HTTP 410 [observed 2026-09-18]; DELETE falling
back to v1 on 410; ``no_such_ask`` -> :class:`OfferUnavailable`.

The API key is read from the operator-placed file whose PATH is configured
(``[vastai] api_key_path``) at the moment a request is made, held only in a
local variable, sent only in the ``Authorization`` header to the vast.ai host,
and never logged, printed, stored or included in an exception message.
``http`` and ``key_reader`` are injectable so no test ever reaches the network
or a real key.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

from trialerror.vastai.errors import OfferUnavailable, VastApiError, VastKeyMissing, redact_secrets

__all__ = [
    "VAST_BASE_URL",
    "Http",
    "VastApiError",
    "VastKeyMissing",
    "OfferUnavailable",
    "looks_like_a_key",
    "read_api_key",
    "urllib_http",
    "VastClient",
]

VAST_BASE_URL = "https://console.vast.ai/api/v0"
_MAX_LIST_PAGES = 20

#: ``http(method, url, headers, body_bytes_or_None, timeout_s) -> (status, parsed_json)``
Http = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, Any]]


def looks_like_a_key(value: str) -> bool:
    """A key path has a separator or a suffix; a vast.ai key is a long bare
    token. Anything that looks like the latter is never echoed."""
    return len(value) >= 20 and not any(c in value for c in r"/\.:")


def read_api_key(path: Path | str | None) -> str:
    """Read the operator-placed key file. Error messages name the PATH only,
    and not even that when the "path" looks like key material: a caller who
    passes the key itself where the path belongs must not get it echoed."""
    if not path:
        raise VastKeyMissing(
            "no [vastai] api_key_path in the DEV toml -- the operator places the key in a file and "
            "configures its path",
            next_actions=["set [vastai] api_key_path to the PATH of the operator-placed key file"],
        )
    if looks_like_a_key(str(path)):
        raise VastKeyMissing(
            "the vast.ai key path looks like a key, not a path (value withheld) -- pass the PATH of the "
            "key file ([vastai] api_key_path), never the key itself"
        )
    p = Path(path)
    try:
        key = p.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise VastKeyMissing(f"vast.ai key file not readable at {p} ({type(exc).__name__})") from None
    if not key:
        raise VastKeyMissing(f"vast.ai key file at {p} is empty")
    return key


def urllib_http(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout_s: float) -> tuple[int, Any]:
    """The one function in this package that touches the network. Tests
    replace it (``tests/_vastai_fakes.py``'s tripwire fails any test that
    reaches it)."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - fixed https host
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b""
        status = exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise VastApiError(f"vast.ai {method} {url.split('?')[0]} failed: {type(exc).__name__}") from None
    try:
        return status, json.loads(raw.decode("utf-8") or "null")
    except ValueError:
        return status, {"raw": raw[:500].decode("utf-8", "replace")}


class VastClient:
    def __init__(
        self,
        api_key_path: Path | str | None,
        *,
        http: Http | None = None,
        key_reader: Callable[[Any], str] | None = None,
        base_url: str = VAST_BASE_URL,
        timeout_s: float = 30.0,
    ):
        self._key_path = api_key_path
        self._http = http or urllib_http
        self._key_reader = key_reader or read_api_key
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s

    def _call(self, method: str, path: str, body: Any = None, *, v1: bool = False) -> Any:
        key = self._key_reader(self._key_path)
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            status, payload = self._http(method, f"{self._v1_base() if v1 else self._base}{path}", headers, data, self._timeout)
        except VastApiError as exc:
            # The transport's own text (it may name the URL) never carries the key.
            raise VastApiError(redact_secrets(exc, key)[:500], status=exc.status) from None
        if status >= 400:
            msg = payload.get("msg") or payload.get("error") if isinstance(payload, dict) else None
            # Redact (the key itself, Bearer <anything>, api_key=<anything>) BEFORE the cut.
            text = redact_secrets(f"{path.split('?')[0]} -> HTTP {status}: {msg or payload}", key)
            del key, headers
            raise VastApiError(f"vast.ai {method} {text[:360]}", status=status)
        del key, headers
        return payload

    def _redact(self, text: Any) -> str:
        """``text`` without this client's key (read again; unreadable = the
        credential shapes only), for an error built from a success payload."""
        try:
            key = self._key_reader(self._key_path)
        except Exception:  # noqa: BLE001 - redaction never raises
            key = None
        out = redact_secrets(text, key)
        del key
        return out

    def _v1_base(self) -> str:
        # vast.ai moved instance listing to /api/v1 (v0 answers HTTP 410,
        # observed 2026-09-18).
        return self._base[: -len("/v0")] + "/v1" if self._base.endswith("/v0") else self._base

    # -- the calls --------------------------------------------------------
    def search_offers(
        self,
        query: Mapping[str, Any] | None = None,
        *,
        gpu_names: list[str] | None = None,
        min_vram_gb: float | None = None,
        max_dph: float | None = None,
        min_reliability: float | None = None,
        limit: int = 64,
    ) -> list[dict[str, Any]]:
        """One read-only offer search, in either of two forms.

        * The public copy's keyword form (the embedding lane):
          ``search_offers(gpu_names=..., min_vram_gb=..., max_dph=...,
          min_reliability=..., limit=64)`` builds the filter body itself; the
          body and the answer are the public copy's, verbatim.
        * The OCR lane's query form: ``search_offers(query)``, where ``query``
          is the whole filter body (:func:`trialerror.vastai.pricing.offer_query`).
          The server's filtering is not trusted: the caller re-checks every
          returned offer.

        The two forms do not mix: a ``query`` with any keyword filter, or the
        keyword form with a filter missing, is a ``TypeError``."""
        filters = {"gpu_names": gpu_names, "min_vram_gb": min_vram_gb, "max_dph": max_dph,
                   "min_reliability": min_reliability}
        if query is not None:
            given = sorted(k for k, v in filters.items() if v is not None)
            if given:
                raise TypeError(f"search_offers: a query body and keyword filters do not mix ({', '.join(given)})")
            payload = self._call("POST", "/bundles/", dict(query))
            offers = payload.get("offers", []) if isinstance(payload, dict) else payload
            return [dict(o) for o in (offers or []) if isinstance(o, dict)]
        missing = sorted(k for k, v in filters.items() if v is None)
        if missing:
            raise TypeError(f"search_offers() missing required keyword argument(s): {', '.join(missing)}")
        body = {
            "gpu_name": {"in": [g.replace(" ", "_") for g in gpu_names] + list(gpu_names)},
            "gpu_ram": {"gte": int(min_vram_gb * 1024)},
            "dph_total": {"lte": max_dph},
            "reliability": {"gte": min_reliability},
            "num_gpus": {"eq": 1},
            "rentable": {"eq": True},
            "rented": {"eq": False},
            "verified": {"eq": True},
            "type": "ondemand",
            "order": [["dph_total", "asc"]],
            "limit": limit,
        }
        payload = self._call("POST", "/bundles/", body)
        offers = payload.get("offers", []) if isinstance(payload, dict) else payload
        return list(offers or [])

    def create_instance(self, offer_id: int | str, *, image: str, disk_gb: int, label: str, onstart: str) -> int:
        try:
            payload = self._call(
                "PUT",
                f"/asks/{offer_id}/",
                {"client_id": "me", "image": image, "disk": disk_gb, "label": label, "onstart": onstart, "runtype": "ssh"},
            )
        except OfferUnavailable:
            raise
        except VastApiError as exc:
            if "no_such_ask" in str(exc):
                raise OfferUnavailable(str(exc), offer_id=offer_id, status=exc.status) from None
            raise
        if not (isinstance(payload, dict) and payload.get("success") and payload.get("new_contract")):
            raise VastApiError(
                f"vast.ai create on offer {offer_id} did not return an instance id: {self._redact(payload)[:300]}"
            )
        return int(payload["new_contract"])

    def list_instances(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        token = None
        for _page in range(_MAX_LIST_PAGES):
            q = "?owner=me" + (f"&next_token={urllib.parse.quote(str(token))}" if token else "")
            payload = self._call("GET", "/instances/" + q, v1=True) or {}
            out.extend(payload.get("instances") or [])
            nxt = payload.get("next_token")
            if not nxt or nxt == token:
                return out
            token = nxt
        raise VastApiError(f"vast.ai instance listing did not end after {_MAX_LIST_PAGES} pages")

    def show_instance(self, instance_id: int) -> dict[str, Any] | None:
        for inst in self.list_instances():
            if str(inst.get("id")) == str(instance_id):
                return inst
        return None

    def destroy_instance(self, instance_id: int) -> None:
        path = f"/instances/{int(instance_id)}/"
        try:
            self._call("DELETE", path)
        except VastApiError as exc:
            if exc.status != 410:
                raise
            self._call("DELETE", path, v1=True)

    def ssh_keys(self) -> list[str] | None:
        """The public-key lines registered with the ACCOUNT, best effort:
        ``None`` when neither endpoint answers a shape
        :mod:`trialerror.vastai.sshkeys` reads (the pre-flight then says
        "unknown" rather than refusing). A free read. The lines are public
        halves; nothing here logs them."""
        from trialerror.vastai.sshkeys import SSH_KEYS_FALLBACK_PATH, SSH_KEYS_PATH, public_keys_in_payload

        for path in (SSH_KEYS_PATH, SSH_KEYS_FALLBACK_PATH):
            try:
                payload = self._call("GET", path)
            except VastKeyMissing:
                raise
            except VastApiError as exc:
                if exc.status in (400, 401, 403, 404, 405, 410):
                    continue
                raise
            keys = public_keys_in_payload(payload)
            if keys is not None:
                return keys
        return None

    def account_credit(self) -> float | None:
        """The account's credit in dollars, best effort: ``None`` when the
        endpoint or the ``credit`` field is absent [assumption: field name,
        design section 15 item 4]. Any other API error propagates, so a caller
        that caps on credit refuses rather than guessing."""
        try:
            payload = self._call("GET", "/users/current/")
        except VastKeyMissing:
            raise
        except VastApiError as exc:
            if exc.status in (404, 405, 410):
                return None
            raise
        if not isinstance(payload, dict):
            return None
        value = payload.get("credit")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)
