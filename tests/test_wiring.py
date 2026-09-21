"""Static checks on the UI modules, for the bugs a headless load cannot catch.

The UI needs a running Kit app, so these files are not exercised by the rest of
the suite. That left a real gap: a method referenced but never defined is
invisible until the branch that calls it runs, and `_render_session_section`
went missing in an edit and only surfaced when a person clicked the Library tab.

These read the parsed source instead of running it, which catches that class of
mistake without a GPU. They are deliberately narrow: they check that the wiring
exists, not that it does anything.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

MODULE_DIR = (
    Path(__file__).resolve().parents[1]
    / "exts"
    / "rundiffusion.omniverse"
    / "rundiffusion_omniverse"
)

UI_MODULES = [
    "panel.py",
    "form.py",
    "dialogs.py",
    "asset_grid.py",
    "compare.py",
    "viewport_preview.py",
]


def _parse(name: str) -> ast.Module:
    return ast.parse((MODULE_DIR / name).read_text(encoding="utf-8"))


def _classes(tree: ast.Module) -> list[ast.ClassDef]:
    return [node for node in tree.body if isinstance(node, ast.ClassDef)]


def _self_attributes(node: ast.AST) -> set[str]:
    return {
        child.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Attribute)
        and isinstance(child.value, ast.Name)
        and child.value.id == "self"
    }


def _assigned_attributes(cls: ast.ClassDef) -> set[str]:
    assigned = set()
    for node in ast.walk(cls):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    assigned.add(target.attr)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Attribute):
            assigned.add(node.target.attr)
    return assigned


@pytest.mark.parametrize("module_name", UI_MODULES)
def test_every_self_reference_resolves(module_name: str):
    """No method or attribute is referenced that the class never defines.

    This is the check that would have caught the missing tab builder before a
    person did.
    """
    tree = _parse(module_name)
    for cls in _classes(tree):
        defined = {
            member.name
            for member in cls.body
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        known = defined | _assigned_attributes(cls)
        dangling = sorted(
            attr
            for attr in _self_attributes(cls)
            if attr.startswith("_") and not attr.startswith("__") and attr not in known
        )
        assert not dangling, f"{module_name}:{cls.name} references {dangling}"


@pytest.mark.parametrize("module_name", UI_MODULES)
def test_no_methods_stranded_outside_their_class(module_name: str):
    """Catch the append-edit failure that hides methods inside another function.

    Two AssetGrid methods once landed after a module-level function and were
    swallowed into its body as dead code following the return. It imported
    cleanly and the avatars simply never loaded.
    """
    tree = _parse(module_name)
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nested = [
            child.name
            for child in node.body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            and child.args.args
            and child.args.args[0].arg == "self"
        ]
        assert not nested, (
            f"{module_name}: {nested} take `self` but are nested inside "
            f"the module-level function {node.name}"
        )


def test_every_tab_has_a_builder():
    """Each tab constant is reachable, so no tab can crash on being clicked."""
    tree = _parse("panel.py")
    panel = next(cls for cls in _classes(tree) if cls.name == "RunDiffusionPanel")
    defined = {
        member.name
        for member in panel.body
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    for builder in (
        "_build_create_tab",
        "_build_library_tab",
        "_build_uploads_tab",
        "_build_account_tab",
    ):
        assert builder in defined, f"panel.py is missing {builder}"


#: The panel's collaborators: the attribute it holds each one under, and the
#: class that attribute is. Kept explicit rather than inferred, because guessing
#: a type from a name is how a guard starts passing for the wrong reason.
COLLABORATORS = {
    "_form": ("form.py", "ToolForm"),
    "_viewport_preview": ("viewport_preview.py", "ViewportPreview"),
    "_grid": ("asset_grid.py", "AssetGrid"),
    "_compare": ("compare.py", "CompareWindow"),
    "_asset_picker": ("dialogs.py", "AssetPicker"),
    "_viewer": ("dialogs.py", "ImageViewer"),
    "_tool_browser": ("dialogs.py", "ToolBrowser"),
}


def _members(module_name: str, class_name: str) -> set[str]:
    tree = _parse(module_name)
    cls = next(c for c in _classes(tree) if c.name == class_name)
    members = {
        member.name
        for member in cls.body
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    return members | _assigned_attributes(cls) | {
        node.target.id
        for node in cls.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }


@pytest.mark.parametrize("attribute", sorted(COLLABORATORS))
def test_the_panel_only_calls_methods_its_collaborators_have(attribute: str):
    """Catch a call to a method that was renamed on the other side.

    The self-reference guard above only sees one class at a time, so
    `self._form.refresh_previews()` survived that method being renamed and would
    have thrown the first time a library image was chosen. Nothing in the suite
    clicks that button, and a headless load never reaches the branch.
    """
    module_name, class_name = COLLABORATORS[attribute]
    available = _members(module_name, class_name)

    tree = _parse("panel.py")
    used = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == attribute
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "self"
    }

    missing = sorted(name for name in used if name not in available)
    assert not missing, f"panel.py calls {missing} on {class_name}, which lacks them"


def _collapsable_attributes(tree: ast.Module) -> set[str]:
    """`self.<attr>` assigned from a ui.CollapsableFrame(...) call."""
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "CollapsableFrame"
        ):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                found.add(target.attr)
    return found


def _cleared_attributes(tree: ast.Module) -> set[str]:
    """Which `self.<attr>` frames get `.clear()`ed, directly or through a local.

    The local matters and was the first version's blind spot: the renderer that
    actually had this bug reads `frame = self._session_frame` and then calls
    `frame.clear()`, so a check that only understood `self.x.clear()` passed
    while looking at the defect. Aliases are resolved per function, which is as
    far as the pattern ever goes here.
    """
    found = set()
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        aliases = {}
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "self"
            ):
                aliases[node.targets[0].id] = node.value.attr
        for node in ast.walk(function):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "clear"
            ):
                continue
            target = node.func.value
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                found.add(target.attr)
            elif isinstance(target, ast.Name) and target.id in aliases:
                found.add(aliases[target.id])
    return found


@pytest.mark.parametrize("module_name", UI_MODULES)
def test_no_collapsable_frame_is_rebuilt_in_place(module_name: str):
    """A CollapsableFrame is built once and filled once. Never redrawn.

    It owns a header and a body and constructs them with itself, so re-entering
    one later from another method to replace its child does not draw: the
    Library tab's This session box came up empty every time, while the identical
    pattern on a plain Frame works everywhere else in the panel.

    The fix is an indirection rather than a workaround, so this is the guard
    that keeps it: put a plain Frame inside the collapsable one and rebuild
    that. This fails the moment someone clears a collapsable frame directly.
    """
    tree = _parse(module_name)
    rebuilt = _collapsable_attributes(tree) & _cleared_attributes(tree)

    assert not rebuilt, (
        f"{module_name}: {sorted(rebuilt)} is a CollapsableFrame being rebuilt "
        "in place. Put a plain Frame inside it and rebuild that instead."
    )


#: Frames the Create tab owns, which a background job renders into whenever a
#: run changes state. Switching tabs destroys the widgets, so the panel must let
#: go of them: a destroyed frame still answers, and writing into one is how a
#: finished run painted into nothing.
TAB_OWNED_FRAMES = (
    "_results_frame",
    "_jobs_frame",
    "_cost_label",
)

#: The same rule for the frames a BROWSING SURFACE owns. They are dropped in
#: their own method rather than inline, because either surface can be torn out
#: into a window of its own and a tab switch then destroys nothing of its. Same
#: hazard either way: the session box is rendered into by every job state
#: change.
#:
#: The last two are kept per surface, so they are released with a pop rather
#: than by being set to None. Both matter: the action row is a destroyed
#: widget, and the selection is what that row would be rebuilt to show, so
#: leaving it behind means the surface comes back with an asset selected that
#: is not on screen.
SURFACE_OWNED_FRAMES = (
    "_session_frame",
    "_session_body",
    "_library_frame",
    "_library_pager",
    "_filters_frame",
    "_filters_body",
    "_uploads_frame",
    "_uploads_pager",
    "_asset_actions_frame",
    "_selected_asset",
)


#: The two ends of carrying a Create-tab choice across a rebuild, as
#: (method, call) pairs.
#:
#: Neither of these lives in a control that `collect` can read back, so neither
#: survives the widgets being torn down on its own. The image state was carried
#: from the start; the capture source was not, so a field set to a named camera
#: came back on the active viewport with no message and the next capture
#: recorded whatever the user happened to be orbiting.
CARRIED_ACROSS_A_REBUILD = (
    ("_show_tab", "export_image_state"),
    ("_show_tab", "export_capture_sources"),
    ("_build_create_tab", "restore_image_state"),
    ("_build_create_tab", "restore_capture_sources"),
)


#: Each browsing surface, as (tab builder, body builder, surface name).
#:
#: The tab and the window build the SAME body, and the tab is the only one of
#: the two that has to check whether the surface is somewhere else. Getting
#: that check wrong does not crash: the tab simply builds a second copy of a
#: surface that is already live in a window, and the two then fight over the
#: single set of frame attributes they both write to.
SURFACE_TAB_BUILDERS = (
    ("_build_library_tab", "_build_library_body"),
    ("_build_uploads_tab", "_build_uploads_body"),
)


def _method(class_name: str, method_name: str, module: str = "panel.py"):
    tree = _parse(module)
    owner = next(cls for cls in _classes(tree) if cls.name == class_name)
    return next(
        member
        for member in owner.body
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
        and member.name == method_name
    )


def _dropped_in(method) -> set:
    """Attributes the method assigns None to, or pops off a dict on self.

    Both halves are needed, and only the first half was here. A frame the panel
    holds ONE of is let go with `self._x = None`; a frame it holds one of per
    surface is let go with `self._x.pop(surface, None)`, and the library window
    made several of those. Collecting only the assignments meant the pops could
    not be pinned by the guards below at all: naming one in the tuples would
    have failed the test rather than protecting it, so the drops that matter
    most to a library living in two places were the ones going unguarded.
    """
    dropped = {
        target.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and node.value.value is None
        for target in node.targets
        if isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    }
    # `self._frames.pop(key, None)` names `_frames`, which is the attribute the
    # guard is about: the dict is the thing holding a destroyed widget.
    dropped |= {
        node.func.value.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "pop"
        and isinstance(node.func.value, ast.Attribute)
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "self"
    }
    return dropped


@pytest.mark.parametrize("attribute", TAB_OWNED_FRAMES)
def test_switching_tabs_lets_go_of_the_widgets_it_destroys(attribute: str):
    assert attribute in _dropped_in(_method("RunDiffusionPanel", "_show_tab")), (
        f"_show_tab does not drop {attribute}"
    )


@pytest.mark.parametrize("attribute", SURFACE_OWNED_FRAMES)
def test_a_surface_lets_go_of_its_own_widgets(attribute: str):
    assert attribute in _dropped_in(
        _method("RunDiffusionPanel", "_drop_surface_frames")
    ), f"_drop_surface_frames does not drop {attribute}"


@pytest.mark.parametrize(("method_name", "call"), CARRIED_ACROSS_A_REBUILD)
def test_the_create_tab_carries_its_choices_across_a_rebuild(
    method_name: str, call: str
):
    calls = {
        node.func.attr
        for node in ast.walk(_method("RunDiffusionPanel", method_name))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert call in calls, f"{method_name} does not call {call}"


def _calls_in(method) -> set:
    """Names of the methods called on `self` anywhere in this one."""
    return {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


@pytest.mark.parametrize(("builder", "body"), SURFACE_TAB_BUILDERS)
def test_a_tab_says_where_its_surface_went(builder: str, body: str):
    """Torn out, the tab must show the note rather than a second copy of the
    surface. The tab stays in the strip either way: a tab that comes and goes
    moves the three beside it, and someone who tore a surface out and forgot
    has to be told where it went by something."""
    method = _method("RunDiffusionPanel", builder)

    assert "_torn_out" in _self_attributes(method), (
        f"{builder} never asks whether its surface is torn out"
    )
    calls = _calls_in(method)
    assert "_build_elsewhere_notice" in calls, (
        f"{builder} has no note to show while its surface is elsewhere"
    )
    assert body in calls, f"{builder} never builds {body}"


@pytest.mark.parametrize(("_builder", "body"), SURFACE_TAB_BUILDERS)
def test_a_surface_body_offers_to_pop_itself_out(_builder: str, body: str):
    """From the tab only. In the window the title bar's X is the same request
    and a button repeating it is clutter, which is what `in_window` decides."""
    method = _method("RunDiffusionPanel", body)

    assert "_build_pop_out_button" in _calls_in(method), (
        f"{body} offers no way to pop the surface out"
    )


def test_the_pop_out_button_is_built_in_exactly_one_place():
    """Two surfaces, one button. The label, the icon, the height and the
    deferral are decided together or they drift apart, and a second copy is
    how one surface quietly ends up with last month's version of it."""
    tree = _parse("panel.py")
    built = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_build_pop_out_button"
    ]

    assert len(built) == len(SURFACE_TAB_BUILDERS), (
        "every surface body calls _build_pop_out_button, and nothing else does"
    )
    assert len(
        [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_build_pop_out_button"
        ]
    ) == 1


def test_the_pop_out_button_carries_its_icon():
    """`open-in-new` is the picture every application in the room already uses
    for this, so the words are not carrying the verb alone.

    The keys are checked as well as the constant. Naming the asset somewhere in
    the method is not the same as handing it to the button under the name
    omni.ui reads it by, and the difference between those two is a button that
    silently has no icon.
    """
    method = _method("RunDiffusionPanel", "_build_pop_out_button")
    names = {node.id for node in ast.walk(method) if isinstance(node, ast.Name)}
    keys = {
        node.value
        for node in ast.walk(method)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    assert "OPEN_IN_NEW_ICON" in names, (
        "_build_pop_out_button no longer references the icon"
    )
    assert "ICON_SIZE" in names, "the icon is drawn at no particular size"
    for key in ("image_url", "image_width", "image_height"):
        assert key in keys, f"the icon is never handed over as {key}"


def test_the_icon_sits_beside_the_words_rather_than_above_them():
    """omni.ui stacks a button's image and its text TOP_TO_BOTTOM by default.
    The first version of this shipped without saying otherwise, and the icon
    landed on its own line above the label: a two-line button with a picture
    stranded over it. Reported off the branch."""
    method = _method("RunDiffusionPanel", "_build_pop_out_button")
    keys = {
        node.value
        for node in ast.walk(method)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    directions = {
        node.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Attribute)
    }

    assert "stack_direction" in keys, (
        "the button never says which way to stack its image and its text"
    )
    assert "LEFT_TO_RIGHT" in directions, (
        "the button stacks its image and its text some way other than beside"
    )


def test_dropping_what_a_surface_holds_goes_on_to_show_it_again():
    """`_forget_uploads` and `_forget_browsing` empty what a surface is
    listing; neither redraws anything. Every caller has to follow with
    `_rebuild_surface`, or the surface goes on showing a list that is no longer
    true. Rebuilding the TAB is not enough: a window is never rebuilt by a
    visit."""
    for method_name in ("_send_to_rundiffusion", "_on_account_changed"):
        method = _method("RunDiffusionPanel", method_name)
        assert "_rebuild_surface" in _calls_in(method), (
            f"{method_name} drops held pages without showing the surface again"
        )


def test_the_window_can_build_either_surface():
    """One window mechanism, two surfaces. A dispatch that forgot one would
    give that surface a window that opens empty."""
    calls = _calls_in(_method("RunDiffusionPanel", "_build_surface_body"))

    for _builder, body in SURFACE_TAB_BUILDERS:
        assert body in calls, f"_build_surface_body never builds {body}"


def test_a_tab_switch_drops_only_the_surfaces_it_destroys():
    """Torn out, a surface is in a window a tab switch does not touch, and
    dropping its frames there would leave a live window nothing can draw into.
    Docked, its widgets die with the tab body like everything else.

    `_docked_surfaces` is what draws that line, so the drop has to be driven by
    it rather than run over every surface there is.
    """
    show_tab = _method("RunDiffusionPanel", "_show_tab")
    loops = [
        node
        for node in ast.walk(show_tab)
        if isinstance(node, ast.For)
        and any(
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "_drop_surface_frames"
            for inner in ast.walk(node)
        )
    ]

    assert loops, "_show_tab never drops a surface's frames"
    assert any(
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Attribute)
        and inner.func.attr == "_docked_surfaces"
        for loop in loops
        for inner in ast.walk(loop.iter)
    ), "_show_tab drops surface frames without asking which are docked"


def test_the_tab_switch_spares_a_torn_out_surfaces_redraws():
    """A rebuild booked by a surface in its own window is still looking at
    something. Cancelling it would leave the window showing whatever it had
    before the click that got here."""
    show_tab = _method("RunDiffusionPanel", "_show_tab")
    cancels = [
        node
        for node in ast.walk(show_tab)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "cancel"
    ]

    assert cancels, "_show_tab never cancels pending redraws"
    assert any(
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Attribute)
        and inner.func.attr == "_torn_out_prefixes"
        for call in cancels
        for inner in ast.walk(call)
    ), "_show_tab cancels redraws without sparing the torn-out surfaces"
