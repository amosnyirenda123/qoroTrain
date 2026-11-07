"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/tuners/tuner.py

Description:
    The Tuner owns the loop; a subclass only decides what to try next.

    .. code-block:: text

        run ends (status event) --> settle --> score --> stop rules --> propose --> review --> start next run

    Threading. Handlers run on the bridge's single dispatch thread, so they only record data
    (statuses and metrics go to the store). Everything slow happens on the tuner's own decision
    thread: waiting for stragglers, scoring, ``propose`` (an LLM call, later), and the review
    (which may wait for a person). Metrics keep flowing while the tuner thinks.

    Call :meth:`Tuner.begin` once the bridge is up (and the runner is subscribed) to launch the first run.

    Design:
        Template Method pattern. :class:`Tuner` implements messaging,
        persistence, scoring, stop rules, proposal completion and review;
        subclasses implement only :meth:`Tuner.propose`. That keeps every
        algorithm on an identical pipeline, so comparisons between algorithms
        isolate the search strategy.

    Session protocol:
        Runs are strictly sequential: the Tuner launches one run, waits for
        its terminal status, then decides the next. Run ids are
        ``<session>-<NNN>``. Status and metric messages for runs this Tuner
        did not launch are ignored, so several sessions can share a broker.

    Failure handling:
        A run that fails or produces no finite objective is recorded as a
        ``failed`` trial; three consecutive failures end the session. If the
        Runner never acknowledges a start command, it is re-sent once after
        ``start_timeout`` seconds, and the session ends after a second
        timeout. Unexpected exceptions on the decision thread end the session
        with ``internal error``.

===============================================================================
"""
import logging
import queue
import random
import threading
import time
import uuid
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from ..config.models import ObjectiveSpec, StopSpec
from ..config.environment import DEFAULT_TOPIC_ROOT
from ..messages.message import Message
from ..config.contracts.schema import (
    SchemaError,
    StartCommand,
    Status,
    StopCommand,
    decode_metrics,
    decode_status,
    encode,
    metrics_pattern,
    start_topic,
    status_pattern,
    stop_topic,
)
from ..routes.route import Route
from ..store.metric_store import MetricStore
from .objective import aggregate, is_better
from .review import AutoApprove, Reviewer
from .space import SearchSpace
from .types import Proposal, Trial

if TYPE_CHECKING:
    from ..bridges.bridge import Bridge

log = logging.getLogger(__name__)

__all__ = ["Tuner", "RandomTuner", "GridTuner", "AgenticTuner", "OptunaTuner", "StopTuning", "Trial", "Proposal"]


class StopTuning(Exception):
    """Raised by ``propose`` when the algorithm has nothing left to try."""


class Tuner(ABC):
    """Abstract tuning session: a Bridge component that runs the propose-run-score loop.

    The Tuner subscribes to the status and metrics topics of every run in its
    project, records what it receives in the
    :class:`~qorotrain.store.metric_store.MetricStore`, and publishes start
    (and, on abort, stop) commands for the Runner.

    Stop rules, checked after each run in this order:

    1. ``max_failures`` consecutive failed runs.
    2. ``stop.target`` reached by the best objective.
    3. ``max_runs`` runs completed (baseline included).
    4. ``stop.patience`` runs since the best one without improvement.

    Attributes:
        finished (threading.Event): Set when the session is over, for any
            reason. :meth:`wait` blocks on it.
        stop_reason (str | None): Human-readable reason the session ended,
            ``None`` while it is running.
    """

    def __init__(
        self,
        *,
        space: SearchSpace,
        objective: ObjectiveSpec,
        project: str,
        store: MetricStore,
        topic_root: str = DEFAULT_TOPIC_ROOT,
        max_runs: int = 8,
        stop: StopSpec | None = None,
        initial: dict[str, Any] | None = None,
        reviewer: Reviewer | None = None,
        max_failures: int = 3,
        settle_seconds: float = 0.5,
        start_timeout: float = 30.0,
        session: str | None = None,
    ) -> None:
        """Configure the session. Nothing happens until the Bridge starts it and :meth:`begin` is called.

        Args:
            space: Search space used to validate every proposal.
            objective: Metric, aggregation and direction used for scoring.
            project: Experiment name (topic segment and store ``project``).
            store: Where runs and metrics are recorded.
            topic_root: Topic root.
            max_runs: Run budget, including the baseline run.
            stop: Optional ``target`` / ``patience`` rules.
            initial: Baseline parameters for run 0. ``None`` makes run 0 the
                algorithm's first proposal.
            reviewer: Reviewer for proposals; defaults to
                :class:`~qorotrain.tuners.review.AutoApprove`.
            max_failures: Consecutive failed runs that end the session.
            settle_seconds: Delay after a terminal status before scoring, so
                that metric batches still in flight are stored first.
            start_timeout: Seconds to wait for the Runner's ``started``
                status before re-sending (once) or giving up.
            session: Run-id prefix; a random 6-hex-digit value by default.
        """
        self._space = space
        self._objective = objective
        self._project = project
        self._store = store
        self._root = topic_root
        self._max_runs = max_runs
        self._stop_rules = stop or StopSpec()
        self._initial = dict(initial) if initial else None
        self._reviewer = reviewer or AutoApprove()
        self._max_failures = max_failures
        self._settle = settle_seconds
        self._start_timeout = start_timeout
        self._session = session or uuid.uuid4().hex[:6]

        self._lock = threading.RLock()
        self._bridge: "Bridge | None" = None
        self._thread: threading.Thread | None = None
        self._halt = threading.Event()  # the component is being stopped
        self._ended: "queue.Queue[tuple[Status, dict]]" = queue.Queue()

        self._history: list[Trial] = []
        self._known: set[str] = set()
        self._counter = 0
        self._current: str | None = None
        self._current_params: dict[str, Any] = {}
        self._launched_at = 0.0
        self._started_seen = False
        self._retries_left = 0

        self.finished = threading.Event()
        self.stop_reason: str | None = None

    # ------------------------------------------------------------- algorithm

    @abstractmethod
    def propose(self, history: list[Trial]) -> Proposal:
        """The next configuration to try. May return only the parameters that change; the rest are
        taken from the previous run. Raise :class:`StopTuning` when there is nothing left to try.

        Called on the decision thread, so it may be slow.

        Args:
            history: Every completed trial, oldest first (empty for the first
                proposal when there is no baseline).

        Returns:
            The proposal. It is validated against the search space after
            completion; an invalid proposal ends the session.

        Raises:
            StopTuning: When the algorithm is exhausted.
        """

    # ------------------------------------------------------------- component

    def routes(self) -> list[Route]:
        """Subscribe to every run's status and metrics topics in the project, at QoS 1.

        Returns:
            The status and metrics routes.
        """
        return [
            Route(status_pattern(self._root, self._project), self.on_status, qos=1),
            Route(metrics_pattern(self._root, self._project), self.on_metrics, qos=1),
        ]

    def start(self, bridge: "Bridge") -> None:
        """Keep the Bridge and start the decision thread (``tuner-decisions``) if it is not running.

        Args:
            bridge: The running Bridge.
        """
        self._bridge = bridge
        if self._thread is None or not self._thread.is_alive():
            self._halt.clear()
            self._thread = threading.Thread(target=self._loop, name="tuner-decisions", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Halt the decision thread (waiting up to 5 s) and forget the Bridge.

        This does not end the session (``finished`` is not set); use
        :meth:`abort` for that.
        """
        self._halt.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        self._thread = None
        self._bridge = None

    # --------------------------------------------------------------- control

    def begin(self) -> None:
        """Launch the first run: the baseline (``initial``) if there is one, otherwise ``propose([])``.

        The baseline is launched without review; a first proposal is reviewed
        like any other.

        Raises:
            RuntimeError: If the tuner is not attached to a running Bridge, or
                has already begun.
        """
        if self._bridge is None:
            raise RuntimeError("The tuner is not attached to a running bridge.")
        if self._current is not None or self._history:
            raise RuntimeError("The tuner has already begun.")
        if self._initial is not None:
            params = dict(self._initial)
        else:
            try:
                proposal = self.propose([])
            except StopTuning as exc:
                self._finish(str(exc) or "the algorithm has nothing to try")
                return
            params = self._review(proposal.params, proposal, [])
            if params is None:
                return
        self._launch(params)

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the session is over. True if it finished within ``timeout``.

        Args:
            timeout: Seconds to wait, or ``None`` to wait indefinitely.

        Returns:
            Whether the session has finished.
        """
        return self.finished.wait(timeout)

    def abort(self, reason: str = "aborted") -> None:
        """End the session now and ask the runner to stop the active run.

        The active run is forgotten first, so its terminal status will not be
        scored. Publishing the stop command is best-effort.

        Args:
            reason: Recorded as ``stop_reason`` (unless the session had
                already finished).
        """
        with self._lock:
            run = self._current
            self._current = None
        self._finish(reason)
        if run is not None and self._bridge is not None:
            try:
                self._bridge.publish(Message(stop_topic(self._root, self._project), encode(StopCommand(run)), qos=1))
            except Exception:
                log.exception("could not publish the stop command for run %s", run)

    @property
    def history(self) -> list[Trial]:
        """A copy of the completed trials, oldest first."""
        with self._lock:
            return list(self._history)

    @property
    def best(self) -> Trial | None:
        """The trial with the best objective (earliest wins ties), or ``None`` if none scored."""
        return self._best(self.history)

    # -------------------------------------------------------------- handlers

    def on_status(self, message: Message) -> None:
        """Route handler for run statuses (dispatch thread).

        ``started`` for the current run marks it started (in memory and in the
        store). A terminal status for the current run is queued for the
        decision thread exactly once: the current run is cleared at the same
        time, so duplicate deliveries are ignored. Statuses of unknown runs
        and malformed payloads are ignored (the latter with an error log).

        Args:
            message: The received status message.
        """
        try:
            status = decode_status(message.payload)
        except SchemaError as exc:
            log.error("ignoring malformed status: %s", exc)
            return

        with self._lock:
            if status.run not in self._known:
                return
            if status.state == "started":
                if status.run == self._current:
                    self._started_seen = True
            elif status.run == self._current:
                params, self._current = self._current_params, None  # consumed: duplicates are ignored
            else:
                return
        if status.state == "started":
            self._store.mark_started(status.run, status.ts)
            return
        self._ended.put((status, params))

    def on_metrics(self, message: Message) -> None:
        """Route handler for metric batches (dispatch thread): store batches of known runs.

        Redelivered batches are harmless because the store ignores duplicate
        ``(run, metric, step)`` rows.

        Args:
            message: The received metrics message.
        """
        try:
            batch = decode_metrics(message.payload)
        except SchemaError as exc:
            log.error("ignoring malformed metrics: %s", exc)
            return
        with self._lock:
            if batch.run not in self._known:
                return
        self._store.add_metrics(batch.run, batch.points)

    # ------------------------------------------------------- decision thread

    def _loop(self) -> None:
        """Decision thread body: handle ended runs until halted or finished.

        Polls the queue every 0.2 s so that the start watchdog runs while
        idle.
        """
        while not self._halt.is_set() and not self.finished.is_set():
            try:
                status, params = self._ended.get(timeout=0.2)
            except queue.Empty:
                self._check_start_watchdog()
                continue
            try:
                self._on_run_ended(status, params)
            except Exception:
                log.exception("the tuner failed while handling the end of run %s", status.run)
                self._finish("internal error (see the log)")

    def _check_start_watchdog(self) -> None:
        """A start command can be lost if the runner had not subscribed yet. Resend once, then give up."""
        with self._lock:
            if self._current is None or self._started_seen or self.finished.is_set():
                return
            if time.monotonic() - self._launched_at < self._start_timeout:
                return
            run, params = self._current, self._current_params
            if self._retries_left > 0:
                self._retries_left -= 1
                self._launched_at = time.monotonic()
                resend = True
            else:
                resend = False
        if resend:
            log.warning("run %s: no answer from the runner, sending the start command again", run)
            self._publish_start(run, params)
        else:
            self._finish(f"the runner did not answer for run {run} (is it running and subscribed?)")

    def _on_run_ended(self, status: Status, params: dict[str, Any]) -> None:
        """Score the ended run, record it, apply the stop rules, and launch the next proposal.

        Args:
            status: The run's terminal status.
            params: The parameters the run was launched with.
        """
        if self._halt.wait(self._settle):  # let late metric batches arrive before scoring
            return

        trial = self._score(status, params)
        self._store.finish_run(trial.run, trial.status, trial.objective, trial.summary.get("error"), status.ts)
        with self._lock:
            self._history.append(trial)
        log.info(
            "run %s %s: objective=%s params=%s",
            trial.run, trial.status, "n/a" if trial.objective is None else f"{trial.objective:.6g}", params,
        )

        history = self.history
        reason = self._stop_reason(history)
        if reason:
            self._finish(reason)
            return

        try:
            proposal = self.propose(history)
        except StopTuning as exc:
            self._finish(str(exc) or "the algorithm has nothing left to try")
            return

        params = self._review(self._complete(proposal.params, history), proposal, history)
        if params is not None:
            self._launch(params)

    def _score(self, status: Status, params: dict[str, Any]) -> Trial:
        """Turn a terminal status and the stored metric series into a :class:`Trial`.

        Only ``finished`` runs are scored. A finished run without a finite
        objective becomes ``failed`` with an explanatory error; a stopped run
        stays ``stopped``.

        Args:
            status: The run's terminal status.
            params: The run's parameters.

        Returns:
            The trial, with ``summary`` holding ``points``, ``last`` and
            ``error`` as applicable.
        """
        run, metric = status.run, self._objective.metric
        series = [value for _, value in self._store.series(run, metric)]
        summary: dict[str, Any] = {"points": len(series)}
        objective, error = None, status.error

        if status.state == "finished":
            objective = aggregate(series, self._objective.aggregate, self._objective.direction)
            if objective is None:
                error = (
                    f"the run logged no values for metric '{metric}'"
                    if not series
                    else f"the objective '{metric}' is not a finite number (the run may have diverged)"
                )
        if series:
            summary["last"] = series[-1]
        if error:
            summary["error"] = error

        state = "finished" if objective is not None else ("stopped" if status.state == "stopped" else "failed")
        return Trial(params=params, objective=objective, summary=summary, status=state, run=run)

    def _stop_reason(self, history: list[Trial]) -> str | None:
        """Apply the stop rules (see the class docstring) to the history.

        Args:
            history: Completed trials, oldest first.

        Returns:
            Why the session should stop, or ``None`` to continue.
        """
        failures = 0
        for trial in reversed(history):
            if trial.status != "failed":
                break
            failures += 1
        if failures >= self._max_failures:
            return f"{failures} consecutive failed runs"

        best = self._best(history)
        target = self._stop_rules.target
        if best is not None and target is not None:
            reached = best.objective >= target if self._objective.direction == "maximize" else best.objective <= target
            if reached:
                return f"target reached ({best.objective:.6g})"

        if len(history) >= self._max_runs:
            return f"reached max_runs ({self._max_runs})"

        patience = self._stop_rules.patience
        if patience is not None and best is not None:
            since_best = len(history) - 1 - history.index(best)
            if since_best >= patience:
                return f"no improvement in {since_best} runs"
        return None

    def _best(self, history: list[Trial]) -> Trial | None:
        """Best-scoring trial of ``history`` (strict improvement, so the earliest wins ties)."""
        best = None
        for trial in history:
            if trial.objective is not None and (
                best is None or is_better(trial.objective, best.objective, self._objective.direction)
            ):
                best = trial
        return best

    def _complete(self, params: dict[str, Any], history: list[Trial]) -> dict[str, Any]:
        """A proposal may name only what changes: the rest comes from the previous run."""
        base = history[-1].params if history else (self._initial or {})
        return {**base, **params}

    def _review(self, params: dict[str, Any], proposal: Proposal, history: list[Trial]) -> dict[str, Any] | None:
        """Validate a complete proposal, ask the reviewer, and validate any edit.

        Any validation failure or rejection ends the session.

        Args:
            params: The complete parameters to run.
            proposal: The original proposal (for its rationale).
            history: Completed trials.

        Returns:
            The parameters to launch, or ``None`` if the session ended.
        """
        problems = self._space.validate(params)
        if problems:
            self._finish("invalid proposal: " + "; ".join(problems))
            return None

        decision = self._reviewer.review(Proposal(params, proposal.rationale), history)
        if decision.action == "reject":
            self._finish("rejected by the reviewer" + (f": {decision.reason}" if decision.reason else ""))
            return None
        if decision.params is not None:
            params = {**params, **decision.params}
            problems = self._space.validate(params)
            if problems:
                self._finish("invalid edit: " + "; ".join(problems))
                return None
        return params

    # ------------------------------------------------------------- launching

    def _launch(self, params: dict[str, Any]) -> None:
        """Allocate a run id, record the run in the store, and publish its start command."""
        with self._lock:
            run = f"{self._session}-{self._counter:03d}"
            self._counter += 1
            self._known.add(run)
            self._current, self._current_params = run, dict(params)
            self._started_seen = False
            self._retries_left = 1
            self._launched_at = time.monotonic()
        self._store.create_run(run, self._project, params)
        log.info("run %s: starting", run)
        self._publish_start(run, params)

    def _publish_start(self, run: str, params: dict[str, Any]) -> None:
        """Publish a start command at QoS 1; end the session if publishing fails."""
        try:
            self._bridge.publish(
                Message(start_topic(self._root, self._project), encode(StartCommand(run, params)), qos=1)
            )
        except Exception as exc:
            log.exception("could not publish the start command for run %s", run)
            self._finish(f"could not publish the start command ({exc})")

    def _finish(self, reason: str) -> None:
        """End the session once: record ``reason`` and set :attr:`finished` (later calls are ignored)."""
        with self._lock:
            if self.finished.is_set():
                return
            self.stop_reason = reason
            self.finished.set()
        log.info("tuning finished: %s", reason)


# ---------------------------------------------------------------- algorithms


class RandomTuner(Tuner):
    """Random search: uniform samples, or log-uniform for ranges marked ``log``.

    Seeded, so a session can be repeated exactly. It avoids re-proposing a configuration that
    was already tried (it gives up on that after a few attempts, which only matters for tiny spaces).
    """

    def __init__(self, *, seed: int = 0, **kwargs: Any) -> None:
        """Create the tuner and its private random generator.

        Args:
            seed: Seed of the generator.
            **kwargs: Passed to :class:`Tuner`.
        """
        super().__init__(**kwargs)
        self._rng = random.Random(seed)

    def propose(self, history: list[Trial]) -> Proposal:
        """Sample a configuration, re-drawing up to 20 times to avoid one already tried.

        Args:
            history: Completed trials.

        Returns:
            A complete random configuration (possibly a repeat in very small
            spaces).
        """
        tried = [trial.params for trial in history]
        params = self._space.sample(self._rng)
        for _ in range(20):
            if params not in tried:
                break
            params = self._space.sample(self._rng)
        return Proposal(params, "random sample")


class GridTuner(Tuner):
    """Grid search that interpolates between each parameter's min and max.

    Every range contributes ``points`` values, evenly spaced between min and max (evenly spaced in
    log space for ``log`` ranges); choices contribute all of their values. The full grid is visited in a
    reproducible shuffled order, so stopping at ``max_runs`` still covers the whole space instead of
    only varying the last parameter. When the grid is used up the session ends.
    """

    def __init__(self, *, points: int = 3, seed: int = 0, **kwargs: Any) -> None:
        """Build and shuffle the grid.

        Args:
            points: Values per numeric range (at least 2).
            seed: Seed of the shuffle.
            **kwargs: Passed to :class:`Tuner`.

        Raises:
            ValueError: If ``points < 2`` or the grid exceeds
                :data:`~qorotrain.tuners.space.MAX_GRID_SIZE` cells.
        """
        super().__init__(**kwargs)
        self._cells = self._space.grid(points)
        random.Random(seed).shuffle(self._cells)
        self._next = 0

    @property
    def grid_size(self) -> int:
        """Total number of grid cells."""
        return len(self._cells)

    def propose(self, history: list[Trial]) -> Proposal:
        """Return the next grid cell not already tried.

        Args:
            history: Completed trials.

        Returns:
            The next cell, with ``grid cell i of n`` as rationale.

        Raises:
            StopTuning: When every cell has been proposed.
        """
        tried = [trial.params for trial in history]
        while self._next < len(self._cells):
            cell = self._cells[self._next]
            self._next += 1
            if cell not in tried:  # skips the baseline if it happens to be a grid point
                return Proposal(cell, f"grid cell {self._next} of {len(self._cells)}")
        raise StopTuning(f"grid exhausted ({len(self._cells)} configurations)")


class AgenticTuner(Tuner):
    """Placeholder for the LLM agent algorithm (not implemented)."""

    def propose(self, history: list[Trial]) -> Proposal:
        """Not implemented yet.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError("the LLM agent is not implemented yet")


class OptunaTuner(Tuner):
    """Placeholder for an Optuna (TPE) baseline (not implemented)."""

    def propose(self, history: list[Trial]) -> Proposal:
        """Not implemented yet.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError("the Optuna tuner is not implemented yet")
