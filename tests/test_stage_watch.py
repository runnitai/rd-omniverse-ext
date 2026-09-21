"""Hearing that the stage moved, so the camera dropdown stops going stale.

`stage_cameras` answers when it is asked, and for a while the only thing that
asked was an image row being rebuilt. Authoring a camera is not one of the
things that rebuilds a row, so a camera created with the panel open did not
appear in the dropdown at all until the user happened to add an image, switch
tool, or leave the Create tab and come back. Reported off the branch, with
"switch tabs and switch back" as the workaround people had found.

The watch is the other half. What it owes the caller is small and exact: it
must speak up when prims appear or disappear, it must stay quiet while
somebody is merely looking around, and it must never let anything escape into
the middle of a USD edit.
"""

from __future__ import annotations

import sys
import types

import pytest

from rundiffusion_omniverse.cameras import StageWatch


class FakeListener:
    def __init__(self) -> None:
        self.revoked = 0

    def Revoke(self) -> None:  # noqa: N802 - USD's own spelling
        self.revoked += 1


class FakeNotice:
    """Stands in for Usd.Notice.ObjectsChanged."""

    def __init__(self, resynced=(), info_only=()) -> None:
        self._resynced = list(resynced)
        self._info_only = list(info_only)

    def GetResyncedPaths(self):  # noqa: N802 - USD's own spelling
        return self._resynced

    def GetChangedInfoOnlyPaths(self):  # noqa: N802 - USD's own spelling
        return self._info_only


@pytest.fixture
def pxr(monkeypatch):
    """A `pxr` that records what was registered, in a process with no USD."""
    state = types.SimpleNamespace(registered=[], listener=FakeListener(), fail=None)

    def register_globally(notice_type, callback):
        if state.fail is not None:
            raise state.fail
        state.registered.append((notice_type, callback))
        return state.listener

    tf = types.SimpleNamespace(
        Notice=types.SimpleNamespace(RegisterGlobally=register_globally)
    )
    usd = types.SimpleNamespace(
        Notice=types.SimpleNamespace(ObjectsChanged="ObjectsChanged")
    )
    module = types.ModuleType("pxr")
    module.Tf = tf
    module.Usd = usd
    monkeypatch.setitem(sys.modules, "pxr", module)
    return state


class TestStartingAndStopping:
    def test_it_registers_for_object_changes(self, pxr):
        StageWatch(lambda: None).start()

        assert [notice for notice, _cb in pxr.registered] == ["ObjectsChanged"]

    def test_it_registers_globally_rather_than_against_one_stage(self, pxr):
        """`Tf.Notice.Register` binds to a stage, which would mean tracking
        opens and closes just to re-point the listener. Registering globally
        makes the swap arrive as an ordinary notice instead."""
        watch = StageWatch(lambda: None)

        watch.start()

        assert watch.watching
        assert pxr.registered, "nothing was registered"

    def test_starting_twice_registers_once(self, pxr):
        watch = StageWatch(lambda: None)

        watch.start()
        watch.start()

        assert len(pxr.registered) == 1

    def test_stopping_revokes_the_listener(self, pxr):
        watch = StageWatch(lambda: None)
        watch.start()

        watch.stop()

        assert pxr.listener.revoked == 1
        assert not watch.watching

    def test_stopping_twice_revokes_once(self, pxr):
        watch = StageWatch(lambda: None)
        watch.start()

        watch.stop()
        watch.stop()

        assert pxr.listener.revoked == 1

    def test_stopping_one_that_never_started_is_harmless(self, pxr):
        StageWatch(lambda: None).stop()

        assert pxr.listener.revoked == 0

    def test_it_can_be_started_again_after_stopping(self, pxr):
        """The Create tab is left and returned to all day."""
        watch = StageWatch(lambda: None)

        watch.start()
        watch.stop()
        watch.start()

        assert len(pxr.registered) == 2
        assert watch.watching


class TestWhenThereIsNoUsdAtAll:
    def test_starting_is_harmless(self, monkeypatch):
        """The state in a test and in a process with no Kit. Nothing to watch
        is not a failure."""
        monkeypatch.setitem(sys.modules, "pxr", None)
        watch = StageWatch(lambda: None)

        watch.start()

        assert not watch.watching

    def test_a_registration_that_fails_leaves_it_not_watching(self, pxr):
        """A stale dropdown is not worth taking the panel down for."""
        pxr.fail = RuntimeError("no")
        watch = StageWatch(lambda: None)

        watch.start()

        assert not watch.watching

    def test_a_revoke_that_fails_still_lets_go(self, pxr):
        """Otherwise the watch can never be started again, because it believes
        it already is."""
        watch = StageWatch(lambda: None)
        watch.start()
        pxr.listener.Revoke = lambda: (_ for _ in ()).throw(RuntimeError("no"))

        watch.stop()

        assert not watch.watching


class TestWhatItPassesOn:
    def _fired(self, pxr, notice) -> list:
        told: list = []
        watch = StageWatch(lambda: told.append("changed"))
        watch.start()
        _type, callback = pxr.registered[0]
        callback(notice, object())
        return told

    def test_a_prim_appearing_is_worth_saying(self, pxr):
        assert self._fired(pxr, FakeNotice(resynced=["/World/Cameras/Lobby"])) == [
            "changed"
        ]

    def test_an_attribute_edit_is_not(self, pxr):
        """Orbiting the viewport writes a transform on every frame. Without
        this filter the panel would be told the stage moved continuously while
        somebody was simply looking around."""
        notice = FakeNotice(resynced=[], info_only=["/OmniverseKit_Persp.xformOp"])

        assert self._fired(pxr, notice) == []

    def test_it_does_not_try_to_say_what_changed(self, pxr):
        """A removed prim cannot be inspected to find out what it was, so the
        watch reports only that the answer may have moved. The caller compares
        the new list with the old, which it has to do anyway."""
        told: list = []
        watch = StageWatch(lambda *args: told.append(args))
        watch.start()
        _type, callback = pxr.registered[0]

        callback(FakeNotice(resynced=["/World/AnyOldPrim"]), object())

        assert told == [()]


class TestNothingEscapesIntoUsd:
    """These run inside USD's own notice dispatch. An exception here does not
    land in this plugin's log where somebody will find it; it lands in the
    middle of whatever edit the user was making."""

    def test_a_callback_that_raises_is_contained(self, pxr):
        watch = StageWatch(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        watch.start()
        _type, callback = pxr.registered[0]

        callback(FakeNotice(resynced=["/World/X"]), object())

    def test_a_notice_that_cannot_be_read_is_contained(self, pxr):
        told: list = []
        watch = StageWatch(lambda: told.append("changed"))
        watch.start()
        _type, callback = pxr.registered[0]

        class Unreadable:
            def GetResyncedPaths(self):  # noqa: N802 - USD's own spelling
                raise RuntimeError("no")

        callback(Unreadable(), object())

        assert told == []
