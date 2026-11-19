"""A team role that says no is named before anything is spent.

A team's role can switch generating off for everyone holding it, or hide some
tools from them. The server refuses a run on such a role before it will even
price one, and that refusal used to reach the panel as an elided "Cost
unavailable" in the cost line and then as a generic job failure. Neither named
the team, and neither said who could change the role.

So the role is read up front, from the account list the panel already holds:
Generate waits until the role is known, a role without generating gets a wall
naming its team and the people who manage it, and the tools a role blocks are
hidden wherever the panel lists tools, kits included, because a kit carries
copies of catalogue rows. The refusal itself is still mapped, for a role that
changed after the account list was read.
"""

from __future__ import annotations

import asyncio
import contextlib
import types

import omni.ui as ui
import pytest

from rundiffusion_omniverse import dialogs as dialogs_module
from rundiffusion_omniverse import panel as panel_module
from rundiffusion_omniverse.api import generate as generate_api
from rundiffusion_omniverse.api import kits as kits_api
from rundiffusion_omniverse.api import tools as tools_api
from rundiffusion_omniverse.api.kits import Kit, KitTool, parse_kits, visible_kits
from rundiffusion_omniverse.api.session import Account, RdSession
from rundiffusion_omniverse.api.token_wall import (
    VARIANT_GET_TOKENS,
    VARIANT_ROLE_BLOCKED,
    blocked_tool_message,
    is_generate_refusal,
    role_blocked_wall,
    role_wall,
    teams_worth_checking,
    wall_for_error,
)
from rundiffusion_omniverse.api.tools import ToolDetail, ToolSummary, tool_tag_ids
from rundiffusion_omniverse.api.transport import ApiError
from rundiffusion_omniverse.panel import RunDiffusionPanel, _AccountCarry

TOOL = ToolDetail(id="tool-1", name="Flux 2 Dev", tool_fields_hash="h1")

#: The refusal exactly as the API sends it: the permission is named in
#: `details`, in the same vocabulary the account list uses.
GENERATE_DETAILS = {"required_permission": "GENERATE"}


def _generate_refusal() -> ApiError:
    return ApiError(
        403,
        "PERMISSION_DENIED",
        "Permission GENERATE is required for this action.",
        dict(GENERATE_DETAILS),
    )


def _personal() -> Account:
    return Account(
        id="p1", label="Personal account", kind="PERSONAL",
        permissions={"USE_PLUGINS": True, "GENERATE": True, "BLOCK_TOOLS": False},
        plugin_kinds=["*"],
    )


def _team(
    team_id: str = "t1",
    label: str | None = "alpha Team",
    *,
    generate: bool = True,
    blocked_ids=(),
    blocked_tags=(),
    team_label: str = "Team",
) -> Account:
    """A team account as the account list publishes it, fresh for each test."""
    blocking = bool(blocked_ids or blocked_tags)
    settings = {}
    if blocking:
        settings["restrict_tools_settings"] = {
            "tool_ids": list(blocked_ids),
            "tool_tags": {tag: True for tag in blocked_tags},
        }
    return Account(
        id=team_id, label=label, kind="TEAM",
        permissions={"USE_PLUGINS": True, "GENERATE": generate, "BLOCK_TOOLS": blocking},
        permission_settings=settings,
        plugin_kinds=["*"],
        team_label=team_label,
    )


def _run(coroutine):
    return asyncio.new_event_loop().run_until_complete(coroutine)


class OfflineSession:
    """Only what a request asks of a session, with no token to refresh, so a
    test that reads a response never reaches the network."""

    def account_params(self, _account=None) -> dict:
        return {}

    def account_request(self, _account=None):
        return contextlib.nullcontext()

    async def auth_headers(self) -> dict:
        return {}


def _all_copy(wall) -> str:
    return " ".join(part for part in (wall.message, wall.detail, wall.action_label) if part)


# -- the pure rules ---------------------------------------------------------


class TestTheRefusalIsRead:
    def test_a_refusal_naming_generate_is_the_roles(self):
        assert is_generate_refusal("PERMISSION_DENIED", GENERATE_DETAILS)

    @pytest.mark.parametrize(
        "code,details",
        [
            # The same code is used for other permissions, which this wall
            # would misdescribe.
            ("PERMISSION_DENIED", {"required_permission": "SHARE_ASSETS"}),
            ("PERMISSION_DENIED", {}),
            ("PERMISSION_DENIED", None),
            ("PERMISSION_DENIED", "GENERATE"),
            ("INSUFFICIENT_BALANCE", GENERATE_DETAILS),
        ],
    )
    def test_nothing_else_is(self, code, details):
        assert not is_generate_refusal(code, details)

    def test_a_failed_generate_names_the_team_and_its_administrators(self):
        wall = wall_for_error(
            "PERMISSION_DENIED", 403, details=GENERATE_DETAILS,
            is_personal=False, email_verified=True, account_name="alpha Team",
        )

        assert wall.variant == VARIANT_ROLE_BLOCKED
        assert wall.message == "alpha Team does not allow your role to generate."
        assert "your account administrators" in wall.detail
        assert "BILLED TO" in wall.detail
        assert wall.action_label is None and wall.action_url is None
        assert wall.switch_to is None

    def test_verifying_an_email_would_not_fix_it_so_the_role_is_named(self):
        """The role is checked before anything else, so this refusal arrives
        whether or not the email is verified, and only the role can lift it."""
        wall = wall_for_error(
            "PERMISSION_DENIED", 403, details=GENERATE_DETAILS,
            is_personal=False, email_verified=False, account_name="alpha Team",
        )

        assert wall.variant == VARIANT_ROLE_BLOCKED

    def test_another_permission_is_left_as_it_was(self):
        assert (
            wall_for_error(
                "PERMISSION_DENIED", 403, details={"required_permission": "SHARE_ASSETS"},
                is_personal=False, email_verified=True, account_name="alpha Team",
            )
            is None
        )

    def test_a_nameless_team_is_called_what_its_company_calls_teams(self):
        wall = role_blocked_wall(account_name=None, team_label="Studio")

        assert wall.message.startswith("This Studio does not allow")

    def test_a_personal_account_is_not_described_as_a_team(self):
        wall = role_blocked_wall(account_name="Personal", is_personal=True)

        assert "role" not in wall.message
        assert "administrators" not in _all_copy(wall)

    def test_no_copy_has_an_em_dash(self):
        copy = [
            _all_copy(role_blocked_wall(account_name="alpha Team")),
            _all_copy(role_blocked_wall(account_name=None, is_personal=True)),
            blocked_tool_message(tool_name="Flux 2 Dev", account_name="alpha Team"),
            blocked_tool_message(tool_name="Flux 2 Dev", account_name=None, is_personal=True),
        ]
        for text in copy:
            assert chr(0x2014) not in text


class TestTheRoleIsReadUpFront:
    def test_a_role_without_generating_has_its_wall_before_any_request(self):
        wall = role_wall(_team(generate=False), account_name="alpha Team")

        assert wall.variant == VARIANT_ROLE_BLOCKED
        assert "alpha Team" in wall.message

    def test_the_wall_takes_the_teams_own_name_when_none_is_given(self):
        wall = role_wall(_team(label=None, generate=False, team_label="Studio"))

        assert wall.message.startswith("This Studio ")

    def test_a_role_that_generates_has_none(self):
        assert role_wall(_team()) is None

    def test_no_account_has_none(self):
        assert role_wall(None) is None

    def test_a_role_not_yet_known_has_no_wall_and_cannot_generate(self):
        """Nothing to name yet, and nothing to allow: Generate waits."""
        loading = Account(id="t1", label="alpha Team", kind="TEAM")

        assert role_wall(loading) is None
        assert not loading.role_is_known
        assert not loading.can_generate

    def test_generating_needs_an_explicit_yes(self):
        assert Account(id="p1", label="P", kind="PERSONAL", permissions={"GENERATE": True}).can_generate
        assert not Account(id="p1", label="P", kind="PERSONAL", permissions={"USE_PLUGINS": True}).can_generate
        assert not _team(generate=False).can_generate


class TestBlockedTools:
    def test_a_listed_tool_is_blocked(self):
        team = _team(blocked_ids=["tool-1"])

        assert team.blocks_tool(ToolSummary(id="tool-1", name="Flux 2 Dev"))
        assert not team.blocks_tool(ToolSummary(id="tool-2", name="Other"))

    def test_a_tool_carrying_a_blocked_tag_is_blocked(self):
        team = _team(blocked_tags=["video"])

        assert team.blocks_tool(ToolSummary(id="t", name="T", tool_tag_ids=("image", "video")))
        assert not team.blocks_tool(ToolSummary(id="t", name="T", tool_tag_ids=("image",)))
        assert not team.blocks_tool(ToolSummary(id="t", name="T"))

    def test_a_tag_set_false_blocks_nothing(self):
        team = _team(blocked_tags=["video"])
        team.permission_settings["restrict_tools_settings"]["tool_tags"]["video"] = False

        assert not team.blocks_tool(ToolSummary(id="t", name="T", tool_tag_ids=("video",)))

    def test_settings_without_the_restriction_on_block_nothing(self):
        """A stale list must never hide a tool the role allows."""
        team = _team(blocked_ids=["tool-1"])
        team.permissions = {**team.permissions, "BLOCK_TOOLS": False}

        assert not team.blocks_tool(ToolSummary(id="tool-1", name="Flux 2 Dev"))

    def test_the_restriction_on_without_settings_blocks_nothing(self):
        team = _team()
        team.permissions = {**team.permissions, "BLOCK_TOOLS": True}
        team.permission_settings = {"restrict_tools_settings": None}

        assert not team.blocks_tool(ToolSummary(id="tool-1", name="Flux 2 Dev"))

    def test_the_rule_reads_a_tools_inputs_too(self):
        team = _team(blocked_tags=["video"])
        detail = ToolDetail(id="tool-1", name="Flux 2 Dev", tool_fields_hash="h", tool_tag_ids=("video",))

        assert team.blocks_tool(detail)

    def test_no_tool_is_not_blocked(self):
        assert not _team(blocked_ids=["tool-1"]).blocks_tool(None)


class TestWhatTheServerSendsIsRead:
    def test_tag_ids_are_taken_from_whole_tags_and_from_bare_ids(self):
        """A bare id is still read: failing open here would show a tool the
        team blocked."""
        raw = [{"id": "a", "type": "MEDIA", "label": "Image"}, "b", {"type": "X"}, None, 3, ""]

        assert tool_tag_ids(raw) == ("a", "b")
        assert tool_tag_ids(None) == ()

    def test_the_catalogue_carries_each_tools_tags(self, monkeypatch):
        async def answer(_method, _url, **_kwargs):
            return {"data": [{"id": "t1", "name": "T1", "tool_tags": [{"id": "video", "type": "MEDIA"}]}]}

        monkeypatch.setattr(tools_api, "request_json_async", answer)

        page = _run(tools_api.list_tools(OfflineSession()))

        assert page.items[0].tool_tag_ids == ("video",)

    def test_a_tools_inputs_carry_its_tags(self, monkeypatch):
        async def answer(_method, _url, **_kwargs):
            return {"id": "t1", "name": "T1", "tool_fields_hash": "h", "fields": [], "tool_tags": [{"id": "video"}]}

        monkeypatch.setattr(tools_api, "request_json_async", answer)

        detail = _run(tools_api.get_tool(OfflineSession(), "t1"))

        assert detail.tool_tag_ids == ("video",)

    def test_a_kits_tools_carry_their_tags(self):
        [kit] = parse_kits(
            {"data": [{"id": "k", "label": "K", "tools": [{"tool": {"id": "t1", "tool_tags": [{"id": "video"}]}}]}]}
        )

        assert kit.tools[0].tool.tool_tag_ids == ("video",)

    def test_the_account_list_carries_the_roles_settings(self):
        session = RdSession()
        restrict = {"tool_ids": ["tool-1"], "tool_tags": {"video": True}}

        session.adopt_identity(
            {
                "user": {},
                "accounts": [
                    {
                        "id": "t1", "label": "alpha Team", "kind": "TEAM",
                        "permissions": {"GENERATE": True, "BLOCK_TOOLS": True},
                        "permission_settings": {"restrict_tools_settings": restrict},
                    },
                    {"id": "p1", "kind": "PERSONAL", "permission_settings": None},
                ],
            }
        )

        team, personal = session.accounts
        assert team.permission_settings == {"restrict_tools_settings": restrict}
        assert team.blocks_tool(ToolSummary(id="tool-1", name="Flux 2 Dev"))
        assert personal.permission_settings == {}


class TestKitsHideBlockedTools:
    """A kit carries copies of catalogue rows, so hiding a tool from the flat
    list alone would leave it one kit card away."""

    def _kit(self, *tool_ids, default=None) -> Kit:
        return Kit(
            id="k", label="K", default_tool_id=default,
            tools=tuple(KitTool(tool=ToolSummary(id=t, name=t)) for t in tool_ids),
        )

    def test_a_blocked_tool_is_taken_out_of_its_kit(self):
        [kit] = visible_kits([self._kit("a", "blocked", "b")], lambda tool: tool.id == "blocked")

        assert [entry.tool.id for entry in kit.tools] == ["a", "b"]

    def test_a_kit_left_empty_is_dropped(self):
        kits = visible_kits([self._kit("blocked"), self._kit("a")], lambda tool: tool.id == "blocked")

        assert [[entry.tool.id for entry in kit.tools] for kit in kits] == [["a"]]

    def test_a_default_the_role_blocks_is_forgotten(self):
        [kit] = visible_kits([self._kit("a", "blocked", default="blocked")], lambda tool: tool.id == "blocked")

        assert kit.default_tool_id is None

    def test_a_visible_default_is_kept(self):
        [kit] = visible_kits([self._kit("a", "blocked", default="a")], lambda tool: tool.id == "blocked")

        assert kit.default_tool_id == "a"

    def test_a_tab_left_empty_is_dropped_and_the_rest_keep_their_order(self):
        """Otherwise the tab still shows, and opens onto nothing."""
        kit = Kit(
            id="k", label="K", kit_tags=("Featured", "Video", "Other"),
            tools=(
                KitTool(tool=ToolSummary(id="a", name="A"), kit_tags=("Other", "Featured")),
                KitTool(tool=ToolSummary(id="clip", name="Clip"), kit_tags=("Video",)),
                KitTool(tool=ToolSummary(id="b", name="B"), kit_tags=("Other",)),
            ),
        )

        [visible] = visible_kits([kit], lambda tool: tool.id == "clip")

        assert visible.kit_tags == ("Featured", "Other")
        assert visible.tabs == ("All", "Featured", "Other")


class TestWhichTeamsAreWorthAsking:
    def test_a_team_whose_role_blocks_the_tool_is_not_offered(self):
        """It would price the run and then hide the tool the switch carried."""
        blocking = _team("t1", blocked_ids=["tool-1"])
        able = _team("t2", "Beta")

        assert teams_worth_checking([_personal(), blocking, able], tool=TOOL) == [able]

    def test_without_a_tool_every_team_is_still_asked(self):
        blocking = _team("t1", blocked_ids=["tool-1"])

        assert teams_worth_checking([blocking]) == [blocking]


# -- the panel --------------------------------------------------------------


class FakeForm:
    def __init__(self, values=None) -> None:
        self.values = dict(values or {})
        self.built_with = None

    def collect(self):
        return dict(self.values)

    def image_selections(self):
        return {}

    def export_image_state(self):
        return {}

    def export_capture_sources(self):
        return {}

    def build(self, _frame, tool, values, capture_container=None):
        self.built_with = (tool.id, dict(values))

    def restore_image_state(self, _state):
        return None

    def restore_capture_sources(self, _state):
        return None


class ImmediateRedraws:
    def __init__(self) -> None:
        self.keys: list = []

    def request(self, key, render) -> None:
        self.keys.append(key)
        render()


class Jobs:
    """The job queue, recording what was started."""

    def __init__(self) -> None:
        self.submitted: list = []

    def submit(self, name, run, prompt=None):
        self.submitted.append(name)

    def update(self, *_args, **_kwargs):
        return None


class _Frame:
    def clear(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None


def _session(*accounts, selected=None, verified=True) -> RdSession:
    session = RdSession()
    session.refresh_token = "r"
    session.accounts = list(accounts)
    session.selected_account_id = selected
    session.user = {"email": "a@b.test", "email_verified": verified}
    session._persist = lambda: None
    return session


class Browser:
    """The tool browser, recording what it was asked to do."""

    def __init__(self) -> None:
        self.shown: list = []
        self.updates: list = []
        self.closed = 0

    def show(self, tools, on_pick, *, kits=None, hides=None):
        self.shown.append((list(tools), on_pick, kits, hides))

    def update_tools(self, tools):
        self.updates.append(list(tools))

    def close(self):
        self.closed += 1


def _panel(session, form=None) -> RunDiffusionPanel:
    panel = object.__new__(RunDiffusionPanel)
    panel._session = session
    panel._tool_detail = TOOL
    panel._tools = [ToolSummary(id=TOOL.id, name=TOOL.name)]
    panel._catalog = list(panel._tools)
    panel._kits = []
    panel._all_kits = []
    panel._selected_tool_id = TOOL.id
    panel._team_answers = {}
    panel._redraws = ImmediateRedraws()
    panel._account_redraws = ImmediateRedraws()
    panel._cost_task = None
    panel._cost_request = None
    panel._estimate_generation = 0
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
    panel._generate_button = None
    panel._jobs = Jobs()
    panel._tool_browser = Browser()
    panel._current_tab = panel_module.TAB_CREATE
    panel.walls = []
    panel.cost = []
    panel.errors = []
    panel._render_wall = panel.walls.append
    panel._clear_wall = lambda: panel.walls.append(None)
    panel._set_cost = panel.cost.append
    panel._set_error = panel.errors.append
    panel._set_status = lambda _m: None
    return panel


def _never_priced(*_args, **_kwargs):
    pytest.fail("a role without generating is never priced")


class TestPricingOnARoleWithoutGenerating:
    def test_nothing_is_asked_and_the_wall_names_the_team(self, monkeypatch):
        monkeypatch.setattr(generate_api, "preview_cost", _never_priced)
        panel = _panel(_session(_personal(), _team(generate=False), selected="t1"))

        _run(panel._preview_cost())

        wall = panel.walls[-1]
        assert wall.variant == VARIANT_ROLE_BLOCKED
        assert wall.message == "alpha Team does not allow your role to generate."
        assert panel.cost == [""]

    def test_a_refusal_the_account_list_did_not_predict_is_named_too(self, monkeypatch):
        """The role changed after the list was read. The refusal used to show
        as "Cost unavailable" in a line too narrow to read it."""

        async def refuse(*_a, **_k):
            raise _generate_refusal()

        monkeypatch.setattr(generate_api, "preview_cost", refuse)
        panel = _panel(_session(_personal(), _team(), selected="t1"))

        _run(panel._preview_cost())

        assert panel.walls[-1].variant == VARIANT_ROLE_BLOCKED
        assert "alpha Team" in panel.walls[-1].message
        assert panel.cost == [""]

    def test_any_other_failure_still_says_the_cost_is_unavailable(self, monkeypatch):
        async def fail(*_a, **_k):
            raise ApiError(500, "INTERNAL", "Something broke.")

        monkeypatch.setattr(generate_api, "preview_cost", fail)
        panel = _panel(_session(_personal(), _team(), selected="t1"))

        _run(panel._preview_cost())

        assert panel.walls == [None]
        assert panel.cost == ["Cost unavailable. Something broke."]

    def test_a_late_refusal_for_an_account_since_left_draws_nothing(self, monkeypatch):
        session = _session(_personal(), _team(), selected="t1")
        panel = _panel(session)

        async def refuse(*_a, **_k):
            session.selected_account_id = None
            raise _generate_refusal()

        monkeypatch.setattr(generate_api, "preview_cost", refuse)

        _run(panel._preview_cost())

        assert panel.walls == []

    def test_a_team_that_blocks_the_tool_is_not_offered_as_the_switch(self, monkeypatch):
        asked = []

        async def priced(session, tool, values, num_results=1, image_selections=None, account=None):
            asked.append(account.id if account is not None else "selected")
            return {"can_run": account is not None, "blocking_reason": "Too few tokens.", "total_tokens": 4}

        monkeypatch.setattr(generate_api, "preview_cost", priced)
        panel = _panel(_session(_personal(), _team(blocked_ids=["tool-1"])))

        _run(panel._preview_cost())

        assert asked == ["selected"]
        assert panel.walls[-1].variant == VARIANT_GET_TOKENS


class TestARunTheRoleRefuses:
    def test_the_wall_names_the_team_rather_than_a_job_error(self, monkeypatch):
        team = _team()
        panel = _panel(_session(_personal(), team, selected="t1"))
        monkeypatch.setattr(tools_api, "build_inputs", lambda *a, **k: {})
        monkeypatch.setattr(tools_api, "missing_required_fields", lambda *a, **k: [])

        async def refuse(*_a, **_k):
            raise _generate_refusal()

        monkeypatch.setattr(generate_api, "submit", refuse)
        monkeypatch.setattr(generate_api, "preview_cost", _never_priced)

        with pytest.raises(RuntimeError, match="does not allow your role to generate"):
            _run(panel._run_job(object(), TOOL, {}, {}, 1, {}, team))

        assert panel.walls[-1].variant == VARIANT_ROLE_BLOCKED


class TestGenerateWaitsForTheRole:
    def test_a_role_without_generating_starts_nothing(self):
        panel = _panel(_session(_personal(), _team(generate=False), selected="t1"))

        panel._on_generate_clicked()

        assert panel._jobs.submitted == []
        assert panel.errors[-1] == "alpha Team does not allow your role to generate."

    def test_nothing_starts_before_the_role_is_known(self):
        loading = Account(id="t1", label="alpha Team", kind="TEAM")
        panel = _panel(_session(loading, selected="t1"))

        panel._on_generate_clicked()

        assert panel._jobs.submitted == []
        assert "loading" in panel.errors[-1].lower()

    def test_a_blocked_tool_still_on_the_form_is_refused_at_submit(self):
        panel = _panel(_session(_personal(), _team(blocked_ids=["tool-1"]), selected="t1"))

        panel._on_generate_clicked()

        assert panel._jobs.submitted == []
        assert "Flux 2 Dev" in panel.errors[-1]
        assert "alpha Team" in panel.errors[-1]

    def test_a_role_that_allows_it_starts_the_run(self):
        panel = _panel(_session(_personal(), _team(), selected="t1"))

        panel._on_generate_clicked()

        assert panel._jobs.submitted == ["Flux 2 Dev"]


class TestTheGenerateButton:
    """omni.ui draws a disabled button like an enabled one, so the state is in
    its style as well as in whether it answers a click."""

    def _button(self, panel):
        panel._generate_button = types.SimpleNamespace(enabled=True, style=None)
        panel._sync_generate_button()
        return panel._generate_button

    def test_it_is_unavailable_while_the_role_says_no(self):
        button = self._button(_panel(_session(_personal(), _team(generate=False), selected="t1")))

        assert button.enabled is False
        assert button.style == panel_module._STYLE_PRIMARY_UNAVAILABLE

    def test_it_is_unavailable_while_the_role_is_loading(self):
        loading = Account(id="t1", label="alpha Team", kind="TEAM")
        button = self._button(_panel(_session(loading, selected="t1")))

        assert button.enabled is False

    def test_it_is_ready_when_the_role_allows(self):
        button = self._button(_panel(_session(_personal(), _team(), selected="t1")))

        assert button.enabled is True
        assert button.style == panel_module._STYLE_PRIMARY

    def test_no_button_is_not_a_failure(self):
        panel = _panel(_session(_personal()))

        panel._sync_generate_button()

    def test_the_account_check_draws_the_wall_and_sets_the_button(self):
        """Reached on sign-in and on every account change. Tools still load:
        the library and the catalogue are not what the role turned off."""
        panel = _panel(_session(_personal(), _team(generate=False), selected="t1"))
        panel._generate_button = types.SimpleNamespace(enabled=True, style=None)

        assert panel._check_plugin_access() is True

        assert panel.walls[-1].variant == VARIANT_ROLE_BLOCKED
        assert panel._generate_button.enabled is False

    def test_an_allowed_role_takes_the_wall_down(self):
        panel = _panel(_session(_personal(), _team(), selected="t1"))

        assert panel._check_plugin_access() is True

        assert panel.walls == [None]


class TestToolsTheRoleBlocks:
    def _blocking_panel(self):
        team = _team(blocked_ids=["blocked"], blocked_tags=["video"])
        panel = _panel(_session(_personal(), team, selected="t1"))
        panel._tools = []
        panel._selected_tool_id = None
        return panel

    def test_the_catalogue_hides_them(self):
        panel = self._blocking_panel()

        panel._on_tools_page(
            [
                ToolSummary(id="a", name="A"),
                ToolSummary(id="blocked", name="Blocked"),
                ToolSummary(id="clip", name="Clip", tool_tag_ids=("video",)),
                ToolSummary(id="b", name="B"),
            ]
        )

        assert [t.id for t in panel._tools] == ["a", "b"]
        assert panel._selected_tool_id == "a"

    def test_the_whole_catalogue_is_kept_and_handed_to_the_browser(self):
        """The browser applies whichever rule it was opened with: hidden for
        choosing a tool to run, everything for filtering the library."""
        panel = self._blocking_panel()
        listed = [ToolSummary(id="a", name="A"), ToolSummary(id="blocked", name="Blocked")]

        panel._on_tools_page(listed)

        assert [t.id for t in panel._catalog] == ["a", "blocked"]
        assert [t.id for t in panel._tool_browser.updates[-1]] == ["a", "blocked"]

    def test_a_selected_tool_the_role_blocks_is_dropped(self):
        panel = self._blocking_panel()
        panel._selected_tool_id = "blocked"

        panel._on_tools_page([ToolSummary(id="blocked", name="Blocked"), ToolSummary(id="a", name="A")])

        assert panel._selected_tool_id == "a"

    def test_a_carried_tool_the_new_role_blocks_is_not_adopted(self, monkeypatch):
        panel = self._blocking_panel()
        panel._current_tab = panel_module.TAB_LIBRARY
        panel._selected_tool_id = "blocked"
        panel._account_carry = _AccountCarry(
            account_id="t1", tool_id="blocked", tool=ToolSummary(id="blocked", name="Blocked")
        )

        async def list_all_tools(_session, on_page=None):
            listed = [ToolSummary(id="a", name="A")]
            if on_page is not None:
                on_page(listed)
            return listed

        monkeypatch.setattr(tools_api, "list_all_tools", list_all_tools)

        _run(panel._load_tools())

        assert [t.id for t in panel._tools] == ["a"]
        assert panel._account_carry is None
        assert panel._selected_tool_id == "a"

    def test_the_kits_hide_them(self, monkeypatch):
        panel = self._blocking_panel()
        kit = Kit(
            id="k", label="K",
            tools=(
                KitTool(tool=ToolSummary(id="a", name="A")),
                KitTool(tool=ToolSummary(id="clip", name="Clip", tool_tag_ids=("video",))),
            ),
        )

        async def list_kits(_session):
            return [kit]

        monkeypatch.setattr(kits_api, "list_kits", list_kits)

        _run(panel._load_kits())

        assert [[entry.tool.id for entry in k.tools] for k in panel._kits] == [["a"]]

    def test_a_browsed_tool_the_role_blocks_is_refused(self):
        panel = self._blocking_panel()
        panel._tools = [ToolSummary(id="a", name="A")]
        panel._selected_tool_id = "a"
        spawned = []
        panel._spawn = spawned.append

        panel._on_browsed_tool(ToolSummary(id="blocked", name="Blocked"))

        assert panel._selected_tool_id == "a"
        assert [t.id for t in panel._tools] == ["a"]
        assert spawned == []
        assert "Blocked" in panel.errors[-1]

    def test_a_tool_whose_inputs_show_a_blocked_tag_is_dropped(self, monkeypatch):
        """Its listing did not carry the tag (a tool carried across accounts
        can arrive without one), but its own record does."""
        panel = self._blocking_panel()
        panel._tool_detail = None
        panel._tools = [ToolSummary(id="clip", name="Clip"), ToolSummary(id="a", name="A")]
        panel._selected_tool_id = "clip"
        panel._form_frame = _Frame()
        panel._capture_frame = None
        panel._chosen_assets = {}
        panel._sync_viewport_watch = lambda: None
        panel._schedule_cost_refresh = lambda: None
        labels = []
        monkeypatch.setattr(ui, "Label", lambda text="", *a, **k: labels.append(text), raising=False)

        async def get_tool(_session, tool_id):
            return ToolDetail(id=tool_id, name="Clip", tool_fields_hash="h", tool_tag_ids=("video",))

        monkeypatch.setattr(tools_api, "get_tool", get_tool)

        _run(panel._load_tool_detail())

        assert panel._tool_detail is None
        assert panel._tool_detail_pending is False
        assert panel._form.built_with is None
        assert [t.id for t in panel._tools] == ["a"]
        assert panel._selected_tool_id is None
        # Said where its inputs would have been.
        assert labels[-1] == "alpha Team does not allow your role to use Clip. Choose another tool."

    def test_the_browser_hides_them_in_kits_it_fetches_itself(self, tmp_path, monkeypatch):
        browser = dialogs_module.ToolBrowser(session=None, cache_dir=tmp_path)
        browser._hides = lambda tool: tool.id == "blocked"
        browser._render = lambda: None
        kit = Kit(
            id="k", label="K",
            tools=(KitTool(tool=ToolSummary(id="a", name="A")), KitTool(tool=ToolSummary(id="blocked", name="B"))),
        )

        async def list_kits(_session):
            return [kit]

        monkeypatch.setattr(kits_api, "list_kits", list_kits)

        _run(browser._load_kits())

        assert [[entry.tool.id for entry in k.tools] for k in browser._kits] == [["a"]]

    def test_a_carry_made_without_a_listing_keeps_the_tools_tags(self):
        """So the account it is carried to can still tell it is blocked there."""
        panel = _panel(_session(_personal(), _team()))
        panel._tool_detail = ToolDetail(id="tool-9", name="Clip", tool_fields_hash="h", tool_tag_ids=("video",))
        panel._tools = []

        carry = panel._carry_for("t1")

        assert carry.tool.tool_tag_ids == ("video",)


# -- a refusal the server confirmed -----------------------------------------


class TestARefusalTheServerConfirmed:
    """The account list said yes and the server said no. Until the role is
    read again, the server's answer is the one the panel follows, so Generate
    stops offering a run that is refused every time."""

    def _refusing_panel(self, monkeypatch):
        session = _session(_personal(), _team(), selected="t1")
        panel = _panel(session)
        panel._generate_button = types.SimpleNamespace(enabled=True, style=None)
        asked = []

        async def refuse(*_a, **_k):
            asked.append(True)
            raise _generate_refusal()

        monkeypatch.setattr(generate_api, "preview_cost", refuse)
        return panel, asked

    def test_a_refused_estimate_holds_generate_back(self, monkeypatch):
        panel, _asked = self._refusing_panel(monkeypatch)

        _run(panel._preview_cost())

        assert panel._session.selected_account.can_generate is False
        assert panel._generate_button.enabled is False
        panel._on_generate_clicked()
        assert panel._jobs.submitted == []

    def test_the_next_estimate_is_not_asked_again(self, monkeypatch):
        panel, asked = self._refusing_panel(monkeypatch)

        _run(panel._preview_cost())
        _run(panel._preview_cost())

        assert asked == [True]
        assert panel.walls[-1].variant == VARIANT_ROLE_BLOCKED

    def test_a_refused_run_holds_generate_back(self, monkeypatch):
        session = _session(_personal(), _team(), selected="t1")
        panel = _panel(session)
        panel._generate_button = types.SimpleNamespace(enabled=True, style=None)
        monkeypatch.setattr(tools_api, "build_inputs", lambda *a, **k: {})
        monkeypatch.setattr(tools_api, "missing_required_fields", lambda *a, **k: [])

        async def refuse(*_a, **_k):
            raise _generate_refusal()

        monkeypatch.setattr(generate_api, "submit", refuse)

        with pytest.raises(RuntimeError):
            _run(panel._run_job(object(), TOOL, {}, {}, 1, {}, session.selected_account))

        assert session.selected_account.can_generate is False
        assert panel._generate_button.enabled is False

    def test_a_refused_run_holds_back_the_account_it_was_sent_on(self, monkeypatch):
        """The person moved to Personal while the run was in the air: the team
        is held back, Personal is not."""
        session = _session(_personal(), _team(), selected="t1")
        team = session.selected_account
        panel = _panel(session)
        monkeypatch.setattr(tools_api, "build_inputs", lambda *a, **k: {})
        monkeypatch.setattr(tools_api, "missing_required_fields", lambda *a, **k: [])

        async def refuse(*_a, **_k):
            session.selected_account_id = None
            raise _generate_refusal()

        monkeypatch.setattr(generate_api, "submit", refuse)

        with pytest.raises(RuntimeError):
            _run(panel._run_job(object(), TOOL, {}, {}, 1, {}, team))

        personal_now, team_now = session.accounts
        assert team_now.can_generate is False
        assert personal_now.can_generate is True
        assert panel.walls == []

    def test_reading_the_role_again_lifts_it(self, monkeypatch):
        panel, _asked = self._refusing_panel(monkeypatch)
        _run(panel._preview_cost())
        session = panel._session

        generation = session.begin_accounts_refresh()
        session.finish_accounts_refresh(
            generation,
            {
                "user": session.user,
                "accounts": [
                    {"id": "p1", "kind": "PERSONAL", "permissions": {"GENERATE": True}},
                    {"id": "t1", "label": "alpha Team", "kind": "TEAM",
                     "permissions": {"USE_PLUGINS": True, "GENERATE": True}, "plugin_kinds": ["*"]},
                ],
            },
        )

        assert session.selected_account.can_generate is True


# -- the tool browser across an account change -------------------------------


class FakeWindow:
    def __init__(self) -> None:
        self.visible = True


class TestTheBrowserOnAnAccountChange:
    def test_an_account_change_closes_it(self):
        """Its list, its kits, the kit it has open and the choice it would
        report all belong to the account that was left."""
        panel = _panel(_session(_personal(), _team()))
        panel._show_tab = lambda _index: None
        panel._rebuild_surface = lambda _surface: None
        spawned = []
        panel._spawn = spawned.append

        panel._redraw_for_account()

        assert panel._tool_browser.closed == 1
        for coroutine in spawned:
            coroutine.close()

    def test_closing_hides_the_window(self, tmp_path):
        browser = dialogs_module.ToolBrowser(session=None, cache_dir=tmp_path)
        browser._window = FakeWindow()

        browser.close()

        assert browser._window.visible is False

    def test_closing_before_it_ever_opened_is_not_a_failure(self, tmp_path):
        dialogs_module.ToolBrowser(session=None, cache_dir=tmp_path).close()

    def _open_browser(self, tmp_path, hides=None):
        browser = dialogs_module.ToolBrowser(session=None, cache_dir=tmp_path)
        browser._window = FakeWindow()
        browser._hides = hides
        renders = []
        browser._render = lambda: renders.append(True)
        return browser, renders

    def test_a_different_list_of_the_same_length_is_taken(self, tmp_path):
        browser, renders = self._open_browser(tmp_path)
        browser._tools = [ToolSummary(id="a", name="A"), ToolSummary(id="b", name="B")]

        browser.update_tools([ToolSummary(id="c", name="C"), ToolSummary(id="d", name="D")])

        assert [t.id for t in browser._tools] == ["c", "d"]
        assert renders == [True]

    def test_the_same_list_is_not_redrawn(self, tmp_path):
        browser, renders = self._open_browser(tmp_path)
        browser._tools = [ToolSummary(id="a", name="A")]

        browser.update_tools([ToolSummary(id="a", name="A")])

        assert renders == []

    def test_a_longer_list_keeps_its_rule(self, tmp_path):
        browser, _renders = self._open_browser(tmp_path, hides=lambda tool: tool.id == "blocked")

        browser.update_tools([ToolSummary(id="a", name="A"), ToolSummary(id="blocked", name="B")])

        assert [t.id for t in browser._tools] == ["a"]


# -- the library filter -------------------------------------------------------


class TestTheLibraryFilterOffersEveryTool:
    """A role that blocks a tool still leaves that tool's past results in the
    library, so filtering by it stays possible. Only running it is hidden."""

    def _panel(self):
        panel = _panel(_session(_personal(), _team(blocked_ids=["blocked"]), selected="t1"))
        panel._catalog = [ToolSummary(id="a", name="A"), ToolSummary(id="blocked", name="Blocked")]
        panel._tools = [ToolSummary(id="a", name="A")]
        panel._all_kits = ["every kit"]
        panel._kits = ["kits without blocked tools"]
        return panel

    def test_the_filter_browser_is_handed_the_whole_catalogue(self):
        panel = self._panel()

        panel._choose_filter_tool()

        tools, _on_pick, kits, hides = panel._tool_browser.shown[-1]
        assert [t.id for t in tools] == ["a", "blocked"]
        assert kits == ["every kit"]
        assert hides is None

    def test_a_filter_on_a_blocked_tool_is_still_named(self):
        assert self._panel()._tool_filter_label("blocked") == "Blocked"

    def test_choosing_a_tool_to_run_still_hides_them(self):
        panel = self._panel()

        panel._on_browse_clicked()

        tools, _on_pick, kits, hides = panel._tool_browser.shown[-1]
        assert [t.id for t in tools] == ["a"]
        assert kits == ["kits without blocked tools"]
        assert hides is not None and hides(ToolSummary(id="blocked", name="Blocked"))


# -- a role that allows no tools ---------------------------------------------


class Recorder:
    """A frame that records the labels drawn into it."""

    def __init__(self, labels) -> None:
        self.labels = labels
        self.cleared = 0

    def clear(self):
        self.cleared += 1

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None


class TestARoleThatAllowsNoTools:
    """Every tool in the catalogue blocked used to leave the panel on
    "Loading tools..." for good, with Browse asking a signed-in person to sign
    in and Generate saying no tool was chosen."""

    def _panel(self, monkeypatch):
        panel = _panel(_session(_personal(), _team(blocked_tags=["image"]), selected="t1"))
        panel._tools = []
        panel._catalog = []
        panel._selected_tool_id = None
        panel._tool_detail = None
        labels = []
        panel._tool_frame = Recorder(labels)
        panel._form_frame = Recorder(labels)
        panel.labels = labels
        monkeypatch.setattr(ui, "Label", lambda text="", *a, **k: labels.append(text), raising=False)
        monkeypatch.setattr(ui, "HStack", lambda *a, **k: Recorder(labels), raising=False)
        monkeypatch.setattr(ui, "VStack", lambda *a, **k: Recorder(labels), raising=False)
        monkeypatch.setattr(ui, "Spacer", lambda *a, **k: None, raising=False)
        monkeypatch.setattr(ui, "Button", lambda *a, **k: None, raising=False)

        async def list_all_tools(_session, on_page=None):
            listed = [
                ToolSummary(id="a", name="A", tool_tag_ids=("image",)),
                ToolSummary(id="b", name="B", tool_tag_ids=("image",)),
            ]
            if on_page is not None:
                on_page(listed)
            return listed

        monkeypatch.setattr(tools_api, "list_all_tools", list_all_tools)
        return panel

    def test_the_create_tab_says_so_and_names_the_team(self, monkeypatch):
        panel = self._panel(monkeypatch)

        _run(panel._load_tools())

        assert panel._tools == []
        assert "No tools available" in panel.labels
        assert any(
            text.startswith("alpha Team does not allow your role to use any tools.")
            and "your account administrators" in text
            for text in panel.labels
        )

    def test_browse_names_the_team_rather_than_asking_to_sign_in(self, monkeypatch):
        panel = self._panel(monkeypatch)
        _run(panel._load_tools())

        panel._on_browse_clicked()

        assert panel.errors[-1] == "alpha Team does not allow your role to use any tools."
        assert panel._tool_browser.shown == []

    def test_generate_names_the_team_rather_than_a_missing_tool(self, monkeypatch):
        panel = self._panel(monkeypatch)
        _run(panel._load_tools())

        panel._on_generate_clicked()

        assert panel.errors[-1] == "alpha Team does not allow your role to use any tools."

    def test_browse_before_the_catalogue_lands_says_it_is_loading(self, monkeypatch):
        panel = self._panel(monkeypatch)

        panel._on_browse_clicked()

        assert "loaded" in panel.errors[-1]
        assert "Sign in" not in panel.errors[-1]

    def test_browse_signed_out_still_asks_to_sign_in(self, monkeypatch):
        panel = self._panel(monkeypatch)
        panel._session.refresh_token = None

        panel._on_browse_clicked()

        assert panel.errors[-1] == "Sign in to browse tools."
