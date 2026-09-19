"""D-FB-6: the backend root is named for what it is, and one check reads it.

Three things are pinned here, and each of them is a way the rename could
have gone wrong:

* ``--backend-config-root`` and the suppressed ``--program-root`` alias
  resolve to the SAME value, and a run that used the old spelling says so in
  the envelope's ``meta`` (every envelope, refusals included) while a run
  that used the new one carries nothing extra.
* ``offload doctor`` is a fifth-and-then-some verb over checks that already
  existed plus the new one -- it registers nothing, so every name it runs is
  also reachable as ``trialerror doctor --only <name>``.
* ``offload_backend_root_resolved`` and ``trialerror offload worker`` read
  one root through ONE construction path
  (``ConfigDevBackends.describe`` / ``.validate``), so the check cannot
  report a root the worker then refuses, nor the reverse. The refusal text
  is compared string-for-string, not by shape.

Every path in this module is a ``tmp_path`` the test made: no host path, no
program name and no person's handle is written down anywhere (C-0078), and
the resolved values only ever appear inside a runtime result.
"""

from __future__ import annotations

import pytest

from trialerror.cli import build_parser
from trialerror.offload import protocol
from trialerror.offload.checks import check_offload_backend_root_resolved
from trialerror.offload.worker import ConfigDevBackends, WorkerConfigError
from trialerror.util.doctor import DoctorContext

from tests._offload_fixtures import write_offload_toml


def _run(argv: list[str]) -> dict:
    args = build_parser().parse_args(argv)
    return args.handler(args)


@pytest.fixture()
def offload_program(store, program_root):
    """The SANDBOX half of the split: both GPU stages routed to the queue."""
    write_offload_toml(program_root)
    protocol.ensure_layout(protocol.offload_root(program_root))
    return program_root


def _real_toml(program_root, *, ocr_exe, python_exe, module_dir) -> None:
    """A DEV-shaped config: both stages naming local installs. The values are
    whatever the caller made under ``tmp_path`` this run."""
    (program_root / "trialerror.toml").write_text(
        "\n".join(
            [
                "[program]",
                'id = "backend-root-test"',
                "",
                "[ingest.ocr]",
                'backend = "marker"',
                f'marker_single_exe = "{ocr_exe.as_posix()}"',
                "",
                "[ingest.embed]",
                'backend = "stub-real-embed"',
                f'python_exe = "{python_exe.as_posix()}"',
                f'module_dir = "{module_dir.as_posix()}"',
                "model_key = \"stub-real-embed\"",
                "dims = 8",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _ctx(program_root, platform_root) -> DoctorContext:
    return DoctorContext(program_root=program_root, platform_root=platform_root)


# ---------------------------------------------------------------------------
# the flag and its alias
# ---------------------------------------------------------------------------
def test_the_alias_and_the_new_name_resolve_to_the_same_value():
    parser = build_parser()
    new = parser.parse_args(["offload", "worker", "--queue-root", "q", "--backend-config-root", "r"])
    old = parser.parse_args(["offload", "worker", "--queue-root", "q", "--program-root", "r"])
    assert new.program_root == old.program_root == "r"


def test_the_old_spelling_is_hidden_from_the_help_and_the_new_one_is_not():
    """A suppressed alias is what keeps the DEV launcher working without
    teaching the next operator the retired name."""
    import argparse

    groups = next(
        a
        for a in build_parser()._actions  # noqa: SLF001 - parser introspection is the point
        if isinstance(a, argparse._SubParsersAction)  # noqa: SLF001
    )
    verbs = next(
        a
        for a in groups.choices["offload"]._actions  # noqa: SLF001
        if isinstance(a, argparse._SubParsersAction)  # noqa: SLF001
    )
    worker_help = verbs.choices["worker"].format_help()
    assert "--backend-config-root" in worker_help
    assert "--program-root" not in worker_help


def test_a_run_under_the_old_spelling_carries_the_deprecation_note(offload_program, platform_root):
    """And it is on the REFUSAL, which is the envelope this fixture produces
    (a sandbox root names ``offload``, which a worker declines) -- a note only
    successful runs carried would be invisible exactly when an operator is
    reading the output."""
    env = _run(
        [
            "offload", "worker", "--queue-root", str(protocol.offload_root(offload_program)),
            "--program-root", str(offload_program), "--platform-root", str(platform_root),
        ]
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "fake_backend_refused"
    note = env["meta"]["deprecated_flags"]["--program-root"]
    assert "--backend-config-root" in note


def test_a_run_under_the_new_spelling_carries_no_note_and_the_same_error(
    offload_program, platform_root
):
    argv = [
        "offload", "worker", "--queue-root", str(protocol.offload_root(offload_program)),
        "--platform-root", str(platform_root),
    ]
    new = _run([*argv, "--backend-config-root", str(offload_program)])
    old = _run([*argv, "--program-root", str(offload_program)])
    assert new["meta"] == {}
    assert "warnings" not in new
    assert new["error"] == old["error"]


def test_the_deprecation_also_rides_the_channel_format_text_PRINTS(offload_program, platform_root):
    """V-2. ``render_text`` renders status, result/error, warnings and
    nextActions -- never ``meta`` -- and the one caller in this repo that
    still spells the alias runs ``--format text``. A note only a JSON reader
    sees reaches exactly the operators who have already moved."""
    from trialerror.util.envelope import render_text

    env = _run(
        [
            "offload", "worker", "--queue-root", str(protocol.offload_root(offload_program)),
            "--program-root", str(offload_program), "--platform-root", str(platform_root),
        ]
    )
    assert env["warnings"][0]["code"] == "deprecated_flag"
    assert env["warnings"][0]["message"] == env["meta"]["deprecated_flags"]["--program-root"]
    assert "--backend-config-root" in render_text(env)


def test_the_renamed_flag_does_not_print_the_old_name_as_its_metavar():
    """V-4: the ``dest`` stays ``program_root`` (one attribute for every code
    path below), so without an explicit metavar the renamed flag's own usage
    line reads ``--backend-config-root PROGRAM_ROOT``."""
    import argparse

    groups = next(
        a
        for a in build_parser()._actions  # noqa: SLF001 - parser introspection is the point
        if isinstance(a, argparse._SubParsersAction)  # noqa: SLF001
    )
    verbs = next(
        a
        for a in groups.choices["offload"]._actions  # noqa: SLF001
        if isinstance(a, argparse._SubParsersAction)  # noqa: SLF001
    )
    worker_help = verbs.choices["worker"].format_help()
    assert "--backend-config-root ROOT" in worker_help
    assert "PROGRAM_ROOT" not in worker_help


# ---------------------------------------------------------------------------
# offload doctor
# ---------------------------------------------------------------------------
def test_offload_doctor_runs_the_subsystems_checks_and_nothing_else(offload_program, platform_root):
    from trialerror.cli.offload import _OFFLOAD_CHECK_NAMES

    env = _run(
        ["offload", "doctor", "--program-root", str(offload_program), "--platform-root", str(platform_root)]
    )
    assert env["ok"] is True, env
    result = env["result"]
    assert [c["name"] for c in result["checks"]] == list(_OFFLOAD_CHECK_NAMES)
    assert result["summary"]["total"] == len(_OFFLOAD_CHECK_NAMES)
    assert result["summary"]["failed"] == 0
    assert result["backend_config_root"] == str(offload_program)
    # every name it runs is a registered check, i.e. also reachable as
    # `trialerror doctor --only <name>`
    assert not [c for c in result["checks"] if c["category"] == "unknown"]


def test_offload_doctor_names_the_offload_stub_on_the_queue_host(offload_program, platform_root):
    """The acceptance sentence for this item: on the live (sandbox) program the
    verb reads the root, names the stub, and does not traceback."""
    env = _run(
        ["offload", "doctor", "--program-root", str(offload_program), "--platform-root", str(platform_root)]
    )
    row = next(c for c in env["result"]["checks"] if c["name"] == "offload_backend_root_resolved")
    assert row["status"] == "pass"
    assert row["details"]["stages"]["ocr"]["backend"] == "offload"
    assert row["details"]["stages"]["embed"]["backend"] == "offload"
    assert row["details"]["stages"]["ocr"]["runs_here"] is False
    assert "'offload'" in row["message"]


def test_offload_doctor_returns_a_non_ok_envelope_when_a_check_fails(program_root, platform_root):
    """A stage that names a real backend without the keys it needs: a worker
    started against this root refuses in its first second, so the verb a
    script reads has to be able to say so through its exit code."""
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "x"\n\n[ingest.ocr]\nbackend = "marker"\n', encoding="utf-8"
    )
    env = _run(
        ["offload", "doctor", "--program-root", str(program_root), "--platform-root", str(platform_root)]
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "offload_doctor_checks_failed"
    row = next(
        c for c in env["error"]["details"]["checks"] if c["name"] == "offload_backend_root_resolved"
    )
    assert row["status"] == "fail"
    assert "marker_single_exe" in row["message"]


# ---------------------------------------------------------------------------
# the check itself, against the worker's own reading
# ---------------------------------------------------------------------------
def test_the_check_skips_a_root_with_no_config(program_root, platform_root):
    result = check_offload_backend_root_resolved(_ctx(program_root, platform_root))
    assert result.status == "skip"
    assert result.details["backend_config_root"] == str(program_root)


def test_the_check_skips_when_there_is_no_root_at_all(platform_root):
    result = check_offload_backend_root_resolved(DoctorContext(program_root=None))
    assert result.status == "skip"


def test_a_resolvable_dev_root_passes_and_the_worker_agrees(program_root, platform_root, tmp_path):
    exe = tmp_path / "marker_single"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    py = tmp_path / "python"
    py.write_text("#!/bin/sh\n", encoding="utf-8")
    module_dir = tmp_path / "embeddings"
    module_dir.mkdir()
    _real_toml(program_root, ocr_exe=exe, python_exe=py, module_dir=module_dir)

    result = check_offload_backend_root_resolved(_ctx(program_root, platform_root))
    assert result.status == "pass", result.message
    stages = result.details["stages"]
    assert stages["ocr"]["constructed"] is True
    assert stages["ocr"]["paths"]["marker_single_exe"]["exists"] is True
    assert stages["embed"]["paths"]["module_dir"]["exists"] is True

    # the other half of "cannot disagree": the worker's own pre-flight over
    # the same config raises nothing.
    from trialerror.util.config import CONFIG_FILENAME, load_config

    config = load_config(program_root / CONFIG_FILENAME).raw
    ConfigDevBackends(config).validate()


def test_a_path_that_does_not_exist_here_warns_rather_than_fails(program_root, platform_root, tmp_path):
    """Constructed is what a worker acts on; a path absent on THIS machine is
    a real finding and not a refusal, because the config of the machine that
    runs the model is legitimately readable from one that does not."""
    py = tmp_path / "python"
    py.write_text("#!/bin/sh\n", encoding="utf-8")
    module_dir = tmp_path / "embeddings"
    module_dir.mkdir()
    _real_toml(program_root, ocr_exe=tmp_path / "gone", python_exe=py, module_dir=module_dir)

    result = check_offload_backend_root_resolved(_ctx(program_root, platform_root))
    assert result.status == "warn"
    assert "marker_single_exe" in result.message
    assert result.details["stages"]["ocr"]["paths"]["marker_single_exe"]["exists"] is False


def test_an_unresolvable_stage_fails_with_the_constructors_own_reason(program_root, platform_root):
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "x"\n\n[ingest.embed]\nbackend = "some-real-embedder"\n', encoding="utf-8"
    )
    result = check_offload_backend_root_resolved(_ctx(program_root, platform_root))
    assert result.status == "fail"
    assert "python_exe" in result.message
    assert result.details["stages"]["embed"]["constructed"] is False


def test_fake_fails_only_where_the_program_requires_that_stage_real(program_root, platform_root):
    toml = program_root / "trialerror.toml"
    toml.write_text(
        '[program]\nid = "x"\n\n[ingest]\nrequire_real_backends = true\n', encoding="utf-8"
    )
    result = check_offload_backend_root_resolved(_ctx(program_root, platform_root))
    assert result.status == "fail"
    assert "'fake'" in result.message

    toml.write_text('[program]\nid = "x"\n', encoding="utf-8")
    relaxed = check_offload_backend_root_resolved(_ctx(program_root, platform_root))
    assert relaxed.status == "pass"
    assert relaxed.details["stages"]["ocr"]["backend"] == "fake"


@pytest.mark.parametrize(
    ("toml", "why"),
    [
        ("this = = [[[\n", "unparseable"),
        ('[ingest.ocr]\nbackend = "marker"\nmarker_single_exe = "/nowhere"\n', "no [program] id"),
    ],
)
def test_a_config_the_worker_cannot_LOAD_fails_here_too(program_root, platform_root, toml, why):
    """V-1. ``load_config`` raising is the worker's ``bad_config`` refusal;
    reading it as "nothing declared" would default both stages to ``fake``,
    miss every severity branch, and print an affirmatively false sentence
    about a file that says ``marker`` -- the one direction this check is not
    allowed to be wrong in."""
    (program_root / "trialerror.toml").write_text(toml, encoding="utf-8")

    result = check_offload_backend_root_resolved(_ctx(program_root, platform_root))
    assert result.status == "fail", (why, result.message)
    assert "cannot be loaded" in result.message
    assert "fake" not in result.message
    assert result.details["config_error"]

    # and the worker on the same root, which is the comparison that matters
    env = _run(
        [
            "offload", "worker", "--queue-root", str(program_root / "no-such-queue"),
            "--backend-config-root", str(program_root), "--platform-root", str(platform_root),
        ]
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "bad_config"


def test_offload_doctor_is_non_ok_on_a_root_whose_config_will_not_load(program_root, platform_root):
    """The verb's exit code, not just the row: a broken root was nobody's
    finding inside ``offload doctor`` before V-1."""
    (program_root / "trialerror.toml").write_text("this = = [[[\n", encoding="utf-8")
    env = _run(
        ["offload", "doctor", "--program-root", str(program_root), "--platform-root", str(platform_root)]
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "offload_doctor_checks_failed"
    row = next(
        c for c in env["error"]["details"]["checks"] if c["name"] == "offload_backend_root_resolved"
    )
    assert row["status"] == "fail"


def test_the_check_and_the_worker_share_one_refusal_TEXT(offload_program, platform_root):
    """Not "both refuse" -- the SAME sentence. Two copies of this reasoning
    is how a doctor that says fine and a worker that will not start come to
    stand beside each other."""
    from trialerror.util.config import CONFIG_FILENAME, load_config

    config = load_config(offload_program / CONFIG_FILENAME).raw
    with pytest.raises(WorkerConfigError) as exc:
        ConfigDevBackends(config).validate()

    result = check_offload_backend_root_resolved(_ctx(offload_program, platform_root))
    assert result.details["stages"]["ocr"]["refusal"] == str(exc.value)


def test_describe_never_raises_over_a_broken_table():
    """A doctor check that tracebacked over the thing it is checking would
    leave the operator with less than the check they ran."""
    described = ConfigDevBackends({"ingest": {"ocr": {"backend": "no-such-backend"}}}).describe()
    assert described["stages"]["ocr"]["constructed"] is False
    assert "no-such-backend" in described["stages"]["ocr"]["error"]
