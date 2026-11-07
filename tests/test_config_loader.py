import pytest

from nbhelpers import experiment_yaml
from qorotrain.config import ConfigError, load_experiment, parse_experiment
from qorotrain.connectors.config import MQTTConfig

MINIMAL = """\
version: 1
application: {name: demo}
tuner: {algorithm: random}
job:
  source: demo.ipynb
  objective: {metric: reward, direction: maximize}
  hyperparameters:
    lr: {type: float, min: 0.001, max: 0.1}
"""


def problems(text: str) -> list[str]:
    with pytest.raises(ConfigError) as exc:
        parse_experiment(text)
    return exc.value.problems


def replace(text: str, old: str, new: str) -> str:
    assert old in text
    return text.replace(old, new)


# -------------------------------------------------------------------- defaults


def test_minimal_file_loads_with_defaults():
    spec = parse_experiment(MINIMAL)
    assert spec.tuner.max_runs == 8
    assert spec.tuner.review.mode == "manual"
    assert spec.runner.timeout == 3600
    assert spec.store.path == "./qorotrain.db"
    assert spec.bridge.connectors[0].host == "localhost"
    assert spec.job.objective.aggregate == "last"


def test_connector_spec_converts_to_an_mqtt_config():
    text = replace(
        experiment_yaml(), "{type: mqtt, host: localhost, port: 1883}",
        "{type: mqtt, host: broker.lan, port: 8883, username: u, password: p, default_qos: 2}",
    )
    cfg = parse_experiment(text).bridge.connectors[0].to_config()
    assert cfg == MQTTConfig(host="broker.lan", port=8883, qos=2, username="u", password="p")


def test_load_experiment_reads_a_file(tmp_path):
    path = tmp_path / "experiment.yaml"
    path.write_text(MINIMAL)
    assert load_experiment(path).application.name == "demo"


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="cannot read file"):
        load_experiment(tmp_path / "nope.yaml")


# --------------------------------------------------------------- strictness


def test_unknown_keys_are_rejected_with_their_path():
    assert "tuner.max_run: unknown key" in problems(replace(MINIMAL, "{algorithm: random}", "{algorithm: random, max_run: 3}"))


def test_missing_required_fields_are_reported():
    text = MINIMAL.replace("application: {name: demo}\n", "")
    assert "application: required" in problems(text)


def test_all_problems_are_reported_together():
    text = replace(MINIMAL, "{algorithm: random}", "{algorithm: nope, max_runs: 0}")
    found = problems(text)
    assert any(p.startswith("tuner.algorithm") for p in found)
    assert any(p.startswith("tuner.max_runs") for p in found)


def test_wrong_version_is_rejected():
    assert any(p.startswith("version") for p in problems(MINIMAL.replace("version: 1", "version: 2")))


def test_top_level_must_be_a_mapping():
    assert "mapping" in problems("- a\n- b\n")[0]


def test_invalid_yaml_is_a_config_error():
    assert "invalid YAML" in problems("a: [1, 2\n")[0]


def test_duplicate_keys_are_rejected():
    text = MINIMAL + "tuner: {algorithm: optuna}\n"
    assert "duplicate key 'tuner'" in problems(text)[0]


# --------------------------------------------------------------- numbers


def test_scientific_notation_without_a_dot_is_a_float():
    spec = parse_experiment(replace(MINIMAL, "min: 0.001, max: 0.1", "min: 1e-5, max: 1e-2"))
    hp = spec.job.hyperparameters["lr"]
    assert hp.min == 1e-5 and hp.max == 1e-2


def test_fixed_values_keep_their_types():
    spec = parse_experiment(MINIMAL + "  fixed: {lr_floor: 1e-6, seed: 31, tag: x}\n")
    assert spec.job.fixed == {"lr_floor": 1e-6, "seed": 31, "tag": "x"}


# --------------------------------------------------------- hyperparameters


@pytest.mark.parametrize(
    "spec, fragment",
    [
        ("{type: float, min: 1, max: 1}", "min must be less than max"),
        ("{type: float, min: 0, max: 1, log: true}", "log scale needs min > 0"),
        ("{type: float, min: 0.1}", "needs both min and max"),
        ("{type: float, choices: [1, 2], min: 0, max: 3}", "either choices or min/max"),
        ("{type: float, choices: [1, 2], log: true}", "log only applies"),
        ("{type: int, min: 0.5, max: 4}", "whole-number bounds"),
        ("{type: int, choices: [1]}", "at least 2"),
        ("{type: str, min: 0, max: 1}", "type"),
    ],
)
def test_invalid_hyperparameters(spec, fragment):
    text = replace(MINIMAL, "{type: float, min: 0.001, max: 0.1}", spec)
    assert any(fragment in p for p in problems(text))


def test_hyperparameter_names_must_be_variables():
    text = replace(MINIMAL, "    lr:", "    learning-rate:")
    assert any("not a valid Python variable name" in p for p in problems(text))


def test_a_name_cannot_be_both_tuned_and_fixed():
    assert any("both tuned and fixed: lr" in p for p in problems(MINIMAL + "  fixed: {lr: 0.01}\n"))


def test_at_least_one_hyperparameter_is_required():
    text = MINIMAL.replace("    lr: {type: float, min: 0.001, max: 0.1}\n", "").replace(
        "hyperparameters:\n", "hyperparameters: {}\n"
    )
    assert any(p.startswith("job.hyperparameters") for p in problems(text))


@pytest.mark.parametrize(
    "spec, value, ok",
    [
        ("{type: float, min: 0.1, max: 1}", 0.5, True),
        ("{type: float, min: 0.1, max: 1}", 5, False),
        ("{type: float, min: 0.1, max: 1}", "x", False),
        ("{type: float, min: 0.1, max: 1}", True, False),
        ("{type: int, min: 1, max: 10}", 3, True),
        ("{type: int, min: 1, max: 10}", 3.0, True),
        ("{type: int, min: 1, max: 10}", 3.5, False),
        ("{type: int, choices: [32, 64]}", 64, True),
        ("{type: int, choices: [32, 64]}", 48, False),
    ],
)
def test_accepts(spec, value, ok):
    hp = parse_experiment(replace(MINIMAL, "{type: float, min: 0.001, max: 0.1}", spec)).job.hyperparameters["lr"]
    assert (hp.accepts(value) is None) is ok


# ------------------------------------------------------------------- tuner


def test_agent_algorithm_requires_an_llm():
    assert any("tuner.agent" in p for p in problems(replace(MINIMAL, "random", "agent")))


def test_agent_algorithm_with_an_llm_loads():
    text = replace(MINIMAL, "{algorithm: random}", "{algorithm: agent, agent: {llm: {model: 'qwen2.5:3b'}}}")
    assert parse_experiment(text).tuner.agent.llm.model == "qwen2.5:3b"


def test_ema_fast_must_exceed_slow():
    text = replace(MINIMAL, "{algorithm: random}", "{algorithm: random, summarizer: {ema: {fast: 0.01, slow: 0.1}}}")
    assert any("ema.fast must be larger" in p for p in problems(text))


def test_duplicate_connector_names_are_rejected():
    text = MINIMAL + "bridge:\n  connectors: [{name: a}, {name: a}]\n"
    assert any("connector names must be unique" in p for p in problems(text))


@pytest.mark.parametrize("aggregate, ok", [("last", True), ("best", True), ("mean_last_100", True), ("mean_last_0", False), ("median", False)])
def test_objective_aggregate(aggregate, ok):
    text = replace(MINIMAL, "direction: maximize", f"direction: maximize, aggregate: {aggregate}")
    if ok:
        assert parse_experiment(text).job.objective.aggregate == aggregate
    else:
        assert any("aggregate" in p for p in problems(text))


# ------------------------------------------------------------ environment


def test_env_vars_are_substituted(monkeypatch):
    monkeypatch.setenv("MQTT_PASSWORD", "s3cret")
    text = MINIMAL + "bridge:\n  connectors: [{username: me, password: '${MQTT_PASSWORD}'}]\n"
    assert parse_experiment(text).bridge.connectors[0].password == "s3cret"


def test_missing_env_vars_are_all_reported(monkeypatch):
    monkeypatch.delenv("NOPE_A", raising=False)
    monkeypatch.delenv("NOPE_B", raising=False)
    text = MINIMAL + "bridge:\n  connectors: [{username: '${NOPE_A}', password: '${NOPE_B}'}]\n"
    found = problems(text)
    assert len(found) == 2
    assert any("NOPE_A" in p for p in found) and any("NOPE_B" in p for p in found)


# ------------------------------------------------------------- tuner settings


def test_tuner_defaults_for_seed_baseline_and_grid():
    tuner = parse_experiment(MINIMAL).tuner
    assert (tuner.seed, tuner.baseline, tuner.grid_points) == (0, True, 3)


def test_grid_is_a_valid_algorithm_with_its_own_settings():
    text = replace(MINIMAL, "{algorithm: random}", "{algorithm: grid, grid_points: 5, seed: 7, baseline: false}")
    tuner = parse_experiment(text).tuner
    assert (tuner.algorithm, tuner.grid_points, tuner.seed, tuner.baseline) == ("grid", 5, 7, False)


def test_a_grid_needs_at_least_two_points():
    assert any("grid_points" in p for p in problems(replace(MINIMAL, "{algorithm: random}", "{algorithm: grid, grid_points: 1}")))