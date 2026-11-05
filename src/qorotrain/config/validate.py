import socket
from dataclasses import dataclass, field
from pathlib import Path

from .models import ExperimentSpec
from .notebook import PARAMETERS_TAG, NotebookError, read_notebook


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def format(self) -> str:
        lines = [f"error: {e}" for e in self.errors] + [f"warning: {w}" for w in self.warnings]
        return "\n".join(lines)


def resolve_source(spec: ExperimentSpec, base_dir: str | Path) -> Path:
    """The notebook path, relative to the directory of experiment.yaml."""
    source = Path(spec.job.source)
    return source if source.is_absolute() else Path(base_dir) / source


def validate_experiment(
    spec: ExperimentSpec,
    base_dir: str | Path = ".",
    check_broker: bool = True,
    broker_timeout: float = 1.0,
) -> ValidationReport:
    """Check an already-loaded spec against the notebook and, optionally, the broker."""
    report = ValidationReport()

    _check_objective_placeholder(spec, report)
    _check_notebook(spec, resolve_source(spec, base_dir), report)
    if check_broker:
        _check_brokers(spec, broker_timeout, report)
    return report


def _check_objective_placeholder(spec: ExperimentSpec, report: ValidationReport) -> None:
    if spec.job.objective.metric.strip().lower() == "todo":
        report.errors.append(
            "job.objective.metric is still TODO; set it to a metric your notebook logs"
        )


def _check_notebook(spec: ExperimentSpec, path: Path, report: ValidationReport) -> None:
    try:
        notebook = read_notebook(path)
    except NotebookError as exc:
        report.errors.append(str(exc))
        return

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
    for i, connector in enumerate(spec.bridge.connectors):
        label = connector.name or f"connector-{i}"
        try:
            socket.create_connection((connector.host, connector.port), timeout=timeout).close()
        except OSError as exc:
            report.errors.append(
                f"cannot reach {connector.type} broker '{label}' at {connector.host}:{connector.port} ({exc})"
            )