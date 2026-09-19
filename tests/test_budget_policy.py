"""Tests for ``trialerror.budget.policy`` — model-class ranking helpers."""

from __future__ import annotations

from trialerror.budget.policy import class_rank, meets_minimum, required_class_for_purpose


def test_class_rank_ordering():
    assert class_rank("small") < class_rank("mid") < class_rank("top")


def test_class_rank_unknown_ranks_below_small():
    assert class_rank("bogus") < class_rank("small")


def test_meets_minimum_none_always_satisfies():
    assert meets_minimum("small", None) is True


def test_meets_minimum_respects_rank():
    assert meets_minimum("top", "mid") is True
    assert meets_minimum("mid", "top") is False
    assert meets_minimum("top", "top") is True


def test_required_class_for_purpose():
    policy = {"ideation": "top", "mechanical": "small"}
    assert required_class_for_purpose(policy, "ideation") == "top"
    assert required_class_for_purpose(policy, "unlisted") is None
    assert required_class_for_purpose(None, "ideation") is None
    assert required_class_for_purpose({}, "ideation") is None
