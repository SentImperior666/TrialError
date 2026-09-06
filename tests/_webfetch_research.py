"""Fixtures for the research side of web ingestion.

The sidecar's own suite already drives the real fetcher against a real local
``http.server``. What the handlers need is different: a way to put a *result*
into the queue without caring how it was fetched, so the settlement table of
design §5 can be tested row by row — a 304, a paywall, a private-IP refusal,
a sha that does not match — without standing up a server that behaves that
way.

:class:`FakeSidecar` is that. "Fake" only in the sense that it invents the
bytes: it claims through the real :class:`~trialerror.webfetch.protocol.Queue`,
builds its result through the real ``new_result`` validator, and publishes
through the real atomic-rename publish. Everything the research handler reads
is therefore something the real sidecar could have written — and if the
protocol changes under it, these tests break, which is the point.

``tests/test_webfetch_e2e.py`` runs the actual :class:`~trialerror.webfetch.
sidecar.Sidecar` loop against a real socket for the whole-path case; this
module is for the branches.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from trialerror.stores.store import Store
from trialerror.webfetch.protocol import BODY_FILENAME, REPO_FILENAME, Queue, new_result

__all__ = ["FakeSidecar", "write_program_config", "bootstrap_program"]

DEFAULT_LAUNCH_PURPOSE = "webfetch fixture"


def write_program_config(
    program_root: Path,
    *,
    enabled: bool = True,
    queue_dir: str = "webfetch-queue",
    wait_s: float = 0.05,
    poll_interval_s: float = 0.01,
    extra: str = "",
) -> Path:
    """A ``trialerror.toml`` with web fetching switched on.

    ``wait_s`` is tiny on purpose: the handler's in-process wait is real, and
    a test that parks a job should take milliseconds rather than the 45
    seconds the deployment uses. The ``[license]`` routes are the ones design
    §5 says must be allowed — ``web`` for a fetched page and
    ``user_delivered`` for the ``wanted`` row a refusal leaves behind.
    """
    path = Path(program_root) / "trialerror.toml"
    path.write_text(
        "[program]\n"
        'id = "PROG-webfetch-test"\n'
        "\n"
        "[license]\n"
        'allowed_acquisition_routes = ["web", "user_delivered", "api", "author_posted"]\n'
        "\n"
        "[webfetch]\n"
        f"enabled = {str(enabled).lower()}\n"
        f'queue_dir = "{queue_dir}"\n'
        f"wait_s = {wait_s}\n"
        f"poll_interval_s = {poll_interval_s}\n"
        f"{extra}",
        encoding="utf-8",
    )
    return path


def bootstrap_program(store: Store, program_root: Path, **config_kwargs) -> str:
    """Config + an account/session/launch chain. Returns the ``launch_id``."""
    from tests._ingest_fixtures import bootstrap_launch

    write_program_config(program_root, **config_kwargs)
    return bootstrap_launch(store)


class FakeSidecar:
    """Publish a canned result for whatever is pending, through the real queue."""

    def __init__(self, queue_dir: Path | str, worker_id: str = "fake-sidecar") -> None:
        self.queue = Queue(queue_dir).ensure_layout()
        self.worker_id = worker_id

    # -- inspection ------------------------------------------------------
    def pending(self) -> list[str]:
        return self.queue.pending_job_ids()

    def manifest_for(self, job_id: str) -> dict[str, Any]:
        return self.queue.read_manifest(job_id).to_dict()

    # -- publication -----------------------------------------------------
    def serve(
        self,
        *,
        body: bytes = b"<html><body><main><p>hello</p></main></body></html>",
        content_class: str = "html",
        content_type: str = "text/html; charset=utf-8",
        http_status: int = 200,
        outcome: str = "fetched",
        reason: str | None = None,
        headers: Mapping[str, str] | None = None,
        final_url: str | None = None,
        git: Mapping[str, str | None] | None = None,
        robots: Mapping[str, Any] | None = None,
        policy: Mapping[str, Any] | None = None,
        payload_name: str | None = None,
        corrupt_sha: bool = False,
        corrupt_size: bool = False,
        into_done: bool = False,
    ) -> str | None:
        """Claim one pending job and settle it. Returns the job id, or None.

        ``corrupt_sha``/``corrupt_size`` publish a result whose numbers do not
        describe the bytes beside them — the "a compromised sidecar wrote
        this" case the research handler must refuse to ingest.
        """
        claim = self.queue.claim_next(self.worker_id)
        if claim is None:
            return None
        assert claim.manifest is not None, claim.error
        manifest = claim.manifest

        digest = hashlib.sha256(body).hexdigest()
        result = new_result(
            manifest=manifest,
            outcome=outcome,
            url_norm=manifest.url,
            final_url=final_url or manifest.url,
            http_status=http_status,
            content_type=content_type if outcome != "refused" else None,
            content_class=content_class if outcome != "refused" else None,
            payload_bytes=(len(body) + 1) if corrupt_size else len(body),
            content_sha256=("f" * 64) if corrupt_sha else (digest if outcome == "fetched" else None),
            resolved_ips=["93.184.216.34"],
            headers_subset=dict(headers or {}),
            robots=dict(robots or {"fetched": True, "verdict": "allow", "crawl_delay_s": 0}),
            policy=dict(policy or {"verdict": "allow" if outcome != "refused" else "refused",
                                   "host_rule": "allowed-hosts.conf:1", "query_stripped": False}),
            reason=reason,
            git=dict(git) if git is not None else None,
            elapsed_ms=12,
            bytes_out=200,
        )
        if outcome == "refused" and not into_done:
            self.queue.fail(claim, result)
            return claim.job_id

        payloads: dict[str, bytes] = {}
        if outcome == "fetched":
            name = payload_name or (REPO_FILENAME if content_class == "git" else BODY_FILENAME)
            payloads[name] = body
        self.queue.publish(claim, result, payloads or None)
        return claim.job_id

    def refuse(self, reason: str, **kwargs) -> str | None:
        return self.serve(outcome="refused", reason=reason, http_status=None, body=b"", **kwargs)

    def unchanged(self, **kwargs) -> str | None:
        return self.serve(outcome="unchanged", http_status=304, body=b"", **kwargs)


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
