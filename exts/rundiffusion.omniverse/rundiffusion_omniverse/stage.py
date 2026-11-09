"""Putting a generated 3D asset onto the stage the user already has open.

This is the one thing this plugin can do that no other RunDiffusion surface
can, and it is also the one that touches something the user owns. Their stage
is their work, so the rules here are narrower than anywhere else in the panel:

**Reference, never merge.** The asset arrives as its own file and is referenced
under a prim of its own. Nothing existing is edited, and removing the prim
removes the asset completely.

**Under one parent.** Everything this plugin adds goes under `/RunDiffusion`,
so a person can find what came from here and delete it in one action without
hunting through their hierarchy.

**Through a command, so it can be undone.** Ctrl+Z is what a user reaches for
when something lands wrong, and an insertion done straight through the USD API
does not appear in the undo stack. The direct call is kept as a fallback for a
host with no command registered, and says so rather than pretending it undoes.

**USD is referenceable; the rest is not.** A `.usd`, `.usda`, `.usdc` or
`.usdz` can be referenced as it stands. A `.glb`, `.gltf`, `.obj` or `.fbx`
cannot, and has to be converted first. `omni.kit.asset_converter` ships in the
stock Kit SDK and in the packaged Composer host (both checked before depending
on it), so the conversion costs no vendoring.

**At the size the user asked for.** A generated model has no real-world size:
it is about one unit across in whatever unit its file happens to claim, so in a
centimetre stage it arrives as a speck and in a metre stage as a chair-sized
blob, whatever it depicts. The user names the length of its longest side and a
unit, and the new prim is scaled so its bounding box matches. Unlike most
hosts, a USD stage records its own unit (`metersPerUnit`), so "2.5 m" can be
honoured in any stage rather than read as "2.5 of whatever this document uses".
"""

from __future__ import annotations

import contextlib
import logging
import math
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: The prim everything this plugin adds lives under.
ROOT_PATH = "/RunDiffusion"

#: Referenceable as they stand.
USD_SUFFIXES = frozenset({".usd", ".usda", ".usdc", ".usdz"})

#: Need `omni.kit.asset_converter` first. USD cannot reference these directly,
#: and referencing one anyway produces a prim that resolves to nothing: an
#: insertion that reports success and adds an empty Xform.
CONVERTIBLE_SUFFIXES = frozenset({".glb", ".gltf", ".obj", ".fbx"})


#: The units a size can be given in, as (unit, meters in one of it), in the
#: order the dropdown lists them. Metric first, then the two imperial units an
#: architect or a set designer actually measures in.
UNITS: tuple[tuple[str, float], ...] = (
    ("m", 1.0),
    ("cm", 0.01),
    ("mm", 0.001),
    ("ft", 0.3048),
    ("in", 0.0254),
)

_METERS_PER = dict(UNITS)

#: What a stage with no `metersPerUnit` authored is measured in. USD's own
#: fallback, so this agrees with what every other tool reading the stage assumes.
DEFAULT_STAGE_METERS_PER_UNIT = 0.01

#: Below this, a bounding box is treated as having no size at all. Scaling a
#: degenerate box up to a real length would divide by nearly nothing and throw
#: the prim to an absurd scale.
_DEGENERATE = 1e-9


@dataclass(frozen=True)
class InsertSize:
    """How long the asset's longest side should be, in a unit the user chose."""

    length: float
    unit: str

    def in_stage_units(self, meters_per_unit: float) -> float:
        """This length expressed in a stage measured in `meters_per_unit`."""
        return target_in_stage_units(self.length, self.unit, meters_per_unit)


@dataclass(frozen=True)
class Inserted:
    """Where an asset landed, and whether it took the size it was asked for."""

    path: str
    #: False only when a size was asked for and could not be applied. The asset
    #: is still on the stage at its original size, and the user should be told
    #: that rather than find out with a tape measure.
    sized: bool = True


def meters_per(unit: str) -> float:
    """Meters in one `unit`. Raises ValueError for a unit not in UNITS."""
    try:
        return _METERS_PER[unit]
    except KeyError:
        raise ValueError(f"Unknown unit {unit!r}.") from None


def is_known_unit(unit: str) -> bool:
    return unit in _METERS_PER


def target_in_stage_units(length: float, unit: str, meters_per_unit: float) -> float:
    """`length` of `unit`, expressed in a stage measured in `meters_per_unit`.

    A stage reporting a unit of zero or less is not measurable, so it is read as
    USD's default rather than dividing by it.
    """
    if not (math.isfinite(meters_per_unit) and meters_per_unit > 0):
        meters_per_unit = DEFAULT_STAGE_METERS_PER_UNIT
    return length * meters_per(unit) / meters_per_unit


def scale_factor(current: float, target: float) -> float:
    """What to multiply the asset's scale by so `current` becomes `target`.

    1.0, meaning leave it alone, when either side is not a usable length: an
    empty or unmeasurable asset is inserted as it came rather than blown up.
    """
    if not (math.isfinite(current) and math.isfinite(target)):
        return 1.0
    if current <= _DEGENERATE or target <= _DEGENERATE:
        return 1.0
    return target / current


def needs_conversion(path: Path) -> bool:
    """True when this file has to become USD before the stage can take it."""
    return path.suffix.lower() in CONVERTIBLE_SUFFIXES


def is_supported(path: Path) -> bool:
    """True when this file can reach the stage at all, converted or not."""
    suffix = path.suffix.lower()
    return suffix in USD_SUFFIXES or suffix in CONVERTIBLE_SUFFIXES


def usd_name(text: str, fallback: str = "asset") -> str:
    """`text` as a name USD will accept, or `fallback` when nothing survives.

    USD accepts letters, digits and underscores, and will not accept a name
    starting with a digit. A generated result is named `result-<hex>.glb`, so
    without this every insertion would be rejected on the hyphen.
    """
    cleaned = "".join(
        character if character.isalnum() else "_" for character in (text or "")
    ).strip("_")
    # Stripped first, so a name that was nothing but separators reads as having
    # no name at all rather than becoming a row of underscores.
    if not cleaned:
        return fallback
    if cleaned[0].isdigit():
        return f"{fallback}_{cleaned}"
    return cleaned


def prim_name(source: Path) -> str:
    """A USD-legal prim name derived from the file."""
    return usd_name(source.stem)


def unique_prim_path(taken: set[str], name: str, root: str = ROOT_PATH) -> str:
    """`/RunDiffusion/<name>`, suffixed until it collides with nothing.

    Generating the same tool twice is ordinary, and the second result must not
    silently replace the first one on the stage.
    """
    candidate = f"{root}/{name}"
    if candidate not in taken:
        return candidate
    index = 2
    while f"{candidate}_{index}" in taken:
        index += 1
    return f"{candidate}_{index}"


def _sdf():
    from pxr import Sdf

    return Sdf


def run_command(name: str, **kwargs) -> bool:
    """Run one Kit command and say whether it actually worked.

    `omni.kit.commands.execute` does not raise when the command inside it
    fails: it logs and hands back a falsey result. Ignoring that return value
    is how a failed AddReference came to look like a successful insertion.
    """
    import omni.kit.commands

    outcome = omni.kit.commands.execute(name, **kwargs)
    if isinstance(outcome, tuple):
        return bool(outcome and outcome[0])
    return outcome is not False


def _has_reference(stage, path: str) -> bool:
    """Whether the prim exists AND something is actually referenced on it.

    An Xform with no reference is the shape a failed insertion leaves behind:
    a prim in the tree and nothing to see in the viewport.
    """
    try:
        prim = stage.GetPrimAtPath(path)
        return bool(prim and prim.IsValid() and prim.HasAuthoredReferences())
    except Exception:  # noqa: BLE001 - an unreadable prim is not a success
        logger.exception("RunDiffusion: could not confirm the reference on %s.", path)
        return False


def _undo_group():
    """One undo step for everything inside it, or nothing where Kit has none.

    Without it, taking an insertion back off the stage is three presses of
    Ctrl+Z (the scale, then the reference, then the prim), and the first press
    leaves the asset sitting there at the wrong size, which looks like undo
    did nothing.
    """
    try:
        import omni.kit.undo

        return omni.kit.undo.group()
    except Exception:  # noqa: BLE001 - ungrouped still undoes, just in steps
        return contextlib.nullcontext()


def stage_meters_per_unit(stage) -> float:
    """What one unit of this stage is in meters."""
    from pxr import UsdGeom

    value = UsdGeom.GetStageMetersPerUnit(stage)
    return value if value and value > 0 else DEFAULT_STAGE_METERS_PER_UNIT


def longest_side(stage, path: str) -> float:
    """The longest side of the prim's world-space bounding box, in stage units.

    World space, so whatever already scales this prim is counted: a parent the
    user has transformed, or a units correction Kit put on the prim when the
    reference arrived in a different unit from the stage. Measuring anything
    less would size the asset correctly in its own file and wrongly on screen.
    """
    from pxr import Usd, UsdGeom

    prim = stage.GetPrimAtPath(path)
    cache = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render],
        useExtentsHint=False,
    )
    box = cache.ComputeWorldBound(prim).ComputeAlignedRange()
    if box.IsEmpty():
        return 0.0
    return max(box.GetSize())


def _apply_size(stage, path: str, size: InsertSize, *, undoable: bool) -> bool:
    """Scale the prim at `path` so its longest side is `size`. True when it is.

    The factor MULTIPLIES the prim's existing scale rather than replacing it,
    because the prim may already carry one this plugin did not write: Kit can
    correct a reference's units with a scale on the referencing prim, and
    overwriting that would undo the correction and land the asset off by the
    ratio of the two units.
    """
    try:
        from pxr import Gf

        current = longest_side(stage, path)
        if current <= _DEGENERATE:
            logger.info("RunDiffusion: %s has no measurable size to scale.", path)
            return False
        factor = scale_factor(current, size.in_stage_units(stage_meters_per_unit(stage)))
        if factor == 1.0:
            return True

        if undoable:
            import omni.usd

            prim = stage.GetPrimAtPath(path)
            old_scale = omni.usd.get_local_transform_SRT(prim)[0]
            new_scale = Gf.Vec3d(*(axis * factor for axis in old_scale))
            if not run_command("TransformPrimSRTCommand", path=path, new_scale=new_scale):
                return False
        else:
            from pxr import UsdGeom

            common = UsdGeom.XformCommonAPI(stage.GetPrimAtPath(path))
            old_scale = common.GetXformVectors(_default_time())[1]
            new_scale = Gf.Vec3f(*(axis * factor for axis in old_scale))
            if not common.SetScale(new_scale):
                return False
    except Exception:  # noqa: BLE001 - an unsized asset is still an inserted one
        logger.exception("RunDiffusion: could not size %s.", path)
        return False
    return True


def _default_time():
    from pxr import Usd

    return Usd.TimeCode.Default()


def select(path: str) -> None:
    """Select the new prim, so 'added to the stage' is visibly true.

    A referenced asset can land anywhere in a large scene, and an insertion
    nobody can find reads exactly like one that did not happen.
    """
    try:
        import omni.usd

        context = omni.usd.get_context()
        if context is not None:
            context.get_selection().set_selected_prim_paths([path], True)
    except Exception:  # noqa: BLE001 - selection is a courtesy, not the result
        logger.debug("RunDiffusion: could not select %s.", path)


def existing_paths(stage) -> set[str]:
    try:
        return {prim.GetPath().pathString for prim in stage.Traverse()}
    except Exception:  # noqa: BLE001 - an unreadable stage is not a crash here
        logger.exception("RunDiffusion: could not read the stage's prims.")
        return set()


async def convert_to_usd(source: Path, destination: Path) -> bool:
    """Turn a non-USD asset into something the stage can reference.

    Returns False rather than raising: a conversion that cannot run leaves the
    file on disk, where saving it and sending it still work.
    """
    try:
        import omni.kit.asset_converter as converter
    except ImportError:
        logger.warning(
            "RunDiffusion: omni.kit.asset_converter is not available, so %s "
            "cannot be added to the stage.",
            source.suffix,
        )
        return False

    try:
        task = converter.get_instance().create_converter_task(
            str(source), str(destination), None
        )
        if not await task.wait_until_finished():
            logger.warning(
                "RunDiffusion: could not convert %s. %s",
                source.name,
                task.get_error_message(),
            )
            return False
    except Exception:  # noqa: BLE001 - conversion is a bonus, not the result
        logger.exception("RunDiffusion: the asset conversion failed.")
        return False
    return destination.exists()


async def add_to_stage(
    source: Path, work_dir: Path, size: InsertSize | None = None
) -> Inserted | None:
    """Reference `source` onto the open stage. Returns where it landed, or None.

    `work_dir` is where a converted copy is written, so the original the user
    can save is never moved or rewritten. `size` is the length its longest
    side should be; None inserts it at whatever size it arrives.
    """
    try:
        import omni.usd
    except ImportError:
        logger.warning("RunDiffusion: omni.usd is not available.")
        return None

    if not is_supported(source):
        logger.info("RunDiffusion: %s cannot be added to a stage.", source.suffix)
        return None

    asset = source
    if needs_conversion(source):
        converted = work_dir / f"{source.stem}.usd"
        if not await convert_to_usd(source, converted):
            return None
        asset = converted

    context = omni.usd.get_context()
    stage = context.get_stage() if context else None
    if stage is None:
        logger.info("RunDiffusion: there is no open stage to add to.")
        return None

    taken = existing_paths(stage)
    path = unique_prim_path(taken, prim_name(source))

    try:
        # Through the commands so Ctrl+Z takes it back off the stage. The prim
        # has to exist before a reference can be put on it, and it can only be
        # measured once the reference has brought its geometry in. Every step
        # is a command in one undo group, so one Ctrl+Z removes the lot.
        with _undo_group():
            # The parent is created as a step of its own when this is the
            # first insertion. Left to CreatePrimCommand it appears implicitly,
            # outside the undo stack, and undo leaves an empty /RunDiffusion
            # behind on a stage that never had one.
            if (ROOT_PATH in taken or run_command(
                "CreatePrimCommand",
                prim_path=ROOT_PATH,
                prim_type="Xform",
                select_new_prim=False,
            )) and run_command(
                "CreatePrimCommand",
                prim_path=path,
                prim_type="Xform",
                select_new_prim=False,
            ) and run_command(
                "AddReference",
                stage=stage,
                prim_path=_sdf().Path(path),
                # An Sdf.Reference, not a string. The command reads `.assetPath`
                # off whatever it is given, so a path string raises inside it
                # and the command reports the failure without raising to the
                # caller.
                reference=_sdf().Reference(str(asset)),
            ) and _has_reference(stage, path):
                sized = size is None or _apply_size(stage, path, size, undoable=True)
                return Inserted(path, sized)
        logger.info(
            "RunDiffusion: the undoable insertion did not take, adding directly. "
            "This one will not come back with Ctrl+Z."
        )
    except Exception:  # noqa: BLE001 - fall back rather than lose the insertion
        logger.exception("RunDiffusion: the undoable insertion raised.")

    try:
        prim = stage.DefinePrim(path, "Xform")
        prim.GetReferences().AddReference(str(asset))
    except Exception:  # noqa: BLE001 - the file is still on disk either way
        logger.exception("RunDiffusion: could not add the asset to the stage.")
        return None

    # Verified rather than assumed. The first version reported the prim path
    # whatever happened, so a reference that never attached was indistinguishable
    # from one that did: the panel said it had been added and the stage was
    # unchanged.
    if not _has_reference(stage, path):
        logger.warning("RunDiffusion: the reference did not attach to %s.", path)
        return None
    sized = size is None or _apply_size(stage, path, size, undoable=False)
    return Inserted(path, sized)
