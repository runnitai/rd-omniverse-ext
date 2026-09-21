"""The chosen account has to outlive the Kit session that chose it.

Getting this wrong is expensive and silent in the direction that matters: a
person who works on a team account reopens Kit, the panel quietly falls back to
personal, and the next run is billed to the wrong budget while the footer says
so in words nobody re-reads.

The store itself is DPAPI and Windows-only, so the sealing is stubbed here and
what is tested is the part that has the bugs: what gets written, when it gets
written, and what happens when a remembered account no longer exists.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from rundiffusion_omniverse.api import token_store
from rundiffusion_omniverse.api.session import Account, RdSession

PERSONAL = Account(id="p1", label="Personal", kind="PERSONAL")
TEAM = Account(id="t1", label="Acme", kind="TEAM")
OTHER_TEAM = Account(id="t2", label="Globex", kind="TEAM")


class FakeStore:
    """Stands in for the sealed file, recording every write."""

    def __init__(self, stored: token_store.StoredSession | None = None) -> None:
        self.stored = stored
        self.writes: list[token_store.StoredSession] = []
        self.cleared = False

    def load(self):
        return self.stored

    def save(self, session):
        self.writes.append(session)
        self.stored = session

    def clear(self):
        self.cleared = True
        self.stored = None

    #: The session module asks about this class by name when a write fails.
    SecureStoreUnavailable = token_store.SecureStoreUnavailable
    StoredSession = token_store.StoredSession


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr("rundiffusion_omniverse.api.session.token_store", fake)
    return fake


def _signed_in(store, *accounts: Account) -> RdSession:
    session = RdSession()
    session.refresh_token = "refresh-1"
    session.device_id = "device-1"
    session.accounts = list(accounts)
    return session


class TestChoosing:
    def test_choosing_an_account_writes_it_down(self, store):
        session = _signed_in(store, PERSONAL, TEAM)

        session.choose_account("t1")

        assert session.selected_account_id == "t1"
        assert store.stored.selected_account_id == "t1"

    def test_it_is_written_at_the_moment_of_choosing(self, store):
        """Not at shutdown. A DCC app is closed by being closed, and a
        preference saved on the way out is one lost whenever Kit goes down
        badly."""
        session = _signed_in(store, PERSONAL, TEAM)

        session.choose_account("t1")

        assert len(store.writes) == 1

    def test_choosing_personal_again_is_remembered_as_a_choice(self, store):
        """None means personal, and it has to be written rather than skipped:
        leaving the previous team id on disk would undo the switch back on the
        next launch."""
        session = _signed_in(store, PERSONAL, TEAM)
        session.choose_account("t1")

        session.choose_account(None)

        assert store.stored.selected_account_id is None

    def test_the_credentials_ride_along_unharmed(self, store):
        """The account is written into the same blob, so a careless write here
        would sign the user out."""
        session = _signed_in(store, PERSONAL, TEAM)

        session.choose_account("t1")

        assert store.stored.refresh_token == "refresh-1"
        assert store.stored.device_id == "device-1"

    def test_a_signed_out_session_writes_nothing(self, store):
        session = RdSession()

        session.choose_account("t1")

        assert store.writes == []


class TestRestoring:
    def test_a_remembered_account_that_still_exists_is_taken_back(self, store):
        session = _signed_in(store, PERSONAL, TEAM)

        session.adopt_stored_account("t1")

        assert session.selected_account_id == "t1"
        assert session.selected_account is TEAM

    def test_an_account_that_is_gone_falls_back_to_personal(self, store):
        """Someone is removed from a team, or the team is deleted. The id
        outlives the membership that made it mean anything."""
        session = _signed_in(store, PERSONAL)

        session.adopt_stored_account("t1")

        assert session.selected_account_id is None
        assert session.selected_account is PERSONAL

    def test_a_dead_account_is_forgotten_rather_than_rewritten_forever(self, store):
        session = _signed_in(store, PERSONAL)

        session.adopt_stored_account("t1")

        assert store.stored is not None
        assert store.stored.selected_account_id is None

    def test_remembering_nothing_changes_nothing(self, store):
        session = _signed_in(store, PERSONAL, TEAM)
        session.selected_account_id = "t1"

        session.adopt_stored_account(None)

        assert session.selected_account_id == "t1"
        assert store.writes == []

    def test_restore_applies_the_account_before_the_token_refresh(self, monkeypatch):
        """The refresh rewrites this same file when the server rotates the
        token. Restoring the account afterwards would persist None over the
        remembered choice first, and lose it for good if anything failed in
        between."""
        fake = FakeStore(
            token_store.StoredSession(
                refresh_token="refresh-1", device_id="device-1", selected_account_id="t1"
            )
        )
        monkeypatch.setattr("rundiffusion_omniverse.api.session.token_store", fake)
        session = RdSession()

        async def fake_refresh():
            # What the real one does at the end of a rotation.
            session._persist()

        async def fake_fetch_me():
            return {
                "user": {"email": "a@b.com"},
                "accounts": [
                    {"id": "p1", "label": "Personal", "kind": "PERSONAL"},
                    {"id": "t1", "label": "Acme", "kind": "TEAM"},
                ],
            }

        monkeypatch.setattr(session, "refresh", fake_refresh)
        monkeypatch.setattr(session, "fetch_me", fake_fetch_me)

        assert asyncio.run(session.restore()) is True

        assert session.selected_account_id == "t1"
        # The write the refresh made already carried it.
        assert store_ids(fake) == ["t1"]


def store_ids(fake: FakeStore) -> list[str | None]:
    return [write.selected_account_id for write in fake.writes]


class TestSigningOut:
    def test_signing_out_forgets_the_account(self, store):
        """The next person to sign in on this machine must not inherit a team.
        The file goes too, but the in-memory value is what the panel reads while
        it draws the sign-in screen."""
        session = _signed_in(store, PERSONAL, TEAM)
        session.choose_account("t1")

        asyncio.run(session.sign_out())

        assert session.selected_account_id is None
        assert store.cleared


class TestWhatIsWrittenToDisk:
    """The sealing is Windows DPAPI; the payload is plain JSON inside it.

    Stubbing only the seal leaves the actual read and write paths under test,
    which is where a renamed key would silently drop the account on the floor.
    """

    @pytest.fixture
    def unsealed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(token_store, "_protect", lambda data: data)
        monkeypatch.setattr(token_store, "_unprotect", lambda data: data)
        monkeypatch.setattr(token_store, "is_supported", lambda: True)
        monkeypatch.setattr(token_store, "_store_path", lambda: tmp_path / "session.bin")
        return tmp_path / "session.bin"

    def test_the_account_survives_a_round_trip(self, unsealed):
        token_store.save(
            token_store.StoredSession("refresh-1", "device-1", "t1")
        )

        assert token_store.load().selected_account_id == "t1"

    def test_a_blob_written_before_this_feature_still_loads(self, unsealed):
        """An existing install has a sealed file with two keys in it. Reading it
        must mean "no account remembered", not "corrupt", because corrupt takes
        the whole session down."""
        unsealed.write_bytes(
            json.dumps({"refresh_token": "refresh-1", "device_id": "device-1"}).encode()
        )

        stored = token_store.load()

        assert stored.refresh_token == "refresh-1"
        assert stored.selected_account_id is None

    def test_personal_round_trips_as_nothing_rather_than_as_missing(self, unsealed):
        token_store.save(token_store.StoredSession("refresh-1", "device-1", None))

        assert token_store.load().selected_account_id is None


class FakeComboModel:
    def __init__(self, index: int) -> None:
        self._index = index

    def get_item_value_model(self):
        return type("Value", (), {"as_int": self._index})()


class FakeCombo:
    def __init__(self, index: int) -> None:
        self.model = FakeComboModel(index)


class TestThePickerActuallyChooses:
    """The footer's BILLED TO picker was dead for a fortnight.

    A botched edit indented `choose_account` into the guard above it, so the
    call sat after that guard's `return` and never ran. Nothing looked wrong:
    switching accounts still refetched the tools and the library, so the panel
    redrew as though it had worked, while every later request carried the old
    `team_id` and every run was billed to the account the user had just
    switched away from.

    It survived because the tests reached past the handler and called
    `choose_account` directly. These go through the handler.
    """

    def _panel(self, session, index: int):
        from rundiffusion_omniverse.panel import RunDiffusionPanel

        panel = object.__new__(RunDiffusionPanel)
        panel._session = session
        panel._account_combo = FakeCombo(index)
        panel._current_tab = 0
        panel._tools = []
        panel._kits = []
        panel._tool_tags = {}
        panel._selected_tool_id = None
        panel._tool_detail = None
        panel._library_items = []
        panel._library_cursor = None
        panel._library_has_more = False
        panel._library_generation = 0
        panel._uploads_items = []
        panel._uploads_cursor = None
        panel._uploads_has_more = False
        panel._uploads_generation = 0
        # Keyed by surface since the library can be torn out into its own
        # window and the two grids each hold their own selection.
        panel._selected_asset = {}
        panel._asset_actions_frame = {}
        # Docked, so there is no separate window to rebuild. The torn-out
        # case is TestSwitchingAccountWithASurfaceTornOut below.
        panel._torn_out = {"library": False, "uploads": False}
        panel._windows = {}
        panel._clear_wall = lambda: None
        panel._set_cost = lambda _message: None
        panel._show_tab = lambda _index: None
        panel._check_plugin_access = lambda: False  # stops before the refetches
        return panel

    def test_choosing_a_team_selects_it_on_the_session(self, store):
        session = _signed_in(store, PERSONAL, TEAM)

        self._panel(session, 1)._on_account_changed()

        assert session.selected_account_id == TEAM.id
        assert session.account_params() == {"team_id": TEAM.id}

    def test_choosing_a_team_is_also_remembered(self, store):
        """The whole point of going through the session rather than assigning
        the attribute: `choose_account` persists, assignment does not."""
        session = _signed_in(store, PERSONAL, TEAM)

        self._panel(session, 1)._on_account_changed()

        assert store.writes
        assert store.writes[-1].selected_account_id == TEAM.id

    def test_choosing_personal_goes_back_to_omitting_the_account(self, store):
        session = _signed_in(store, PERSONAL, TEAM)
        session.choose_account(TEAM.id)

        self._panel(session, 0)._on_account_changed()

        assert session.account_params() == {}

    def test_an_index_outside_the_list_changes_nothing(self, store):
        session = _signed_in(store, PERSONAL, TEAM)
        session.choose_account(TEAM.id)

        self._panel(session, 7)._on_account_changed()

        assert session.selected_account_id == TEAM.id


class TestSwitchingAccountWithASurfaceTornOut:
    """A surface in its own window is not rebuilt by a tab switch.

    Dropping the held pages redraws nothing by itself, so without this the
    window goes on showing the previous account's thumbnails with one of them
    still selected, and Save or Open fetches another account's asset while the
    footer says this one is paying.
    """

    def _panel(self, session, *, torn_out: dict, windows: dict):
        from rundiffusion_omniverse.panel import RunDiffusionPanel

        panel = object.__new__(RunDiffusionPanel)
        panel._session = session
        panel._account_combo = FakeCombo(1)
        panel._current_tab = 0
        panel._tools = []
        panel._kits = []
        panel._tool_tags = {}
        panel._selected_tool_id = None
        panel._tool_detail = None
        panel._library_items = ["someone-elses-render"]
        panel._library_cursor = None
        panel._library_has_more = False
        panel._library_generation = 0
        panel._uploads_items = []
        panel._uploads_cursor = None
        panel._uploads_has_more = False
        panel._uploads_generation = 0
        panel._selected_asset = {}
        panel._asset_actions_frame = {}
        panel._torn_out = torn_out
        panel._windows = windows
        panel.rebuilds = []
        panel._open_window = panel.rebuilds.append
        panel._clear_wall = lambda: None
        panel._set_cost = lambda _message: None
        panel._show_tab = lambda _index: None
        panel._check_plugin_access = lambda: False
        return panel

    def test_a_docked_surface_on_screen_is_rebuilt_too(self, store):
        """The case that was missed. Only the Account tab was rebuilt, so a
        user sitting on the Library tab when the account changed kept the
        previous account's grid, with the in-cell Save and Open still live on
        every thumbnail in it."""
        from rundiffusion_omniverse.panel import TAB_LIBRARY

        session = _signed_in(store, PERSONAL, TEAM)
        panel = self._panel(
            session,
            torn_out={"library": False, "uploads": False},
            windows={},
        )
        panel._current_tab = TAB_LIBRARY
        shown: list = []
        panel._show_tab = shown.append

        panel._on_account_changed()

        assert shown == [TAB_LIBRARY]

    def test_the_library_window_is_rebuilt_for_the_new_account(self, store):
        session = _signed_in(store, PERSONAL, TEAM)
        panel = self._panel(
            session,
            torn_out={"library": True, "uploads": False},
            windows={"library": object()},
        )

        panel._on_account_changed()

        assert panel.rebuilds == ["library"]
        assert panel._library_items == []

    def test_the_uploads_window_is_too(self, store):
        """Uploads belong to the account exactly as the library does."""
        session = _signed_in(store, PERSONAL, TEAM)
        panel = self._panel(
            session,
            torn_out={"library": False, "uploads": True},
            windows={"uploads": object()},
        )

        panel._on_account_changed()

        assert panel.rebuilds == ["uploads"]

    def test_both_are_when_both_are_out(self, store):
        session = _signed_in(store, PERSONAL, TEAM)
        panel = self._panel(
            session,
            torn_out={"library": True, "uploads": True},
            windows={"library": object(), "uploads": object()},
        )

        panel._on_account_changed()

        assert sorted(panel.rebuilds) == ["library", "uploads"]

    def test_a_docked_surface_is_left_to_the_tab_switch(self, store):
        session = _signed_in(store, PERSONAL, TEAM)
        panel = self._panel(
            session,
            torn_out={"library": False, "uploads": False},
            windows={},
        )

        panel._on_account_changed()

        assert panel.rebuilds == []

    def test_a_window_that_was_never_opened_is_not_built_now(self, store):
        """Torn out is remembered across sessions, so the preference can say
        yes before the workbench has built anything."""
        session = _signed_in(store, PERSONAL, TEAM)
        panel = self._panel(
            session,
            torn_out={"library": True, "uploads": True},
            windows={},
        )

        panel._on_account_changed()

        assert panel.rebuilds == []
