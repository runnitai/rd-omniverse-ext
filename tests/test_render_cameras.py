"""Keeping the view a capture was taken from, and getting back to it.

A capture from a named camera was always reproducible. A capture from the
active viewport was not: it was wherever the orbit had stopped, and the moment
the user moved, the framing behind that image was gone.

What this covers is the part that touches a stage somebody else owns. Nothing
is authored unless it was asked for; what is authored goes under one parent and
through a command so Ctrl+Z takes it back; and the way back refuses rather than
going quiet when the camera has been deleted since the run.

Writing the transform and the lens onto the new prim needs real USD, so it is
stubbed here and covered by the manual pass. What these tests hold is the part
that was wrong in the first build: the camera has to be BUILT in the file the
user is working in, not copied from `/OmniverseKit_Persp`, which lives in the
session layer and took every pinned view down there with it. Invisible in the
outliner, drawn in the viewport, gone on save.
"""

from __future__ import annotations

import sys
import types

import pytest

from rundiffusion_omniverse import render_cameras, stage as stage_module
from rundiffusion_omniverse.form import cameras_in_image_state
from rundiffusion_omniverse.render_cameras import (
    RENDER_CAMERA_ROOT,
    camera_label,
    look_through,
    save_active_view,
)

VIEWPORT_CAMERA = "/OmniverseKit_Persp"


class FakePrim:
    def __init__(self, valid: bool = True) -> None:
        self._valid = valid
        self._path = ""

    def IsValid(self) -> bool:  # noqa: N802 - the USD spelling
        return self._valid

    def GetPath(self):  # noqa: N802 - the USD spelling
        return types.SimpleNamespace(pathString=self._path)


class FakeLayer:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeEditTarget:
    def __init__(self, layer: FakeLayer) -> None:
        self._layer = layer

    def GetLayer(self):  # noqa: N802 - the USD spelling
        return self._layer


class FakeStage:
    def __init__(self, paths: set[str]) -> None:
        self.paths = set(paths)
        self.session_layer = FakeLayer("session")
        self.root_layer = FakeLayer("root")
        self.edit_target = FakeEditTarget(self.root_layer)
        self.edit_targets_set: list = []

    def GetSessionLayer(self):  # noqa: N802 - the USD spelling
        return self.session_layer

    def GetRootLayer(self):  # noqa: N802 - the USD spelling
        return self.root_layer

    def GetEditTarget(self):  # noqa: N802 - the USD spelling
        return self.edit_target

    def SetEditTarget(self, target):  # noqa: N802 - the USD spelling
        self.edit_targets_set.append(target)
        self.edit_target = target

    def GetPrimAtPath(self, path):  # noqa: N802 - the USD spelling
        return FakePrim() if str(path) in self.paths else None

    def Traverse(self):  # noqa: N802 - the USD spelling
        prims = []
        for path in sorted(self.paths):
            prim = FakePrim()
            prim._path = path
            prims.append(prim)
        return prims


class FakeViewport:
    def __init__(self, camera: str = VIEWPORT_CAMERA) -> None:
        self.camera_path = camera


@pytest.fixture
def kit(monkeypatch):
    """A stage, a viewport, and a record of every command run against them."""
    state = types.SimpleNamespace(
        stage=FakeStage({VIEWPORT_CAMERA, "/World"}),
        viewport=FakeViewport(),
        commands=[],
        refuse=set(),
    )

    usd = types.ModuleType("omni.usd")
    usd.get_context = lambda *_a, **_k: types.SimpleNamespace(
        get_stage=lambda: state.stage
    )
    monkeypatch.setitem(sys.modules, "omni.usd", usd)
    monkeypatch.setattr(sys.modules["omni"], "usd", usd, raising=False)

    viewport_utility = types.ModuleType("omni.kit.viewport.utility")
    viewport_utility.get_active_viewport = lambda *_a, **_k: state.viewport
    kit_package = types.ModuleType("omni.kit")
    viewport_package = types.ModuleType("omni.kit.viewport")
    viewport_package.utility = viewport_utility
    kit_package.viewport = viewport_package
    monkeypatch.setitem(sys.modules, "omni.kit", kit_package)
    monkeypatch.setitem(sys.modules, "omni.kit.viewport", viewport_package)
    monkeypatch.setitem(sys.modules, "omni.kit.viewport.utility", viewport_utility)

    def run_command(name: str, **kwargs) -> bool:
        state.commands.append((name, kwargs))
        if name in state.refuse:
            return False
        # A command that worked put something on the stage, which is what the
        # verification after the copy goes looking for.
        if name == "CreatePrimCommand":
            state.stage.paths.add(kwargs["prim_path"])
        if name == "CopyPrim":
            state.stage.paths.add(kwargs["path_to"])
        return True

    monkeypatch.setattr(stage_module, "run_command", run_command)

    # Writing a transform and a lens onto a prim needs real USD. What this
    # stub keeps is the question the tests are about: which camera was pinned,
    # and where it was put.
    state.authored = []

    def author_camera(stage, source_path, target_path) -> bool:
        state.authored.append((source_path, target_path))
        return not state.refuse_authoring

    state.refuse_authoring = False
    monkeypatch.setattr(render_cameras, "_author_camera", author_camera)

    # pxr is not importable in this process, so the edit-target swap is
    # exercised through its own seam rather than through Usd.EditTarget.
    monkeypatch.setattr(
        render_cameras,
        "_authoring_layer",
        lambda stage: _swap_session_target(stage),
    )
    return state


def _swap_session_target(stage):
    """What `_authoring_layer` does, without pxr: session layer out, root in."""
    target = stage.GetEditTarget()
    if target.GetLayer() is not stage.GetSessionLayer():
        return None
    stage.SetEditTarget(FakeEditTarget(stage.GetRootLayer()))
    return target


class TestNamingACamera:
    def test_a_camera_is_named_by_its_own_name(self):
        """The path is what points a viewport; the name is what goes in a
        sentence somebody reads."""
        assert camera_label("/World/Cameras/Lobby") == "Lobby"
        assert camera_label(f"{RENDER_CAMERA_ROOT}/View_2") == "View_2"

    def test_nothing_is_named_nothing(self):
        assert camera_label(None) == ""


class TestPinningTheView:
    def test_it_takes_the_framing_from_the_camera_the_viewport_is_on(self, kit):
        save_active_view()

        assert kit.authored == [(VIEWPORT_CAMERA, f"{RENDER_CAMERA_ROOT}/View")]

    def test_the_camera_is_built_rather_than_copied(self, kit):
        """`/OmniverseKit_Persp` lives in the session layer, and copying a prim
        spec copies it where it already is. Every pinned view landed in the
        session layer: absent from the outliner, drawn in the viewport, and
        gone on save."""
        path = save_active_view()

        assert "CopyPrim" not in [name for name, _kwargs in kit.commands]
        created = next(
            kwargs
            for name, kwargs in kit.commands
            if name == "CreatePrimCommand" and kwargs["prim_path"] == path
        )
        assert created["prim_type"] == "Camera"

    def test_it_lands_under_the_plugin_s_own_parent(self, kit):
        """So everything this plugin put on a stage can be found together and
        removed in one action."""
        assert save_active_view() == f"{RENDER_CAMERA_ROOT}/View"

    def test_a_second_view_does_not_land_on_the_first(self, kit):
        first = save_active_view()
        second = save_active_view()

        assert first != second
        assert second == f"{RENDER_CAMERA_ROOT}/View_2"

    def test_a_name_usd_would_refuse_is_made_legal(self, kit):
        assert save_active_view("north-west entry") == (
            f"{RENDER_CAMERA_ROOT}/north_west_entry"
        )

    def test_the_parent_scopes_are_made_before_the_camera(self, kit):
        save_active_view()

        made = [
            kwargs["prim_path"]
            for name, kwargs in kit.commands
            if name == "CreatePrimCommand"
        ]
        assert made == [
            "/RunDiffusion",
            RENDER_CAMERA_ROOT,
            f"{RENDER_CAMERA_ROOT}/View",
        ]

    def test_it_leaves_the_user_s_selection_alone(self, kit):
        """They are in the middle of building a run, not looking at the
        outliner."""
        save_active_view()

        created = kit.commands[-1][1]
        assert created["select_new_prim"] is False

    def test_a_session_layer_edit_target_is_swapped_for_the_root(self, kit):
        """The session layer is not saved, so a camera authored into it is
        gone the next time the stage is opened. Pinning a view is the opposite
        of that promise."""
        kit.stage.edit_target = FakeEditTarget(kit.stage.session_layer)

        save_active_view()

        assert kit.stage.edit_targets_set[0].GetLayer() is kit.stage.root_layer

    def test_and_is_put_back_afterwards(self, kit):
        original = FakeEditTarget(kit.stage.session_layer)
        kit.stage.edit_target = original

        save_active_view()

        assert kit.stage.edit_target is original

    def test_an_ordinary_edit_target_is_left_alone(self, kit):
        """A user editing a sublayer chose that sublayer."""
        save_active_view()

        assert kit.stage.edit_targets_set == []

    def test_a_prim_that_was_not_created_is_not_reported_as_a_camera(self, kit):
        """Otherwise the panel names a camera that is not on the stage, and
        the button on the result goes nowhere."""
        kit.refuse.add("CreatePrimCommand")

        assert save_active_view() is None

    def test_a_camera_with_no_framing_on_it_is_not_reported_either(self, kit):
        kit.refuse_authoring = True

        assert save_active_view() is None

    def test_no_open_stage_is_a_state_rather_than_a_failure(self, kit):
        kit.stage = None

        assert save_active_view() is None
        assert kit.commands == []


class TestGoingBackToIt:
    def test_the_viewport_is_pointed_at_the_camera(self, kit):
        kit.stage.paths.add(f"{RENDER_CAMERA_ROOT}/View")

        assert look_through(f"{RENDER_CAMERA_ROOT}/View") is True
        assert kit.viewport.camera_path == f"{RENDER_CAMERA_ROOT}/View"

    def test_a_camera_deleted_since_the_run_is_refused_out_loud(self, kit):
        """Assigning a path that does not resolve is not an error Kit raises:
        the viewport stays where it was and nothing says why."""
        assert look_through(f"{RENDER_CAMERA_ROOT}/Gone") is False
        assert kit.viewport.camera_path == VIEWPORT_CAMERA

    def test_nothing_to_go_back_to_moves_nothing(self, kit):
        assert look_through("") is False
        assert kit.viewport.camera_path == VIEWPORT_CAMERA


class TestWhichCameraAResultCanOfferToGoBackTo:
    def _slot(self, slot_id, camera_path=None):
        return (slot_id, None, "", b"png", None, camera_path)

    def test_a_run_captured_from_a_pinned_view_carries_it(self):
        state = {"image": [self._slot(1, f"{RENDER_CAMERA_ROOT}/View")]}

        assert cameras_in_image_state(state) == [f"{RENDER_CAMERA_ROOT}/View"]

    def test_a_run_nobody_pinned_a_view_for_carries_nothing(self):
        """Which is most of them, and the caller shows no way back rather than
        one that goes nowhere."""
        assert cameras_in_image_state({"image": [self._slot(1)]}) == []

    def test_two_captures_from_one_camera_are_one_place_to_stand(self):
        state = {
            "image": [
                self._slot(1, "/World/Cameras/Lobby"),
                self._slot(2, "/World/Cameras/Lobby"),
            ]
        }

        assert cameras_in_image_state(state) == ["/World/Cameras/Lobby"]

    def test_the_order_is_the_order_the_run_was_sent_in(self):
        state = {
            "start": [self._slot(1, "/World/Cameras/Lobby")],
            "end": [self._slot(2, "/World/Cameras/Atrium")],
        }

        assert cameras_in_image_state(state) == [
            "/World/Cameras/Lobby",
            "/World/Cameras/Atrium",
        ]

    def test_a_run_with_no_images_at_all_carries_nothing(self):
        assert cameras_in_image_state(None) == []
        assert cameras_in_image_state({}) == []


def test_the_root_sits_under_everything_else_this_plugin_adds():
    """One parent to find and one action to remove, which is the rule for
    anything this plugin puts on a stage it does not own."""
    assert RENDER_CAMERA_ROOT.startswith(f"{stage_module.ROOT_PATH}/")
    assert render_cameras.DEFAULT_CAMERA_NAME
