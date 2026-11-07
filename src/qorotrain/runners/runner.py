"""Runner: executes the notebook, one run at a time, and owns run lifecycle status.

    Tuner --command/start--> Runner --subprocess (papermill)--> notebook (Tracker)
                               |
                               +--status: started, then finished | failed | stopped--> broker

The Runner is the parent of the notebook process, so it knows how the run ended
even if the notebook crashes hard or never calls ``tracker.finish()``. The
Tracker owns telemetry; the Runner owns status. Each fact has one publisher.

Handlers must return quickly because the bridge dispatches on a single thread, so
launching only spawns the process and a watcher thread waits for it.

POSIX only: runs get their own process group so that stopping a run also stops
anything the notebook spawned.
"""
import logging
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ..config.environment import (
    DEFAULT_TOPIC_ROOT,
    ENV_BROKER,
    ENV_PASSWORD,
    ENV_PROJECT,
    ENV_RUN_ID,
    ENV_TOPIC_ROOT,
    ENV_USERNAME,
)
from ..messages.message import Message
from ..config.contracts.schema import (
    SchemaError,
    StartCommand,
    Status,
    decode_start,
    decode_stop,
    encode,
    start_topic,
    status_topic,
    stop_topic,
)
from ..routes.route import Route

if TYPE_CHECKING:
    from ..bridges.bridge import Bridge
    from ..config.models import ExperimentSpec

log = logging.getLogger(__name__)

_ERROR_TAIL_LINES = 12
_ERROR_TAIL_CHARS = 1000


@dataclass
class _Run:
    id: str
    proc: subprocess.Popen
    log_path: Path
    thread: threading.Thread | None = None
    stopping: bool = False


class Runner:
    def __init__(
        self,
        source: str | Path,
        project: str,
        output_dir: str | Path,
        *,
        topic_root: str = DEFAULT_TOPIC_ROOT,
        broker: str = "localhost:1883",
        username: str | None = None,
        password: str | None = None,
        fixed: dict[str, Any] | None = None,
        kernel: str = "python3",
        workdir: str | Path | None = None,
        timeout: int = 3600,
        python: str | None = None,
        grace_seconds: float = 5.0,
    ) -> None:
        self._source = Path(source)
        self._project = project
        self._output_dir = Path(output_dir)
        self._root = topic_root
        self._broker = broker
        self._username = username
        self._password = password
        self._fixed = dict(fixed or {})
        self._kernel = kernel
        self._workdir = Path(workdir) if workdir else None
        self._timeout = timeout
        self._python = python or sys.executable
        self._grace = grace_seconds

        self._bridge: "Bridge | None" = None
        self._active: _Run | None = None
        self._seen: set[str] = set()  # run ids already handled: MQTT may deliver a command twice
        self._lock = threading.Lock()

    @classmethod
    def from_spec(cls, spec: "ExperimentSpec", base_dir: str | Path = ".") -> "Runner":
        """Build a runner from a loaded experiment.yaml; relative paths are relative to ``base_dir``."""
        base = Path(base_dir)

        def resolve(path: str) -> Path:
            p = Path(path)
            return p if p.is_absolute() else base / p

        connector = spec.bridge.connectors[0]
        return cls(
            source=resolve(spec.job.source),
            project=spec.application.name,
            output_dir=resolve(spec.runner.output_dir),
            topic_root=spec.bridge.topic_root,
            broker=f"{connector.host}:{connector.port}",
            username=connector.username,
            password=connector.password,
            fixed=spec.job.fixed,
            kernel=spec.runner.kernel,
            workdir=resolve(spec.runner.workdir) if spec.runner.workdir else None,
            timeout=spec.runner.timeout,
        )

    # --------------------------------------------------------------- component

    def routes(self) -> list[Route]:
        return [
            Route(start_topic(self._root, self._project), self._on_start, qos=1),
            Route(stop_topic(self._root, self._project), self._on_stop, qos=1),
        ]

    def start(self, bridge: "Bridge") -> None:
        self._bridge = bridge

    def stop(self) -> None:
        """Stop the active run, if any, and wait for its watcher."""
        with self._lock:
            run = self._active
            if run is not None:
                run.stopping = True
        if run is not None:
            self._terminate(run)
            if run.thread is not None and run.thread.is_alive():
                run.thread.join(timeout=self._grace + 2)
        self._bridge = None

    @property
    def active_run(self) -> str | None:
        with self._lock:
            return self._active.id if self._active else None

    # ---------------------------------------------------------------- handlers

    def _on_start(self, message: Message) -> None:
        try:
            command = decode_start(message.payload)
        except SchemaError as exc:
            log.error("ignoring malformed start command: %s", exc)
            return

        run, error = None, None
        with self._lock:
            if command.run in self._seen:
                log.info("duplicate start command for run %s ignored", command.run)
                return
            self._seen.add(command.run)
            if self._active is not None:
                error = f"runner is busy with run {self._active.id}"
            else:
                try:
                    run = self._spawn(command)
                    self._active = run
                except Exception as exc:
                    error = f"could not launch the notebook: {exc}"

        if run is None:
            log.error("run %s not started: %s", command.run, error)
            self._publish(Status(command.run, "failed", error=error))
            return

        # `started` goes out before the watcher can possibly publish a terminal status.
        self._publish(Status(command.run, "started"))
        run.thread.start()

    def _on_stop(self, message: Message) -> None:
        try:
            command = decode_stop(message.payload)
        except SchemaError as exc:
            log.error("ignoring malformed stop command: %s", exc)
            return
        with self._lock:
            run = self._active
            if run is None or run.id != command.run:
                log.info("stop for %s ignored: not the active run", command.run)
                return
            run.stopping = True
        self._terminate(run)

    # --------------------------------------------------------------- launching

    def _command(self, command: StartCommand, params_file: Path, output_notebook: Path) -> list[str]:
        """The process to run. A method so tests can substitute a stand-in for papermill."""
        argv = [
            self._python, "-m", "papermill",
            str(self._source.resolve()), str(output_notebook.resolve()),
            "-f", str(params_file.resolve()),
            "-k", self._kernel,
            "--no-progress-bar",
            "--execution-timeout", str(self._timeout),
        ]
        if self._workdir is not None:
            argv += ["--cwd", str(self._workdir.resolve())]
        return argv

    def _environment(self, run: str) -> dict[str, str]:
        env = os.environ.copy()
        env[ENV_RUN_ID] = run
        env[ENV_PROJECT] = self._project
        env[ENV_BROKER] = self._broker
        env[ENV_TOPIC_ROOT] = self._root
        for key, value in ((ENV_USERNAME, self._username), (ENV_PASSWORD, self._password)):
            if value is None:
                env.pop(key, None)  # never inherit stale credentials
            else:
                env[key] = value
        return env

    def _spawn(self, command: StartCommand) -> _Run:
        run_dir = self._output_dir / command.run
        run_dir.mkdir(parents=True, exist_ok=False)  # run ids must be unique

        params_file = run_dir / "params.yaml"
        params_file.write_text(yaml.safe_dump({**self._fixed, **command.params}), encoding="utf-8")

        log_path = run_dir / "papermill.log"
        argv = self._command(command, params_file, run_dir / "run.ipynb")
        with open(log_path, "wb") as log_file:
            proc = subprocess.Popen(
                argv,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=self._environment(command.run),
                start_new_session=True,  # own process group, so stop can kill the whole tree
            )
        run = _Run(id=command.run, proc=proc, log_path=log_path)
        run.thread = threading.Thread(target=self._watch, args=(run,), name=f"runner-{command.run}", daemon=True)
        return run

    # ---------------------------------------------------------------- watching

    def _watch(self, run: _Run) -> None:
        returncode = run.proc.wait()
        with self._lock:
            stopping = run.stopping

        if stopping:
            status = Status(run.id, "stopped", exit_code=returncode)
        elif returncode == 0:
            status = Status(run.id, "finished", exit_code=0)
        else:
            status = Status(run.id, "failed", exit_code=returncode, error=self._error_tail(run, returncode))

        # Free the slot BEFORE announcing the end: the Tuner may answer with the next
        # start command immediately, and it must not find the runner "busy".
        with self._lock:
            if self._active is run:
                self._active = None
        self._publish(status)

    @staticmethod
    def _error_tail(run: _Run, returncode: int) -> str:
        prefix = f"terminated by signal {-returncode}" if returncode < 0 else f"exit code {returncode}"
        try:
            with open(run.log_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 4000))
                text = f.read().decode("utf-8", errors="replace")
        except OSError:
            return prefix
        lines = [line.rstrip() for line in text.splitlines() if line.strip()]
        tail = "\n".join(lines[-_ERROR_TAIL_LINES:])[-_ERROR_TAIL_CHARS:]
        return f"{prefix}\n{tail}" if tail else prefix

    # ---------------------------------------------------------------- stopping

    def _terminate(self, run: _Run) -> None:
        """SIGTERM the run's process group, then SIGKILL it if it is still alive after the grace period."""
        try:
            os.killpg(os.getpgid(run.proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            return
        timer = threading.Timer(self._grace, self._kill, args=(run,))
        timer.daemon = True
        timer.start()

    @staticmethod
    def _kill(run: _Run) -> None:
        if run.proc.poll() is None:
            try:
                os.killpg(os.getpgid(run.proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass

    # -------------------------------------------------------------- publishing

    def _publish(self, status: Status) -> None:
        bridge = self._bridge
        if bridge is None:
            return
        try:
            topic = status_topic(self._root, self._project, status.run)
            bridge.publish(Message(topic, encode(status), qos=1))
        except Exception:
            log.exception("could not publish status %s for run %s", status.state, status.run)