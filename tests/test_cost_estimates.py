"""When the run is priced, and how often the server is asked to price it.

The cost above Generate follows the form as it changes, which makes the
estimate the one request a person sets off just by typing. So it keeps to
three rules:

- A burst of changes is one estimate.
- One estimate at a time. A change made while one is out waits for its answer,
  and then the newest form is priced once. Cancelling the estimate out and
  asking again would only stop the panel listening: the request still runs to
  the end, and the server still works it out.
- A question already answered is not asked again. The same body for the same
  account, recently, is drawn from the estimate already on screen. Coming back
  to the Create tab is the everyday case.

And the converse. Anything that changes the question (another account, another
tool, another value) is asked about, and only a change to the run re-prices it.
The viewfinder and the stage watch fire over and over while someone works in
the viewport, and neither of them is a change to the run.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from rundiffusion_omniverse import cameras as cameras_module
from rundiffusion_omniverse import form as form_module
from rundiffusion_omniverse import panel as panel_module
from rundiffusion_omniverse.api import generate as generate_api
from rundiffusion_omniverse.api.session import Account, RdSession
from rundiffusion_omniverse.api.tools import ToolDetail, ToolField
from rundiffusion_omniverse.api.transport import ApiError
from rundiffusion_omniverse.cameras import StageCamera
from rundiffusion_omniverse.panel import (
    CAMERA_LIST_CACHE_SECONDS,
    COST_QUOTE_SECONDS,
    TAB_CREATE,
    RunDiffusionPanel,
)

PERSONAL = Account(
    id="p1", label="Personal account", kind="PERSONAL",
    permissions={"USE_PLUGINS": True, "GENERATE": True}, plugin_kinds=["*"],
)
STUDIO = Account(
    id="t1", label="Studio One", kind="TEAM",
    permissions={"USE_PLUGINS": True, "GENERATE": True}, plugin_kinds=["*"],
)
TOOL = ToolDetail(id="tool-1", name="Flux 2 Dev", tool_fields_hash="h1")
BLOCKED_REASON = "Your balance is too low to run this tool."


# -- the estimate ------------------------------------------------------------


class Server:
    """preview-cost, answering only when the test says so.

    Records every estimate as it is sent, how many were in the air at once,
    and how many were abandoned on the way.
    """

    def __init__(self, can_run: bool = True) -> None:
        self.can_run = can_run
        self.sent: list = []
        self.in_air = 0
        self.most_in_air = 0
        self.abandoned = 0
        self._waiting: list = []

    async def __call__(
        self, session, tool, values, num_results=1, image_selections=None, account=None
    ):
        who = account if account is not None else session.selected_account
        self.sent.append((who.id if who is not None else None, values.get("prompt")))
        self.in_air += 1
        self.most_in_air = max(self.most_in_air, self.in_air)
        answer = asyncio.get_running_loop().create_future()
        self._waiting.append(answer)
        try:
            return await answer
        except asyncio.CancelledError:
            self.abandoned += 1
            raise
        finally:
            self.in_air -= 1

    def answer(self, error: ApiError | None = None) -> None:
        """Answer the oldest estimate still waiting for one."""
        while self._waiting and self._waiting[0].done():
            self._waiting.pop(0)
        waiting = self._waiting.pop(0)
        if error is not None:
            waiting.set_exception(error)
            return
        waiting.set_result(
            {
                "can_run": self.can_run,
                "blocking_reason": None if self.can_run else BLOCKED_REASON,
                "total_tokens": 4,
            }
        )


class Form:
    """Enough of ToolForm to be priced: the values it would send."""

    def __init__(self, prompt: str = "a") -> None:
        self.values = {"prompt": prompt}

    def collect(self):
        return dict(self.values)

    def image_selections(self):
        return {}

    def export_image_state(self):
        return {}


class Label:
    def __init__(self, text: str = "") -> None:
        self.text = text


def _session(*accounts: Account) -> RdSession:
    session = RdSession()
    session.refresh_token = "r"
    session.accounts = list(accounts)
    session.user = {"email": "a@b.test", "email_verified": True}
    session._persist = lambda: None
    return session


def _panel(monkeypatch, *accounts: Account, can_run: bool = True):
    server = Server(can_run=can_run)
    monkeypatch.setattr(generate_api, "preview_cost", server)
    # The burst rule is the debounce's, and a test that waited it out would
    # only be slower.
    monkeypatch.setattr(panel_module, "COST_DEBOUNCE_SECONDS", 0)

    panel = object.__new__(RunDiffusionPanel)
    panel._session = _session(*(accounts or (PERSONAL,)))
    panel._form = Form()
    panel._tool_detail = TOOL
    panel._result_count = 1
    panel._team_answers = {}
    panel._tasks = set()
    panel._cost_task = None
    panel._estimate_generation = 0
    panel._cost_request = None
    panel._cost_request_key = None
    panel._cost_wanted_again = False
    panel._cost_quote = None
    panel._cost_message = ""
    panel._cost_label = Label()
    panel.walls = []
    panel._render_wall = panel.walls.append
    panel._clear_wall = lambda: panel.walls.append(None)
    panel.failures = []

    def finished(task) -> None:
        panel._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            panel.failures.append(task.exception())

    panel._task_finished = finished
    return server, panel


async def _settle() -> None:
    """Let every task that can move, move."""
    for _ in range(25):
        await asyncio.sleep(0)


def _change(panel, prompt: str) -> None:
    panel._form.values["prompt"] = prompt
    panel._schedule_cost_refresh()


def _run(panel, scenario) -> None:
    asyncio.run(scenario())
    assert panel.failures == []


class TestABurstOfChanges:
    def test_is_one_estimate(self, monkeypatch):
        server, panel = _panel(monkeypatch)

        async def scenario():
            for prompt in "abcde":
                _change(panel, prompt)
            await _settle()
            server.answer()
            await _settle()

        _run(panel, scenario)

        assert server.sent == [("p1", "e")]
        assert panel._cost_label.text == "4 tokens"


class TestOneEstimateAtATime:
    def test_a_change_while_one_is_out_sends_nothing_yet(self, monkeypatch):
        server, panel = _panel(monkeypatch)

        async def scenario():
            _change(panel, "a")
            await _settle()
            _change(panel, "b")
            await _settle()
            assert server.sent == [("p1", "a")]
            assert server.in_air == 1

        _run(panel, scenario)

    def test_the_newest_form_is_priced_when_the_answer_lands(self, monkeypatch):
        """Each pause in typing used to cancel the estimate out and send
        another. Now every change made meanwhile is one estimate, of the form
        as it is when the first answer lands."""
        server, panel = _panel(monkeypatch)

        async def scenario():
            _change(panel, "a")
            await _settle()
            for prompt in "bcd":
                _change(panel, prompt)
                await _settle()
            server.answer()
            await _settle()
            server.answer()
            await _settle()

        _run(panel, scenario)

        assert server.sent == [("p1", "a"), ("p1", "d")]
        assert server.most_in_air == 1

    def test_the_estimate_out_is_not_cancelled_by_a_change(self, monkeypatch):
        """Cancelling it stops the panel waiting, not the server working."""
        server, panel = _panel(monkeypatch)

        async def scenario():
            _change(panel, "a")
            await _settle()
            _change(panel, "b")
            await _settle()
            server.answer()
            await _settle()
            server.answer()
            await _settle()

        _run(panel, scenario)

        assert server.abandoned == 0

    def test_a_form_put_back_while_one_is_out_asks_nothing_more(self, monkeypatch):
        server, panel = _panel(monkeypatch)

        async def scenario():
            _change(panel, "a")
            await _settle()
            _change(panel, "b")
            await _settle()
            _change(panel, "a")
            await _settle()
            server.answer()
            await _settle()

        _run(panel, scenario)

        assert server.sent == [("p1", "a")]


class TestTheSameQuestionAgain:
    def test_is_answered_from_the_estimate_on_screen(self, monkeypatch):
        """A visit to the Create tab builds the cost line again, and the form's
        rebuild reports every value it puts back as a change."""
        server, panel = _panel(monkeypatch)

        async def scenario():
            _change(panel, "a")
            await _settle()
            server.answer()
            await _settle()
            panel._cost_label = Label()
            for _ in range(3):
                panel._schedule_cost_refresh()
            await _settle()

        _run(panel, scenario)

        assert server.sent == [("p1", "a")]
        assert panel._cost_label.text == "4 tokens"

    def test_its_wall_is_drawn_back_with_it(self, monkeypatch):
        server, panel = _panel(monkeypatch, can_run=False)

        async def scenario():
            _change(panel, "a")
            await _settle()
            server.answer()
            await _settle()
            panel._schedule_cost_refresh()
            await _settle()

        _run(panel, scenario)

        assert len(server.sent) == 1
        first, again = panel.walls
        assert first is not None and first.message == BLOCKED_REASON
        assert again is first

    def test_an_old_answer_is_asked_again(self, monkeypatch):
        """So a plan or a balance changed on the web still shows up on the next
        visit, without a restart."""
        server, panel = _panel(monkeypatch)

        async def scenario():
            _change(panel, "a")
            await _settle()
            server.answer()
            await _settle()
            panel._cost_quote.landed_at -= COST_QUOTE_SECONDS + 1
            panel._schedule_cost_refresh()
            await _settle()
            server.answer()
            await _settle()

        _run(panel, scenario)

        assert server.sent == [("p1", "a"), ("p1", "a")]

    def test_a_failed_estimate_is_asked_again(self, monkeypatch):
        """Only an answer that was drawn is held. A failure is not one."""
        server, panel = _panel(monkeypatch)

        async def scenario():
            _change(panel, "a")
            await _settle()
            server.answer(ApiError(0, "TIMEOUT", "Took too long."))
            await _settle()
            assert panel._cost_label.text == "Cost unavailable. Took too long."
            panel._schedule_cost_refresh()
            await _settle()
            server.answer()
            await _settle()

        _run(panel, scenario)

        assert server.sent == [("p1", "a"), ("p1", "a")]
        assert panel._cost_label.text == "4 tokens"


class FakeCombo:
    def __init__(self, index: int) -> None:
        self.index = index
        self.model = self

    def get_item_value_model(self):
        return self

    @property
    def as_int(self):
        return self.index


def _choosable(panel) -> None:
    """What choosing in the BILLED TO picker reads and writes, and no more."""
    panel._account_combo = FakeCombo(0)
    panel._account_carry = None
    panel._catalog_generation = 0
    panel._tool_load_generation = 0
    panel._tool_detail_pending = False
    panel._tools = []
    panel._kits = []
    panel._tool_tags = {}
    panel._selected_tool_id = None
    panel._forget_browsing = lambda: None
    panel._account_redraws = types.SimpleNamespace(request=lambda _key, _render: None)


class TestAChangedQuestion:
    def test_the_same_form_on_another_account_is_asked(self, monkeypatch):
        server, panel = _panel(monkeypatch, PERSONAL, STUDIO)

        async def scenario():
            _change(panel, "a")
            await _settle()
            server.answer()
            await _settle()
            panel._session.selected_account_id = STUDIO.id
            panel._schedule_cost_refresh()
            await _settle()
            server.answer()
            await _settle()

        _run(panel, scenario)

        assert server.sent == [("p1", "a"), ("t1", "a")]

    def test_choosing_another_account_drops_the_estimate_out_and_the_one_held(
        self, monkeypatch
    ):
        """Both were asked for the account being left. The one out is
        abandoned rather than left to ask every team about it too."""
        server, panel = _panel(monkeypatch, PERSONAL, STUDIO)
        _choosable(panel)

        async def scenario():
            _change(panel, "a")
            await _settle()
            server.answer()
            await _settle()
            _change(panel, "b")
            await _settle()
            panel._account_combo.index = 1
            panel._on_account_changed()
            await _settle()
            assert server.abandoned == 1
            assert panel._cost_quote is None
            # The studio's catalogue arrives and its tool is loaded.
            panel._tool_detail = TOOL
            _change(panel, "a")
            await _settle()
            server.answer()
            await _settle()

        _run(panel, scenario)

        assert server.sent == [("p1", "a"), ("p1", "b"), ("t1", "a")]

    def test_another_tool_is_asked(self, monkeypatch):
        server, panel = _panel(monkeypatch)

        async def scenario():
            _change(panel, "a")
            await _settle()
            server.answer()
            await _settle()
            panel._tool_detail = ToolDetail(id="tool-2", name="Other", tool_fields_hash="h2")
            panel._schedule_cost_refresh()
            await _settle()
            server.answer()
            await _settle()

        _run(panel, scenario)

        assert len(server.sent) == 2

    def test_a_run_started_means_the_next_look_asks_again(self, monkeypatch):
        """Spending moves the balance the held estimate was about."""
        server, panel = _panel(monkeypatch)
        submitted = []
        panel._jobs = types.SimpleNamespace(
            submit=lambda name, start, prompt=None: submitted.append(name)
        )

        async def scenario():
            _change(panel, "a")
            await _settle()
            server.answer()
            await _settle()
            panel._on_generate_clicked()
            panel._schedule_cost_refresh()
            await _settle()
            server.answer()
            await _settle()

        _run(panel, scenario)

        assert submitted == [TOOL.name]
        assert server.sent == [("p1", "a"), ("p1", "a")]


class TestARoleThatHoldsGenerateBack:
    def test_a_held_estimate_is_not_drawn_over_the_role_wall(self, monkeypatch):
        """The estimate on screen was drawn while the role could generate. Once
        the role cannot, the same form is the same question, and drawing the
        held answer again would take the role's wall down with nothing asked."""
        server, panel = _panel(monkeypatch)

        async def scenario():
            _change(panel, "a")
            await _settle()
            server.answer()
            await _settle()
            # The account list is read again and the role no longer generates.
            panel._session.accounts = [
                Account(
                    id="p1", label="Personal account", kind="PERSONAL",
                    permissions={"USE_PLUGINS": True, "GENERATE": False},
                    plugin_kinds=["*"],
                )
            ]
            panel._schedule_cost_refresh()
            await _settle()

        _run(panel, scenario)

        assert server.sent == [("p1", "a")]
        assert panel.walls[-1] is not None
        assert panel.walls[-1].variant == "role_blocked"
        assert panel._cost_label.text == ""


# -- what never re-prices -----------------------------------------------------


class _AnythingType(type):
    def __getattr__(cls, _name):
        return Anything()


class Anything(metaclass=_AnythingType):
    """Any widget, model, enum member or style value omni.ui would hand back.

    The callbacks handed to a model are recorded rather than kept, so a test
    can tell which controls a redraw built. This stand-in never fires them, so
    what is pinned here is the panel's code and the form's, not the toolkit's.
    """

    registered: list = []

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def __call__(self, *_args, **_kwargs):
        return Anything()

    def __getattr__(self, name):
        if name in ("add_value_changed_fn", "add_item_changed_fn"):
            return _register
        return Anything()

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def _register(callback, *_args, **_kwargs):
    Anything.registered.append(callback)
    return Anything()


class _AnyModule:
    def __getattr__(self, _name):
        return Anything


class Immediate:
    """The form's redraws, run at once and recorded."""

    def __init__(self) -> None:
        self.keys: list = []

    def request(self, key, render) -> None:
        self.keys.append(key)
        render()

    def cancel(self, *_args, **_kwargs) -> None:
        return None


class NextFrame:
    """The panel's redraws, held until the test lets a frame pass.

    Coalesced per key like the real ones, which matters here: the stage watch
    re-books itself while the camera list is fresh, and running that at once
    would never end.
    """

    def __init__(self) -> None:
        self.pending: dict = {}

    def request(self, key, render) -> None:
        self.pending[key] = render

    def cancel(self, *_args, **_kwargs) -> None:
        self.pending.clear()

    def frame(self) -> None:
        pending, self.pending = self.pending, {}
        for render in pending.values():
            render()


class FakePreview:
    """The live picture of the viewport, counting each time it is drawn."""

    message = "Looking..."

    def __init__(self) -> None:
        self.drawn = 0

    @property
    def path(self):
        self.drawn += 1
        return None

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


class FakeStills:
    def __init__(self) -> None:
        self.ensured: list = []

    def ensure(self, camera_path, _spawn) -> None:
        self.ensured.append(camera_path)

    def path(self, _camera_path):
        return None

    def message(self, _camera_path):
        return "Looking..."


LOBBY = StageCamera(path="/World/Cameras/Lobby", name="Lobby", label="Lobby")
ATRIUM = StageCamera(path="/World/Cameras/Atrium", name="Atrium", label="Atrium")


def _viewport_tool() -> ToolDetail:
    size = {"select": {"options": [{"text": "Square", "width": 1024, "height": 1024}]}}
    return ToolDetail(
        id="t",
        name="Flux 2 Dev",
        tool_fields_hash="h",
        fields=[
            ToolField("prompt", "PROMPT", "Prompt", True),
            ToolField("images", "IMGS", "Images to edit", False),
            ToolField("size", "WIDTH_HEIGHT", "Output size", False, display=size),
            ToolField("steps", "STEPS", "Steps", False, 28),
        ],
    )


@pytest.fixture
def watching(monkeypatch, tmp_path):
    """A Create tab built for real, wired the way the panel wires it.

    The form's value-changed hook and the panel's re-pricing entry points all
    record into `repriced`, so any route from these callbacks to a re-price
    shows up there, whichever way it goes.
    """
    for module in (form_module, panel_module):
        monkeypatch.setattr(module, "ui", _AnyModule())
    monkeypatch.setattr(cameras_module, "stage_cameras", lambda: [LOBBY], raising=False)
    monkeypatch.setattr(Anything, "registered", [])

    repriced: list = []
    panel = object.__new__(RunDiffusionPanel)
    panel._schedule_cost_refresh = lambda *_a: repriced.append("schedule")
    panel._request_cost = lambda: repriced.append("request")

    async def preview_cost(*_a, **_k):
        repriced.append("preview")

    panel._preview_cost = preview_cost
    panel._redraws = NextFrame()
    panel._current_tab = TAB_CREATE
    panel._camera_sources = None
    panel._camera_sources_read_at = 0.0
    panel._cropped_previews = {}
    panel._result_dir = tmp_path
    panel._viewport_preview = FakePreview()
    panel._camera_stills = FakeStills()
    panel._camera_watch = types.SimpleNamespace(start=lambda: None, stop=lambda: None)
    panel._spawn = lambda coroutine: coroutine.close()

    form = form_module.ToolForm(
        capture_sources=panel._capture_sources,
        on_render_viewfinder=panel._render_viewfinder,
        on_values_changed=panel._schedule_cost_refresh,
        viewfinder_aspect=lambda: 16 / 9,
        on_capture_zone_rendered=panel._sync_viewport_watch,
    )
    form._redraws = Immediate()
    panel._form = form
    form.build(Anything(), _viewport_tool(), capture_container=Anything())
    assert "images" in form._viewfinders, "the viewfinder was not built"

    # Whatever the build itself drew or reported is the build's business.
    repriced.clear()
    Anything.registered.clear()
    form._redraws.keys.clear()
    panel._viewport_preview.drawn = 0
    return types.SimpleNamespace(panel=panel, form=form, repriced=repriced)


class TestWhatNeverReprices:
    def test_the_live_viewfinder(self, watching):
        """It redraws every time the camera settles, which is all the time
        while someone is framing a shot."""
        preview = watching.panel._viewport_preview

        for _ in range(30):
            watching.panel._on_viewport_preview_changed()

        assert preview.drawn == 30, "the viewfinder was not redrawn"
        assert watching.repriced == []
        assert watching.form._on_control_changed not in Anything.registered

    def test_a_named_cameras_still(self, watching):
        """A still landing calls the same door as the live picture."""
        watching.form._capture_source["images"] = LOBBY.path

        for _ in range(10):
            watching.panel._on_viewport_preview_changed()

        assert watching.panel._camera_stills.ensured, "the still was not drawn"
        assert watching.repriced == []

    def test_the_stage_watch(self, monkeypatch, watching):
        """A camera appearing rebuilds the image row and the capture zone.
        Neither is a change to the run, and neither rebuilds a value control,
        which is the one thing that could report a value being set."""
        panel = watching.panel
        panel._camera_sources_read_at -= CAMERA_LIST_CACHE_SECONDS + 1
        monkeypatch.setattr(
            cameras_module, "stage_cameras", lambda: [LOBBY, ATRIUM], raising=False
        )

        for _ in range(10):
            panel._on_stage_cameras_changed()
        panel._redraws.frame()

        assert "image:images" in watching.form._redraws.keys, "the row was not redrawn"
        assert watching.repriced == []
        assert watching.form._on_control_changed not in Anything.registered
