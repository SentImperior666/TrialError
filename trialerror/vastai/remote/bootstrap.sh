#!/bin/sh
# Runs ON the rented vast.ai instance, never on DEV (the vast.ai OCR design, sections 5.1 and 5.2).
# DEV uploads it into the lease's scratch directory and calls it over ssh:
#
#   sh bootstrap.sh install <requirements.lock> <model cache dir> [venv dir]
#       Finds the interpreter that imports the image's torch, installs DEV's frozen marker stack
#       INTO AN ENVIRONMENT THAT SEES THAT TORCH with every wheel's hash checked, then prints ONE
#       line "TE-BOOTSTRAP {json}": which interpreter was taken and how, the versions (marker,
#       surya, torch, its CUDA, the driver), the absolute marker_single it installed, whether torch
#       sees the GPU, and the size of /dev/shm. DEV compares them; nothing is decided here.
#   sh bootstrap.sh models <model cache dir> [python]
#       Prints "<sha256>  <relative path>" for every file of the model cache, which DEV compares
#       with the manifest generated from its own cache. DEV passes the interpreter the install
#       chose, because an image need not have `python3` on a non-interactive PATH at all.
#
# It receives no document, no job id and no key, and it prints nothing that names one.
#
# Why the interpreter is searched for (canary C4b, 2026-09-20, live finding 4): a non-interactive
# `ssh host cmd` does not read the login shell's profile, so PATH is not an interactive session's.
# On pytorch/pytorch:2.13.0-cuda13.0-cudnn9-runtime that PATH's `python3` is Debian's system
# Python 3.12 -- which does not carry torch and which refuses `pip install` under PEP 668
# ("externally-managed-environment"). The interpreter that carries torch is elsewhere on the image.
#
# Noise (pip's output, every candidate's verdict) goes to <lease dir>/bootstrap.log, never to DEV's
# stderr; on a failure this script prints a BOUNDED digest of that log (first and last 20 lines,
# each cut to 200 characters), the interpreter it chose, and the pip version, between the markers
# TE-BOOTSTRAP-FAIL / TE-BOOTSTRAP-LOG-HEAD / TE-BOOTSTRAP-LOG-TAIL / TE-BOOTSTRAP-END.
#
# [assumption] marker/surya 1.10.2 read their model cache location from MODEL_CACHE_DIR; set explicitly
# so the cache DEV hashes is the cache marker loads.
set -eu

#: The interpreters tried, in order: the login PATH's first, then the places images keep their own.
#: TE_BOOTSTRAP_CANDIDATES overrides the list (space separated). DEV never sets it -- the ssh command
#: it sends carries no environment -- and it is no new trust: an image able to set it already owns
#: every interpreter on the list. It exists so the tests can build each image shape, and so a future
#: image's location can be named without changing this file.
CANDIDATES="${TE_BOOTSTRAP_CANDIDATES:-python python3 /opt/conda/bin/python /opt/venv/bin/python /usr/local/bin/python3}"
LOG_LINES=20
LOG_COLS=200

log=""
stage="startup"
base_py=""
env_py=""
env_kind=""
py_version="unknown"
pip_version="unknown"
externally_managed="unknown"
venv_error=""
pip_extra=""

note() {
    [ -n "$log" ] && echo "$@" >>"$log" 2>/dev/null || true
}

digest() {
    # Bounded: never pip's whole essay.
    [ -n "$log" ] && [ -f "$log" ] || return 0
    echo "TE-BOOTSTRAP-LOG-HEAD" >&2
    head -n "$LOG_LINES" "$log" 2>/dev/null | cut -c "1-$LOG_COLS" >&2 || true
    echo "TE-BOOTSTRAP-LOG-TAIL" >&2
    tail -n "$LOG_LINES" "$log" 2>/dev/null | cut -c "1-$LOG_COLS" >&2 || true
}

fail() {
    rc="${1:-1}"
    [ "$rc" -ne 0 ] 2>/dev/null || rc=1
    echo "TE-BOOTSTRAP-FAIL stage=${stage} rc=${rc} python=${env_py:-${base_py:-none}}" \
         "python_version=${py_version} pip=${pip_version} env=${env_kind:-none}" \
         "externally_managed=${externally_managed} venv_error=${venv_error:-none}" >&2
    digest
    echo "TE-BOOTSTRAP-END" >&2
    exit "$rc"
}

mode="${1:-}"
case "$mode" in
install)
    lock="$2"
    cache="$3"
    venv="${4:-/var/tmp/te-bootstrap-venv}"
    log="$(dirname -- "$lock")/bootstrap.log"
    : >"$log" 2>/dev/null || log=/dev/null
    mkdir -p -- "$cache"
    export MODEL_CACHE_DIR="$cache"

    # 1. The interpreter that imports torch. The image supplies torch; we never install it.
    stage="interpreter"
    for cand in $CANDIDATES; do
        case "$cand" in
        /*)
            exe="$cand"
            if [ ! -x "$exe" ]; then note "candidate $cand: no such executable"; continue; fi
            ;;
        *)
            exe="$(command -v "$cand" 2>/dev/null || true)"
            if [ -z "$exe" ]; then note "candidate $cand: not on this PATH ($PATH)"; continue; fi
            ;;
        esac
        if "$exe" -c 'import torch' >>"$log" 2>&1; then
            note "candidate $cand -> $exe: imports torch"
            base_py="$exe"
            break
        fi
        note "candidate $cand -> $exe: does not import torch"
    done
    [ -n "$base_py" ] || fail 3
    py_version="$("$base_py" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>>"$log" || echo unknown)"

    # 2. Where the lock may be installed. PEP 668: an interpreter is externally managed when its
    #    stdlib holds an EXTERNALLY-MANAGED marker -- pip ignores that marker inside a virtualenv,
    #    so being in one already is the same as not being managed.
    stage="environment"
    externally_managed="$("$base_py" -c 'import os, sys, sysconfig
marker = os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED")
print("yes" if sys.prefix == sys.base_prefix and os.path.exists(marker) else "no")' 2>>"$log" || echo unknown)"
    if [ "$externally_managed" = "yes" ]; then
        mkdir -p -- "$(dirname -- "$venv")" >>"$log" 2>&1 || true
        venv_out="$(dirname -- "$lock")/venv.out"
        if "$base_py" -m venv --system-site-packages "$venv" >"$venv_out" 2>&1 && [ -x "$venv/bin/python" ]; then
            # Sees the image's torch through the system site-packages; installs the lock beside it.
            env_py="$venv/bin/python"
            env_kind="venv"
            note "venv with system site packages: $env_py"
        else
            # Why, in one line (live records, canary attempt 3, 2026-09-20): the last non-empty line venv printed, cut. Both
            # streams, because which one a distribution's patched venv writes its message to is
            # not ours to know.
            venv_error="$(grep -v '^[[:space:]]*$' "$venv_out" 2>/dev/null | tail -n 1 | cut -c "1-$LOG_COLS" || true)"
            [ -n "$venv_error" ] || venv_error="venv printed nothing and left no executable $venv/bin/python"
            # Stated fallback, acceptable ONLY because this container is rented, disposable and
            # destroyed in every exit path: the image has no usable `venv`/`ensurepip`.
            env_py="$base_py"
            env_kind="user-break-system-packages"
            pip_extra="--break-system-packages --user"
            note "no usable venv/ensurepip on this image ($venv_error); falling back to $pip_extra in the disposable container"
        fi
        # The whole of what venv said still goes to the log, so the failure digest carries it.
        cat "$venv_out" >>"$log" 2>/dev/null || true
    else
        env_py="$base_py"
        env_kind="direct"
        note "not externally managed: installing into $env_py"
    fi

    stage="pip"
    pip_version="$("$env_py" -m pip --version 2>>"$log" | head -n 1 | cut -d' ' -f2 || true)"
    [ -n "$pip_version" ] || { pip_version="unknown"; fail 4; }

    # 3. DEV's freeze, every wheel's sha256 checked, no dependency resolution: torch is never touched.
    stage="pip install"
    "$env_py" -m pip install --no-deps --require-hashes --no-input --disable-pip-version-check -q \
        $pip_extra -r "$lock" >>"$log" 2>&1 || fail $?

    stage="report"
    "$env_py" - "$cache" "$env_kind" "$base_py" "$pip_version" "$venv" "$externally_managed" \
        "$venv_error" <<'PY' || fail $?
import json
import os
import subprocess
import sys
import sysconfig
from importlib import metadata


def version(dist):
    try:
        return metadata.version(dist)
    except Exception:
        return None


def space(path):
    try:
        st = os.statvfs(path)
    except OSError:
        return None, None
    return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize


def script(name):
    """The console script THIS environment installed, absolute: DEV runs it by
    path, because the PATH of a non-interactive ssh command is not the one that
    would find it."""
    seen = []
    for get in (lambda: sysconfig.get_path("scripts"),
                lambda: sysconfig.get_path("scripts", sysconfig.get_preferred_scheme("user")),
                lambda: os.path.dirname(os.path.abspath(sys.executable)),
                lambda: os.path.expanduser("~/.local/bin")):
        try:
            directory = get()
        except Exception:
            continue
        if not directory or directory in seen:
            continue
        seen.append(directory)
        path = os.path.join(directory, name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


cache, env_kind, base_python, pip_version, venv, externally_managed = sys.argv[1:7]
venv_error = sys.argv[7] if len(sys.argv) > 7 else ""
report = {
    "marker": version("marker-pdf"),
    "surya": version("surya-ocr"),
    "torch": version("torch"),
    "python": "%d.%d.%d" % sys.version_info[:3],
    "python_exe": os.path.abspath(sys.executable),
    "base_python": base_python,
    "env_kind": env_kind,
    # Why the venv was not built, when it was tried and failed (live records, canary attempt 3, 2026-09-20):
    # None when one was built, or when the interpreter was not externally managed.
    "venv_error": venv_error or None,
    "externally_managed": externally_managed,
    "pip_version": pip_version,
    "venv_dir": venv if env_kind == "venv" else None,
    "marker_exe": script("marker_single"),
    "model_cache_dir": cache,
}
try:
    import torch

    report["torch_cuda"] = torch.version.cuda
    report["cuda_available"] = bool(torch.cuda.is_available())
    report["gpu"] = torch.cuda.get_device_name(0) if report["cuda_available"] else None
except Exception as exc:  # reported, DEV decides
    report["torch_error"] = "%s: %s" % (type(exc).__name__, exc)
    report["cuda_available"] = False
try:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
        capture_output=True, text=True, timeout=60,
    )
    report["driver"] = (out.stdout or "").strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else None
except Exception:
    report["driver"] = None
report["shm_size_bytes"], report["shm_avail_bytes"] = space("/dev/shm")
report["disk_avail_bytes"] = space("/var/tmp")[1]
print("TE-BOOTSTRAP " + json.dumps(report, sort_keys=True), flush=True)
PY
    ;;
models)
    cache="$2"
    models_py="${3:-python3}"
    "$models_py" - "$cache" <<'PY'
import hashlib
import os
import sys

root = sys.argv[1]
for dirpath, dirnames, filenames in os.walk(root):
    dirnames.sort()
    for name in sorted(filenames):
        path = os.path.join(dirpath, name)
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        print("%s  %s" % (digest.hexdigest(), os.path.relpath(path, root).replace(os.sep, "/")))
PY
    ;;
*)
    echo "usage: sh bootstrap.sh install <lock> <model cache dir> [venv dir] | models <model cache dir> [python]" >&2
    exit 2
    ;;
esac
