"""Enforce the I/O boundary of the numerical core.

This is the one test in the suite that is about structure rather than numbers,
and it is the reason the boundary survives contact with a deadline: it parses
every module in ``tes_pricer.math`` with ``ast`` and fails if any of them so
much as imports a network client. It uses the standard library only, so it runs
on a bare checkout.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

FORBIDDEN_MODULES = frozenset(
    {
        "requests",
        "sodapy",
        "urllib",
        "urllib3",
        "http",
        "httpx",
        "aiohttp",
        "socket",
        "ftplib",
        "tes_pricer.data",
    }
)

FORBIDDEN_CALL_PATTERNS = (
    "read_csv",
    "read_json",
    "read_html",
    "read_excel",
    "urlopen",
)


def _math_modules(math_package_dir: Path) -> list[Path]:
    return sorted(p for p in math_package_dir.glob("*.py") if p.name != "__init__.py")


def _imported_module_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


@pytest.mark.unit
def test_math_package_has_modules(math_package_dir: Path) -> None:
    """Guard against the boundary test silently passing on an empty package."""
    assert _math_modules(math_package_dir), "no modules found in tes_pricer.math"


@pytest.mark.unit
def test_math_modules_do_not_import_network_clients(math_package_dir: Path) -> None:
    """No module in `math/` may import a network client or the data layer."""
    offenders: list[str] = []
    for module_path in _math_modules(math_package_dir):
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for imported in _imported_module_names(tree):
            root = imported.split(".")[0]
            if root in FORBIDDEN_MODULES or imported in FORBIDDEN_MODULES:
                offenders.append(f"{module_path.name} imports {imported}")
    assert not offenders, "network imports leaked into the numerical core: " + "; ".join(offenders)


@pytest.mark.unit
def test_math_modules_do_not_read_remote_data(math_package_dir: Path) -> None:
    """No module in `math/` may call a reader that can take a URL."""
    offenders: list[str] = []
    for module_path in _math_modules(math_package_dir):
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in FORBIDDEN_CALL_PATTERNS:
                offenders.append(f"{module_path.name}:{node.lineno} calls {name}")
    assert not offenders, "remote data access in the numerical core: " + "; ".join(offenders)


@pytest.mark.unit
def test_every_math_module_is_documented(math_package_dir: Path) -> None:
    """Each numerical module must carry a module docstring stating its conventions."""
    undocumented = [
        module_path.name
        for module_path in _math_modules(math_package_dir)
        if not ast.get_docstring(ast.parse(module_path.read_text(encoding="utf-8")))
    ]
    assert not undocumented, f"missing module docstrings: {undocumented}"
