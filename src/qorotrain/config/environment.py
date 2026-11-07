"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/config/environment.py

Description:
    Environment variables the Runner sets for a notebook and the Tracker reads.

    The Runner launches each notebook as a child process and tells it who it
    is and where to send telemetry through these variables, so that a notebook
    can create ``Tracker()`` with no arguments. Keeping the names in one module
    makes the producer (:class:`~qorotrain.runners.runner.Runner`) and the
    consumer (:class:`~qorotrain.trackers.tracker.Tracker`) agree by
    construction.

    When the variables are absent (the notebook is run by hand, outside
    ``qorotrainctl``) the Tracker disables itself.

===============================================================================
"""

ENV_RUN_ID = "QOROTRAIN_RUN_ID"
"""Identifier of the run; becomes the ``<run>`` segment of the metrics topic."""

ENV_PROJECT = "QOROTRAIN_PROJECT"
"""Experiment name (``application.name``); the ``<project>`` topic segment."""

ENV_BROKER = "QOROTRAIN_BROKER"  # "host:port"
"""Broker address as ``host:port`` (the port is optional)."""

ENV_TOPIC_ROOT = "QOROTRAIN_TOPIC_ROOT"
"""First segment of every topic (``bridge.topic_root``)."""

ENV_USERNAME = "QOROTRAIN_USERNAME"
"""Broker username; unset when the broker needs no credentials."""

ENV_PASSWORD = "QOROTRAIN_PASSWORD"
"""Broker password; unset when the broker needs no credentials."""

DEFAULT_TOPIC_ROOT = "tuning"
"""Topic root used when neither configuration nor environment provides one."""

DEFAULT_PORT = 1883
"""Standard unencrypted MQTT port, used when the broker address has no port."""
