"""Read a notebook without running it.

Everything here works on source text with ``ast``: no cell is ever executed.
"""
import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import nbformat

PARAMETERS_TAG = "parameters"

_MAGIC = re.compile(r"^\s*[%!]")
_IMPORT_QOROTRAIN = re.compile(r"^\s*(?:import|from)\s+qorotrain\b", re.MULTILINE)

_KIND = {
    ast.Import: "import",
    ast.ImportFrom: "import",
    ast.If: "control flow",
    ast.For: "control flow",
    ast.While: "control flow",
    ast.With: "control flow",
    ast.Try: "control flow",
    ast.FunctionDef: "function definition",
    ast.ClassDef: "class definition",
    ast.Expr: "expression statement",
    ast.Assign: "chained or unpacked assignment",
    ast.AnnAssign: "annotation without a value",
    ast.AugAssign: "augmented assignment",
}


class NotebookError(Exception):
    """The notebook could not be read."""


@dataclass
class ParametersCell:
    cell_index: int | None = None
    values: dict[str, Any] = field(default_factory=dict)  # name -> literal value
    assigned: set[str] = field(default_factory=set)  # every name assigned in the cell
    problems: list[str] = field(default_factory=list)  # "line N: reason: code"

    @property
    def found(self) -> bool:
        return self.cell_index is not None


@dataclass
class NotebookInfo:
    path: Path
    code_cells: list[str]
    parameters: ParametersCell

    def imports_qorotrain(self) -> bool:
        return any(_IMPORT_QOROTRAIN.search(src) for src in self.code_cells)

    def logged_metrics(self) -> set[str]:
        """Keyword names passed to any ``<obj>.log(...)`` call (``step`` excluded)."""
        names: set[str] = set()
        for source in self.code_cells:
            try:
                tree = ast.parse(_clean(source))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "log"
                ):
                    names.update(kw.arg for kw in node.keywords if kw.arg)
        names.discard("step")
        return names


def _clean(source: str) -> str:
    """Blank IPython magics (``%x``, ``!x``) but keep line numbers intact."""
    return "\n".join("" if _MAGIC.match(line) else line for line in source.splitlines())


def _target(node: ast.stmt) -> tuple[str, ast.expr] | None:
    """(name, value) for ``x = ...`` and ``x: T = ...``; None for anything else."""
    if isinstance(node, ast.Assign):
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            return node.targets[0].id, node.value
    elif isinstance(node, ast.AnnAssign):
        if isinstance(node.target, ast.Name) and node.value is not None:
            return node.target.id, node.value
    return None


def _explain(value: ast.expr) -> str:
    if any(isinstance(n, ast.Call) for n in ast.walk(value)):
        return "function call"
    if any(isinstance(n, ast.Name) for n in ast.walk(value)):
        return "derived from other names"
    return "not a literal"


def _problem(text: str, node: ast.stmt, reason: str) -> str:
    segment = (ast.get_source_segment(text, node) or "").strip().splitlines()
    snippet = segment[0] if segment else ""
    if len(snippet) > 60 or len(segment) > 1:
        snippet = snippet[:57] + "..."
    return f"line {node.lineno}: {reason}: {snippet}"


def parse_parameters(source: str) -> ParametersCell:
    """Collect literal assignments; report every other statement as a problem."""
    cell = ParametersCell()
    text = _clean(source)
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        cell.problems.append(f"line {exc.lineno}: syntax error: {exc.msg}")
        return cell

    for node in tree.body:
        target = _target(node)
        if target is None:
            reason = _KIND.get(type(node), type(node).__name__)
            cell.problems.append(_problem(text, node, reason))
            continue
        name, value = target
        cell.assigned.add(name)
        try:
            cell.values[name] = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            cell.problems.append(_problem(text, node, _explain(value)))
    return cell


def read_notebook(path: str | Path) -> NotebookInfo:
    path = Path(path)
    if not path.exists():
        raise NotebookError(f"notebook not found: {path}")
    if path.suffix != ".ipynb":
        raise NotebookError(f"not a notebook (.ipynb): {path}")
    try:
        nb = nbformat.read(path, as_version=4)
    except Exception as exc:
        raise NotebookError(f"cannot read {path}: {exc}") from exc

    code_cells = [c.source for c in nb.cells if c.cell_type == "code"]
    tagged = [
        i
        for i, c in enumerate(nb.cells)
        if c.cell_type == "code" and PARAMETERS_TAG in c.metadata.get("tags", [])
    ]
    if not tagged:
        params = ParametersCell()
    else:
        params = parse_parameters(nb.cells[tagged[0]].source)
        params.cell_index = tagged[0]
        if len(tagged) > 1:
            params.problems.append(
                f"{len(tagged)} cells are tagged '{PARAMETERS_TAG}'; keep exactly one"
            )
    return NotebookInfo(path=path, code_cells=code_cells, parameters=params)