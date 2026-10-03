"""L10 part D1: the container-to-host outbox's harness side. ``push``/
``remind`` queue a file instead of failing with ``no_notify_cmd`` when
``[packet] outbox = true`` and no ``notify_cmd`` is set; a receipt (written
by the host job, simulated here as a plain file drop) is reconciled at the
start of every packet verb; queued is never sent."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from trialerror.packet import build as pb
from trialerror.packet import outbox as ob
from trialerror.packet import store as ps
from trialerror.packet.store import PacketError, packet_settings

T0 = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)


def _settings(tmp_path, **packet_table):
    root = tmp_path / "prog"
    root.mkdir()
    table = {"outbox": True, **packet_table}
    body = "\n".join(f'{k} = {json.dumps(v)}' for k, v in table.items())
    (root / "trialerror.toml").write_text(f'[program]\nid = "demo"\n[packet]\n{body}\n', encoding="utf-8")
    return packet_settings(root)


def _add_and_build(settings, what="Q?", **kw):
    ps.add_item(
        settings,
        {
            "what": what, "why": "It matters.",
            "options": [{"key": "a", "label": "Yes", "consequence": "Ok."},
                        {"key": "b", "label": "No", "consequence": "Not ok."}],
            "recommended": "a", "if_undecided": "Nothing.", "needed_by": "next-session",
        },
        now=T0,
    )
    return pb.build_packet(settings, "manual", now=T0, **kw)


def _receipt(settings, entry_id, *, delivered, exit_code, ts=None):
    ob._write_atomic_json(
        settings.outbox_receipts / f"{entry_id}.json",
        {"id": entry_id, "exit_code": exit_code, "delivered": delivered, "ts": ts or ps.utc_iso(T0)},
    )


# --------------------------------------------------------------- queueing


def test_push_queues_instead_of_failing_with_no_notify_cmd(tmp_path):
    settings = _settings(tmp_path)
    _add_and_build(settings)
    result = pb.push_packet(settings, now=T0)
    assert result["queued"] is True and result["outbox_id"]
    files = list(settings.outbox_dir.glob("*.json"))
    assert len(files) == 1
    entry = json.loads(files[0].read_text(encoding="utf-8"))
    assert entry["kind"] == "push" and entry["id"] == result["outbox_id"]
    assert entry["title"] == result["title"] and entry["body"] == result["body"]
    # nothing is "sent" yet -- queued is not sent (design §6 trap 3)
    assert ps.read_jsonl(settings.sent) == []


def test_without_outbox_a_missing_notify_cmd_still_refuses(tmp_path):
    root = tmp_path / "prog"
    root.mkdir()
    (root / "trialerror.toml").write_text('[program]\nid = "demo"\n', encoding="utf-8")
    settings = packet_settings(root)
    _add_and_build(settings)
    with pytest.raises(PacketError) as exc:
        pb.push_packet(settings, now=T0)
    assert exc.value.code == "no_notify_cmd"


def test_the_queued_bodys_carries_counts_and_the_link_only(tmp_path):
    settings = _settings(tmp_path, link="https://example.invalid/packet")
    _add_and_build(settings, what="Some very specific decision text")
    result = pb.push_packet(settings, now=T0)
    assert "Some very specific decision text" not in result["body"]
    assert result["body"] == "1 decision needs you before the next session (about 3 minutes). Read: https://example.invalid/packet."


# ---------------------------------------------------------- receipt reconciliation


def test_a_delivered_receipt_appends_sent_jsonl_once(tmp_path):
    settings = _settings(tmp_path)
    _add_and_build(settings)
    result = pb.push_packet(settings, now=T0)
    _receipt(settings, result["outbox_id"], delivered=True, exit_code=0)
    r1 = ob.reconcile_receipts(settings, now=T0)
    assert r1["delivered"] == [result["outbox_id"]]
    sent = ps.read_jsonl(settings.sent)
    assert len(sent) == 1 and sent[0]["outbox_id"] == result["outbox_id"] and sent[0]["reminder"] is False
    # reconciling again must not append a second row for the same id
    r2 = ob.reconcile_receipts(settings, now=T0)
    assert r2["delivered"] == []
    assert len(ps.read_jsonl(settings.sent)) == 1


def test_a_failed_receipt_never_appends_sent_and_is_reported(tmp_path):
    settings = _settings(tmp_path)
    _add_and_build(settings)
    result = pb.push_packet(settings, now=T0)
    _receipt(settings, result["outbox_id"], delivered=False, exit_code=2)
    ob.reconcile_receipts(settings, now=T0)
    assert ps.read_jsonl(settings.sent) == []
    status = ob.notification_status(settings, now=T0)
    assert status == "the last notification was not sent: no channel is configured on the host"


# --------------------------------------------------------------- the 24h limit


def test_a_pending_entry_counts_for_the_24h_limit_and_one_push_per_packet(tmp_path):
    settings = _settings(tmp_path)
    _add_and_build(settings)
    first = pb.push_packet(settings, now=T0)
    with pytest.raises(PacketError) as exc:
        pb.push_packet(settings, now=T0 + timedelta(minutes=1))
    assert exc.value.code == "already_pushed"
    # a DIFFERENT packet, still within 24h of the pending (undelivered) one
    _add_and_build(settings, what="Second?", dry_run=False)
    with pytest.raises(PacketError) as exc2:
        pb.push_packet(settings, now=T0 + timedelta(hours=2))
    assert exc2.value.code == "push_limit_24h"
    # a failed entry does NOT count
    _receipt(settings, first["outbox_id"], delivered=False, exit_code=1)
    ob.reconcile_receipts(settings, now=T0 + timedelta(hours=2))
    second = pb.push_packet(settings, now=T0 + timedelta(hours=2))
    assert second["queued"] is True


def test_force_still_bypasses_the_limits_with_the_outbox(tmp_path):
    settings = _settings(tmp_path)
    _add_and_build(settings)
    pb.push_packet(settings, now=T0)
    again = pb.push_packet(settings, now=T0 + timedelta(minutes=1), force=True)
    assert again["queued"] is True


# ------------------------------------------------------------------- remind


def test_remind_through_the_outbox(tmp_path):
    settings = _settings(tmp_path)
    _add_and_build(settings)
    pushed = pb.push_packet(settings, now=T0)
    _receipt(settings, pushed["outbox_id"], delivered=True, exit_code=0)
    due = pb.remind(settings, now=T0 + timedelta(days=settings.remind_after_days))
    assert due["reminded"] is True and due["queued"] is True
    entries = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in settings.outbox_dir.glob("*.json")}
    reminder_entries = [e for e in entries.values() if e["kind"] == "reminder"]
    assert len(reminder_entries) == 1
    # a second call before the first reminder is delivered does not queue a duplicate
    again = pb.remind(settings, now=T0 + timedelta(days=settings.remind_after_days, hours=1))
    assert again["reminded"] is False and "already queued" in again["reason"]


# ------------------------------------------------------------- missing receipt


def test_a_stuck_queued_entry_is_reported_after_15_minutes(tmp_path):
    settings = _settings(tmp_path)
    _add_and_build(settings)
    pb.push_packet(settings, now=T0)
    assert ob.notification_status(settings, now=T0 + timedelta(minutes=5)) is None
    late = ob.notification_status(settings, now=T0 + timedelta(minutes=16))
    assert late is not None
    assert "has not been sent yet" in late and "the host job may not be running" in late


# --------------------------------------------------------------- packet list

def test_packet_list_surfaces_the_notification_status(tmp_path):
    settings = _settings(tmp_path)
    _add_and_build(settings)
    result = pb.push_packet(settings, now=T0)
    _receipt(settings, result["outbox_id"], delivered=False, exit_code=1)
    listed = ps.list_items(settings, open_only=False, now=T0)
    assert listed["notification"] == "the last notification was not sent: the push failed"


# --------------------------------------------------------- B-2: the weekly cooldown


def test_a_delivered_outbox_push_starts_the_weekly_cooldown(tmp_path):
    """Review B-2 / probe_weekly.py: a packet delivered through the outbox must carry
    its trigger into sent.jsonl, so the weekly-limit trigger's 5-day cooldown sees it --
    without this fix the hourly weekly job re-announces about once a day instead."""
    settings = _settings(tmp_path)
    ps.add_item(
        settings,
        {
            "what": "Q?", "why": "It unblocks the next step.",
            "options": [{"key": "a", "label": "Go", "consequence": "It runs."},
                        {"key": "b", "label": "Hold", "consequence": "It waits."}],
            "recommended": "a", "if_undecided": "It waits.", "needed_by": "next-session",
        },
        now=T0,
    )
    quota_dir = tmp_path / "quota"

    def capture(now, pct=90):
        quota_dir.mkdir(exist_ok=True)
        (quota_dir / "latest.json").write_text(json.dumps({
            "epoch": now.timestamp(), "captured_ts": "x",
            "rate_limits": {"seven_day": {"used_percentage": pct, "resets_at": now.timestamp() + 5 * 86400}},
        }), encoding="utf-8")

    def weekly(now):
        capture(now)
        return pb.build_packet(settings, "weekly_limit", now=now, when_weekly_pct=85, quota_dir=str(quota_dir))

    first = weekly(T0)
    assert first.get("built")
    pushed = pb.push_packet(settings, packet_id=first["packet"]["packet_id"], now=T0)
    assert pushed["queued"] is True
    _receipt(settings, pushed["outbox_id"], delivered=True, exit_code=0, ts=ps.utc_iso(T0 + timedelta(minutes=4)))
    ob.reconcile_receipts(settings, now=T0 + timedelta(minutes=5))
    sent = ps.read_jsonl(settings.sent)
    assert len(sent) == 1 and sent[0]["trigger"] == "weekly_limit"

    at_1h = weekly(T0 + timedelta(hours=1))
    assert not at_1h.get("built") and "already announced" in at_1h.get("skipped", "")
    at_25h = weekly(T0 + timedelta(hours=25))
    assert not at_25h.get("built") and "already announced" in at_25h.get("skipped", "")


# ----------------------------------------------------------- S-3: the status wording


def test_notification_status_ignores_an_old_failure_once_a_later_entry_delivered(tmp_path):
    """Review S-3 / probe Q1: a failure on day 0, then a delivery on day 2, must not
    still say "the last notification was not sent" -- that must track the most RECENT
    entry (by when it was queued), not the most recent failure among never-pruned
    receipts."""
    settings = _settings(tmp_path)
    e1 = ob.queue_notification(settings, kind="push", packet_id="PACKET_A", title="t", body="b", now=T0)
    e2 = ob.queue_notification(
        settings, kind="push", packet_id="PACKET_B", title="t", body="b", now=T0 + timedelta(days=2)
    )
    _receipt(settings, e1["id"], delivered=False, exit_code=1, ts=ps.utc_iso(T0))
    _receipt(settings, e2["id"], delivered=True, exit_code=0, ts=ps.utc_iso(T0 + timedelta(days=2, minutes=1)))
    ob.reconcile_receipts(settings, now=T0 + timedelta(days=2, minutes=5))
    assert ob.notification_status(settings, now=T0 + timedelta(days=2, minutes=30)) is None


# --------------------------------------------------------------------------- N-7


def test_a_non_ascii_packet_id_still_gets_an_ascii_safe_filename(tmp_path):
    """Review N-7: the host's NAME_RE is ASCII-only; a Unicode packet id must not mint
    a filename the host job never sweeps."""
    settings = _settings(tmp_path)
    entry = ob.queue_notification(settings, kind="push", packet_id="PAKET_é", title="t", body="b", now=T0)
    allowed = set("0123456789TZ_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-")
    assert all(c in allowed for c in entry["id"])
    assert (settings.outbox_dir / f"{entry['id']}.json").is_file()


# ------------------------------------------------------------ L9 §3 E6: kind "alert"


def test_e6_queue_notification_accepts_kind_alert(tmp_path):
    settings = _settings(tmp_path)
    entry = ob.queue_notification(
        settings, kind="alert", packet_id="credit-risk-2026-09", title="t", body="b", now=T0,
    )
    assert entry["kind"] == "alert"
    assert entry["id"].endswith("_credit-risk-2026-09_alert")


def test_e6_a_delivered_alert_writes_no_sent_jsonl_row(tmp_path):
    settings = _settings(tmp_path)
    entry = ob.queue_notification(
        settings, kind="alert", packet_id="credit-risk-2026-09", title="t", body="b", now=T0,
    )
    _receipt(settings, entry["id"], delivered=True, exit_code=0)
    result = ob.reconcile_receipts(settings, now=T0)
    assert result["delivered"] == [entry["id"]]
    assert ps.read_jsonl(settings.sent) == []


def test_e6_a_failed_alert_is_reported_in_result_but_never_written_to_sent(tmp_path):
    settings = _settings(tmp_path)
    entry = ob.queue_notification(
        settings, kind="alert", packet_id="credit-risk-2026-09", title="t", body="b", now=T0,
    )
    _receipt(settings, entry["id"], delivered=False, exit_code=4)
    result = ob.reconcile_receipts(settings, now=T0)
    assert result["failed"] == [entry["id"]]
    assert ps.read_jsonl(settings.sent) == []


def test_e6_a_delivered_alert_does_not_refuse_the_next_packet_push(tmp_path):
    """A pending or delivered alert must never count toward the packet's own
    24h/one-push-per-packet limits -- push_packet counts sent.jsonl rows and
    pending "push"-kind entries only."""
    settings = _settings(tmp_path)
    ob.queue_notification(settings, kind="alert", packet_id="credit-risk-2026-09", title="t", body="b", now=T0)
    _add_and_build(settings)
    result = pb.push_packet(settings, now=T0)
    assert result["queued"] is True


def test_n_c_reconcile_reports_each_receipts_own_ts_and_exit_code(tmp_path):
    """Review fix check N-c: check_credit_risk needs the RECEIPT's own ts
    for a trigger's sent_ts (not the run's clock), and a failure's exit
    code for the trigger's own detail -- both now travel in a "receipts"
    map, additive alongside the existing delivered/failed id lists."""
    settings = _settings(tmp_path)
    delivered_entry = ob.queue_notification(settings, kind="alert", packet_id="a", title="t", body="b", now=T0)
    failed_entry = ob.queue_notification(settings, kind="alert", packet_id="b", title="t", body="b", now=T0)
    _receipt(settings, delivered_entry["id"], delivered=True, exit_code=0, ts=ps.utc_iso(T0 + timedelta(minutes=3)))
    _receipt(settings, failed_entry["id"], delivered=False, exit_code=4, ts=ps.utc_iso(T0 + timedelta(minutes=4)))

    result = ob.reconcile_receipts(settings, now=T0 + timedelta(minutes=5))

    assert result["receipts"][delivered_entry["id"]]["exit_code"] == 0
    assert result["receipts"][delivered_entry["id"]]["ts"] == ps.utc_iso(T0 + timedelta(minutes=3))
    assert result["receipts"][failed_entry["id"]]["exit_code"] == 4
    assert result["receipts"][failed_entry["id"]]["ts"] == ps.utc_iso(T0 + timedelta(minutes=4))


def test_e6_a_pending_alert_does_not_hold_back_a_packet_push(tmp_path):
    settings = _settings(tmp_path)
    ob.queue_notification(
        settings, kind="alert", packet_id="credit-risk-2026-09", title="t", body="b",
        now=T0 + timedelta(minutes=1),
    )
    _add_and_build(settings)
    result = pb.push_packet(settings, now=T0 + timedelta(minutes=2))
    assert result["queued"] is True
