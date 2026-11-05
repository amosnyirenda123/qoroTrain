from pathlib import Path

import nbformat

PARAMS = """\
learning_rate = 5e-4
gamma = 0.9
hidden_size = 64
seed = 31
num_episodes = 300
"""

TRAINING = """\
from qorotrain import Tracker

tracker = Tracker()
for ep in range(num_episodes):
    tracker.log(step=ep, reward=1.0, loss=0.5)
"""


def write_notebook(path: Path, cells: list[tuple[str, list[str]]]) -> Path:
    """cells: (source, tags) pairs, all code cells."""
    nb = nbformat.v4.new_notebook()
    nb.cells = [
        nbformat.v4.new_code_cell(source, metadata={"tags": tags} if tags else {})
        for source, tags in cells
    ]
    nbformat.write(nb, path)
    return path


def good_notebook(path: Path, params: str = PARAMS, training: str = TRAINING) -> Path:
    return write_notebook(path, [(params, ["parameters"]), (training, [])])


EXPERIMENT = """\
version: 1
application: {{name: demo}}
bridge:
  connectors:
    - {{type: mqtt, host: {host}, port: {port}}}
tuner: {{algorithm: random, max_runs: 4}}
job:
  source: {source}
  objective: {{metric: {metric}, direction: maximize}}
  hyperparameters:
    learning_rate: {{type: float, min: 1e-5, max: 1e-2, log: true}}
    gamma: {{type: float, min: 0.9, max: 0.999}}
    hidden_size: {{type: int, choices: [32, 64, 128]}}
  fixed:
    seed: 31
    num_episodes: 300
"""


def experiment_yaml(source="demo.ipynb", metric="reward", host="localhost", port=1883) -> str:
    return EXPERIMENT.format(source=source, metric=metric, host=host, port=port)