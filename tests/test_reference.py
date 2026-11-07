"""The YAML reference must describe every key of the schema, and only keys that exist."""
import re
from pathlib import Path

import pytest

from qorotrain.config import parse_experiment
from qorotrain.config.reference import DESCRIPTIONS, LABEL, RESERVED, fields, render_markdown

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "experiment-yaml-reference.md"
SRC = ROOT / "src" / "qorotrain"

MINIMAL = """
version: 1
application: {name: demo}
tuner: {algorithm: random}
job:
  source: demo.ipynb
  objective: {metric: reward, direction: maximize}
  hyperparameters: {lr: {type: float, min: 0.001, max: 0.1}}
"""


def test_every_key_has_a_description_and_no_description_is_stale():
    paths = {d.path for d in fields()}
    assert not paths - set(DESCRIPTIONS), f"keys without a description: {sorted(paths - set(DESCRIPTIONS))}"
    assert not set(DESCRIPTIONS) - paths, f"descriptions of keys that no longer exist: {sorted(set(DESCRIPTIONS) - paths)}"


def test_descriptions_say_something():
    assert all(len(entry.meaning.split()) >= 3 for entry in DESCRIPTIONS.values())


def test_every_key_appears_in_the_rendered_reference():
    text = render_markdown()
    assert all(f"`{doc.path}`" in text for doc in fields())


def test_tables_are_well_formed():
    """Four columns means five unescaped pipes per row, whatever the descriptions contain."""
    rows = [line for line in render_markdown().splitlines() if line.startswith("|")]
    assert rows
    for row in rows:
        assert len(re.findall(r"(?<!\\)\|", row)) == 5, row


def test_the_checked_in_page_is_up_to_date():
    assert PAGE.read_text() == render_markdown(), (
        "docs/experiment-yaml-reference.md is stale; regenerate it with: "
        "python -m qorotrain.config.reference > docs/experiment-yaml-reference.md"
    )


@pytest.mark.parametrize(
    "path, shown",
    [
        ("application.log_level", "`info`"),
        ("bridge.topic_root", "`tuning`"),
        ("bridge.connectors[].host", "`localhost`"),
        ("bridge.connectors[].port", "`1883`"),
        ("bridge.connectors[].default_qos", "`1`"),
        ("store.path", "`./qorotrain.db`"),
        ("tuner.max_runs", "`8`"),
        ("tuner.seed", "`0`"),
        ("tuner.baseline", "`true`"),
        ("tuner.grid_points", "`3`"),
        ("tuner.review.mode", "`manual`"),
        ("runner.kernel", "`python3`"),
        ("runner.timeout", "`3600`"),
        ("runner.output_dir", "`./runs`"),
        ("job.objective.aggregate", "`last`"),
    ],
)
def test_defaults_shown_in_the_reference_are_the_real_defaults(path, shown):
    spec = parse_experiment(MINIMAL)
    actual = spec
    for part in path.replace("[]", "").split("."):
        actual = actual[0] if isinstance(actual, list) else actual
        actual = getattr(actual, part)
    doc = next(d for d in fields() if d.path == path)
    assert doc.default_text == shown
    assert f"`{actual}`".lower() == shown.lower() or str(actual).lower() == shown.strip("`").lower()


def test_keys_marked_not_used_yet_really_are_not_read_by_the_code():
    """When the agent or summarizer arrives, this fails and reminds you to update the reference."""
    reserved = {p for p, e in DESCRIPTIONS.items() if e.status == RESERVED}
    assert {"tuner.summarizer", "tuner.agent", "tuner.agent.retrieval"} <= reserved
    sources = {
        p: p.read_text()
        for p in SRC.rglob("*.py")
        if p.name not in {"models.py", "reference.py"}
    }
    for token in ("summarizer", "use_notebook_context", "retrieval", "tuner.agent", ".llm"):
        users = [p.name for p, text in sources.items() if token in text]
        assert not users, f"'{token}' is read by {users}: update its status in reference.py"


def test_label_only_keys_are_flagged():
    assert {p for p, e in DESCRIPTIONS.items() if e.status == LABEL} == {"job.name", "bridge.connectors[].name"}