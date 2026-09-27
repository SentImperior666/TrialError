"""vast.ai round 2, lane L3: ``trialerror vastai ...`` on fakes.

``approve-ocr`` (TTY only, typed challenge, sealed approval, an
``approval_minted`` ledger row without the mac), ``approve-high`` (ported),
``plan`` (one read-only search, rents nothing, says which caps pass),
``ledger`` (filters, torn lines), ``reap`` (this root only, ``--dry-run``) and
``lock-deps`` (hashes for every file of each release, the image's CUDA stack
dropped). No network: FakeVast behind the injectable client, a fake PyPI, and
two tripwires.
"""

from __future__ import annotations

import io
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import trialerror.cli.vastai as cli_vastai
from trialerror.cli import build_parser
from trialerror.vastai import envlock, guard
from trialerror.vastai.api import VastClient
from trialerror.vastai.config import load_vast_config
from trialerror.vastai.egress import sign_egress_approval, verify_egress_approval, write_egress_approval
from trialerror.vastai.guard import program_fingerprint, verify_high_tier_approval
from trialerror.vastai.lease import make_label
from trialerror.vastai.ledger import Ledger
from tests._vastai_fakes import (  # noqa: F401 - fixtures
    FakeVast,
    dev_toml,
    isolated_state,
    network_tripwire,
    toml_text,
    write_key,
)

A4_PT = (595.0, 842.0)
DOC_SHA = "ab" * 32


@pytest.fixture(autouse=True)
def _guarded(network_tripwire, isolated_state, monkeypatch):
    """Every test: no real vast.ai call, no real PyPI call, state in tmp."""

    def pypi_tripwire(url, **_kw):
        raise AssertionError(f"lock-deps reached the real network: {url}")

    monkeypatch.setattr(envlock, "urllib_get", pypi_tripwire)
    monkeypatch.setattr(cli_vastai, "_out", io.StringIO())
    yield isolated_state


def _root(tmp_path: Path, **toml_kwargs) -> Path:
    root = tmp_path / "devroot"
    root.mkdir(exist_ok=True)
    write_key(root / "keys")
    toml_kwargs.setdefault("egress", {"allow_documents": [DOC_SHA]})
    (root / "trialerror.toml").write_text(toml_text(dev_toml(**toml_kwargs)), encoding="utf-8")
    return root


def _cfg(root: Path):
    from trialerror.util.config import load_config

    return load_vast_config(load_config(root / "trialerror.toml").raw, config_root=root)


def _run(*argv: str) -> dict:
    args = build_parser().parse_args(["vastai", *argv])
    return args.handler(args)


def _interactive(monkeypatch, *, answer: str | None = None) -> None:
    """A TTY whose operator types back the printed challenge (or ``answer``)."""
    monkeypatch.setattr(guard, "_is_interactive", lambda: True)

    def typed(_prompt: str = "") -> str:
        if answer is not None:
            return answer
        shown = cli_vastai._out.getvalue()
        return re.findall(r"Type ([0-9a-f]+) to confirm", shown)[-1]

    monkeypatch.setattr(cli_vastai, "_input", typed)


def _fake_client(monkeypatch, fake: FakeVast) -> None:
    monkeypatch.setattr(cli_vastai, "_client_factory", lambda key_path: VastClient(key_path, http=fake.http))


def _write_pdf(path: Path, pages: int = 10) -> Path:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=A4_PT[0], height=A4_PT[1])
    with open(path, "wb") as f:
        writer.write(f)
    return path


def _sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _approve(root: Path) -> dict:
    cfg = _cfg(root)
    body = sign_egress_approval(cfg, now=datetime.now(timezone.utc))
    write_egress_approval(cfg, body)
    return body


# ---------------------------------------------------------------------------
# the group
# ---------------------------------------------------------------------------
def test_every_verb_takes_the_backend_config_root():
    parser = build_parser()
    group = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction").choices["vastai"]
    verbs = next(a for a in group._actions if a.__class__.__name__ == "_SubParsersAction").choices
    assert set(verbs) == {"approve-ocr", "approve-high", "plan", "ledger", "reap", "lock-deps", "run", "ssh-probe"}
    for name, sub in verbs.items():
        flags = {o for a in sub._actions for o in a.option_strings}
        assert "--backend-config-root" in flags, name


def test_a_missing_root_is_a_named_refusal_with_next_actions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = _run("reap", "--ocr")
    assert env["ok"] is False and env["error"]["code"] == "no_backend_config_root"
    assert env["nextActions"]


def test_a_refused_config_names_the_key(tmp_path):
    root = _root(tmp_path, vastai={"keep_alive": True})
    env = _run("reap", "--backend-config-root", str(root))
    assert env["ok"] is False and "keep_alive" in env["error"]["message"]
    assert env["nextActions"]


# ---------------------------------------------------------------------------
# approve-ocr
# ---------------------------------------------------------------------------
def test_approve_ocr_refuses_without_a_tty(tmp_path, monkeypatch, isolated_state):
    root = _root(tmp_path)
    monkeypatch.setattr(guard, "_is_interactive", lambda: False)
    env = _run("approve-ocr", "--backend-config-root", str(root))
    assert env["ok"] is False and env["error"]["code"] == "not_interactive"
    assert env["nextActions"]
    assert not _cfg(root).approval_path.exists()
    assert Ledger(isolated_state).read().rows == []


def test_approve_ocr_mints_a_sealed_approval_and_a_ledger_row_without_the_mac(tmp_path, monkeypatch, isolated_state):
    root = _root(tmp_path, egress={"allow_documents": [DOC_SHA], "allow_license_tiers": ["open"]})
    _interactive(monkeypatch)
    env = _run("approve-ocr", "--backend-config-root", str(root), "--days", "3", "--max-total-usd", "12",
               "--max-job-usd", "2")
    assert env["ok"] is True, env
    shown = cli_vastai._out.getvalue()
    for words in ("licence tiers   : open", "named documents : 1", "datacenter only : True", "at most $12.00",
                  "at most $2.00"):
        assert words in shown, words

    cfg = _cfg(root)
    body = json.loads(cfg.approval_path.read_text(encoding="utf-8"))
    verify_egress_approval(cfg, body, now=datetime.now(timezone.utc))  # sealed, signed, in date
    assert body["max_total_usd"] == 12.0 and body["max_job_usd"] == 2.0
    issued, expires = (datetime.fromisoformat(body[k]) for k in ("issued", "expires"))
    assert expires - issued == timedelta(days=3)

    rows = Ledger(isolated_state).read().rows
    assert [r["kind"] for r in rows] == ["approval_minted"]
    assert rows[0]["nonce"] == body["nonce"] == env["result"]["nonce"]
    assert rows[0]["summary"]["allow_license_tiers"] == ["open"]
    assert "mac" not in json.dumps(rows[0])


def test_approve_ocr_a_wrong_challenge_writes_nothing(tmp_path, monkeypatch, isolated_state):
    root = _root(tmp_path)
    _interactive(monkeypatch, answer="nope")
    env = _run("approve-ocr", "--backend-config-root", str(root))
    assert env["ok"] is False and env["error"]["code"] == "challenge_mismatch"
    assert not _cfg(root).approval_path.exists()
    assert Ledger(isolated_state).read().rows == []


@pytest.mark.parametrize(
    "argv",
    [("--days", "8"), ("--days", "0"), ("--max-total-usd", "25.01"), ("--max-total-usd", "-1"),
     ("--max-total-usd", "5", "--max-job-usd", "6")],
)
def test_approve_ocr_refuses_an_envelope_or_lifetime_outside_the_policy(tmp_path, monkeypatch, argv):
    root = _root(tmp_path)
    _interactive(monkeypatch)
    env = _run("approve-ocr", "--backend-config-root", str(root), *argv)
    assert env["ok"] is False and env["error"]["code"] == "approval_refused"
    assert env["nextActions"]
    assert not _cfg(root).approval_path.exists()


@pytest.mark.parametrize(
    "egress, code",
    [({}, "nothing-to-approve"), ({"allow_documents": [DOC_SHA], "require_approval": False}, "approval-not-required")],
)
def test_approve_ocr_refuses_an_approval_that_would_mean_nothing(tmp_path, monkeypatch, egress, code):
    root = _root(tmp_path, egress=egress)
    _interactive(monkeypatch)
    env = _run("approve-ocr", "--backend-config-root", str(root))
    assert env["ok"] is False and env["error"]["code"] == code
    assert env["nextActions"]


def test_approve_ocr_with_the_key_file_absent_is_refused_by_path(tmp_path, monkeypatch):
    root = _root(tmp_path)
    (root / "keys" / "vastai.key").unlink()
    _interactive(monkeypatch)
    env = _run("approve-ocr", "--backend-config-root", str(root))
    assert env["ok"] is False and env["error"]["code"] == "key-missing"


# ---------------------------------------------------------------------------
# approve-high (ported)
# ---------------------------------------------------------------------------
def test_approve_high_refuses_without_a_tty(tmp_path, monkeypatch):
    root = _root(tmp_path)
    monkeypatch.setattr(guard, "_is_interactive", lambda: False)
    env = _run("approve-high", "--backend-config-root", str(root), "--hours", "4", "--max-job-usd", "5")
    assert env["ok"] is False and env["nextActions"]
    assert not _cfg(root).high_tier_approval_path.exists()


def test_approve_high_writes_the_ported_approval_beside_the_key(tmp_path, monkeypatch, isolated_state):
    root = _root(tmp_path)
    _interactive(monkeypatch)
    env = _run("approve-high", "--backend-config-root", str(root), "--hours", "4", "--max-job-usd", "5")
    assert env["ok"] is True, env
    cfg = _cfg(root)
    body = verify_high_tier_approval(root, cfg.api_key_path, path=cfg.high_tier_approval_path)
    assert body["max_job_usd"] == 5.0
    rows = Ledger(isolated_state).read().rows
    assert rows[-1]["kind"] == "approval_minted" and rows[-1]["purpose"] == "vastai-high-tier"
    assert "mac" not in rows[-1]


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------
def test_plan_makes_one_read_only_search_and_rents_nothing(tmp_path, monkeypatch):
    root = _root(tmp_path)
    fake = FakeVast()
    _fake_client(monkeypatch, fake)
    pdf = _write_pdf(tmp_path / "doc.pdf", pages=10)
    env = _run("plan", "--backend-config-root", str(root), "--input", str(pdf))
    assert env["ok"] is True, env
    result = env["result"]
    assert len(fake.search_bodies()) == 1
    assert fake.instances == {} and not fake.verbs("PUT") and not fake.verbs("DELETE")
    assert result["rented"] is False
    assert result["plan"]["pages"] == 10 and result["ram_floor_gb"] > 0
    assert result["admitted"] >= 1 and result["offers"][0]["offer_id"] == result["best"]["offer_id"]
    assert result["ttl_s"] > 0 and result["worst_usd"] > 0
    assert set(result["caps"]) >= {"egress", "document_size", "offer", "ttl", "job", "run", "approval", "credit"}
    # no approval yet, and the document is not named: the worker would refuse it
    assert result["verdict"] == "would-refuse" and result["reason_code"] == "approval-missing"
    assert result["egress"]["named_in_allow_documents"] is False


def test_plan_with_the_document_named_and_approved_would_rent(tmp_path, monkeypatch):
    pdf = _write_pdf(tmp_path / "doc.pdf", pages=10)
    root = _root(tmp_path, egress={"allow_documents": [_sha(pdf)]})
    _approve(root)
    _fake_client(monkeypatch, FakeVast())
    env = _run("plan", "--backend-config-root", str(root), "--input", str(pdf))
    result = env["result"]
    assert result["verdict"] == "would-rent", result["caps"]
    assert result["reason_code"] is None
    assert result["caps"]["approval"]["pass"] is True and result["caps"]["egress"]["pass"] is True
    assert all(c["pass"] is not False for c in result["caps"].values())


def test_plan_names_the_cap_that_fails(tmp_path, monkeypatch):
    pdf = _write_pdf(tmp_path / "doc.pdf", pages=10)
    root = _root(tmp_path, egress={"allow_documents": [_sha(pdf)]}, vastai={"max_job_usd": 0.01})
    _approve(root)
    _fake_client(monkeypatch, FakeVast())
    result = _run("plan", "--backend-config-root", str(root), "--input", str(pdf))["result"]
    assert result["verdict"] == "would-refuse" and result["reason_code"] == "cap-job"
    assert result["caps"]["job"]["pass"] is False
    assert "cap-job" in result["best"]["fails"]


def test_plan_json_carries_every_offer_and_every_rejection(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _fake_client(monkeypatch, FakeVast(ignore_filters=True))
    pdf = _write_pdf(tmp_path / "doc.pdf", pages=4)
    result = _run("plan", "--backend-config-root", str(root), "--input", str(pdf), "--json")["result"]
    assert "query" in result
    rejected = {r["offer_id"]: r["reasons"] for r in result["rejected"]}
    assert {14, 15} <= set(rejected), rejected  # the home host and the unverified one
    assert all(rejected[i] for i in rejected)


def test_plan_an_api_error_is_a_named_refusal(tmp_path, monkeypatch):
    root = _root(tmp_path)
    fake = FakeVast()
    fake.search_status = 503
    _fake_client(monkeypatch, fake)
    pdf = _write_pdf(tmp_path / "doc.pdf", pages=2)
    env = _run("plan", "--backend-config-root", str(root), "--input", str(pdf))
    assert env["ok"] is False and env["error"]["code"] == "api-error"
    assert env["nextActions"]


def test_plan_refuses_an_input_that_is_not_there(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _fake_client(monkeypatch, FakeVast())
    env = _run("plan", "--backend-config-root", str(root), "--input", str(tmp_path / "absent.pdf"))
    assert env["ok"] is False and env["error"]["code"] == "bad_input" and env["nextActions"]


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------
def _seed_ledger(state_dir: Path) -> None:
    led = Ledger(state_dir)
    led.append("refused", ts="2026-09-10T00:00:00Z", job_id="JOB-a", sha256=DOC_SHA, bytes=10,
               reason_code="approval-missing")
    led.append("intent", ts="2026-09-18T00:00:00Z", job_id="JOB-b", lease_id="VOCR-1", sha256=DOC_SHA, bytes=10,
               host_id=7011, datacenter=True, verified=True, worst_usd=1.5, approval_nonce="n1")
    led.append("shipped", ts="2026-09-18T00:05:00Z", lease_id="VOCR-1", instance_id=5001,
               start="2026-09-18T00:05:00Z")
    led.append("outcome", ts="2026-09-18T00:30:00Z", lease_id="VOCR-1", instance_id=5001,
               end="2026-09-18T00:30:00Z", estimated_cost_usd=0.4, result="published")


def test_ledger_prints_rows_and_filters_by_job_through_its_leases(tmp_path, isolated_state):
    _seed_ledger(isolated_state)
    env = _run("ledger")
    assert env["ok"] is True and env["result"]["count"] == 4
    assert all(isinstance(line, str) for line in env["result"]["rows"])

    by_job = _run("ledger", "--job-id", "JOB-b", "--json")["result"]
    assert [r["kind"] for r in by_job["rows"]] == ["intent", "shipped", "outcome"]
    assert by_job["spend_usd"] == 0.4 and by_job["unsettled_leases"] == []

    since = _run("ledger", "--since", "2026-09-18T00:04:00Z")["result"]
    assert since["count"] == 2


def test_ledger_reports_a_torn_line(tmp_path, isolated_state):
    _seed_ledger(isolated_state)
    with open(Ledger(isolated_state).path, "ab") as f:
        f.write(b'{"kind": "outcome", "lease_id": "VOCR-2"')
    env = _run("ledger")
    assert env["ok"] is True
    assert env["result"]["torn"] and env["warnings"][0]["code"] == "ledger_torn"


def test_ledger_a_bad_since_is_refused(isolated_state):
    env = _run("ledger", "--since", "yesterday")
    assert env["ok"] is False and env["error"]["code"] == "bad_arguments" and env["nextActions"]


# ---------------------------------------------------------------------------
# reap
# ---------------------------------------------------------------------------
def _orphan(fake: FakeVast, root: Path, *, iid: int = 4242) -> int:
    past = datetime.now(timezone.utc).timestamp() - 3600
    fake.instances[iid] = {"id": iid, "label": make_label(program_fingerprint(root), "VOCR-orphan", past),
                           "actual_status": "running"}
    return iid


def test_reap_dry_run_destroys_and_records_nothing(tmp_path, monkeypatch, isolated_state):
    root = _root(tmp_path)
    fake = FakeVast()
    iid = _orphan(fake, root)
    _fake_client(monkeypatch, fake)
    env = _run("reap", "--ocr", "--backend-config-root", str(root), "--dry-run")
    assert env["ok"] is True, env
    assert [e["action"] for e in env["result"]["entries"]] == ["would_destroy"]
    assert iid in fake.instances and not fake.verbs("DELETE")
    assert Ledger(isolated_state).read().rows == []


def test_reap_destroys_this_roots_orphan_and_records_it(tmp_path, monkeypatch, isolated_state):
    root = _root(tmp_path)
    fake = FakeVast()
    iid = _orphan(fake, root)
    _fake_client(monkeypatch, fake)
    env = _run("reap", "--ocr", "--backend-config-root", str(root))
    assert env["ok"] is True, env
    assert iid not in fake.instances
    assert [r["kind"] for r in Ledger(isolated_state).read().rows] == ["reaped"]


def test_reap_a_failed_destroy_is_loud(tmp_path, monkeypatch):
    root = _root(tmp_path)
    fake = FakeVast()
    iid = _orphan(fake, root)
    fake.sticky.add(iid)
    _fake_client(monkeypatch, fake)
    env = _run("reap", "--ocr", "--backend-config-root", str(root))
    assert env["ok"] is False and env["error"]["code"] == "destroy_failed" and env["nextActions"]


# ---------------------------------------------------------------------------
# lock-deps
# ---------------------------------------------------------------------------
PINS = """\
# pip freeze of the marker environment
marker-pdf==1.10.2
Pillow==10.4.0
torch==2.13.0+cu130
nvidia-cublas-cu13==13.0.0.19
triton==3.5.0
"""


def _pypi(files_per_release: int = 2, *, answer_for: dict | None = None, seen: list | None = None):
    def http(url: str) -> bytes:
        if seen is not None:
            seen.append(url)
        m = re.match(r"https://pypi\.org/pypi/([^/]+)/([^/]+)/json$", url)
        assert m, url
        name, version = m.groups()
        info = (answer_for or {}).get(name, {"name": name, "version": version})
        digests = [f"{name}-{version}-{i}".encode().hex().ljust(64, "0")[:64] for i in range(files_per_release)]
        return json.dumps({
            "info": info,
            "urls": [{"filename": f"{name}-{version}-{i}.whl", "digests": {"sha256": d}}
                     for i, d in enumerate(digests)],
        }).encode()
    return http


def test_lock_deps_writes_every_files_hash_and_drops_the_image_stack(tmp_path, monkeypatch):
    root = _root(tmp_path)
    pins = tmp_path / "pins.txt"
    pins.write_text(PINS, encoding="utf-8")
    seen: list[str] = []
    monkeypatch.setattr(cli_vastai, "_lock_http", _pypi(3, seen=seen))
    env = _run("lock-deps", "--backend-config-root", str(root), "--pins", str(pins))
    assert env["ok"] is True, env
    lock = root / "marker-requirements.lock"  # the configured default, under the root
    assert Path(env["result"]["lock"]) == lock
    text = lock.read_text(encoding="utf-8")
    assert "marker-pdf==1.10.2 \\\n    --hash=sha256:" in text and "Pillow==10.4.0 \\" in text
    assert text.count("--hash=sha256:") == 6 == env["result"]["hashes"]
    assert "torch==" not in text.split("\n", 3)[3] and "triton==3.5.0 \\" not in text and "nvidia-cublas" not in \
        text.split("\n", 3)[3]
    assert sorted(env["result"]["dropped"]) == ["nvidia-cublas-cu13==13.0.0.19", "torch==2.13.0+cu130",
                                                "triton==3.5.0"]
    assert seen == ["https://pypi.org/pypi/marker-pdf/1.10.2/json", "https://pypi.org/pypi/pillow/10.4.0/json"]


@pytest.mark.parametrize("line", ["-e git+https://example.invalid/x.git#egg=x", "x @ file:///tmp/x.whl",
                                  "requests>=2", "requests==2.0 ; python_version < '3.9'"])
def test_lock_deps_refuses_a_line_that_is_not_name_eq_version(tmp_path, monkeypatch, line):
    pins = tmp_path / "pins.txt"
    pins.write_text(f"marker-pdf==1.10.2\n{line}\n", encoding="utf-8")
    monkeypatch.setattr(cli_vastai, "_lock_http", _pypi())
    out = tmp_path / "out.lock"
    env = _run("lock-deps", "--pins", str(pins), "--out", str(out))
    assert env["ok"] is False and env["error"]["code"] == "lock_refused"
    assert "pins.txt:2" in env["error"]["message"] and env["nextActions"]
    assert not out.exists()


def test_lock_deps_refuses_a_pypi_answer_for_another_release(tmp_path, monkeypatch):
    pins = tmp_path / "pins.txt"
    pins.write_text("marker-pdf==1.10.2\n", encoding="utf-8")
    monkeypatch.setattr(cli_vastai, "_lock_http",
                        _pypi(answer_for={"marker-pdf": {"name": "marker-pdf", "version": "1.10.1"}}))
    env = _run("lock-deps", "--pins", str(pins), "--out", str(tmp_path / "out.lock"))
    assert env["ok"] is False and "1.10.1" in env["error"]["message"]


def test_lock_deps_refuses_a_release_without_hashes(tmp_path, monkeypatch):
    pins = tmp_path / "pins.txt"
    pins.write_text("marker-pdf==1.10.2\n", encoding="utf-8")
    monkeypatch.setattr(cli_vastai, "_lock_http", _pypi(0))
    env = _run("lock-deps", "--pins", str(pins), "--out", str(tmp_path / "out.lock"))
    assert env["ok"] is False and "no file with a sha256" in env["error"]["message"]


def test_lock_deps_with_no_pins_file_names_the_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(envlock, "PACKAGED_PINS", tmp_path / "absent.pins.txt")
    env = _run("lock-deps", "--out", str(tmp_path / "out.lock"))
    assert env["ok"] is False and env["error"]["code"] == "pins_missing"
    assert any("--pins" in (a.get("description") or "") for a in env["nextActions"])
