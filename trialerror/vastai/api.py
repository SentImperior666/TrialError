"""The vast.ai REST client.

Endpoint shapes [spec: docs.vast.ai as of 2026-09; instance fields partly
from memory -- confirm at first live use, design section 8]:

    POST   /api/v0/bundles/            search offers   (body: filter JSON)
    PUT    /api/v0/asks/<offer_id>/    create instance -> {"success", "new_contract"}
    GET    /api/v0/instances/          list own instances -> {"instances": [...]}
    DELETE /api/v0/instances/<id>/     destroy

The API key is read from the operator-placed file whose PATH is configured
(``[vastai] api_key_path``) at the moment a request is made, held only in a
local variable, sent only in the ``Authorization`` header to the vast.ai
host, and never logged, printed, stored or included in an exception message.
``http`` is injectable so no test ever reaches the network.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

__all__ = ["VAST_BASE_URL", "VastApiError", "VastKeyMissing", "read_api_key", "VastClient", "urllib_http"]

VAST_BASE_URL = "https://console.vast.ai/api/v0"

#: ``http(method, url, headers, body_bytes_or_None, timeout_s) -> (status, parsed_json)``
Http = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, Any]]


class VastApiError(RuntimeError):
    pass


class VastKeyMissing(VastApiError):
    pass


def read_api_key(path: Path | str | None) -> str:
    """Read the operator-placed key file. Error messages name the PATH only."""
    if not path:
        raise VastKeyMissing(
            "no [vastai] api_key_path in trialerror.toml -- the operator places the key in a file "
            "(e.g. keys/vastai.key) and configures its path"
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
        base_url: str = VAST_BASE_URL,
        timeout_s: float = 30.0,
    ):
        self._key_path = api_key_path
        self._http = http or urllib_http
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        key = read_api_key(self._key_path)
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, payload = self._http(method, f"{self._base}{path}", headers, data, self._timeout)
        del key, headers
        if status >= 400:
            msg = payload.get("msg") or payload.get("error") if isinstance(payload, dict) else None
            raise VastApiError(f"vast.ai {method} {path} -> HTTP {status}: {str(msg or payload)[:300]}")
        return payload

    # -- the five calls ---------------------------------------------------
    def search_offers(self, *, gpu_names: list[str], min_vram_gb: float, max_dph: float, min_reliability: float, limit: int = 64) -> list[dict[str, Any]]:
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
        payload = self._call(
            "PUT",
            f"/asks/{offer_id}/",
            {"client_id": "me", "image": image, "disk": disk_gb, "label": label, "onstart": onstart, "runtype": "ssh"},
        )
        if not (isinstance(payload, dict) and payload.get("success") and payload.get("new_contract")):
            raise VastApiError(f"vast.ai create on offer {offer_id} did not return an instance id: {str(payload)[:300]}")
        return int(payload["new_contract"])

    def list_instances(self) -> list[dict[str, Any]]:
        payload = self._call("GET", "/instances/?owner=me")
        return list((payload or {}).get("instances") or [])

    def show_instance(self, instance_id: int) -> dict[str, Any] | None:
        for inst in self.list_instances():
            if int(inst.get("id", -1)) == int(instance_id):
                return inst
        return None

    def destroy_instance(self, instance_id: int) -> None:
        self._call("DELETE", f"/instances/{int(instance_id)}/")
