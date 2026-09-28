#!/usr/bin/env python3
"""Runs ON the rented vast.ai instance, never on DEV: ONE ``marker_single``
invocation and ONE JSON status line (the vast.ai OCR design, section 5.4).

    python3 te_range.py --timeout S --out DIR --stem input --model-cache DIR -- marker_single ...

The argv after ``--`` is DEV's own ``marker_single`` argv, every element
quoted on DEV. This wrapper runs it as its single child, then prints
``TE-RANGE {json}`` with:

* ``rc`` -- the exit code (128 + N for a child killed by signal N, so an OOM
  kill reads 137) or ``null`` when the timeout killed it (``timed_out``);
* ``stderr_tail`` -- the last 4000 characters of the child's stderr;
* ``ru_maxrss_bytes`` -- ``getrusage(RUSAGE_CHILDREN).ru_maxrss`` read after
  the child was reaped. With exactly one child that is THIS invocation's own
  peak (on DEV it is cumulative);
* ``md_path`` / ``md_sha256`` / ``md_bytes`` -- the markdown it produced
  (``<out>/<stem>/<stem>.md``, else the first ``.md`` under ``<out>``).

Only the markdown is kept: marker's extracted images are never fetched (DEV
reads the ``.md`` only), and the scratch is RAM. The wrapper itself always
exits 0 once it has printed its line; DEV decides what the numbers mean.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

PREFIX = "TE-RANGE "
STDERR_TAIL_CHARS = 4000


def _find_markdown(out: Path, stem: str) -> Path | None:
    expected = out / stem / f"{stem}.md"
    if expected.is_file():
        return expected
    found = sorted(out.rglob("*.md"))
    return found[0] if found else None


def _prune(out: Path, keep: Path | None) -> None:
    for path in sorted(out.rglob("*"), reverse=True):
        try:
            if path.is_file() and path != keep:
                path.unlink()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="te_range.py")
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--stem", required=True)
    parser.add_argument("--model-cache", required=True)
    parser.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, MODEL_CACHE_DIR=args.model_cache)
    status: dict = {"rc": None, "timed_out": False, "stderr_tail": ""}
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env, timeout=args.timeout
        )
        rc, stderr = proc.returncode, proc.stderr or b""
    except subprocess.TimeoutExpired as exc:
        rc, stderr = None, exc.stderr or b""
        status["timed_out"] = True
    except OSError as exc:
        rc, stderr = 127, str(exc).encode("utf-8", "replace")
    if rc is not None and rc < 0:
        rc = 128 - rc
    status["rc"] = rc
    status["wall_s"] = round(time.monotonic() - started, 3)
    status["stderr_tail"] = stderr.decode("utf-8", "replace")[-STDERR_TAIL_CHARS:]
    try:
        # Kilobytes on Linux.
        status["ru_maxrss_bytes"] = int(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss) * 1024
    except Exception:
        status["ru_maxrss_bytes"] = None
    markdown = _find_markdown(out, args.stem)
    if markdown is not None:
        data = markdown.read_bytes()
        status["md_path"] = str(markdown)
        status["md_sha256"] = hashlib.sha256(data).hexdigest()
        status["md_bytes"] = len(data)
    else:
        status["md_path"] = status["md_sha256"] = status["md_bytes"] = None
    _prune(out, markdown)
    print(PREFIX + json.dumps(status, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
