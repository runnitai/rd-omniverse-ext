"""The form's half of choosing a capture source.

What the form owes the panel is small and load-bearing: which
source a field is on, read at the moment of the click, and a slot that
remembers where its picture came from.

The reading-on-the-click part is the one worth a test. A capture is
asynchronous and the image row is rebuilt whenever an image is added or
removed, so a panel that went back and asked the form after the fact could be
asking a row that no longer exists about a dropdown that has been replaced.

Surviving a rebuild is the other. `build` clears the choice, and it is right
to: a camera chosen for one tool's field says nothing about another tool's. But
the Create tab is rebuilt for reasons that are not a tool change at all, and
across those the choice has to come back or the field silently captures
somewhere else.
"""

from __future__ import annotations

from rundiffusion_omniverse.form import ToolForm


def _form(**kwargs) -> ToolForm:
    form = ToolForm(**kwargs)
    form.image_field_keys = ["image"]
    form._plural_keys = {"image"}
    # The row is not built, so there is nothing to redraw. Every test here is
    # about state rather than widgets.
    form._render_image_field = lambda _key: None
    return form


class TestWhatAFieldIsCapturingFrom:
    def test_a_field_nobody_has_chosen_for_is_the_active_viewport(self):
        """None means the active viewport everywhere, so an untouched field
        behaves exactly as it did before this existed."""
        assert _form().capture_source("image") is None

    def test_a_field_that_was_never_built_is_too(self):
        assert _form().capture_source("no-such-field") is None

    def test_the_choice_is_per_field(self):
        """A tool with a start frame and an end frame is exactly the case where
        two different cameras is the point."""
        form = _form()
        form._capture_source = {"start": "/World/Cameras/Lobby", "end": None}

        assert form.capture_source("start") == "/World/Cameras/Lobby"
        assert form.capture_source("end") is None


class TestHandingItToThePanel:
    def test_the_source_travels_with_the_capture_request(self):
        """Read on the click and handed over, so the panel never has to go back
        and ask a row that may have been rebuilt underneath it."""
        asked: list = []
        form = _form(on_capture=lambda key, source: asked.append((key, source)))
        form._capture_source = {"image": "/World/Cameras/Lobby"}

        form._capture_into("image")

        assert asked == [("image", "/World/Cameras/Lobby")]

    def test_the_active_viewport_travels_as_None(self):
        asked: list = []
        form = _form(on_capture=lambda key, source: asked.append((key, source)))

        form._capture_into("image")

        assert asked == [("image", None)]


class TestACaptureThatLandsAfterTheToolChanged:
    """A capture outlives the form that asked for it: sixty frames of settle
    plus a readback is a second or more, the button is not disabled while it
    runs, and switching tool empties `image_slots` in between."""

    def test_it_is_refused_rather_than_orphaned(self):
        form = _form()
        form.image_field_keys = ["a-field-the-new-tool-has"]

        assert form.add_capture("the-old-tools-field", b"png") is None

    def test_it_does_not_eat_the_next_run_s_budget(self):
        """Nothing draws or sends an orphan, because everything else iterates
        `image_field_keys`. But `total_images` sums the whole dict, so it
        silently costs the next run one of its twelve images."""
        form = _form()
        form.image_field_keys = ["a-field-the-new-tool-has"]

        form.add_capture("the-old-tools-field", b"png")

        assert form.total_images() == 0

    def test_a_capture_for_a_field_the_tool_still_has_is_taken(self):
        form = _form()

        assert form.add_capture("image", b"png") is not None


class TestSurvivingARebuildOfTheTab:
    """Switching to the Library and back rebuilds the Create tab, and so does
    using an image from a grid. A field left pointed at the active viewport by
    one of those captures whatever the user happens to be orbiting, under a
    dropdown that has quietly changed what it says."""

    def test_a_chosen_camera_is_exported(self):
        form = _form()
        form._capture_source = {"image": "/World/Cameras/Lobby"}

        assert form.export_capture_sources() == {"image": "/World/Cameras/Lobby"}

    def test_the_active_viewport_is_not_worth_carrying(self):
        """None IS the default, so carrying it would be carrying nothing."""
        form = _form()
        form._capture_source = {"image": None}

        assert form.export_capture_sources() == {}

    def test_it_comes_back_after_a_rebuild(self):
        form = _form()
        form._capture_source = {"image": "/World/Cameras/Lobby"}
        carried = form.export_capture_sources()

        rebuilt = _form()
        rebuilt.restore_capture_sources(carried)

        assert rebuilt.capture_source("image") == "/World/Cameras/Lobby"

    def test_a_field_the_next_tool_does_not_have_is_dropped(self):
        """The same rule `restore_image_state` follows: a camera chosen for a
        field that is not on this tool answers a question nobody asked."""
        rebuilt = _form()

        rebuilt.restore_capture_sources({"some-other-field": "/World/Cameras/X"})

        assert rebuilt.capture_source("some-other-field") is None

    def test_restoring_nothing_is_harmless(self):
        rebuilt = _form()

        rebuilt.restore_capture_sources(None)

        assert rebuilt.capture_source("image") is None

    def test_the_row_is_redrawn_so_the_dropdown_agrees(self):
        """The restored value has to reach the ComboBox's index, and the
        viewfinder beside it has to stop showing the wrong source."""
        redrawn: list = []
        rebuilt = _form()
        rebuilt._render_image_field = redrawn.append

        rebuilt.restore_capture_sources({"image": "/World/Cameras/Lobby"})

        assert redrawn == ["image"]


class TestWhatASlotRemembers:
    def test_a_capture_can_carry_where_it_came_from(self):
        form = _form()

        slot_id = form.add_capture(
            "image", b"png", None, "Captured from Lobby"
        )

        slot = form.image_slots["image"][0]
        assert slot.id == slot_id
        assert slot.description == "Captured from Lobby"

    def test_a_viewport_capture_says_nothing_in_particular(self):
        """It has nothing more specific to say than the words the compare
        picker already uses for it."""
        form = _form()

        form.add_capture("image", b"png")

        assert form.image_slots["image"][0].description == ""

    def test_the_description_survives_the_form_being_torn_down(self):
        """The Create tab is rebuilt on every tab switch, and the exported state
        is also what travels with a submitted run."""
        form = _form()
        form.add_capture("image", b"png", None, "Captured from Lobby")

        exported = form.export_image_state()

        assert exported["image"][0][2] == "Captured from Lobby"

    def test_a_restored_capture_still_knows_its_camera(self):
        form = _form()
        form.add_capture("image", b"png", None, "Captured from Lobby")

        restored = _form()
        restored.restore_image_state(form.export_image_state())

        assert restored.image_slots["image"][0].description == "Captured from Lobby"
