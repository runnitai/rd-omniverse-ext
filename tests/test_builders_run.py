"""Every builder on the redesigned surfaces runs to the end.

Nothing here says the panel looks right; that needs Kit and is in the manual
test script. What it does say is that each builder can be walked from its first
widget to its last without a NameError, a missing import or a keyword typo,
which is the class of fault that otherwise only shows up inside Kit as a panel
that stops drawing halfway down.

`omni.ui` is replaced by a stand-in that accepts every widget, keyword and
attribute, so the builders exercise their own logic and nothing of the
toolkit's.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime
from pathlib import Path

import pytest

from rundiffusion_omniverse import compare as compare_module
from rundiffusion_omniverse import dialogs as dialogs_module
from rundiffusion_omniverse import form as form_module
from rundiffusion_omniverse import panel as panel_module
from rundiffusion_omniverse.api.kits import Kit, KitTool
from rundiffusion_omniverse.api.tools import ToolDetail, ToolField, ToolSummary
from rundiffusion_omniverse.jobs import STATE_DONE, STATE_FAILED, Job, JobQueue


class _AnythingType(type):
    def __getattr__(cls, _name):
        return Anything()


class Anything(metaclass=_AnythingType):
    """A widget, a model, an enum member or a style value: whatever is asked."""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def __call__(self, *_args, **_kwargs):
        return Anything()

    def __getattr__(self, _name):
        return Anything()

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


class _AnyModule:
    def __getattr__(self, _name):
        return Anything


class RecordingRedraws:
    def __init__(self) -> None:
        self.booked: list[str] = []

    def request(self, key: str, render) -> None:
        self.booked.append(key)
        render()

    def cancel(self, *_args, **_kwargs) -> None:
        return None


@pytest.fixture(autouse=True)
def permissive_ui(monkeypatch):
    for module in (form_module, panel_module, compare_module, dialogs_module):
        monkeypatch.setattr(module, "ui", _AnyModule())
    app = types.ModuleType("omni.kit.app")
    app.get_app = lambda: Anything()
    kit = types.ModuleType("omni.kit")
    kit.app = app
    monkeypatch.setitem(sys.modules, "omni.kit", kit)
    monkeypatch.setitem(sys.modules, "omni.kit.app", app)
    monkeypatch.setattr(sys.modules["omni"], "kit", kit, raising=False)


def _tool() -> ToolDetail:
    size = {"select": {"options": [{"text": "Square", "width": 1024, "height": 1024}]}}
    return ToolDetail(
        id="t",
        name="Flux 2 Dev",
        tool_fields_hash="h",
        fields=[
            ToolField("prompt", "PROMPT", "Prompt", True),
            ToolField("images", "IMGS", "Images to edit", False),
            ToolField("size", "WIDTH_HEIGHT", "Output size", False, display=size),
            ToolField("cfg", "CFG", "Guidance scale", False, 2.5),
            ToolField("steps", "STEPS", "Steps", False, 28),
            ToolField("seed", "SEED", "Seed", False),
            ToolField("expand", "BOOLEAN", "Expand prompt", False, False),
        ],
    )


def _form() -> ToolForm:
    form = form_module.ToolForm(
        capture_sources=lambda: [("Active viewport", None), ("Lobby", "/World/Cameras/Lobby")],
        viewfinder_aspect=lambda: 16 / 9,
    )
    form._redraws = RecordingRedraws()
    form.build(Anything(), _tool(), capture_container=Anything())
    return form


ToolForm = form_module.ToolForm


class TestTheCreateForm:
    def test_the_whole_form_and_the_open_frame_build(self):
        form = _form()

        assert "capture-zone" in form._redraws.booked
        assert form.capture_target == "images"
        assert "images" in form._viewfinders

    def test_the_frame_builds_while_capturing_and_after_a_refusal(self):
        form = _form()
        form.show_capture_busy("images", form_module.CAPTURING_TEXT)
        form.clear_capture_busy("images")
        form.show_capture_error("images", "There is no active viewport to capture.")

    def test_the_strip_builds_once_a_capture_is_in(self):
        form = _form()
        form.add_capture("images", b"png", Path("thumb.png"))

        assert form._capture_layout == (True, form_module.STRIP_HEIGHT)

    def test_the_frame_builds_on_a_named_camera(self):
        form = _form()
        form._capture_source["images"] = "/World/Cameras/Lobby"
        form._render_capture_zone_now()

    def test_the_image_row_builds_selected(self):
        form = _form()
        form.add_chosen("images", "lib:1", "Sending kitchen.png.")
        slot_id = form.image_slots["images"][0].id
        form._select_slot("images", slot_id)

    def test_the_more_settings_header_builds_both_ways(self):
        form = _form()
        form._build_more_header(True, "More settings (3)")
        form._build_more_header(False, "More settings (3)")

    def test_a_size_matched_to_the_view_builds_its_note(self):
        _form().match_capture_aspect(16 / 9)

    def test_two_image_fields_offer_the_choice_of_where_to_capture(self):
        form = form_module.ToolForm()
        form._redraws = RecordingRedraws()
        tool = _tool()
        tool.fields.append(ToolField("end", "IMG", "End frame", False))
        form.build(Anything(), tool, capture_container=Anything())
        form._render_capture_zone_now()


def _panel(tmp_path) -> panel_module.RunDiffusionPanel:
    panel = object.__new__(panel_module.RunDiffusionPanel)
    panel._redraws = RecordingRedraws()
    panel._results_frame = Anything()
    panel._jobs_frame = Anything()
    panel._content = None
    panel._session_results = []
    panel._selected_result = None
    panel._result_cell = None
    panel._run_bars = []
    panel._run_bar_phase = 0.0
    panel._run_bar_subscription = None
    panel._jobs = JobQueue(on_changed=lambda: None)
    panel._spawn = lambda coroutine: coroutine.close()
    panel._tool_button = None
    return panel


def _result(tmp_path, kind="IMAGE") -> panel_module.SessionResult:
    return panel_module.SessionResult(
        "req-1",
        tmp_path / "render.png",
        "Flux 2 Dev",
        "t",
        {},
        {"images": [(1, None, "Captured from Lobby", b"png", None, "/World/Cameras/Lobby")]},
        "a lobby at dusk",
        kind=kind,
    )


class TestThePanel:
    def test_the_empty_results_strip_builds(self, tmp_path):
        _panel(tmp_path)._render_results_now()

    def test_the_results_strip_builds_with_results_and_runs_in_flight(self, tmp_path):
        panel = _panel(tmp_path)
        panel._session_results = [_result(tmp_path), _result(tmp_path, kind="ASSET_3D")]
        panel._jobs._jobs = [Job(id="j", tool_name="Flux 2 Dev", detail="queued")]

        panel._render_results_now()

        assert panel._result_cell == panel._result_cell_size()

    def test_the_selected_caption_names_the_source(self, tmp_path):
        result = _result(tmp_path)
        caption = _panel(tmp_path)._selected_caption(result)

        assert caption == f"Selected · Flux 2 Dev at {result.created:%H:%M} · from Lobby"

    def test_the_runs_list_builds_running_finished_and_failed(self, tmp_path):
        panel = _panel(tmp_path)
        panel._jobs._jobs = [
            Job(id="a", tool_name="Flux 2 Dev", detail="processing"),
            Job(id="b", tool_name="Flux 2 Dev", state=STATE_DONE, results=[1, 2]),
            Job(id="c", tool_name="Flux 2 Dev", state=STATE_FAILED, error="still needs Prompt"),
        ]

        panel._render_jobs_now()

        assert len(panel._run_bars) == 1

    def test_a_bar_tick_moves_without_failing(self, tmp_path):
        panel = _panel(tmp_path)
        panel._run_bars = [Anything()]
        panel._on_run_bar_tick(types.SimpleNamespace(payload={"dt": 0.5}))

        assert 0.0 < panel._run_bar_phase < 1.0

    def test_the_tool_row_builds(self, tmp_path):
        _panel(tmp_path)._build_tool_row("Flux 2 Dev")

    def test_compare_offers_save_send_and_use_for_an_image(self, tmp_path):
        panel = _panel(tmp_path)
        panel._compare = Anything()

        labels = [label for label, _action in panel._compare_actions(_result(tmp_path))]

        assert labels == ["Save", "Send", "Use in Create"]

    def test_compare_offers_only_save_for_a_model(self, tmp_path):
        panel = _panel(tmp_path)

        labels = [
            label
            for label, _action in panel._compare_actions(_result(tmp_path, kind="ASSET_3D"))
        ]

        assert labels == ["Save"]


class TestTheWindows:
    def test_compare_builds_with_and_without_inputs(self, tmp_path):
        window = compare_module.CompareWindow()
        window.show(
            tmp_path / "render.png",
            [("Captured view · Lobby", tmp_path / "in.png", "Captured view")],
            title="Flux 2 Dev",
            actions=[("Save", lambda: None)],
        )
        window.show(tmp_path / "render.png", [], title="Flux 2 Dev")
        window._move_seam(0.25)

    def test_the_tool_browser_builds_every_view(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dialogs_module.prefs, "_FALLBACK", {})
        dialogs_module.prefs.remember_tool("a")
        browser = dialogs_module.ToolBrowser(session=None, cache_dir=tmp_path)
        tools = [
            ToolSummary("a", "Flux 2 Dev", avatar_url="https://example.invalid/a.png"),
            ToolSummary("b", "Seedream", description="Flagship generation and editing."),
        ]
        kit = Kit(
            id="image",
            label="Image",
            kit_tags=("Featured",),
            tools=(KitTool(tool=tools[0], kit_tags=("Featured",)), KitTool(tool=tools[1])),
        )
        browser._tools = tools
        browser._kits = [kit]
        browser._scroll = Anything()

        browser._render_browse()
        browser._open_kit = kit
        browser._active_tab = "Featured"
        browser._render_browse()
        browser._search = "seed"
        browser._render_search()
