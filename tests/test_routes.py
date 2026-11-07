"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: tests/test_routes.py

Description:
    Tests of :mod:`qorotrain.routes.route`: MQTT wildcard matching, rejection of
    malformed filters and QoS values, and identity semantics of routes.

===============================================================================
"""
import pytest

from qorotrain.routes.route import Route, topic_matches


@pytest.mark.parametrize(
    "pattern, topic, expected",
    [
        ("a/b", "a/b", True),
        ("a/b", "a/b/c", False),
        ("a/b/c", "a/b", False),
        ("a/+", "a/b", True),
        ("a/+", "a", False),
        ("a/+", "a/b/c", False),
        ("a/#", "a/b/c", True),
        ("a/#", "a", True),
        ("#", "x/y", True),
        ("tuning/+/+/metrics", "tuning/p/r/metrics", True),
        ("tuning/+/+/metrics", "tuning/p/r/status", False),
    ],
)
def test_topic_matching(pattern, topic, expected):
    assert topic_matches(pattern, topic) is expected


@pytest.mark.parametrize("pattern", ["", "a/#/b", "a/b#", "a/+b", "a/b+"])
def test_invalid_patterns_are_rejected(pattern):
    with pytest.raises(ValueError):
        Route(pattern, lambda m: None)


def test_invalid_qos_is_rejected():
    with pytest.raises(ValueError):
        Route("a/b", lambda m: None, qos=3)


def test_routes_compare_by_identity():
    handler = lambda m: None  # noqa: E731
    assert Route("a", handler) != Route("a", handler)