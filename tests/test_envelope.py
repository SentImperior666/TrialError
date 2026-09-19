import json

import pytest

from trialerror.util.envelope import (
    PROTOCOL_VERSION,
    error_envelope,
    make_envelope,
    next_action,
    ok_envelope,
    render_text,
    to_json_line,
)


def test_ok_envelope_shape():
    env = ok_envelope("widget.list", result={"items": [1, 2, 3]})
    assert env == {
        "ok": True,
        "command": "widget.list",
        "protocolVersion": PROTOCOL_VERSION,
        "result": {"items": [1, 2, 3]},
        "nextActions": [],
        "meta": {},
    }


def test_ok_envelope_defaults_result_to_empty_dict():
    env = ok_envelope("noop")
    assert env["result"] == {}


def test_error_envelope_shape():
    env = error_envelope("widget.get", "not_found", "no such widget", details={"id": "W1"})
    assert env["ok"] is False
    assert "result" not in env
    assert env["error"] == {"code": "not_found", "message": "no such widget", "details": {"id": "W1"}}


def test_make_envelope_rejects_ok_true_with_error():
    with pytest.raises(ValueError):
        make_envelope(ok=True, command="x", error={"code": "e", "message": "m"})


def test_make_envelope_rejects_ok_false_with_result():
    with pytest.raises(ValueError):
        make_envelope(ok=False, command="x", result={"a": 1}, error={"code": "e", "message": "m"})


def test_make_envelope_rejects_ok_false_without_error():
    with pytest.raises(ValueError):
        make_envelope(ok=False, command="x")


def test_next_actions_coerced_from_helper_and_dict():
    env = ok_envelope(
        "x",
        next_actions=[
            next_action(["trialerror", "doctor"], "run doctor"),
            {"kind": "shell", "argv": ["trialerror", "--version"]},
        ],
    )
    assert env["nextActions"] == [
        {"kind": "shell", "argv": ["trialerror", "doctor"], "description": "run doctor"},
        {"kind": "shell", "argv": ["trialerror", "--version"]},
    ]


def test_to_json_line_is_single_line_and_round_trips():
    env = ok_envelope("x", result={"a": 1})
    line = to_json_line(env)
    assert "\n" not in line
    assert json.loads(line) == env


def test_render_text_ok_contains_status_and_command():
    env = ok_envelope("widget.list", result={"n": 3})
    text = render_text(env)
    assert "[OK]" in text
    assert "widget.list" in text


def test_render_text_error_contains_code_and_message():
    env = error_envelope("widget.get", "not_found", "no such widget")
    text = render_text(env)
    assert "[FAIL]" in text
    assert "not_found" in text
    assert "no such widget" in text
