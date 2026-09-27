"""``trialerror archive audit``: one test per check, plus the report and cadence.
Synthetic files in temporary folders only."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trialerror.archive import audit, store
from trialerror.archive.store import open_index, run_archive
from trialerror.cli import main

T0 = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
OLD = 1_700_000_000  # a file mtime long before every run in these tests


def put(path: Path, data: bytes, mtime: int = OLD) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def settings(tmp_path: Path, days=3650, **extra) -> Path:
    body = dict(extra)
    if days is not None:
        body["cleanupPeriodDays"] = days
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


@pytest.fixture()
def arc(tmp_path, monkeypatch):
    """A source with two transcripts, archived twice, and a good settings file."""
    src = tmp_path / "projects"
    put(src / "proj" / "s1.jsonl", b'{"version":"2.1.0","timestamp":"2026-01-01T00:00:00Z"}\n')
    put(src / "proj" / "s2.jsonl", b'{"timestamp":"2026-01-02T00:00:00Z"}\n')
    dest = tmp_path / "arc"
    settings(tmp_path)
    run_archive(src, dest, "hx", now=T0)
    run_archive(src, dest, "hx", now=T0 + timedelta(hours=2))
    real = audit.shutil.disk_usage
    monkeypatch.setattr(audit.shutil, "disk_usage", lambda p: real(p)._replace(free=500 * 1000 ** 3))
    return src, dest, tmp_path


def do_audit(arc_, hours=3, **kw):
    src, dest, _ = arc_
    kw.setdefault("src", src)
    kw.setdefault("rng", random.Random(1))
    return audit.run_audit(dest, "hx", now=T0 + timedelta(hours=hours), **kw)


def keep_running(src, dest, first_hour, last_hour, step=2):
    """The scheduler's runs, every two hours, so a later audit sees a regular archive."""
    for h in range(first_hour, last_hour + 1, step):
        run_archive(src, dest, "hx", now=T0 + timedelta(hours=h))


def status_of(report, n):
    return next(c for c in report["checks"] if c["n"] == n)


def test_a_healthy_archive_audits_clean_and_files_the_report(arc):
    _, dest, _ = arc
    report = do_audit(arc)
    assert [c["status"] for c in report["checks"] if c["status"] == "fail"] == []
    assert report["clean"] is True and report["headline"] == "clean"
    md = Path(report["report_md"]).read_text(encoding="utf-8")
    assert md.splitlines()[2] == "clean"
    assert Path(report["report_md"]).name == "AUDIT_hx_2026-01-10.md"
    assert json.loads(Path(report["report_json"]).read_text(encoding="utf-8"))["clean"] is True
    assert len(report["checks"]) == 8


# 1 · runs
def test_runs_fail_when_the_archive_stops_running(arc):
    report = do_audit(arc, hours=12)  # last run was T0+2h: ten hours of silence
    check = status_of(report, 1)
    assert check["status"] == "fail" and "without running" in check["sentence"]
    assert report["clean"] is False and report["headline"].startswith("1 problem, the first is ")


def test_runs_fail_when_it_has_never_run(tmp_path):
    dest = tmp_path / "arc"
    open_index(dest).close()
    report = audit.run_audit(dest, "hx", now=T0, settings_file=settings(tmp_path))
    assert status_of(report, 1)["status"] == "fail"


def test_runs_fail_on_a_hole_in_the_middle(tmp_path):
    src = tmp_path / "projects"
    put(src / "p" / "a.jsonl", b"x\n")
    dest = tmp_path / "arc"
    for hours in (0, 2, 12, 14):
        run_archive(src, dest, "hx", now=T0 + timedelta(hours=hours))
    report = audit.run_audit(dest, "hx", now=T0 + timedelta(hours=15), settings_file=settings(tmp_path))
    check = status_of(report, 1)
    assert check["status"] == "fail" and check["details"]["gaps"] == 1


# 2 · coverage
def test_coverage_fails_when_a_file_changed_without_a_run(arc):
    src, _, _ = arc
    put(src / "proj" / "s1.jsonl", b"changed while nothing ran\n", OLD + 500)
    put(src / "proj" / "s3.jsonl", b"brand new, never archived\n", OLD + 600)
    check = status_of(do_audit(arc), 2)
    assert check["status"] == "fail"
    assert check["details"]["missing_total"] == 2


def test_coverage_expects_files_written_after_the_last_run_started(arc):
    src, _, _ = arc
    late = int((T0 + timedelta(hours=2, minutes=30)).timestamp())
    put(src / "proj" / "s1.jsonl", b"a live session kept writing\n", late)
    check = status_of(do_audit(arc), 2)
    assert check["status"] == "pass" and check["details"]["newer"] == 1


def test_coverage_is_a_warn_without_a_source(arc):
    _, dest, tmp = arc
    report = audit.run_audit(dest, "hx", now=T0 + timedelta(hours=3), settings_file=tmp / "settings.json")
    assert status_of(report, 2)["status"] == "warn"


# 3 · missing transcripts
def test_missing_transcripts_are_counted_by_project_and_version_and_are_only_a_warn(tmp_path):
    src = tmp_path / "projects"
    put(src / "proj" / "sess1.jsonl", b'{"version":"9.9.9"}\n')
    put(src / "proj" / "sess1" / "subagents" / "agent-a.jsonl", b"x\n")
    put(src / "proj" / "sess1" / "subagents" / "agent-a.meta.json", b"{}")
    put(src / "proj" / "sess1" / "subagents" / "agent-b.meta.json", b"{}")  # no transcript
    put(src / "proj" / "sess1" / "subagents" / "agent-c.meta.json", b"{}")  # no transcript
    put(src / "other" / "sess2" / "subagents" / "agent-d.meta.json", b"{}")  # no main file either
    dest = tmp_path / "arc"
    run_archive(src, dest, "hx", now=T0)
    report = audit.run_audit(
        dest, "hx", src=src, now=T0 + timedelta(hours=1), settings_file=settings(tmp_path), rng=random.Random(1)
    )
    check = status_of(report, 3)
    assert check["status"] == "warn" and check["details"]["missing_total"] == 3
    assert check["details"]["by_project"] == {"proj": {"9.9.9": 2}, "other": {"unknown": 1}}
    assert "cannot copy what was never written" in check["sentence"]
    assert report["clean"] is True  # a warn is never a fail
    # a second audit reports the trend
    put(src / "proj" / "sess1" / "subagents" / "agent-e.meta.json", b"{}", OLD + 1)
    keep_running(src, dest, 2, 24)
    again = audit.run_audit(
        dest, "hx", src=src, now=T0 + timedelta(hours=25), settings_file=settings(tmp_path), rng=random.Random(1)
    )
    assert "up 1 since the last audit" in status_of(again, 3)["sentence"]


# 4 · known gaps
def test_gap_entry_without_and_with_an_explanation(arc, tmp_path):
    gaps = tmp_path / "gaps.json"
    gaps.write_text(
        json.dumps([{"id": "G1", "host": "hx", "what": "no transcript for a week", "window": "2026-01-01..2026-01-07",
                     "opened_ts": "2026-01-05T00:00:00Z", "explanation": "", "closed_ts": None}]),
        encoding="utf-8",
    )
    open_check = status_of(do_audit(arc, gap_file=gaps), 4)
    assert open_check["status"] == "warn" and "G1" in open_check["sentence"] and "open 5 days" in open_check["sentence"]
    gaps.write_text(
        json.dumps([{"id": "G1", "host": "hx", "what": "x", "window": "", "opened_ts": "2026-01-05T00:00:00Z",
                     "explanation": "the machine was off", "closed_ts": "2026-01-09T00:00:00Z"},
                    {"id": "G2", "host": "other-host", "what": "not mine", "explanation": ""}]),
        encoding="utf-8",
    )
    assert status_of(do_audit(arc, gap_file=gaps), 4)["status"] == "pass"


def test_gap_probe_reports_timestamps_only(arc, tmp_path):
    gaps = tmp_path / "gaps.json"
    gaps.write_text(
        json.dumps([{"id": "G1", "host": "hx", "what": "quiet week", "project": "proj",
                     "window": "2026-01-01..2026-01-01", "opened_ts": "2026-01-05T00:00:00Z", "explanation": ""}]),
        encoding="utf-8",
    )
    check = status_of(do_audit(arc, gap_file=gaps), 4)
    evidence = check["details"]["evidence"]["G1"]
    assert evidence["files_checked"] == 2
    found = evidence["files_with_entries_in_window"]
    assert [f["rel_path"] for f in found] == ["proj/s1.jsonl"]
    assert found[0]["in_window"] == 1 and found[0]["first"] == "2026-01-01T00:00:00Z"
    assert set(found[0]) == {"host", "rel_path", "first", "last", "in_window"}  # no message text


def test_an_unreadable_gap_file_is_a_fail(arc, tmp_path):
    gaps = tmp_path / "gaps.json"
    gaps.write_text("{not json", encoding="utf-8")
    assert status_of(do_audit(arc, gap_file=gaps), 4)["status"] == "fail"


# 5 · integrity
def test_integrity_fails_on_a_corrupted_object(arc):
    _, dest, _ = arc
    data = b'{"timestamp":"2026-01-02T00:00:00Z"}\n'
    obj = dest / "objects" / sha(data)[:2] / f"{sha(data)}.gz"
    obj.write_bytes(gzip.compress(b"tampered"))
    check = status_of(do_audit(arc, sample=50), 5)
    assert check["status"] == "fail" and check["details"]["corrupted"] == [sha(data)]


def test_integrity_fails_on_a_missing_object_file_unless_it_was_pruned(arc):
    _, dest, _ = arc
    data = b'{"timestamp":"2026-01-02T00:00:00Z"}\n'
    (dest / "objects" / sha(data)[:2] / f"{sha(data)}.gz").unlink()
    check = status_of(do_audit(arc), 5)
    assert check["status"] == "fail" and check["details"]["missing_files"] == [sha(data)]
    db = open_index(dest)
    db.execute("UPDATE object SET deleted_ts = '2026-01-09T00:00:00Z' WHERE sha256 = ?", (sha(data),))
    db.close()
    # pruned objects are excused only if nothing current points at them; this one is still latest
    # for its path, yet the design excuses pruned rows: the row says deleted on purpose
    assert status_of(do_audit(arc), 5)["status"] == "pass"


# 6 · retention
@pytest.mark.parametrize(
    "days, expected",
    [(3650, "pass"), (365, "pass"), (364, "fail"), (30, "fail"), (None, "fail")],
)
def test_retention_reads_only_cleanup_period_days(arc, tmp_path, days, expected):
    # the other keys, including a nested decoy, must not change the answer
    path = settings(tmp_path, days=days, env={"cleanupPeriodDays": 1 if expected == "pass" else 9999}, statusLine={"x": 1})
    check = status_of(do_audit(arc, settings_file=path), 6)
    assert check["status"] == expected


def test_retention_defaults_to_the_file_next_to_the_source(arc, tmp_path):
    settings(tmp_path, days=10)  # tmp_path/settings.json sits next to tmp_path/projects
    assert status_of(do_audit(arc), 6)["status"] == "fail"


def test_retention_fails_when_there_is_no_settings_file_and_warns_when_it_cannot_be_read(arc, tmp_path):
    missing = status_of(do_audit(arc, settings_file=tmp_path / "nope.json"), 6)
    assert missing["status"] == "fail" and "30 days" in missing["sentence"]
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert status_of(do_audit(arc, settings_file=broken), 6)["status"] == "warn"
    assert status_of(do_audit(arc, settings_file=tmp_path), 6)["status"] == "warn"  # a folder: unreadable


def test_retention_counts_files_gone_since_the_last_audit(arc):
    src, _, _ = arc
    do_audit(arc, hours=3)  # files the first audit
    (src / "proj" / "s2.jsonl").unlink()
    _, dest, _ = arc
    keep_running(src, dest, 4, 26)  # the run at 4 h sees the file gone; the audit is the next day
    later = audit.run_audit(dest, "hx", src=src, now=T0 + timedelta(hours=27), rng=random.Random(1))
    check = status_of(later, 6)
    assert check["status"] == "pass" and check["details"]["gone_since_last_audit"] == 1


# 7 · privacy
def test_privacy_fails_inside_a_repository(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    dest = repo / "arc"
    open_index(dest).close()
    report = audit.run_audit(dest, "hx", now=T0, settings_file=settings(tmp_path))
    check = status_of(report, 7)
    assert check["status"] == "fail" and "git working tree" in check["sentence"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_privacy_fails_when_others_can_read(arc):
    _, dest, _ = arc
    os.chmod(dest, 0o755)
    assert status_of(do_audit(arc), 7)["status"] == "fail"
    os.chmod(dest, 0o700)
    assert status_of(do_audit(arc), 7)["status"] == "pass"


# 8 · space
def test_space_warns_below_the_floor_and_reports_growth(arc, monkeypatch):
    first = do_audit(arc, hours=3)
    assert status_of(first, 8)["status"] == "pass"
    real = audit.shutil.disk_usage
    monkeypatch.setattr(audit.shutil, "disk_usage", lambda p: real(p)._replace(free=2 * 1000 ** 3))
    keep_running(arc[0], arc[1], 4, 26)
    second = do_audit(arc, hours=27)
    check = status_of(second, 8)
    assert check["status"] == "warn" and "below the 10 GB floor" in check["sentence"]
    assert "since the last audit" in check["sentence"]


# report and cadence
def test_cadence_goes_monthly_after_two_clean_audits_and_back_after_a_failure(arc):
    _, dest, _ = arc
    first = do_audit(arc, hours=3)
    assert first["cadence"] == "every two weeks"
    keep_running(arc[0], arc[1], 4, 28)
    second = do_audit(arc, hours=27)  # the next day: two clean days in a row
    assert second["clean"] is True and second["cadence"].startswith("monthly")
    failed = do_audit(arc, hours=60)  # runs silent for a day and a half
    assert failed["clean"] is False and failed["cadence"].startswith("every two weeks")


def test_a_second_audit_on_the_same_day_is_kept_and_does_not_count_as_the_previous_one(arc):
    _, dest, _ = arc
    first = do_audit(arc, hours=3)
    second = do_audit(arc, hours=3.5)
    assert Path(first["report_md"]).name == "AUDIT_hx_2026-01-10.md"
    assert Path(second["report_md"]).name == "AUDIT_hx_2026-01-10_2.md"
    assert Path(first["report_md"]).is_file() and Path(first["report_json"]).is_file()
    assert Path(second["report_json"]).is_file()
    # two clean audits in one day are one day, not two
    assert first["cadence"] == "every two weeks" and second["cadence"] == "every two weeks"
    # and the first is not "the last audit" for the second's growth line
    assert "since the last audit" not in status_of(second, 8)["sentence"]
    assert audit.latest_audit(dest, "hx")["generated_ts"] == second["generated_ts"]


def test_the_headline_counts_problems(arc):
    report = do_audit(arc, hours=30, settings_file=None)  # runs fail; retention reads the sibling file: fine
    assert report["headline"].startswith("1 problem, the first is ")
    md = Path(report["report_md"]).read_text(encoding="utf-8")
    assert "| 1 | Runs | FAIL |" in md


def test_latest_audit_finds_the_newest(arc):
    _, dest, _ = arc
    assert audit.latest_audit(dest) is None
    do_audit(arc, hours=3)
    assert audit.latest_audit(dest, "hx")["host"] == "hx"


def test_the_audit_never_touches_the_source_or_the_objects(arc):
    src, dest, _ = arc
    before = sorted(str(p.relative_to(dest)) for p in (dest / "objects").rglob("*"))
    do_audit(arc)
    assert sorted(str(p.relative_to(dest)) for p in (dest / "objects").rglob("*")) == before


def test_cli_audit_files_a_report(arc, capsys):
    src, dest, tmp = arc
    code = main(["archive", "audit", "--dest", str(dest), "--host", "hx", "--src", str(src),
                 "--settings", str(tmp / "settings.json")])
    env = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0 and env["ok"] is True
    assert len(env["result"]["checks"]) == 8
    assert Path(env["result"]["report_md"]).is_file()
    # the runs check fails because the fixture's runs are years old: the envelope says so in words
    assert any("Runs" in w["message"] for w in env.get("warnings", []))


def test_cli_audit_on_a_folder_without_an_archive(tmp_path, capsys):
    code = main(["archive", "audit", "--dest", str(tmp_path / "none"), "--host", "hx"])
    env = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 1 and env["error"]["code"] == "no_archive"


def test_coverage_fails_for_a_file_over_the_size_limit_and_names_it(arc, monkeypatch):
    src, dest, _ = arc
    monkeypatch.setattr(store, "MAX_FILE_BYTES", 100)
    put(src / "proj" / "huge.jsonl", b"x" * 200)
    run_archive(src, dest, "hx", now=T0 + timedelta(hours=2, minutes=30))
    check = status_of(do_audit(arc), 2)
    assert check["status"] == "fail"
    assert check["details"]["never_archived_total"] == 1
    assert "proj/huge.jsonl" in check["sentence"] and "can never be archived" in check["sentence"]


def test_coverage_fails_for_a_file_that_cannot_be_read(arc, monkeypatch):
    src, _, _ = arc
    real = store.walk_source

    def walk(root):
        yield from real(root)
        yield "proj/deep.jsonl", root / "proj" / "deep.jsonl", None, "unreadable"

    monkeypatch.setattr(store, "walk_source", walk)
    check = status_of(do_audit(arc), 2)
    assert check["status"] == "fail" and "proj/deep.jsonl (could not be read)" in check["sentence"]
