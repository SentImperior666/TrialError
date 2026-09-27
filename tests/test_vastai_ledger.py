"""vast.ai OCR executor, lane L1: the run ledger (design section 9.5).
Append-only JSONL, fsynced per row, one inter-process lock, torn lines
reported, and the spend views the caps read.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import trialerror
from tests._vastai_fakes import isolated_state, network_tripwire  # noqa: F401 - fixtures
from trialerror.offload.lock import worker_state_dir
from trialerror.vastai import ledger as ledger_mod
from trialerror.vastai.ledger import (
    LEDGER_KINDS,
    REQUIRED_FIELDS,
    Ledger,
    LedgerError,
    lease_spend,
    ledger_path,
    spend_view,
    spent_in_run,
    spent_under_approval,
    unsettled_leases,
)

SHA = "c" * 64
INTENT = {"sha256": SHA, "bytes": 10, "host_id": 7, "datacenter": True, "verified": True}


@pytest.fixture(autouse=True)
def _guards(network_tripwire, isolated_state):  # noqa: F811
    yield


def _valid(kind):
    return {
        "refused": {"sha256": SHA, "bytes": 10, "reason_code": "tier-not-allowed"},
        "intent": {**INTENT, "lease_id": "VOCR-1", "worst_usd": 1.0},
        "shipped": {"lease_id": "VOCR-1", "instance_id": 5001, "start": "2026-09-19T12:00:00Z"},
        "outcome": {"lease_id": "VOCR-1", "instance_id": 5001, "end": "2026-09-19T13:00:00Z",
                    "estimated_cost_usd": 0.4, "result": "published"},
        "approval_minted": {"nonce": "n1"},
        "reaped": {"instance_id": 5001, "reason": "owner_dead"},
        "destroy_failed": {"instance_id": 5001},
    }[kind]


def test_the_ledger_lives_in_the_worker_state_dir():
    assert ledger_path() == worker_state_dir() / "vastai" / "ledger.jsonl"
    assert Ledger().path == ledger_path()


def test_every_kind_appends_one_json_line_with_kind_and_ts(isolated_state):  # noqa: F811
    ledger = Ledger(isolated_state)
    for kind in LEDGER_KINDS:
        row = ledger.append(kind, **_valid(kind))
        assert row["kind"] == kind and row["ts"].endswith("Z")
    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(LEDGER_KINDS)
    assert [json.loads(line)["kind"] for line in lines] == list(LEDGER_KINDS)
    read = ledger.read()
    assert read.ok and len(read.rows) == len(LEDGER_KINDS)


def test_each_row_is_flushed_and_fsynced(isolated_state, monkeypatch):  # noqa: F811
    synced = []
    real = os.fsync
    monkeypatch.setattr(ledger_mod.os, "fsync", lambda fd: (synced.append(fd), real(fd)))
    ledger = Ledger(isolated_state)
    ledger.append("approval_minted", nonce="a")
    ledger.append("approval_minted", nonce="b")
    assert len(synced) == 2


@pytest.mark.parametrize("kind", LEDGER_KINDS)
def test_the_charter_fields_are_required_per_kind(isolated_state, kind):  # noqa: F811
    ledger = Ledger(isolated_state)
    for field in REQUIRED_FIELDS[kind]:
        fields = dict(_valid(kind))
        del fields[field]
        with pytest.raises(LedgerError, match=field):
            ledger.append(kind, **fields)
    assert not ledger.path.exists()  # a refused row writes nothing


def test_malformed_rows_are_refused(isolated_state):  # noqa: F811
    ledger = Ledger(isolated_state)
    with pytest.raises(LedgerError, match="not one of"):
        ledger.append("shipment", **_valid("shipped"))
    with pytest.raises(LedgerError, match="sha256"):
        ledger.append("refused", **{**_valid("refused"), "sha256": "ABC"})
    with pytest.raises(LedgerError, match="reason code"):
        ledger.append("refused", **{**_valid("refused"), "reason_code": "because"})
    with pytest.raises(LedgerError, match="datacenter"):
        ledger.append("intent", **{**_valid("intent"), "datacenter": "yes"})
    with pytest.raises(LedgerError, match="instance_id may be null only"):
        ledger.append("outcome", **{**_valid("outcome"), "instance_id": None})
    with pytest.raises(LedgerError, match="finite"):
        ledger.append("outcome", **{**_valid("outcome"), "estimated_cost_usd": float("nan")})
    ledger.append("outcome", **{**_valid("outcome"), "instance_id": None, "result": "not_created"})
    with pytest.raises(LedgerError, match="never goes into the ledger"):
        ledger.append("approval_minted", nonce="n", mac="0" * 64)


def test_a_torn_last_line_is_reported_never_dropped(isolated_state):  # noqa: F811
    ledger = Ledger(isolated_state)
    ledger.append("approval_minted", nonce="a")
    with open(ledger.path, "ab") as f:
        f.write(b'{"kind": "intent", "ts": "2026-09-19T12:00:00Z", "lease_id": "VOC')  # crash mid-write
    read = ledger.read()
    assert len(read.rows) == 1 and len(read.torn) == 1 and read.torn[0].line_no == 2 and not read.ok
    ledger.append("approval_minted", nonce="b")  # starts on a fresh line
    read = ledger.read()
    assert [r["nonce"] for r in read.rows] == ["a", "b"]
    assert len(read.torn) == 1 and read.torn[0].line_no == 2 and "VOC" in read.torn[0].text


def test_a_complete_row_missing_only_its_newline_is_still_reported(isolated_state):  # noqa: F811
    ledger = Ledger(isolated_state)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_bytes(b'{"kind": "approval_minted", "ts": "t", "nonce": "a"}')
    read = ledger.read()
    assert read.rows == [] and "no newline" in read.torn[0].error


def test_a_garbage_line_in_the_middle_is_reported(isolated_state):  # noqa: F811
    ledger = Ledger(isolated_state)
    ledger.append("approval_minted", nonce="a")
    with open(ledger.path, "ab") as f:
        f.write(b"[1, 2, 3]\n")
    ledger.append("approval_minted", nonce="b")
    read = ledger.read()
    assert [r["nonce"] for r in read.rows] == ["a", "b"] and read.torn[0].line_no == 2


def test_concurrent_appends_from_threads_never_interleave(isolated_state):  # noqa: F811
    ledger = Ledger(isolated_state)

    def worker(n):
        for i in range(40):
            Ledger(isolated_state).append("approval_minted", nonce=f"t{n}-{i}", pad="x" * 500)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    read = ledger.read()
    assert read.ok and len(read.rows) == 160 and len({r["nonce"] for r in read.rows}) == 160


def test_concurrent_appends_from_processes_never_interleave(isolated_state):  # noqa: F811
    worktree = Path(trialerror.__file__).resolve().parents[1]
    script = (
        "import sys\n"
        "from trialerror.vastai.ledger import Ledger\n"
        "for i in range(30):\n"
        "    Ledger(sys.argv[1]).append('approval_minted', nonce=f'{sys.argv[2]}-{i}', pad='y' * 800)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(worktree)}
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(isolated_state), f"p{n}"], env=env, cwd=str(worktree))
        for n in range(2)
    ]
    assert [p.wait(timeout=120) for p in procs] == [0, 0]
    read = Ledger(isolated_state).read()
    assert read.ok and len(read.rows) == 60


def test_spend_counts_settled_estimates_and_unsettled_worst_cases():
    rows = [
        {"kind": "intent", "lease_id": "L1", "approval_nonce": "N", "worker_run_id": "R1", "worst_usd": 1.5},
        {"kind": "outcome", "lease_id": "L1", "estimated_cost_usd": 0.4},
        {"kind": "intent", "lease_id": "L2", "approval_nonce": "N", "worker_run_id": "R2", "worst_usd": 2.0},
        {"kind": "intent", "lease_id": "L3", "approval_nonce": "M", "worker_run_id": "R2", "worst_usd": 3.0},
        {"kind": "outcome", "lease_id": "L3", "estimated_cost_usd": 0.0, "result": "not_created"},
        {"kind": "outcome", "lease_id": "L9", "estimated_cost_usd": 9.0},  # no intent: not attributable
        {"kind": "refused", "sha256": SHA, "bytes": 1, "reason_code": "cap-job"},
    ]
    spend = lease_spend(rows)
    assert set(spend) == {"L1", "L2", "L3"} and spend["L1"].settled and not spend["L2"].settled
    assert spent_under_approval(rows, "N") == pytest.approx(2.4)
    assert spent_under_approval(rows, None) == 0.0
    assert spent_in_run(rows, "R2") == pytest.approx(2.0)
    assert unsettled_leases(rows) == ["L2"]
    view = spend_view(rows, approval_nonce="M", worker_run_id="R1")
    assert (view.approval_usd, view.run_usd, view.unsettled) == (0.0, 0.4, ("L2",))
