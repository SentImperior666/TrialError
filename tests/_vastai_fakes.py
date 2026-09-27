"""Fakes for the vast.ai OCR executor's tests. No network, no GPU, no real key.

* :class:`FakeVast` -- an in-memory vast.ai API behind ``VastClient``'s
  injectable ``http``. It applies the search filters server-side, the way the
  real API does (``ignore_filters=True`` returns every offer, to prove the
  client re-checks), and it keeps the drifts the public TrialError copy's
  embedding backend recorded: v0 instance listing answers 410; a v0 DELETE
  410 toggle; ``no_such_ask`` for an offer taken between search and create.
* :class:`FakeClock` -- epoch seconds that move only when told to (``sleep``
  advances it), for the lease watchdog, destroy retries and deadlines.
* ``network_tripwire`` -- replaces ``trialerror.vastai.api.urllib_http``, the
  package's one network function, with one that raises and records; the test
  FAILS at teardown if anything reached it, even when the code under test
  swallowed the exception. Use it autouse in every vast.ai test module.
* ``isolated_state`` -- points the worker state directory (``LOCALAPPDATA`` /
  ``XDG_STATE_HOME``) into the test's tmp dir, so no test ever reads or writes
  the machine's real ledger or run records.

The "key" is written by the tests themselves (:data:`FAKE_KEY`); it is not a
secret.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

FAKE_KEY = "test-key-not-a-real-secret"


def make_offer(offer_id: int, **overrides: Any) -> dict[str, Any]:
    """A datacenter, verified, mid-tier offer with every field the OCR lane
    reads. ``verified`` follows ``verification`` unless overridden."""
    offer: dict[str, Any] = {
        "id": offer_id,
        "host_id": 7000 + offer_id,
        "machine_id": 9000 + offer_id,
        "gpu_name": "RTX 3090",
        "gpu_ram": 24576,
        "num_gpus": 1,
        "dph_base": 0.30,
        "storage_cost": 0.10,
        "dph_total": 0.30 + 32 * 0.10 / 730.0,
        "reliability": 0.99,
        "datacenter": True,
        "verification": "verified",
        "geolocation": "Sweden, SE",
        "cpu_ram": 128 * 1024,
        "inet_down_cost": 0.005,
        "inet_up_cost": 0.005,
        "cuda_max_good": 13.0,
        "disk_space": 200.0,
        "rentable": True,
        "rented": False,
    }
    offer.update(overrides)
    if "dph_total" not in overrides:
        offer["dph_total"] = offer["dph_base"] + 32 * offer["storage_cost"] / 730.0
    if "verified" not in overrides:
        offer["verified"] = offer.get("verification") == "verified"
    return offer


def default_offers() -> list[dict[str, Any]]:
    return [
        make_offer(11, dph_base=0.30),
        make_offer(12, dph_base=0.35),
        make_offer(13, gpu_name="RTX 4090", dph_base=0.60),  # high tier only
        make_offer(14, dph_base=0.20, datacenter=False),  # cheapest, but a home host
        make_offer(15, dph_base=0.22, verification="unverified"),  # cheap, unverified
    ]


def _country(value: Any) -> str:
    return str(value or "").split(",")[-1].strip().upper()


def _matches(field: str, value: Any, cond: dict[str, Any]) -> bool:
    for op, target in cond.items():
        if op in ("eq", "neq"):
            ok = value == target
            if op == "neq":
                ok = not ok
        elif op in ("gte", "gt", "lte", "lt"):
            if value is None or isinstance(value, bool):
                return False
            v, t = float(value), float(target)
            ok = {"gte": v >= t, "gt": v > t, "lte": v <= t, "lt": v < t}[op]
        elif op in ("in", "notin"):
            candidates = {str(value)} | ({_country(value)} if field == "geolocation" else set())
            ok = bool(candidates & {str(t) for t in target})
            if op == "notin":
                ok = not ok
        else:
            raise AssertionError(f"FakeVast: unknown operator {op!r} on {field!r}")
        if not ok:
            return False
    return True


class FakeVast:
    """In-memory vast.ai. Knobs (all attributes, set them in a test):

    ``offers``, ``ignore_filters``, ``gone`` (offer ids answering
    ``no_such_ask``), ``v0_delete_gone``, ``credit`` (``None`` = field absent),
    ``search_status`` (answer the search with that HTTP status), ``create_status``
    (answer creates with that status; ``create_lands`` = the instance exists
    anyway), ``delete_failures`` (the next N DELETEs answer 500), ``sticky``
    (instance ids still listed after DELETE), ``list_failures`` (the next N
    listings answer 503), ``boot_polls`` (listings that show ``loading``
    before ``running``), ``exit_on_boot`` (new instances show ``exited``)."""

    def __init__(self, offers: list[dict[str, Any]] | None = None, *, ignore_filters: bool = False,
                 key: str = FAKE_KEY) -> None:
        self.offers = offers if offers is not None else default_offers()
        self.ignore_filters = ignore_filters
        self.key = key
        self.instances: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[tuple[str, str, Any]] = []
        self.next_id = 5000
        self.gone: set[int] = set()
        self.v0_delete_gone = False
        self.credit: float | None = None
        #: Round 5: the ACCOUNT's registered ssh keys. ``None`` = the endpoint
        #: answers 404 (what a client that does not know it sees).
        self.account_ssh_keys: list[str] | None = None
        self.search_status: int | None = None
        self.create_status: int | None = None
        self.create_lands = False
        self.delete_failures = 0
        self.sticky: set[int] = set()
        self.list_failures = 0
        self.boot_polls = 0
        self.exit_on_boot = False
        self._polls: dict[int, int] = {}

    # -- the server ----------------------------------------------------------
    def http(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float):
        assert headers["Authorization"] == f"Bearer {self.key}"
        version = "v1" if "/api/v1" in url else "v0"
        path = url.split(f"/api/{version}", 1)[1]
        parsed = json.loads(body) if body else None
        self.calls.append((method, path))
        self.bodies.append((method, path, parsed))
        if method == "GET" and path.startswith("/instances") and version == "v0":
            return 410, {"error": "/api/v0/instances/ is deprecated. Use /api/v1/instances/ instead."}
        if method == "DELETE" and version == "v0" and self.v0_delete_gone:
            return 410, {"error": "deprecated"}
        if method == "POST" and path.startswith("/bundles"):
            if self.search_status:
                return self.search_status, {"error": "search unavailable"}
            return 200, {"offers": self.search(parsed or {})}
        if method == "PUT" and path.startswith("/asks/"):
            offer_id = int(path.strip("/").split("/")[1])
            if offer_id in self.gone:
                return 400, {"error": "error 404/3603: no_such_ask  Instance type is not available."}
            if self.create_status:
                if self.create_lands:
                    self._new_instance(offer_id, parsed)
                return self.create_status, {"error": "create failed"}
            iid = self._new_instance(offer_id, parsed)
            return 200, {"success": True, "new_contract": iid}
        if method == "GET" and path.startswith("/instances"):
            if self.list_failures > 0:
                self.list_failures -= 1
                return 503, {"error": "unavailable"}
            return 200, {"instances": [self._view(i) for i in self.instances.values()], "next_token": None}
        if method == "DELETE" and path.startswith("/instances/"):
            if self.delete_failures > 0:
                self.delete_failures -= 1
                return 500, {"error": "internal error"}
            iid = int(path.strip("/").split("/")[1])
            if iid not in self.sticky:
                self.instances.pop(iid, None)
            return 200, {"success": True}
        if method == "GET" and path.startswith("/ssh"):
            if self.account_ssh_keys is None:
                return 404, {"msg": "no route"}
            return 200, {"ssh_keys": [{"id": i, "ssh_key": k} for i, k in enumerate(self.account_ssh_keys)]}
        if method == "GET" and path.startswith("/users/current"):
            return 200, ({} if self.credit is None else {"credit": self.credit})
        return 404, {"msg": "no route"}

    def search(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        if self.ignore_filters:
            return [dict(o) for o in self.offers]
        out = []
        for offer in self.offers:
            if all(
                _matches(field, offer.get(field), cond)
                for field, cond in query.items()
                if isinstance(cond, dict)
            ):
                out.append(dict(offer))
        return out

    def _new_instance(self, offer_id: int, req: dict[str, Any]) -> int:
        self.next_id += 1
        offer = next((o for o in self.offers if o.get("id") == offer_id), {})
        self.instances[self.next_id] = {
            "id": self.next_id,
            "label": req["label"],
            "actual_status": "exited" if self.exit_on_boot else "running",
            "ssh_host": "10.0.0.1",
            "ssh_port": 2222,
            "onstart": req["onstart"],
            "image": req["image"],
            "disk": req["disk"],
            "offer_id": offer_id,
            "machine_id": offer.get("machine_id"),
            "host_id": offer.get("host_id"),
            "gpu_name": offer.get("gpu_name"),
        }
        self._polls[self.next_id] = 0
        return self.next_id

    def _view(self, inst: dict[str, Any]) -> dict[str, Any]:
        view = dict(inst)
        iid = inst["id"]
        if iid in self._polls:
            self._polls[iid] += 1
            if self._polls[iid] <= self.boot_polls and view["actual_status"] == "running":
                view["actual_status"] = "loading"
        return view

    # -- inspection ----------------------------------------------------------
    def verbs(self, method: str) -> list[str]:
        return [p for m, p in self.calls if m == method]

    def search_bodies(self) -> list[dict[str, Any]]:
        return [b for m, p, b in self.bodies if m == "POST" and p.startswith("/bundles")]


class FakeClock:
    """``clock()`` returns epoch seconds; ``sleep(s)`` advances them."""

    def __init__(self, start: float = 1_900_000_000.0) -> None:
        self.t = float(start)
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.t += float(seconds)

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.t, timezone.utc)


class NetworkTripwire(BaseException):
    """Raised by the tripwire. A ``BaseException`` so an ``except Exception``
    in the code under test cannot swallow it (the teardown check fails the
    test anyway)."""


@pytest.fixture
def network_tripwire(monkeypatch):
    """Fail the test on ANY call to ``trialerror.vastai.api.urllib_http``."""
    import trialerror.vastai.api as api_mod

    calls: list[tuple[str, str]] = []

    def tripwire(method, url, headers, body, timeout_s):
        calls.append((method, str(url).split("?")[0]))
        raise NetworkTripwire(f"network tripwire: a test reached the real vast.ai client ({method} {url.split('?')[0]})")

    monkeypatch.setattr(api_mod, "urllib_http", tripwire)
    yield calls
    if calls:
        pytest.fail(f"network tripwire: {len(calls)} real urllib_http call(s): {calls}")


@pytest.fixture
def isolated_state(tmp_path, monkeypatch) -> Path:
    """The worker state dir under tmp_path; returns the vast.ai state dir."""
    base = tmp_path / "localstate"
    monkeypatch.setenv("LOCALAPPDATA", str(base))
    monkeypatch.setenv("XDG_STATE_HOME", str(base))
    from trialerror.vastai.ledger import default_state_dir

    return default_state_dir()


def write_key(directory: Path, key: str = FAKE_KEY) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "vastai.key"
    path.write_text(key + "\n", encoding="utf-8")
    return path


def dev_toml(*, executor: str = "vastai", vastai: dict[str, Any] | None = None, ocr: dict[str, Any] | None = None,
             egress: dict[str, Any] | None = None, ingest_ocr: dict[str, Any] | None = None) -> dict[str, Any]:
    """A parsed DEV toml with a ``[vastai]`` table whose key and identity
    paths are relative to the backend-config-root (``keys/``)."""
    table: dict[str, Any] = {"api_key_path": "keys/vastai.key", "ssh_identity_path": "keys/vastai_ed25519"}
    table.update(vastai or {})
    if ocr is not None:
        table["ocr"] = dict(ocr)
    if egress is not None:
        table["egress"] = dict(egress)
    return {
        "program": {"id": "dev-root"},
        "ingest": {"ocr": {"backend": "marker", "executor": executor, **(ingest_ocr or {})}},
        "vastai": table,
    }


def toml_text(raw: dict[str, Any]) -> str:
    """A minimal TOML writer for :func:`dev_toml`-shaped dicts (tables of
    scalars and lists of strings, nested tables), for tests that need a
    ``trialerror.toml`` on disk."""
    lines: list[str] = []

    def scalar(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, (list, tuple)):
            return "[" + ", ".join(scalar(x) for x in v) + "]"
        return json.dumps(str(v))

    def emit(prefix: str, table: dict[str, Any]) -> None:
        simple = {k: v for k, v in table.items() if not isinstance(v, dict)}
        nested = {k: v for k, v in table.items() if isinstance(v, dict)}
        if prefix and (simple or not nested):
            lines.append(f"[{prefix}]")
        for k, v in simple.items():
            lines.append(f"{k} = {scalar(v)}")
        if simple or not nested:
            lines.append("")
        for k, v in nested.items():
            emit(f"{prefix}.{k}" if prefix else k, v)

    emit("", raw)
    return "\n".join(lines) + "\n"
