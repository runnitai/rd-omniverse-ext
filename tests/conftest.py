"""Let the pure-Python layers be tested outside Kit.

The extension package's `__init__` imports `omni.ext`, which only exists inside
a running Kit app. Stubbing the host here means the request-shaping code, which
is where this plugin's bugs have actually been, can be tested in a plain Python
process with no app, no GPU, and no network.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

EXT_ROOT = Path(__file__).resolve().parents[1] / "exts" / "rundiffusion.omniverse"


def _stub_omni() -> None:
    for name in ("omni", "omni.ext", "omni.ui"):
        if name in sys.modules:
            continue
        module = types.ModuleType(name)
        if name == "omni.ext":
            module.IExt = object
        sys.modules[name] = module
    sys.modules["omni"].ext = sys.modules["omni.ext"]
    sys.modules["omni"].ui = sys.modules["omni.ui"]
    # Alignment is a plain enumeration the layout code reads as it builds, so
    # a test that draws into fake widgets needs the names to exist. Their
    # values mean nothing without a renderer, which is the point of the stub.
    if not hasattr(sys.modules["omni.ui"], "Alignment"):
        sys.modules["omni.ui"].Alignment = types.SimpleNamespace(
            LEFT_CENTER="LEFT_CENTER",
            CENTER="CENTER",
            RIGHT_CENTER="RIGHT_CENTER",
            RIGHT_BOTTOM="RIGHT_BOTTOM",
            CENTER_BOTTOM="CENTER_BOTTOM",
        )


_stub_omni()

if str(EXT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXT_ROOT))
