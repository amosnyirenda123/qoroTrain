"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: tests/test_notebook.py

Description:
    Tests of :mod:`qorotrain.config.notebook`: literal extraction from the
    ``parameters`` cell, problems reported with line numbers for derived values,
    calls and other statements, IPython magics, syntax errors, tagged-cell
    discovery and file errors.

===============================================================================
"""
import pytest

from nbhelpers import good_notebook, write_notebook
from qorotrain.config.notebook import NotebookError, parse_parameters, read_notebook



def test_plain_literals_are_read():
    cell = parse_parameters(
        "lr = 5e-4\nn = 64\nflag = True\nname = 'dqn'\nneg = -1\nsizes = [32, 64]\nnothing = None\n"
    )
    assert cell.problems == []
    assert cell.values == {
        "lr": 5e-4, "n": 64, "flag": True, "name": "dqn", "neg": -1, "sizes": [32, 64], "nothing": None,
    }


def test_annotated_assignments_and_comments_are_allowed():
    cell = parse_parameters("# hyperparameters\nlr: float = 0.001  # learning rate\n\nn: int = 3\n")
    assert cell.problems == []
    assert cell.values == {"lr": 0.001, "n": 3}


def test_derived_values_are_reported_with_line_numbers():
    cell = parse_parameters("num_episodes = 1000\nepsilon_decay_steps = num_episodes * .15\n")
    assert cell.values == {"num_episodes": 1000}
    assert cell.assigned == {"num_episodes", "epsilon_decay_steps"}
    assert len(cell.problems) == 1
    assert cell.problems[0].startswith("line 2: derived from other names")
    assert "epsilon_decay_steps" in cell.problems[0]


def test_function_calls_are_reported():
    cell = parse_parameters("env = gym.make('CartPole-v1')\n")
    assert cell.problems[0].startswith("line 1: function call")
    assert cell.values == {}


@pytest.mark.parametrize(
    "source, reason",
    [
        ("import numpy as np", "import"),
        ("from math import pi", "import"),
        ("np.random.seed(1)", "expression statement"),
        ("if True:\n    x = 1", "control flow"),
        ("for i in range(3):\n    pass", "control flow"),
        ("def f():\n    pass", "function definition"),
        ("a = b = 1", "chained or unpacked assignment"),
        ("a, b = 1, 2", "chained or unpacked assignment"),
        ("x += 1", "augmented assignment"),
    ],
)
def test_non_assignment_statements_are_reported(source, reason):
    cell = parse_parameters(source)
    assert len(cell.problems) == 1
    assert reason in cell.problems[0]


def test_ipython_magics_are_ignored_and_line_numbers_are_kept():
    cell = parse_parameters("%matplotlib inline\nlr = 0.1\nbad = f(1)\n")
    assert cell.values == {"lr": 0.1}
    assert cell.problems[0].startswith("line 3:")


def test_syntax_errors_are_reported_not_raised():
    cell = parse_parameters("lr = = 1\n")
    assert len(cell.problems) == 1
    assert "syntax error" in cell.problems[0]




def test_reads_the_tagged_cell(tmp_path):
    nb = read_notebook(good_notebook(tmp_path / "a.ipynb"))
    assert nb.parameters.found and nb.parameters.cell_index == 0
    assert nb.parameters.values["learning_rate"] == 5e-4
    assert nb.parameters.problems == []


def test_no_tagged_cell(tmp_path):
    path = write_notebook(tmp_path / "a.ipynb", [("lr = 1", [])])
    assert read_notebook(path).parameters.found is False


def test_more_than_one_tagged_cell_is_a_problem(tmp_path):
    path = write_notebook(tmp_path / "a.ipynb", [("a = 1", ["parameters"]), ("b = 2", ["parameters"])])
    params = read_notebook(path).parameters
    assert params.values == {"a": 1}  # the first one is used
    assert any("2 cells are tagged" in p for p in params.problems)


def test_cell_index_counts_all_cells(tmp_path):
    path = write_notebook(tmp_path / "a.ipynb", [("print(1)", []), ("lr = 1", ["parameters"])])
    assert read_notebook(path).parameters.cell_index == 1


def test_missing_or_wrong_files_raise(tmp_path):
    with pytest.raises(NotebookError, match="not found"):
        read_notebook(tmp_path / "nope.ipynb")
    (tmp_path / "x.txt").write_text("hi")
    with pytest.raises(NotebookError, match="not a notebook"):
        read_notebook(tmp_path / "x.txt")
    (tmp_path / "bad.ipynb").write_text("{not json")
    with pytest.raises(NotebookError, match="cannot read"):
        read_notebook(tmp_path / "bad.ipynb")





def test_detects_qorotrain_import_and_logged_metrics(tmp_path):
    nb = read_notebook(good_notebook(tmp_path / "a.ipynb"))
    assert nb.imports_qorotrain()
    assert nb.logged_metrics() == {"reward", "loss"}  # `step` is not a metric


def test_notebook_without_telemetry(tmp_path):
    nb = read_notebook(write_notebook(tmp_path / "a.ipynb", [("lr = 1", ["parameters"]), ("print(lr)", [])]))
    assert not nb.imports_qorotrain()
    assert nb.logged_metrics() == set()


def test_cells_with_magics_do_not_break_metric_detection(tmp_path):
    cell = "%matplotlib inline\ntracker.log(step=1, reward=2)\n"
    nb = read_notebook(write_notebook(tmp_path / "a.ipynb", [("lr = 1", ["parameters"]), (cell, [])]))
    assert nb.logged_metrics() == {"reward"}