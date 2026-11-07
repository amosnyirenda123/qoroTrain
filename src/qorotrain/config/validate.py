"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/config/validate.py

Description:
    Semantic validation of an experiment beyond what the schema can check.

    :mod:`qorotrain.config.loader` guarantees that ``experiment.yaml`` is
    well-formed. This module checks that it is *consistent with the outside
    world*: that the notebook exists and has a clean ``parameters`` cell that
    assigns every declared parameter, that the objective metric is actually
    logged, that the chosen algorithm is implemented, and (optionally) that
    each broker accepts TCP connections.

    Errors and warnings:
        Errors (the run cannot work) make :attr:`ValidationReport.ok` false
        and block ``qorotrainctl run``. Warnings (the run will work but
        probably not as intended) are printed and do not block.

    The checks are static: the notebook is parsed with ``ast`` through
    :mod:`qorotrain.config.notebook` and never executed.

===============================================================================
"""
import socket
from dataclasses import dataclass, field
from pathlib import Path

from .models import ExperimentSpec
from .notebook import PARAMETERS_TAG, NotebookError, read_notebook


@dataclass
class ValidationReport:
    """Accumulated findings of :func:`validate_experiment`.

    Attributes:
        errors (list[str]): Problems that prevent a run.
        warnings (list[str]): Problems worth fixing that do not prevent a run.
    """

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """``True`` when there are no errors (warnings are allowed)."""
        return not self.errors

    def format(self) -> str:
        """Render the findings as ``error: ...`` lines followed by ``warning: ...`` lines.

        Returns:
            The report text, empty if there are no findings.
        """
        lines = [f"error: {e}" for e in self.errors] + [f"warning: {w}" for w in self.warnings]
        return "\n".join(lines)


def resolve_source(spec: ExperimentSpec, base_dir: str | Path) -> Path:
    """The notebook path, relative to the directory of experiment.yaml.

    Args:
        spec: The loaded experiment.
        base_dir: Directory containing ``experiment.yaml``.

    Returns:
        ``job.source`` unchanged if absolute, otherwise joined to ``base_dir``.
    """
    source = Path(spec.job.source)
    return source if source.is_absolute() else Path(base_dir) / source


def validate_experiment(
    spec: ExperimentSpec,
    base_dir: str | Path = ".",
    check_broker: bool = True,
    broker_timeout: float = 1.0,
) -> ValidationReport:
    """Check an already-loaded spec against the notebook and, optionally, the broker.

    Args:
        spec: The loaded experiment.
        base_dir: Directory containing ``experiment.yaml``; relative paths in
            the spec are resolved against it.
        check_broker: Whether to open a TCP connection to every connector.
        broker_timeout: Seconds to wait for each broker connection.

    Returns:
        The collected errors and warnings. This function does not raise for
        validation problems; they are all reported in the result.

    Side Effects:
        Reads the notebook file and, when ``check_broker`` is true, opens and
        immediately closes a TCP connection to each broker.
    """
    report = ValidationReport()

    _check_objective_placeholder(spec, report)
    _check_algorithm(spec, report)
    _check_notebook(spec, resolve_source(spec, base_dir), report)
    if check_broker:
        _check_brokers(spec, broker_timeout, report)
    return report


def _check_objective_placeholder(spec: ExperimentSpec, report: ValidationReport) -> None:
    """Error if the objective metric is still the ``TODO`` placeholder written by ``init``.

    Args:
        spec: The loaded experiment.
        report: Report to append to.
    """
    if spec.job.objective.metric.strip().lower() == "todo":
        report.errors.append(
            "job.objective.metric is still TODO; set it to a metric your notebook logs"
        )


AVAILABLE_ALGORITHMS = ("random", "grid")
"""Values of ``tuner.algorithm`` that ``qorotrainctl run`` can execute today."""


def _check_algorithm(spec: ExperimentSpec, report: ValidationReport) -> None:
    """Warn if the algorithm is accepted by the schema but not implemented.

    Args:
        spec: The loaded experiment.
        report: Report to append to.
    """
    if spec.tuner.algorithm not in AVAILABLE_ALGORITHMS:
        report.warnings.append(
            f"tuner.algorithm '{spec.tuner.algorithm}' is not available yet "
            f"(`run` supports: {', '.join(AVAILABLE_ALGORITHMS)})"
        )


def _check_notebook(spec: ExperimentSpec, path: Path, report: ValidationReport) -> None:
    """Check the notebook against the declared parameters and objective.

    Errors: unreadable notebook, missing kernel metadata, no ``parameters``
    cell, non-literal statements in that cell, and declared parameters the
    cell does not assign. Warnings: notebook values outside the declared
    range, cell variables that are neither tuned nor fixed, no ``qorotrain``
    import, and an objective metric that no ``.log(...)`` call uses.

    Args:
        spec: The loaded experiment.
        path: Resolved notebook path.
        report: Report to append to.
    """
    try:
        notebook = read_notebook(path)
    except NotebookError as exc:
        report.errors.append(str(exc))
        return

    if not notebook.language:
        report.errors.append(
            f"{path.name} has no kernelspec or language_info metadata, so Papermill cannot inject "
            "parameters (open the notebook in Jupyter and save it once)"
        )

    params = notebook.parameters
    if not params.found:
        report.errors.append(
            f"no cell is tagged '{PARAMETERS_TAG}' in {path.name} "
            "(JupyterLab: select the cell, open the right sidebar, Common Tools, Cell Tags)"
        )
        return

    for problem in params.problems:
        report.errors.append(
            f"'{PARAMETERS_TAG}' cell: {problem} "
            "(keep only plain assignments here; move everything else to the next cell)"
        )

    declared = {**spec.job.hyperparameters, **spec.job.fixed}
    for name in declared:
        if name not in params.assigned:
            report.errors.append(f"'{name}' is declared in the YAML but not assigned in the '{PARAMETERS_TAG}' cell")

    for name, hp in spec.job.hyperparameters.items():
        if name in params.values:
            reason = hp.accepts(params.values[name])
            if reason:
                report.warnings.append(f"hyperparameter '{name}': the notebook's {reason}")

    for name in sorted(set(params.values) - set(declared)):
        report.warnings.append(
            f"'{name}' is assigned in the '{PARAMETERS_TAG}' cell but is neither tuned nor fixed "
            "in the YAML; the notebook value will be used"
        )

    if not notebook.imports_qorotrain():
        report.warnings.append(
            f"{path.name} never imports qorotrain, so it will not send any telemetry"
        )
    else:
        logged = notebook.logged_metrics()
        metric = spec.job.objective.metric
        if logged and metric not in logged and metric.strip().lower() != "todo":
            report.warnings.append(
                f"objective metric '{metric}' is not among the metrics the notebook logs "
                f"({', '.join(sorted(logged))})"
            )


def _check_brokers(spec: ExperimentSpec, timeout: float, report: ValidationReport) -> None:
    """Error for every connector whose broker does not accept a TCP connection.

    Only reachability is tested (a TCP handshake); credentials and the MQTT
    protocol handshake are not.

    Args:
        spec: The loaded experiment.
        timeout: Seconds to wait per broker.
        report: Report to append to.
    """
    for i, connector in enumerate(spec.bridge.connectors):
        label = connector.name or f"connector-{i}"
        try:
            socket.create_connection((connector.host, connector.port), timeout=timeout).close()
        except OSError as exc:
            report.errors.append(
                f"cannot reach {connector.type} broker '{label}' at {connector.host}:{connector.port} ({exc})"
            )
