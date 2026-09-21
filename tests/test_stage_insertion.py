"""Putting a generated 3D asset onto the user's own stage.

Groundwork for 3D output. The pure parts are covered here: which formats can
reach a stage at all, what the prim is called, and that a second generation
does not land on top of the first. The insertion itself needs a running Kit.

The naming rules are not cosmetic. A generated result is written as
`result-<hex>.glb`, and USD will not accept a prim name containing a hyphen, so
without `prim_name` every single insertion would be rejected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rundiffusion_omniverse.stage import (
    ROOT_PATH,
    is_supported,
    needs_conversion,
    prim_name,
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
