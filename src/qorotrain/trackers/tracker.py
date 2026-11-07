"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/trackers/tracker.py

Description:
    Telemetry client used inside a training notebook.

    .. code-block:: python

        from qorotrain import Tracker

        tracker = Tracker()
        for step in range(num_steps):
            ...
            tracker.log(step, reward=reward, loss=loss)
        tracker.finish()

    ``Tracker()`` needs no arguments: the Runner passes the run id and broker through
    environment variables. Outside of ``qorotrainctl`` those are missing, and the
    tracker silently does nothing, so the same notebook still runs on its own.

    Telemetry must never break a training run: every failure is reported once as a
    warning and the data is dropped.

    Batching:
        Points are buffered and published as one
        :class:`~qorotrain.config.contracts.schema.MetricBatch` when
        ``flush_every`` points have accumulated or ``flush_seconds`` have
        passed since the last flush (checked on each :meth:`Tracker.log`
        call). This keeps per-step overhead to an append under a lock; the
        actual network I/O is done by Paho's background thread.

    Responsibility split:
        The Tracker only publishes metrics. Run status (started, finished,
        failed) is published by the Runner, which is the notebook's parent
        process and knows its exit code even if the notebook crashes.

===============================================================================
"""
import atexit
import logging
import os
import threading
import time
import warnings

from ..connectors.config import MQTTConfig
from ..connectors.connector import Connector, MQTTConnector
from ..config.environment import (
    DEFAULT_PORT,
    DEFAULT_TOPIC_ROOT,
    ENV_BROKER,
    ENV_PASSWORD,
    ENV_PROJECT,
    ENV_RUN_ID,
    ENV_TOPIC_ROOT,
    ENV_USERNAME,
)
from ..messages.message import Message
from ..config.contracts.schema import MetricBatch, MetricPoint, encode, metrics_topic

log = logging.getLogger(__name__)


def _parse_broker(address: str) -> tuple[str, int]:
    """Split ``host[:port]`` into host and port (default 1883).

    The split is on the *last* colon, so bracket-less IPv6 addresses are not
    supported.

    Raises:
        ValueError: If the port is not an integer.
    """
    host, sep, port = address.rpartition(":")
    if not sep:
        return address, DEFAULT_PORT
    return host, int(port)


class Tracker:
    """Buffers metric points in the notebook and publishes them to the broker.

    Each setting is taken from the keyword argument if given, otherwise from
    the corresponding ``QOROTRAIN_*`` environment variable
    (:mod:`qorotrain.config.environment`). Telemetry is enabled only if a run
    id, a project and a broker (or an injected connector) are all known and
    the connection succeeds.

    Usable as a context manager: ``with Tracker() as tracker:`` guarantees
    :meth:`finish` even when the training code raises. :meth:`finish` is also
    registered with :mod:`atexit`.

    Threading:
        :meth:`log` may be called from several threads; the buffer is guarded
        by a lock and publishing happens outside it.
    """

    def __init__(
        self,
        *,
        run: str | None = None,
        project: str | None = None,
        broker: str | None = None,
        topic_root: str | None = None,
        username: str | None = None,
        password: str | None = None,
        flush_every: int = 50,
        flush_seconds: float = 1.0,
        connect_timeout: float = 5.0,
        connector: Connector | None = None,
    ) -> None:
        """Resolve the settings and connect, or disable telemetry.

        Never raises because of the broker: a failed connection disables
        telemetry with a single ``RuntimeWarning``.

        Args:
            run: Run identifier (``QOROTRAIN_RUN_ID``).
            project: Experiment name (``QOROTRAIN_PROJECT``).
            broker: Broker address ``host[:port]`` (``QOROTRAIN_BROKER``).
            topic_root: Topic root (``QOROTRAIN_TOPIC_ROOT``, default
                ``tuning``).
            username: Broker username (``QOROTRAIN_USERNAME``).
            password: Broker password (``QOROTRAIN_PASSWORD``).
            flush_every: Publish after this many buffered points (minimum 1).
            flush_seconds: Publish when this long has passed since the last
                flush, checked on the next :meth:`log` call.
            connect_timeout: Seconds to wait for the broker's CONNACK.
            connector: Pre-built connector to use instead of creating an
                :class:`~qorotrain.connectors.connector.MQTTConnector`
                (intended for tests). It is connected here.
        """
        env = os.environ
        self._run = run or env.get(ENV_RUN_ID)
        self._project = project or env.get(ENV_PROJECT)
        broker = broker or env.get(ENV_BROKER)
        self._root = topic_root or env.get(ENV_TOPIC_ROOT) or DEFAULT_TOPIC_ROOT
        username = username or env.get(ENV_USERNAME)
        password = password or env.get(ENV_PASSWORD)

        self._flush_every = max(1, flush_every)
        self._flush_seconds = flush_seconds
        self._buffer: list[MetricPoint] = []
        self._last_flush = time.monotonic()
        self._lock = threading.Lock()
        self._warned: set[str] = set()
        self._finished = False
        self._enabled = False
        self._connector: Connector | None = None

        if not (self._run and self._project and (broker or connector)):
            log.info("qorotrain: not running under qorotrainctl, telemetry is disabled")
            return

        try:
            if connector is None:
                host, port = _parse_broker(broker)
                connector = MQTTConnector(
                    MQTTConfig(host=host, port=port, qos=1, username=username, password=password)
                )
                connector.connect(timeout=connect_timeout)
            else:
                connector.connect()  # an injected connector (tests) decides its own timeout
        except Exception as exc:
            self._warn_once("connect", f"qorotrain: cannot connect to the broker, telemetry is disabled ({exc})")
            return

        self._connector = connector
        self._topic = metrics_topic(self._root, self._project, self._run)
        self._enabled = True
        atexit.register(self.finish)

    # ------------------------------------------------------------------ state

    @property
    def enabled(self) -> bool:
        """True while telemetry is being sent."""
        return self._enabled and not self._finished

    # -------------------------------------------------------------------- API

    def log(self, step: int, **values: object) -> None:
        """Record metric values for a step. Never blocks on the network, never raises.

        Values are converted with ``float()``; values that cannot be converted
        are skipped (with a one-time warning per metric name), and a point
        with no remaining values is not recorded. NaN and infinities are kept
        so that divergence can be detected downstream.

        Args:
            step: Step index (episode, epoch...). Converted with ``int()``;
                a non-integer step drops the point with a one-time warning.
            **values: Metric name to value, for example ``reward=12.5``.
        """
        if not self.enabled:
            return
        try:
            step = int(step)
        except (TypeError, ValueError):
            self._warn_once("step", f"qorotrain: step must be an integer, got {step!r}; point dropped")
            return

        clean: dict[str, float] = {}
        for name, value in values.items():
            try:
                clean[name] = float(value)
            except (TypeError, ValueError):
                self._warn_once(
                    f"value:{name}", f"qorotrain: metric '{name}' is not a number ({value!r}); it is ignored"
                )
        if not clean:
            return

        batch = None
        with self._lock:
            self._buffer.append(MetricPoint(step=step, values=clean))
            due = (
                len(self._buffer) >= self._flush_every
                or time.monotonic() - self._last_flush >= self._flush_seconds
            )
            if due:
                batch = self._take()
        if batch:
            self._publish(batch)

    def finish(self, timeout: float = 5.0) -> None:
        """Send what is buffered, wait until the broker has it, and disconnect.

        Safe to call more than once. It also runs automatically at interpreter exit.

        Args:
            timeout: Seconds to wait for the broker to acknowledge outstanding
                messages; a timeout produces a one-time warning, not an error.
        """
        if not self.enabled:
            return
        self._finished = True
        try:
            atexit.unregister(self.finish)
        except Exception:  # pragma: no cover - defensive
            pass

        with self._lock:
            batch = self._take()
        if batch:
            self._publish(batch)

        try:
            if not self._connector.flush(timeout):
                self._warn_once("flush", "qorotrain: the broker did not acknowledge every message in time")
        except Exception as exc:
            self._warn_once("flush", f"qorotrain: could not flush telemetry ({exc})")
        finally:
            try:
                self._connector.disconnect()
            except Exception:
                log.exception("error closing the telemetry connection")

    def __enter__(self) -> "Tracker":
        """Return the tracker itself."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Call :meth:`finish`; exceptions from the ``with`` body propagate."""
        self.finish()

    # --------------------------------------------------------------- internals

    def _take(self) -> list[MetricPoint]:
        """Swap the buffer out. Caller holds the lock."""
        batch, self._buffer = self._buffer, []
        self._last_flush = time.monotonic()
        return batch

    def _publish(self, points: list[MetricPoint]) -> None:
        """Encode and publish a batch at QoS 1; on failure warn once and drop the batch."""
        try:
            payload = encode(MetricBatch(run=self._run, points=points))
            self._connector.publish(Message(self._topic, payload, qos=1))
        except Exception as exc:
            self._warn_once("publish", f"qorotrain: could not publish telemetry ({exc}); data is dropped")

    def _warn_once(self, key: str, message: str) -> None:
        """Emit ``message`` as a ``RuntimeWarning`` the first time ``key`` is seen.

        ``stacklevel=3`` attributes the warning to the notebook line that
        called the public method rather than to this module.
        """
        if key in self._warned:
            return
        self._warned.add(key)
        warnings.warn(message, RuntimeWarning, stacklevel=3)
