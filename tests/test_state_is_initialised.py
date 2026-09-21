"""Every attribute a class assigns must be assigned in `__init__`.

This is here because of a real failure. A change added `_sweep_task`, set it in
`destroy` and read it in `_ensure_sweeping`, but the edit meant to add it to
`__init__` silently matched nothing. Nothing failed at import, nothing failed
in the suite, and the browser threw `AttributeError` on every render inside
Kit, where the only symptom a person sees is that the pictures stop arriving.

An attribute set anywhere but never set at construction is always this bug:
the object has a state some of its methods assume and its constructor does not
establish.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

EXT = Path(__file__).resolve().parents[1] / "exts" / "rundiffusion.omniverse"
PACKAGE = EXT / "rundiffusion_omniverse"

#: The stateful classes. Dataclasses are excluded: their fields are the
#: constructor, written by the decorator.
CLASSES = [
    ("dialogs.py", "ToolBrowser"),
    ("dialogs.py", "AssetPicker"),
    ("asset_grid.py", "AssetGrid"),
    ("panel.py", "RunDiffusionPanel"),
]


def _class(module: str, name: str) -> ast.ClassDef:
    tree = ast.parse((PACKAGE / module).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {module}")


def _self_attributes_assigned(node: ast.AST) -> set[str]:
    found: set[str] = set()
    for child in ast.walk(node):
        targets = []
        if isinstance(child, ast.Assign):
            targets = child.targets
        elif isinstance(child, (ast.AnnAssign, ast.AugAssign)):
            targets = [child.target]
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                found.add(target.attr)
    return found


@pytest.mark.parametrize("module,name", CLASSES, ids=[c[1] for c in CLASSES])
def test_every_attribute_is_established_in_init(module: str, name: str):
    cls = _class(module, name)
    init = next(
        (m for m in cls.body if isinstance(m, ast.FunctionDef) and m.name == "__init__"),
        None,
    )
    assert init is not None, f"{name} has no __init__"

    established = _self_attributes_assigned(init)
    assigned_elsewhere: set[str] = set()
    for member in cls.body:
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if member.name == "__init__":
                continue
            assigned_elsewhere |= _self_attributes_assigned(member)

    missing = sorted(assigned_elsewhere - established)
    assert not missing, (
        f"{name} assigns {missing} outside __init__ without establishing "
        f"it there. Reading one before its first assignment is an "
        f"AttributeError at runtime."
    )
