"""Which cameras are offered, and what they are called.

The traversal itself needs a stage and cannot be tested here.
Everything that decides what the user actually sees can be, and it is where the
mistakes are: a list with four rows of Kit furniture in it, or two rows reading
`Cam` that nobody can tell apart.
"""

from __future__ import annotations

from rundiffusion_omniverse.cameras import (
    IMPLICIT_PREFIX,
    StageCamera,
    _display_labels,
    is_implicit,
)
from rundiffusion_omniverse.compare import _capture_name


class TestKitsOwnCameras:
    def test_the_four_implicit_ones_are_recognised(self):
        for name in ("Persp", "Top", "Front", "Right"):
            assert is_implicit(f"{IMPLICIT_PREFIX}{name}")

    def test_an_authored_camera_is_not(self):
        assert not is_implicit("/World/Cameras/Lobby")

    def test_a_camera_merely_named_like_one_is_not(self):
        """The prefix is a path prefix, not a substring. A camera a user named
        after the Kit convention is still theirs."""
        assert not is_implicit("/World/OmniverseKit_Persp")


class TestLabelling:
    def test_a_unique_name_is_shown_on_its_own(self):
        cameras = _display_labels([("/World/Cameras/Lobby", "Lobby")])

        assert cameras == [
            StageCamera(path="/World/Cameras/Lobby", name="Lobby", label="Lobby")
        ]

    def test_a_repeated_name_carries_its_path(self):
        """Two rows reading `Cam` is a dropdown nobody can use."""
        cameras = _display_labels(
            [("/World/North/Cam", "Cam"), ("/World/South/Cam", "Cam")]
        )

        assert [camera.label for camera in cameras] == [
            "Cam  (/World/North/Cam)",
            "Cam  (/World/South/Cam)",
        ]

    def test_only_the_repeated_names_are_qualified(self):
        cameras = _display_labels(
            [("/a/Cam", "Cam"), ("/b/Cam", "Cam"), ("/c/Lobby", "Lobby")]
        )

        assert [camera.label for camera in cameras if camera.name == "Lobby"] == ["Lobby"]

    def test_the_list_is_sorted_without_regard_to_case(self):
        """Or `lobby` sorts after `Zone` and the list looks unsorted to anyone
        who names cameras inconsistently."""
        cameras = _display_labels(
            [("/a", "Zone"), ("/b", "lobby"), ("/c", "Atrium")]
        )

        assert [camera.name for camera in cameras] == ["Atrium", "lobby", "Zone"]

    def test_an_empty_stage_is_an_empty_list(self):
        assert _display_labels([]) == []

    def test_the_path_is_kept_whatever_the_label_becomes(self):
        """The path is what a viewport is pointed at and what is recorded with
        a run. The label is only for reading."""
        cameras = _display_labels([("/World/North/Cam", "Cam"), ("/World/South/Cam", "Cam")])

        assert [camera.path for camera in cameras] == [
            "/World/North/Cam",
            "/World/South/Cam",
        ]


class TestNamingACaptureAfterwards:
    def test_a_camera_capture_says_which_camera(self):
        """A run made from three cameras is three rows in the compare picker,
        and the same words three times over say nothing about which is which.
        The row already says it is a captured view, so the source is just the
        camera."""
        assert _capture_name("Captured from Lobby") == "Lobby"

    def test_a_viewport_capture_names_the_viewport(self):
        assert _capture_name("") == "Active viewport"

    def test_a_capture_recorded_before_this_existed_still_reads(self):
        """Image state travels with a result, so a run made yesterday is read
        by today's code."""
        assert _capture_name(None) == "Active viewport"
