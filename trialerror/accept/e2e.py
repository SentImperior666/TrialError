"""The end-to-end handover-gate journeys (``docs/reviews/E2E_TEST_PLAN.md``
Section 7), as the acceptance harness's second suite.

``trialerror.accept.journeys`` owns the M15 clean-checkout smoke -- the floor
this suite stands on and never re-proves. This module owns everything the
plan's Section 1 table lists as "does not exist yet": a corpus journey that
drives ``program init`` and ``session boot --create-account`` through the real
CLI and asserts both lexical backends on one corpus; a dashboard journey with
HTTP assertions on a scratch program INCLUDING the token-guarded write path; an
ops journey that exercises the PostToolUse hook, the feed translator's
fail-closed seam behind a real booking, law append/verify with a stale-pin
negative control, and the jobs worker through the real console entry point; an
offload enqueue/verify pair; the operator-item enumeration; and the
``e2e_check`` evidence row every check in the plan is recorded as.

House style, inherited from :mod:`trialerror.accept.journeys` and kept
deliberately identical: one canonical function per journey, returning a single
:class:`~trialerror.util.doctor.CheckResult` whose ``details["steps"]`` names
every attempted step in order; ``_step``/``AcceptanceStepError`` imported from
that module rather than re-implemented; business logic through the landed
Python API; REAL subprocesses only where the subprocess boundary is itself what
is being proven (the hook scripts, ``program init``, ``session boot
--create-account``, ``ingest reindex-fulltext``, ``law append``/``verify``,
``feed translate``, ``jobs start-worker --job-id``, ``jobs pause``,
``jobs logs``, ``dashboard serve`` + HTTP against it, ``events tail``/
``export --out``). Nothing here imports from ``tests/``.

**Three rules this module enforces on itself, because the plan's safety
section turns on them.**

1. *Explicit roots, always.* Every subprocess is launched with BOTH
   ``--program-root`` and ``--platform-root`` AND ``TRIALERROR_PLATFORM_ROOT``
   in its environment (:func:`_subprocess_env`) -- several CLI groups
   (``law``, ``feed``) open their store without threading the flag through, so
   the environment variable is the only thing standing between a scratch run
   and the caller's real platform root. :func:`_cli_argv` also pins
   ``PYTHONPATH`` at the parent of the *running* ``trialerror`` package so a
   subprocess can never resolve a different checkout than its parent did.

2. *Balanced launches.* Every journey holds exactly one launch in the
   production shape -- book, consume through the REAL ``spawn_gate`` hook
   subprocess, return through the REAL ``post_task`` hook subprocess, reconcile
   -- via :func:`hold_journey_launch`. That is what lets the doctor's
   ``spawns_vs_bookings`` reconciliation be REQUIRED to pass rather than
   tolerated (plan E-16/E-20). A journey that booked "for attribution" and
   reconciled at the end would leave a consumed booking with no
   ``subagent_return`` and make that criterion false by construction.

3. *A blocked capability is never a pass, and never a gap either.* A step whose
   lane is absent records ``{"blocked": True, "owner": ...}`` and drops the
   whole journey's status to ``warn``. There is no code path in this module
   that turns a missing capability into a green check -- and a journey that
   returns early on an absent capability still writes its ``blocked``
   ``e2e_check`` row first, because ``--phase report`` reads a missing row as
   ``MISSING``, which loses both the status and the owner.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from trialerror.accept.journeys import (
    AcceptanceStepError,
    _CITECHECK_SENTENCE,
    _run_hook_script,
    _step,
    _write_pdf_text_fixture,
)
from trialerror.util.doctor import CheckResult, DoctorContext, discover_and_register_checks, run_checks

__all__ = [
    "E2E_CHECK_EVENT_TYPE",
    "E2E_CHECK_CATALOGUE",
    "E2E_OPERATOR_ITEMS",
    "E2ECheckSpec",
    "E2ERecorder",
    "Capabilities",
    "automated_check_ids",
    "e2e_check_sequence",
    "e2e_operator_enumeration",
    "hold_journey_launch",
    "journey_check_ids",
    "probe_capabilities",
    "run_e2e_corpus",
    "run_e2e_dashboard",
    "run_e2e_offload_enqueue",
    "run_e2e_ops",
    "verify_e2e_offload_roundtrip",
]

#: The one event type every check in the plan is recorded as (plan Section 2's
#: evidence convention, Section 7.3's payload shape).
E2E_CHECK_EVENT_TYPE = "e2e_check"

#: ``--run-id`` shape. The plan takes the run id from the shell clock
#: (``e2e-$(date -u +%Y%m%dT%H%M%SZ)``) and nowhere else; the CLI refuses
#: anything else so a hand-typed id cannot enter the record.
RUN_ID_RE = re.compile(r"^e2e-\d{8}T\d{6}Z$")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_PARENT = Path(__file__).resolve().parents[2]
_SESSION_START_HOOK = _REPO_ROOT / "plugin" / "hooks" / "session_start.py"
_SPAWN_GATE_HOOK = _REPO_ROOT / "plugin" / "hooks" / "spawn_gate.py"
_POST_TASK_HOOK = _REPO_ROOT / "plugin" / "hooks" / "post_task.py"

#: The plan's own relative path, used for the ``plan_blob`` provenance field.
PLAN_PATH = "docs/reviews/E2E_TEST_PLAN.md"

#: The sentence the pdf fixture carries and E-15/E-17 search for. Shared with
#: the clean-checkout smoke so both suites ground on one string.
ACCEPTANCE_SENTENCE = _CITECHECK_SENTENCE

#: Two-word phrases lifted from ``trialerror.demo.content.DOCUMENTS`` -- E-14's
#: parity queries beside the acceptance sentence.
PARITY_QUERIES: tuple[str, ...] = (ACCEPTANCE_SENTENCE, "desirable difficulties", "delayed retention")


# ===========================================================================
# the check catalogue -- one row per plan id, the single source of truth for
# what is automated, what is a human step, and which journey owns which id
# ===========================================================================
@dataclass(frozen=True)
class E2ECheckSpec:
    """One row of the plan's Section 3 catalogue.

    ``by`` is who discharges the check; ``journey`` names the journey function
    that records it when ``by == "journey"``. ``operator_key`` is set for every
    check that ALSO needs a human stand-in in :data:`E2E_OPERATOR_ITEMS` --
    including E-52, whose DEV-worker half is an operator step even though its
    sandbox-side verification is automated.
    """

    check_id: str
    phase: str
    by: str
    title: str
    gating: bool
    journey: str | None = None
    operator_key: str | None = None
    instruction: str | None = None


def _spec(
    check_id: str,
    phase: str,
    by: str,
    title: str,
    *,
    gating: bool = True,
    journey: str | None = None,
    operator_key: str | None = None,
    instruction: str | None = None,
) -> E2ECheckSpec:
    return E2ECheckSpec(
        check_id=check_id, phase=phase, by=by, title=title, gating=gating,
        journey=journey, operator_key=operator_key, instruction=instruction,
    )


#: Placeholder vocabulary, verbatim from the plan's header: ``<sandbox-host>``
#: is the machine running the container, ``<deploy-root>`` the deploy directory
#: on it, ``<program>`` the live program, ``P``/``PP``/``POFF`` the scratch
#: roots, ``RUN`` the shell-clock run id, ``TE``/``TEOFF``/``EV``/``PG``/
#: ``SQLRO`` the shell functions the plan's header defines. Nothing here names
#: a host, a person, or a program.
_PLAN_REF = "docs/reviews/E2E_TEST_PLAN.md"

E2E_CHECK_CATALOGUE: tuple[E2ECheckSpec, ...] = (
    _spec(
        "E-00", "P0", "orchestrator", "capability probes + live baseline",
        operator_key="e2e_e00_capability_probes_and_baseline",
        instruction=(
            "P0, on <sandbox-host>, before anything else (see " + _PLAN_REF + " Section 3 E-00 for the "
            "full block and its pass criterion). Run, with the plan header's shell functions defined:\n"
            "  docker inspect -f '{{.RestartCount}} {{.State.Status}}' <container>\n"
            "  docker exec -u node <container> tmux -L te list-windows -t main -F '#{window_name}' | paste -sd' ' -\n"
            "  PG -fa '[c]laude '                      # bracketed pattern, `|| true` on the HOST side\n"
            "  PG -fc '[b]in/trialerror jobs start-worker'   # the worker singleton: expect <= 1\n"
            "  docker exec -u node <container> trialerror --version\n"
            "  docker exec -u node <container> git -C /workspace/research-harness rev-parse --short HEAD\n"
            "  docker exec -u node <container> sh -c 'trialerror ingest reindex-fulltext --help >/dev/null 2>&1 "
            "&& echo tantivy_cli=1 || echo tantivy_cli=0; python -c \"import tantivy\" 2>/dev/null "
            "&& echo tantivy_pkg=1 || echo tantivy_pkg=0'\n"
            "  docker exec -u node <container> sh -c 'trialerror feed translate --help >/dev/null 2>&1 "
            "&& echo translator=1 || echo translator=0'\n"
            "  docker exec -u node <container> sh -c 'trialerror offload --help >/dev/null 2>&1 "
            "&& echo offload=1 || echo offload=0'\n"
            "  SQLRO /workspace/platform/platform.db 'select count(*) from launch;'\n"
            "  curl -s -o /dev/null -w '%{http_code}\\n' http://127.0.0.1:8850/dashboard/api/all\n"
            "  <deploy-root>/te-status.sh\n"
            "PASS: restart count 0 + status running, exactly the three accepted tmux windows, worker count <= 1. "
            "The probe lines are recorded whatever they say; each dependent check re-probes by invoking the verb. "
            "`trialerror accept --suite e2e` runs its own in-process probe (probe_capabilities) for the journeys -- "
            "this item is the CONTAINER-side baseline E-60 compares against, and only a human with the host shell "
            "can take it."
        ),
    ),
    _spec(
        "E-01", "P0", "orchestrator", "scratch roots created node-owned inside the bind mount",
        operator_key="e2e_e01_scratch_roots",
        instruction=(
            "P0. docker exec -u node <container> mkdir -p \"${E2E}\" (if that fails on ownership: "
            "docker exec <container> install -d -o node -g node \"${E2E}\"), then "
            "docker exec -u node <container> sh -c \"test -w ${E2E} && stat -c '%U %a' ${E2E}\". "
            "PASS: owner node, writable. A root-owned tree that root can write is excluded by the -u node "
            "writability test (the WAL-sidecar ownership failure class). Cleanup at E-60."
        ),
    ),
    _spec(
        "E-02", "P1", "journey", "clean-checkout smoke on the scratch roots",
        journey="e2e_ops",
    ),
    _spec("E-10", "P2", "journey", "scratch program init through the real CLI", journey="e2e_corpus"),
    _spec("E-11", "P2", "journey", "session boot through the real CLI, then the SessionStart hook reuses it", journey="e2e_corpus"),
    _spec("E-12", "P2", "journey", "ingest the fixture slice to indexed", journey="e2e_corpus"),
    _spec("E-13", "P2", "journey", "full-text reindex + staleness doctor", journey="e2e_corpus"),
    _spec("E-14", "P2", "journey", "lexical-tier parity on this corpus", journey="e2e_corpus"),
    _spec("E-15", "P2", "journey", "retrieve engine, auto mode, citation + fence", journey="e2e_corpus"),
    _spec("E-16", "P2", "journey", "doctor: 0 failed on the scratch program", journey="e2e_corpus"),
    _spec("E-17", "P3", "journey", "the dashboard serves the SCRATCH program", journey="e2e_dashboard"),
    _spec("E-70", "P3", "journey", "the token-guarded write path, refused then accepted", journey="e2e_dashboard"),
    _spec(
        "E-18", "P3", "orchestrator", "the LIVE dashboard is untouched",
        operator_key="e2e_e18_live_dashboard_untouched",
        instruction=(
            "P3, from <sandbox-host>'s loopback, WHILE/AFTER the dashboard journey ran:\n"
            "  curl -s http://127.0.0.1:8850/dashboard/api/all | python3 -c "
            "'import json,sys; print(json.load(sys.stdin)[\"meta\"][\"program_root\"])'\n"
            "  PG -fc '[b]in/trialerror dashboard serve --foreground'   # expect 1 (the live serve)\n"
            "  PG -fa -- \"--port <the port the dashboard journey reported>\"   # expect no output\n"
            "PASS: the LIVE program root, exactly one serve process, nothing bound to the journey's port. "
            "The journey reports its port in the e2e_check row for E-17 (evidence.port) and in its step detail. "
            "If the port line matches, kill only that pid -- never restart the dashboard window."
        ),
    ),
    _spec("E-19", "P4", "journey", "feed thread + post + translation fails CLOSED", journey="e2e_ops"),
    _spec("E-20", "P4", "journey", "budget: book -> spawn -> return -> reconcile -> zero dangling", journey="e2e_ops"),
    _spec("E-21", "P4", "journey", "law append/verify, chain_ok, stale-pin control", journey="e2e_ops"),
    _spec("E-22", "P4", "journey", "jobs worker claim/finish through the console entry point", journey="e2e_ops"),
    _spec("E-23", "P4", "journey", "events tail + byte-stable export", journey="e2e_ops"),
    _spec("E-24", "P4", "journey", "session close with handoff", journey="e2e_ops"),
    _spec(
        "E-30", "P5", "orchestrator", "start the e2e session in a 4th tmux window",
        operator_key="e2e_e30_start_live_session",
        instruction=(
            "P5. The e2e session ALWAYS runs in its own `e2e` tmux window -- never by repointing an existing "
            "session's cwd at the scratch program (that would be a mixed-root session). Run:\n"
            "  docker exec -u node <container> tmux -L te new-window -d -t main -n e2e -c \"${P}\"\n"
            "  docker exec -u node <container> tmux -L te send-keys -t main:e2e "
            "\"export TRIALERROR_PLATFORM_ROOT=${PP}; claude --remote-control --name ${RUN} "
            "--plugin-dir /workspace/research-harness/plugin --permission-mode acceptEdits\" Enter\n"
            "  sleep 20; docker exec -u node <container> tmux -L te list-windows -t main -F '#{window_name}'\n"
            "  PG -fc \"[c]laude --remote-control --name ${RUN}\"\n"
            "  docker exec -u node <container> tmux -L te capture-pane -p -t main:e2e | tail -15\n"
            "PASS: the three accepted windows plus `e2e`, exactly one claude process, the Remote Control banner "
            "in the pane. NEVER start it with a permission-bypass flag."
        ),
    ),
    _spec(
        "E-31", "P5", "orchestrator", "SessionStart hook fired live",
        operator_key="e2e_e31_live_session_start_hook",
        instruction=(
            "P5. $TE events tail --type hook_alive --limit 20 ; $TE session status. PASS: a NEW hook_alive row "
            "with payload.hook == 'session_start' whose ts is after E-30 and whose session_id is the session "
            "`session status` reports open (necessarily different from the journeys' session, which E-24 closed), "
            "AND the operator confirms the boot bundle text (pin status, dangling launches, inbox count, budget "
            "headroom, memory index) actually appeared as injected context. The journeys' own hook_alive rows are "
            "excluded by the ts/session_id comparison. Offline proxy: this suite's E-11."
        ),
    ),
    _spec(
        "E-32", "P5", "operator", "the spawn gate refuses an unbooked spawn, live",
        operator_key="e2e_e32_live_spawn_gate_refusal",
        instruction=(
            "P5. In the e2e session the operator types: \"Spawn a subagent (Agent tool) that runs 'echo e2e' -- "
            "do not book anything first.\" PASS: the tool call is surfaced as BLOCKED with the gate's own message, "
            "which contains the literal fix command `trialerror budget book`, AND "
            "$TE events tail --type hook_alive shows a hook_alive{spawn_gate} row for the live session. Outcome "
            "alone cannot distinguish the matcher firing from never firing -- both halves are required. "
            "Offline proxy: the clean-checkout smoke's spawn_gate_refusal_no_token step."
        ),
    ),
    _spec(
        "E-33", "P5", "operator", "booked spawn + post-task return + reconcile, live",
        operator_key="e2e_e33_live_booked_spawn_and_return",
        instruction=(
            "P5, in order, typed into the e2e session: (1) Run: trialerror --program-root <P> --platform-root <PP> "
            "budget book --session-id <the open session id> --program-id <run-id> --agent-kind e2e-probe "
            "--model-class small --model haiku --purpose mechanical --est-tokens 200 ; (2) Spawn a subagent "
            "(Agent tool) whose prompt STARTS with the `launch_id:` line from that booking and asks it to reply "
            "with the single word done ; (3) Run: trialerror ... budget reconcile --launch-id <id> "
            "--actual-tokens 150. PASS: the spawn proceeds, $TE events tail --type subagent_return shows exactly "
            "ONE row whose launch_id equals the booked id, hook_alive{post_task} exists for the session, and the "
            "launch is observed RUNNING between spawn and reconcile and RECONCILED after. Also capture the real "
            "tool_input shape from the pane and record whether it carried prompt/description. "
            "NOTE on --program-id: the run id ALREADY begins with `e2e-`, and the scratch program the journeys "
            "created is named <run-id> itself, so `e2e-<run-id>` would both double the prefix and label the "
            "launch row with a program that does not exist. "
            "Offline proxy: this suite's E-20 (same two hook scripts, synthetic stdin)."
        ),
    ),
    _spec(
        "E-34", "P5", "operator", "(ADVISORY) Stop hook blocks once", gating=False,
        operator_key="e2e_e34_live_stop_hook",
        instruction=(
            "P5, ADVISORY (Claude Code UI behaviour with no mechanical row). Run from the tmux pane, not the "
            "phone. Book one more launch WITHOUT spawning it, then end the turn / let the session stop; expect "
            "the checklist message ONCE naming the dangling launch, and a second stop allowed (never trapping "
            "the user). Then reconcile that launch with --actual-tokens 0 --reconcile-source manual."
        ),
    ),
    _spec(
        "E-35", "P5", "orchestrator", "close the live session; zero dangling; no leak into the live platform",
        operator_key="e2e_e35_close_live_session_and_leak_check",
        instruction=(
            "P5. $TE session close --course-check '{\"rungs\":\"e2e live\",\"build_vs_theory\":\"build\","
            "\"drift_flag\":false}' --notes \"e2e ${RUN} live session\" ; then, AS NODE and read-only:\n"
            "  SQLRO \"${PP}/platform.db\" \"select count(*) from launch where state in ('PROVISIONAL','RUNNING');\"\n"
            "  SQLRO /workspace/platform/platform.db 'select count(*) from launch;'\n"
            "PASS: close ok with a handoff path (never --override-ruling-id), 0 scratch dangling, and the live "
            "launch count EQUAL to the integer E-00 recorded -- that third line is the gating proof that nothing "
            "in P2..P5 wrote to the live platform. The claude process stays up for P6."
        ),
    ),
    _spec(
        "E-40", "P6", "operator", "the phone sees the container's session",
        operator_key="e2e_e40_phone_sees_the_session",
        instruction=(
            "P6. The operator opens the mobile app, finds the session named <run-id>, and reads its last two "
            "lines into chat; the orchestrator runs docker exec -u node <container> tmux -L te capture-pane -p "
            "-t main:e2e | tail -5. PASS: the operator's two lines appear in the pane capture (trailing "
            "whitespace ignored). Recorded `pending-operator` -- never `pass` -- when no operator is present."
        ),
    ),
    _spec(
        "E-41", "P6", "operator", "a command sent from the phone lands in the container",
        operator_key="e2e_e41_phone_command_lands",
        instruction=(
            "P6. The operator invents a 6-character nonce ON THE PHONE (never announced in the terminal or chat "
            "first) and sends: Run exactly: trialerror --program-root <P> --platform-root <PP> events append "
            "--type e2e_phone_ping --payload '{\"nonce\":\"<nonce>\"}' --session-id <live session id>. The "
            "orchestrator notes the send time, then reads $TE events tail --type e2e_phone_ping --limit 5 and "
            "the pane capture. PASS: one row whose payload.nonce equals the nonce and whose ts follows the send, "
            "AND the pane shows the container's session running exactly that command. NOTE: `events append` "
            "stores --session-id verbatim and never derives it -- session_id is a label on this row, never "
            "evidence. Recorded `pending-operator` when no operator is present."
        ),
    ),
    _spec(
        "E-42", "P6", "operator", "(ADVISORY) simultaneous browser + phone viewers", gating=False,
        operator_key="e2e_e42_simultaneous_viewers",
        instruction=(
            "P6, ADVISORY, a recording rather than a criterion (the behaviour is undocumented). With the phone "
            "attached, open the same session in a desktop browser and send `print the run-id` from both within "
            "a minute; record what happens (both see both? one detaches?)."
        ),
    ),
    _spec("E-50", "P7", "journey", "offload enqueue (C-enq, partial)", journey="e2e_offload_enqueue"),
    _spec(
        "E-51", "P7", "operator", "DEV OFF for >= 30 minutes (C-off)",
        operator_key="e2e_e51_dev_off_window",
        instruction=(
            "P7. The operator powers the GPU workstation off (or fully disconnects it) for >= 30 minutes. While "
            "it is off, four things are evidenced: (1) remote view + control still work -- repeat E-40/E-41 from "
            "the phone with a NEW nonce and payload {\"nonce\":..., \"dev\":\"off\"}; (2) from the phone, in the "
            "e2e session, Run: curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8850/dashboard/api/all "
            "-> 200; (3) the sandbox's OWN loop keeps deferring without escalating -- at least TWICE, >= 5 min "
            "apart, run the same cycle the supervisor runs for the live program, but against the OFFLOAD scratch "
            "program: jobs tick; offload reclaim; offload kick; jobs start-worker --foreground --mode loop "
            "--max-idle-polls 3 (each verb with BOTH scratch roots). Read `jobs list --state pending` (there is "
            "no `deferred` STATE -- only `deferred` ledger EVENTS) and `jobs logs <job_id>` before the first "
            "cycle and after the last; (4) AFTER the machine returns, from <sandbox-host>: tail -n 20 "
            "<deploy-root>/logs/mirror.cron.log shows >= 1 run inside the window, and note whether the newest "
            "snapshot is newer than the window start (advisory when the window was < 60 min). "
            "PASS: both phone rows with \"dev\":\"off\"; 200; attempts UNCHANGED across the window with >= 2 new "
            "`deferred` events and NO retry_scheduled/abandoned/complete, and `doctor --only offload_backlog` "
            "= warn (never pass while the job waits); the mirror-log line. "
            "'Attempts unchanged because nothing ran at all' is excluded by requiring the >= 2 loop cycles."
        ),
    ),
    _spec(
        "E-52", "P7", "journey", "DEV ON: one GPU embed job round-trips (C-e2e, embed half)",
        journey="e2e_offload_roundtrip",
        operator_key="e2e_e52_dev_worker_publishes_then_verify",
        instruction=(
            "P7, the DEV half (the sandbox-side verification is the automated `--suite e2e --phase "
            "offload-verify` journey). On the GPU workstation, after power-on, run the offload worker pointed at "
            "the SCRATCH offload root -- never the live program's -- and let it publish exactly one job; the "
            "worker process must exit 0 with one job published. The forced command on the sandbox side has to "
            "serve that scratch root for the duration: that is an OPERATOR edit of one authorized-keys line, "
            "never an agent's. Then on <sandbox-host>:\n"
            "  $TEOFF offload reclaim; $TEOFF offload kick\n"
            "  docker exec ... trialerror --program-root ${POFF} --platform-root ${PP} jobs start-worker "
            "--foreground --mode loop --max-idle-polls 3\n"
            "  docker exec ... trialerror accept --suite e2e --phase offload-verify --run-id ${RUN} "
            "--program-root ${POFF} --platform-root ${PP} --job-id <job_id> --chunk-count <n>\n"
            "(the jobs tmux window ticks the LIVE program only, so the e2e runs its own loop for the scratch "
            "offload program). PASS: the worker process exits 0 with exactly one job published, AND the verify "
            "journey's frozen set holds -- completion within 30 min of the publish, emb rows == chunk count, a "
            "real (non-fake) model key at the configured dimensionality, the document indexed, and the four "
            "offload/ingest doctor checks green. A worker that 'completed' with fake vectors is excluded by the "
            "model-key and dimensionality assertions; a job recomputed locally instead is impossible under the "
            "offload program's `require_real_backends = true`, which is exactly why it carries that flag."
        ),
    ),
    _spec(
        "E-53", "P7", "operator", "(ADVISORY) OCR round trip", gating=False,
        operator_key="e2e_e53_ocr_round_trip",
        instruction=(
            "P7, ADVISORY. The same protocol as E-52 with one image-only PDF supplied by the operator (the repo "
            "ships no scanned fixture, and this build did not add a synthetic image-only PDF helper -- see the "
            "implementation note). PASS if run: document.ocr_backend is the real OCR backend, page_count > 0, "
            "anchors resolve. Recorded `skip` with this reason when no fixture is supplied."
        ),
    ),
    _spec(
        "E-60", "P8", "orchestrator", "cleanup, window list restored, leak check",
        operator_key="e2e_e60_cleanup_and_leak_check",
        instruction=(
            "P8, and ONLY after E-61 has copied the run record out of the container. From <sandbox-host>:\n"
            "  docker exec -u node <container> tmux -L te send-keys -t main:e2e \"/exit\" Enter; sleep 5\n"
            "  PG -fc \"[c]laude --remote-control --name ${RUN}\"      # expect 0\n"
            "  docker exec -u node <container> tmux -L te kill-window -t main:e2e   # only THIS window, ever\n"
            "  docker exec -u node <container> tmux -L te list-windows -t main -F '#{window_name}'\n"
            "  PG -fc '[b]in/trialerror dashboard serve --foreground'  # expect 1\n"
            "  PG -fa -- \"--port <E-17 port>\"                        # expect no output\n"
            "  PG -fc '[b]in/trialerror jobs start-worker'             # expect <= 1, as at E-00\n"
            "  SQLRO /workspace/platform/platform.db 'select count(*) from launch;'   # expect E-00's integer\n"
            "  curl -s -o /dev/null -w '%{http_code}\\n' http://127.0.0.1:8850/dashboard/api/all   # expect 200\n"
            "  <deploy-root>/te-status.sh    # no item FAIL that was OK|WARN at E-00\n"
            "  docker exec <container> rm -rf \"${E2E}\"   # LAST, from the host, never from inside a session\n"
            "PASS: every annotated expectation above, compared item by item against what E-00 recorded -- the "
            "accepted window list restored, no process still holding the dashboard journey's port, the live "
            "launch count unchanged, and no te-status item newly FAIL. Leaving the extra window in place, or "
            "reading '0 claude processes' off a self-matching pgrep, are the two hollow passes this excludes."
        ),
    ),
    _spec(
        "E-61", "P8", "orchestrator", "run record exported and committed",
        operator_key="e2e_e61_run_record",
        instruction=(
            "P8, BEFORE E-60's rm -rf. docker exec -u node <container> mkdir -p \"${REC}\" ; "
            "$TE events export --type e2e_check --out \"${REC}/${RUN}.e2e_check.jsonl\" (--out is REQUIRED and a "
            "`>` redirect after `docker exec` would be the HOST shell's) ; docker cp the file out (or read it "
            "straight from the bind mount -- same bytes) ; sha256sum it into the commit message. The record is "
            "committed to the RESEARCH PROGRAM's repository, not the harness repository. PASS: one row per "
            "catalogue id (blocked/skip rows included) and plan_blob matching the plan's blob hash at run time. "
            "`trialerror accept --suite e2e --phase report` lists every catalogue id with its recorded status "
            "or MISSING -- run it first and close any MISSING row."
        ),
    ),
)

#: check_id -> spec, for the report phase and the recorder's own validation.
_CATALOGUE_BY_ID: dict[str, E2ECheckSpec] = {s.check_id: s for s in E2E_CHECK_CATALOGUE}

#: The plan's human steps, keyed the way ``GPU_LIVE_CC_ITEMS`` is: key = the
#: check id in lower snake form, value = the exact command block + criterion.
#: ``trialerror accept --suite e2e`` lists these as ``skip`` entries so a
#: reader of the CLI's output sees the whole e2e surface, automated and not.
E2E_OPERATOR_ITEMS: dict[str, str] = {
    s.operator_key: f"[{s.check_id} · {s.phase} · {s.title}] {s.instruction}"
    for s in E2E_CHECK_CATALOGUE
    if s.operator_key is not None and s.instruction is not None
}


def automated_check_ids() -> tuple[str, ...]:
    """Every ``E-nn`` id the plan marks automated, in catalogue order."""
    return tuple(s.check_id for s in E2E_CHECK_CATALOGUE if s.by == "journey")


def journey_check_ids(journey: str) -> tuple[str, ...]:
    """The ids one journey function is responsible for recording."""
    return tuple(s.check_id for s in E2E_CHECK_CATALOGUE if s.journey == journey)


def e2e_check_sequence() -> list[dict[str, Any]]:
    """The whole catalogue as plain dicts, in plan order -- what the CLI's
    ``--phase report`` joins recorded rows against, and the enumeration a
    reader can diff against the plan's Section 2 sequence."""
    return [
        {
            "check_id": s.check_id,
            "phase": s.phase,
            "by": s.by,
            "journey": s.journey,
            "title": s.title,
            "gating": s.gating,
            "operator_key": s.operator_key,
        }
        for s in E2E_CHECK_CATALOGUE
    ]


def e2e_operator_enumeration() -> list[CheckResult]:
    """The operator/orchestrator items as always-``skip`` CheckResults, the
    shape ``trialerror accept``'s doctor-shaped summary already speaks (the
    ``gpu_and_live_cc_enumeration`` pattern)."""
    return [
        CheckResult(name=key, category="e2e_operator", status="skip", message=message)
        for key, message in E2E_OPERATOR_ITEMS.items()
    ]


# ===========================================================================
# capability probes
# ===========================================================================
@dataclass
class Capabilities:
    """What the journeys are allowed to assert on this deployment.

    Each flag gates a lane-dependent check; a false flag makes the dependent
    step ``blocked`` (owner named) and the journey ``warn`` -- never ``pass``.
    """

    tantivy_pkg: bool = False
    tantivy_cli: bool = False
    translator: bool = False
    offload: bool = False
    plan_blob: str | None = None

    @property
    def tantivy(self) -> bool:
        """Both halves: the package importable AND the repair verb reachable."""
        return self.tantivy_pkg and self.tantivy_cli

    def to_dict(self) -> dict[str, Any]:
        return {
            "tantivy_pkg": self.tantivy_pkg,
            "tantivy_cli": self.tantivy_cli,
            "translator": self.translator,
            "offload": self.offload,
            "plan_blob": self.plan_blob,
        }


def _console_script() -> str | None:
    """The ``trialerror`` console script, preferring the one beside the running
    interpreter (a venv's own) over whatever PATH happens to resolve. Returned
    as a plain string so the caller can put it in argv[0]; ``None`` when no
    console script is installed and ``python -m trialerror.cli`` is the only
    entry point."""
    bindir = Path(sys.executable).parent
    for name in ("trialerror.exe", "trialerror"):
        candidate = bindir / name
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("trialerror")
    return found


def _cli_argv(*, console: bool = False) -> list[str]:
    """argv prefix for a ``trialerror`` subprocess.

    ``console=True`` asks for the console script specifically -- the dashboard
    serve subprocess needs it so the running process matches the
    ``bin/trialerror dashboard serve --foreground`` pattern E-18/E-60 count
    with ``pgrep``; ``python -m trialerror.cli`` would never match that name.
    Everywhere else the module form is preferred: it cannot resolve a
    different checkout than this process did.
    """
    if console:
        script = _console_script()
        if script is not None:
            return [script]
    return [sys.executable, "-m", "trialerror.cli"]


def _subprocess_env(platform_root: Path | str | None) -> dict[str, str]:
    """The environment EVERY subprocess in this module runs with.

    ``TRIALERROR_PLATFORM_ROOT`` because several CLI groups open their store
    without threading ``--platform-root`` through (``law``, ``feed``), so the
    flag alone would let a scratch run touch the caller's real platform root.
    ``PYTHONPATH`` pinned at the parent of the RUNNING ``trialerror`` package so
    a console script (whose sys.path has no cwd entry) resolves this checkout
    and not a different editable install of the same distribution.
    """
    env = dict(os.environ)
    if platform_root is not None:
        env["TRIALERROR_PLATFORM_ROOT"] = str(platform_root)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{_PACKAGE_PARENT}{os.pathsep}{existing}" if existing else str(_PACKAGE_PARENT)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _run_cli(
    args: Sequence[str],
    *,
    program_root: Path | None,
    platform_root: Path | None,
    cwd: Path | None = None,
    timeout: float = 300.0,
    global_roots: bool = True,
    console: bool = False,
) -> tuple[dict[str, Any], subprocess.CompletedProcess]:
    """Run one ``trialerror`` CLI verb as a REAL subprocess and parse its
    envelope. Returns ``(envelope, completed_process)``; a non-JSON stdout
    raises, because an unparseable envelope from a CLI whose whole contract is
    "every command emits an AgentEnvelope" is a failure, not a soft signal."""
    argv = _cli_argv(console=console)
    if global_roots:
        if program_root is not None:
            argv += ["--program-root", str(program_root)]
        if platform_root is not None:
            argv += ["--platform-root", str(platform_root)]
    argv += list(args)
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=_subprocess_env(platform_root),
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
    )
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{' '.join(argv[-4:])!r} produced no JSON envelope (rc={proc.returncode}): "
            f"stdout={proc.stdout[:400]!r} stderr={proc.stderr[:400]!r}"
        ) from exc
    return envelope, proc


def probe_capabilities(*, repo_root: Path | None = None, timeout: float = 60.0) -> Capabilities:
    """Which lanes this deployment actually carries (plan E-00's four probes),
    measured by invoking the verbs rather than by reading a version string.

    Each ``--help`` call is a real subprocess of the same entry point the
    journeys will use, so a lane that is importable but not wired into the CLI
    reads as absent -- which is the honest answer for a check that will drive
    that CLI.
    """
    repo_root = Path(repo_root) if repo_root is not None else _REPO_ROOT

    def _help_ok(args: Sequence[str]) -> bool:
        argv = _cli_argv() + list(args) + ["--help"]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, env=_subprocess_env(None), timeout=timeout
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    plan_blob: str | None = None
    try:
        blob = subprocess.run(
            ["git", "rev-parse", f"HEAD:{PLAN_PATH}"],
            capture_output=True, text=True, cwd=str(repo_root), timeout=30,
        )
        if blob.returncode == 0 and blob.stdout.strip():
            plan_blob = blob.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        plan_blob = None

    return Capabilities(
        tantivy_pkg=importlib.util.find_spec("tantivy") is not None,
        tantivy_cli=_help_ok(["ingest", "reindex-fulltext"]),
        translator=_help_ok(["feed", "translate"]),
        offload=_help_ok(["offload"]),
        plan_blob=plan_blob,
    )


# ===========================================================================
# the evidence row
# ===========================================================================
class E2ERecorder:
    """Owns ``run_id`` + ``plan_blob`` and writes one ``e2e_check`` event per
    check id, the moment the step(s) behind that id finish.

    ONE recorder per journey, and a journey never records another journey's id:
    E-23 tails the events table expecting the corpus and dashboard rows to be
    IN ``ops.db`` already, which is only true if each journey wrote its own as
    it went (plan Section 2's evidence convention, restated in Section 7.1).

    The collected rows are also kept in memory so the CLI can print them
    without a second read.
    """

    VALID_STATUSES = ("pass", "fail", "blocked", "skip")

    def __init__(self, *, run_id: str, plan_blob: str | None = None, by: str = "journey") -> None:
        self.run_id = run_id
        self.plan_blob = plan_blob
        self.by = by
        self.session_id: str | None = None
        self.rows: list[dict[str, Any]] = []

    def record(
        self,
        store: Any,
        check_id: str,
        status: str,
        *,
        by: str | None = None,
        evidence: Mapping[str, Any] | None = None,
        owner: str | None = None,
    ) -> dict[str, Any]:
        if status not in self.VALID_STATUSES:
            raise ValueError(f"e2e_check status must be one of {self.VALID_STATUSES!r}, got {status!r}")
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "check_id": check_id,
            "status": status,
            "by": by or self.by,
            "evidence": dict(evidence or {}),
            "plan_blob": self.plan_blob,
        }
        if owner is not None:
            payload["owner"] = owner
        elif status in ("blocked", "fail"):
            payload["owner"] = "unassigned"
        from trialerror.events.api import append_event

        row = append_event(
            store, event_type=E2E_CHECK_EVENT_TYPE, session_id=self.session_id, payload=payload
        )
        self.rows.append(payload)
        return row


def read_recorded_checks(store: Any, *, run_id: str | None = None) -> dict[str, dict[str, Any]]:
    """Every ``e2e_check`` row in this program's ``ops.db``, keyed by check id
    (last write wins -- a re-run of one check supersedes its earlier row).
    ``run_id`` filters to one run."""
    from trialerror.events.api import export_events

    out: dict[str, dict[str, Any]] = {}
    for row in export_events(store, event_type=E2E_CHECK_EVENT_TYPE):
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        if run_id is not None and payload.get("run_id") != run_id:
            continue
        check_id = payload.get("check_id")
        if isinstance(check_id, str):
            out[check_id] = payload
    return out


# ===========================================================================
# shared helpers
# ===========================================================================
def _sql_count(conn: Any, sql: str, params: Sequence[Any] = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def _launch_states(store: Any) -> dict[str, str]:
    """``{launch_id: state}`` for the whole platform ledger.

    A COUNT would not notice a state transition on an existing row, and
    "no launch row CHANGED STATE" is what E-19(a)'s criterion actually says.
    """
    return {r["launch_id"]: r["state"] for r in store.platform.execute("SELECT launch_id, state FROM launch")}


def _program_is_scaffolded(program_root: Path) -> bool:
    """Whether ``program_root`` is a program at all.

    ``program init`` writes ``trialerror.toml`` and refuses a second run when
    it already exists, so its presence is the same test the CLI itself makes.
    Checking it BEFORE ``open_store`` matters: ``open_store`` would otherwise
    create and migrate a whole scaffold at a mistyped root, and the phase would
    then fail for a confusing reason having already made a mess.
    """
    return (Path(program_root) / "trialerror.toml").is_file()


def _open_session_row(store: Any) -> dict[str, Any]:
    row = store.ops.execute("SELECT * FROM session WHERE status='open'").fetchone()
    if row is None:
        raise RuntimeError("no OPEN session in this program (the corpus journey boots it; run --phase corpus first)")
    return dict(row)


def _open_scratch_store(program_root: Path, platform_root: Path) -> Any:
    """``open_store`` for a phase that expects the corpus journey to have run
    already -- refusing an unscaffolded root BEFORE anything is created.

    Kept separate from :func:`_open_session_row` on purpose: the caller binds
    the store from this call and reads the session with the next one, so a
    program with no OPEN session still leaves the caller holding an open store
    to record its ``fail`` row into and to close in ``finally``.
    """
    from trialerror.stores.store import open_store

    if not _program_is_scaffolded(program_root):
        raise RuntimeError(
            f"{program_root} is not a program scaffold (no trialerror.toml) -- check the --program-root spelling; "
            "the corpus journey (`--phase corpus`) is what creates it"
        )
    return open_store(program_root, platform_root=platform_root)


def _ensure_pool(store: Any, *, account_id: str, model_class: str = "top", cap_tokens: int = 1_000_000) -> str:
    """Create the journeys' budget pool once. Idempotent, because the corpus,
    dashboard and ops journeys run as three separate processes against one
    program and only the first of them finds an empty pool table."""
    from trialerror.budget.pools import create_pool

    row = store.platform.execute(
        "SELECT pool_id FROM budget_pool WHERE account_id = ? AND model_class = ? ORDER BY period_start DESC LIMIT 1",
        (account_id, model_class),
    ).fetchone()
    if row is not None:
        return row["pool_id"]
    return create_pool(
        store, account_id=account_id, model_class=model_class, period="weekly", cap_tokens=cap_tokens
    )["pool_id"]


def _launch_state(store: Any, launch_id: str) -> str | None:
    row = store.platform.execute("SELECT state FROM launch WHERE launch_id=?", (launch_id,)).fetchone()
    return row["state"] if row else None


def hold_journey_launch(
    store: Any,
    *,
    session_id: str,
    agent_kind: str,
    program_root: Path,
    platform_root: Path,
    program_id: str,
    est_tokens: int = 500,
) -> dict[str, Any]:
    """The ONE shape every journey launch takes: book -> consume through the
    REAL ``spawn_gate`` hook subprocess -> return through the REAL
    ``post_task`` hook subprocess. The journey reconciles it as its last step.

    Net effect per journey: one consumed booking and exactly one
    ``subagent_return`` event under the same session. That balance is why the
    doctor's ``spawns_vs_bookings`` reconciliation can be REQUIRED to pass at
    E-16/E-20 rather than merely tolerated -- ``reconcile_launch`` accepts a
    still-PROVISIONAL launch, so "booked for attribution, reconciled at the
    end, never spawned" is a legal shape that would silently break that
    criterion. Nothing here bypasses either hook subprocess.
    """
    from trialerror.budget.pools import book_launch

    booked = book_launch(
        store,
        session_id=session_id,
        program_id=program_id,
        agent_kind=agent_kind,
        model_class="top",
        model="sonnet",
        purpose="mechanical",
        est_tokens=est_tokens,
    )
    if not booked.ok or booked.state != "PROVISIONAL":
        raise RuntimeError(f"book_launch for {agent_kind!r} not PROVISIONAL (state={booked.state}): {booked.reason}")
    launch_id = booked.launch_id
    states_seen = ["PROVISIONAL"]

    tool_input = {"prompt": f"launch_id: {launch_id}\ncarry out the {agent_kind} journey's work"}
    consumed = _run_hook_script(
        _SPAWN_GATE_HOOK,
        {"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_input": tool_input, "cwd": str(program_root)},
        platform_root=platform_root,
    )
    if consumed.returncode != 0:
        raise RuntimeError(f"spawn_gate refused the journey launch (exit {consumed.returncode}): {consumed.stderr!r}")
    state = _launch_state(store, launch_id)
    if state != "RUNNING":
        raise RuntimeError(f"launch {launch_id} is {state!r} after a consumed spawn, expected RUNNING")
    states_seen.append("RUNNING")

    returned = _run_hook_script(
        _POST_TASK_HOOK,
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Agent",
            "tool_input": tool_input,
            "tool_response": {"content": "done"},
            "cwd": str(program_root),
        },
        platform_root=platform_root,
    )
    if returned.returncode != 0:
        raise RuntimeError(f"post_task exited {returned.returncode}: {returned.stderr!r}")

    rows = store.ops.execute(
        "SELECT event_id, session_id FROM event WHERE type='subagent_return' AND launch_id=?", (launch_id,)
    ).fetchall()
    if len(rows) != 1:
        raise RuntimeError(f"expected exactly one subagent_return for {launch_id}, got {len(rows)}")
    if rows[0]["session_id"] != session_id:
        raise RuntimeError(
            f"subagent_return landed under session {rows[0]['session_id']!r}, expected {session_id!r}"
        )
    hook_alive = _sql_count(
        store.ops,
        "SELECT COUNT(*) FROM event WHERE type='hook_alive' AND session_id=? AND payload LIKE '%post_task%'",
        (session_id,),
    )
    if hook_alive < 1:
        raise RuntimeError("post_task ran but recorded no hook_alive{post_task} row for this session")

    return {
        "launch_id": launch_id,
        "subagent_return_event_id": rows[0]["event_id"],
        "states_seen": states_seen,
        "agent_kind": agent_kind,
    }


def _dangling_counts(store: Any, session_id: str) -> dict[str, int]:
    """PROVISIONAL|RUNNING launches, session-scoped and platform-wide -- the
    two numbers E-20 reads before and after its reconcile. Read from
    ``platform.db`` with the same SQL the plan names, so "0 dangling" cannot be
    satisfied by the TTL-based doctor check's silence."""
    return {
        "session": _sql_count(
            store.platform,
            "SELECT COUNT(*) FROM launch WHERE session_id = ? AND state IN ('PROVISIONAL','RUNNING')",
            (session_id,),
        ),
        "platform": _sql_count(
            store.platform, "SELECT COUNT(*) FROM launch WHERE state IN ('PROVISIONAL','RUNNING')"
        ),
    }


def _toml_table(name: str, values: Mapping[str, Any]) -> str:
    lines = [f"[{name}]"]
    for key, value in values.items():
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (int, float)):
            rendered = str(value)
        elif isinstance(value, (list, tuple)):
            rendered = "[" + ", ".join(json.dumps(v) for v in value) + "]"
        else:
            rendered = json.dumps(str(value))
        lines.append(f"{key} = {rendered}")
    return "\n".join(lines) + "\n"


def _write_scratch_toml(
    program_root: Path,
    *,
    ocr: str,
    embed: str,
    fulltext_backend: str | None = None,
    translator: str | None = None,
    require_real_backends: bool = False,
    extra_paths: Mapping[str, Any] | None = None,
    embed_extra: Mapping[str, Any] | None = None,
    ocr_extra: Mapping[str, Any] | None = None,
) -> list[str]:
    """Append the e2e's own tables to the toml ``program init`` generated, then
    re-parse the file to prove it is still valid TOML.

    The backends are written EXPLICITLY even when the value equals the
    documented default: the record of a run should say which backend served it,
    not require the reader to know what an absent table falls back to.
    """
    path = program_root / "trialerror.toml"
    text = path.read_text(encoding="utf-8")
    written: list[str] = []
    blocks: list[str] = ["\n# --- e2e scratch program (generated by trialerror.accept.e2e) ---\n"]

    paths_table: dict[str, Any] = {"ingest_roots": ["raw"]}
    if extra_paths:
        paths_table.update(extra_paths)
    blocks.append(_toml_table("paths", paths_table))
    written.append("paths")

    if require_real_backends:
        blocks.append(_toml_table("ingest", {"require_real_backends": True}))
        written.append("ingest")

    blocks.append(_toml_table("ingest.ocr", {"backend": ocr, **dict(ocr_extra or {})}))
    written.append("ingest.ocr")
    blocks.append(_toml_table("ingest.embed", {"backend": embed, **dict(embed_extra or {})}))
    written.append("ingest.embed")

    if fulltext_backend is not None:
        blocks.append(_toml_table("retrieve", {"fulltext_backend": fulltext_backend}))
        written.append("retrieve")
    if translator is not None:
        blocks.append(_toml_table("feed.translator", {"backend": translator}))
        written.append("feed.translator")

    path.write_text(text + "\n".join(blocks), encoding="utf-8")

    import tomllib

    with open(path, "rb") as fh:
        tomllib.load(fh)  # raises TOMLDecodeError if the append broke the file
    return written


_FULLTEXT_LINE_RE = re.compile(r'(?m)^fulltext_backend = ".*"$')


def _set_fulltext_backend(program_root: Path, backend: str) -> None:
    """Rewrite the ACTIVE ``[retrieve] fulltext_backend`` line (the generated
    template also carries a commented-out one, which this deliberately does not
    match). The engine re-reads the toml per call, so this is the whole toggle
    E-14 needs."""
    path = program_root / "trialerror.toml"
    text = path.read_text(encoding="utf-8")
    if _FULLTEXT_LINE_RE.search(text):
        text = _FULLTEXT_LINE_RE.sub(f'fulltext_backend = "{backend}"', text)
    else:
        text += "\n" + _toml_table("retrieve", {"fulltext_backend": backend})
    path.write_text(text, encoding="utf-8")


def _long_synthetic_markdown(n_sections: int, *, run_id: str) -> str:
    """Deterministic, explicitly-labelled synthetic prose, long enough that the
    chunker yields one chunk per section.

    Not a real document and not anyone's copyrighted text: the banner says so
    on line 2, and every sentence below is generated from the section index.
    Each section carries comfortably more than the chunker's
    ``MIN_STANDALONE_TOKENS`` floor and far less than ``MAX_CHUNK_TOKENS``, so
    sections neither recombine nor split and the chunk count is predictable.
    """
    head = (
        f"# Synthetic offload fixture {run_id}\n\n"
        "> **Synthetic fixture, machine-generated.** Every sentence below was produced by "
        "`trialerror.accept.e2e` to give the offload queue a document with enough chunks to "
        "exercise a real embedding batch. It is not a real paper, quotes no source, and "
        "reports no real result.\n"
    )
    parts = [head]
    for i in range(n_sections):
        parts.append(
            f"\n## Section {i:04d}\n\n"
            f"This is generated paragraph {i:04d} of the synthetic offload fixture. "
            f"It exists so that section {i:04d} carries enough tokens to stand on its own as one chunk "
            f"without being recombined with its neighbour by the boundary-aware chunker. "
            f"The sentence structure repeats deliberately, because a deterministic fixture is worth more "
            f"to an acceptance run than a varied one: the chunk count must be the same on every machine "
            f"and on every re-run of this journey. "
            f"Marker token seven-{i:04d}-alpha appears exactly once in the whole document, which makes "
            f"section {i:04d} addressable by a lexical query if a later check ever needs to name it. "
            f"Nothing in this paragraph asserts anything about the world.\n"
        )
    return "".join(parts)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


_TOKEN_META_RE = re.compile(r'<meta\s+name="dashboard-write-token"\s+content="([0-9a-f]+)"\s*>')


def _http_get(url: str, *, timeout: float = 10.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return int(resp.status), resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", errors="replace")


def _http_get_json(url: str, *, timeout: float = 20.0) -> tuple[int, Any]:
    status, body = _http_get(url, timeout=timeout)
    try:
        return status, json.loads(body)
    except json.JSONDecodeError:
        return status, body


def _http_post_json(
    url: str, payload: Mapping[str, Any], *, token: str | None = None, timeout: float = 20.0
) -> tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    if token is not None:
        from trialerror.dashboard.serve import WRITE_TOKEN_HEADER

        request.add_header(WRITE_TOKEN_HEADER, token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return int(resp.status), json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return int(exc.code), json.loads(raw)
        except json.JSONDecodeError:
            return int(exc.code), raw


def _wait_for_server(host: str, port: int, *, timeout_s: float = 15.0) -> None:
    deadline = time.time() + timeout_s
    last: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/", timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last = exc
            time.sleep(0.2)
    raise RuntimeError(f"dashboard server never came up on {host}:{port}: {last}")


def _scratch_meta_program_root_ok(meta: Mapping[str, Any], program_root: Path) -> bool:
    """Is the server we are talking to serving OUR scratch program?

    The one assertion that distinguishes a green dashboard journey from a green
    reading of the LIVE dashboard on its own port.
    """
    served = meta.get("program_root")
    if not served:
        return False
    try:
        return Path(str(served)).resolve() == Path(program_root).resolve()
    except (OSError, ValueError):
        return False


def _blocked(steps: list[dict[str, Any]], name: str, owner: str, reason: str) -> None:
    """Record one step as blocked -- ``ok`` so the journey continues, but with
    the marker that turns the journey's own status into ``warn``."""
    steps.append({"name": name, "ok": True, "detail": {"blocked": True, "owner": owner, "reason": reason}})


def _record_into(
    recorder: E2ERecorder,
    record_root: Path,
    platform_root: Path,
    check_id: str,
    status: str,
    **kwargs: Any,
) -> None:
    """Write one ``e2e_check`` row into a program's ``ops.db`` by ROOT rather
    than through an already-open store.

    The offload journeys (E-50/E-52) run against a SECOND scratch program while
    the run record -- and ``--phase report``, and E-61's export -- read the
    first one, so their rows have to be able to land somewhere other than the
    program they are testing. ``event.session_id`` is a foreign key into that
    program's own ``session`` table, so the row is written session-less and the
    offload session id travels in the evidence instead (``--session-id`` is a
    label on a row and never evidence, exactly as the plan says of it).

    This also opens the store, which is what lets the ``blocked`` early return
    record a row at all: at that point the journey has opened nothing.
    """
    from trialerror.stores.store import open_store

    saved = recorder.session_id
    store = open_store(record_root, platform_root=platform_root)
    try:
        recorder.session_id = None
        recorder.record(store, check_id, status, **kwargs)
    finally:
        recorder.session_id = saved
        store.close()


def _try_record_blocked(
    record: Any, check_id: str, owner: str, reason: str, record_root: Path
) -> dict[str, Any]:
    """Write the ``blocked`` row for a journey that is about to return early,
    and report whether it landed.

    Never raises: the row is evidence ABOUT an absent capability, and failing
    the capability check itself because the record could not be written would
    report the wrong thing. A row that did not land shows up as ``MISSING`` in
    ``--phase report``, which is loud in the place that matters.
    """
    detail: dict[str, Any] = {"check_id": check_id, "record_program_root": str(record_root)}
    try:
        record(check_id, "blocked", owner=owner, evidence={"reason": reason, "blocked": True})
        detail["recorded"] = True
    except Exception as exc:  # noqa: BLE001
        detail["recorded"] = False
        detail["error"] = f"{type(exc).__name__}: {exc}"
    return detail


def _finish(
    name: str, steps: list[dict[str, Any]], program_root: Path, extra: Mapping[str, Any] | None = None
) -> CheckResult:
    blocked = [s["name"] for s in steps if isinstance(s.get("detail"), dict) and s["detail"].get("blocked")]
    details: dict[str, Any] = {"steps": steps, "program_root": str(program_root)}
    if extra:
        details.update(extra)
    if blocked:
        details["blocked_steps"] = blocked
        return CheckResult(
            name=name, category="e2e", status="warn",
            message=f"{len(steps)} step(s) ran; {len(blocked)} blocked on an absent capability: {blocked}",
            details=details,
        )
    return CheckResult(
        name=name, category="e2e", status="pass", message=f"all {len(steps)} step(s) passed", details=details
    )


def _failed(name: str, exc: AcceptanceStepError, steps: list[dict[str, Any]], program_root: Path) -> CheckResult:
    return CheckResult(
        name=name, category="e2e", status="fail",
        message=f"step {exc.step!r} failed: {exc.detail}",
        details={"steps": steps, "program_root": str(program_root)},
    )


def _program_init(
    program_root: Path, platform_root: Path, *, program_id: str
) -> dict[str, Any]:
    """``trialerror program init`` as a real subprocess, twice: the second run
    must be REFUSED with ``already_scaffolded``.

    ``program init`` is the operator's own first command and the clean-checkout
    smoke never runs it (it starts at ``open_store``), so this is the only place
    the scaffold's own CLI is proven.
    """
    envelope, _proc = _run_cli(
        ["program", "init", program_id, "--dir", str(program_root), "--platform-root", str(platform_root)],
        program_root=None, platform_root=platform_root, global_roots=False,
    )
    if not envelope.get("ok"):
        raise RuntimeError(f"program init refused: {envelope.get('error')}")
    second, _proc2 = _run_cli(
        ["program", "init", program_id, "--dir", str(program_root), "--platform-root", str(platform_root)],
        program_root=None, platform_root=platform_root, global_roots=False,
    )
    code = (second.get("error") or {}).get("code")
    if second.get("ok") or code != "already_scaffolded":
        raise RuntimeError(f"a second program init should refuse with already_scaffolded, got ok={second.get('ok')} code={code!r}")

    missing = [
        name for name in ("raw", "archive", "memory", "law", "handoffs", "artifacts", "requests")
        if not (program_root / name).is_dir()
    ]
    if missing:
        raise RuntimeError(f"program init left these scaffold dirs missing: {missing}")
    for db in ("knowledge.db", "ops.db", "jobs.db"):
        if not (program_root / "stores" / db).is_file():
            raise RuntimeError(f"program init did not migrate {db}")
    if not (platform_root / "platform.db").is_file():
        raise RuntimeError(f"program init did not migrate platform.db under {platform_root}")
    return {
        "program_id": program_id,
        "second_init_code": code,
        "stores_dir": str(program_root / "stores"),
    }


def _session_boot_cli_then_hook(
    program_root: Path, platform_root: Path, *, account_label: str = "e2e-harness"
) -> dict[str, Any]:
    """The production boot sequence, not the smoke's direct account insert:
    ``session boot --create-account`` through the real CLI, then the REAL
    SessionStart hook subprocess, which must REUSE that session rather than
    open a second one."""
    envelope, _proc = _run_cli(
        ["session", "boot", "--create-account", account_label],
        program_root=program_root, platform_root=platform_root,
    )
    if not envelope.get("ok"):
        raise RuntimeError(f"session boot refused: {envelope.get('error')}")
    result = envelope["result"]
    session_id = result["session_id"]
    account_id = result["account_id"]

    from trialerror.stores.store import open_store

    store = open_store(program_root, platform_root=platform_root)
    try:
        boot_pin_version = store.ops.execute(
            "SELECT boot_pin_version FROM session WHERE session_id=?", (session_id,)
        ).fetchone()["boot_pin_version"]
        open_before = [
            r["session_id"] for r in store.ops.execute("SELECT session_id FROM session WHERE status='open'")
        ]
    finally:
        store.close()
    if open_before != [session_id]:
        raise RuntimeError(f"expected exactly one open session {session_id!r} after boot, found {open_before}")

    hook = _run_hook_script(
        _SESSION_START_HOOK,
        {"session_id": f"e2e-{session_id}", "cwd": str(program_root),
         "hook_event_name": "SessionStart", "source": "startup"},
        platform_root=platform_root,
    )
    if hook.returncode != 0:
        raise RuntimeError(f"session_start.py exited {hook.returncode}: {hook.stderr!r}")
    if not hook.stdout.strip():
        raise RuntimeError("session_start.py exited 0 but emitted no boot bundle on stdout")

    store = open_store(program_root, platform_root=platform_root)
    try:
        open_after = [
            r["session_id"] for r in store.ops.execute("SELECT session_id FROM session WHERE status='open'")
        ]
        hook_alive = _sql_count(
            store.ops,
            "SELECT COUNT(*) FROM event WHERE type='hook_alive' AND session_id=? AND payload LIKE '%session_start%'",
            (session_id,),
        )
    finally:
        store.close()
    if open_after != [session_id]:
        raise RuntimeError(
            f"the SessionStart hook did not reuse the open session: before={[session_id]} after={open_after}"
        )
    if hook_alive < 1:
        raise RuntimeError("the SessionStart hook recorded no hook_alive{session_start} row for that session")

    return {
        "session_id": session_id,
        "account_id": account_id,
        "boot_pin_version": boot_pin_version,
        "hook_reused_open": True,
        "hook_stdout_bytes": len(hook.stdout),
    }


# ===========================================================================
# P2 -- the corpus journey
# ===========================================================================
def run_e2e_corpus(
    program_root: Path,
    platform_root: Path,
    *,
    run_id: str,
    repo_root: Path | None = None,
    caps: Capabilities | None = None,
) -> CheckResult:
    """E-10..E-16: a scratch program from ``program init`` to a green doctor.

    Records its own ``e2e_check`` row as each id's step(s) complete, and leaves
    the session OPEN -- the dashboard and ops journeys run against it, and E-24
    is what closes it.
    """
    repo_root = (Path(repo_root) if repo_root is not None else _REPO_ROOT).resolve()
    program_root = Path(program_root).resolve()
    platform_root = Path(platform_root).resolve()
    platform_root.mkdir(parents=True, exist_ok=True)
    caps = caps if caps is not None else probe_capabilities(repo_root=repo_root)

    steps: list[dict[str, Any]] = []
    recorder = E2ERecorder(run_id=run_id, plan_blob=caps.plan_blob)
    store = None
    program_id = run_id  # the run id is already `e2e-<shell clock>`; no second prefix

    try:
        # -- E-10 ---------------------------------------------------------
        with _step(steps, "program_init_cli"):
            init = _program_init(program_root, platform_root, program_id=program_id)
        steps.append({"name": "program_init_cli", "ok": True, "detail": init})

        with _step(steps, "write_scratch_toml"):
            tables = _write_scratch_toml(
                program_root,
                ocr="fake",
                embed="fake",
                fulltext_backend="tantivy" if caps.tantivy else None,
                translator="model" if caps.translator else None,
            )
        steps.append({"name": "write_scratch_toml", "ok": True, "detail": {"toml_tables_written": tables}})

        from trialerror.stores.store import open_store

        store = open_store(program_root, platform_root=platform_root)
        recorder.record(
            store, "E-10", "pass",
            evidence={"program_id": program_id, "toml_tables_written": tables,
                      "second_init_code": init["second_init_code"]},
        )

        # -- E-11 ---------------------------------------------------------
        with _step(steps, "session_boot_cli_then_hook"):
            boot = _session_boot_cli_then_hook(program_root, platform_root)
            session_id = boot["session_id"]
            account_id = boot["account_id"]
        steps.append({"name": "session_boot_cli_then_hook", "ok": True, "detail": boot})
        recorder.session_id = session_id
        recorder.record(store, "E-11", "pass", evidence=boot)

        # -- the journey's own launch, in the production shape -------------
        with _step(steps, "hold_journey_launch"):
            _ensure_pool(store, account_id=account_id)
            held = hold_journey_launch(
                store, session_id=session_id, agent_kind="e2e-corpus",
                program_root=program_root, platform_root=platform_root, program_id=program_id,
            )
            launch_id = held["launch_id"]
        steps.append({"name": "hold_journey_launch", "ok": True, "detail": held})

        # -- E-12 ---------------------------------------------------------
        with _step(steps, "ingest_fixture_slice"):
            e12 = _ingest_fixture_slice(store, program_root, launch_id=launch_id)
        steps.append({"name": "ingest_fixture_slice", "ok": True, "detail": e12})
        recorder.record(store, "E-12", "pass", evidence=e12)

        chunks = e12["chunks"]

        # -- E-13 ---------------------------------------------------------
        if not caps.tantivy:
            reason = (
                f"the tantivy lexical tier is not present on this deployment "
                f"(tantivy_pkg={caps.tantivy_pkg}, tantivy_cli={caps.tantivy_cli})"
            )
            _blocked(steps, "reindex_fulltext", "lane-d", reason)
            recorder.record(store, "E-13", "blocked", owner="lane-d", evidence={"reason": reason})
        else:
            with _step(steps, "reindex_fulltext"):
                e13 = _reindex_fulltext(program_root, platform_root, repo_root=repo_root, chunks=chunks)
            steps.append({"name": "reindex_fulltext", "ok": True, "detail": e13})
            recorder.record(store, "E-13", "pass", evidence=e13)

        # -- E-14 ---------------------------------------------------------
        if not caps.tantivy:
            reason = "the tantivy lexical tier is not present on this deployment"
            _blocked(steps, "backend_parity", "lane-d", reason)
            recorder.record(store, "E-14", "blocked", owner="lane-d", evidence={"reason": reason})
        else:
            with _step(steps, "backend_parity"):
                e14 = _backend_parity(store, program_root)
            steps.append({"name": "backend_parity", "ok": True, "detail": e14})
            recorder.record(store, "E-14", "pass", evidence=e14)

        # -- E-15 ---------------------------------------------------------
        with _step(steps, "retrieve_auto_citation_fence"):
            e15 = _retrieve_auto_citation_fence(store)
        steps.append({"name": "retrieve_auto_citation_fence", "ok": True, "detail": e15})

        with _step(steps, "corpus_stats"):
            from trialerror.retrieve import engine

            stats = engine.corpus_stats(store)
            if stats["documents"] != 4:
                raise RuntimeError(f"corpus_stats reports {stats['documents']} documents, expected 4")
            if stats["chunks_missing_fts"] != 0:
                raise RuntimeError(f"{stats['chunks_missing_fts']} chunk(s) missing from chunk_fts")
            corpus = {
                "documents": stats["documents"], "chunks": stats["chunks"],
                "chunks_missing_fts": stats["chunks_missing_fts"],
                "fulltext_backend": stats.get("fulltext_backend"),
            }
        steps.append({"name": "corpus_stats", "ok": True, "detail": corpus})
        recorder.record(store, "E-15", "pass", evidence={**e15, "corpus_stats": corpus})

        # -- E-16 ---------------------------------------------------------
        with _step(steps, "doctor_green"):
            e16 = _doctor_green(program_root, platform_root, repo_root=repo_root)
        steps.append({"name": "doctor_green", "ok": True, "detail": e16})
        recorder.record(store, "E-16", "pass", evidence=e16)

        # -- settle the journey's launch ----------------------------------
        with _step(steps, "reconcile_journey_launch"):
            from trialerror.budget.pools import reconcile_launch

            reconcile_launch(store, launch_id=launch_id, actual_tokens=450)
            final_state = _launch_state(store, launch_id)
            if final_state != "RECONCILED":
                raise RuntimeError(f"journey launch is {final_state!r} after reconcile, expected RECONCILED")
        steps.append({
            "name": "reconcile_journey_launch", "ok": True,
            "detail": {"launch_id": launch_id, "states_seen": [*held["states_seen"], "RECONCILED"]},
        })

    except AcceptanceStepError as exc:
        if store is not None:
            try:
                recorder.record(store, _failing_check_id(exc.step, "e2e_corpus"), "fail",
                                owner="harness", evidence={"step": exc.step, "error": exc.detail})
            except Exception:  # noqa: BLE001 - a recorder failure must not mask the real one
                pass
        return _failed("e2e_corpus", exc, steps, program_root)
    finally:
        if store is not None:
            store.close()

    return _finish("e2e_corpus", steps, program_root, {"run_id": run_id, "recorded": recorder.rows})


#: step name -> the check id whose row a failure is recorded against, so a
#: failed run still leaves an ``e2e_check`` row for the id that broke rather
#: than a silent gap the report phase reports as MISSING.
_STEP_TO_CHECK: dict[str, str] = {
    "program_init_cli": "E-10", "write_scratch_toml": "E-10",
    "session_boot_cli_then_hook": "E-11", "hold_journey_launch": "E-20",
    "ingest_fixture_slice": "E-12", "reindex_fulltext": "E-13", "backend_parity": "E-14",
    "retrieve_auto_citation_fence": "E-15", "corpus_stats": "E-15", "doctor_green": "E-16",
    # `open_scratch_session` is deliberately absent: it is the first step of BOTH
    # the dashboard and the ops journey, so its check id is the journey's own
    # fallback (E-17 / E-19) rather than a single hard-coded id.
    "serve_subprocess_up": "E-17", "index_has_write_token": "E-17", "api_all_corpus_panel": "E-17",
    "api_all_meta_program_root_is_scratch": "E-17", "api_search_evidence_anchor": "E-17",
    "api_search_fenced": "E-17", "api_feed_ok": "E-17",
    "create_e2e_thread": "E-70", "write_refused_without_token": "E-70",
    "write_refused_with_wrong_token": "E-70", "write_accepted_with_token": "E-70",
    "feed_post_row_visible": "E-70", "shutdown_clean": "E-70",
    "feed_thread_post": "E-19", "feed_translate_fail_closed": "E-19",
    "budget_book_spawn_return_reconcile": "E-20",
    "law_append_verify_with_stale_control": "E-21", "jobs_worker_cli_once": "E-22",
    "events_tail_types": "E-23", "session_close_and_handoff": "E-24",
    "doctor_green_after_close": "E-24", "record_prior_checks": "E-24",
    "write_offload_toml": "E-50", "ingest_long_synthetic_markdown": "E-50",
    "drain_until_embed_deferred": "E-50", "pending_manifest_present": "E-50",
    "job_complete": "E-52", "emb_rows_equal_chunks": "E-52", "model_key_is_real": "E-52",
    "dims_match": "E-52", "document_indexed": "E-52", "doctor_offload_checks": "E-52",
    "done_dir_observed": "E-52",
}

_JOURNEY_FALLBACK_CHECK: dict[str, str] = {
    "e2e_corpus": "E-10", "e2e_dashboard": "E-17", "e2e_ops": "E-19",
    "e2e_offload_enqueue": "E-50", "e2e_offload_roundtrip": "E-52",
}


def _failing_check_id(step: str, journey: str) -> str:
    return _STEP_TO_CHECK.get(step, _JOURNEY_FALLBACK_CHECK.get(journey, "E-00"))


def _ingest_fixture_slice(store: Any, program_root: Path, *, launch_id: str) -> dict[str, Any]:
    """E-12. The repo's own synthetic, explicitly-labelled demo documents (the
    three that give the corpus its license mix) plus ONE generated pdf-text
    file carrying the acceptance sentence -- the pdf route and the fence's
    restricted tier both exercised, and never a scanned or copyrighted book."""
    from trialerror.demo import content
    from trialerror.ingest import pipeline
    from trialerror.jobs.registry import discover_and_register_handlers
    from trialerror.jobs.worker import run_loop

    discover_and_register_handlers()
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    doc_ids: list[str] = []
    source_ids: dict[str, str] = {}
    for filename, source_kwargs, body in content.DOCUMENTS:
        source = pipeline.register_source(store, registered_by_launch=launch_id, **source_kwargs)
        source_ids[source_kwargs["license_tier"]] = source["source_id"]
        raw_path = raw_dir / filename
        raw_path.write_text(body, encoding="utf-8")
        added = pipeline.add_document(
            store, program_root=program_root, source_id=source["source_id"], raw_path=raw_path,
            created_by_launch=launch_id, config={}, yes=True,
        )
        doc_ids.append(added["document"]["doc_id"])

    pdf_source = pipeline.register_source(
        store, kind="paper", title="[E2E] Generated pdf-text fixture", license_tier="open",
        acquisition_route="user_delivered", registered_by_launch=launch_id, config={},
    )
    source_ids["pdf_open"] = pdf_source["source_id"]
    pdf_path = raw_dir / "e2e_pdf_fixture.pdf"
    _write_pdf_text_fixture(
        pdf_path, [ACCEPTANCE_SENTENCE, "Second page filler text for the e2e pdf fixture document."]
    )
    added = pipeline.add_document(
        store, program_root=program_root, source_id=pdf_source["source_id"], raw_path=pdf_path,
        created_by_launch=launch_id, config={}, yes=True,
    )
    doc_ids.append(added["document"]["doc_id"])

    results = run_loop(store, worker_id="e2e-corpus-worker", poll_interval_s=0.01, max_idle_polls=3)
    jobs_complete = sum(1 for r in results if r.get("status") == "complete")

    statuses = {
        r["doc_id"]: r["status"]
        for r in store.knowledge.execute("SELECT doc_id, status FROM document")
    }
    not_indexed = {d: statuses.get(d, "MISSING") for d in doc_ids if statuses.get(d) != "indexed"}
    if not_indexed:
        raise RuntimeError(f"these documents never reached 'indexed': {not_indexed}")
    if len(doc_ids) != 4:
        raise RuntimeError(f"expected 4 fixture documents, ingested {len(doc_ids)}")
    if jobs_complete < 16:
        raise RuntimeError(f"only {jobs_complete} job(s) completed; expected >= 16 (4 docs x 4 stages)")

    chunks = _sql_count(store.knowledge, "SELECT COUNT(*) FROM chunk")
    emb_rows = _sql_count(store.knowledge, "SELECT COUNT(*) FROM emb")
    fts_rows = _sql_count(store.knowledge, "SELECT COUNT(*) FROM chunk_fts")
    if chunks <= 0:
        raise RuntimeError("the corpus has zero chunks after a full drain")
    if emb_rows != chunks:
        raise RuntimeError(f"emb rows ({emb_rows}) != chunk rows ({chunks}) -- the embed stage did not cover the corpus")
    if fts_rows != chunks:
        raise RuntimeError(f"chunk_fts rows ({fts_rows}) != chunk rows ({chunks})")

    return {
        "doc_ids": doc_ids,
        "source_ids": source_ids,
        "statuses": {d: statuses[d] for d in doc_ids},
        "jobs_complete": jobs_complete,
        "chunks": chunks,
        "emb_rows": emb_rows,
        "chunk_fts_rows": fts_rows,
    }


def _reindex_fulltext(
    program_root: Path, platform_root: Path, *, repo_root: Path, chunks: int
) -> dict[str, Any]:
    """E-13. The repair verb as a real subprocess, then the doctor check that
    is supposed to go green because of it. A ``skip`` from that check (index
    dir absent, backend opted out) is NOT a pass: the criterion is literally
    ``status == "pass"``."""
    envelope, _proc = _run_cli(
        ["ingest", "reindex-fulltext"], program_root=program_root, platform_root=platform_root
    )
    if not envelope.get("ok"):
        raise RuntimeError(f"ingest reindex-fulltext refused: {envelope.get('error')}")
    result = envelope["result"]
    if result.get("chunks_indexed") != chunks:
        raise RuntimeError(f"reindex indexed {result.get('chunks_indexed')} chunk(s), corpus has {chunks}")

    doctor, _proc2 = _run_cli(
        ["doctor", "--only", "fulltext_index_stale", "--repo-root", str(repo_root)],
        program_root=program_root, platform_root=platform_root,
    )
    checks = (doctor.get("result") or doctor.get("error", {}).get("details") or {}).get("checks") or []
    status = checks[0]["status"] if checks else "MISSING"
    if status != "pass":
        raise RuntimeError(f"doctor fulltext_index_stale is {status!r} after a reindex, expected 'pass'")
    return {
        "chunks_indexed": result.get("chunks_indexed"),
        "indexed_docs": result.get("indexed_docs"),
        "doctor_status": status,
        "index_dir": result.get("index_dir"),
    }


def _backend_parity(store: Any, program_root: Path) -> dict[str, Any]:
    """E-14. Both lexical backends over the SAME corpus, on three queries.

    The two ``stats.fulltext_backend`` values must DIFFER -- otherwise tantivy
    silently fell back to FTS5 and the "parity" would be one backend compared
    with itself. Order inside a score-tie block is each backend's own business
    (an observed, legitimate difference), so the criterion is the hit SET plus
    the top-1 id, which is the plan's own stated floor.
    """
    from trialerror.retrieve import engine

    observed: list[dict[str, Any]] = []
    try:
        for query in PARITY_QUERIES:
            _set_fulltext_backend(program_root, "fts5")
            left = engine.search(store, query=query, k=10, mode="fts")
            _set_fulltext_backend(program_root, "tantivy")
            right = engine.search(store, query=query, k=10, mode="fts")

            left_backend = left["stats"].get("fulltext_backend")
            right_backend = right["stats"].get("fulltext_backend")
            if left_backend != "fts5" or right_backend != "tantivy":
                raise RuntimeError(
                    f"query {query[:40]!r}: backends did not toggle "
                    f"(left={left_backend!r} right={right_backend!r}) -- tantivy fell back to FTS5"
                )
            left_ids = [r["chunk_id"] for r in left["results"]]
            right_ids = [r["chunk_id"] for r in right["results"]]
            if set(left_ids) != set(right_ids):
                raise RuntimeError(
                    f"query {query[:40]!r}: hit SETS differ -- fts5={sorted(left_ids)} tantivy={sorted(right_ids)}"
                )
            top1_equal = bool(left_ids) and left_ids[0] == right_ids[0]
            if not top1_equal:
                raise RuntimeError(
                    f"query {query[:40]!r}: top-1 differs -- fts5={left_ids[:1]} tantivy={right_ids[:1]}"
                )
            observed.append({
                "q": query[:60], "fts5_ids": left_ids, "tantivy_ids": right_ids, "top1_equal": top1_equal,
            })
    finally:
        # the corpus is left on the tier the program asked for
        _set_fulltext_backend(program_root, "tantivy")
    return {"queries": observed}


def _retrieve_auto_citation_fence(store: Any) -> dict[str, Any]:
    """E-15. One open-license hit with a real anchor and both tiers, one
    restricted hit that comes back fenced with no verbatim run over 20 words.

    NOTE (deviation from the plan's literal wording, recorded in the
    implementation doc): the plan named ``len(text) <= 300`` for the fenced
    row's unwrapped text. 300 is ``MAX_OPEN_CITATION_QUOTE_CHARS`` -- the cap
    on an OPEN row's ``citation.quote`` -- not a cap on the fenced serving
    banner, which is a structured line plus a 20-word excerpt and measures
    350-odd characters on this corpus. The mechanical property the plan is
    reaching for is asserted directly instead: the raw chunk text is NOT served
    verbatim, and the citation quote is <= 20 words. The measured character
    count is recorded either way.
    """
    from trialerror.retrieve import engine
    from trialerror.retrieve.fence import MAX_FENCED_EXCERPT_WORDS
    from trialerror.retrieve.wrap import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

    open_hit = engine.search(store, query=ACCEPTANCE_SENTENCE, k=5, mode="auto")
    if not open_hit["results"]:
        raise RuntimeError("auto-mode search for the acceptance sentence returned zero results")
    row = open_hit["results"][0]
    if row.get("fenced") is not False:
        raise RuntimeError(f"the open-license row came back fenced: {row.get('citation')}")
    citation = row.get("citation") or {}
    anchor_id = ((citation.get("anchor") or {}).get("anchor_id"))
    if not anchor_id:
        raise RuntimeError("the open-license row carries no citation anchor")
    tiers_used = set(open_hit.get("tiers_used") or [])
    if not {"fts", "vector"} <= tiers_used:
        raise RuntimeError(f"tiers_used={sorted(tiers_used)} does not include both 'fts' and 'vector'")

    restricted_source = store.knowledge.execute(
        "SELECT source_id FROM source WHERE license_tier='commercial_restricted' LIMIT 1"
    ).fetchone()
    if restricted_source is None:
        raise RuntimeError("the fixture slice registered no commercial_restricted source to fence against")
    fenced_hit = engine.search(
        store, query="desirable difficulties", k=5, mode="auto",
        filters={"source_ids": [restricted_source["source_id"]]},
    )
    if not fenced_hit["results"]:
        raise RuntimeError("the restricted-source search returned zero results")
    frow = fenced_hit["results"][0]
    if frow.get("fenced") is not True:
        raise RuntimeError(f"a commercial_restricted row came back UNFENCED: {frow.get('chunk_id')}")
    quote_words = len(((frow.get("citation") or {}).get("quote") or "").split())
    if quote_words > MAX_FENCED_EXCERPT_WORDS:
        raise RuntimeError(f"the fenced citation quote runs {quote_words} words (cap {MAX_FENCED_EXCERPT_WORDS})")
    served = frow.get("text") or ""
    inner = served
    if UNTRUSTED_OPEN in served and UNTRUSTED_CLOSE in served:
        inner = served.split(UNTRUSTED_OPEN, 1)[1].rsplit(UNTRUSTED_CLOSE, 1)[0].strip()
    chunk_text_row = store.knowledge.execute(
        "SELECT text FROM chunk WHERE chunk_id=?", (frow["chunk_id"],)
    ).fetchone()
    chunk_text = chunk_text_row["text"] if chunk_text_row else ""
    if chunk_text and chunk_text in served:
        raise RuntimeError("the fenced row served the restricted chunk's full text verbatim")

    return {
        "open_anchor_id": anchor_id,
        "tiers_used": sorted(tiers_used),
        "fenced_quote_words": quote_words,
        "fenced_text_chars": len(inner),
        "fenced_chunk_chars": len(chunk_text),
        "restricted_source_id": restricted_source["source_id"],
    }


def _doctor_green(program_root: Path, platform_root: Path, *, repo_root: Path) -> dict[str, Any]:
    """E-16. Zero ``fail`` in BOTH the in-process run and the CLI envelope, a
    catalogue-size floor so a run with program-scoped checks silently skipping
    cannot pass, and ``spawns_vs_bookings`` REQUIRED to be ``pass`` (not merely
    non-fail) -- which is only possible because every journey launch went
    through both hook subprocesses."""
    discover_and_register_checks()
    results = run_checks(DoctorContext(repo_root=repo_root, program_root=program_root, platform_root=platform_root))
    failed = [r.name for r in results if r.status == "fail"]
    if failed:
        raise RuntimeError(f"{len(failed)} doctor check(s) failed in-process: {failed}")
    if len(results) < 40:
        raise RuntimeError(
            f"only {len(results)} doctor check(s) ran; expected at least 40 -- program-scoped checks are skipping"
        )
    by_name = {r.name: r for r in results}
    svb = by_name.get("spawns_vs_bookings")
    if svb is None or svb.status != "pass":
        raise RuntimeError(
            f"spawns_vs_bookings is {getattr(svb, 'status', 'MISSING')!r}, must be 'pass': "
            f"{getattr(svb, 'message', '')}"
        )

    envelope, _proc = _run_cli(
        ["doctor", "--repo-root", str(repo_root)], program_root=program_root, platform_root=platform_root
    )
    payload = envelope.get("result") or (envelope.get("error") or {}).get("details") or {}
    summary = payload.get("summary") or {}
    if summary.get("failed") != 0:
        raise RuntimeError(f"the doctor CLI reports {summary.get('failed')} failed check(s): {summary}")

    return {
        "total": len(results),
        "failed": 0,
        "warned": sorted(r.name for r in results if r.status == "warn"),
        "spawns_vs_bookings": svb.status,
        "cli_summary": summary,
    }


# ===========================================================================
# P3 -- the dashboard journey
# ===========================================================================
def run_e2e_dashboard(
    program_root: Path,
    platform_root: Path,
    *,
    run_id: str,
    repo_root: Path | None = None,
    port: int | None = None,
    expected_documents: int = 4,
    expected_anchor_id: str | None = None,
    caps: Capabilities | None = None,
) -> CheckResult:
    """E-17 + E-70: the dashboard serving the SCRATCH program over real HTTP,
    including the write path refused twice and then accepted.

    ``expected_anchor_id`` defaults to the value the corpus journey stored in
    its own E-15 ``e2e_check`` row, read back from ``ops.db`` -- so the two
    journeys agree across process boundaries without a shared variable.
    """
    # RESOLVE, not just Path(): the serve subprocess below is the one process
    # this module starts with a cwd of its own (_PACKAGE_PARENT, so the console
    # script resolves this checkout), while `--program-root` is passed through
    # verbatim -- a RELATIVE root would make the server open a different
    # program than every other step in this journey.
    repo_root = (Path(repo_root) if repo_root is not None else _REPO_ROOT).resolve()
    program_root = Path(program_root).resolve()
    platform_root = Path(platform_root).resolve()
    caps = caps if caps is not None else probe_capabilities(repo_root=repo_root)

    steps: list[dict[str, Any]] = []
    recorder = E2ERecorder(run_id=run_id, plan_blob=caps.plan_blob)
    store = None
    proc: subprocess.Popen | None = None
    log_fh = None
    host = "127.0.0.1"
    chosen_port = port or _free_port()

    try:
        # inside a _step: a program with no OPEN session is a FAILED phase with
        # a recorded row, never an uncaught RuntimeError that leaves the CLI
        # with no envelope to print at all.
        with _step(steps, "open_scratch_session"):
            store = _open_scratch_store(program_root, platform_root)
            session = _open_session_row(store)
            recorder.session_id = session["session_id"]
            if expected_anchor_id is None:
                prior = read_recorded_checks(store, run_id=run_id).get("E-15")
                expected_anchor_id = (prior or {}).get("evidence", {}).get("open_anchor_id")
        steps.append({
            "name": "open_scratch_session", "ok": True,
            "detail": {"session_id": session["session_id"], "expected_anchor_id": expected_anchor_id},
        })

        with _step(steps, "hold_journey_launch"):
            _ensure_pool(store, account_id=session["account_id"])
            held = hold_journey_launch(
                store, session_id=session["session_id"], agent_kind="e2e-dashboard",
                program_root=program_root, platform_root=platform_root, program_id=run_id,
            )
            launch_id = held["launch_id"]
        steps.append({"name": "hold_journey_launch", "ok": True, "detail": held})

        # -- E-17 ---------------------------------------------------------
        with _step(steps, "serve_subprocess_up"):
            argv = _cli_argv(console=True) + [
                "dashboard", "serve", "--foreground",
                "--host", host, "--port", str(chosen_port),
                "--program-root", str(program_root), "--platform-root", str(platform_root),
                "--repo-root", str(repo_root),
                "--poll-interval", "0.5", "--debounce", "0.5",
            ]
            log_path = program_root.parent / f"e2e-dashboard-serve-{chosen_port}.log"
            log_fh = open(log_path, "wb")
            proc = subprocess.Popen(
                argv, cwd=str(_PACKAGE_PARENT), stdout=log_fh, stderr=subprocess.STDOUT,
                env=_subprocess_env(platform_root),
            )
            _wait_for_server(host, chosen_port, timeout_s=15.0)
        steps.append({
            "name": "serve_subprocess_up", "ok": True,
            "detail": {"port": chosen_port, "argv0": argv[0], "log": str(log_path), "pid": proc.pid},
        })

        base = f"http://{host}:{chosen_port}"

        with _step(steps, "index_has_write_token"):
            status, body = _http_get(f"{base}/")
            if status != 200:
                raise RuntimeError(f"GET / returned {status}")
            match = _TOKEN_META_RE.search(body)
            if match is None:
                raise RuntimeError("the served index page carries no dashboard-write-token <meta> tag")
            write_token = match.group(1)
        steps.append({
            "name": "index_has_write_token", "ok": True,
            "detail": {"status": status, "token_chars": len(write_token)},
        })

        with _step(steps, "api_all_corpus_panel"):
            status, api_all = _http_get_json(f"{base}/dashboard/api/all")
            if status != 200:
                raise RuntimeError(f"GET /dashboard/api/all returned {status}")
            corpus = (api_all.get("panels") or {}).get("corpus") or {}
            if corpus.get("status") != "ok":
                raise RuntimeError(f"the corpus panel is {corpus.get('status')!r}, expected 'ok'")
            counts = corpus.get("counts") or {}
            if counts.get("documents") != expected_documents:
                raise RuntimeError(
                    f"the corpus panel reports {counts.get('documents')} documents, expected {expected_documents} "
                    "(a different program is being served)"
                )
            if not counts.get("chunks", 0) > 0:
                raise RuntimeError("the corpus panel reports zero chunks")
            panels = api_all.get("panels") or {}
            for required in ("session", "doctor"):
                if required not in panels:
                    raise RuntimeError(f"/dashboard/api/all carries no {required!r} panel")
        steps.append({
            "name": "api_all_corpus_panel", "ok": True,
            "detail": {"api_all_keys": sorted(panels), "corpus_counts": counts},
        })

        with _step(steps, "api_all_meta_program_root_is_scratch"):
            meta = api_all.get("meta") or {}
            if not _scratch_meta_program_root_ok(meta, program_root):
                raise RuntimeError(
                    f"meta.program_root is {meta.get('program_root')!r}, not this journey's scratch root "
                    f"{program_root} -- this is the LIVE dashboard, not the scratch one"
                )
        steps.append({
            "name": "api_all_meta_program_root_is_scratch", "ok": True,
            "detail": {"meta_program_root": meta.get("program_root")},
        })

        with _step(steps, "api_search_evidence_anchor"):
            query = urllib.parse.quote(ACCEPTANCE_SENTENCE)
            status, search = _http_get_json(f"{base}/dashboard/api/search?q={query}&k=5")
            if status != 200:
                raise RuntimeError(f"GET /dashboard/api/search returned {status}")
            results = search.get("results") or []
            if not results:
                raise RuntimeError("the dashboard search route returned zero results for the acceptance sentence")
            served_anchor = ((results[0].get("citation") or {}).get("anchor") or {}).get("anchor_id")
            if not served_anchor:
                raise RuntimeError("the dashboard search route served a row with no citation anchor")
            if expected_anchor_id is not None and served_anchor != expected_anchor_id:
                raise RuntimeError(
                    f"the dashboard's top anchor {served_anchor!r} differs from the corpus journey's "
                    f"E-15 anchor {expected_anchor_id!r}"
                )
        steps.append({
            "name": "api_search_evidence_anchor", "ok": True,
            "detail": {"anchor_id": served_anchor, "matched_corpus_journey": expected_anchor_id is not None},
        })

        with _step(steps, "api_search_fenced"):
            restricted = store.knowledge.execute(
                "SELECT source_id FROM source WHERE license_tier='commercial_restricted' LIMIT 1"
            ).fetchone()
            if restricted is None:
                raise RuntimeError("no commercial_restricted source in this program to fence against")
            status, fenced = _http_get_json(
                f"{base}/dashboard/api/search?q=desirable+difficulties&source_ids={restricted['source_id']}"
            )
            if status != 200:
                raise RuntimeError(f"the fenced search route returned {status}")
            frows = fenced.get("results") or []
            if not frows or frows[0].get("fenced") is not True:
                raise RuntimeError("the dashboard served a commercial_restricted row UNFENCED")
        steps.append({"name": "api_search_fenced", "ok": True, "detail": {"fenced_true": True}})

        with _step(steps, "api_feed_ok"):
            status, feed = _http_get_json(f"{base}/dashboard/api/feed")
            if status != 200:
                raise RuntimeError(f"GET /dashboard/api/feed returned {status}")
        steps.append({"name": "api_feed_ok", "ok": True, "detail": {"status": status, "feed_status": feed.get("status")}})

        recorder.record(
            store, "E-17", "pass",
            evidence={
                "port": chosen_port, "api_all_keys": sorted(panels),
                "corpus": {"status": corpus.get("status"), "documents": counts.get("documents"),
                           "chunks": counts.get("chunks")},
                "meta_program_root": meta.get("program_root"),
                "search_anchor_id": served_anchor, "fenced_true": True, "write_token_present": True,
            },
        )

        # -- E-70 ---------------------------------------------------------
        with _step(steps, "create_e2e_thread"):
            from trialerror.events.api import create_thread

            thread = create_thread(store, title=f"e2e {run_id} dashboard", launch_id=launch_id)
            thread_id = thread["thread_id"]
        steps.append({"name": "create_e2e_thread", "ok": True, "detail": {"thread_id": thread_id}})

        write_url = f"{base}/dashboard/api/write/feed-post"
        write_body = {"thread_id": thread_id, "body": f"e2e {run_id} dashboard write"}

        with _step(steps, "write_refused_without_token"):
            status, refused = _http_post_json(write_url, write_body, token=None)
            if status != 403:
                raise RuntimeError(f"an unauthenticated write returned {status}, expected 403: {refused}")
            if not isinstance(refused, dict) or refused.get("ok") is not False or refused.get("status") != "forbidden":
                raise RuntimeError(f"the 403 body is not the documented refusal shape: {refused}")
        steps.append({"name": "write_refused_without_token", "ok": True, "detail": {"status": status}})

        with _step(steps, "write_refused_with_wrong_token"):
            status_wrong, refused_wrong = _http_post_json(write_url, write_body, token="0" * 40)
            if status_wrong != 403:
                raise RuntimeError(f"a wrong-token write returned {status_wrong}, expected 403: {refused_wrong}")
        steps.append({"name": "write_refused_with_wrong_token", "ok": True, "detail": {"status": status_wrong}})

        with _step(steps, "write_accepted_with_token"):
            status_ok, accepted = _http_post_json(write_url, write_body, token=write_token)
            if status_ok != 200:
                raise RuntimeError(f"the token-carrying write returned {status_ok}: {accepted}")
            if not isinstance(accepted, dict) or accepted.get("ok") is not True:
                raise RuntimeError(f"the accepted write did not return ok:true -- {accepted}")
            result = accepted.get("result") or {}
            for field_name in ("post_id", "thread_id", "author", "ts"):
                if field_name not in result:
                    raise RuntimeError(f"the write result is missing {field_name!r}: {result}")
            if not str(result["author"]).startswith("orchestrator:"):
                raise RuntimeError(
                    f"author is {result['author']!r} -- authorship must be server-derived as 'orchestrator:<session>'"
                )
            post_id = result["post_id"]
        steps.append({
            "name": "write_accepted_with_token", "ok": True,
            "detail": {"status": status_ok, "post_id": post_id, "author": result["author"]},
        })

        with _step(steps, "feed_post_row_visible"):
            status_feed, feed_after = _http_get_json(f"{base}/dashboard/api/feed?thread_id={thread_id}")
            if status_feed != 200:
                raise RuntimeError(f"GET /dashboard/api/feed?thread_id= returned {status_feed}")
            post_ids = [p.get("post_id") for p in (feed_after.get("posts") or [])]
            if post_id not in post_ids:
                raise RuntimeError(f"post {post_id} is not in the feed panel's thread {thread_id}: {post_ids}")
            row = store.ops.execute("SELECT * FROM feed_post WHERE post_id=?", (post_id,)).fetchone()
            if row is None:
                raise RuntimeError(f"no feed_post row for {post_id} -- the write never reached the store")
            if row["thread_id"] != thread_id:
                raise RuntimeError(f"the feed_post row is under thread {row['thread_id']!r}, expected {thread_id!r}")
        steps.append({
            "name": "feed_post_row_visible", "ok": True,
            "detail": {"post_id": post_id, "thread_id": thread_id, "row_author": row["author"]},
        })

        with _step(steps, "shutdown_clean"):
            t0 = time.time()
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired as exc:
                proc.kill()
                raise RuntimeError("the dashboard serve subprocess did not exit within 10 s of SIGTERM") from exc
            shutdown_s = round(time.time() - t0, 2)
            proc = None
        steps.append({"name": "shutdown_clean", "ok": True, "detail": {"shutdown_s": shutdown_s}})

        recorder.record(
            store, "E-70", "pass",
            evidence={
                "thread_id": thread_id, "post_id": post_id, "author_prefix": "orchestrator:",
                "refused_status": [403, 403], "accepted_status": 200, "shutdown_s": shutdown_s,
            },
        )

        with _step(steps, "reconcile_journey_launch"):
            from trialerror.budget.pools import reconcile_launch

            reconcile_launch(store, launch_id=launch_id, actual_tokens=450)
            if _launch_state(store, launch_id) != "RECONCILED":
                raise RuntimeError("the dashboard journey's launch did not reach RECONCILED")
        steps.append({"name": "reconcile_journey_launch", "ok": True, "detail": {"launch_id": launch_id}})

    except AcceptanceStepError as exc:
        if store is not None:
            try:
                recorder.record(store, _failing_check_id(exc.step, "e2e_dashboard"), "fail",
                                owner="harness", evidence={"step": exc.step, "error": exc.detail,
                                                           "port": chosen_port})
            except Exception:  # noqa: BLE001
                pass
        return _failed("e2e_dashboard", exc, steps, program_root)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if log_fh is not None:
            log_fh.close()
        if store is not None:
            store.close()

    return _finish(
        "e2e_dashboard", steps, program_root, {"run_id": run_id, "port": chosen_port, "recorded": recorder.rows}
    )


# ===========================================================================
# P4 -- the ops journey
# ===========================================================================
def run_e2e_ops(
    program_root: Path,
    platform_root: Path,
    *,
    run_id: str,
    repo_root: Path | None = None,
    caps: Capabilities | None = None,
    smoke_envelope: Mapping[str, Any] | None = None,
    record_dir: Path | None = None,
) -> CheckResult:
    """E-19..E-24 (+ the copied E-02 envelope): the ops surfaces, then the
    close.

    Step order is NOT id order. E-20 reads ``spawns_vs_bookings`` while every
    consumed launch under the session still has a matching ``subagent_return``;
    E-19's translator half then consumes a booking through a JOB HANDLER, which
    has no subagent to return it, and that is exactly the one-off mismatch E-24
    predicts and asserts. Running the translator half first would make E-20's
    criterion false for a documented reason, which is worse than useless.
    """
    repo_root = (Path(repo_root) if repo_root is not None else _REPO_ROOT).resolve()
    program_root = Path(program_root).resolve()
    platform_root = Path(platform_root).resolve()
    caps = caps if caps is not None else probe_capabilities(repo_root=repo_root)

    steps: list[dict[str, Any]] = []
    recorder = E2ERecorder(run_id=run_id, plan_blob=caps.plan_blob)
    store = None
    translator_ran = False

    try:
        with _step(steps, "open_scratch_session"):
            store = _open_scratch_store(program_root, platform_root)
            session = _open_session_row(store)
            session_id = session["session_id"]
            recorder.session_id = session_id
        steps.append({"name": "open_scratch_session", "ok": True, "detail": {"session_id": session_id}})

        with _step(steps, "hold_journey_launch"):
            _ensure_pool(store, account_id=session["account_id"])
            held = hold_journey_launch(
                store, session_id=session_id, agent_kind="e2e-ops",
                program_root=program_root, platform_root=platform_root, program_id=run_id,
            )
            launch_id = held["launch_id"]
        steps.append({"name": "hold_journey_launch", "ok": True, "detail": held})

        # -- E-19, thread/post half ----------------------------------------
        with _step(steps, "feed_thread_post"):
            e19a = _feed_thread_post(store, program_root, platform_root, run_id=run_id, launch_id=launch_id)
            thread_id, post_id = e19a["thread_id"], e19a["post_id"]
        steps.append({"name": "feed_thread_post", "ok": True, "detail": e19a})

        # -- E-20 -----------------------------------------------------------
        with _step(steps, "budget_book_spawn_return_reconcile"):
            e20 = _budget_round_trip(
                store, program_root, platform_root, repo_root=repo_root,
                session_id=session_id, account_id=session["account_id"], held=held,
            )
        steps.append({"name": "budget_book_spawn_return_reconcile", "ok": True, "detail": e20})
        recorder.record(store, "E-20", "pass", evidence=e20)

        # -- E-19, translator half ------------------------------------------
        if not caps.translator:
            reason = "the feed translator is not present on this deployment (`feed translate` is not a CLI verb)"
            _blocked(steps, "feed_translate_fail_closed", "lane-b", reason)
            recorder.record(store, "E-19", "blocked", owner="lane-b",
                            evidence={**e19a, "translator_reason": reason})
        else:
            with _step(steps, "feed_translate_fail_closed"):
                e19b = _feed_translate_fail_closed(
                    store, program_root, platform_root, session_id=session_id,
                    program_id=run_id, post_id=post_id,
                )
                translator_ran = True
            steps.append({"name": "feed_translate_fail_closed", "ok": True, "detail": e19b})
            recorder.record(store, "E-19", "pass", evidence={**e19a, **e19b})

        # -- E-21 -----------------------------------------------------------
        with _step(steps, "law_append_verify_with_stale_control"):
            e21 = _law_append_verify(store, program_root, platform_root, repo_root=repo_root, run_id=run_id)
        steps.append({"name": "law_append_verify_with_stale_control", "ok": True, "detail": e21})
        recorder.record(store, "E-21", "pass", evidence=e21)

        # -- E-22 -----------------------------------------------------------
        with _step(steps, "jobs_worker_cli_once"):
            e22 = _jobs_worker_cli_once(store, program_root, platform_root, launch_id=launch_id)
        steps.append({"name": "jobs_worker_cli_once", "ok": True, "detail": e22})
        recorder.record(store, "E-22", "pass", evidence=e22)

        # -- E-23 -----------------------------------------------------------
        with _step(steps, "events_tail_types"):
            e23 = _events_tail_and_export(
                store, program_root, platform_root, session_id=session_id,
                record_dir=record_dir or (program_root.parent / "record"),
            )
        steps.append({"name": "events_tail_types", "ok": True, "detail": e23})
        recorder.record(store, "E-23", "pass", evidence=e23)

        # -- E-24 -----------------------------------------------------------
        with _step(steps, "session_close_and_handoff"):
            from trialerror.sessions.lifecycle import close_session

            closed = close_session(
                store,
                course_check={"rungs": "e2e", "build_vs_theory": "build", "drift_flag": False},
                session_id=session_id,
                notes=f"e2e {run_id}",
            )
            if not closed.ok:
                raise RuntimeError(f"close refused: code={closed.code!r} message={closed.message!r}")
            handoff = Path(closed.handoff_path) if closed.handoff_path else None
            if handoff is None:
                raise RuntimeError("close succeeded but reported no handoff path")
            resolved = handoff if handoff.is_absolute() else program_root / handoff
            if not resolved.is_file():
                raise RuntimeError(f"close reported handoff {closed.handoff_path!r} but no such file exists")
        steps.append({
            "name": "session_close_and_handoff", "ok": True,
            "detail": {"session_id": session_id, "handoff_path": closed.handoff_path},
        })

        with _step(steps, "doctor_green_after_close"):
            e24 = _doctor_after_close(
                program_root, platform_root, repo_root=repo_root,
                session_id=session_id, translator_ran=translator_ran,
            )
        steps.append({"name": "doctor_green_after_close", "ok": True, "detail": e24})

        with _step(steps, "record_prior_checks"):
            recorder.record(
                store, "E-24", "pass",
                evidence={"session_id": session_id, "handoff_path": closed.handoff_path, **e24},
            )
            if smoke_envelope is not None:
                recorder.record(store, "E-02", _smoke_status(smoke_envelope), by="journey",
                                evidence=_smoke_evidence(smoke_envelope))
        steps.append({
            "name": "record_prior_checks", "ok": True,
            "detail": {"recorded": [r["check_id"] for r in recorder.rows],
                       "smoke_envelope_copied": smoke_envelope is not None},
        })

    except AcceptanceStepError as exc:
        if store is not None:
            try:
                recorder.record(store, _failing_check_id(exc.step, "e2e_ops"), "fail",
                                owner="harness", evidence={"step": exc.step, "error": exc.detail})
            except Exception:  # noqa: BLE001
                pass
        return _failed("e2e_ops", exc, steps, program_root)
    finally:
        if store is not None:
            store.close()

    return _finish("e2e_ops", steps, program_root, {"run_id": run_id, "recorded": recorder.rows})


def _smoke_status(envelope: Mapping[str, Any]) -> str:
    payload = envelope.get("result") or (envelope.get("error") or {}).get("details") or {}
    summary = payload.get("summary") or {}
    return "pass" if envelope.get("ok") and summary.get("failed") == 0 else "fail"


def _smoke_evidence(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """E-02's row: the smoke's own summary plus its 12 step names, which is the
    assertion that distinguishes ``ok:true`` from ``ok:true with fewer steps``."""
    payload = envelope.get("result") or (envelope.get("error") or {}).get("details") or {}
    checks = payload.get("checks") or []
    smoke = next((c for c in checks if c.get("name") == "clean_checkout_smoke"), None)
    steps = [s.get("name") for s in ((smoke or {}).get("details") or {}).get("steps", [])]
    return {
        "summary": payload.get("summary"),
        "steps": steps,
        "step_count": len(steps),
        "all_steps_ok": all(s.get("ok") for s in ((smoke or {}).get("details") or {}).get("steps", [])),
    }


def _feed_thread_post(
    store: Any, program_root: Path, platform_root: Path, *, run_id: str, launch_id: str
) -> dict[str, Any]:
    """E-19's first half: a thread and a full-text post, then the two read
    verbs as real subprocesses. ``author`` is DERIVED from the posting launch
    and is never a caller-set string -- that is what the assertion checks."""
    from trialerror.events.api import create_thread, post_feed

    thread = create_thread(store, title=f"e2e {run_id}", launch_id=launch_id)
    thread_id = thread["thread_id"]
    body = (
        f"Opening the e2e ops thread for run {run_id}. This post is full text, not a summary, because the "
        f"feed's whole contract is that a reader never has to go somewhere else for what was actually said. "
        f"It exists so the translator seam below has a real post to fail closed on."
    )
    post = post_feed(store, thread_id=thread_id, body=body, launch_id=launch_id)
    post_id = post["post_id"]

    agent_kind = store.platform.execute(
        "SELECT agent_kind FROM launch WHERE launch_id=?", (launch_id,)
    ).fetchone()["agent_kind"]
    expected_author = f"{agent_kind}:{launch_id}"
    if post["author"] != expected_author:
        raise RuntimeError(f"author is {post['author']!r}, expected the derived {expected_author!r}")

    threads_env, _p1 = _run_cli(["feed", "threads"], program_root=program_root, platform_root=platform_root)
    if not threads_env.get("ok"):
        raise RuntimeError(f"feed threads refused: {threads_env.get('error')}")
    listed = [t["thread_id"] for t in threads_env["result"]["threads"]]
    if thread_id not in listed:
        raise RuntimeError(f"feed threads does not list {thread_id}: {listed}")

    read_env, _p2 = _run_cli(
        ["feed", "read", "--thread-id", thread_id], program_root=program_root, platform_root=platform_root
    )
    if not read_env.get("ok"):
        raise RuntimeError(f"feed read refused: {read_env.get('error')}")
    posts = read_env["result"]["posts"]
    match = next((p for p in posts if p["post_id"] == post_id), None)
    if match is None:
        raise RuntimeError(f"feed read does not show post {post_id}")
    if match["author"] != expected_author:
        raise RuntimeError(f"feed read reports author {match['author']!r}, expected {expected_author!r}")
    if match["body"] != body:
        raise RuntimeError("feed read returned a body that is not the full text that was posted")

    return {"thread_id": thread_id, "post_id": post_id, "author": post["author"], "body_chars": len(body)}


def _budget_round_trip(
    store: Any,
    program_root: Path,
    platform_root: Path,
    *,
    repo_root: Path,
    session_id: str,
    account_id: str,
    held: Mapping[str, Any],
) -> dict[str, Any]:
    """E-20. The journey's own launch is the one under the microscope: booked,
    consumed through the real PreToolUse hook, returned through the real
    PostToolUse hook (all three already done by :func:`hold_journey_launch`),
    and reconciled HERE with the dangling count read from ``platform.db``
    before and after.

    ``budget_dangling_launches`` is TTL-based, so "0 dangling" straight after a
    booking is not evidence of anything; the before/after pair from the same
    SQL is. And ``spawns_vs_bookings`` must be ``pass`` -- one
    ``subagent_return`` per consumed booking, counted, for all three journey
    launches under this session.
    """
    from trialerror.budget.pools import budget_status, reconcile_launch

    launch_id = held["launch_id"]
    state_before = _launch_state(store, launch_id)
    if state_before != "RUNNING":
        raise RuntimeError(f"the ops journey's launch is {state_before!r} before reconcile, expected RUNNING")

    returns = store.ops.execute(
        "SELECT event_id FROM event WHERE type='subagent_return' AND launch_id=?", (launch_id,)
    ).fetchall()
    if len(returns) != 1:
        raise RuntimeError(f"expected one subagent_return for the ops launch, found {len(returns)}")

    hook_alive_post_task = _sql_count(
        store.ops,
        "SELECT COUNT(*) FROM event WHERE type='hook_alive' AND session_id=? AND payload LIKE '%post_task%'",
        (session_id,),
    )
    if hook_alive_post_task < 1:
        raise RuntimeError("no hook_alive{post_task} row for this session")

    dangling_before = _dangling_counts(store, session_id)
    if dangling_before["session"] != 1:
        raise RuntimeError(
            f"expected exactly 1 dangling launch under this session before reconcile, found "
            f"{dangling_before['session']}"
        )
    if dangling_before["platform"] != 1:
        raise RuntimeError(
            f"expected exactly 1 dangling launch platform-wide before reconcile, found "
            f"{dangling_before['platform']} -- another journey left one behind"
        )

    reconcile_launch(store, launch_id=launch_id, actual_tokens=450)
    if _launch_state(store, launch_id) != "RECONCILED":
        raise RuntimeError("the ops journey's launch did not reach RECONCILED")

    dangling_after = _dangling_counts(store, session_id)
    if dangling_after != {"session": 0, "platform": 0}:
        raise RuntimeError(f"dangling launches after reconcile: {dangling_after}, expected zero on both counts")

    status = budget_status(store, account_id=account_id)
    headroom = None
    for pool in status.get("pools", []):
        if pool.get("model_class") == "top":
            headroom = pool.get("headroom_tokens", pool.get("headroom"))
    if headroom is not None and headroom <= 0:
        raise RuntimeError(f"the top pool reports no headroom after reconcile: {headroom}")

    discover_and_register_checks()
    results = run_checks(
        DoctorContext(repo_root=repo_root, program_root=program_root, platform_root=platform_root),
        only=["budget_dangling_launches", "spawns_vs_bookings"],
    )
    by_name = {r.name: r for r in results}
    if by_name["budget_dangling_launches"].status != "pass":
        raise RuntimeError(
            f"budget_dangling_launches is {by_name['budget_dangling_launches'].status!r}: "
            f"{by_name['budget_dangling_launches'].message}"
        )
    svb = by_name["spawns_vs_bookings"]
    if svb.status != "pass":
        raise RuntimeError(f"spawns_vs_bookings is {svb.status!r}: {svb.message}")

    consumed = _sql_count(
        store.platform,
        "SELECT COUNT(*) FROM launch WHERE session_id=? AND state IN ('RUNNING','RECONCILED')",
        (session_id,),
    )
    returned = _sql_count(
        store.ops, "SELECT COUNT(*) FROM event WHERE type='subagent_return' AND session_id=?", (session_id,)
    )

    return {
        "launch_id": launch_id,
        "states_seen": [*held["states_seen"], "RECONCILED"],
        "subagent_return_event_id": held["subagent_return_event_id"],
        "dangling_before": dangling_before,
        "dangling_after": dangling_after,
        "headroom": headroom,
        "spawns_vs_bookings": svb.status,
        "consumed_vs_returned": [consumed, returned],
    }


def _job_row(store: Any, job_id: str) -> dict[str, Any]:
    row = store.jobs.execute("SELECT * FROM job WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"no job row for {job_id!r}")
    return dict(row)


def _payload_field(payload: Any, key: str) -> Any:
    """One field out of a job payload, whether it arrived as the stored JSON
    string or as an already-decoded object -- an envelope round trip and a
    direct store read disagree on that, and neither is wrong."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    return payload.get(key) if isinstance(payload, dict) else None


def _feed_translate_fail_closed(
    store: Any,
    program_root: Path,
    platform_root: Path,
    *,
    session_id: str,
    program_id: str,
    post_id: str,
) -> dict[str, Any]:
    """E-19's second half: the ``model`` translator backend refuses LOUDLY, in
    two different ways, at two different points.

    (a) The BOOKING GATE, a negative control: the handler checks the job's
        ``created_by_launch`` BEFORE it ever calls the backend, so a job
        enqueued without ``--by-launch`` must fail on "created_by_launch is
        empty" and must NOT mention the missing driver.
    (b) The DRIVER SEAM, behind a real booking: the handler consumes the
        booking (PROVISIONAL -> RUNNING) and only then calls the backend, which
        raises. The failure is a LOGIC failure carrying the exact
        "has no generation driver" substring, no translation is stored, and the
        launch is left RUNNING.

    Two different messages with the launch consumed only in (b) is what
    excludes a "fail-closed" that never reached the backend at all -- and the
    named substring is what excludes a translator that quietly degraded to the
    fake or pending backend.

    The step then SETTLES what it opened: a handler-consumed launch has no
    subagent to return it, so it is reconciled here (otherwise E-24's close
    would refuse ``dangling_launches``), and both jobs are paused (a logic
    failure below max attempts becomes claimable again after the backoff, and a
    retry would only burn an attempt and make the program non-deterministic for
    E-22).
    """
    from trialerror.budget.pools import book_launch, reconcile_launch

    # the criterion is "no launch row CHANGED STATE", so the snapshot is the
    # whole {launch_id: state} mapping: a COUNT would be blind to a transition
    # on a row that was already there.
    launch_ledger_before = _launch_states(store)

    # -- (a) the booking gate -------------------------------------------
    env_a, _pa = _run_cli(
        ["feed", "translate", "--post-id", post_id], program_root=program_root, platform_root=platform_root
    )
    if not env_a.get("ok"):
        raise RuntimeError(f"feed translate (no --by-launch) refused at enqueue time: {env_a.get('error')}")
    job_id_a = env_a["result"]["job"]["job_id"]

    run_a, _pra = _run_cli(
        ["jobs", "start-worker", "--foreground", "--mode", "once", "--job-id", job_id_a],
        program_root=program_root, platform_root=platform_root,
    )
    status_a = (run_a.get("result") or {}).get("status")
    if status_a != "failed":
        raise RuntimeError(f"the unbooked translate job settled as {status_a!r}, expected 'failed'")
    row_a = _job_row(store, job_id_a)
    last_error_a = row_a.get("last_error") or ""
    if "created_by_launch is empty" not in last_error_a:
        raise RuntimeError(f"the booking-gate refusal does not name created_by_launch: {last_error_a[:200]!r}")
    if "has no generation driver" in last_error_a:
        raise RuntimeError(
            "the unbooked job reached the BACKEND -- the booking gate did not fire before the driver seam"
        )
    launch_ledger_after_a = _launch_states(store)
    if launch_ledger_after_a != launch_ledger_before:
        added = sorted(set(launch_ledger_after_a) - set(launch_ledger_before))
        removed = sorted(set(launch_ledger_before) - set(launch_ledger_after_a))
        moved = {
            lid: (launch_ledger_before[lid], launch_ledger_after_a[lid])
            for lid in set(launch_ledger_before) & set(launch_ledger_after_a)
            if launch_ledger_before[lid] != launch_ledger_after_a[lid]
        }
        raise RuntimeError(
            f"the booking-gate refusal changed the launch ledger; it must not "
            f"(added={added} removed={removed} state_changes={moved})"
        )

    _run_cli(["jobs", "pause", job_id_a], program_root=program_root, platform_root=platform_root)

    # -- (b) the driver seam behind a real booking -----------------------
    booked = book_launch(
        store, session_id=session_id, program_id=program_id, agent_kind="e2e-translator",
        model_class="small", model="haiku", purpose="mechanical", est_tokens=200,
    )
    if not booked.ok:
        raise RuntimeError(f"book_launch for the translator refused: {booked.state} {booked.reason}")
    translator_launch_id = booked.launch_id
    launch_states = ["PROVISIONAL"]

    env_b, _pb = _run_cli(
        ["feed", "translate", "--post-id", post_id, "--by-launch", translator_launch_id],
        program_root=program_root, platform_root=platform_root,
    )
    if not env_b.get("ok"):
        raise RuntimeError(f"feed translate --by-launch refused at enqueue time: {env_b.get('error')}")
    job_b = env_b["result"]["job"]
    job_id_b = job_b["job_id"]
    # NOTE (deviation, recorded in the implementation doc): `feed translate`
    # enqueues job KIND "custom" and names the handler inside the payload --
    # there is no `feed_translate` job kind. The handler name is what the
    # criterion is really about, so that is what is asserted.
    handler_b = _payload_field(job_b.get("payload"), "handler")
    if handler_b != "feed_translate":
        raise RuntimeError(
            f"the enqueued job's handler is {handler_b!r}, expected 'feed_translate' "
            f"(job kind {job_b.get('kind')!r})"
        )

    run_b, _prb = _run_cli(
        ["jobs", "start-worker", "--foreground", "--mode", "once", "--job-id", job_id_b],
        program_root=program_root, platform_root=platform_root,
    )
    status_b = (run_b.get("result") or {}).get("status")
    if status_b != "failed":
        raise RuntimeError(f"the booked translate job settled as {status_b!r}, expected 'failed'")

    state_b = _launch_state(store, translator_launch_id)
    if state_b != "RUNNING":
        raise RuntimeError(
            f"the translator launch is {state_b!r} after the handler ran; the handler must have consumed it "
            "(PROVISIONAL -> RUNNING) before calling the backend"
        )
    launch_states.append("RUNNING")

    row_b = _job_row(store, job_id_b)
    if row_b.get("failure_class") != "logic":
        raise RuntimeError(f"failure_class is {row_b.get('failure_class')!r}, expected 'logic'")
    last_error_b = row_b.get("last_error") or ""
    if "has no generation driver" not in last_error_b:
        raise RuntimeError(
            f"the driver-seam failure does not carry the named substring: {last_error_b[:200]!r} -- the "
            "translator degraded to another backend instead of refusing"
        )

    logs_b, _plb = _run_cli(
        ["jobs", "logs", job_id_b], program_root=program_root, platform_root=platform_root
    )
    log_types = [e["type"] for e in (logs_b.get("result") or {}).get("events", [])]

    trans_env, _pt = _run_cli(
        ["feed", "translations", "--post-id", post_id], program_root=program_root, platform_root=platform_root
    )
    if trans_env.get("ok"):
        raise RuntimeError("a current translation exists for the post; the gate should have withheld everything")
    if (trans_env.get("error") or {}).get("code") != "not_found":
        raise RuntimeError(f"feed translations failed for an unexpected reason: {trans_env.get('error')}")

    from trialerror.dashboard.data import build_feed_panel
    from trialerror.dashboard.store_ro import open_store_ro

    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        panel = build_feed_panel(rostore, thread_id=None)
    finally:
        rostore.close()
    served = next((p for p in panel.get("posts", []) if p.get("post_id") == post_id), None)
    if served is not None and served.get("translation"):
        raise RuntimeError("the feed panel is serving a translation for a post whose translation failed closed")

    reconcile_launch(store, launch_id=translator_launch_id, actual_tokens=0, reconcile_source="manual")
    if _launch_state(store, translator_launch_id) != "RECONCILED":
        raise RuntimeError("the translator launch did not reach RECONCILED")
    launch_states.append("RECONCILED")

    _run_cli(["jobs", "pause", job_id_b], program_root=program_root, platform_root=platform_root)
    paused = [
        jid for jid in (job_id_a, job_id_b)
        if _job_row(store, jid)["state"] == "paused"
    ]
    if len(paused) != 2:
        raise RuntimeError(f"expected both translate jobs paused, got {paused}")

    return {
        "gate_job_id": job_id_a,
        "gate_error_head": last_error_a[:160],
        "gate_launch_ledger_unchanged": True,
        "gate_launch_rows_seen": len(launch_ledger_before),
        "translator_launch_id": translator_launch_id,
        "launch_states_seen": launch_states,
        "translate_job_id": job_id_b,
        "job_state": row_b["state"],
        "failure_class": row_b["failure_class"],
        "last_error_head": last_error_b[:160],
        "ledger_event_types": sorted(set(log_types)),
        "paused": paused,
        "feed_panel_translation": None,
    }


def _law_append_verify(
    store: Any, program_root: Path, platform_root: Path, *, repo_root: Path, run_id: str
) -> dict[str, Any]:
    """E-21. TWO appends, because a fresh program has no pin at all and
    ``law verify --pin`` only accepts the ``vNN@YYYY-MM-DD`` string form: the
    first append creates a pin, the second makes the first one STALE, and
    verifying the stale one is the negative control that proves verify is
    actually comparing pins rather than always saying valid."""
    from trialerror.law.service import current_pin

    if current_pin(store) is not None:
        raise RuntimeError("this program already has a law digest; E-21 needs a program with no pin")

    append1, _p1 = _run_cli(
        ["law", "append", "--summary", f"e2e {run_id}: scratch ruling 1"],
        program_root=program_root, platform_root=platform_root,
    )
    if not append1.get("ok"):
        raise RuntimeError(f"law append (1) refused: {append1.get('error')}")
    ruling_1 = append1["result"]["ruling_id"]
    pin0 = current_pin(store)
    if not pin0:
        raise RuntimeError("no pin after the first append")

    append2, _p2 = _run_cli(
        ["law", "append", "--summary", f"e2e {run_id}: scratch ruling 2"],
        program_root=program_root, platform_root=platform_root,
    )
    if not append2.get("ok"):
        raise RuntimeError(f"law append (2) refused: {append2.get('error')}")
    ruling_2 = append2["result"]["ruling_id"]
    pin1 = current_pin(store)
    if not pin1 or pin1 == pin0:
        raise RuntimeError(f"the second append did not bump the pin: pin0={pin0!r} pin1={pin1!r}")

    verify1, _p3 = _run_cli(
        ["law", "verify", "--pin", pin1], program_root=program_root, platform_root=platform_root
    )
    if not verify1.get("ok"):
        raise RuntimeError(f"law verify on the CURRENT pin failed: {verify1.get('error')}")
    v1 = verify1["result"]
    if not (v1["valid"] and v1["chain_ok"] and v1["pin_stale"] is False):
        raise RuntimeError(f"verify(pin1) is not valid/chain_ok/fresh: {v1}")

    verify0, _p4 = _run_cli(
        ["law", "verify", "--pin", pin0], program_root=program_root, platform_root=platform_root
    )
    v0 = verify0.get("result") or (verify0.get("error") or {}).get("details") or {}
    if verify0.get("ok"):
        raise RuntimeError("law verify accepted the SUPERSEDED pin -- the staleness control did not fire")
    if not (v0.get("pin_stale") is True and v0.get("chain_ok") is True and v0.get("valid") is False):
        raise RuntimeError(f"verify(pin0) did not report a stale-but-intact chain: {v0}")

    digest_path = program_root / "law" / "LAW_DIGEST.md"
    if not digest_path.is_file():
        raise RuntimeError(f"no rendered digest at {digest_path}")
    digest_text = digest_path.read_text(encoding="utf-8")
    for needle in (f"e2e {run_id}: scratch ruling 1", f"e2e {run_id}: scratch ruling 2"):
        if needle not in digest_text:
            raise RuntimeError(f"the rendered digest does not contain {needle!r}")

    discover_and_register_checks()
    law_checks = run_checks(
        DoctorContext(repo_root=repo_root, program_root=program_root, platform_root=platform_root),
        only=["law_digest_lockstep", "law_chain_integrity", "law_pin_format"],
    )
    bad = {r.name: r.status for r in law_checks if r.status not in ("pass", "skip")}
    if bad:
        raise RuntimeError(f"law doctor checks not green: {bad}")

    return {
        "pin_none_before": True,
        "ruling_ids": [ruling_1, ruling_2],
        "pin0": pin0,
        "pin1": pin1,
        "verify_pin1": {"valid": v1["valid"], "chain_ok": v1["chain_ok"], "pin_stale": v1["pin_stale"]},
        "verify_pin0": {"valid": v0.get("valid"), "chain_ok": v0.get("chain_ok"), "pin_stale": v0.get("pin_stale")},
        "law_doctor": {r.name: r.status for r in law_checks},
    }


def _jobs_worker_cli_once(
    store: Any, program_root: Path, platform_root: Path, *, launch_id: str
) -> dict[str, Any]:
    """E-22. The jobs worker through the REAL console entry point -- the same
    command the container's jobs window runs -- not ``run_one`` in process
    (which the clean-checkout smoke's drain already proves).

    ``--job-id`` is not optional here: an unselected ``--mode once`` claims
    whichever eligible job sorts first, and this program has other jobs in it.
    """
    from trialerror.ingest import pipeline

    doc = store.knowledge.execute("SELECT doc_id FROM document WHERE status='indexed' LIMIT 1").fetchone()
    if doc is None:
        raise RuntimeError("no indexed document to re-embed")
    job = pipeline.requeue_stage(store, doc_id=doc["doc_id"], kind="embed", created_by_launch=launch_id)
    job_id = job["job_id"]

    run_env, _p1 = _run_cli(
        ["jobs", "start-worker", "--foreground", "--mode", "once", "--job-id", job_id],
        program_root=program_root, platform_root=platform_root,
    )
    result = run_env.get("result") or {}
    if result.get("status") != "complete":
        raise RuntimeError(f"the embed job settled as {result.get('status')!r}, expected 'complete'")
    if result.get("job_id") != job_id:
        raise RuntimeError(f"the worker settled {result.get('job_id')!r}, not the job it was pointed at")
    worker_id = result.get("worker_id") or ""
    if not re.match(r"^\d+:\d{4}-\d{2}-\d{2}T", worker_id):
        raise RuntimeError(f"worker_id {worker_id!r} is not the pid+start_ts shape")

    logs_env, _p2 = _run_cli(["jobs", "logs", job_id], program_root=program_root, platform_root=platform_root)
    types = [e["type"] for e in (logs_env.get("result") or {}).get("events", [])]
    # NOTE: the ledger's completion event is `completed`, not `complete` (the
    # job STATE is `complete`) -- recorded as an implementation-note deviation
    # from the plan's wording, which named the state.
    for required in ("claimed", "completed"):
        if required not in types:
            raise RuntimeError(f"the job ledger has no {required!r} event: {types}")

    list_env, _p3 = _run_cli(
        ["jobs", "list", "--state", "complete", "--limit", "200"],
        program_root=program_root, platform_root=platform_root,
    )
    listed = [j["job_id"] for j in (list_env.get("result") or {}).get("jobs", [])]
    if job_id not in listed:
        raise RuntimeError(f"jobs list --state complete does not include {job_id}")

    # The embed handler chains an `index` job with a deterministic id, so this
    # second worker either runs a freshly-enqueued one or finds the queue idle
    # because that id already settled during the corpus drain. Either way the
    # criterion is the same and is read off the LEDGER, not off the worker's
    # own return value: the document's index job exists and is `complete`.
    index_env, _p4 = _run_cli(
        ["jobs", "start-worker", "--foreground", "--mode", "once", "--kinds", "index"],
        program_root=program_root, platform_root=platform_root,
    )
    index_result = index_env.get("result") or {}
    if index_result.get("status") not in ("complete", "idle"):
        raise RuntimeError(f"the chained index worker settled as {index_result.get('status')!r}")
    index_row = store.jobs.execute(
        "SELECT job_id, state FROM job WHERE kind='index' AND payload LIKE ?", (f"%{doc['doc_id']}%",)
    ).fetchone()
    if index_row is None or index_row["state"] != "complete":
        raise RuntimeError(
            f"the document's index job is {index_row['state'] if index_row else 'MISSING'!r}, expected 'complete'"
        )

    return {
        "job_id": job_id,
        "worker_id": worker_id,
        "ledger_event_types": sorted(set(types)),
        "index_job_id": index_row["job_id"],
        "index_job_state": index_row["state"],
        "index_worker_status": index_result.get("status"),
    }


def _events_tail_and_export(
    store: Any, program_root: Path, platform_root: Path, *, session_id: str, record_dir: Path
) -> dict[str, Any]:
    """E-23. The event types this run wrote, the three hooks that fired for
    THIS session, and a byte-stable export.

    ``events export`` REQUIRES ``--out`` and a shell redirect after
    ``docker exec`` would be the host's, so the two exports go to real files
    inside the record directory and are compared by sha256.
    """
    tail_env, _p1 = _run_cli(
        ["events", "tail", "--limit", "500"], program_root=program_root, platform_root=platform_root
    )
    if not tail_env.get("ok"):
        raise RuntimeError(f"events tail refused: {tail_env.get('error')}")
    rows = tail_env["result"]["events"] if "events" in tail_env["result"] else tail_env["result"]
    if isinstance(rows, dict):
        rows = rows.get("events", [])
    types_seen = {r["type"] for r in rows}
    for required in ("hook_alive", "subagent_return", E2E_CHECK_EVENT_TYPE):
        if required not in types_seen:
            raise RuntimeError(f"events tail shows no {required!r} rows: {sorted(types_seen)}")

    hooks_seen = {
        (r.get("payload") or {}).get("hook")
        for r in rows
        if r["type"] == "hook_alive" and r.get("session_id") == session_id
    }
    hooks_seen.discard(None)
    for required in ("session_start", "spawn_gate", "post_task"):
        if required not in hooks_seen:
            raise RuntimeError(f"no hook_alive{{{required}}} row for this session: {sorted(hooks_seen)}")

    record_dir = Path(record_dir)
    try:
        record_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        record_dir = program_root / "record"
        record_dir.mkdir(parents=True, exist_ok=True)
    out_a = record_dir / "e2e_check.a.jsonl"
    out_b = record_dir / "e2e_check.b.jsonl"
    for out in (out_a, out_b):
        env, _p = _run_cli(
            ["events", "export", "--type", E2E_CHECK_EVENT_TYPE, "--out", str(out)],
            program_root=program_root, platform_root=platform_root,
        )
        if not env.get("ok"):
            raise RuntimeError(f"events export to {out} refused: {env.get('error')}")
        if not out.is_file():
            raise RuntimeError(f"events export reported ok but wrote no file at {out}")

    digests = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (out_a, out_b)]
    if digests[0] != digests[1]:
        raise RuntimeError(f"two exports of the same rows are not byte-identical: {digests}")

    return {
        "types_seen": sorted(types_seen),
        "hooks_seen": sorted(hooks_seen),
        "export_sha256": digests[0],
        "export_paths": [str(out_a), str(out_b)],
        "rows_tailed": len(rows),
    }


def _doctor_after_close(
    program_root: Path, platform_root: Path, *, repo_root: Path, session_id: str, translator_ran: bool
) -> dict[str, Any]:
    """E-24's second half: doctor still has zero failures after the close, and
    ``spawns_vs_bookings`` is in EXACTLY the shape E-19 predicted -- ``warn``
    with one mismatched session at 4 consumed / 3 returned when the translator
    half ran (a booking consumed by a JOB HANDLER has no subagent to return
    it), ``pass`` when it was blocked. Any other shape is a real finding."""
    discover_and_register_checks()
    results = run_checks(DoctorContext(repo_root=repo_root, program_root=program_root, platform_root=platform_root))
    failed = [r.name for r in results if r.status == "fail"]
    if failed:
        raise RuntimeError(f"{len(failed)} doctor check(s) failed after close: {failed}")
    by_name = {r.name: r for r in results}
    svb = by_name["spawns_vs_bookings"]
    mismatched = svb.details.get("mismatched_sessions") or []
    bad_events = svb.details.get("bad_launch_id_events") or []

    if translator_ran:
        if svb.status != "warn":
            raise RuntimeError(
                f"spawns_vs_bookings is {svb.status!r}; with the translator half run it must be 'warn' "
                "(one handler-consumed booking with no subagent_return)"
            )
        if len(mismatched) != 1 or mismatched[0].get("session_id") != session_id:
            raise RuntimeError(f"unexpected mismatched_sessions: {mismatched}")
        entry = mismatched[0]
        if entry.get("consumed_launch_count") != entry.get("subagent_return_count", -1) + 1:
            raise RuntimeError(
                f"the mismatch is not the documented one-off: {entry} (expected consumed == returned + 1)"
            )
        if bad_events:
            raise RuntimeError(f"subagent_return rows with a null/unknown launch_id: {bad_events}")
    else:
        if svb.status != "pass":
            raise RuntimeError(
                f"spawns_vs_bookings is {svb.status!r}; with the translator half blocked it must be 'pass': "
                f"{svb.message}"
            )

    return {
        "doctor_total": len(results),
        "doctor_failed": 0,
        "doctor_warned": sorted(r.name for r in results if r.status == "warn"),
        "spawns_vs_bookings": {
            "status": svb.status, "mismatched_sessions": mismatched, "bad_launch_id_events": bad_events,
        },
        "translator_ran": translator_ran,
    }


# ===========================================================================
# P7 -- offload
# ===========================================================================
def run_e2e_offload_enqueue(
    program_root: Path,
    platform_root: Path,
    *,
    run_id: str,
    repo_root: Path | None = None,
    min_chunks: int = 200,
    embed_model_key: str = "qwen3-4b",
    embed_dims: int = 2048,
    caps: Capabilities | None = None,
    record_program_root: Path | None = None,
) -> CheckResult:
    """E-50. A SECOND scratch program whose toml is the production shape --
    ``require_real_backends = true`` with both backends offloaded -- ingests one
    long synthetic document and parks its embed stage.

    The document is generated, banner-labelled synthetic prose, never a scan
    and never a copyrighted text. ``require_real_backends`` is what makes the
    "vacuous pass" impossible: a fake backend cannot even load, so an embed job
    that "completed" here could not have completed on hash-derived vectors.

    ``record_program_root`` is where the ``e2e_check`` row LANDS, and it
    defaults to the offload program itself only because that is the sole root
    this function is guaranteed to have. On a real run it must be the FIRST
    scratch program (``${P}``): ``--phase report`` and E-61's export read one
    program, so an E-50 row left behind in the offload program reads as
    ``MISSING`` in the record however well the journey went.
    """
    repo_root = (Path(repo_root) if repo_root is not None else _REPO_ROOT).resolve()
    program_root = Path(program_root).resolve()
    platform_root = Path(platform_root).resolve()
    platform_root.mkdir(parents=True, exist_ok=True)
    caps = caps if caps is not None else probe_capabilities(repo_root=repo_root)
    record_root = Path(record_program_root).resolve() if record_program_root is not None else program_root

    steps: list[dict[str, Any]] = []
    recorder = E2ERecorder(run_id=run_id, plan_blob=caps.plan_blob)
    store = None
    program_id = f"{run_id}-offload"
    extra: dict[str, Any] = {"run_id": run_id, "record_program_root": str(record_root)}

    def _record(check_id: str, status: str, **kwargs: Any) -> None:
        """Land one row wherever the run record lives -- through the journey's
        own store when that IS the record target, and by root otherwise (which
        is also the only way the blocked early return below can record at all,
        since it has opened nothing)."""
        if store is not None and record_root == program_root:
            recorder.record(store, check_id, status, **kwargs)
            return
        evidence = dict(kwargs.pop("evidence", None) or {})
        evidence.setdefault("offload_program_root", str(program_root))
        if recorder.session_id:
            evidence.setdefault("offload_session_id", recorder.session_id)
        _record_into(recorder, record_root, platform_root, check_id, status, evidence=evidence, **kwargs)

    if not caps.offload:
        reason = "the GPU offload protocol is not present on this deployment (`offload` is not a CLI group)"
        _blocked(steps, "offload_capability", "L0-C", reason)
        # the row goes in BEFORE the return: the plan's Section 4 rule is that a
        # blocked check is "recorded blocked with its owner", and E-61 counts
        # blocked rows as present. An early return with no row at all would make
        # a gating check read MISSING and lose the owner with it.
        steps.append({"name": "record_blocked_row", "ok": True,
                      "detail": _try_record_blocked(_record, "E-50", "L0-C", reason, record_root)})
        return _finish("e2e_offload_enqueue", steps, program_root, extra)

    try:
        with _step(steps, "program_init_cli"):
            init = _program_init(program_root, platform_root, program_id=program_id)
        steps.append({"name": "program_init_cli", "ok": True, "detail": init})

        with _step(steps, "write_offload_toml"):
            tables = _write_scratch_toml(
                program_root,
                ocr="offload",
                embed="offload",
                require_real_backends=True,
                # the model key and dimensionality the DEV worker must report
                # back: emb rows and the vector index are keyed by them, so
                # they are the manifest's `expect` block and cannot be
                # defaulted (the marker refuses to load without them).
                embed_extra={"model_key": embed_model_key, "dims": embed_dims},
                ocr_extra={"expect_backend": "marker"},
            )
        steps.append({"name": "write_offload_toml", "ok": True, "detail": {"toml_tables_written": tables}})

        from trialerror.stores.store import open_store

        store = open_store(program_root, platform_root=platform_root)

        with _step(steps, "session_boot_cli_then_hook"):
            boot = _session_boot_cli_then_hook(program_root, platform_root, account_label="e2e-offload")
            session_id = boot["session_id"]
        steps.append({"name": "session_boot_cli_then_hook", "ok": True, "detail": boot})
        recorder.session_id = session_id

        with _step(steps, "hold_journey_launch"):
            _ensure_pool(store, account_id=boot["account_id"])
            held = hold_journey_launch(
                store, session_id=session_id, agent_kind="e2e-offload",
                program_root=program_root, platform_root=platform_root, program_id=program_id,
            )
            launch_id = held["launch_id"]
        steps.append({"name": "hold_journey_launch", "ok": True, "detail": held})

        with _step(steps, "ingest_long_synthetic_markdown"):
            ingested = _ingest_long_synthetic(
                store, program_root, launch_id=launch_id, run_id=run_id, min_chunks=min_chunks
            )
            doc_id = ingested["doc_id"]
        steps.append({"name": "ingest_long_synthetic_markdown", "ok": True, "detail": ingested})

        with _step(steps, "drain_until_embed_deferred"):
            deferred = _assert_embed_parked(
                store, program_root, platform_root, doc_id=doc_id, min_chunks=min_chunks
            )
            job_id = deferred["job_id"]
            chunk_count = deferred["chunk_count"]
        steps.append({"name": "drain_until_embed_deferred", "ok": True, "detail": deferred})

        with _step(steps, "pending_manifest_present"):
            manifest = _assert_pending_manifest(program_root, job_id=job_id, chunk_count=chunk_count)
        steps.append({"name": "pending_manifest_present", "ok": True, "detail": manifest})

        _record(
            "E-50", "pass",
            evidence={
                "doc_id": doc_id, "job_id": job_id, "chunk_count": chunk_count,
                "attempts": deferred["attempts"], "job_state": deferred["job_state"],
                "deferred_events": deferred["deferred_events"],
                "manifest_path": manifest["manifest_path"],
                "branch_shape": manifest["branch_shape"],
            },
        )

        with _step(steps, "reconcile_journey_launch"):
            from trialerror.budget.pools import reconcile_launch

            reconcile_launch(store, launch_id=launch_id, actual_tokens=450)
        steps.append({"name": "reconcile_journey_launch", "ok": True, "detail": {"launch_id": launch_id}})

    except AcceptanceStepError as exc:
        try:
            _record("E-50", "fail", owner="L0-C", evidence={"step": exc.step, "error": exc.detail})
        except Exception:  # noqa: BLE001
            pass
        return _failed("e2e_offload_enqueue", exc, steps, program_root)
    finally:
        if store is not None:
            store.close()

    return _finish(
        "e2e_offload_enqueue", steps, program_root,
        {**extra, "job_id": job_id, "chunk_count": chunk_count, "recorded": recorder.rows},
    )


def _ingest_long_synthetic(
    store: Any, program_root: Path, *, launch_id: str, run_id: str, min_chunks: int
) -> dict[str, Any]:
    from trialerror.ingest import pipeline
    from trialerror.jobs.registry import discover_and_register_handlers

    discover_and_register_handlers()
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    # one section -> one chunk (each section clears the chunker's standalone
    # floor and stays far under its ceiling), plus a margin so a chunker tweak
    # that merges a few neighbours still clears the batch size.
    n_sections = min_chunks + 16
    path = raw_dir / f"e2e_offload_fixture_{run_id}.md"
    path.write_text(_long_synthetic_markdown(n_sections, run_id=run_id), encoding="utf-8")

    source = pipeline.register_source(
        store, kind="report", title=f"[E2E] Synthetic offload fixture {run_id}", license_tier="open",
        acquisition_route="user_delivered", registered_by_launch=launch_id, config={},
    )
    added = pipeline.add_document(
        store, program_root=program_root, source_id=source["source_id"], raw_path=path,
        created_by_launch=launch_id, config={}, yes=True,
    )
    doc_id = added["document"]["doc_id"]
    return {"doc_id": doc_id, "source_id": source["source_id"], "sections": n_sections, "raw_path": str(path)}


def _assert_embed_parked(
    store: Any, program_root: Path, platform_root: Path, *, doc_id: str, min_chunks: int = 0
) -> dict[str, Any]:
    """Drain the queue and prove the embed stage PARKED rather than failed.

    An environmental failure returns the job to state ``pending`` with
    ``next_attempt_ts`` set and ``attempts`` UNCHANGED and writes a ``deferred``
    ledger EVENT -- there is no ``deferred`` job STATE, so this never queries
    one.
    """
    from trialerror.jobs import ledger
    from trialerror.jobs.worker import run_loop

    run_loop(store, worker_id="e2e-offload-worker", poll_interval_s=0.01, max_idle_polls=3)

    chunk_count = _sql_count(store.knowledge, "SELECT COUNT(*) FROM chunk WHERE doc_id=?", (doc_id,))
    if chunk_count < max(min_chunks, 1):
        raise RuntimeError(
            f"the synthetic document produced {chunk_count} chunk(s); the offload batch needs at least "
            f"{max(min_chunks, 1)}"
        )

    rows = [dict(r) for r in store.jobs.execute("SELECT * FROM job WHERE kind='embed'")]
    embed_jobs = [r for r in rows if doc_id in (r.get("payload") or "")]
    if len(embed_jobs) != 1:
        raise RuntimeError(f"expected exactly one embed job for {doc_id}, found {len(embed_jobs)}")
    job = embed_jobs[0]
    job_id = job["job_id"]

    if job["state"] != "pending":
        raise RuntimeError(f"the embed job is {job['state']!r}, expected 'pending' (parked, not failed)")
    if not job.get("next_attempt_ts"):
        raise RuntimeError("the parked embed job has no next_attempt_ts")
    if int(job.get("attempts") or 0) != 0:
        raise RuntimeError(
            f"the parked embed job has attempts={job.get('attempts')}; an absent GPU must cost the job nothing"
        )

    events = ledger.list_events(store, job_id, limit=200)
    types = [e["type"] for e in events]
    deferred_events = types.count("deferred")
    if deferred_events < 1:
        raise RuntimeError(f"the embed job's ledger shows no `deferred` event: {types}")
    for forbidden in ("retry_scheduled", "abandoned", "completed"):
        if forbidden in types:
            raise RuntimeError(f"the parked embed job escalated: ledger carries a {forbidden!r} event")

    for stage in ("normalize", "chunk"):
        done = store.jobs.execute(
            "SELECT state FROM job WHERE kind=? AND payload LIKE ?", (stage, f"%{doc_id}%")
        ).fetchone()
        if done is None or done["state"] != "complete":
            raise RuntimeError(f"the {stage} stage did not complete for {doc_id}")

    return {
        "job_id": job_id,
        "job_state": job["state"],
        "attempts": int(job.get("attempts") or 0),
        "next_attempt_ts": job.get("next_attempt_ts"),
        "deferred_events": deferred_events,
        "ledger_event_types": sorted(set(types)),
        "chunk_count": chunk_count,
    }


def _assert_pending_manifest(program_root: Path, *, job_id: str, chunk_count: int) -> dict[str, Any]:
    """FROZEN: a pending manifest naming this job exists under the program's
    own ``offload/pending/``.

    Everything else here is BRANCH SHAPE -- recorded, not asserted. The plan
    pinned E-50 to the design ids while the offload lane was unmerged and said
    those field names become criteria only under a later changelog line; this
    build reports them so that line can be written against real observations.
    """
    from trialerror.offload import protocol

    root = protocol.offload_root(program_root)
    pending = protocol.pending_dir(root)
    manifest_path = pending / f"{job_id}.json"
    candidates = sorted(p.name for p in pending.glob("*.json")) if pending.is_dir() else []
    if not manifest_path.is_file():
        raise RuntimeError(f"no pending manifest naming {job_id} under {pending} (found {candidates})")

    branch: dict[str, Any] = {"manifest_json_present": True}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        branch["offload_attempts"] = manifest.get("offload_attempts")
        branch["has_expect_block"] = isinstance(manifest.get("expect"), dict)
        branch["stage"] = manifest.get("stage")
    except (OSError, json.JSONDecodeError) as exc:
        branch["manifest_read_error"] = str(exc)

    chunks_file = pending / job_id / "chunks.jsonl"
    if chunks_file.is_file():
        lines = sum(1 for _ in chunks_file.open("r", encoding="utf-8"))
        branch["chunks_jsonl_lines"] = lines
        branch["chunks_jsonl_matches_chunk_count"] = lines == chunk_count
    else:
        branch["chunks_jsonl_lines"] = None

    return {"manifest_path": str(manifest_path), "pending_entries": candidates, "branch_shape": branch}


def verify_e2e_offload_roundtrip(
    program_root: Path,
    platform_root: Path,
    *,
    run_id: str,
    job_id: str,
    chunk_count: int,
    repo_root: Path | None = None,
    expected_dims: int | None = None,
    max_completion_s: float = 1800.0,
    caps: Capabilities | None = None,
    record_program_root: Path | None = None,
) -> CheckResult:
    """E-52's sandbox half: the job the GPU worker published actually landed,
    with real vectors.

    "The worker completed it with fake vectors" is excluded by the model-key
    and dimensionality assertions; "a second local run recomputed it" is
    impossible under ``require_real_backends``, which is exactly why the
    offload program carries that flag.

    ``record_program_root``: as in :func:`run_e2e_offload_enqueue` -- the row
    belongs wherever the rest of the run record is, which is not this program.
    """
    repo_root = (Path(repo_root) if repo_root is not None else _REPO_ROOT).resolve()
    program_root = Path(program_root).resolve()
    platform_root = Path(platform_root).resolve()
    caps = caps if caps is not None else probe_capabilities(repo_root=repo_root)
    record_root = Path(record_program_root).resolve() if record_program_root is not None else program_root

    steps: list[dict[str, Any]] = []
    recorder = E2ERecorder(run_id=run_id, plan_blob=caps.plan_blob)
    store = None
    extra: dict[str, Any] = {"run_id": run_id, "record_program_root": str(record_root)}

    def _record(check_id: str, status: str, **kwargs: Any) -> None:
        if store is not None and record_root == program_root:
            recorder.record(store, check_id, status, **kwargs)
            return
        evidence = dict(kwargs.pop("evidence", None) or {})
        evidence.setdefault("offload_program_root", str(program_root))
        _record_into(recorder, record_root, platform_root, check_id, status, evidence=evidence, **kwargs)

    if not caps.offload:
        reason = "the GPU offload protocol is not present on this deployment"
        _blocked(steps, "offload_capability", "L0-C", reason)
        steps.append({"name": "record_blocked_row", "ok": True,
                      "detail": _try_record_blocked(_record, "E-52", "L0-C", reason, record_root)})
        return _finish("e2e_offload_roundtrip", steps, program_root, extra)

    from trialerror.stores.store import open_store

    try:
        store = open_store(program_root, platform_root=platform_root)

        with _step(steps, "job_complete"):
            job = _job_row(store, job_id)
            if job["state"] != "complete":
                raise RuntimeError(f"job {job_id} is {job['state']!r}, expected 'complete'")
            publish_ts, elapsed_s = _publish_to_settle(program_root, job, job_id=job_id)
            if elapsed_s is not None and elapsed_s > max_completion_s:
                raise RuntimeError(
                    f"the job settled {elapsed_s:.0f}s after the worker published it (bound {max_completion_s:.0f}s)"
                )
            # the plan lists "done/ absent (swept)" as a legitimate branch
            # shape, and a swept done/ takes the publish timestamp with it. The
            # bound is then NOT EVALUATED, and says so in its own status --
            # `elapsed_s: null` beside an unqualified `pass` would read as a
            # bound that held.
            completion_bound = (
                {"status": "pass",
                 "note": f"settled {elapsed_s:.0f}s after the worker published (bound {max_completion_s:.0f}s)"}
                if elapsed_s is not None else
                {"status": "skip",
                 "note": "publish ts unknowable: done/<job_id>/result.json has already been swept, so the "
                         "'<= 30 min after the worker publishes' bound could not be evaluated. The operator "
                         "half of E-52 records the worker's own publish line"}
            )
            completion_bound["max_completion_s"] = max_completion_s
            doc_id = _payload_field(job.get("payload"), "doc_id")
            if not doc_id:
                raise RuntimeError(f"job {job_id} carries no doc_id in its payload")
        steps.append({
            "name": "job_complete", "ok": True,
            "detail": {"job_id": job_id, "settled_ts": job.get("settled_ts"), "publish_ts": publish_ts,
                       "elapsed_s": elapsed_s, "completion_bound": completion_bound},
        })

        with _step(steps, "emb_rows_equal_chunks"):
            emb_rows = _sql_count(
                store.knowledge,
                "SELECT COUNT(*) FROM emb e JOIN chunk c ON c.sha256 = e.chunk_sha256 WHERE c.doc_id = ?",
                (doc_id,),
            )
            doc_chunks = _sql_count(store.knowledge, "SELECT COUNT(*) FROM chunk WHERE doc_id=?", (doc_id,))
            if doc_chunks != chunk_count:
                raise RuntimeError(f"the document has {doc_chunks} chunks, the enqueue journey reported {chunk_count}")
            if emb_rows != chunk_count:
                raise RuntimeError(f"{emb_rows} emb row(s) for {chunk_count} chunk(s)")
        steps.append({
            "name": "emb_rows_equal_chunks", "ok": True,
            "detail": {"emb_rows": emb_rows, "chunk_count": chunk_count},
        })

        with _step(steps, "model_key_is_real"):
            model_keys = sorted({
                r["model_key"]
                for r in store.knowledge.execute(
                    "SELECT DISTINCT e.model_key FROM emb e JOIN chunk c ON c.sha256 = e.chunk_sha256 "
                    "WHERE c.doc_id = ?", (doc_id,)
                )
            })
            fake = [m for m in model_keys if str(m).startswith("fake-")]
            if fake:
                raise RuntimeError(f"emb rows carry FAKE model key(s) {fake} -- these are not real vectors")
            configured = _configured_embed_model_key(program_root)
            if configured is not None and model_keys != [configured]:
                raise RuntimeError(
                    f"emb model keys {model_keys} do not match the configured [ingest.embed] model_key "
                    f"{configured!r}"
                )
        steps.append({"name": "model_key_is_real", "ok": True, "detail": {"model_keys": model_keys}})

        with _step(steps, "dims_match"):
            want = expected_dims if expected_dims is not None else _configured_embed_dims(program_root)
            observed = _observed_emb_dims(store, doc_id)
            if want is not None and observed is not None and observed != want:
                raise RuntimeError(f"vectors are {observed}-dimensional, configured dims is {want}")
        steps.append({"name": "dims_match", "ok": True, "detail": {"dims": observed, "configured_dims": want}})

        with _step(steps, "document_indexed"):
            row = store.knowledge.execute("SELECT status FROM document WHERE doc_id=?", (doc_id,)).fetchone()
            if row is None or row["status"] != "indexed":
                raise RuntimeError(f"document {doc_id} is {row['status'] if row else 'MISSING'!r}, expected 'indexed'")
        steps.append({"name": "document_indexed", "ok": True, "detail": {"doc_id": doc_id, "status": "indexed"}})

        with _step(steps, "doctor_offload_checks"):
            discover_and_register_checks()
            names = ["offload_backlog", "offload_stale_claims", "offload_failed", "fake_backend_rows"]
            results = run_checks(
                DoctorContext(repo_root=repo_root, program_root=program_root, platform_root=platform_root),
                only=names,
            )
            statuses = {r.name: r.status for r in results}
            not_pass = {n: s for n, s in statuses.items() if s != "pass"}
            if not_pass:
                raise RuntimeError(f"offload doctor checks not green after the round trip: {not_pass}")
        steps.append({"name": "doctor_offload_checks", "ok": True, "detail": statuses})

        with _step(steps, "done_dir_observed"):
            branch = _observe_done_dir(program_root, job_id=job_id)
        steps.append({"name": "done_dir_observed", "ok": True, "detail": branch})

        _record(
            "E-52", "pass",
            evidence={
                "job_id": job_id, "completed_ts": job.get("settled_ts"), "publish_ts": publish_ts,
                "elapsed_s": elapsed_s, "completion_bound": completion_bound,
                "emb_rows": emb_rows, "chunk_count": chunk_count,
                "model_key": model_keys, "dims": observed, "doctor": statuses, "branch_shape": branch,
            },
        )

    except AcceptanceStepError as exc:
        try:
            _record("E-52", "fail", owner="L0-C", evidence={"step": exc.step, "error": exc.detail})
        except Exception:  # noqa: BLE001
            pass
        return _failed("e2e_offload_roundtrip", exc, steps, program_root)
    finally:
        if store is not None:
            store.close()

    return _finish("e2e_offload_roundtrip", steps, program_root, {**extra, "recorded": recorder.rows})


def _program_config_raw(program_root: Path) -> dict[str, Any]:
    import tomllib

    path = program_root / "trialerror.toml"
    if not path.is_file():
        return {}
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _configured_embed_model_key(program_root: Path) -> str | None:
    embed = (_program_config_raw(program_root).get("ingest") or {}).get("embed") or {}
    key = embed.get("model_key")
    return str(key) if key else None


def _configured_embed_dims(program_root: Path) -> int | None:
    embed = (_program_config_raw(program_root).get("ingest") or {}).get("embed") or {}
    dims = embed.get("dims")
    return int(dims) if dims else None


def _observed_emb_dims(store: Any, doc_id: str) -> int | None:
    row = store.knowledge.execute(
        "SELECT e.dims FROM emb e JOIN chunk c ON c.sha256 = e.chunk_sha256 WHERE c.doc_id = ? LIMIT 1",
        (doc_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        return int(row["dims"])
    except (KeyError, TypeError, ValueError):
        return None


def _publish_to_settle(program_root: Path, job: Mapping[str, Any], *, job_id: str) -> tuple[str | None, float | None]:
    """When did the GPU worker publish, and how long did the sandbox take to
    settle the job after that? ``done/<job_id>/`` may already have been swept,
    in which case the elapsed time is unknowable and recorded as ``None``
    rather than guessed."""
    from trialerror.offload import protocol
    from trialerror.util.timeutil import parse as parse_ts

    done = protocol.done_dir(protocol.offload_root(program_root)) / job_id / "result.json"
    publish_ts: str | None = None
    if done.is_file():
        try:
            publish_ts = json.loads(done.read_text(encoding="utf-8")).get("published_ts")
        except (OSError, json.JSONDecodeError):
            publish_ts = None
        if publish_ts is None:
            try:
                from datetime import datetime, timezone

                publish_ts = datetime.fromtimestamp(done.stat().st_mtime, tz=timezone.utc).isoformat()
            except OSError:
                publish_ts = None
    settled = job.get("settled_ts")
    if publish_ts is None or not settled:
        return publish_ts, None
    try:
        return publish_ts, (parse_ts(settled) - parse_ts(publish_ts)).total_seconds()
    except Exception:  # noqa: BLE001 - an unparseable timestamp is recorded, never fatal
        return publish_ts, None


def _observe_done_dir(program_root: Path, *, job_id: str) -> dict[str, Any]:
    from trialerror.offload import protocol

    done = protocol.done_dir(protocol.offload_root(program_root)) / job_id
    if not done.is_dir():
        return {"done_dir": "absent (swept)", "result_json": None}
    result = done / "result.json"
    if not result.is_file():
        return {"done_dir": str(done), "result_json": "missing"}
    try:
        payload = json.loads(result.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"done_dir": str(done), "result_json": f"unreadable: {exc}"}
    return {
        "done_dir": str(done),
        "result_json": "present",
        "expect_block": payload.get("expect"),
        "stage": payload.get("stage"),
    }
