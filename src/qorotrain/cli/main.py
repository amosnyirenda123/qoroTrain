"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/cli/main.py

Description:
    qorotrainctl: command line entry point.

    Exit codes: 0 success, 1 validation errors or no usable result, 2 usage or I/O errors,
    130 interrupted.

    Subcommands:
        - ``init NOTEBOOK [-o FILE] [--force]``: write a starting
          ``experiment.yaml`` (:func:`cmd_init`).
        - ``validate -f FILE [--skip-broker]``: check the YAML, the notebook
          and the broker (:func:`cmd_validate`).
        - ``run -f FILE [--dry-run]``: execute a tuning session
          (:func:`cmd_run`).

    Composition root:
        :func:`cmd_run` is where the components are wired together for a
        session: it loads and validates the spec, reads the baseline from the
        notebook, opens the :class:`~qorotrain.store.metric_store.MetricStore`,
        builds the Tuner and the Runner, attaches both to a
        :class:`~qorotrain.bridges.bridge.Bridge`, and waits for the Tuner to
        finish. Today Tuner and Runner share one process and one Bridge.

    Output streams:
        Results and the plan go to stdout; errors, warnings and the library's
        progress log go to stderr.

===============================================================================
"""
import argparse
import logging
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..bridges.bridge import Bridge
from ..config import (
    ConfigError,
    InitError,
    NotebookError,
    generate_experiment,
    load_experiment,
    validate_experiment,
)
from ..config.models import ExperimentSpec, HyperParamSpec
from ..runners.runner import Runner
from ..store.metric_store import MetricStore
from ..tuners.factory import create_tuner, initial_from_notebook
from ..tuners.types import Trial

SUBSCRIPTION_SETTLE_SECONDS = 0.5  # lets the runner's and tuner's subscriptions become active
"""Pause between starting the Bridge and launching the first run.

Subscriptions are sent asynchronously; without this pause the first start
command could be published before the Runner's subscription is active at the
broker. (The Tuner's start watchdog is the second line of defence.)
"""


def _err(message: str) -> None:
    """Print a message to stderr."""
    print(message, file=sys.stderr)


# --------------------------------------------------------------------------- init


def cmd_init(args: argparse.Namespace) -> int:
    """``qorotrainctl init``: generate a starting ``experiment.yaml`` from a notebook.

    Args:
        args: Parsed arguments with ``notebook``, ``output`` and ``force``.

    Returns:
        0 on success; 2 if the output exists without ``--force`` or the
        notebook cannot be used.

    Side Effects:
        Writes the output file and prints a summary of the tuned and fixed
        parameters.
    """
    output = Path(args.output)
    if output.exists() and not args.force:
        _err(f"{output} already exists (use --force to overwrite)")
        return 2

    try:
        result = generate_experiment(args.notebook, output_dir=output.resolve().parent)
    except (InitError, NotebookError) as exc:
        _err(f"error: {exc}")
        return 2

    output.write_text(result.yaml, encoding="utf-8")
    print(f"wrote {output}")
    for name, guess in result.tuned.items():
        scale = ", log scale" if guess.log else ""
        print(f"  tuned  {name}: {guess.kind} [{guess.min:g}, {guess.max:g}]{scale}")
    for name in result.fixed:
        print(f"  fixed  {name}")
    if result.problems:
        print("\nthe parameters cell has lines init could not read (validate will report them):")
        for problem in result.problems:
            print(f"  {problem}")
    print("\nnext: set job.objective.metric, review the guessed ranges, then run `qorotrainctl validate`")
    return 0


# ----------------------------------------------------------------------- validate


def cmd_validate(args: argparse.Namespace) -> int:
    """``qorotrainctl validate``: load the spec and report errors and warnings.

    Args:
        args: Parsed arguments with ``file`` and ``skip_broker``.

    Returns:
        0 if there are no errors (warnings allowed), 1 otherwise.
    """
    path = Path(args.file)
    try:
        spec = load_experiment(path)
    except ConfigError as exc:
        _err(str(exc))
        return 1

    report = validate_experiment(spec, base_dir=path.resolve().parent, check_broker=not args.skip_broker)
    if report.errors or report.warnings:
        print(report.format())
    if report.ok:
        print(f"{path} is valid ({len(report.warnings)} warning(s))")
        return 0
    print(f"{path} has {len(report.errors)} error(s)")
    return 1


# --------------------------------------------------------------------------- run


def _number(x: object) -> str:
    """Format floats compactly (``%g``) and everything else with ``str``."""
    return f"{x:g}" if isinstance(x, float) else str(x)


def format_params(params: dict[str, Any]) -> str:
    """Render parameters as ``name=value, ...`` for terminal output.

    Args:
        params: Parameter values.

    Returns:
        The formatted line.
    """
    return ", ".join(f"{name}={_number(value)}" for name, value in params.items())


def describe_param(spec: HyperParamSpec) -> str:
    """Describe a search range in one line, for example ``float [1e-05, 0.01] log``.

    Args:
        spec: The parameter's specification.

    Returns:
        The description.
    """
    if spec.choices is not None:
        return f"{spec.type} choices {[_number(c) for c in spec.choices]}".replace("'", "")
    return f"{spec.type} [{_number(spec.min)}, {_number(spec.max)}]" + (" log" if spec.log else "")


def describe_plan(spec: ExperimentSpec, initial: dict[str, Any] | None) -> list[str]:
    """Summarise what ``run`` is about to do (printed before starting and by ``--dry-run``).

    Args:
        spec: The loaded experiment.
        initial: Baseline parameters, or ``None`` when there is no baseline.

    Returns:
        Lines describing experiment, algorithm, objective, baseline and
        search ranges.
    """
    tuner, job = spec.tuner, spec.job
    seeded = f" (seed {tuner.seed})" if tuner.algorithm in ("random", "grid") else ""
    lines = [
        f"experiment : {spec.application.name}",
        f"algorithm  : {tuner.algorithm}{seeded}, up to {tuner.max_runs} runs, review {tuner.review.mode}",
        f"objective  : {job.objective.direction} {job.objective.metric} ({job.objective.aggregate})",
    ]
    if initial is not None:
        lines.append(f"baseline   : {format_params(initial)}")
    lines += [f"tuning     : {name}: {describe_param(hp)}" for name, hp in job.hyperparameters.items()]
    return lines


def format_summary(trials: list[Trial], best: Trial | None, stop_reason: str | None, direction: str) -> str:
    """Render the end-of-session table of runs, the best run and the stop reason.

    Args:
        trials: Completed trials, oldest first.
        best: The best trial, or ``None``.
        stop_reason: Why the session ended.
        direction: ``maximize`` or ``minimize`` (shown next to the best run).

    Returns:
        The multi-line summary.
    """
    lines = [f"{'run':<12}{'status':<10}{'objective':>12}  params"]
    for trial in trials:
        objective = "-" if trial.objective is None else f"{trial.objective:.6g}"
        lines.append(f"{trial.run:<12}{trial.status:<10}{objective:>12}  {format_params(trial.params)}")
    lines.append("")
    if best is not None:
        lines.append(f"best    : {best.run} ({direction}d) objective {best.objective:.6g}  {format_params(best.params)}")
    else:
        lines.append("best    : none (no run produced an objective)")
    lines.append(f"stopped : {stop_reason or 'unknown'}")
    return "\n".join(lines)


@contextmanager
def _cli_logging(debug: bool) -> Iterator[None]:
    """Show the library's progress messages on stderr for the duration of a command, then restore."""
    logger = logging.getLogger("qorotrain")
    previous_level = logger.level
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _resolve(base: Path, path: str) -> Path:
    """Resolve ``path`` against ``base`` unless it is absolute."""
    p = Path(path)
    return p if p.is_absolute() else base / p


def cmd_run(args: argparse.Namespace) -> int:
    """``qorotrainctl run``: validate, print the plan, and execute the tuning session.

    The broker check is skipped with ``--dry-run``, which stops after
    printing the plan.

    Args:
        args: Parsed arguments with ``file`` and ``dry_run``.

    Returns:
        0 if a best run was found (or on a dry run); 1 for configuration or
        validation errors, an unreachable broker, or no usable result; 2 for
        unavailable algorithms or review modes; 130 if interrupted.

    Side Effects:
        Creates the store directory and file, ``bridge.db``, and the per-run
        output directories; launches notebook processes.
    """
    path = Path(args.file)
    try:
        spec = load_experiment(path)
    except ConfigError as exc:
        _err(str(exc))
        return 1
    base = path.resolve().parent

    report = validate_experiment(spec, base_dir=base, check_broker=not args.dry_run)
    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if not report.ok:
        for error in report.errors:
            print(f"error: {error}", file=sys.stderr)
        _err(f"{path} has {len(report.errors)} error(s); run `qorotrainctl validate -f {path}` for details")
        return 1

    initial = None
    if spec.tuner.baseline:
        try:
            initial = initial_from_notebook(spec, base)
        except ValueError as exc:
            _err(f"error: {exc}")
            return 1

    print("\n".join(describe_plan(spec, initial)))
    if args.dry_run:
        print("\ndry run: nothing was started")
        return 0

    store_path = _resolve(base, spec.store.path)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store = MetricStore(str(store_path))
    try:
        with _cli_logging(spec.application.log_level == "debug"):
            return _run_session(spec, store, store_path, base, initial)
    finally:
        store.close()


def _run_session(spec: ExperimentSpec, store: MetricStore, store_path: Path, base: Path, initial: dict[str, Any] | None) -> int:
    """Wire Tuner, Runner and Bridge together, run the session, and print the summary.

    Ctrl-C at any point aborts the Tuner (which asks the Runner to stop the
    active run) and still prints the summary.

    Args:
        spec: The loaded experiment.
        store: Open metric store.
        store_path: Path of the store; ``bridge.db`` is created next to it.
        base: Directory of ``experiment.yaml``.
        initial: Baseline parameters, or ``None``.

    Returns:
        The process exit code (see :func:`cmd_run`).
    """
    try:
        tuner = create_tuner(spec, store, initial=initial)
    except NotImplementedError as exc:
        _err(f"error: {exc}")
        return 2

    runner = Runner.from_spec(spec, base)
    bridge = Bridge.from_config(
        [c.to_config() for c in spec.bridge.connectors],
        components=[runner, tuner],
        db_path=str(store_path.with_name("bridge.db")),
    )
    print()
    interrupted = False
    try:
        with bridge:
            time.sleep(SUBSCRIPTION_SETTLE_SECONDS)
            tuner.begin()
            try:
                while not tuner.wait(0.2):
                    pass
            except KeyboardInterrupt:
                interrupted = True
                tuner.abort("interrupted")
    except KeyboardInterrupt:  # during start-up or shutdown, outside the wait loop
        interrupted = True
        tuner.abort("interrupted")
    except OSError as exc:
        _err(f"error: cannot connect to the broker ({exc})")
        return 1

    print("\n" + format_summary(tuner.history, tuner.best, tuner.stop_reason, spec.job.objective.direction))
    if interrupted:
        return 130
    return 0 if tuner.best is not None else 1


# ------------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    """Build the ``qorotrainctl`` argument parser with its three subcommands.

    Returns:
        The parser; each subcommand stores its handler in ``func``.
    """
    parser = argparse.ArgumentParser(prog="qorotrainctl", description="Declarative LLM-driven training tuner")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="generate a starting experiment.yaml from a notebook")
    init.add_argument("notebook", help="notebook with a cell tagged 'parameters'")
    init.add_argument("-o", "--output", default="experiment.yaml")
    init.add_argument("--force", action="store_true", help="overwrite an existing file")
    init.set_defaults(func=cmd_init)

    validate = sub.add_parser("validate", help="check experiment.yaml, the notebook and the broker")
    validate.add_argument("-f", "--file", required=True, help="path to experiment.yaml")
    validate.add_argument("--skip-broker", action="store_true", help="do not check broker reachability")
    validate.set_defaults(func=cmd_validate)

    run = sub.add_parser("run", help="run the tuning session described by experiment.yaml")
    run.add_argument("-f", "--file", required=True, help="path to experiment.yaml")
    run.add_argument("--dry-run", action="store_true", help="validate and print the plan, start nothing")
    run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point of the ``qorotrainctl`` console script.

    Args:
        argv: Arguments without the program name; ``None`` uses
            ``sys.argv[1:]``.

    Returns:
        The process exit code.

    Raises:
        SystemExit: From ``argparse`` on invalid usage (exit code 2) or
            ``--help`` (exit code 0).
    """
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
