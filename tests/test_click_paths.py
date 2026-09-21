"""What the common paths through the panel cost in clicks.

Two of them were charging for something the user had already said:

**The library route ended somewhere else than the picker route.** Both put an
image into the same field, but choosing through the field's own picker left the
user looking at the field it filled, while choosing from the Library tab left
them on the Library tab reading a status line that named the tab they now had
to click. One outcome, two endings, and the discoverable one was the longer.

**The viewer charged for closing itself.** Its actions each take the user
somewhere else, and the window stayed up over wherever they were taken, so
finishing with a render was act-then-close rather than act.

Both are tested here rather than in the browser because both are decisions
about what a click does, not about what it looks like.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rundiffusion_omniverse.api.generate import (
    KIND_ASSET_3D,
    KIND_IMAGE,
    KIND_VIDEO,
)
from rundiffusion_omniverse.panel import (
    TAB_CREATE,
    VIEWER_DISMISSING_ACTIONS,
    RunDiffusionPanel,
    SessionResult,
)


class FakeField:
    key = "image"
    label = "Image"


class FakeTool:
    name = "Tool"
    image_field = FakeField()


class FakeForm:
    """Enough of ToolForm to record what an added image did to it."""

    def __init__(self, *, room: bool = True) -> None:
        self._room = room
        self.added: list[tuple[str, object]] = []
        self.exports = 0

    def has_room(self, _key: str) -> bool:
        return self._room

    def add_chosen(self, key: str, ref, _message: str):
        self.added.append((key, ref))
        return f"slot-{len(self.added)}"

    def export_image_state(self):
        self.exports += 1
        return {"exported": self.exports}


class FakeAsset:
    is_image = True
    kind = "LIBRARY_REF"
    name = "a-render.png"

    def as_input_ref(self):
        return {"ref": self.name}


class FakeViewer:
    def __init__(self) -> None:
        self.hides = 0

    def hide(self) -> None:
        self.hides += 1


class ImmediateRedraws:
    """Redraws with the frame taken out, so a test needs no event loop.

    The real one waits a frame before running the rebuild. What these tests are
    about is what the rebuild DOES, so the wait is collapsed and the keys are
    recorded: `booked` is what proves the work was deferred rather than run
    inside the click.
    """

    def __init__(self) -> None:
        self.booked: list[str] = []

    def request(self, key: str, render) -> None:
        self.booked.append(key)
        render()


def _panel(*, form: FakeForm | None = None) -> RunDiffusionPanel:
    """A panel with only the attributes these two paths touch.

    `object.__new__` rather than the constructor, which builds a ui.Window.
    """
    panel = object.__new__(RunDiffusionPanel)
    panel._form = form if form is not None else FakeForm()
    panel._tool_detail = FakeTool()
    panel._chosen_assets = {}
    panel._image_state = None
    panel._redraws = ImmediateRedraws()
    panel._viewer = FakeViewer()
    panel.shown_tabs = []
    panel.statuses = []
    panel.errors = []
    panel._show_tab = panel.shown_tabs.append
    panel._set_status = panel.statuses.append
    panel._set_error = panel.errors.append
    return panel


class TestTheLibraryRouteLandsWhereTheImageDid:
    def test_using_a_library_image_opens_the_tab_it_went_to(self):
        panel = _panel()

        panel._use_asset_as_input(FakeAsset())

        assert panel.shown_tabs == [TAB_CREATE]

    def test_the_status_line_no_longer_has_to_name_a_tab(self):
        """It named one because it was the only thing that could. The image is
        now on screen in the field, so a sentence pointing at another tab would
        be pointing at the tab the user is already on."""
        panel = _panel()

        panel._use_asset_as_input(FakeAsset())

        assert panel.statuses == ["Added to Image."]

    def test_the_image_state_is_snapshotted_before_the_tab_is_built(self):
        """The tab switch rebuilds the form FROM that snapshot. Exporting after
        it would rebuild from the state as it was before this image, and the
        image would be dropped on arrival."""
        form = FakeForm()
        order: list[str] = []
        panel = _panel(form=form)
        form.export_image_state = lambda: order.append("export") or {}
        panel._show_tab = lambda index: order.append("show")

        panel._use_asset_as_input(FakeAsset())

        assert order == ["export", "show"]

    def test_the_image_still_reaches_the_form(self):
        form = FakeForm()
        panel = _panel(form=form)
        asset = FakeAsset()

        panel._use_asset_as_input(asset)

        assert form.added == [("image", {"ref": "a-render.png"})]
        assert panel._chosen_assets == {"slot-1": asset}

    def test_a_full_field_says_so_and_stays_where_it_is(self):
        """The tab switch is the confirmation, so it must not happen when
        there is nothing to confirm."""
        panel = _panel(form=FakeForm(room=False))

        panel._use_asset_as_input(FakeAsset())

        assert panel.shown_tabs == []
        assert panel.errors

    def test_the_tab_switch_waits_for_the_next_frame(self):
        """One of the buttons that gets here is the Use action INSIDE a grid
        cell. `_show_tab` clears the tab body, which destroys the VGrid, the
        cell, and the button still being dispatched. omni.ui refuses a
        container clear during dispatch whichever container it is."""
        panel = _panel()

        panel._use_asset_as_input(FakeAsset())

        assert panel._redraws.booked == ["tab"]


def _result(kind: str, suffix: str = ".png") -> SessionResult:
    return SessionResult(
        "req-1", Path(f"result{suffix}"), "Tool", "tool-1", {}, {}, "", kind=kind
    )


def _every_viewer_label() -> set[str]:
    """Every label a result can put in the viewer's action row."""
    panel = object.__new__(RunDiffusionPanel)
    labels: set[str] = set()
    for kind, suffix in ((KIND_IMAGE, ".png"), (KIND_VIDEO, ".mp4"), (KIND_ASSET_3D, ".glb")):
        labels |= {full for _short, full, _fn in panel._result_action_specs(_result(kind, suffix))}
    return labels


class TestTheViewerDoesNotChargeForClosingItself:
    @pytest.mark.parametrize("label", sorted(VIEWER_DISMISSING_ACTIONS))
    def test_an_action_that_ends_the_look_closes_the_window(self, label: str):
        panel = _panel()
        ran: list[str] = []

        wrapped = panel._dismissing([(label, lambda: ran.append(label))])
        wrapped[0][1]()

        assert panel._viewer.hides == 1
        assert ran == [label]

    def test_the_window_is_hidden_before_the_action_runs(self):
        """Matching the picker and the tool browser. Anything the action has to
        say belongs over the panel, and a slow action must not leave the viewer
        up looking like the click missed."""
        panel = _panel()
        order: list[str] = []
        panel._viewer.hide = lambda: order.append("hide")

        wrapped = panel._dismissing([("Compare", lambda: order.append("act"))])
        wrapped[0][1]()

        assert order == ["hide", "act"]

    def test_saving_leaves_the_render_on_screen(self):
        """A save dialog opens over the viewer and the render is usually still
        worth looking at once it is written."""
        panel = _panel()

        wrapped = panel._dismissing([("Save to computer", lambda: None)])
        wrapped[0][1]()

        assert panel._viewer.hides == 0

    def test_every_action_keeps_its_label(self):
        panel = _panel()
        actions = [("Compare", lambda: None), ("Save to computer", lambda: None)]

        assert [label for label, _fn in panel._dismissing(actions)] == [
            "Compare",
            "Save to computer",
        ]

    def test_the_set_is_pinned_to_the_labels_that_exist(self):
        """Matched by label, so a renamed button would silently stop closing
        the window. `Use in Create` is the one label the viewer gets from a
        library asset rather than from a result."""
        assert VIEWER_DISMISSING_ACTIONS - {"Use in Create"} == _every_viewer_label() - {
            "Save to computer"
        }


class FakeVideoAsset(FakeAsset):
    is_image = False


class TestTheGridOffersItsActionsWithoutASelectStep:
    def test_an_image_offers_the_same_three_the_row_below_does(self):
        panel = _panel()

        assert [label for label, _fn in panel._asset_cell_actions(FakeAsset())] == [
            "Use",
            "Save",
            "Open",
        ]

    def test_a_video_is_not_offered_a_field_that_would_reject_it(self):
        """The same rule the action row uses. An image field takes an image
        reference, so Use on a video is a button whose only outcome is a
        rejected run."""
        panel = _panel()

        assert [label for label, _fn in panel._asset_cell_actions(FakeVideoAsset())] == [
            "Save",
            "Open",
        ]

    def test_use_takes_the_asset_it_is_given(self):
        """Not one closed over at build time. The grid holds these for the life
        of a cell, and a rebuilt cell must not act on the asset that used to be
        in its place."""
        panel = _panel()
        asset = FakeAsset()
        use = dict(panel._asset_cell_actions(asset))["Use"]

        use(asset)

        assert panel.shown_tabs == [TAB_CREATE]
        assert panel._chosen_assets == {"slot-1": asset}
