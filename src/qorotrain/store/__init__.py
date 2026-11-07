"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/store/__init__.py

Description:
    Persistence layer of Qorotrain.

    Re-exports :class:`~qorotrain.store.metric_store.MetricStore`, the SQLite
    store that holds every run of a tuning session and the metric points its
    notebook streamed. The Tuner writes to it from its route handlers and reads
    from it when it scores a finished run.

===============================================================================
"""
from .metric_store import MetricStore

__all__ = ["MetricStore"]
