"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: tests/test_validate_init.py

Description:
    Tests of :func:`~qorotrain.config.validate.validate_experiment` (errors
    versus warnings for notebook, parameters, objective, algorithm and broker
    checks) and of ``qorotrainctl init``'s generator
    (:func:`~qorotrain.config.init.classify` heuristics and loadability of the
    generated file).

===============================================================================
"""
import socket

import pytest

from nbhelpers import PARAMS, TRAINING, experiment_yaml, good_notebook, write_notebook
from qorotrain.config import (
    InitError,
    generate_experiment,
    parse_experiment,
    validate_experiment,
)
from qorotrain.config.init import classify


def check(tmp_path, *, notebook_cells=None, yaml_text=None, **kwargs):
    """Write a notebook and an experiment file, then validate (broker check off by default)."""
    if notebook_cells is None:
        good_notebook(tmp_path / "demo.ipynb")
    else:
        write_notebook(tmp_path / "demo.ipynb", notebook_cells)
    spec = parse_experiment(yaml_text or experiment_yaml())
    kwargs.setdefault("check_broker", False)
    return validate_experiment(spec, base_dir=tmp_path, **kwargs)


# --------------------------------------------------------------------- validate


def test_a_consistent_experiment_is_valid_without_warnings(tmp_path):
    report = check(tmp_path)
    assert report.ok and report.warnings == []


def test_algorithms_that_are_not_built_yet_are_a_warning(tmp_path):
    text = experiment_yaml().replace("{algorithm: random, max_runs: 4}", "{algorithm: optuna}")
    report = check(tmp_path, yaml_text=text)
    assert report.ok
    assert any("'optuna' is not available yet" in w and "random, grid" in w for w in report.warnings)


def test_todo_metric_is_an_error(tmp_path):
    report = check(tmp_path, yaml_text=experiment_yaml(metric="TODO"))
    assert any("still TODO" in e for e in report.errors)


def test_missing_notebook(tmp_path):
    spec = parse_experiment(experiment_yaml(source="nope.ipynb"))
    report = validate_experiment(spec, base_dir=tmp_path, check_broker=False)
    assert any("notebook not found" in e for e in report.errors)


def test_notebook_path_is_relative_to_the_yaml_directory(tmp_path):
    (tmp_path / "nbs").mkdir()
    good_notebook(tmp_path / "nbs" / "demo.ipynb")
    spec = parse_experiment(experiment_yaml(source="nbs/demo.ipynb"))
    assert validate_experiment(spec, base_dir=tmp_path, check_broker=False).ok


def test_notebook_without_kernel_metadata_is_an_error(tmp_path):
    write_notebook(tmp_path / "demo.ipynb", [(PARAMS, ["parameters"]), (TRAINING, [])], metadata=None)
    report = validate_experiment(parse_experiment(experiment_yaml()), base_dir=tmp_path, check_broker=False)
    assert any("no kernelspec or language_info metadata" in e and "Papermill" in e for e in report.errors)


def test_no_parameters_cell(tmp_path):
    report = check(tmp_path, notebook_cells=[(PARAMS, []), (TRAINING, [])])
    assert any("no cell is tagged 'parameters'" in e for e in report.errors)


def test_declared_parameter_missing_from_the_cell(tmp_path):
    params = PARAMS.replace("gamma = 0.9\n", "")
    report = check(tmp_path, notebook_cells=[(params, ["parameters"]), (TRAINING, [])])
    assert any("'gamma' is declared in the YAML but not assigned" in e for e in report.errors)


def test_a_cell_mixing_parameters_and_setup_is_an_error(tmp_path):
    """The shape of the real DQN notebook: derived values, calls and control flow."""
    cell = PARAMS + "env = gym.make('CartPole-v1')\nnp.random.seed(seed)\nepsilon_step = learning_rate / 2\n"
    report = check(tmp_path, notebook_cells=[(cell, ["parameters"]), (TRAINING, [])])
    messages = "\n".join(report.errors)
    assert "function call" in messages and "expression statement" in messages
    assert "derived from other names" in messages
    assert "move everything else to the next cell" in messages


def test_a_declared_parameter_that_is_derived_is_reported_once_as_a_problem(tmp_path):
    cell = PARAMS.replace("hidden_size = 64", "hidden_size = seed * 2")
    report = check(tmp_path, notebook_cells=[(cell, ["parameters"]), (TRAINING, [])])
    assert any("derived from other names" in e for e in report.errors)
    assert not any("'hidden_size' is declared in the YAML but not assigned" in e for e in report.errors)


def test_value_outside_the_declared_range_is_a_warning(tmp_path):
    params = PARAMS.replace("learning_rate = 5e-4", "learning_rate = 0.5")
    report = check(tmp_path, notebook_cells=[(params, ["parameters"]), (TRAINING, [])])
    assert report.ok
    assert any("learning_rate" in w and "outside the range" in w for w in report.warnings)


def test_undeclared_parameter_is_a_warning(tmp_path):
    params = PARAMS + "batch_size = 64\n"
    report = check(tmp_path, notebook_cells=[(params, ["parameters"]), (TRAINING, [])])
    assert report.ok
    assert any("'batch_size'" in w and "neither tuned nor fixed" in w for w in report.warnings)


def test_notebook_without_telemetry_is_a_warning(tmp_path):
    report = check(tmp_path, notebook_cells=[(PARAMS, ["parameters"]), ("print(learning_rate)", [])])
    assert report.ok
    assert any("never imports qorotrain" in w for w in report.warnings)


def test_objective_metric_that_is_never_logged_is_a_warning(tmp_path):
    report = check(tmp_path, yaml_text=experiment_yaml(metric="accuracy"))
    assert report.ok
    assert any("'accuracy' is not among the metrics" in w and "reward" in w for w in report.warnings)


def test_broker_check_fails_on_a_closed_port(tmp_path):
    with socket.socket() as s:  # grab a free port, then close it
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    report = check(tmp_path, yaml_text=experiment_yaml(host="127.0.0.1", port=port), check_broker=True)
    assert any("cannot reach mqtt broker" in e for e in report.errors)


def test_broker_check_passes_when_something_is_listening(tmp_path):
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        report = check(tmp_path, yaml_text=experiment_yaml(host="127.0.0.1", port=port), check_broker=True)
    assert report.ok


def test_report_formatting(tmp_path):
    report = check(tmp_path, yaml_text=experiment_yaml(metric="TODO"))
    assert report.format().startswith("error: ")


# ------------------------------------------------------------------- classify


@pytest.mark.parametrize(
    "name, value, kind, low, high, log",
    [
        ("learning_rate", 5e-4, "float", 5e-5, 5e-3, True),
        ("eps", 0.2, "float", 0.02, 1.0, False),
        ("gamma", 0.9, "float", 0.9, 0.999, False),
        ("gamma", 0.99, "float", 0.9, 0.999, False),
        ("temperature", 2.0, "float", 0.2, 20.0, True),
        ("t_decay", 0.99, "float", 0.9, 0.999, False),
        ("hidden_size", 64, "int", 32, 128, False),
        ("update_target_rate", 100, "int", 50, 200, False),
    ],
)
def test_tunable_guesses(name, value, kind, low, high, log):
    guess = classify(name, value)
    assert (guess.kind, guess.min, guess.max, guess.log) == (kind, low, high, log)


@pytest.mark.parametrize(
    "name, value",
    [
        ("seed", 31), ("num_episodes", 1000), ("n_plays", 1000.0), ("num_timesteps", 20000),
        ("use_cuda", True), ("env_name", "CartPole-v1"), ("sizes", [1, 2]),
        ("flag_count", 1), ("zero", 0.0), ("negative", -0.5),
        ("epsilon_start", 1.0),  # a probability already at 1.0: not a range to search
        ("n_arms", 10), ("n_classes", 3),  # sizes of the problem, not knobs
    ],
)
def test_fixed_guesses(name, value):
    assert classify(name, value).kind == "fixed"


def test_epsilon_decay_steps_is_tunable_even_though_it_contains_steps():
    assert classify("epsilon_decay_steps", 150).kind == "int"


# ----------------------------------------------------------------------- init


def test_init_generates_a_loadable_file_that_only_fails_on_the_todo_metric(tmp_path):
    nb = good_notebook(tmp_path / "demo.ipynb")
    result = generate_experiment(nb, output_dir=tmp_path)

    spec = parse_experiment(result.yaml)
    assert set(spec.job.hyperparameters) == {"learning_rate", "gamma", "hidden_size"}
    assert spec.job.fixed == {"seed": 31, "num_episodes": 300}
    assert spec.job.hyperparameters["learning_rate"].log is True

    report = validate_experiment(spec, base_dir=tmp_path, check_broker=False)
    assert report.errors == ["job.objective.metric is still TODO; set it to a metric your notebook logs"]


def test_init_output_becomes_valid_once_the_metric_is_set(tmp_path):
    nb = good_notebook(tmp_path / "demo.ipynb")
    text = generate_experiment(nb, output_dir=tmp_path).yaml.replace("metric: TODO", "metric: reward")
    report = validate_experiment(parse_experiment(text), base_dir=tmp_path, check_broker=False)
    assert report.ok, report.errors


def test_init_marks_guesses_for_review(tmp_path):
    nb = good_notebook(tmp_path / "demo.ipynb")
    text = generate_experiment(nb, output_dir=tmp_path).yaml
    assert "# review: guessed from 0.0005" in text
    assert "# review: kept fixed" in text  # seed and num_episodes


def test_init_writes_the_source_relative_to_the_output_directory(tmp_path):
    (tmp_path / "notebooks").mkdir()
    (tmp_path / "runs").mkdir()
    nb = good_notebook(tmp_path / "notebooks" / "demo.ipynb")
    text = generate_experiment(nb, output_dir=tmp_path / "runs").yaml
    assert "source: ../notebooks/demo.ipynb" in text
    assert parse_experiment(text).job.source == "../notebooks/demo.ipynb"


def test_init_keeps_going_when_the_cell_has_unreadable_lines(tmp_path):
    cell = PARAMS + "env = gym.make('CartPole-v1')\n"
    nb = write_notebook(tmp_path / "demo.ipynb", [(cell, ["parameters"]), (TRAINING, [])])
    result = generate_experiment(nb, output_dir=tmp_path)
    assert "learning_rate" in result.tuned
    assert any("function call" in p for p in result.problems)


def test_init_without_a_parameters_cell_explains_how_to_tag_one(tmp_path):
    nb = write_notebook(tmp_path / "demo.ipynb", [(PARAMS, [])])
    with pytest.raises(InitError, match="Cell Tags"):
        generate_experiment(nb, output_dir=tmp_path)


def test_init_with_nothing_readable_in_the_cell(tmp_path):
    nb = write_notebook(tmp_path / "demo.ipynb", [("env = gym.make('x')\n", ["parameters"])])
    with pytest.raises(InitError, match="no plain assignments"):
        generate_experiment(nb, output_dir=tmp_path)


def test_init_names_the_application_after_the_notebook(tmp_path):
    nb = good_notebook(tmp_path / "DQN CartPole (v2).ipynb")
    assert parse_experiment(generate_experiment(nb, output_dir=tmp_path).yaml).application.name == "dqn-cartpole-v2"