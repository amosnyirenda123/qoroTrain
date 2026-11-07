"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/config/notebook.py

Description:
    Read a notebook without running it.

    Everything here works on source text with ``ast``: no cell is ever executed.

    Qorotrain injects parameters with Papermill, which appends a cell of
    assignments after the cell tagged ``parameters``. Injection only works if
    that tagged cell contains nothing but plain literal assignments: anything
    derived (``hidden = 2 * width``) would be computed from the *old* value.
    :func:`parse_parameters` therefore extracts the literal values and reports
    every other statement as a problem, with its line number.

    The module also provides cheap static checks used by
    :mod:`qorotrain.config.validate`: whether the notebook imports
    ``qorotrain`` and which metric names it passes to ``.log(...)`` calls.

    IPython syntax:
        Lines starting with ``%`` or ``!`` (magics and shell escapes) are
        blanked before parsing, keeping line numbers aligned with the cell.

===============================================================================
"""
import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import nbformat

PARAMETERS_TAG = "parameters"
"""Papermill's cell tag marking the cell whose assignments may be overridden."""

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
    """What was found in the cell tagged ``parameters``.

    Attributes:
        cell_index (int | None): Index of the tagged cell among *all* cells
            (markdown included), or ``None`` if no cell is tagged.
        values (dict[str, Any]): Name to literal value, for every assignment
            whose right-hand side ``ast.literal_eval`` accepts.
        assigned (set[str]): Every simple name assigned in the cell, literal
            or not.
        problems (list[str]): One ``"line N: reason: code"`` entry per
            statement that is not a plain literal assignment.
    """

    cell_index: int | None = None
    values: dict[str, Any] = field(default_factory=dict)  # name -> literal value
    assigned: set[str] = field(default_factory=set)  # every name assigned in the cell
    problems: list[str] = field(default_factory=list)  # "line N: reason: code"

    @property
    def found(self) -> bool:
        """``True`` if the notebook has a cell tagged ``parameters``."""
        return self.cell_index is not None


@dataclass
class NotebookInfo:
    """Static summary of a notebook.

    Attributes:
        path (pathlib.Path): The notebook file.
        code_cells (list[str]): Source of every code cell, in order.
        parameters (ParametersCell): Analysis of the ``parameters`` cell.
        language (str | None): Programming language of the notebook, read
            from its kernel metadata the way Papermill reads it (see
            :func:`notebook_language`). ``None`` when the metadata does not
            name one, in which case Papermill cannot inject parameters.
    """

    path: Path
    code_cells: list[str]
    parameters: ParametersCell
    language: str | None = None

    def imports_qorotrain(self) -> bool:
        """Tell whether any code cell starts a line with ``import qorotrain`` or ``from qorotrain``.

        Returns:
            ``True`` if such an import is found (a regular-expression check,
            not a full parse).
        """
        return any(_IMPORT_QOROTRAIN.search(src) for src in self.code_cells)

    def logged_metrics(self) -> set[str]:
        """Keyword names passed to any ``<obj>.log(...)`` call (``step`` excluded).

        Cells that do not parse are skipped. The check is purely syntactic:
        any method named ``log`` counts, and metrics passed through
        ``**kwargs`` are invisible.

        Returns:
            The metric names found.
        """
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
    """Say why a right-hand side is not a literal: a call, a reference to other names, or other."""
    if any(isinstance(n, ast.Call) for n in ast.walk(value)):
        return "function call"
    if any(isinstance(n, ast.Name) for n in ast.walk(value)):
        return "derived from other names"
    return "not a literal"


def _problem(text: str, node: ast.stmt, reason: str) -> str:
    """Format a problem as ``line N: reason: snippet``, truncating long or multi-line code to 60 characters."""
    segment = (ast.get_source_segment(text, node) or "").strip().splitlines()
    snippet = segment[0] if segment else ""
    if len(snippet) > 60 or len(segment) > 1:
        snippet = snippet[:57] + "..."
    return f"line {node.lineno}: {reason}: {snippet}"


def parse_parameters(source: str) -> ParametersCell:
    """Collect literal assignments; report every other statement as a problem.

    Only top-level statements are examined. A syntax error is reported as a
    single problem rather than raised.

    Args:
        source: Source code of the ``parameters`` cell.

    Returns:
        The analysis, with ``cell_index`` left as ``None`` (the caller sets
        it).
    """
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
    """Load a notebook file and analyse it statically.

    If several cells are tagged ``parameters``, the first is analysed and an
    extra problem is recorded (Papermill's behaviour with several tagged cells
    is not what users expect).

    Args:
        path: Path of an ``.ipynb`` file.

    Returns:
        The notebook summary.

    Raises:
        NotebookError: If the file does not exist, does not have the
            ``.ipynb`` suffix, or cannot be parsed by ``nbformat``.
    """
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
    return NotebookInfo(path=path, code_cells=code_cells, parameters=params, language=notebook_language(nb.metadata))


def notebook_language(metadata: Any) -> str | None:
    """The notebook's programming language, as Papermill determines it.

    Papermill (``papermill.utils.nb_language``) uses
    ``metadata.language_info.name`` and falls back to
    ``metadata.kernelspec.language`` for older notebooks; it needs a language to
    choose the translator that writes the injected parameters cell. The same
    order is used here so that validation predicts what Papermill will do.

    Malformed metadata (a section that is not a mapping, a name that is not a
    non-empty string) is treated as missing rather than raising.

    Args:
        metadata: The notebook-level metadata mapping.

    Returns:
        The language name (for example ``"python"``), or ``None`` if the
        metadata does not provide one.
    """
    if not isinstance(metadata, dict):
        return None
    for section, key in (("language_info", "name"), ("kernelspec", "language")):
        value = metadata.get(section)
        name = value.get(key) if isinstance(value, dict) else None
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None
