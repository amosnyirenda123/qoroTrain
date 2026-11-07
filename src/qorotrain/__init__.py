"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/__init__.py

Description:
    Top-level package of Qorotrain, an event-driven framework that tunes the
    hyperparameters of a Jupyter training notebook from the telemetry the
    notebook streams over MQTT.

    The package root deliberately exposes only what a training notebook needs:
    :class:`~qorotrain.trackers.tracker.Tracker`, so that notebooks can write
    ``from qorotrain import Tracker``. Everything else (configuration, bridge,
    runner, tuners, store, CLI) lives in subpackages and is imported from
    there explicitly.

    Subpackages:
        - ``config``: ``experiment.yaml`` schema, loading, validation,
          notebook inspection and the wire contract (``config.contracts``).
        - ``connectors`` / ``messages`` / ``routes`` / ``bridges``: the
          broker-neutral messaging layer.
        - ``runners``: executes the notebook with Papermill and reports status.
        - ``trackers``: the in-notebook telemetry client.
        - ``tuners``: the tuning loop and search algorithms.
        - ``store``: SQLite persistence of runs and metrics.
        - ``cli``: the ``qorotrainctl`` command line.

    Dependencies:
        Importing this package imports the tracker and therefore ``paho-mqtt``.

===============================================================================
"""
from .trackers.tracker import Tracker

__all__ = ["Tracker", "main"]


def main() -> None:
    """Entry point of the ``qorotrain`` console script.

    Placeholder generated with the project skeleton: it only prints a greeting.
    The functional command line is ``qorotrainctl``
    (:func:`qorotrain.cli.main.main`).
    """
    print("Hello from qorotrain!")
