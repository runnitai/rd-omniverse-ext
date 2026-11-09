"""A team-plan member is pointed at their team before any upgrade prompt.

Reported by a tester: someone whose team is on a paid plan, generating on their
free personal account, was told to upgrade their plan. What they needed was to
pick their team under BILLED TO. The prompt sent them to buy a second plan.

So before an upgrade prompt for a personal account, the panel prices the same
run on each of the caller's teams, and if one can run it the wall offers to
switch to it instead. A selected team that is refused is named, with the people
who can fix it, rather than offered a checkout that cannot reach it.
"""

from __future__ import annotations

import asyncio
import types

import omni.ui as ui
import pytest

from rundiffusion_omniverse.api import generate as generate_api
from rundiffusion_omniverse.api import tools as tools_api
from rundiffusion_omniverse.api.session import Account, RdSession
from rundiffusion_omniverse.api.token_wall import (
    SUBSCRIPTIONS_URL,
    VARIANT_GET_PLAN,
    VARIANT_GET_TOKENS,
    VARIANT_SWITCH_ACCOUNT,
    VARIANT_TEAM_BLOCKED,
    VARIANT_TEAM_INACTIVE,
    VARIANT_TEAM_PLAN_REQUIRED,
    VARIANT_VERIFY,
    personal_cannot_pay,
    switch_wall,
    team_blocked_wall,
    team_name,
    teams_worth_checking,
    wall_for_error,
    wall_for_preview,
)
from rundiffusion_omniverse.api.tools import ImageSelection, ToolDetail, ToolSummary
from rundiffusion_omniverse.api.transport import ApiError
from rundiffusion_omniverse.panel import RunDiffusionPanel, _AccountCarry

PERSONAL = Account(id="p1", label="Personal account", kind="PERSONAL")
ALPHA = Account(
    id="t1", label="alpha Team", kind="TEAM",
    permissions={"USE_PLUGINS": True, "GENERATE": True}, plugin_kinds=["*"],
    team_label="Team",
)
BETA = Account(
    id="t2", label="Beta Studio", kind="TEAM",
    permissions={"USE_PLUGINS": True, "GENERATE": True}, plugin_kinds=["*"],
    team_label="Studio",
)
NO_PLUGIN = Account(
    id="t3", label="Gamma", kind="TEAM",
    permissions={"USE_PLUGINS": False}, plugin_kinds=["*"],
)
NO_GENERATE = Account(
    id="t4", label="Delta", kind="TEAM",
    permissions={"USE_PLUGINS": True, "GENERATE": False}, plugin_kinds=["*"],
)

FREEMIUM_REASON = (
    "A paid plan is required to generate through the API or plugins. "
    "Upgrade your plan to continue."
)

TOOL = ToolDetail(id="tool-1", name="Flux 2 Dev", tool_fields_hash="h1")


def _run(coroutine):
    return asyncio.new_event_loop().run_until_complete(coroutine)


def _all_copy(wall) -> str:
    return " ".join(part for part in (wall.message, wall.detail, wall.action_label) if part)


# -- the pure rules ---------------------------------------------------------


class TestSwitchWall:
    def test_it_offers_the_team_rather_than_an_upgrade(self):
        wall = switch_wall(team_id="t1", account_name="alpha Team")

        assert wall.variant == VARIANT_SWITCH_ACCOUNT
        assert wall.switch_to == "t1"
        assert wall.action_label == "Switch to alpha Team"
        assert wall.action_url is None
        assert "upgrade" not in _all_copy(wall).lower()
        assert "alpha Team can" in wall.detail


class TestSelectedTeamIsBlocked:
    def test_the_team_is_named_with_the_servers_reason_and_its_administrators(self):
        wall = wall_for_preview(
            can_run=False,
            blocking_reason="Your balance of (12) is too low to run this tool",
            is_personal=False,
            email_verified=True,
            account_name="alpha Team",
        )

        assert wall.variant == VARIANT_TEAM_BLOCKED
        assert wall.message == "alpha Team cannot run this tool."
        # The server's sentence gets its full stop before the next one starts.
        assert wall.detail.startswith("Your balance of (12) is too low to run this tool. ")
        assert "your account administrators" in wall.detail
        assert wall.action_url is None and wall.switch_to is None

    def test_no_reason_still_says_who_can_fix_it(self):
        wall = team_blocked_wall(account_name="alpha Team", reason=None)

        assert wall.detail.startswith("Its plan, token budget and permissions")

    def test_a_free_team_plan_on_generate_names_the_team(self):
        wall = wall_for_error(
            "FREEMIUM_API_NOT_ALLOWED", 403, is_personal=False, email_verified=True,
            account_name="alpha Team",
        )

        assert wall.variant == VARIANT_TEAM_PLAN_REQUIRED
        assert wall.message.startswith("alpha Team needs a paid plan")
        assert wall.action_url is None

    def test_a_deactivated_team_says_so(self):
        wall = wall_for_error(
            "TEAM_INACTIVE", 403, is_personal=False, email_verified=True,
            account_name="alpha Team",
        )

        assert wall.variant == VARIANT_TEAM_INACTIVE
        assert "BILLED TO" in wall.detail

    def test_a_nameless_team_is_called_what_its_company_calls_teams(self):
        """A company can rename the concept, so the fallback never hardcodes it."""
        assert team_name(None, "Studio") == "This Studio"
        assert team_name("Beta", "Studio") == "Beta"


class TestPersonalPlanWall:
    def test_a_free_personal_plan_on_generate_is_offered_plans(self):
        wall = wall_for_error(
            "FREEMIUM_API_NOT_ALLOWED", 403, is_personal=True, email_verified=True
        )

        assert wall.variant == VARIANT_GET_PLAN
        assert wall.action_url == SUBSCRIPTIONS_URL

    def test_unverified_still_wins_over_the_plan(self):
        wall = wall_for_error(
            "FREEMIUM_API_NOT_ALLOWED", 403, is_personal=True, email_verified=False
        )

        assert wall.variant == VARIANT_VERIFY


class TestWhenATeamIsWorthAsking:
    def test_a_blocked_personal_estimate_is(self):
        assert personal_cannot_pay(is_personal=True, email_verified=True, blocked_by_preview=True)

    @pytest.mark.parametrize("code,status", [("FREEMIUM_API_NOT_ALLOWED", 403), ("INSUFFICIENT_BALANCE", 402), (None, 402)])
    def test_a_personal_payment_refusal_is(self, code, status):
        assert personal_cannot_pay(
            is_personal=True, email_verified=True, error_code=code, status=status
        )

    def test_an_unverified_caller_is_not(self):
        """Refused on every account until they verify."""
        assert not personal_cannot_pay(is_personal=True, email_verified=False, blocked_by_preview=True)

    def test_a_selected_team_is_not(self):
        assert not personal_cannot_pay(is_personal=False, email_verified=True, blocked_by_preview=True)

    def test_an_unrelated_refusal_is_not(self):
        assert not personal_cannot_pay(
            is_personal=True, email_verified=True, error_code="INVALID_REQUEST", status=400
        )


class TestWhichTeams:
    def test_teams_whose_role_refuses_are_never_asked(self):
        accounts = [PERSONAL, ALPHA, NO_PLUGIN, NO_GENERATE, BETA]

        assert teams_worth_checking(accounts) == [ALPHA, BETA]

    def test_a_role_that_publishes_nothing_is_left_to_the_server(self):
        quiet = Account(id="t9", label="Quiet", kind="TEAM")

        assert teams_worth_checking([quiet]) == [quiet]


def test_no_wall_copy_has_an_em_dash():
    walls = [
        switch_wall(team_id="t1", account_name="alpha Team"),
        team_blocked_wall(account_name="alpha Team", reason=FREEMIUM_REASON),
        wall_for_error("FREEMIUM_API_NOT_ALLOWED", 403, is_personal=True, email_verified=True),
        wall_for_error("FREEMIUM_API_NOT_ALLOWED", 403, is_personal=False, email_verified=True),
        wall_for_error("INSUFFICIENT_BALANCE", 402, is_personal=False, email_verified=True),
        wall_for_error("TEAM_INACTIVE", 403, is_personal=False, email_verified=True),
    ]
    for wall in walls:
        assert chr(0x2014) not in _all_copy(wall)


# -- the panel --------------------------------------------------------------


class FakeForm:
    def __init__(self, values=None, selections=None, image_state=None) -> None:
        self.values = dict(values or {})
        self.selections = dict(selections or {})
        self.image_state = dict(image_state or {})
        self.capture_sources = {"image": "viewport"}
        self.built_with = None
        self.restored_images = None
        self.restored_sources = None

    def collect(self):
        return dict(self.values)

    def image_selections(self):
        return dict(self.selections)

    def export_image_state(self):
        return dict(self.image_state)

    def export_capture_sources(self):
        return dict(self.capture_sources)

    def build(self, _frame, tool, values, capture_container=None):
        self.built_with = (tool.id, dict(values))

    def restore_image_state(self, state):
        self.restored_images = state

    def restore_capture_sources(self, state):
        self.restored_sources = state


class FakeCombo:
    def __init__(self, index: int, on_change) -> None:
        self.index = index
        self._on_change = on_change
        self.model = self

    def get_item_value_model(self):
        return self

    @property
    def as_int(self):
        return self.index

    def set_value(self, index):
        self.index = index
        self._on_change()


class PricedBy:
    """preview-cost, answered per account, recording who was asked."""

    def __init__(self, can_run: dict, reason=FREEMIUM_REASON, failing=()) -> None:
        self.can_run = can_run
        self.reason = reason
        self.failing = set(failing)
        self.asked: list = []
        self.selections: dict = {}

    async def __call__(self, session, tool, values, num_results=1, image_selections=None, account=None):
        who = account if account is not None else session.selected_account
        key = who.id if who is not None else None
        self.asked.append(key)
        self.selections[key] = image_selections
        if key in self.failing:
            raise ApiError(403, "PERMISSION_DENIED", "Not on this team.")
        allowed = self.can_run.get(key, False)
        return {
            "can_run": allowed,
            "blocking_reason": None if allowed else self.reason,
            "total_tokens": 4,
        }


def _session(*accounts, selected=None, verified=True) -> RdSession:
    session = RdSession()
    session.refresh_token = "r"
    session.accounts = list(accounts)
    session.selected_account_id = selected
    session.user = {"email": "a@b.test", "email_verified": verified}
    session._persist = lambda: None
    return session


class ImmediateRedraws:
    """Runs a deferred redraw at once, so a test can see what it did."""

    def __init__(self) -> None:
        self.keys: list = []

    def request(self, key, render) -> None:
        self.keys.append(key)
        render()


def _panel(session, form=None) -> RunDiffusionPanel:
    panel = object.__new__(RunDiffusionPanel)
    panel._session = session
    panel._tool_detail = TOOL
    panel._tools = [ToolSummary(id=TOOL.id, name=TOOL.name)]
    panel._team_answers = {}
    panel._redraws = ImmediateRedraws()
    panel._account_redraws = ImmediateRedraws()
    panel._cost_task = None
    panel._catalog_generation = 0
    panel._tool_load_generation = 0
    panel._tool_detail_pending = False
    panel._form = form or FakeForm(values={"prompt": "a red barn"})
    panel._form_built = True
    panel._form_values = {}
    panel._image_state = {}
    panel._capture_state = {}
    panel._result_count = 1
    panel._account_carry = None
    panel.walls = []
    panel.cost = []
    panel.errors = []
    panel._render_wall = panel.walls.append
    panel._clear_wall = lambda: panel.walls.append(None)
    panel._set_cost = panel.cost.append
    panel._set_error = panel.errors.append
    panel._set_status = lambda _m: None
    return panel


class TestPricingFindsTheTeam:
    def test_a_blocked_personal_account_is_offered_the_team_that_can_run_it(self, monkeypatch):
        priced = PricedBy({"t1": True})
        monkeypatch.setattr(generate_api, "preview_cost", priced)
        panel = _panel(_session(PERSONAL, ALPHA))

        _run(panel._preview_cost())

        wall = panel.walls[-1]
        assert wall.variant == VARIANT_SWITCH_ACCOUNT
        assert wall.switch_to == "t1"
        assert priced.asked == ["p1", "t1"]

    def test_the_upgrade_prompt_is_never_drawn_first(self):
        """Asked before anything is drawn, so it never flashes and changes."""
        priced = PricedBy({"t1": True})
        panel = _panel(_session(PERSONAL, ALPHA))
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(generate_api, "preview_cost", priced)
            _run(panel._preview_cost())

        assert [w.variant for w in panel.walls] == [VARIANT_SWITCH_ACCOUNT]

    def test_it_stops_at_the_first_team_that_says_yes(self, monkeypatch):
        priced = PricedBy({"t1": True, "t2": True})
        monkeypatch.setattr(generate_api, "preview_cost", priced)
        panel = _panel(_session(PERSONAL, ALPHA, BETA))

        _run(panel._preview_cost())

        assert priced.asked == ["p1", "t1"]

    def test_a_team_that_fails_to_answer_is_passed_over(self, monkeypatch):
        priced = PricedBy({"t2": True}, failing={"t1"})
        monkeypatch.setattr(generate_api, "preview_cost", priced)
        panel = _panel(_session(PERSONAL, ALPHA, BETA))

        _run(panel._preview_cost())

        assert panel.walls[-1].switch_to == "t2"

    def test_no_team_able_leaves_todays_prompt(self, monkeypatch):
        priced = PricedBy({})
        monkeypatch.setattr(generate_api, "preview_cost", priced)
        panel = _panel(_session(PERSONAL, ALPHA))

        _run(panel._preview_cost())

        wall = panel.walls[-1]
        assert wall.variant == VARIANT_GET_TOKENS
        assert wall.message == FREEMIUM_REASON

    def test_no_teams_means_no_extra_requests(self, monkeypatch):
        priced = PricedBy({})
        monkeypatch.setattr(generate_api, "preview_cost", priced)
        panel = _panel(_session(PERSONAL))

        _run(panel._preview_cost())

        assert priced.asked == ["p1"]

    def test_an_unverified_caller_is_sent_to_verify_and_no_team_is_asked(self, monkeypatch):
        priced = PricedBy({"t1": True})
        monkeypatch.setattr(generate_api, "preview_cost", priced)
        panel = _panel(_session(PERSONAL, ALPHA, verified=False))

        _run(panel._preview_cost())

        assert panel.walls[-1].variant == VARIANT_VERIFY
        assert priced.asked == ["p1"]

    def test_a_blocked_selected_team_is_named_and_no_other_team_is_asked(self, monkeypatch):
        priced = PricedBy({"t2": True}, reason="Your balance of (0) is too low to run this tool")
        monkeypatch.setattr(generate_api, "preview_cost", priced)
        panel = _panel(_session(PERSONAL, ALPHA, BETA, selected="t1"))

        _run(panel._preview_cost())

        wall = panel.walls[-1]
        assert wall.variant == VARIANT_TEAM_BLOCKED
        assert wall.message == "alpha Team cannot run this tool."
        assert priced.asked == ["t1"]

    def test_a_runnable_estimate_takes_the_wall_down(self, monkeypatch):
        monkeypatch.setattr(generate_api, "preview_cost", PricedBy({"p1": True}))
        panel = _panel(_session(PERSONAL, ALPHA))

        _run(panel._preview_cost())

        assert panel.walls == [None]
        assert panel.cost == ["4 tokens"]

    def test_the_wall_is_not_taken_down_before_the_answer_lands(self, monkeypatch):
        """Clearing first made a blocked account's wall blink on every pause in
        typing."""
        cleared_before_answer = []

        async def priced(session, *_args, **_kwargs):
            cleared_before_answer.append(list(panel.walls))
            return {"can_run": False, "blocking_reason": FREEMIUM_REASON}

        monkeypatch.setattr(generate_api, "preview_cost", priced)
        panel = _panel(_session(PERSONAL))

        _run(panel._preview_cost())

        assert cleared_before_answer == [[]]

    def test_the_team_is_asked_about_what_a_switch_would_carry(self, monkeypatch):
        """Captures come along; personal library picks stay behind, so the team
        is priced without them."""
        capture = ImageSelection(reference=None, png=b"png", description="capture")
        picked = ImageSelection(reference={"kind": "UPLOAD", "id": "u1"}, description="upload")
        priced = PricedBy({"t1": True})
        monkeypatch.setattr(generate_api, "preview_cost", priced)
        panel = _panel(
            _session(PERSONAL, ALPHA),
            FakeForm(selections={"image": [capture, picked], "mask": [picked]}),
        )

        _run(panel._preview_cost())

        assert priced.selections["p1"] == {"image": [capture, picked], "mask": [picked]}
        assert priced.selections["t1"] == {"image": [capture]}


class TestSwitching:
    def _switchable(self, form=None):
        session = _session(PERSONAL, ALPHA)
        panel = _panel(session, form)
        changes = []
        panel._account_combo = FakeCombo(0, lambda: changes.append(panel._account_combo.index))
        panel._selected_tool = lambda: ToolSummary(id="tool-1", name="Flux 2 Dev")
        return panel, changes

    def test_the_switch_goes_through_the_picker(self):
        panel, changes = self._switchable()

        panel._switch_to_account("t1")

        assert changes == [1]

    def test_the_switch_waits_a_frame(self):
        """Setting the picker runs the whole account change, and inside the
        button's own click that can redraw the wall the button sits in."""
        panel, changes = self._switchable()
        booked = []
        panel._account_redraws = types.SimpleNamespace(
            request=lambda key, render: booked.append((key, render))
        )

        panel._switch_to_account("t1")

        assert changes == []
        assert panel._account_carry is None
        booked[0][1]()
        assert changes == [1]
        assert panel._account_carry.account_id == "t1"

    def test_the_carry_names_the_tool_on_the_form_not_the_one_loading(self):
        """`_selected_tool` already points at the next tool while it loads."""
        panel, _changes = self._switchable()
        panel._selected_tool = lambda: ToolSummary(id="tool-2", name="Other")

        carry = panel._carry_for("t1")

        assert carry.tool.id == "tool-1"

    def test_off_the_create_tab_the_snapshot_is_carried(self):
        panel, _changes = self._switchable()
        panel._form_built = False
        panel._form_values = {"prompt": "from the snapshot"}
        panel._image_state = {"image": [(1, None, "capture", b"png", "t.png", None)]}
        panel._capture_state = {"image": "viewport"}

        carry = panel._carry_for("t1")

        assert carry.values == {"prompt": "from the snapshot"}
        assert carry.image_state == panel._image_state
        assert carry.capture_sources == {"image": "viewport"}

    def test_it_carries_the_tool_the_values_and_the_captures_only(self):
        image_state = {
            "image": [
                (1, None, "capture", b"png", "thumb.png", "/World/Camera"),
                (2, {"kind": "LIBRARY", "id": "x"}, "from the library", None, None, None),
            ]
        }
        panel, _changes = self._switchable(FakeForm(values={"prompt": "barn"}, image_state=image_state))

        panel._switch_to_account("t1")

        carry = panel._account_carry
        assert carry.account_id == "t1"
        assert carry.tool_id == "tool-1"
        assert carry.values == {"prompt": "barn"}
        assert carry.image_state == {"image": [image_state["image"][0]]}
        assert carry.left_behind == 1

    def test_an_account_that_has_gone_says_so(self):
        panel, changes = self._switchable()

        panel._switch_to_account("gone")

        assert changes == []
        assert panel.errors and "BILLED TO" in panel.errors[-1]


class TestTheCarryLands:
    def _carry(self, **overrides):
        values = dict(
            account_id="t1",
            tool_id="tool-1",
            tool=ToolSummary(id="tool-1", name="Flux 2 Dev"),
            values={"prompt": "barn"},
            image_state={"image": [(1, None, "capture", b"png", "t.png", None)]},
            capture_sources={"image": "viewport"},
            left_behind=0,
        )
        values.update(overrides)
        return _AccountCarry(**values)

    def _loading_panel(self, monkeypatch, carry, selected_id="tool-1"):
        session = _session(PERSONAL, ALPHA, selected="t1")
        form = FakeForm(values={"prompt": "something typed on the old tool"})
        panel = _panel(session, form)
        panel._tool_detail = None
        panel._account_carry = carry
        panel._tools = [ToolSummary(id="tool-1", name="Flux 2 Dev"), ToolSummary(id="tool-2", name="Other")]
        panel._selected_tool_id = selected_id
        panel._capture_frame = None
        panel._chosen_assets = {}
        panel._sync_viewport_watch = lambda: None
        panel._schedule_cost_refresh = lambda: None

        class Frame:
            def clear(self):
                return None

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return None

        panel._form_frame = Frame()
        monkeypatch.setattr(ui, "Label", lambda *a, **k: None, raising=False)

        async def get_tool(_session, tool_id):
            return ToolDetail(id=tool_id, name="Flux 2 Dev", tool_fields_hash="h1")

        monkeypatch.setattr(tools_api, "get_tool", get_tool)
        return panel, form

    def test_the_form_comes_back_on_the_new_account(self, monkeypatch):
        panel, form = self._loading_panel(monkeypatch, self._carry())

        _run(panel._load_tool_detail())

        assert form.built_with == ("tool-1", {"prompt": "barn"})
        assert form.restored_images == {"image": [(1, None, "capture", b"png", "t.png", None)]}
        assert form.restored_sources == {"image": "viewport"}
        assert panel._account_carry is None
        assert panel.errors == []

    def test_images_left_behind_are_mentioned_even_with_the_status_line_off(self, monkeypatch):
        panel, _form = self._loading_panel(monkeypatch, self._carry(left_behind=2))

        _run(panel._load_tool_detail())

        assert panel.errors == [
            "Images you chose from your personal library or uploads were not "
            "carried over to alpha Team."
        ]

    def test_a_carry_for_an_account_no_longer_selected_is_not_applied(self, monkeypatch):
        panel, form = self._loading_panel(monkeypatch, self._carry())
        panel._session.selected_account_id = None

        _run(panel._load_tool_detail())

        assert form.built_with == ("tool-1", {})
        assert panel._account_carry is None

    def test_a_carry_for_another_tool_is_not_applied(self, monkeypatch):
        panel, form = self._loading_panel(monkeypatch, self._carry(), selected_id="tool-2")

        _run(panel._load_tool_detail())

        assert form.built_with == ("tool-2", {})
        assert panel._account_carry is None


class TestPickerChoice:
    def _panel(self, session, index, carry):
        panel = _panel(session)
        panel._account_combo = FakeCombo(index, lambda: None)
        panel._current_tab = 0
        panel._tools = []
        panel._kits = []
        panel._tool_tags = {}
        panel._selected_tool_id = None
        panel._account_carry = carry
        panel._forget_browsing = lambda: None
        panel._torn_out = {"library": False, "uploads": False}
        panel._windows = {}
        panel._rebuild_surface = lambda _surface: None
        panel._show_tab = lambda _index: None
        panel._check_plugin_access = lambda: False
        panel._cost_task = None
        return panel

    def test_a_switch_the_wall_offered_keeps_the_tool(self):
        carry = _AccountCarry(account_id="t1", tool_id="tool-1")
        panel = self._panel(_session(PERSONAL, ALPHA), 1, carry)

        panel._on_account_changed()

        assert panel._selected_tool_id == "tool-1"
        assert panel._account_carry is carry

    def test_choosing_some_other_account_in_the_picker_drops_the_carry(self):
        carry = _AccountCarry(account_id="t2", tool_id="tool-1")
        panel = self._panel(_session(PERSONAL, ALPHA, BETA), 1, carry)

        panel._on_account_changed()

        assert panel._selected_tool_id is None
        assert panel._account_carry is None


class TestARefusedRun:
    def _job_panel(self, monkeypatch, session):
        panel = _panel(session)
        panel._jobs = types.SimpleNamespace(update=lambda *_a, **_k: None)
        monkeypatch.setattr(tools_api, "build_inputs", lambda *a, **k: {})
        monkeypatch.setattr(tools_api, "missing_required_fields", lambda *a, **k: [])
        return panel

    def test_a_free_personal_plan_is_pointed_at_the_team(self, monkeypatch):
        session = _session(PERSONAL, ALPHA)
        panel = self._job_panel(monkeypatch, session)

        async def refuse(*_a, **_k):
            raise ApiError(403, "FREEMIUM_API_NOT_ALLOWED", FREEMIUM_REASON)

        monkeypatch.setattr(generate_api, "submit", refuse)
        monkeypatch.setattr(generate_api, "preview_cost", PricedBy({"t1": True}))

        with pytest.raises(RuntimeError, match="personal account cannot run"):
            _run(panel._run_job(object(), TOOL, {}, {}, 1, {}, PERSONAL))

        assert panel.walls[-1].variant == VARIANT_SWITCH_ACCOUNT

    def test_a_refusal_for_an_account_since_left_is_not_drawn(self, monkeypatch):
        """The run was sent on Personal; by the time it was refused the person
        had moved to the team. Drawing Personal's wall over the team's form
        would tell them the wrong account's problem."""
        session = _session(PERSONAL, ALPHA, selected="t1")
        panel = self._job_panel(monkeypatch, session)

        async def refuse(*_a, **_k):
            raise ApiError(402, "INSUFFICIENT_BALANCE", "Too few tokens.")

        monkeypatch.setattr(generate_api, "submit", refuse)
        monkeypatch.setattr(generate_api, "preview_cost", PricedBy({}))

        with pytest.raises(RuntimeError):
            _run(panel._run_job(object(), TOOL, {}, {}, 1, {}, PERSONAL))

        assert panel.walls == []

    def test_a_refusal_for_a_tool_no_longer_on_the_form_is_not_drawn(self, monkeypatch):
        """Generate on one tool, then pick another before the refusal lands:
        a Switch drawn over the second tool would carry it to a team that was
        only ever asked about the first."""
        session = _session(PERSONAL, ALPHA)
        panel = self._job_panel(monkeypatch, session)
        panel._tool_detail = ToolDetail(id="tool-2", name="Other", tool_fields_hash="h2")

        async def refuse(*_a, **_k):
            raise ApiError(403, "FREEMIUM_API_NOT_ALLOWED", FREEMIUM_REASON)

        monkeypatch.setattr(generate_api, "submit", refuse)
        monkeypatch.setattr(generate_api, "preview_cost", PricedBy({"t1": True}))

        with pytest.raises(RuntimeError):
            _run(panel._run_job(object(), TOOL, {}, {}, 1, {}, PERSONAL))

        assert panel.walls == []

    def test_the_run_is_billed_to_the_account_chosen_at_the_click(self, monkeypatch):
        session = _session(PERSONAL, ALPHA, selected="t1")
        panel = self._job_panel(monkeypatch, session)
        billed = []

        async def submit(*_a, account=None, **_k):
            billed.append(account)
            raise ApiError(400, "INVALID_REQUEST", "stop here")

        monkeypatch.setattr(generate_api, "submit", submit)

        with pytest.raises(ApiError):
            _run(panel._run_job(object(), TOOL, {}, {}, 1, {}, PERSONAL))

        assert billed == [PERSONAL]

    def test_an_unrelated_failure_is_left_alone(self, monkeypatch):
        session = _session(PERSONAL, ALPHA)
        panel = self._job_panel(monkeypatch, session)

        async def refuse(*_a, **_k):
            raise ApiError(400, "INVALID_REQUEST", "Bad input.")

        monkeypatch.setattr(generate_api, "submit", refuse)

        with pytest.raises(ApiError):
            _run(panel._run_job(object(), TOOL, {}, {}, 1, {}, PERSONAL))

        assert panel.walls == []


class TestWhereAWallShows:
    """A wall about a run means nothing over the Library or the Account tab;
    one about the account (the plugin is not enabled for it) applies to all."""

    def _panel(self, tab):
        panel = object.__new__(RunDiffusionPanel)
        panel._wall_frame = types.SimpleNamespace(visible=False)
        panel._wall_up = False
        panel._wall_create_only = False
        panel._current_tab = tab
        return panel

    def test_a_run_wall_hides_off_the_create_tab_and_comes_back(self):
        from rundiffusion_omniverse.panel import TAB_ACCOUNT, TAB_CREATE

        panel = self._panel(TAB_CREATE)
        panel._wall_up, panel._wall_create_only = True, True
        panel._sync_wall_visibility()
        assert panel._wall_frame.visible

        panel._current_tab = TAB_ACCOUNT
        panel._sync_wall_visibility()
        assert not panel._wall_frame.visible

        panel._current_tab = TAB_CREATE
        panel._sync_wall_visibility()
        assert panel._wall_frame.visible

    def test_an_account_wall_shows_everywhere(self):
        from rundiffusion_omniverse.panel import TAB_ACCOUNT

        panel = self._panel(TAB_ACCOUNT)
        panel._wall_up, panel._wall_create_only = True, False
        panel._sync_wall_visibility()

        assert panel._wall_frame.visible

    def test_a_cleared_wall_stays_hidden(self):
        from rundiffusion_omniverse.panel import TAB_CREATE

        panel = self._panel(TAB_CREATE)
        panel._wall_up = True
        panel._clear_wall()

        assert not panel._wall_frame.visible
        assert not panel._wall_up


def test_the_personal_estimate_button_fits_either_reason():
    """The server's reason may be the plan or the balance and is never parsed,
    so the button does not answer one under a sentence about the other."""
    wall = wall_for_preview(
        can_run=False, blocking_reason=FREEMIUM_REASON, is_personal=True, email_verified=True
    )

    assert wall.action_label == "See plans and tokens"
    assert wall.action_url == SUBSCRIPTIONS_URL


class TestAnAnswerForAnAccountSinceLeft:
    """The person changed the account while the estimate, or the team check
    after it, was in the air. Its wall belongs to the account they left."""

    def test_a_late_estimate_draws_nothing(self, monkeypatch):
        session = _session(PERSONAL, ALPHA)
        panel = _panel(session)

        async def priced(*_a, **_k):
            session.selected_account_id = "t1"
            return {"can_run": False, "blocking_reason": FREEMIUM_REASON}

        monkeypatch.setattr(generate_api, "preview_cost", priced)

        _run(panel._preview_cost())

        assert panel.walls == []
        assert panel.cost == []

    def test_a_late_team_check_draws_nothing(self, monkeypatch):
        session = _session(PERSONAL, ALPHA)
        panel = _panel(session)
        priced = PricedBy({"t1": True})

        async def switching(session_, tool, values, num_results=1, image_selections=None, account=None):
            result = await priced(session_, tool, values, num_results, image_selections, account)
            if account is not None:
                session.selected_account_id = "t1"
            return result

        monkeypatch.setattr(generate_api, "preview_cost", switching)

        _run(panel._preview_cost())

        assert panel.walls == []

    def test_changing_account_cancels_the_estimate_in_flight(self):
        class Task:
            cancelled = False

            def cancel(self):
                Task.cancelled = True

        panel = TestPickerChoice()._panel(_session(PERSONAL, ALPHA), 1, None)
        panel._cost_task = Task()

        panel._on_account_changed()

        assert Task.cancelled
        assert panel._cost_task is None


class TestTeamAnswersAreKept:
    """Returning to the Create tab, or re-pricing an unchanged form, does not
    ask every team again."""

    def test_an_unchanged_question_is_answered_from_what_was_heard(self, monkeypatch):
        priced = PricedBy({"t1": True})
        monkeypatch.setattr(generate_api, "preview_cost", priced)
        panel = _panel(_session(PERSONAL, ALPHA))

        _run(panel._preview_cost())
        _run(panel._preview_cost())

        assert priced.asked == ["p1", "t1", "p1"]
        assert panel.walls[-1].switch_to == "t1"

    def test_a_changed_form_asks_again(self, monkeypatch):
        priced = PricedBy({"t1": True})
        monkeypatch.setattr(generate_api, "preview_cost", priced)
        form = FakeForm(values={"prompt": "a"})
        panel = _panel(_session(PERSONAL, ALPHA), form)

        _run(panel._preview_cost())
        form.values = {"prompt": "b"}
        _run(panel._preview_cost())

        assert priced.asked == ["p1", "t1", "p1", "t1"]

    def test_an_old_answer_is_not_trusted(self, monkeypatch):
        from rundiffusion_omniverse import panel as panel_module

        priced = PricedBy({"t1": True})
        monkeypatch.setattr(generate_api, "preview_cost", priced)
        panel = _panel(_session(PERSONAL, ALPHA))
        clock = [1000.0]
        monkeypatch.setattr(panel_module.time, "monotonic", lambda: clock[0])

        _run(panel._preview_cost())
        clock[0] += panel_module.TEAM_ANSWER_SECONDS + 1
        _run(panel._preview_cost())

        assert priced.asked == ["p1", "t1", "p1", "t1"]



class TestTheSwitchEndToEnd:
    """Press Switch, let the account change and the new catalogue land, and
    the form is back on the team: the whole path, not its pieces."""

    def test_tool_and_prompt_arrive_on_the_team(self, monkeypatch):
        session = _session(PERSONAL, ALPHA)
        form = FakeForm(values={"prompt": "barn"})
        panel = _panel(session, form)
        panel._current_tab = 0
        panel._kits = []
        panel._tool_tags = {}
        panel._selected_tool_id = "tool-1"
        panel._forget_browsing = lambda: None
        panel._torn_out = {"library": False, "uploads": False}
        panel._windows = {}
        panel._rebuild_surface = lambda _surface: None
        panel._show_tab = lambda _index: None
        panel._check_plugin_access = lambda: True
        spawned = []
        panel._spawn = spawned.append
        panel._account_combo = FakeCombo(0, panel._on_account_changed)
        panel._render_tool_picker = lambda: None
        panel._tool_browser = types.SimpleNamespace(update_tools=lambda _tools: None)
        panel._capture_frame = None
        panel._chosen_assets = {}
        panel._sync_viewport_watch = lambda: None
        panel._schedule_cost_refresh = lambda: None

        class Frame:
            def clear(self):
                return None

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return None

        panel._form_frame = Frame()
        monkeypatch.setattr(ui, "Label", lambda *a, **k: None, raising=False)

        async def list_all_tools(_session, on_page=None):
            # The team's listing does not carry the tool: it is adopted.
            listed = [ToolSummary(id="tool-9", name="Team tool")]
            if on_page is not None:
                on_page(listed)
            return listed

        async def get_tool(_session, tool_id):
            return ToolDetail(id=tool_id, name="Flux 2 Dev", tool_fields_hash="h1")

        monkeypatch.setattr(tools_api, "list_all_tools", list_all_tools)
        monkeypatch.setattr(tools_api, "get_tool", get_tool)

        panel._switch_to_account("t1")
        assert session.selected_account_id == "t1"
        for coroutine in spawned:
            coroutine.close()
        spawned.clear()

        _run(panel._load_tools())
        assert "tool-1" in [t.id for t in panel._tools]
        load_detail = [c for c in spawned if c.cr_code.co_name == "_load_tool_detail"]
        assert load_detail, "loading the tools goes on to load the carried tool"
        for coroutine in spawned:
            if coroutine not in load_detail:
                coroutine.close()
        _run(load_detail[0])

        assert form.built_with == ("tool-1", {"prompt": "barn"})
        assert panel._account_carry is None


# -- account changes while a run is in the air ------------------------------


class TestARunIsFollowedOnItsOwnAccount:
    """The server answers a run's status only to the account that started it.
    Polling as whichever account was selected turned a mid-run account change
    into "not found": the job showed as failed while the run was still going,
    and its results never arrived."""

    def test_every_poll_names_the_account_the_run_was_sent_under(self, monkeypatch):
        session = _session(PERSONAL, ALPHA, selected="t1")
        asked = []

        async def request_json_async(method, url, headers=None, body=None):
            asked.append(url)
            # The person changes account while the run is in the air.
            session.selected_account_id = None
            done = len(asked) > 1
            return {"status": "succeeded" if done else "running", "retry_after_seconds": 0}

        async def headers():
            return {}

        async def no_wait(_seconds):
            return None

        session.auth_headers = headers
        monkeypatch.setattr(generate_api, "request_json_async", request_json_async)
        monkeypatch.setattr(generate_api.asyncio, "sleep", no_wait)

        _run(generate_api.poll_until_done(session, "req-1", account=ALPHA))

        assert len(asked) == 2
        assert all("team_id=t1" in url for url in asked)

    def test_the_job_polls_with_the_click_time_account(self, monkeypatch):
        session = _session(PERSONAL, ALPHA, selected="t1")
        panel = TestARefusedRun()._job_panel(monkeypatch, session)
        polled = []

        async def submit(*_a, **_k):
            return "req-1"

        async def poll(_session, _request_id, on_status=None, account=None):
            polled.append(account)
            raise RuntimeError("stop here")

        monkeypatch.setattr(generate_api, "submit", submit)
        monkeypatch.setattr(generate_api, "poll_until_done", poll)
        job = types.SimpleNamespace(request_id=None)

        with pytest.raises(RuntimeError, match="stop here"):
            _run(panel._run_job(job, TOOL, {}, {}, 1, {}, ALPHA))

        assert polled == [ALPHA]


class _Frame:
    def clear(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None


def _detail_panel(monkeypatch, *, carry=None):
    session = _session(PERSONAL, ALPHA, selected="t1")
    form = FakeForm(values={"prompt": "barn"})
    panel = _panel(session, form)
    panel._tool_detail = None
    panel._account_carry = carry
    panel._tools = [ToolSummary(id="tool-1", name="Flux 2 Dev"), ToolSummary(id="tool-2", name="Other")]
    panel._selected_tool_id = "tool-1"
    panel._form_frame = _Frame()
    panel._capture_frame = None
    panel._chosen_assets = {}
    panel._sync_viewport_watch = lambda: None
    panel._schedule_cost_refresh = lambda: None
    monkeypatch.setattr(ui, "Label", lambda *a, **k: None, raising=False)
    return panel, form


class TestALateToolLoad:
    """A tool's inputs come back after the person has moved on. Drawn then,
    that tool (and a form carried with it) landed on whatever the panel showed
    by that time: another account, another tool, or the next person to sign
    in."""

    def _slow_get_tool(self, monkeypatch, meanwhile):
        async def get_tool(_session, tool_id):
            meanwhile()
            return ToolDetail(id=tool_id, name="Flux 2 Dev", tool_fields_hash="h1")

        monkeypatch.setattr(tools_api, "get_tool", get_tool)

    def test_an_account_change_while_loading_drops_it(self, monkeypatch):
        carry = _AccountCarry(
            account_id="t1", tool_id="tool-1", values={"prompt": "barn"},
            image_state={"image": [(1, None, "c", b"png", "t.png", None)]},
        )
        panel, form = _detail_panel(monkeypatch, carry=carry)

        def account_changed():
            panel._session.selected_account_id = None
            panel._tool_load_generation += 1

        self._slow_get_tool(monkeypatch, account_changed)

        _run(panel._load_tool_detail())

        assert panel._tool_detail is None
        assert form.built_with is None
        assert form.restored_images is None

    def test_a_newer_tool_load_wins_over_an_older_one(self, monkeypatch):
        panel, form = _detail_panel(monkeypatch)
        self._slow_get_tool(monkeypatch, lambda: setattr(panel, "_tool_load_generation", 99))

        _run(panel._load_tool_detail())

        assert panel._tool_detail is None
        assert form.built_with is None

    def test_a_load_that_is_still_current_draws(self, monkeypatch):
        panel, form = _detail_panel(monkeypatch)
        self._slow_get_tool(monkeypatch, lambda: None)

        _run(panel._load_tool_detail())

        assert panel._tool_detail.id == "tool-1"
        assert form.built_with == ("tool-1", {})
        assert panel._tool_detail_pending is False

    def test_starting_a_load_cancels_the_estimate_for_the_tool_it_replaces(self, monkeypatch):
        panel, _form = _detail_panel(monkeypatch)
        cancelled = []
        panel._cost_task = types.SimpleNamespace(cancel=lambda: cancelled.append(True))
        self._slow_get_tool(monkeypatch, lambda: None)

        _run(panel._load_tool_detail())

        assert cancelled == [True]

    def test_a_late_catalogue_for_an_account_since_left_is_dropped(self, monkeypatch):
        panel, _form = _detail_panel(monkeypatch)
        panel._tools = []
        panel._tool_browser = types.SimpleNamespace(update_tools=lambda _tools: None)
        spawned = []
        panel._spawn = spawned.append

        async def list_all_tools(_session, on_page=None):
            panel._catalog_generation += 1
            listed = [ToolSummary(id="old-account-tool", name="Old")]
            if on_page is not None:
                on_page(listed)
            return listed

        monkeypatch.setattr(tools_api, "list_all_tools", list_all_tools)

        _run(panel._load_tools())

        assert panel._tools == []
        assert spawned == []


class TestAnEstimateForAToolSinceReplaced:
    def test_it_draws_nothing(self, monkeypatch):
        panel = _panel(_session(PERSONAL, ALPHA))

        async def priced(*_a, **_k):
            panel._tool_detail = ToolDetail(id="tool-2", name="Other", tool_fields_hash="h2")
            return {"can_run": False, "blocking_reason": FREEMIUM_REASON}

        monkeypatch.setattr(generate_api, "preview_cost", priced)

        _run(panel._preview_cost())

        assert panel.walls == []
        assert panel.cost == []


class TestCreateOpenedWithoutItsTool:
    """The catalogue arrived while another tab was open (BILLED TO changed on
    the Account tab), and only an open Create tab started a tool's load. Create
    then named a tool, showed none of its inputs, and Generate said none was
    chosen."""

    def _built(self, monkeypatch, *, tools, detail=None, pending=False):
        from rundiffusion_omniverse import panel as panel_module

        panel = object.__new__(RunDiffusionPanel)
        panel._tools = tools
        panel._tool_detail = detail
        panel._tool_detail_pending = pending
        spawned = []
        panel._spawn = spawned.append
        panel._sync_viewport_watch = lambda: None
        # Only the tail of the tab build matters here: the part that decides
        # whether the inputs still need fetching.
        tail = panel_module.RunDiffusionPanel._ensure_tool_loaded
        tail(panel)
        for coroutine in spawned:
            coroutine.close()
        return spawned

    def test_it_starts_the_load(self, monkeypatch):
        spawned = self._built(monkeypatch, tools=[ToolSummary(id="a", name="A")])

        assert len(spawned) == 1

    def test_not_when_one_is_already_on_its_way(self, monkeypatch):
        spawned = self._built(monkeypatch, tools=[ToolSummary(id="a", name="A")], pending=True)

        assert spawned == []

    def test_not_when_the_inputs_are_already_there(self, monkeypatch):
        spawned = self._built(monkeypatch, tools=[ToolSummary(id="a", name="A")], detail=TOOL)

        assert spawned == []

    def test_not_before_there_is_a_catalogue(self, monkeypatch):
        assert self._built(monkeypatch, tools=[]) == []


class TestAPickerChoiceWaitsAFrame:
    """Chosen in the dropdown, an account change runs inside the dropdown's own
    event, and its redraws (the Account tab, a torn-out window, the wall) are
    container rebuilds omni.ui refuses mid-dispatch."""

    def test_state_changes_now_and_the_drawing_later(self):
        session = _session(PERSONAL, ALPHA)
        panel = TestPickerChoice()._panel(session, 1, None)
        booked = []
        panel._account_redraws = types.SimpleNamespace(
            request=lambda key, render: booked.append((key, render))
        )
        shown = []
        panel._current_tab = 3
        panel._show_tab = shown.append

        panel._on_account_changed()

        assert session.selected_account_id == "t1"
        assert shown == []
        booked[0][1]()
        assert shown == [3]

    def test_it_is_not_on_the_queue_a_tab_switch_cancels(self):
        """A tab switch cancels the panel's frame-deferred redraws; dropping an
        account change there would leave BILLED TO naming one account while the
        panel showed another's tools."""
        session = _session(PERSONAL, ALPHA)
        panel = TestPickerChoice()._panel(session, 1, None)
        panel._redraws = types.SimpleNamespace(request=lambda *_a: pytest.fail("cancellable queue"))
        booked = []
        panel._account_redraws = types.SimpleNamespace(request=lambda key, render: booked.append(key))

        panel._on_account_changed()

        assert booked == ["account-change"]
