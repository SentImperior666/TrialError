"""The guide's sections on `room close` and `gate fail-reproduction` say what the
CLI does: a missing flag is a usage error (exit 2, no envelope), a blank value is
the verb's refusal; and their command blocks keep one command per line, each
continuation ending in a backslash."""

from __future__ import annotations

import re
from pathlib import Path

GUIDE = Path(__file__).resolve().parents[1] / "docs" / "OPERATOR_GUIDE.md"


def _guide() -> str:
    return GUIDE.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n#", start + len(heading))
    return text[start:] if end < 0 else text[start:end]


GATE = "### A gate the critic passed but whose reproduction failed"
ROOM = "## Closing a frozen room"


def test_the_command_blocks_keep_their_line_continuations():
    text = _guide()
    assert (
        'trialerror gate fail-reproduction --id GATE --reason "why it is a failure, in words" \\\n'
        "    --decided-by DECISION-ID --by-launch L\n"
    ) in text
    assert (
        "trialerror artifact register --id ART --by-launch L --as-failed \\\n"
        '    --failure-ref "a string from the artifact that states what failed" --decided-by DECISION-ID\n'
    ) in text
    assert (
        'trialerror room close --id ROOM --reason "why it is closed, in words" \\\n'
        "    --decided-by DECISION-ID --by-launch L\n"
    ) in text
    for heading in (GATE, ROOM):
        for block in re.findall(r"```\n(.*?)```", _section(text, heading), re.S):
            for line in block.splitlines():
                assert not re.search(r"\S {2,}--", line), line  # no joined continuation


def test_the_refusals_distinguish_a_missing_flag_from_a_blank_value():
    text = _guide()
    for heading, code in ((GATE, "fail_refused"), (ROOM, "close_refused")):
        section = " ".join(_section(text, heading).split())
        assert "A missing flag is a usage error (exit 2, no envelope)" in section, heading
        assert f"a blank value is `{code}`" in section, heading
    assert "without `--decided-by` or `--reason`" not in text
