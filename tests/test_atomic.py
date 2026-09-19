import subprocess
import sys
import time
from pathlib import Path

from trialerror.util.atomic import atomic_write_bytes, atomic_write_text

_KILL_MID_WRITE_SCRIPT = """
import sys, time
from trialerror.util.atomic import atomic_write_bytes

def on_chunk(i):
    time.sleep(0.2)

atomic_write_bytes(sys.argv[1], b"X" * 1_000_000, _chunk_size=100_000, _on_chunk=on_chunk)
"""


def test_atomic_write_bytes_basic(tmp_path):
    target = tmp_path / "out.bin"
    atomic_write_bytes(target, b"hello world")
    assert target.read_bytes() == b"hello world"


def test_atomic_write_text_normalizes_crlf(tmp_path):
    target = tmp_path / "out.txt"
    atomic_write_text(target, "line1\r\nline2\rline3\n")
    assert target.read_bytes() == b"line1\nline2\nline3\n"


def test_atomic_write_leaves_no_tmp_file_behind_on_success(tmp_path):
    target = tmp_path / "out.bin"
    atomic_write_bytes(target, b"payload")
    leftovers = [p for p in tmp_path.iterdir() if p != target]
    assert leftovers == []


def test_atomic_write_cleans_up_tmp_on_controlled_failure(tmp_path, monkeypatch):
    target = tmp_path / "out.bin"
    target.write_bytes(b"original")

    def boom(*a, **kw):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr("os.fsync", boom)
    try:
        atomic_write_bytes(target, b"new content")
    except OSError:
        pass

    # target untouched, and no orphaned temp file left in the directory
    assert target.read_bytes() == b"original"
    leftovers = [p for p in tmp_path.iterdir() if p != target]
    assert leftovers == [], f"orphaned temp file(s): {leftovers}"


def test_atomic_write_survives_kill_mid_write_overwrite_case(tmp_path):
    """The M0 acceptance criterion: 'atomic-write survives kill-mid-write
    test'. A child process is killed partway through writing a NEW version
    of an existing file (before it ever reaches os.replace) -- the parent
    asserts the original file content is completely untouched afterward,
    proving the write only ever mutated a temp file, never the target."""
    target = tmp_path / "existing.bin"
    original = b"ORIGINAL-CONTENT-BEFORE-KILL"
    target.write_bytes(original)

    script_path = tmp_path / "_kill_mid_write.py"
    script_path.write_text(_KILL_MID_WRITE_SCRIPT, encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, str(script_path), str(target)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.45)  # well inside the ~2s total write (10 chunks x 0.2s), before replace
        assert proc.poll() is None, "child finished before we could kill it mid-write; test is not exercising the failure window"
        proc.kill()
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    assert target.read_bytes() == original, "target file was corrupted by a kill mid-write"


def test_atomic_write_survives_kill_mid_write_new_file_case(tmp_path):
    """Same scenario, but the target file does not exist yet: after a kill
    mid-write it must still not exist (replace never ran)."""
    target = tmp_path / "brand_new.bin"
    assert not target.exists()

    script_path = tmp_path / "_kill_mid_write_new.py"
    script_path.write_text(_KILL_MID_WRITE_SCRIPT, encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, str(script_path), str(target)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.45)
        assert proc.poll() is None, "child finished before we could kill it mid-write; test is not exercising the failure window"
        proc.kill()
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    assert not target.exists(), "target file was created despite the write being killed before replace"


def test_atomic_write_full_run_completes_and_replaces(tmp_path):
    """Sanity companion to the kill tests: letting the same slow write run
    to completion DOES land the new content (the mechanism isn't just
    'never write', it's 'write atomically')."""
    target = tmp_path / "completed.bin"
    target.write_bytes(b"old")

    calls = []
    atomic_write_bytes(target, b"Y" * 1000, _chunk_size=250, _on_chunk=calls.append)

    assert calls == [0, 250, 500, 750]
    assert target.read_bytes() == b"Y" * 1000
