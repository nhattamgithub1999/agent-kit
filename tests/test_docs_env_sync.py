"""Doc/code sync test for numeric, clamped environment variables.

Every call site in ``hooks/*.py`` that reads a numeric environment variable
with a default and a min/max clamp must have its exact name, default, and
both bounds documented in the ``README.md`` environment variable table. This
is discovered by parsing the real source with ``ast`` and resolving the
literal module-level constants involved, not by re-typing the values by
hand; a call site whose arguments cannot be statically resolved is a test
failure, not something to silently skip.
"""

from __future__ import annotations

import ast
import pathlib
import unittest
from typing import Dict, List, Tuple

from tests.support import REPO_ROOT


HOOKS_DIR = REPO_ROOT / "hooks"
README_PATH = REPO_ROOT / "README.md"


class _UnresolvedLiteral(ValueError):
    """A numeric expression could not be statically resolved."""


def _eval_numeric(node: ast.AST, constants: Dict[str, int]) -> int:
    """Resolve a small subset of numeric expressions: literals, module-level
    constant names, unary minus, +/-/* between resolvable values, and a
    single-argument ``str(...)`` wrapper (used only to build a default
    string for ``os.environ.get``).
    """

    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(
        node.value, bool
    ):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in constants:
            return constants[node.id]
        raise _UnresolvedLiteral("unresolved name {!r}".format(node.id))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval_numeric(node.operand, constants)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
        left = _eval_numeric(node.left, constants)
        right = _eval_numeric(node.right, constants)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        return left * right
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "str"
        and len(node.args) == 1
    ):
        return _eval_numeric(node.args[0], constants)
    raise _UnresolvedLiteral(
        "cannot resolve numeric expression: {}".format(ast.dump(node))
    )


def _module_constants(tree: ast.Module) -> Dict[str, int]:
    """Module-level ``NAME = <numeric expression>`` assignments, resolved in
    source order so a later constant may reference an earlier one exactly as
    Python itself would evaluate them at import time.
    """

    constants: Dict[str, int] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            try:
                constants[node.targets[0].id] = _eval_numeric(node.value, constants)
            except _UnresolvedLiteral:
                continue
    return constants


def _safe_env_int_call_sites(
    tree: ast.Module, constants: Dict[str, int], path: pathlib.Path
) -> List[Tuple[str, int, int, int]]:
    """Every ``safe_env_int(name, default, minimum, maximum)`` call site."""

    found: List[Tuple[str, int, int, int]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "safe_env_int"
        ):
            continue
        if len(node.args) < 4 or not (
            isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
        ):
            raise AssertionError(
                "{}: a safe_env_int call site must use a literal string name and "
                "four positional arguments to be statically resolvable".format(path)
            )
        name = node.args[0].value
        try:
            default = _eval_numeric(node.args[1], constants)
            minimum = _eval_numeric(node.args[2], constants)
            maximum = _eval_numeric(node.args[3], constants)
        except _UnresolvedLiteral as exc:
            raise AssertionError(
                "{}: cannot resolve safe_env_int({!r}, ...) arguments: {}".format(
                    path, name, exc
                )
            ) from exc
        found.append((name, default, minimum, maximum))
    return found


def _bespoke_clamped_env_reads(
    tree: ast.Module, constants: Dict[str, int], path: pathlib.Path
) -> List[Tuple[str, int, int, int]]:
    """Detect the ``os.environ.get(name, default)`` + manual
    ``max(..., min(..., ...))`` clamp shape used outside ``safe_env_int``
    (for example ``route-prompt.py``'s own threshold reader). A plain
    ``os.environ.get`` with no clamp call in the same function is a
    string/boolean toggle, not a numeric env var, and is out of scope here.
    """

    found: List[Tuple[str, int, int, int]] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        env_name = None
        default_node = None
        for call in ast.walk(func):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "get"
                and isinstance(call.func.value, ast.Attribute)
                and call.func.value.attr == "environ"
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)
            ):
                env_name = call.args[0].value
                default_node = call.args[1] if len(call.args) > 1 else None
                break
        if env_name is None:
            continue

        bounds = set()
        has_clamp_marker = False
        for call in ast.walk(func):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id in ("max", "min")
            ):
                has_clamp_marker = True
                for arg in call.args:
                    if isinstance(arg, ast.Name) and arg.id in constants:
                        bounds.add(constants[arg.id])
        if not has_clamp_marker:
            continue
        if len(bounds) != 2 or default_node is None:
            raise AssertionError(
                "{}: found a manual max/min clamp on env var {!r} but could not "
                "resolve exactly two module-level constant bounds and a "
                "default; refusing to silently skip it".format(path, env_name)
            )
        try:
            default = _eval_numeric(default_node, constants)
        except _UnresolvedLiteral as exc:
            raise AssertionError(
                "{}: cannot resolve default for env var {!r}: {}".format(
                    path, env_name, exc
                )
            ) from exc
        found.append((env_name, default, min(bounds), max(bounds)))
    return found


def _readme_env_table_rows() -> Dict[str, Tuple[str, str]]:
    """Parse the `Biến môi trường đang hỗ trợ` Markdown table into
    ``name -> (default_cell, effect_cell)``.
    """

    lines = README_PATH.read_text(encoding="utf-8").splitlines()
    rows: Dict[str, Tuple[str, str]] = {}
    in_table = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("| Biến"):
            in_table = True
            continue
        if not in_table:
            continue
        if not stripped.startswith("|"):
            break
        if set(stripped.replace("|", "").strip()) <= {"-", ":", " "}:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 3:
            continue
        name = cells[0].strip("`")
        rows[name] = (cells[1], cells[2])
    return rows


class DocsEnvSyncTests(unittest.TestCase):
    def test_every_numeric_env_var_default_and_bounds_are_documented(self) -> None:
        readme_rows = _readme_env_table_rows()
        self.assertTrue(
            readme_rows, "could not parse any row from the README env var table"
        )

        checked = 0
        for path in sorted(HOOKS_DIR.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            constants = _module_constants(tree)
            call_sites = _safe_env_int_call_sites(tree, constants, path)
            call_sites += _bespoke_clamped_env_reads(tree, constants, path)
            for name, default, minimum, maximum in call_sites:
                checked += 1
                with self.subTest(path=str(path.relative_to(REPO_ROOT)), env_var=name):
                    self.assertIn(
                        name,
                        readme_rows,
                        "{} is read in {} with a numeric default/min/max clamp "
                        "but has no row in the README.md env var table".format(
                            name, path
                        ),
                    )
                    default_cell, effect_cell = readme_rows[name]
                    self.assertIn(
                        str(default),
                        default_cell,
                        "{}: README default column {!r} does not mention the "
                        "real default {!r} resolved from {}".format(
                            name, default_cell, default, path
                        ),
                    )
                    self.assertIn(
                        str(minimum),
                        effect_cell,
                        "{}: README effect column {!r} does not mention the "
                        "real minimum bound {!r} resolved from {}".format(
                            name, effect_cell, minimum, path
                        ),
                    )
                    self.assertIn(
                        str(maximum),
                        effect_cell,
                        "{}: README effect column {!r} does not mention the "
                        "real maximum bound {!r} resolved from {}".format(
                            name, effect_cell, maximum, path
                        ),
                    )
        self.assertGreater(
            checked, 0, "no numeric, clamped env var call sites were found at all"
        )


if __name__ == "__main__":
    unittest.main()
