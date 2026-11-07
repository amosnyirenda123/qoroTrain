import math
import random
import threading

import pytest

from qorotrain.config.models import HyperParamSpec
from qorotrain.config.contracts.schema import MetricPoint
from qorotrain.store import MetricStore
from qorotrain.tuners.objective import aggregate, is_better
from qorotrain.tuners.space import MAX_GRID_SIZE, SearchSpace


def space(**specs):
    return SearchSpace({name: HyperParamSpec(**spec) for name, spec in specs.items()})


# -------------------------------------------------------------------- objective


@pytest.mark.parametrize(
    "values, how, direction, expected",
    [
        ([1, 5, 3], "last", "maximize", 3),
        ([1, 5, 3], "best", "maximize", 5),
        ([1, 5, 3], "best", "minimize", 1),
        ([1, 5, 3], "mean", "maximize", 3),
        ([1, 2, 3, 10], "mean_last_2", "maximize", 6.5),
        ([4, 6], "mean_last_10", "maximize", 5),  # fewer values than N: use what exists
        ([7], "mean_last_1", "maximize", 7),
    ],
)
def test_aggregate(values, how, direction, expected):
    assert aggregate(values, how, direction) == expected


def test_nothing_to_score_is_none():
    assert aggregate([], "last", "maximize") is None


@pytest.mark.parametrize("how", ["last", "mean", "mean_last_2"])
def test_a_diverged_run_has_no_objective(how):
    assert aggregate([1.0, math.nan], how, "maximize") is None
    assert aggregate([1.0, math.inf], how, "maximize") is None


def test_best_ignores_nan_unless_nothing_else_is_left():
    assert aggregate([1.0, math.nan, 3.0], "best", "maximize") == 3.0
    assert aggregate([math.nan, math.nan], "best", "maximize") is None


def test_unknown_aggregate_is_an_error():
    with pytest.raises(ValueError, match="unknown aggregate"):
        aggregate([1.0], "median", "maximize")


def test_is_better():
    assert is_better(2, 1, "maximize") and not is_better(1, 2, "maximize")
    assert is_better(1, 2, "minimize") and not is_better(2, 1, "minimize")
    assert is_better(0, None, "maximize")
    assert not is_better(1, 1, "maximize")  # ties keep the earlier run


# ------------------------------------------------------------------- validation


def test_validate_accepts_a_complete_valid_assignment():
    s = space(lr={"type": "float", "min": 0.001, "max": 0.1}, n={"type": "int", "choices": [32, 64]})
    assert s.validate({"lr": 0.01, "n": 64}) == []


def test_validate_reports_every_problem():
    s = space(lr={"type": "float", "min": 0.001, "max": 0.1}, n={"type": "int", "min": 1, "max": 4})
    problems = s.validate({"lr": 5.0, "extra": 1})
    assert any("unknown parameter 'extra'" in p for p in problems)
    assert any("'lr'" in p and "outside the range" in p for p in problems)
    assert any("missing parameter 'n'" in p for p in problems)


# --------------------------------------------------------------------- sampling


def test_samples_stay_inside_their_ranges_and_types():
    s = space(
        a={"type": "float", "min": 0.001, "max": 0.1, "log": True},
        b={"type": "float", "min": -1.0, "max": 1.0},
        c={"type": "int", "min": 2, "max": 9},
        d={"type": "int", "min": 10, "max": 1000, "log": True},
        e={"type": "int", "choices": [32, 64, 128]},
    )
    rng = random.Random(0)
    for _ in range(500):
        sample = s.sample(rng)
        assert s.validate(sample) == []
        assert isinstance(sample["c"], int) and isinstance(sample["d"], int)


def test_log_ranges_are_sampled_log_uniformly():
    s = space(lr={"type": "float", "min": 1e-5, "max": 1e-1, "log": True})
    rng = random.Random(1)
    below_1e_3 = sum(s.sample(rng)["lr"] < 1e-3 for _ in range(2000)) / 2000
    assert 0.45 < below_1e_3 < 0.55  # half of the decades; a uniform sampler would give about 1%


def test_sampling_is_reproducible_per_seed():
    s = space(x={"type": "float", "min": 0.0, "max": 1.0})
    first = [s.sample(random.Random(7)) for _ in range(3)]
    again = [s.sample(random.Random(7)) for _ in range(3)]
    other = [s.sample(random.Random(8)) for _ in range(3)]
    assert first == again and first != other


# ------------------------------------------------------------------------ grid


def test_axis_interpolates_between_min_and_max():
    s = space(x={"type": "float", "min": 0.0, "max": 1.0})
    assert s.axis("x", 3) == [0.0, 0.5, 1.0]
    assert s.axis("x", 5) == [0.0, 0.25, 0.5, 0.75, 1.0]


def test_axis_of_a_log_range_is_evenly_spaced_in_log_space():
    s = space(lr={"type": "float", "min": 1e-4, "max": 1e-2, "log": True})
    low, mid, high = s.axis("lr", 3)
    assert low == pytest.approx(1e-4) and mid == pytest.approx(1e-3) and high == pytest.approx(1e-2)


def test_axis_endpoints_are_exact_even_after_rounding():
    s = space(x={"type": "float", "min": 0.0123456789, "max": 0.0987654321})
    values = s.axis("x", 4)
    assert values[0] >= 0.0123456789 and values[-1] <= 0.0987654321
    assert s.validate({"x": values[-1]}) == []


def test_int_axis_rounds_and_removes_duplicates():
    s = space(n={"type": "int", "min": 1, "max": 3})
    assert s.axis("n", 5) == [1, 2, 3]
    assert s.axis("n", 2) == [1, 3]


def test_choices_contribute_all_their_values():
    s = space(h={"type": "int", "choices": [32, 64, 128, 256]})
    assert s.axis("h", 2) == [32, 64, 128, 256]


def test_grid_is_the_product_of_the_axes():
    s = space(a={"type": "float", "min": 0.0, "max": 1.0}, h={"type": "int", "choices": [1, 2]})
    grid = s.grid(3)
    assert len(grid) == 6 == s.grid_size(3)
    assert {"a": 0.0, "h": 1} in grid and {"a": 1.0, "h": 2} in grid
    assert all(s.validate(cell) == [] for cell in grid)


def test_grid_guards_its_size_and_arguments():
    with pytest.raises(ValueError, match="at least 2 points"):
        space(a={"type": "float", "min": 0.0, "max": 1.0}).grid(1)
    wide = SearchSpace({f"p{i}": HyperParamSpec(type="float", min=0.0, max=1.0) for i in range(12)})
    assert wide.grid_size(3) > MAX_GRID_SIZE
    with pytest.raises(ValueError, match="cells"):
        wide.grid(3)


# ------------------------------------------------------------------------ store


@pytest.fixture
def store():
    with MetricStore() as s:
        yield s


def points(*rows):
    return [MetricPoint(step, values, t=float(step)) for step, values in rows]


def test_run_lifecycle_round_trip(store):
    store.create_run("r1", "demo", {"lr": 0.1, "n": 4}, ts=1.0)
    assert store.run("r1")["status"] == "created"

    store.mark_started("r1", ts=2.0)
    store.finish_run("r1", "finished", 12.5, None, ts=3.0)
    run = store.run("r1")
    assert run == {
        "run_id": "r1", "project": "demo", "params": {"lr": 0.1, "n": 4}, "status": "finished",
        "objective": 12.5, "error": None, "created_at": 1.0, "started_at": 2.0, "finished_at": 3.0,
    }


def test_a_late_started_event_does_not_reopen_a_finished_run(store):
    store.create_run("r1", "demo", {})
    store.finish_run("r1", "failed", None, "boom")
    store.mark_started("r1")
    assert store.run("r1")["status"] == "failed"


def test_non_finite_objectives_are_stored_as_null(store):
    store.create_run("r1", "demo", {})
    store.finish_run("r1", "failed", math.nan, "diverged")
    assert store.run("r1")["objective"] is None


def test_unknown_run_and_project_listing(store):
    assert store.run("nope") is None
    store.create_run("a", "p1", {}, ts=1.0)
    store.create_run("b", "p2", {}, ts=2.0)
    store.create_run("c", "p1", {}, ts=3.0)
    assert [r["run_id"] for r in store.runs()] == ["a", "b", "c"]
    assert [r["run_id"] for r in store.runs("p1")] == ["a", "c"]


def test_metrics_are_stored_in_one_batch_and_read_back_in_step_order(store):
    inserted = store.add_metrics("r1", points((2, {"reward": 3.0, "loss": 0.3}), (0, {"reward": 1.0}), (1, {"reward": 2.0})))
    assert inserted == 4
    assert store.series("r1", "reward") == [(0, 1.0), (1, 2.0), (2, 3.0)]
    assert store.series("r1", "loss") == [(2, 0.3)]
    assert store.metric_names("r1") == ["loss", "reward"]
    assert store.series("r1", "unknown") == [] and store.series("other", "reward") == []


def test_redelivered_batches_are_ignored(store):
    batch = points((0, {"reward": 1.0}), (1, {"reward": 2.0}))
    assert store.add_metrics("r1", batch) == 2
    assert store.add_metrics("r1", batch) == 0  # QoS 1 duplicate
    assert store.series("r1", "reward") == [(0, 1.0), (1, 2.0)]


def test_empty_batches_are_fine(store):
    assert store.add_metrics("r1", []) == 0


def test_nan_survives_a_round_trip_so_divergence_stays_visible(store):
    store.add_metrics("r1", points((0, {"loss": 1.0}), (1, {"loss": math.nan}), (2, {"loss": math.inf})))
    series = store.series("r1", "loss")
    assert series[0] == (0, 1.0)
    assert math.isnan(series[1][1]) and series[2][1] == math.inf


def test_concurrent_writers_do_not_lose_data(store):
    def write(run):
        for step in range(50):
            store.add_metrics(run, points((step, {"reward": float(step)})))

    threads = [threading.Thread(target=write, args=(f"r{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(len(store.series(f"r{i}", "reward")) == 50 for i in range(4))


def test_data_persists_in_a_file(tmp_path):
    path = str(tmp_path / "qorotrain.db")
    with MetricStore(path) as first:
        first.create_run("r1", "demo", {"x": 1})
        first.add_metrics("r1", points((0, {"reward": 9.0})))
    with MetricStore(path) as second:
        assert second.run("r1")["params"] == {"x": 1}
        assert second.series("r1", "reward") == [(0, 9.0)]