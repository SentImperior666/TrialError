"""``VastaiMarkerOcrBackend``: DEV's marker OCR, run on a rented vast.ai GPU
(the vast.ai OCR design, sections 3, 5, 7, 8, 10 and 11).

A subclass of :class:`trialerror.ingest.backends.RealMarkerOcrBackend`, so the
range planner, the digest-checked range cache, the ``{N}`` numbering
resolution, the monotonic check and the page count all stay the inherited
code, running on DEV over the markdown that comes back. **Only the
``marker_single`` invocation moves**: :meth:`_run_marker` runs DEV's own argv
(every element ``shlex.quote``d) through a small wrapper on the instance and
fetches the ``.md`` back with its sha256 checked.

One job, in the worker's order:

1. ``admit(manifest)`` (after the claim, before the pull): the egress
   decision (:func:`~trialerror.vastai.egress.decide_egress`) and the spend
   envelopes (:func:`~trialerror.vastai.pricing.check_envelopes`). A refusal
   writes a ledger ``refused`` row and raises a ``JobRefused`` subclass
   (settlement class R). With ``[vastai.egress] when_refused = "local"`` an
   egress refusal runs the job on DEV's own marker instead (O7).
2. ``run()``: rents LAZILY -- no lease until the first range that is not
   already in DEV's cache. Then: price the remaining ranges, create the lease
   (ledger ``intent`` first), wait for ``running`` and for ssh, bootstrap
   (install; marker version; GPU; ``/dev/shm`` by O8's rule; the canary page,
   which also downloads the models; the model manifest; the range flag),
   and only then upload the document into RAM scratch (ledger ``shipped``).
   Per range: the remote run, the download, the hash checks.
3. ``finally``: wipe the remote scratch best-effort, destroy (confirmed by
   listing), ledger ``outcome`` with the estimated cost.

Settlement (lane contract section 3): a host failure (F) fails over to a
fresh host excluding the machine, within ``[vastai.ocr] max_leases_per_job``
and the caps, re-running only the missing ranges; exhausted, the refusal is
``hosts-exhausted`` (``shm-too-small`` when the last host lacked RAM
scratch). A wrong stack is ``StackMismatch`` (R, and vast.ai stops for the
run), raised before any upload. A remote marker failure, an OOM kill (137)
and a range timeout are class E, exactly as on DEV. ``LeaseExpired`` (X)
propagates.

Nothing identifying reaches the instance: the file is ``input.pdf`` (or
``input<ext>``) under ``/dev/shm/te/<lease id>/``, the lease id is random,
and no job id, doc id or title appears in any API body, label, ``onstart``,
remote path or command. Only DEV's ledger links a lease to a job.

UNTESTED LIVE: every path here runs against fakes (``tests/_vastai_fakes.py``,
``tests/_vastai_shell_fakes.py``). The assumptions to settle at the first live
run are the design's section 15.
"""

from __future__ import annotations

import dataclasses
import difflib
import hashlib
import json
import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from trialerror.ingest.backends import (
    DEFAULT_BOUNDED_DPI,
    DEFAULT_PAGE_RANGE_FLAG,
    DEFAULT_PAGE_RANGE_NUMBERING,
    OcrRangesIncomplete,
    OcrResult,
    PageRange,
    RealMarkerOcrBackend,
    load_ocr_backend,
    page_boxes,
    plan_page_ranges,
)
from trialerror.ingest.errors import PageRangeFlagMissingError
from trialerror.jobs.worker import EnvironmentalFailure
from trialerror.offload.settle import ClaimReturned
from trialerror.vastai import shell as sh
from trialerror.vastai.api import VastClient
from trialerror.vastai.config import (
    EXECUTOR_VASTAI,
    VastConfig,
    check_runtime_files,
    load_vast_config,
    runtime_files,
)
from trialerror.vastai.egress import EgressDecision, decide_egress, document_facts, read_egress_approval
from trialerror.vastai.errors import (
    EgressRefused,
    HostFailure,
    JobRefused,
    StackMismatch,
    VastApiError,
    VastConfigError,
    VastError,
    VastPlanRefused,
    VastSpendRefused,
)
from trialerror.vastai.ledger import Ledger, default_state_dir, utc_iso
from trialerror.vastai.lease import LeaseExpired, OcrInstanceLease
from trialerror.vastai.pricing import (
    OUTPUT_BYTES_PER_PAGE,
    DocumentPlan,
    check_envelopes,
    derived_range_timeout_s,
    plan_document,
    price_job,
    spend_from_ledger,
)
from trialerror.vastai.reaper import reap_ocr
from trialerror.vastai.sshkeys import check_ssh_key

__all__ = [
    "PEAK_RSS_SOURCE_REMOTE",
    "REMOTE_MODEL_CACHE",
    "REMOTE_MARKER",
    "TOOL_FILES",
    "SHM_MARGIN_BYTES",
    "FACTORY_OVERRIDES",
    "VastRunState",
    "VastaiMarkerOcrBackend",
    "canary_text",
    "canary_similarity",
    "parse_models_manifest",
    "bootstrap_failure_text",
]

#: What a range record's ``peak_rss_source`` says when the number came from
#: the remote wrapper: its single child's own peak, not DEV's cumulative one.
PEAK_RSS_SOURCE_REMOTE = "remote wrapper: ru_maxrss of this invocation"
#: Where the instance keeps marker's models ([assumption] the platform default
#: on Linux, set explicitly through MODEL_CACHE_DIR by the bootstrap and the
#: wrapper, so the cache hashed is the cache loaded). Container disk: the
#: weights are not sensitive.
REMOTE_MODEL_CACHE = "/root/.cache/datalab/models"
#: The name of the instance's own marker CLI: the console script the bootstrap
#: looks for in the environment it installed into, and whose ABSOLUTE path it
#: reports back as ``marker_exe`` (live finding 4 -- a non-interactive ssh
#: command's PATH is not the login shell's, so nothing is run by bare name).
#: Never exposed as ``marker_single_exe`` (that attribute names a file on THIS
#: machine).
REMOTE_MARKER = "marker_single"
#: Package data shipped to every lease (``trialerror/vastai/remote/``).
TOOL_FILES = ("bootstrap.sh", "te_range.py", "te_canary.py", "canary_text.txt")
#: [estimate] RAM scratch beyond the document and its markdown: one range's
#: extracted images before the wrapper prunes them, and headroom.
SHM_MARGIN_BYTES = 256_000_000
#: Seconds added to a range's own timeout for ssh and the wrapper.
WRAPPER_SLACK_S = 120.0
SMALL_CMD_TIMEOUT_S = 120.0
CANARY_TIMEOUT_S = 1800.0
MODELS_HASH_TIMEOUT_S = 900.0
DOWNLOAD_TIMEOUT_S = 300.0
BOOTSTRAP_PREFIX = "TE-BOOTSTRAP "
#: The bootstrap's bounded failure digest (live finding 4), on stderr.
BOOTSTRAP_FAIL_PREFIX = "TE-BOOTSTRAP-FAIL "
BOOTSTRAP_HEAD_MARKER = "TE-BOOTSTRAP-LOG-HEAD"
BOOTSTRAP_TAIL_MARKER = "TE-BOOTSTRAP-LOG-TAIL"
BOOTSTRAP_END_MARKER = "TE-BOOTSTRAP-END"
RANGE_PREFIX = "TE-RANGE "
#: Test seam: keyword overrides applied by :meth:`VastaiMarkerOcrBackend.from_toml`
#: (``client``, ``shell_factory``, ``clock``, ``sleep``, ``watchdog`` ...), so a
#: backend built by ``ConfigDevBackends`` can run against fakes.
FACTORY_OVERRIDES: dict[str, Any] = {}

_REMOTE_DIR = Path(__file__).resolve().parent / "remote"
_PAGE_MARKER_RE = re.compile(r"^\{\d+\}-{3,}\s*$", re.MULTILINE)
_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,8}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _stderr_log(message: str) -> None:
    print(message, file=sys.stderr)


def _tool_bytes(name: str) -> bytes:
    """A package-data file, LF line endings (the instance runs ``sh``)."""
    return (_REMOTE_DIR / name).read_bytes().replace(b"\r\n", b"\n")


def canary_text() -> str:
    """The canary page's known text (the same file the instance renders)."""
    return _tool_bytes("canary_text.txt").decode("utf-8")


def _normalise(text: str) -> str:
    text = _PAGE_MARKER_RE.sub(" ", text)
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text.lower()).split())


def canary_similarity(expected: str, got: str) -> float:
    """Normalised similarity (0-1) of the canary's OCR to its known text:
    lower-cased, page markers and punctuation dropped, whitespace collapsed."""
    a, b = _normalise(expected), _normalise(got)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def parse_models_manifest(text: str) -> dict[str, str]:
    """``sha256sum``-format lines -> ``{relative path: sha256}``."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, rel = line.partition(" ")
        rel = rel.strip().lstrip("*").replace("\\", "/")
        if rel.startswith("./"):
            rel = rel[2:]
        if digest and rel:
            out[rel] = digest.lower()
    return out


def _prefixed_json(stdout: bytes, prefix: str) -> dict[str, Any] | None:
    for line in reversed(stdout.decode("utf-8", "replace").splitlines()):
        if line.startswith(prefix):
            try:
                value = json.loads(line[len(prefix):])
            except ValueError:
                return None
            return value if isinstance(value, dict) else None
    return None


def _tail(data: bytes | str, n: int = 600) -> str:
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
    return text[-n:].strip()


def bootstrap_failure_text(err: bytes | str, *, line_chars: int = 200, max_lines: int = 20) -> str | None:
    """The bootstrap's own bounded digest of a failed install, read back (live
    finding 4). ``None`` when the instance printed none -- then the caller falls
    back to a plain tail, as before. Never pip's whole essay: the stage, the
    interpreter and pip it used, and at most ``max_lines`` lines of the log's
    head and tail, each cut to ``line_chars``."""
    text = err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err)
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith(BOOTSTRAP_FAIL_PREFIX.strip()))
    except StopIteration:
        return None
    head_line = lines[start][len(BOOTSTRAP_FAIL_PREFIX.strip()):]
    # V9-F04: `venv_error` is the ONE field carrying free text from the image, and
    # it is written last. Take it off whole before the generic split, or a message
    # holding a ` word=` run would split into bogus fields -- and, since ``dict``
    # keeps the LAST pair, could overwrite the real `stage`, `python` or `pip`.
    remote_free_text = re.search(r"\svenv_error=(.*)$", head_line)
    if remote_free_text is not None:
        head_line = head_line[:remote_free_text.start()]
    fields = dict(re.findall(r"(\w+)=(.*?)(?=\s+\w+=|$)", head_line))
    head: list[str] = []
    tail: list[str] = []
    bucket: list[str] | None = None
    for line in lines[start + 1:]:
        if line.strip() == BOOTSTRAP_HEAD_MARKER:
            bucket = head
        elif line.strip() == BOOTSTRAP_TAIL_MARKER:
            bucket = tail
        elif line.strip() == BOOTSTRAP_END_MARKER:
            break
        elif bucket is not None and len(bucket) < max_lines:
            bucket.append(line.strip()[:line_chars])
    where = fields.get("python") or "an unknown interpreter"
    # Live records (canary attempt 3, 2026-09-20): the fail line carries `venv_error` last, so a fallback env in a
    # FAILED bootstrap says why it was a fallback. "none" is "no venv was tried".
    venv_error = (remote_free_text.group(1) if remote_free_text is not None else "").strip()
    parts = [
        f"stage {fields.get('stage', 'unknown')!r} with {where} "
        f"(Python {fields.get('python_version', 'unknown')}, pip {fields.get('pip_version') or fields.get('pip', 'unknown')}, "
        f"env {fields.get('env', 'none')}"
        + (f" after venv failed: {venv_error[:line_chars]}" if venv_error and venv_error != "none" else "")
        + f", externally managed: {fields.get('externally_managed', 'unknown')})"
    ]
    if head:
        parts.append("log head: " + " | ".join(head))
    if tail and tail != head:
        parts.append("log tail: " + " | ".join(tail))
    return "; ".join(parts)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _refuse_path_args(extra_args: Sequence[str]) -> None:
    """``[ingest.ocr] marker_extra_args`` go to the instance verbatim; one that
    names a path would name a DEV path there (design 5.3) and would not
    resolve anyway."""
    for arg in extra_args:
        value = str(arg).split("=", 1)[-1]
        if "/" in value or "\\" in value or _DRIVE_RE.match(value):
            raise VastConfigError(
                f"[ingest.ocr] marker_extra_args carries {arg!r}, which looks like a path: with executor = "
                "\"vastai\" the arguments go to the rented host, where a DEV path must never appear",
                next_actions=["remove the path argument from [ingest.ocr] marker_extra_args"],
            )


@dataclass
class VastRunState:
    """What every backend instance of ONE worker run shares (owned by
    ``ConfigDevBackends``): the run id the per-run spend cap keys on, and the
    latch a ``disable_executor`` refusal sets."""

    worker_run_id: str = field(default_factory=lambda: f"WRUN-{secrets.token_hex(8)}")
    disabled: dict[str, Any] | None = None


class _ShmTooSmall(HostFailure):
    """The host's ``/dev/shm`` cannot hold the document and ``[vastai.egress]``
    requires RAM scratch for it (O8): F, then R as ``shm-too-small``."""


@dataclass
class _Job:
    facts: dict[str, Any]
    route: str  # "vastai" | "local"
    decision: EgressDecision | None = None
    refusal: dict[str, Any] | None = None


@dataclass
class _Live:
    lease: OcrInstanceLease
    dir: str
    shell: Any = None
    scratch: str = ""
    scratch_kind: str | None = None
    #: What the bootstrap reported free in ``/dev/shm`` (live records, canary attempt 3, 2026-09-20): the
    #: number ``_choose_scratch`` decided on, kept so the ledger says how much
    #: room a rented host actually had -- the question "will a host hold this
    #: document in RAM?" was otherwise answerable only by renting one.
    shm_avail_bytes: int | None = None
    remote_input: str = ""
    #: The container-disk twin of ``dir``: the venv the bootstrap may build goes
    #: here, never into RAM scratch, and the cleanup wipes it.
    disk_dir: str = ""
    #: What the bootstrap reported (finding 4): the interpreter that imports the
    #: image's torch (or the venv built from it) and the ``marker_single`` it
    #: installed, both absolute. Every later remote command goes through these.
    python_exe: str = ""
    marker_exe: str = ""
    instance: dict[str, Any] = field(default_factory=dict)
    versions: dict[str, Any] = field(default_factory=dict)
    canary_similarity: float | None = None
    models_extra_files: int | None = None
    known_hosts: Path | None = None
    uploaded_bytes: int = 0
    downloaded_bytes: int = 0
    upload_s: float | None = None
    shipped: bool = False
    ranges_run: list[str] = field(default_factory=list)
    pages_run: int = 0
    compute_s: float = 0.0
    peaks: list[int | None] = field(default_factory=list)


class _RemotePeakProbe:
    """:meth:`RealMarkerOcrBackend._peak_rss_probe` for a remote invocation:
    reports the wrapper's ``ru_maxrss`` of the range that just ran."""

    def __init__(self, backend: "VastaiMarkerOcrBackend") -> None:
        self._backend = backend
        self.peak_rss_bytes: int | None = None
        self.source: str | None = None

    def __enter__(self) -> "_RemotePeakProbe":
        self._backend._last_remote_peak = None
        return self

    def __exit__(self, *_exc: Any) -> None:
        peak = self._backend._last_remote_peak
        if isinstance(peak, int) and not isinstance(peak, bool) and peak > 0:
            self.peak_rss_bytes = peak
            self.source = PEAK_RSS_SOURCE_REMOTE


def _default_shell_factory(*, host: str, port: int, identity_path: Path, known_hosts: Path) -> sh.SshShell:
    return sh.SshShell(host=host, port=port, identity_path=identity_path, known_hosts=known_hosts)


class VastaiMarkerOcrBackend(RealMarkerOcrBackend):
    """See the module docstring. Built by
    ``trialerror.offload.worker.ConfigDevBackends`` for ``[ingest.ocr]
    executor = "vastai"`` through :meth:`from_toml`."""

    name = "marker"
    supports_page_ranges = True

    def __init__(
        self,
        cfg: VastConfig,
        *,
        extra_args: Sequence[str] = (),
        page_range_flag: str = DEFAULT_PAGE_RANGE_FLAG,
        bounded_dpi: float = DEFAULT_BOUNDED_DPI,
        page_range_numbering: str = DEFAULT_PAGE_RANGE_NUMBERING,
        local_backend: RealMarkerOcrBackend | None = None,
        run_state: VastRunState | None = None,
        client: Any = None,
        shell_factory: Callable[..., Any] | None = None,
        state_dir: Path | str | None = None,
        ledger: Ledger | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] | None = None,
        watchdog: bool = True,
        key_reader: Callable[[Any], str] | None = None,
    ) -> None:
        if cfg.executor != EXECUTOR_VASTAI:
            raise VastConfigError("VastaiMarkerOcrBackend needs [ingest.ocr] executor = \"vastai\"")
        _refuse_path_args(extra_args)
        # ``marker_single_exe`` stays EMPTY: the attribute names a file on THIS
        # machine (the doctor reports its existence); the instance's marker is
        # REMOTE_MARKER and is never exposed under it.
        super().__init__(
            marker_single_exe="",
            version=cfg.marker_version,
            extra_args=extra_args,
            page_range_flag=page_range_flag,
            max_range_pixels=cfg.ocr.max_range_pixels,
            bounded_dpi=bounded_dpi,
            page_range_numbering=page_range_numbering,
        )
        self.cfg = cfg
        self.local_backend = local_backend if cfg.egress.when_refused == "local" else None
        self.run_state = run_state or VastRunState()
        self.client = client if client is not None else VastClient(cfg.api_key_path)
        self.shell_factory = shell_factory or _default_shell_factory
        self.state_dir = Path(state_dir) if state_dir is not None else default_state_dir()
        self.clock = clock
        self.sleep = sleep
        self.key_reader = key_reader
        self.ledger = ledger if ledger is not None else Ledger(self.state_dir, now=self._now, secrets=self._ledger_secrets)
        self.log = log or _stderr_log
        self.watchdog = bool(watchdog)
        self._job: _Job | None = None
        self._live: _Live | None = None
        self._plan: list[PageRange] = []
        self._boxes: list[tuple[float, float]] | None = None
        self._input_path: Path | None = None
        self._leases_used = 0
        self._excluded: list[Any] = []
        self._lease_rows: list[dict[str, Any]] = []
        self._ranges_cached = 0
        self._provenance: dict[str, Any] | None = None
        self._last_remote_peak: int | None = None
        self._ever_leased = False
        self._ssh_key_checked = False

    # ------------------------------------------------------------------ build
    @classmethod
    def from_toml(
        cls, toml: Mapping[str, Any], *, config_root: Path | str, run_state: VastRunState | None = None, **overrides: Any
    ) -> "VastaiMarkerOcrBackend":
        """Build from DEV's whole toml. Pure apart from reading nothing: the
        runtime files are checked by :meth:`startup`, never opened here."""
        ingest_ocr = dict(((toml.get("ingest") or {}).get("ocr")) or {})
        backend = ingest_ocr.get("backend", "fake")
        if backend != "marker":
            raise VastConfigError(
                f"[ingest.ocr] executor = \"vastai\" runs marker on a rented GPU, so [ingest.ocr] backend must be "
                f"\"marker\" (it is {backend!r})",
                next_actions=["set [ingest.ocr] backend = \"marker\", or [ingest.ocr] executor = \"local\""],
            )
        cfg = load_vast_config(toml, config_root=config_root)
        local = None
        if cfg.egress.when_refused == "local":
            # DEV's own marker, DEV's own budgets (O7): the table as it would be
            # with the executor absent.
            local = load_ocr_backend({k: v for k, v in ingest_ocr.items() if k != "executor"})
        kwargs = {**FACTORY_OVERRIDES, **overrides}
        return cls(
            cfg,
            extra_args=list(ingest_ocr.get("marker_extra_args", []) or []),
            page_range_flag=ingest_ocr.get("page_range_flag", DEFAULT_PAGE_RANGE_FLAG),
            bounded_dpi=ingest_ocr.get("bounded_dpi", DEFAULT_BOUNDED_DPI),
            page_range_numbering=ingest_ocr.get("page_range_numbering", DEFAULT_PAGE_RANGE_NUMBERING),
            local_backend=local,
            run_state=run_state,
            **kwargs,
        )

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), timezone.utc)

    def _ledger_secrets(self) -> list[str]:
        """The key, for the ledger's redaction (its second line of defence):
        read at each append through the same reader the client uses."""
        reader = self.key_reader
        if reader is None:
            from trialerror.vastai.api import read_api_key as reader
        return [reader(self.cfg.api_key_path)]

    def _say(self, message: str) -> None:
        # Loud lines reach the operator's terminal at once, whatever log the
        # worker collects for its envelope.
        if message.lstrip().startswith("!") and self.log is not _stderr_log:
            _stderr_log(message)
        self.log(message)

    # ------------------------------------------------------- worker lifecycle
    def startup(self) -> None:
        """Worker start (design step 0): every runtime file exists (never
        opened), then one reap pass for this DEV root. An account that cannot
        be listed is logged loudly and the worker goes on: a blind reaper
        must not pretend it found nothing, and it must not stop DEV's worker
        either -- the next doctor run and ``trialerror vastai reap --ocr`` report
        it."""
        check_runtime_files(self.cfg)
        self._reap("start")

    def _reap(self, when: str) -> None:
        try:
            entries = reap_ocr(
                self.client, config_root=self.cfg.config_root, state_dir=self.state_dir, ledger=self.ledger,
                clock=self.clock, log=self._say,
            )
        except VastError as exc:
            self._say(
                f"!!! vast.ai reap at worker {when} could not list the account ({exc}); continuing. Run "
                "`trialerror vastai reap --ocr --backend-config-root <root>` and check the vast.ai console."
            )
            return
        acted = [e for e in entries if e.get("action") in ("destroyed", "destroy_failed")]
        if acted:
            self._say(f"  . vast.ai reap at worker {when}: {len(acted)} instance(s) of this DEV root acted on")

    def runtime_report(self) -> dict[str, Any]:
        """For ``ConfigDevBackends.describe()`` and the doctor: which runtime
        files EXIST (never their contents), and the refusal a worker would
        start with."""
        files = runtime_files(self.cfg)
        approval = self.cfg.approval_path
        files["[vastai] approval_path"] = {
            "path": str(approval) if approval is not None else None,
            "exists": bool(approval is not None and approval.is_file()),
        }
        if self.local_backend is not None:
            exe = str(self.local_backend.marker_single_exe)
            files["[ingest.ocr] marker_single_exe (when_refused = \"local\")"] = {
                "path": exe, "exists": Path(exe).exists(),
            }
        refusal = None
        try:
            check_runtime_files(self.cfg)
        except VastConfigError as exc:
            refusal = f"VastConfigError: {exc}"
        return {
            "executor": EXECUTOR_VASTAI,
            "status": self.cfg.status,
            "when_refused": self.cfg.egress.when_refused,
            "files": files,
            "refusal": refusal,
        }

    def on_pause(self) -> None:
        """The worker is about to block in a pause, claim held: destroy the
        live lease so the pause does not bill. The finished ranges are in
        DEV's cache; the next range leases again. Never raises."""
        try:
            if self._live is not None:
                self._say("  . vast.ai: pause -- destroying the live lease; resume leases again for the remaining ranges")
                self._end_live("returned", "pause")
                # A pause is the operator's, not a host failure: the next lease
                # starts a fresh failover budget.
                self._leases_used = 0
        except Exception as exc:  # noqa: BLE001 - a pause must still pause
            self._say(f"! vast.ai: ending the lease at a pause failed: {exc}")

    def close(self) -> None:
        """The worker run is over (or the resident backend is switched out):
        destroy anything live, then one reap pass if this instance ever
        leased. Never raises."""
        try:
            if self._live is not None:
                self._end_live("returned", "close")
        except Exception as exc:  # noqa: BLE001
            self._say(f"! vast.ai: closing the live lease failed: {exc}")
        if self._ever_leased:
            self._ever_leased = False
            self._reap("exit")

    def result_fields(self) -> dict[str, Any]:
        """The ``vastai`` provenance block for ``result.json`` (design 9.6),
        only for a job that ran on vast.ai."""
        if self._job is None or self._job.route != "vastai" or self._provenance is None:
            return {}
        return {"vastai": dict(self._provenance)}

    # --------------------------------------------------------------- admission
    def admit(self, manifest: Mapping[str, Any]) -> None:
        """After the claim, before the pull. Returns (admitted, or routed to
        DEV's own marker by ``when_refused = "local"``) or raises a
        ``JobRefused`` subclass after writing a ledger ``refused`` row."""
        self._job = None
        self._provenance = None
        facts = document_facts(manifest)  # a malformed manifest is class E
        disabled = self.run_state.disabled
        if disabled is not None:
            self._refuse(
                VastPlanRefused(
                    "vastai-disabled-for-run",
                    f"vast.ai use stopped for this worker run after a {disabled.get('reason_code')} refusal "
                    f"({disabled.get('message')}). Nothing was sent.",
                    next_actions=["fix the cause named above, then start a new worker run"],
                    details={"disabled_by": dict(disabled)},
                ),
                facts,
            )
        try:
            approval = read_egress_approval(self.cfg) if self.cfg.egress.require_approval else None
            decision = decide_egress(
                self.cfg, manifest, approval=approval, now=self._now(),
                **({"key_reader": self.key_reader} if self.key_reader is not None else {}),
            )
            if not decision.allowed:
                raise decision.refusal()
        except EgressRefused as refusal:
            if self.local_backend is not None:
                self._ledger_refused(refusal, facts, routed="local")
                self._job = _Job(facts=facts, route="local", refusal=refusal.as_dict())
                self._say(
                    f"  . {facts['job_id']}: egress refused [{refusal.reason_code}] -- running it on DEV's own "
                    "marker ([vastai.egress] when_refused = \"local\"); nothing was sent"
                )
                return
            self._refuse(refusal, facts)
        spend = spend_from_ledger(
            self.ledger, approval_nonce=decision.approval_nonce, worker_run_id=self.run_state.worker_run_id
        )
        try:
            check_envelopes(self.cfg, spend, approval_max_total_usd=decision.approval_max_total_usd)
        except VastSpendRefused as refusal:
            self._refuse(refusal, facts)
        self._job = _Job(facts=facts, route="vastai", decision=decision)

    def _ledger_refused(self, refusal: ClaimReturned, facts: Mapping[str, Any], **extra: Any) -> None:
        if getattr(refusal, "_te_ledgered", False):
            return
        try:
            self.ledger.append(
                "refused",
                sha256=facts["sha256"],
                bytes=facts["bytes"],
                reason_code=refusal.reason_code,
                job_id=facts.get("job_id"),
                doc_id=facts.get("doc_id"),
                license_tier=facts.get("license_tier"),
                message=refusal.message,
                next_actions=list(refusal.next_actions),
                worker_run_id=self.run_state.worker_run_id,
                details=dict(refusal.details),
                **extra,
            )
        except Exception as exc:  # noqa: BLE001 - the refusal itself must still land
            self._say(f"!!! vast.ai ledger 'refused' row could not be written: {exc}")
        try:
            refusal._te_ledgered = True  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover
            pass

    def _refuse(self, refusal: ClaimReturned, facts: Mapping[str, Any]) -> None:
        self._ledger_refused(refusal, facts)
        self._note_disable(refusal)
        raise refusal

    def _note_disable(self, refusal: ClaimReturned) -> None:
        if refusal.disable_executor and self.run_state.disabled is None:
            self.run_state.disabled = {"reason_code": refusal.reason_code, "message": refusal.message}
            self._say(f"! vast.ai stops for the rest of this worker run: [{refusal.reason_code}] {refusal.message}")

    # ---------------------------------------------------------------- planning
    def plan_ranges(self, input_path: Path) -> list[PageRange] | None:
        """DEV's planner with the vast.ai budgets (``[vastai.ocr]
        max_range_pixels`` and ``max_range_pages``); DEV's own plan for a job
        routed to DEV's marker."""
        if self._job is not None and self._job.route == "local" and self.local_backend is not None:
            return self.local_backend.plan_ranges(input_path)
        try:
            boxes = page_boxes(Path(input_path))
        except Exception:  # noqa: BLE001 - any unreadable page tree is "cannot chunk"
            return None
        if not boxes:
            return None
        return plan_page_ranges(
            boxes,
            dpi=self.planning_dpi,
            max_range_pixels=self.cfg.ocr.max_range_pixels,
            max_range_pages=self.cfg.ocr.max_range_pages,
        )

    def _require_page_range_flag(self) -> None:
        """Deferred: the probe runs against the REMOTE ``marker_single --help``
        in each lease's bootstrap, before the document is uploaded -- and not
        at all when every range is already cached (nothing is rented)."""
        return None

    def _peak_rss_probe(self) -> _RemotePeakProbe:
        return _RemotePeakProbe(self)

    # ------------------------------------------------------------------ running
    def run(
        self,
        *,
        input_path: Path,
        work_dir: Path,
        ranges: Sequence[PageRange] | None = None,
        on_range: Callable[[int, int], str] | None = None,
        on_range_record: Callable[[dict[str, Any]], None] | None = None,
        cache_dir: Path | None = None,
    ) -> OcrResult:
        job = self._job
        if job is None:
            raise VastError(
                "VastaiMarkerOcrBackend.run() without an admitted job: nothing may leave DEV (the offload worker "
                "calls admit(manifest) after the claim)"
            )
        if job.route == "local":
            return self.local_backend.run(  # type: ignore[union-attr]
                input_path=input_path, work_dir=work_dir, ranges=ranges, on_range=on_range,
                on_range_record=on_range_record, cache_dir=cache_dir,
            )
        input_path = Path(input_path)
        actual = _sha256_file(input_path)
        if actual != job.facts["sha256"]:
            raise VastError(
                f"the file about to be sent ({actual[:12]}...) is not the document the egress policy admitted "
                f"({str(job.facts['sha256'])[:12]}...); nothing was sent"
            )
        self._input_path = input_path
        self._plan = list(ranges) if ranges is not None else (self.plan_ranges(input_path) or [])
        try:
            self._boxes = page_boxes(input_path)
        except Exception:  # noqa: BLE001 - not a PDF: priced as one page
            self._boxes = None
        self._leases_used = 0
        self._excluded = []
        self._lease_rows = []
        self._ranges_cached = 0
        self._provenance = None
        wrapped = on_range_record

        def _count(record: dict[str, Any]) -> None:
            if record.get("cached"):
                self._ranges_cached += 1
            if wrapped is not None:
                wrapped(record)

        try:
            result = super().run(
                input_path=input_path, work_dir=work_dir, ranges=self._plan, on_range=on_range,
                on_range_record=_count, cache_dir=cache_dir,
            )
        except OcrRangesIncomplete:
            self._end_live("returned", "stop")
            raise
        except ClaimReturned as refusal:
            self._end_live("returned", "refused", error=str(refusal))
            self._ledger_refused(refusal, job.facts)
            self._note_disable(refusal)
            raise
        except LeaseExpired:
            self._end_live("expired", "expired")
            raise
        except KeyboardInterrupt:
            self._end_live("returned", "interrupted")
            raise
        except BaseException as exc:
            self._end_live("failed", "error", error=f"{type(exc).__name__}: {exc}")
            raise
        self._end_live("published", "done")
        self._provenance = self._build_provenance()
        return result

    def _run_marker(self, input_path: Path, out_dir: Path, *, page_range: PageRange | None) -> str:
        """One range on the rented host (the inherited loop calls this only
        for a range the cache does not hold). Host failures fail over here,
        so the loop simply continues with the next range on the new host."""
        while True:
            try:
                live = self._live or self._open_lease(page_range)
                return self._remote_range(live, page_range)
            except HostFailure as failure:
                self._fail_over(failure)

    # ---------------------------------------------------------------- leases
    def _remaining(self, page_range: PageRange | None) -> list[PageRange]:
        if page_range is None or page_range not in self._plan:
            return list(self._plan)
        return self._plan[self._plan.index(page_range):]

    def _document_plan(self, remaining: Sequence[PageRange]) -> DocumentPlan:
        boxes = self._boxes or [(612.0, 792.0)]
        base = plan_document(boxes, self.cfg, dpi=self.planning_dpi)
        if not remaining or list(remaining) == list(base.ranges):
            return base
        return dataclasses.replace(base, pages=sum(r.count for r in remaining), ranges=tuple(remaining))

    def _preflight_ssh_key(self) -> None:
        """Before the FIRST create of this executor: one free read of the
        account's registered ssh keys against the configured identity's
        public half.

        vast.ai puts only ACCOUNT keys on a new instance (the create call
        carries none), so an unregistered identity makes every rental refuse
        AFTER it is billing -- canary C4 of 2026-09-19 paid 313 s and $0.0309
        for that answer. Here it costs nothing. Only a key list that was read
        and does not hold the fingerprint refuses; an endpoint or a payload
        this version cannot read leaves the run exactly as it was.
        """
        if self._ssh_key_checked:
            return
        self._ssh_key_checked = True
        check = check_ssh_key(self.client, self.cfg.ssh_identity_path)
        if check.refuses:
            raise VastPlanRefused(
                "key-missing",
                f"{check.message()} Nothing was sent.",
                next_actions=check.next_actions(),
                disable_executor=True,
            )
        if check.state == "unknown":
            self._say(f"  . vast.ai ssh key pre-flight skipped: {check.detail}")

    def _open_lease(self, page_range: PageRange | None) -> _Live:
        self._preflight_ssh_key()
        job = self._job
        assert job is not None and job.decision is not None
        facts, decision = job.facts, job.decision
        plan = self._document_plan(self._remaining(page_range))
        spend = spend_from_ledger(
            self.ledger, approval_nonce=decision.approval_nonce, worker_run_id=self.run_state.worker_run_id,
            job_id=facts.get("job_id"),
        )
        priced = price_job(
            self.client, self.cfg, plan, document_bytes=int(facts["bytes"]), spend=spend,
            approval_max_job_usd=decision.approval_max_job_usd,
            approval_max_total_usd=decision.approval_max_total_usd,
            exclude_machine_ids=list(self._excluded), now=self._now(), key_reader=self.key_reader,
        )
        intent = {
            "job_id": facts.get("job_id"),
            "doc_id": facts.get("doc_id"),
            "sha256": facts["sha256"],
            "bytes": int(facts["bytes"]),
            "license_tier": facts.get("license_tier"),
            "approval_nonce": decision.approval_nonce,
            "worker_run_id": self.run_state.worker_run_id,
            "tier": self.cfg.tier,
            "plan_pages": plan.pages,
            "plan_ranges": plan.n_ranges,
            "excluded_machine_ids": list(self._excluded),
        }
        lease = OcrInstanceLease(
            self.client, config_root=self.cfg.config_root, offers=priced.offers, image=self.cfg.image,
            disk_gb=self.cfg.disk_gb, state_dir=self.state_dir, ledger=self.ledger, intent=intent,
            log=self._say, clock=self.clock, sleep=self.sleep, watchdog=self.watchdog,
        )
        lease.__enter__()
        self._leases_used += 1
        self._ever_leased = True
        live = _Live(lease=lease, dir=sh.lease_dir(str(lease.lease_id)),
                     disk_dir=sh.lease_dir(str(lease.lease_id), root=sh.REMOTE_ROOT_DISK))
        self._live = live
        started = self.clock()
        live.instance = lease.wait_ready(self.cfg.poll_interval_s, timeout_s=self.cfg.startup_s)
        live.known_hosts = sh.known_hosts_path(self.state_dir, str(lease.lease_id))
        live.shell = self.shell_factory(
            host=str(live.instance.get("ssh_host")), port=int(live.instance.get("ssh_port")),
            identity_path=self.cfg.ssh_identity_path, known_hosts=live.known_hosts,
        )
        budget = max(60.0, float(self.cfg.startup_s) - (self.clock() - started))
        try:
            live.shell.wait_reachable(check=lease.check, sleep=self.sleep, timeout_s=budget, clock=self.clock)
        except sh.IdentityRejected as exc:
            raise VastPlanRefused(
                "key-missing",
                f"{exc} Nothing was sent.",
                next_actions=["register the dedicated key pair's public half with the vast.ai account",
                              "or point [vastai] ssh_identity_path at its private half"],
                disable_executor=True,
            ) from None
        except sh.ShellError as exc:
            raise self._host_failure(live, f"ssh never became usable: {exc}") from None
        self._bootstrap(live)
        self._upload(live)
        return live

    def _host_failure(self, live: _Live, message: str, cls: type[HostFailure] = HostFailure) -> HostFailure:
        return cls(message, machine_id=live.lease.machine_id, instance_id=live.lease.instance_id)

    def _raise_if_expired(self, live: _Live) -> None:
        if live.lease.poll_watchdog() or live.lease.expired:
            raise LeaseExpired(
                f"vast.ai lease {live.lease.lease_id} reached its TTL; instance {live.lease.instance_id} destroyed "
                "mid-job (raise [vastai.ocr] startup_s or safety if this repeats)"
            )

    def _cmd(self, live: _Live, cmd: str, *, timeout_s: float, what: str, stdin: bytes | None = None,
             raise_timeout: bool = False) -> tuple[int, bytes, bytes]:
        """One remote command. ssh itself failing (exit 255) is a host
        failure -- or the TTL, when the watchdog has fired. A timeout is a
        host failure too, unless the caller settles it (``raise_timeout``:
        a range's own timeout is class E, as on DEV)."""
        try:
            rc, out, err = live.shell.run(cmd, stdin_bytes=stdin, timeout_s=timeout_s)
        except sh.RemoteTimeout:
            self._raise_if_expired(live)
            if raise_timeout:
                raise
            raise self._host_failure(live, f"{what} did not return within {timeout_s:.0f} s") from None
        except sh.ShellError as exc:
            self._raise_if_expired(live)
            raise self._host_failure(live, f"ssh could not run {what}: {exc}") from None
        if rc == 255:
            self._raise_if_expired(live)
            raise self._host_failure(live, f"ssh lost during {what}: {_tail(err)}")
        return rc, out, err

    def _transfer(self, live: _Live, fn: Callable[[], Any], what: str) -> Any:
        try:
            return fn()
        except sh.RemoteTimeout:
            self._raise_if_expired(live)
            raise self._host_failure(live, f"{what} timed out") from None
        except sh.ShellError as exc:
            self._raise_if_expired(live)
            raise self._host_failure(live, f"{what} failed: {exc}") from None

    def _bounded(self, live: _Live, seconds: float) -> float:
        return max(30.0, min(float(seconds), live.lease.remaining_s))

    def _bootstrap(self, live: _Live) -> None:
        """Design 5.2, before the document leaves DEV: install, versions, GPU,
        RAM scratch (O8), canary, model manifest, range flag."""
        d = live.dir
        rc, _o, err = self._cmd(live, f"mkdir -p -- {sh.q(d)}", timeout_s=SMALL_CMD_TIMEOUT_S, what="mkdir")
        if rc != 0:
            raise self._host_failure(live, f"could not create the lease directory ({rc}): {_tail(err)}")
        uploads = [(name, _tool_bytes(name)) for name in TOOL_FILES]
        uploads.append(("requirements.lock", self.cfg.ocr.requirements_lock.read_bytes()))
        for name, data in uploads:
            self._transfer(
                live,
                lambda n=name, b=data: sh.upload_verified(live.shell, b, f"{d}/{n}", timeout_s=SMALL_CMD_TIMEOUT_S),
                f"uploading {name}",
            )
            live.uploaded_bytes += len(data)
        rc, out, err = self._cmd(
            live,
            f"sh {sh.q(d + '/bootstrap.sh')} install {sh.q(d + '/requirements.lock')} {sh.q(REMOTE_MODEL_CACHE)} "
            f"{sh.q(live.disk_dir + '/venv')}",
            timeout_s=self._bounded(live, max(600.0, float(self.cfg.startup_s))),
            what="the bootstrap",
        )
        report = _prefixed_json(out, BOOTSTRAP_PREFIX) if rc == 0 else None
        if report is None:
            raise self._host_failure(
                live, f"the bootstrap failed on the instance ({rc}): {bootstrap_failure_text(err) or _tail(err)}"
            )
        live.versions = {k: report.get(k) for k in ("marker", "surya", "torch", "torch_cuda", "driver", "python", "gpu",
                                                    "python_exe", "env_kind", "venv_error", "marker_exe")}
        live.python_exe = str(report.get("python_exe") or "").strip()
        if not live.python_exe:
            raise self._host_failure(
                live, "the bootstrap did not report which interpreter it installed into; nothing later could be run "
                      "through it. The document was not uploaded."
            )
        # Live records (canary attempt 3, 2026-09-20): `env_kind` alone is a verdict without a cause, and the free
        # room in /dev/shm is what the next document's sizing needs.
        venv_error = str(report.get("venv_error") or "").strip()
        shm_free = report.get("shm_avail_bytes")
        self._say(f"  . vast.ai lease {live.lease.lease_id}: python {report.get('python')} at {live.python_exe} "
                  f"({report.get('env_kind')}{'; venv: ' + venv_error if venv_error else ''}, "
                  f"pip {report.get('pip_version')}), torch {report.get('torch')} "
                  f"cuda {report.get('torch_cuda')}, /dev/shm "
                  f"{shm_free if isinstance(shm_free, (int, float)) and not isinstance(shm_free, bool) else 'unknown'}"
                  " byte(s) free")
        if report.get("marker") != self.cfg.marker_version:
            raise StackMismatch(
                "stack-mismatch",
                f"the rented host installed marker-pdf {report.get('marker')!r}; [ingest.ocr] marker_version is "
                f"{self.cfg.marker_version!r}. The document was not uploaded.",
                next_actions=["regenerate the lock with `trialerror vastai lock-deps`", "or correct [ingest.ocr] marker_version"],
                details={"remote_versions": live.versions},
            )
        live.marker_exe = str(report.get("marker_exe") or "").strip()
        if not live.marker_exe:
            raise StackMismatch(
                "stack-mismatch",
                f"the rented host installed marker-pdf {report.get('marker')!r} into {live.python_exe} "
                f"({report.get('env_kind')}) but no marker_single script came with it, so there is nothing to run by "
                "path -- and the PATH of a non-interactive ssh command is not the login shell's. The document was not "
                "uploaded.",
                next_actions=["check that [vastai.ocr] requirements_lock installs marker-pdf itself, not only its "
                              "dependencies (`trialerror vastai lock-deps`)"],
                details={"remote_versions": live.versions},
            )
        if not report.get("cuda_available"):
            raise self._host_failure(live, f"torch on the rented host sees no usable GPU ({report.get('torch_error') or 'cuda unavailable'})")
        self._choose_scratch(live, report)
        self._canary(live)
        self._check_models(live)
        if len(self._plan) > 1:
            self._probe_range_flag(live)

    def _choose_scratch(self, live: _Live, report: Mapping[str, Any]) -> None:
        """O8: the document goes to RAM-backed ``/dev/shm`` when it fits.
        Otherwise ``remote_scratch = "shm"`` -- or a tier listed in
        ``shm_required_tiers``, or no tier at all while that list is not
        empty -- refuses the host (F, then R as ``shm-too-small``);
        ``"shm_or_disk"`` uses the container disk."""
        facts = self._job.facts  # type: ignore[union-attr]
        pages = sum(r.count for r in self._plan) or 1
        need = int(facts["bytes"]) + pages * OUTPUT_BYTES_PER_PAGE + SHM_MARGIN_BYTES
        avail = report.get("shm_avail_bytes")
        if isinstance(avail, (int, float)) and not isinstance(avail, bool):
            live.shm_avail_bytes = int(avail)
        egress = self.cfg.egress
        tier = facts.get("license_tier")
        required = (
            egress.remote_scratch == "shm"
            or tier in egress.shm_required_tiers
            or (tier is None and bool(egress.shm_required_tiers))
        )
        if isinstance(avail, (int, float)) and avail >= need:
            live.scratch, live.scratch_kind = live.dir, "shm"
            return
        if required:
            raise self._host_failure(
                live,
                f"/dev/shm on the rented host has {avail} byte(s) free; the document needs {need} in RAM scratch "
                f"([vastai.egress] remote_scratch = {egress.remote_scratch!r}, tier {tier!r}). Not uploaded.",
                _ShmTooSmall,
            )
        disk = live.disk_dir
        rc, _o, err = self._cmd(live, f"mkdir -p -- {sh.q(disk)}", timeout_s=SMALL_CMD_TIMEOUT_S, what="mkdir")
        if rc != 0:
            raise self._host_failure(live, f"could not create the disk scratch ({rc}): {_tail(err)}")
        live.scratch, live.scratch_kind = disk, "disk"
        self._say(f"! vast.ai lease {live.lease.lease_id}: /dev/shm too small, the document goes to the container disk "
                  "([vastai.egress] remote_scratch = \"shm_or_disk\")")

    def _canary(self, live: _Live) -> None:
        d = live.dir
        cdir = f"{d}/canary"
        rc, _o, err = self._cmd(
            live,
            f"mkdir -p -- {sh.q(cdir)} && {sh.q(live.python_exe)} {sh.q(d + '/te_canary.py')} "
            f"{sh.q(d + '/canary_text.txt')} "
            f"{sh.q(cdir + '/input.pdf')}",
            timeout_s=SMALL_CMD_TIMEOUT_S, what="the canary generator",
        )
        if rc != 0:
            raise StackMismatch(
                "stack-mismatch",
                f"the canary page could not be rendered on the rented host ({rc}): {_tail(err)}. Not uploaded.",
                next_actions=["check the lock's Pillow pin (`trialerror vastai lock-deps`)"],
            )
        try:
            status = self._wrapper(live, input_path=f"{cdir}/input.pdf", out=f"{cdir}/out", page_range=None,
                                   timeout_s=self._bounded(live, CANARY_TIMEOUT_S), what="the canary page")
        except sh.RemoteTimeout:
            raise self._host_failure(live, "the canary page did not finish in time on the rented host") from None
        if status.get("timed_out") or status.get("rc") != 0 or not status.get("md_path"):
            # The canary's first run also downloads the models: a failure here
            # is as likely the host's network as the stack. Another host.
            raise self._host_failure(
                live, f"marker failed on the canary page (rc {status.get('rc')}): {_tail(status.get('stderr_tail') or '')}"
            )
        data = self._transfer(
            live,
            lambda: sh.download_verified(live.shell, str(status["md_path"]), expected_sha256=str(status.get("md_sha256")),
                                         timeout_s=DOWNLOAD_TIMEOUT_S),
            "downloading the canary markdown",
        )
        live.downloaded_bytes += len(data)
        similarity = canary_similarity(canary_text(), data.decode("utf-8", "replace"))
        live.canary_similarity = round(similarity, 4)
        if similarity < self.cfg.ocr.canary_min_similarity:
            raise StackMismatch(
                "stack-mismatch",
                f"the canary page came back {similarity:.3f} similar to its known text, below [vastai.ocr] "
                f"canary_min_similarity = {self.cfg.ocr.canary_min_similarity:g}: the rented stack does not OCR "
                "like DEV's. The document was not uploaded.",
                next_actions=["compare the remote versions in the ledger with DEV's marker venv"],
                details={"canary_similarity": live.canary_similarity, "remote_versions": live.versions},
            )

    def _check_models(self, live: _Live) -> None:
        rc, out, err = self._cmd(
            live, f"sh {sh.q(live.dir + '/bootstrap.sh')} models {sh.q(REMOTE_MODEL_CACHE)} {sh.q(live.python_exe)}",
            timeout_s=self._bounded(live, MODELS_HASH_TIMEOUT_S), what="hashing the model cache",
        )
        if rc != 0:
            raise self._host_failure(live, f"hashing the model cache failed ({rc}): {_tail(err)}")
        remote = parse_models_manifest(out.decode("utf-8", "replace"))
        expected = parse_models_manifest(self.cfg.ocr.models_manifest.read_text(encoding="utf-8"))
        missing = sorted(p for p in expected if p not in remote)
        differ = sorted(p for p in expected if p in remote and remote[p] != expected[p])
        live.models_extra_files = len([p for p in remote if p not in expected])
        if missing or differ:
            first = (differ or missing)[0]
            raise StackMismatch(
                "stack-mismatch",
                f"the rented host's marker model cache is not DEV's: {len(differ)} file(s) differ and {len(missing)} "
                f"are missing (first: {first}). The document was not uploaded.",
                next_actions=["regenerate [vastai.ocr] models_manifest from DEV's own model cache",
                              "or pin the model source marker downloads from"],
                details={"differ": differ[:10], "missing": missing[:10]},
            )

    def _probe_range_flag(self, live: _Live) -> None:
        """The inherited ``_require_page_range_flag``, against the REMOTE
        ``--help``: a range flag the instance's marker ignores would turn
        every range into a whole-document run."""
        rc, out, err = self._cmd(live, f"{sh.q(live.marker_exe)} --help", timeout_s=SMALL_CMD_TIMEOUT_S,
                                 what="marker_single --help")
        help_text = f"{out.decode('utf-8', 'replace')}\n{err.decode('utf-8', 'replace')}"
        if not re.search(rf"(?<![\w-]){re.escape(self.page_range_flag)}(?![\w-])", help_text):
            raise PageRangeFlagMissingError(
                f"this document needs page-range chunking, and the rented host's marker_single does not advertise "
                f"{self.page_range_flag!r} in its --help. Set [ingest.ocr] page_range_flag to the spelling your "
                "marker release uses. The document was not uploaded."
            )

    def _upload(self, live: _Live) -> None:
        facts = self._job.facts  # type: ignore[union-attr]
        assert self._input_path is not None
        suffix = self._input_path.suffix if _SUFFIX_RE.match(self._input_path.suffix or "") else ".bin"
        live.remote_input = f"{live.scratch}/input{suffix.lower()}"
        data = self._input_path.read_bytes()
        rate = max(0.01, float(self.cfg.ocr.uplink_mb_s)) * 1_000_000
        started = self.clock()
        self._transfer(
            live,
            lambda: sh.upload_verified(live.shell, data, live.remote_input, expected_sha256=str(facts["sha256"]),
                                       timeout_s=self._bounded(live, 3.0 * len(data) / rate + 300.0)),
            "uploading the document",
        )
        live.upload_s = round(max(0.0, self.clock() - started), 3)
        live.uploaded_bytes += len(data)
        live.shipped = True
        self.ledger.append(
            "shipped",
            lease_id=live.lease.lease_id,
            instance_id=live.lease.instance_id,
            start=utc_iso(self.clock()),
            upload_bytes=len(data),
            upload_s=live.upload_s,
            scratch_kind=live.scratch_kind,
            shm_avail_bytes=live.shm_avail_bytes,
            job_id=facts.get("job_id"),
            sha256=facts["sha256"],
            worker_run_id=self.run_state.worker_run_id,
        )

    def _wrapper(self, live: _Live, *, input_path: str, out: str, page_range: PageRange | None, timeout_s: float,
                 what: str) -> dict[str, Any]:
        """DEV's argv, every element quoted, through the remote wrapper."""
        argv = [live.marker_exe, input_path, "--paginate_output", "--output_dir", out, "--disable_tqdm",
                "--disable_multiprocessing"]
        if page_range is not None:
            argv += [self.page_range_flag, page_range.flag_value]
        argv += list(self.extra_args)
        wrapper = [live.python_exe, f"{live.dir}/te_range.py", "--timeout", f"{max(1.0, timeout_s):.0f}", "--out", out,
                   "--stem", "input", "--model-cache", REMOTE_MODEL_CACHE, "--", *argv]
        # A timeout here reaches the caller as sh.RemoteTimeout: a document
        # range settles it as class E, the canary as a host failure.
        rc, stdout, err = self._cmd(live, " ".join(sh.q(a) for a in wrapper),
                                    timeout_s=timeout_s + WRAPPER_SLACK_S, what=what, raise_timeout=True)
        status = _prefixed_json(stdout, RANGE_PREFIX)
        if status is None:
            raise RuntimeError(f"the remote wrapper exited {rc} without a status line for {what}: {_tail(err)}")
        return status

    def _remote_range(self, live: _Live, page_range: PageRange | None) -> str:
        live.lease.check()
        name = "all" if page_range is None else f"r{page_range.first:06d}-{page_range.last:06d}"
        out = f"{live.scratch}/out/{name}"
        pages = page_range.count if page_range is not None else max(1, sum(r.count for r in self._plan))
        factor = self.cfg.gpu_factor(str(live.lease.offer.get("gpu_name") or ""))
        timeout = derived_range_timeout_s(self.cfg, range_pages=pages, factor=factor,
                                          remaining_s=live.lease.remaining_s)
        where = f" (pages {page_range.flag_value})" if page_range is not None else ""
        try:
            status = self._wrapper(live, input_path=live.remote_input, out=out, page_range=page_range,
                                   timeout_s=timeout, what="a marker range")
        except sh.RemoteTimeout:
            # DEV parity: the per-range timeout is EnvironmentalFailure (class E).
            raise EnvironmentalFailure(
                f"marker_single did not return within {timeout:.0f}s on the rented host{where} "
                "([vastai.ocr] range_timeout_s)"
            ) from None
        peak = status.get("ru_maxrss_bytes")
        if status.get("timed_out"):
            self._raise_if_expired(live)
            raise EnvironmentalFailure(
                f"marker_single timed out after {timeout:.0f}s on the rented host{where} ([vastai.ocr] range_timeout_s)"
            )
        rc = status.get("rc")
        if rc != 0:
            peak_note = f" [remote peak RSS {peak / 1e9:.1f} GB]" if isinstance(peak, int) and peak > 0 else ""
            raise RuntimeError(
                f"marker_single exited {rc} on the rented host{where}{peak_note}: {_tail(status.get('stderr_tail') or '', 2000)}"
            )
        if not status.get("md_path"):
            raise RuntimeError(f"marker_single produced no .md output on the rented host{where}")
        data = self._transfer(
            live,
            lambda: sh.download_verified(live.shell, str(status["md_path"]), expected_sha256=str(status.get("md_sha256")),
                                         timeout_s=DOWNLOAD_TIMEOUT_S),
            f"downloading the markdown{where}",
        )
        live.downloaded_bytes += len(data)
        wall = status.get("wall_s")
        if isinstance(wall, (int, float)):
            live.compute_s += float(wall)
        live.peaks.append(peak if isinstance(peak, int) else None)
        self._last_remote_peak = peak if isinstance(peak, int) else None
        text = data.decode("utf-8", errors="replace")
        # Before the text is returned: the inherited loop caches it at once.
        self._check_range_pages(live, text, page_range, where)
        live.ranges_run.append(page_range.flag_value if page_range is not None else "all")
        live.pages_run += pages
        return text

    def _check_range_pages(self, live: _Live, text: str, page_range: PageRange | None, where: str) -> None:
        """A range must come back with exactly one ``{N}`` marker per page.

        Under either numbering convention that is as many markers as the range
        has pages, consecutive and strictly increasing (``first..last`` or
        ``0..count-1``; WHICH convention, and a range that mixes them, stays
        the inherited numbering check's to judge, exactly as on DEV). The
        inherited parser's ``markers`` holds EVERY marker, including a blank
        page's, so a blank page still counts; this relies on marker writing
        ``{N}`` for a blank page (design section 18, on the round-4 canary
        list). Anything else -- an empty text, no marker, a missing or an
        extra page -- is this host's failure (class F): the lease is failed
        over, and the text is never cached."""
        if page_range is None:
            if text.strip():
                return
            problem = "an empty markdown"
        else:
            markers = self._parse_range(text, page_range).markers
            if len(markers) == page_range.count and all(b == a + 1 for a, b in zip(markers, markers[1:])):
                return
            if not text.strip():
                problem = "an empty markdown"
            elif not markers:
                problem = "markdown without any {N} page marker"
            else:
                shown = ", ".join(str(m) for m in markers[:12]) + (", ..." if len(markers) > 12 else "")
                problem = (
                    f"{len(markers)} page marker(s) [{shown}] for a range of {page_range.count} page(s)"
                )
        raise self._host_failure(
            live, f"marker_single exited 0 on the rented host{where} but returned {problem}; the range is not used"
        )

    def _fail_over(self, failure: HostFailure) -> None:
        machine = failure.machine_id
        if machine is None and self._live is not None:
            machine = self._live.lease.machine_id
        self._say(f"! vast.ai host failure ({failure}); destroying the lease")
        self._end_live("failed", "host-failure", error=str(failure))
        if machine is not None and machine not in self._excluded:
            self._excluded.append(machine)
        limit = int(self.cfg.ocr.max_leases_per_job)
        if self._leases_used >= limit:
            code = "shm-too-small" if isinstance(failure, _ShmTooSmall) else "hosts-exhausted"
            raise VastPlanRefused(
                code,
                f"{self._leases_used} lease(s) used of [vastai.ocr] max_leases_per_job = {limit}; the last host "
                f"failed: {failure}. The claim goes back unrun; DEV's range cache keeps what was produced.",
                next_actions=["retry later (host or market state)", "or raise [vastai.ocr] max_leases_per_job"]
                + (["or allow [vastai.egress] remote_scratch = \"shm_or_disk\" for this tier"] if code == "shm-too-small" else []),
                details={"excluded_machine_ids": list(self._excluded), "last_failure": str(failure)},
            )
        self._say(f"  . vast.ai: failing over to a fresh host (excluding machine(s) {self._excluded}); "
                  "only the missing ranges run again")

    def _end_live(self, result: str, ended_by: str, *, error: str | None = None) -> None:
        """Wipe the scratch best-effort, destroy (retried, confirmed), write the
        ``outcome`` row. Idempotent: nothing live, nothing to do."""
        live = self._live
        if live is None:
            return
        self._live = None
        lease = live.lease
        if live.shell is not None and not lease.expired and not lease.destroyed:
            paths = [live.dir]
            for extra in (live.scratch, live.disk_dir):
                if extra and extra not in paths:
                    paths.append(extra)
            try:
                live.shell.run("rm -rf -- " + " ".join(sh.q(p) for p in paths), timeout_s=60.0)
            except Exception:  # noqa: BLE001 - best effort: the destroy below is what matters
                pass
        try:
            lease.__exit__(None, None, None)
        except BaseException as exc:  # noqa: BLE001 - the outcome row must still be written
            self._say(f"! vast.ai lease {lease.lease_id}: destroy raised {type(exc).__name__}: {exc}")
        if live.shell is not None:
            try:
                live.shell.close()
            except Exception:  # noqa: BLE001
                pass
        if live.known_hosts is not None:
            try:
                live.known_hosts.unlink(missing_ok=True)
            except OSError:
                pass
        bootstrap_bytes = int(float(self.cfg.ocr.bootstrap_download_gb) * 1e9)
        fields = lease.outcome_fields(down_bytes=live.uploaded_bytes + bootstrap_bytes, up_bytes=live.downloaded_bytes)
        facts = self._job.facts if self._job is not None else {}
        offer = lease.estimate.intent_fields() if hasattr(lease.estimate, "intent_fields") else {}
        row = {
            **fields,
            "result": "expired" if lease.expired else result,
            "ended_by": ended_by,
            "error": error,
            "job_id": facts.get("job_id"),
            "sha256": facts.get("sha256"),
            "worker_run_id": self.run_state.worker_run_id,
            "datacenter": offer.get("datacenter"),
            "verified": offer.get("verified"),
            "geolocation": offer.get("geolocation"),
            "shipped": live.shipped,
            "scratch_kind": live.scratch_kind,
            # Beside the kind, the number it was decided on: a refused host
            # (`shm-too-small`) records what it did have (live records, canary attempt 3, 2026-09-20).
            "shm_avail_bytes": live.shm_avail_bytes,
            "ranges_run": list(live.ranges_run),
            "pages_run": live.pages_run,
            "ranges_cached": self._ranges_cached,
            "uploaded_bytes": live.uploaded_bytes,
            "downloaded_bytes": live.downloaded_bytes,
            "bootstrap_download_bytes_estimated": bootstrap_bytes,
            "upload_s": live.upload_s,
            "pages_per_min": round(live.pages_run / (live.compute_s / 60.0), 3) if live.compute_s > 0 else None,
            "peak_rss_bytes": list(live.peaks),
            "remote_versions": dict(live.versions),
            "canary_similarity": live.canary_similarity,
            "models_extra_files": live.models_extra_files,
        }
        if not lease.destroyed:
            row["destroy_error"] = lease.destroy_error
        try:
            self.ledger.append("outcome", **row)
        except Exception as exc:  # noqa: BLE001 - loud, never silent
            self._say(f"!!! vast.ai ledger 'outcome' row for lease {lease.lease_id} could not be written: {exc}")
        self._lease_rows.append(row)

    def _build_provenance(self) -> dict[str, Any]:
        leases = [
            {
                "lease_id": r.get("lease_id"),
                "instance_id": r.get("instance_id"),
                "host_id": r.get("host_id"),
                "machine_id": r.get("machine_id"),
                "result": r.get("result"),
                "ended_by": r.get("ended_by"),
                "ranges_run": r.get("ranges_run"),
                "estimated_cost_usd": r.get("estimated_cost_usd"),
            }
            for r in self._lease_rows
        ]
        last = next((r for r in reversed(self._lease_rows) if r.get("ranges_run")), self._lease_rows[-1] if self._lease_rows else None)
        block: dict[str, Any] = {
            "executor": EXECUTOR_VASTAI,
            "leases": leases,
            "ranges_cached": self._ranges_cached,
            "estimated_cost_usd": round(sum(float(r.get("estimated_cost_usd") or 0.0) for r in self._lease_rows), 6),
        }
        if last is None:
            block["note"] = ("no lease in this run: every range came from DEV's range cache (the ledger's outcome rows "
                             "name the leases that produced them)")
            return block
        block.update(
            {
                "lease_id": last.get("lease_id"),
                "instance_id": last.get("instance_id"),
                "host_id": last.get("host_id"),
                "machine_id": last.get("machine_id"),
                "datacenter": last.get("datacenter"),
                "verified": last.get("verified"),
                "geolocation": last.get("geolocation"),
                "gpu_name": last.get("gpu_name"),
                "remote_versions": last.get("remote_versions"),
                "canary_similarity": last.get("canary_similarity"),
                "scratch_kind": last.get("scratch_kind"),
            }
        )
        return block
