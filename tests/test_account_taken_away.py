"""A team taken away while the panel is open on it.

An administrator removes someone from the team they are billing to, or deletes
the team. The server checks the account on every request, so from that moment
everything asked on that team is refused: the catalogue, the library, the
estimate, the run. The panel went on asking anyway, because the account list
was read only at sign-in and on an account change, and the person was left
with a panel in which nothing worked until they chose another account by hand
or restarted Kit.

So a request refused because the selected team is no longer the caller's to
use reads the account list again. A team that is gone from it falls back to
the personal account, the same as one found gone at start, and the panel
reloads for the account now billed and says which team went.

What must not happen is pinned here too:

- several refusals landing together read the list once, not once each;
- a refusal about a team that is not the one selected (the status of a run
  started before a switch, a price asked of another team) changes nothing;
- a refusal that lands after a sign-out is not applied to the next person;
- a list that still has the team is not asked for again in a loop;
- an answer the team was asked for (its catalogue, its kits, its tags, the
  tool on the form) landing after the panel has moved to the personal account
  is not shown under it, and the tool browser is not left open on the team.

The server is stood in for at the socket, so every request goes through the
real client: what is asked, on which account, and what a refusal does.
"""

from __future__ import annotations

import asyncio
import io
import json
import threading
import time
import types
import urllib.error
from urllib.parse import parse_qs, urlsplit

import omni.ui as ui
import pytest

from rundiffusion_omniverse import panel as panel_module
from rundiffusion_omniverse.api import assets as assets_api
from rundiffusion_omniverse.api import generate as generate_api
from rundiffusion_omniverse.api import kits as kits_api
from rundiffusion_omniverse.api import token_store
from rundiffusion_omniverse.api import tool_tags as tool_tags_api
from rundiffusion_omniverse.api import tools as tools_api
from rundiffusion_omniverse.api import transport
from rundiffusion_omniverse.api.constants import API_BASE_URL_ENV_VAR
from rundiffusion_omniverse.api.session import RdSession
from rundiffusion_omniverse.api.tools import ToolDetail
from rundiffusion_omniverse.api.transport import ApiError
from rundiffusion_omniverse.panel import RunDiffusionPanel

API = "https://api.example/v2"

TOOL = ToolDetail(id="tool-1", name="Flux 2 Dev", tool_fields_hash="h1")

#: The two ways the server says a team is no longer the caller's to use.
REMOVED = (403, "ACCOUNT_NOT_MEMBER")
DELETED = (404, "ACCOUNT_NOT_FOUND")


def _run(coroutine):
    return asyncio.new_event_loop().run_until_complete(coroutine)


def _accounts(*teams: str) -> dict:
    """What the account list answers for someone on `teams`."""
    accounts = [
        {"id": "p1", "label": "Personal account", "kind": "PERSONAL",
         "permissions": {"USE_PLUGINS": True, "GENERATE": True}, "plugin_kinds": ["*"]},
    ]
    names = {"t1": "alpha Team", "t2": "beta Team"}
    for team in teams:
        accounts.append(
            {"id": team, "label": names[team], "kind": "TEAM", "team_label": "Team",
             "permissions": {"USE_PLUGINS": True, "GENERATE": True}, "plugin_kinds": ["*"]}
        )
    return {"user": {"email": "a@b.test", "email_verified": True}, "accounts": accounts}


class Answer:
    def __init__(self, payload) -> None:
        self._raw = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None


def _refusal(request, status: int, code: str) -> urllib.error.HTTPError:
    body = {"error": {"code": code, "message": "That team is not yours to use."}}
    return urllib.error.HTTPError(
        request.full_url, status, code, {}, io.BytesIO(json.dumps(body).encode())
    )


class Server:
    """The API, as far as these tests need one.

    It knows which teams the caller can no longer use and what the account
    list says, and it writes down every request with the team it named.
    """

    ANSWERS = {
        "/tools": {"data": [{"id": "tool-1", "name": "Flux 2 Dev"}]},
        "/tools/tool-1": {"id": "tool-1", "name": "Flux 2 Dev", "tool_fields_hash": "h1"},
        "/generate/preview-cost": {"can_run": True, "total_tokens": 4},
        "/generate": {"request_id": "run-1"},
        "/generate/run-1": {"status": "succeeded", "outputs": []},
        # Listing them and adding one are the same path.
        "/uploads": {"data": [], "id": "upload-1"},
    }

    def __init__(self) -> None:
        #: Team id to the refusal every request naming it gets.
        self.refused: dict[str, tuple[int, str]] = {}
        #: What the account list says. Only the personal account unless a test
        #: says otherwise: the team is gone from it.
        self.accounts = _accounts()
        self.account_list_unreachable = False
        #: Set to refuse the account list itself, which the API never does.
        self.account_list_refused: tuple[int, str] | None = None
        self.asked: list[tuple[str, str | None]] = []
        #: Set to keep every answer back until it is released.
        self.hold: threading.Event | None = None
        #: A path and a team, to what that team is answered instead.
        self.for_team: dict[tuple[str, str | None], dict] = {}

    def __call__(self, request, timeout=None):
        parts = urlsplit(request.full_url)
        path = parts.path[len(urlsplit(API).path):]
        team = parse_qs(parts.query).get("team_id", [None])[0]
        self.asked.append((path, team))
        hold = self.hold
        if hold is not None:
            hold.wait(5)
        if path == "/me":
            if self.account_list_unreachable:
                raise OSError("offline")
            if self.account_list_refused is not None:
                raise _refusal(request, *self.account_list_refused)
            return Answer(self.accounts)
        if team in self.refused:
            raise _refusal(request, *self.refused[team])
        return Answer(self.for_team.get((path, team), self.ANSWERS.get(path, {"data": []})))

    @property
    def account_list_reads(self) -> int:
        return sum(1 for path, _team in self.asked if path == "/me")


@pytest.fixture
def server(monkeypatch):
    fake = Server()
    monkeypatch.setenv(API_BASE_URL_ENV_VAR, API)
    monkeypatch.setattr(transport.urllib.request, "urlopen", fake)
    monkeypatch.setattr(token_store, "clear", lambda: None)
    return fake


def _sign_in(session: RdSession, *teams: str, selected: str | None = "t1") -> None:
    session.refresh_token = "r"
    session.id_token = "id"
    session.expires_at = int(time.time()) + 3600
    session.device_id = "device-1"
    session.adopt_identity(_accounts(*teams))
    session.selected_account_id = selected


def _session(*teams: str, selected: str | None = "t1") -> RdSession:
    session = RdSession()
    session._persist = lambda: None
    _sign_in(session, *(teams or ("t1",)), selected=selected)
    return session


def _team(session: RdSession, team_id: str = "t1"):
    return next(account for account in session.accounts if account.id == team_id)


#: Everything the panel asks on an account, by what a person would call it.
REQUESTS = {
    "the tool list": lambda s: tools_api.list_tools(s),
    "a tool's inputs": lambda s: tools_api.get_tool(s, "tool-1"),
    "the kits": lambda s: kits_api.list_kits(s),
    "the library": lambda s: assets_api.list_library(s),
    "the uploads": lambda s: assets_api.list_uploads(s),
    "a new upload": lambda s: assets_api.upload_image(
        s, filename="view.png", content=b"png", mime_type="image/png"
    ),
    "an estimate": lambda s: generate_api.preview_cost(s, TOOL, {}),
    "a run": lambda s: generate_api.submit(s, TOOL, {}, account=s.selected_account),
    "a run's status": lambda s: generate_api.poll_until_done(
        s, "run-1", account=s.selected_account
    ),
}


def _refused(session: RdSession, request=REQUESTS["the tool list"]) -> ApiError:
    """Make `request`, which the server refuses. The refusal it raised."""
    with pytest.raises(ApiError) as caught:
        _run(request(session))
    return caught.value


# -- the panel ----------------------------------------------------------------


class Frame:
    def clear(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None


class Form:
    """The tool form: what is typed on it, and what it was last built with."""

    def __init__(self) -> None:
        self.values = {"prompt": "a red barn"}
        self.built_with = None

    def collect(self):
        return dict(self.values)

    def build(self, _frame, tool, values, capture_container=None):
        self.built_with = (tool.id, dict(values))


class Browser:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1

    def update_tools(self, _tools) -> None:
        return None


class Redraws:
    def request(self, _key, render) -> None:
        render()


def _panel(session: RdSession) -> RunDiffusionPanel:
    """A panel open on the Create tab, with a tool loaded and a prompt typed."""
    panel = object.__new__(RunDiffusionPanel)
    panel._session = session
    # As the panel's constructor wires it.
    session.on_account_gone = panel._account_gone
    panel._current_tab = panel_module.TAB_CREATE
    panel._tools = [tools_api.ToolSummary(id=TOOL.id, name=TOOL.name)]
    panel._catalog = list(panel._tools)
    panel._kits = []
    panel._all_kits = []
    panel._tool_tags = {}
    panel._selected_tool_id = TOOL.id
    panel._tool_detail = TOOL
    panel._tool_detail_pending = False
    panel._account_carry = None
    panel._cost_task = None
    panel._cost_request = None
    panel._cost_request_key = None
    panel._cost_wanted_again = False
    panel._cost_quote = None
    panel._team_answers = {}
    panel._catalog_generation = 0
    panel._tool_load_generation = 0
    panel._form = Form()
    panel._form_frame = Frame()
    panel._form_built = True
    panel._form_values = {}
    panel._image_state = {}
    panel._capture_state = {}
    panel._chosen_assets = {}
    panel._capture_frame = None
    panel._tool_frame = None
    panel._generate_button = types.SimpleNamespace(enabled=True, style=None)
    panel._tool_browser = Browser()
    panel._redraws = Redraws()
    panel._sync_viewport_watch = lambda: None
    panel._schedule_cost_refresh = lambda: None
    panel.spawned = []
    panel._spawn = panel.spawned.append
    #: What the status line was last given. An empty one takes it down.
    panel.status = []
    panel._set_error = panel.status.append
    panel._set_status = panel.status.append
    panel.walls = []
    panel._render_wall = panel.walls.append
    panel._clear_wall = lambda: panel.walls.append(None)
    panel._set_cost = lambda _message: None
    panel.account_bars = 0
    panel.dropped_browsing = 0
    panel.rebuilt = []

    def render_account_bar() -> None:
        panel.account_bars += 1

    def forget_browsing() -> None:
        panel.dropped_browsing += 1

    panel._render_account_bar = render_account_bar
    panel._forget_browsing = forget_browsing
    panel._rebuild_surface = panel.rebuilt.append
    panel._show_tab = lambda _index: None
    return panel


#: What the panel fetches for an account once it knows which one is billed.
RELOADS = ["_load_kits", "_load_tool_tags", "_load_tools"]


def _settle(panel, *, also=()) -> list:
    """Run every account-list read the panel started, and anything named in
    `also`. What else it went on to start is named and not run."""
    started = []
    while panel.spawned:
        coroutine = panel.spawned.pop(0)
        name = coroutine.cr_code.co_name
        if name == "_reread_accounts_then_load" or name in also:
            _run(coroutine)
        else:
            started.append(name)
            coroutine.close()
    return sorted(started)


class TestEveryRequestOnTheTeam:
    @pytest.mark.parametrize("request_name", sorted(REQUESTS))
    @pytest.mark.parametrize("refusal", [REMOVED, DELETED], ids=["removed", "deleted"])
    def test_a_refusal_lands_the_panel_on_the_personal_account(
        self, server, request_name, refusal
    ):
        session = _session()
        panel = _panel(session)
        server.refused = {"t1": refusal}

        _refused(session, REQUESTS[request_name])
        started = _settle(panel)

        assert session.selected_account.is_personal
        assert session.account_params() == {}, "nothing more is asked on the team"
        assert server.account_list_reads == 1
        assert started == RELOADS

    def test_the_tags_behind_the_filters_count_too(self, server):
        """That request never raises: a lost vocabulary costs two filters. The
        refusal inside it is still the server saying the team is gone."""
        session = _session()
        panel = _panel(session)
        server.refused = {"t1": REMOVED}

        assert _run(tool_tags_api.list_tool_tags(session)) == []
        _settle(panel)

        assert session.selected_account.is_personal

    def test_any_other_refusal_reads_nothing_again(self, server):
        """A role that does not allow this plugin is refused with 403 too."""
        session = _session()
        panel = _panel(session)
        server.refused = {"t1": (403, "PERMISSION_DENIED")}

        _refused(session)

        assert panel.spawned == []
        assert session.selected_account_id == "t1"


class TestThePanelAfterTheTeamIsGone:
    def _refused_panel(self, server):
        session = _session()
        panel = _panel(session)
        server.refused = {"t1": REMOVED}
        _refused(session)
        return panel, session

    def test_billed_to_library_and_uploads_are_redrawn_for_personal(self, server):
        panel, _session_ = self._refused_panel(server)

        _settle(panel)

        assert panel.account_bars == 1
        assert panel.dropped_browsing == 1, "the team's pages are not left on screen"
        assert panel.rebuilt == list(panel_module.POPPABLE_SURFACES)

    def test_the_person_is_told_which_team_went(self, server):
        panel, _session_ = self._refused_panel(server)

        _settle(panel)

        said = panel.status[-1]
        assert said.startswith("alpha Team ")
        assert "personal account" in said
        assert "\u2014" not in said

    def test_it_is_still_said_once_the_form_has_come_back(self, server, monkeypatch):
        """The tool is loaded again for the account now billed, and a tool
        that loads cleanly takes the status line down. This is the one thing
        on it the person did not ask for and has to be told."""
        monkeypatch.setattr(ui, "Label", lambda *a, **k: None, raising=False)
        panel, _session_ = self._refused_panel(server)

        _settle(panel, also=("_load_tools", "_load_tool_detail"))

        assert panel._form.built_with == ("tool-1", {"prompt": "a red barn"})
        assert panel.status[-1].startswith("alpha Team ")
        assert ("/tools", None) in server.asked, "the catalogue is the personal one"
        assert ("/tools/tool-1", None) in server.asked

    def test_generate_waits_while_the_list_is_read(self, server):
        panel, session = self._refused_panel(server)

        assert not panel._generate_allowed()
        _settle(panel)

        assert panel._generate_allowed()
        assert not session.refreshing_accounts


class Asked:
    """A request still in the air, as far as letting go of it is concerned."""

    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class TestWhatWasAskedForTheTeam:
    """The team's catalogue, kits, tags and tool were asked for before it
    turned out to be gone. An answer that lands after the panel has moved to
    the personal account is the team's, and the account now billed is not."""

    def _lands_after_the_team_is_gone(self, server, panel, load) -> None:
        """`load` is asked of the team and is still unanswered when the account
        list is read again and the team is not on it. Its answer lands last."""

        async def scenario():
            held = server.hold = threading.Event()
            loading = asyncio.ensure_future(load())
            for _ in range(2000):
                if server.asked:
                    break
                await asyncio.sleep(0.001)
            assert server.asked, "the team was never asked"
            server.hold = None
            await panel._reread_accounts_then_load()
            held.set()
            await loading

        _run(scenario())
        assert server.asked[0][1] == "t1"
        assert panel._session.selected_account.is_personal
        # What the panel went on to fetch for the personal account is not run:
        # the question is what the team's own answer did.
        _settle(panel)

    def test_its_catalogue_is_not_shown_under_personal(self, server):
        panel = _panel(_session())
        server.for_team[("/tools", "t1")] = {"data": [{"id": "team-tool", "name": "Team only"}]}

        self._lands_after_the_team_is_gone(server, panel, panel._load_tools)

        assert "team-tool" not in [tool.id for tool in panel._catalog]
        assert "team-tool" not in [tool.id for tool in panel._tools]

    def test_its_kits_are_not_kept(self, server):
        panel = _panel(_session())
        server.for_team[("/kits", "t1")] = {
            "data": [
                {"id": "kit-team", "label": "Team kit",
                 "tools": [{"tool": {"id": "team-tool", "name": "Team only"}}]},
            ]
        }

        self._lands_after_the_team_is_gone(server, panel, panel._load_kits)

        assert panel._all_kits == []
        assert panel._kits == []

    def test_its_tags_are_not_kept(self, server):
        panel = _panel(_session())
        server.for_team[("/tool-tags", "t1")] = {
            "data": [{"id": "tag-team", "type": "model_family", "label": "Team models"}]
        }

        self._lands_after_the_team_is_gone(server, panel, panel._load_tool_tags)

        assert panel._tool_tags == {}

    def test_the_tool_it_was_loading_is_not_drawn(self, server, monkeypatch):
        monkeypatch.setattr(ui, "Label", lambda *a, **k: None, raising=False)
        panel = _panel(_session())
        server.for_team[("/tools/tool-1", "t1")] = {
            "id": "tool-1", "name": "Flux 2 Dev", "tool_fields_hash": "the-teams",
        }

        self._lands_after_the_team_is_gone(server, panel, panel._load_tool_detail)

        assert panel._form.built_with is None
        assert panel._tool_detail is None

    def test_its_refusal_does_not_take_down_what_the_person_was_told(self, server):
        """Several requests were on their way to the team. The first refusal
        moves the panel and says which team went; the ones behind it are
        refused too, and are no longer about the account on screen."""
        session = _session()
        panel = _panel(session)
        server.refused = {"t1": REMOVED}

        async def scenario():
            held = server.hold = threading.Event()
            catalogue = asyncio.ensure_future(panel._load_tools())
            for _ in range(2000):
                if server.asked:
                    break
                await asyncio.sleep(0.001)
            server.hold = None
            with pytest.raises(ApiError):
                await kits_api.list_kits(session)
            await panel.spawned.pop(0)
            held.set()
            await catalogue

        _run(scenario())
        _settle(panel)

        assert session.selected_account.is_personal
        assert panel.status[-1].startswith("alpha Team ")

    def test_the_tool_browser_is_closed(self, server):
        """Left open, it lists the team's catalogue and kits, and a tool chosen
        in it would land on the personal account."""
        session = _session()
        panel = _panel(session)
        server.refused = {"t1": REMOVED}

        _refused(session)
        _settle(panel)

        assert panel._tool_browser.closed == 1

    def test_the_tool_browser_is_left_alone_while_the_team_is_still_listed(self, server):
        session = _session()
        panel = _panel(session)
        server.refused = {"t1": REMOVED}
        server.accounts = _accounts("t1")

        _refused(session)
        _settle(panel)

        assert panel._tool_browser.closed == 0

    def test_what_browse_would_open_with_is_not_the_teams(self, server):
        """The browser opens on the catalogue, the kits and the tags held. Until
        the personal ones land, holding nothing is the truth."""
        session = _session()
        panel = _panel(session)
        panel._all_kits = panel._kits = ["the team's kit"]
        panel._tool_tags = {"model_family": ["the team's tag"]}
        server.refused = {"t1": REMOVED}

        _refused(session)
        _settle(panel)

        assert panel._catalog == []
        assert panel._all_kits == []
        assert panel._kits == []
        assert panel._tool_tags == {}

    def test_the_form_and_its_tool_stay_where_they_are(self, server):
        """Nobody chose this change of account, so what was typed is kept and
        loaded again for the account now billed."""
        session = _session()
        panel = _panel(session)
        server.refused = {"t1": REMOVED}

        _refused(session)
        _settle(panel)

        assert panel._selected_tool_id == TOOL.id
        assert [tool.id for tool in panel._tools] == [TOOL.id]
        assert panel._tool_detail is TOOL

    def test_an_estimate_asked_of_the_team_is_let_go(self, server):
        """It may still have other teams to ask, one request each, about a
        question nobody is asking any more."""
        session = _session()
        panel = _panel(session)
        estimate = panel._cost_request = Asked()
        panel._team_answers = {"a question": (0.0, "t1")}
        server.refused = {"t1": REMOVED}

        _refused(session)
        _settle(panel)

        assert estimate.cancelled
        assert panel._cost_request is None
        assert panel._team_answers == {}


class TestOneReadAtATime:
    def test_refusals_landing_together_read_the_list_once(self, server):
        session = _session()
        panel = _panel(session)
        server.refused = {"t1": REMOVED}

        for name in ("the tool list", "the kits", "the library"):
            _refused(session, REQUESTS[name])
        _settle(panel)

        assert server.account_list_reads == 1
        assert session.selected_account.is_personal

    def test_a_read_that_fails_is_tried_again_at_the_next_refusal(self, server):
        session = _session()
        panel = _panel(session)
        server.refused = {"t1": REMOVED}
        server.account_list_unreachable = True

        _refused(session)
        started = _settle(panel)

        assert session.selected_account_id == "t1", "nothing was learned"
        assert not session.refreshing_accounts
        assert started == [], "and nothing is fetched again for a team that refuses"

        server.account_list_unreachable = False
        _refused(session, REQUESTS["an estimate"])
        _settle(panel)

        assert server.account_list_reads == 2
        assert session.selected_account.is_personal

    def test_a_list_that_still_has_the_team_is_not_asked_for_in_a_loop(self, server):
        """The list and the refusal disagree. Fetching the team's tools again
        would be refused again and read the list again, for as long as they
        did."""
        session = _session()
        panel = _panel(session)
        server.refused = {"t1": REMOVED}
        server.accounts = _accounts("t1")

        _refused(session)
        started = _settle(panel)

        assert started == []
        assert server.account_list_reads == 1
        assert session.selected_account_id == "t1"
        assert panel._generate_allowed()
        assert panel.status == [], "nothing happened that the person has to be told"


class TestARefusalAboutAnotherAccount:
    def test_the_status_of_a_run_started_before_a_switch(self, server):
        """The run was sent on the team and the person has since chosen their
        personal account. Its status is still asked of the team."""
        session = _session(selected=None)
        panel = _panel(session)
        team = _team(session)
        server.refused = {"t1": REMOVED}

        _refused(session, lambda s: generate_api.poll_until_done(s, "run-1", account=team))

        assert ("/generate/run-1", "t1") in server.asked
        assert panel.spawned == []
        assert server.account_list_reads == 0
        assert not session.refreshing_accounts

    def test_a_switch_to_another_team_is_left_alone(self, server):
        session = _session("t1", "t2", selected="t2")
        panel = _panel(session)
        team = _team(session)
        server.refused = {"t1": DELETED}

        _refused(session, lambda s: generate_api.poll_until_done(s, "run-1", account=team))

        assert panel.spawned == []
        assert session.selected_account_id == "t2"

    def test_a_price_asked_of_a_team_that_is_not_selected(self, server):
        """The personal account cannot pay, so the panel asks whether a team
        could. That team turning out to be gone is not about the one billed."""
        session = _session(selected=None)
        panel = _panel(session)
        team = _team(session)
        server.refused = {"t1": REMOVED}

        _refused(session, lambda s: generate_api.preview_cost(s, TOOL, {}, account=team))

        assert panel.spawned == []
        assert session.selected_account.is_personal


class TestARefusalThatLandsAfterSignOut:
    def test_the_next_person_on_the_same_team_is_not_moved_off_it(self, server):
        session = _session()
        panel = _panel(session)
        server.refused = {"t1": REMOVED}

        async def scenario():
            held = server.hold = threading.Event()
            request = asyncio.ensure_future(tools_api.list_tools(session))
            while not server.asked:
                await asyncio.sleep(0.001)
            # Signed out, and someone else signed in on the same team, while
            # the request was in the air.
            server.hold = None
            await session.sign_out()
            _sign_in(session, "t1")
            held.set()
            with pytest.raises(ApiError):
                await request

        _run(scenario())

        assert ("/tools", "t1") in server.asked
        assert panel.spawned == []
        assert server.account_list_reads == 0
        assert session.selected_account_id == "t1"
        assert not session.refreshing_accounts

    def test_the_same_refusal_without_the_sign_out_does_move_them(self, server):
        """The control for the test above: held back the same way, and landing
        on the sign-in that asked."""
        session = _session()
        panel = _panel(session)
        server.refused = {"t1": REMOVED}

        async def scenario():
            held = server.hold = threading.Event()
            request = asyncio.ensure_future(tools_api.list_tools(session))
            while not server.asked:
                await asyncio.sleep(0.001)
            server.hold = None
            held.set()
            with pytest.raises(ApiError):
                await request

        _run(scenario())
        _settle(panel)

        assert session.selected_account.is_personal


def test_the_account_list_itself_cannot_ask_for_itself(server):
    """It is sent without a team, so even an answer that made no sense could
    not start another read of it."""
    session = _session()
    panel = _panel(session)
    server.account_list_refused = REMOVED

    with pytest.raises(ApiError):
        _run(session.fetch_me())

    assert server.asked == [("/me", None)]
    assert panel.spawned == []
    assert not session.refreshing_accounts
