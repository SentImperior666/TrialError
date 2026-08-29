"""Not a test module (pytest only collects ``test_*.py``) -- run as a
FRESH subprocess by ``tests/test_obs_noop_subprocess.py`` with
``opentelemetry.*`` imports blocked at the meta-path level, genuinely
proving ``trialerror.obs``'s no-op degradation path (rather than merely
monkeypatching a flag in an already-loaded module, which would not catch a
real ``ImportError`` anywhere the try/except in ``trialerror.obs.tracer`` might
have gotten the branching wrong).

Exits 0 and prints ``{"ok": true}`` on success; any failed assertion or
unexpected exception propagates as a normal traceback + non-zero exit,
which the test file asserts against.
"""

from __future__ import annotations

import json
import sys


class _BlockOpentelemetry:
    """A minimal ``sys.meta_path`` finder: refuses to find ANY
    ``opentelemetry``-rooted module, as if the ``obs`` extra were never
    installed in this interpreter -- even though it genuinely IS installed
    in this dev venv (this script's whole point is to prove the no-op path
    independent of what happens to be on disk)."""

    def find_spec(self, name, _path, _target=None):
        if name == "opentelemetry" or name.startswith("opentelemetry."):
            raise ImportError(f"blocked for test: {name!r}")
        return None


def main() -> int:
    sys.meta_path.insert(0, _BlockOpentelemetry())

    from trialerror.obs import spans, state, tracer
    from trialerror.util.doctor import DoctorContext, discover_and_register_checks, run_checks

    assert tracer.is_available() is False, "tracer.is_available() must be False with opentelemetry blocked"

    # Every span-wrapper context manager must be a true no-op: no
    # exception, and (since nothing is configured to persist anywhere) no
    # crash from the missing SDK types either.
    with spans.launch_span(launch_id="LNCH-noop", agent_kind="a", model="m", actual_tokens=1):
        pass
    with spans.retrieval_span(query="q", tiers=["fts"], k=3):
        pass
    with spans.verification_span(procedure="p", subject_id="s", verdict="PASS"):
        pass
    with spans.job_attempt_span(job_id="JOB-noop", kind="k"):
        pass

    # tracer.flush()/shutdown() must also be harmless no-ops.
    assert tracer.flush() is True
    tracer.shutdown()

    # The doctor check must SKIP (not fail/crash) when the extra is absent.
    discover_and_register_checks()
    results = run_checks(DoctorContext(program_root=None), only=["obs_exporter_reachable"])
    assert results[0].status == "skip", results[0].to_dict()

    # And span-drop bookkeeping must still be inert (never invoked, since
    # nothing was ever exported at all in this no-op path).
    assert state.process_drop_count() == 0

    print(json.dumps({"ok": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
