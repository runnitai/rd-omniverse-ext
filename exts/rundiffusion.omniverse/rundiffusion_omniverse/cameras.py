"""The cameras on the open stage, offered as places to capture from.

Until this, every capture was the active viewport: whatever the user happened
to be looking at when they pressed the button. That is fine for a quick look
and useless for the thing architects actually do, which is iterate on a view
they have already framed and named. A capture from `/World/Cameras/Lobby` is
reproducible next week; a capture from wherever the orbit ended up is not.

Two rules about what belongs in the list.

**Kit's own implicit cameras are left out.** A Kit stage carries
`/OmniverseKit_Persp`, `_Top`, `_Front` and `_Right` whether or not anyone
authored a camera, and the perspective one is usually what the active viewport
is already pointing at. Offering them would put four rows in front of the user
that either duplicate "Active viewport" or answer a question nobody asked. They
are the app's furniture, not the scene's content.

**A name is only a label.** Two cameras can be called `Cam` under different
parents, and a dropdown with two identical rows is one nobody can use. Where a
name repeats, the path is what tells them apart, so the label carries it.

**Being asked is not enough.** `stage_cameras` answers when someone asks, and
for a long time the only thing that asked was an image row being rebuilt. A
camera authored with the panel open therefore did not appear until the user
happened to add an image, switch tool, or leave the Create tab and come back:
the workaround people found was switching tabs and switching back. `StageWatch`
is the other half, and the two together are what make the dropdown current.

Nothing here imports `pxr` or `omni.usd` at module scope. Both exist only
inside a running Kit app, and this module is read by the tests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Kit's own viewport cameras, present on every stage. See the module note.
IMPLICIT_PREFIX = "/OmniverseKit_"


class StageWatch:
    """Calls back when the stage's cameras may have changed.

    Deliberately coarse: it says "ask again", never "a camera called X
    arrived". A removed prim cannot be inspected to find out what it was, so a
    watch that tried to report the difference would have to hold a copy of the
    answer to diff against, which is the caller's job and the caller already
    does it. What this owes the caller is only that it does not stay quiet.

    **Registered globally rather than against a stage.** `Tf.Notice.Register`
    binds to one stage, which would mean tracking stage opens and closes just
    to re-point the listener at the new one. Registering globally makes that
    whole problem disappear: a stage being opened resyncs everything under it,
    so the swap arrives as a notice like any other. The cost is notices from
    stages nobody is looking at, which cost the caller one comparison.

    **Only structural changes.** `GetResyncedPaths` is prims added, removed or
    renamed; `GetChangedInfoOnlyPaths` is attribute values. Orbiting the
    viewport writes a transform on every frame and that is info-only, so
    filtering here is what keeps this from firing continuously while somebody
    is simply looking around.
    """

    def __init__(self, on_changed) -> None:
        #: Called with nothing, on the main thread, when the answer may have
        #: moved. Kit delivers USD notices during its own update.
        self._on_changed = on_changed
        self._listener = None

    @property
    def watching(self) -> bool:
        return self._listener is not None

    def start(self) -> None:
        """Begin listening. Harmless to call when already listening."""
        if self._listener is not None:
            return
        try:
            from pxr import Tf, Usd
        except ImportError:
            # No USD means no stage, which is the state in a test and in a
            # process with no Kit. Nothing to watch is not a failure.
            return
        try:
            self._listener = Tf.Notice.RegisterGlobally(
                Usd.Notice.ObjectsChanged, self._on_objects_changed
            )
        except Exception:  # noqa: BLE001 - a stale dropdown is not worth a crash
            logger.exception("RunDiffusion: could not watch the stage.")

    def stop(self) -> None:
        """Stop listening. Harmless to call when not listening.

        Dropped BEFORE the revoke, so a revoke that raises still leaves this
        object saying it is not watching. The alternative is an object that
        can never be started again because it believes it already is.
        """
        listener, self._listener = self._listener, None
        if listener is None:
            return
        try:
            listener.Revoke()
        except Exception:  # noqa: BLE001 - nothing here can help the user
            logger.exception("RunDiffusion: could not stop watching the stage.")

    def _on_objects_changed(self, notice, _sender) -> None:
        """A USD notice. Runs inside USD, so nothing may escape it.

        An exception raised here does not land in this plugin's log with a
        traceback anyone will find; it lands in the middle of whatever edit the
        user was making.
        """
        try:
            if not notice.GetResyncedPaths():
                return
        except Exception:  # noqa: BLE001 - see the docstring
            logger.exception("RunDiffusion: could not read a stage change.")
            return
        try:
            self._on_changed()
        except Exception:  # noqa: BLE001 - see the docstring
            logger.exception("RunDiffusion: could not answer a stage change.")


@dataclass(frozen=True)
class StageCamera:
    """One camera prim, and what to call it on screen."""

    #: The prim path, which is what a viewport is pointed at and what is
    #: recorded with a run. Stable where a display name is not.
    path: str
    #: The prim's own name, for the common case where it is unique.
    name: str
    #: What the dropdown shows. The name alone where that is unambiguous, and
    #: the name with its path where it is not.
    label: str


def _display_labels(paths_and_names: list[tuple[str, str]]) -> list[StageCamera]:
    """Turn (path, name) pairs into cameras with unambiguous labels.

    Split out from the traversal because it is the half worth testing: the
    traversal needs a stage and this needs nothing.
    """
    seen: dict[str, int] = {}
    for _path, name in paths_and_names:
        seen[name] = seen.get(name, 0) + 1

    cameras = [
        StageCamera(
            path=path,
            name=name,
            # The full path rather than just the parent, because a parent name
            # can repeat as easily as a camera name can.
            label=name if seen.get(name, 0) == 1 else f"{name}  ({path})",
        )
        for path, name in paths_and_names
    ]
    # By label rather than by path, so the order on screen is the order the
    # rows read in. Case-folded, or `lobby` sorts after `Zone` and the list
    # looks unsorted to anyone who names cameras inconsistently.
    return sorted(cameras, key=lambda camera: camera.label.casefold())


def is_implicit(path: str) -> bool:
    """True for a camera Kit put on the stage rather than the user."""
    return path.startswith(IMPLICIT_PREFIX)


def exists(camera_path: str) -> bool:
    """True when this camera prim is still on the open stage.

    Asked before a capture borrows the viewport for it. A path that no longer
    resolves is not an error Kit reports: assigning it to a viewport leaves the
    viewport on whatever it was already showing, so the capture succeeds, comes
    back with a picture of somewhere else, and is labelled with the camera that
    was asked for. A wrong answer delivered confidently. Reported from Base
    Editor on 2026-08-27 after deleting a camera with the panel open.

    True when there is nothing to ask, which is the state in a test and in a
    process with no Kit. Absence of evidence is not evidence of absence, and
    the capture fails for its own reasons a moment later anyway.
    """
    try:
        import omni.usd
        from pxr import Sdf
    except ImportError:
        return True

    context = omni.usd.get_context()
    stage = context.get_stage() if context else None
    if stage is None:
        return True
    prim = stage.GetPrimAtPath(Sdf.Path(camera_path))
    return bool(prim and prim.IsValid())


def stage_cameras() -> list[StageCamera]:
    """Every authored camera on the open stage, ready to be listed.

    Returns an empty list rather than raising when there is no stage, which is
    an ordinary state: a Kit app can be running with nothing open. The dropdown
    then holds only the active viewport, which is exactly what it held before
    this existed.
    """
    try:
        import omni.usd
        from pxr import UsdGeom
    except ImportError:
        logger.warning("RunDiffusion: omni.usd is not available.")
        return []

    context = omni.usd.get_context()
    stage = context.get_stage() if context else None
    if stage is None:
        return []

    found: list[tuple[str, str]] = []
    for prim in stage.Traverse():
        if not prim.IsA(UsdGeom.Camera):
            continue
        path = str(prim.GetPath())
        if is_implicit(path):
            continue
        found.append((path, prim.GetName() or path))
    return _display_labels(found)
