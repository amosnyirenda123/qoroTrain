"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: tests/test_examples.py

Description:
    The files in examples/ are documentation that runs: they must keep
    validating.

===============================================================================
"""
from pathlib import Path

import pytest

from qorotrain.config import load_experiment, validate_experiment
from qorotrain.config.notebook import read_notebook
from qorotrain.runners.runner import Runner
from qorotrain.tuners.factory import create_tuner, initial_from_notebook
from qorotrain.store import MetricStore

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture
def spec():
    return load_experiment(EXAMPLES / "bandits.yaml")


def test_the_bandits_example_validates_without_warnings(spec):
    report = validate_experiment(spec, base_dir=EXAMPLES, check_broker=False)
    assert report.errors == [] and report.warnings == []


def test_the_bandits_notebook_has_a_clean_parameters_cell():
    params = read_notebook(EXAMPLES / "bandits.ipynb").parameters
    assert params.problems == []
    assert set(params.values) == {"eps", "n_plays", "n_arms", "n_repeats", "seed"}


def test_the_example_baseline_is_the_notebooks_own_value(spec):
    assert initial_from_notebook(spec, EXAMPLES) == {"eps": 0.2}


def test_the_example_builds_a_runner_and_a_tuner(spec):
    assert Runner.from_spec(spec, EXAMPLES).routes()
    with MetricStore() as store:
        assert create_tuner(spec, store, initial={"eps": 0.2}).propose([]).params["eps"] > 0