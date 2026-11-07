from pathlib import Path

from ..config.models import ExperimentSpec
from ..config.notebook import read_notebook
from ..config.validate import resolve_source
from ..store.metric_store import MetricStore
from .review import Reviewer
from .space import SearchSpace
from .tuner import GridTuner, RandomTuner, Tuner


def initial_from_notebook(spec: ExperimentSpec, base_dir: str | Path = ".") -> dict:
    """The notebook's current values of the tunable parameters: the baseline run."""
    values = read_notebook(resolve_source(spec, base_dir)).parameters.values
    missing = [name for name in spec.job.hyperparameters if name not in values]
    if missing:
        raise ValueError(f"the notebook does not assign: {', '.join(missing)}")
    return {name: values[name] for name in spec.job.hyperparameters}


def create_tuner(
    spec: ExperimentSpec,
    store: MetricStore,
    *,
    initial: dict | None = None,
    reviewer: Reviewer | None = None,
    session: str | None = None,
    **overrides,
) -> Tuner:
    """Build the tuner named by ``tuner.algorithm``. ``overrides`` reach the Tuner constructor."""
    tuner = spec.tuner
    if tuner.review.mode == "manual" and reviewer is None:
        raise NotImplementedError(
            "manual review is not available yet: set tuner.review.mode to 'auto', or pass a reviewer"
        )

    common = dict(
        space=SearchSpace.from_spec(spec.job),
        objective=spec.job.objective,
        project=spec.application.name,
        store=store,
        topic_root=spec.bridge.topic_root,
        max_runs=tuner.max_runs,
        stop=tuner.stop,
        initial=initial,
        reviewer=reviewer,
        session=session,
    )
    common.update(overrides)

    match tuner.algorithm:
        case "random":
            return RandomTuner(seed=tuner.seed, **common)
        case "grid":
            return GridTuner(points=tuner.grid_points, seed=tuner.seed, **common)
        case other:
            raise NotImplementedError(f"tuner.algorithm '{other}' is not available yet (available: random, grid)")