"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: tests/test_factory.py

Description:
    Tests of :mod:`qorotrain.tuners.factory`: which Tuner subclass is built for
    each algorithm, how seed, grid points, initial values and overrides reach
    it, manual review requiring a reviewer, unavailable algorithms, and reading
    the baseline from the ``parameters`` cell.

===============================================================================
"""
import pytest

from nbhelpers import good_notebook, write_notebook
from qorotrain.config import parse_experiment
from qorotrain.store import MetricStore
from qorotrain.tuners import Decision, GridTuner, RandomTuner
from qorotrain.tuners.factory import create_tuner, initial_from_notebook

BASE = """
version: 1
application: {{name: demo}}
bridge: {{topic_root: lab}}
tuner: {tuner}
job:
  source: demo.ipynb
  objective: {{metric: reward, direction: maximize}}
  hyperparameters:
    learning_rate: {{type: float, min: 1e-5, max: 1e-2, log: true}}
    gamma: {{type: float, min: 0.9, max: 0.999}}
"""


def spec(tuner="{algorithm: random, review: {mode: auto}}"):
    return parse_experiment(BASE.format(tuner=tuner))


@pytest.fixture
def store():
    with MetricStore() as s:
        yield s


def test_random_algorithm_builds_a_random_tuner_from_the_spec(store):
    tuner = create_tuner(spec("{algorithm: random, max_runs: 5, seed: 9, stop: {target: 3.5}, review: {mode: auto}}"), store)
    assert isinstance(tuner, RandomTuner)
    assert tuner._max_runs == 5 and tuner._stop_rules.target == 3.5
    assert tuner._root == "lab" and tuner._project == "demo"
    assert tuner._space.names == ["learning_rate", "gamma"]


def test_grid_algorithm_gets_its_points_from_the_spec(store):
    tuner = create_tuner(spec("{algorithm: grid, grid_points: 4, review: {mode: auto}}"), store)
    assert isinstance(tuner, GridTuner) and tuner.grid_size == 16


def test_the_seed_comes_from_the_spec(store):
    first = create_tuner(spec("{algorithm: random, seed: 5, review: {mode: auto}}"), store)
    again = create_tuner(spec("{algorithm: random, seed: 5, review: {mode: auto}}"), store)
    other = create_tuner(spec("{algorithm: random, seed: 6, review: {mode: auto}}"), store)
    assert first.propose([]).params == again.propose([]).params != other.propose([]).params


def test_initial_values_and_overrides_reach_the_tuner(store):
    tuner = create_tuner(spec(), store, initial={"learning_rate": 0.001, "gamma": 0.95}, settle_seconds=0.01, session="x")
    assert tuner._initial == {"learning_rate": 0.001, "gamma": 0.95}
    assert tuner._settle == 0.01 and tuner._session == "x"


def test_manual_review_needs_a_reviewer(store):
    manual = spec("{algorithm: random}")  # review defaults to manual
    with pytest.raises(NotImplementedError, match="manual review is not available yet"):
        create_tuner(manual, store)

    class Always:
        def review(self, proposal, history):
            return Decision("approve")

    assert create_tuner(manual, store, reviewer=Always())._reviewer is not None


@pytest.mark.parametrize("algorithm", ["optuna"])
def test_algorithms_that_do_not_exist_yet_say_what_is_available(store, algorithm):
    with pytest.raises(NotImplementedError, match="not available yet.*random, grid"):
        create_tuner(spec("{algorithm: %s, review: {mode: auto}}" % algorithm), store)


def test_the_agent_needs_its_llm_config_and_is_not_available_yet(store):
    text = "{algorithm: agent, agent: {llm: {model: m}}, review: {mode: auto}}"
    with pytest.raises(NotImplementedError, match="'agent' is not available yet"):
        create_tuner(spec(text), store)


# --------------------------------------------------------------- the baseline


def test_initial_values_come_from_the_parameters_cell(tmp_path):
    good_notebook(tmp_path / "demo.ipynb")
    assert initial_from_notebook(spec(), tmp_path) == {"learning_rate": 5e-4, "gamma": 0.9}


def test_a_missing_baseline_value_is_an_error(tmp_path):
    write_notebook(tmp_path / "demo.ipynb", [("learning_rate = 0.001", ["parameters"])])
    with pytest.raises(ValueError, match="does not assign: gamma"):
        initial_from_notebook(spec(), tmp_path)