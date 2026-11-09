"""Putting a generated 3D asset onto the user's own stage.

Groundwork for 3D output. The pure parts are covered here: which formats can
reach a stage at all, what the prim is called, and that a second generation
does not land on top of the first. So is the sizing arithmetic, and the order the
insertion runs its commands in, against a stage and a command stack faked just
far enough to record them. Whether the asset then looks right needs a running
Kit.

The naming rules are not cosmetic. A generated result is written as
`result-<hex>.glb`, and USD will not accept a prim name containing a hyphen, so
without `prim_name` every single insertion would be rejected.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import sys
import types
from pathlib import Path

import pytest

from rundiffusion_omniverse import stage as stage_module
from rundiffusion_omniverse.stage import (
    ROOT_PATH,
    InsertSize,
    Inserted,
    is_supported,
    needs_conversion,
    prim_name,
    scale_factor,
    target_in_stage_units,
    unique_prim_path,
)


class TestWhichFormatsCanReachAStage:
    @pytest.mark.parametrize("suffix", [".usd", ".usda", ".usdc", ".usdz", ".USD"])
    def test_usd_is_referenced_as_it_stands(self, suffix):
        path = Path(f"result{suffix}")
        assert is_supported(path) is True
        assert needs_conversion(path) is False

    @pytest.mark.parametrize("suffix", [".glb", ".gltf", ".obj", ".fbx"])
    def test_everything_else_is_converted_first(self, suffix):
        # Referencing one of these directly produces a prim that resolves to
        # nothing: an insertion that reports success and adds an empty Xform.
        path = Path(f"result{suffix}")
        assert is_supported(path) is True
        assert needs_conversion(path) is True

    @pytest.mark.parametrize("suffix", [".png", ".mp4", ".bin", ""])
    def test_what_is_not_a_model_cannot_reach_the_stage(self, suffix):
        assert is_supported(Path(f"result{suffix}")) is False


class TestPrimNaming:
    def test_a_generated_filename_becomes_usd_legal(self):
        # The real shape: `result-<uuid hex>.glb`. USD rejects the hyphen.
        assert prim_name(Path("result-9f2c4b.glb")) == "result_9f2c4b"

    def test_a_name_starting_with_a_digit_is_prefixed(self):
        # USD will not accept a prim name that starts with a digit.
        assert prim_name(Path("3d-model.glb")).startswith("asset_")

    def test_a_name_of_nothing_usable_still_produces_one(self):
        # Separators only. Better a plain "asset" than a row of underscores.
        assert prim_name(Path("---.glb")) == "asset"

    def test_a_dotfile_name_is_still_legal(self):
        # Path(".glb").stem is ".glb": Python reads it as a name, not a suffix.
        assert prim_name(Path(".glb")) == "glb"

    def test_no_name_survives_as_leading_or_trailing_separators(self):
        assert prim_name(Path("_result_.glb")) == "result"


class TestUniquePaths:
    def test_the_first_insertion_takes_the_plain_path(self):
        assert unique_prim_path(set(), "chair") == f"{ROOT_PATH}/chair"

    def test_a_second_generation_does_not_replace_the_first(self):
        taken = {f"{ROOT_PATH}/chair"}
        assert unique_prim_path(taken, "chair") == f"{ROOT_PATH}/chair_2"

    def test_it_keeps_counting_past_a_run_of_them(self):
        taken = {f"{ROOT_PATH}/chair", f"{ROOT_PATH}/chair_2", f"{ROOT_PATH}/chair_3"}
        assert unique_prim_path(taken, "chair") == f"{ROOT_PATH}/chair_4"

    def test_everything_lands_under_one_parent(self):
        # So a person can delete everything this plugin added in one action.
        assert unique_prim_path(set(), "chair").startswith(f"{ROOT_PATH}/")


class TestConvertingASizeIntoStageUnits:
    def test_meters_into_a_centimetre_stage(self):
        # Kit's default stage is centimetres, so this is the common case.
        assert target_in_stage_units(2.5, "m", 0.01) == pytest.approx(250.0)

    def test_centimetres_into_a_metre_stage(self):
        assert target_in_stage_units(250, "cm", 1.0) == pytest.approx(2.5)

    def test_feet_and_inches_are_exact(self):
        # Defined lengths, not approximations: a foot is 0.3048 m exactly.
        assert target_in_stage_units(1, "ft", 1.0) == pytest.approx(0.3048)
        assert target_in_stage_units(12, "in", 0.3048) == pytest.approx(1.0)

    def test_millimetres_into_a_millimetre_stage_are_unchanged(self):
        assert target_in_stage_units(40, "mm", 0.001) == pytest.approx(40.0)

    @pytest.mark.parametrize("meters_per_unit", [0.0, -1.0, math.nan])
    def test_an_unmeasurable_stage_reads_as_the_usd_default(self, meters_per_unit):
        # Dividing by a zero unit would throw the asset to infinity. USD's own
        # fallback is what every other tool reading the stage would assume.
        assert target_in_stage_units(1, "m", meters_per_unit) == pytest.approx(100.0)

    def test_an_unknown_unit_is_refused_rather_than_guessed(self):
        with pytest.raises(ValueError):
            target_in_stage_units(1, "furlong", 1.0)

    def test_the_size_converts_itself(self):
        assert InsertSize(3, "ft").in_stage_units(0.01) == pytest.approx(91.44)


class TestScaleFactor:
    def test_it_is_the_ratio_of_target_to_current(self):
        assert scale_factor(0.5, 250.0) == pytest.approx(500.0)

    def test_it_shrinks_as_readily_as_it_grows(self):
        assert scale_factor(200.0, 50.0) == pytest.approx(0.25)

    @pytest.mark.parametrize(
        "current, target",
        [(0.0, 1.0), (1e-12, 1.0), (1.0, 0.0), (math.nan, 1.0), (1.0, math.inf)],
    )
    def test_an_unusable_length_leaves_the_asset_alone(self, current, target):
        # An empty asset scaled to a real length would divide by nearly nothing.
        assert scale_factor(current, target) == 1.0


@pytest.fixture
def sizing(monkeypatch):
    """`_apply_size` against a measured prim, recording the scale it writes."""
    state = types.SimpleNamespace(
        longest=0.5, meters_per_unit=0.01, local_scale=(1.0, 1.0, 1.0), commands=[]
    )
    monkeypatch.setattr(stage_module, "longest_side", lambda _stage, _path: state.longest)
    monkeypatch.setattr(
        stage_module, "stage_meters_per_unit", lambda _stage: state.meters_per_unit
    )

    def run_command(name, **kwargs):
        state.commands.append((name, kwargs))
        return True

    monkeypatch.setattr(stage_module, "run_command", run_command)

    # No USD in this process. Gf.Vec3d only has to carry three numbers.
    pxr = types.ModuleType("pxr")
    pxr.Gf = types.SimpleNamespace(Vec3d=lambda *axes: tuple(axes))
    monkeypatch.setitem(sys.modules, "pxr", pxr)

    usd = types.ModuleType("omni.usd")
    usd.get_local_transform_SRT = lambda _prim: (state.local_scale, None, None, None)
    monkeypatch.setitem(sys.modules, "omni.usd", usd)
    monkeypatch.setattr(sys.modules["omni"], "usd", usd, raising=False)

    stage = types.SimpleNamespace(GetPrimAtPath=lambda _path: object())
    state.apply = lambda size: stage_module._apply_size(
        stage, "/RunDiffusion/chair", size, undoable=True
    )
    return state


class TestSizingTheInsertedPrim:
    def test_the_longest_side_is_scaled_to_the_asked_length(self, sizing):
        # 0.5 units across in a centimetre stage; asked for 1 m, so 100 units.
        assert sizing.apply(InsertSize(1.0, "m")) is True
        [(name, kwargs)] = sizing.commands
        assert name == "TransformPrimSRTCommand"
        assert kwargs["path"] == "/RunDiffusion/chair"
        assert kwargs["new_scale"] == pytest.approx((200.0, 200.0, 200.0))

    def test_a_scale_already_on_the_prim_is_multiplied_not_replaced(self, sizing):
        # Kit can correct a reference's units with a scale on this very prim,
        # and the measurement already includes it. Overwriting it with the bare
        # factor would land the asset off by the ratio of the two units.
        sizing.local_scale = (100.0, 100.0, 100.0)
        sizing.longest = 50.0
        assert sizing.apply(InsertSize(1.0, "m")) is True
        [(_name, kwargs)] = sizing.commands
        assert kwargs["new_scale"] == pytest.approx((200.0, 200.0, 200.0))

    def test_an_asset_already_the_right_size_is_not_touched(self, sizing):
        sizing.longest = 100.0
        assert sizing.apply(InsertSize(1.0, "m")) is True
        assert sizing.commands == []

    def test_an_asset_with_no_size_reports_that_it_was_not_sized(self, sizing):
        sizing.longest = 0.0
        assert sizing.apply(InsertSize(1.0, "m")) is False
        assert sizing.commands == []


class FakeInsertionStage:
    def __init__(self) -> None:
        self.paths: set[str] = {ROOT_PATH}

    def Traverse(self):  # noqa: N802 - the USD spelling
        return [
            types.SimpleNamespace(GetPath=lambda p=p: types.SimpleNamespace(pathString=p))
            for p in sorted(self.paths)
        ]


@pytest.fixture
def insertion(monkeypatch, tmp_path):
    """`add_to_stage` with the commands, the undo stack and the sizing faked."""
    state = types.SimpleNamespace(
        stage=FakeInsertionStage(),
        commands=[],
        in_group=False,
        sized_with=[],
        size_works=True,
        source=tmp_path / "result-ab12.usd",
    )
    state.source.write_text("#usda 1.0\n")

    usd = types.ModuleType("omni.usd")
    usd.get_context = lambda *_a, **_k: types.SimpleNamespace(get_stage=lambda: state.stage)
    monkeypatch.setitem(sys.modules, "omni.usd", usd)
    monkeypatch.setattr(sys.modules["omni"], "usd", usd, raising=False)

    @contextlib.contextmanager
    def group():
        state.in_group = True
        try:
            yield
        finally:
            state.in_group = False

    monkeypatch.setattr(stage_module, "_undo_group", group)

    def run_command(name, **kwargs):
        state.commands.append((name, state.in_group))
        if name == "CreatePrimCommand":
            state.stage.paths.add(kwargs["prim_path"])
        return True

    monkeypatch.setattr(stage_module, "run_command", run_command)
    monkeypatch.setattr(stage_module, "_has_reference", lambda _stage, _path: True)
    monkeypatch.setattr(
        stage_module, "_sdf", lambda: types.SimpleNamespace(Path=str, Reference=str)
    )

    def apply_size(_stage, path, size, *, undoable):
        state.sized_with.append((path, size, undoable, state.in_group))
        return state.size_works

    monkeypatch.setattr(stage_module, "_apply_size", apply_size)
    state.add = lambda size=None: asyncio.run(
        stage_module.add_to_stage(state.source, tmp_path, size)
    )
    return state


class TestInsertingAtASize:
    def test_create_reference_and_scale_are_one_undo_step(self, insertion):
        # Otherwise the first Ctrl+Z only takes the scale off, and the asset
        # sitting there at its raw size looks like undo did nothing.
        inserted = insertion.add(InsertSize(2.0, "m"))
        assert inserted == Inserted(f"{ROOT_PATH}/result_ab12", sized=True)
        assert insertion.commands == [("CreatePrimCommand", True), ("AddReference", True)]
        assert insertion.sized_with == [
            (f"{ROOT_PATH}/result_ab12", InsertSize(2.0, "m"), True, True)
        ]

    def test_the_first_insertion_creates_the_parent_inside_the_same_step(self, insertion):
        # Left implicit, the parent is created outside the undo stack and undo
        # leaves an empty /RunDiffusion on a stage that never had one.
        insertion.stage.paths.clear()
        insertion.add(InsertSize(2.0, "m"))
        assert insertion.commands == [
            ("CreatePrimCommand", True),
            ("CreatePrimCommand", True),
            ("AddReference", True),
        ]
        assert ROOT_PATH in insertion.stage.paths

    def test_keeping_the_original_size_does_not_scale(self, insertion):
        assert insertion.add(None) == Inserted(f"{ROOT_PATH}/result_ab12", sized=True)
        assert insertion.sized_with == []

    def test_a_size_that_would_not_apply_is_reported_not_hidden(self, insertion):
        # The asset is still on the stage, at the size it arrived. The caller
        # has to be able to say so, or the user finds out with a tape measure.
        insertion.size_works = False
        inserted = insertion.add(InsertSize(2.0, "m"))
        assert inserted == Inserted(f"{ROOT_PATH}/result_ab12", sized=False)
