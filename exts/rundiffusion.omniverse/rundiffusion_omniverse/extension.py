"""Extension entry point.

Kit instantiates any omni.ext.IExt subclass in the module named by
`[[python.module]]` in extension.toml, calls on_startup when the extension is
enabled, and on_shutdown when it is disabled. Both run on the main thread.
"""

from __future__ import annotations

import logging

import omni.ext

from .panel import RunDiffusionPanel

logger = logging.getLogger(__name__)


class RunDiffusionExtension(omni.ext.IExt):
    """Owns the panel for as long as the extension is enabled."""

    def __init__(self) -> None:
        super().__init__()
        self._panel: RunDiffusionPanel | None = None

    def on_startup(self, _ext_id: str) -> None:
        logger.info("RunDiffusion: extension startup.")
        self._panel = RunDiffusionPanel()

    def on_shutdown(self) -> None:
        logger.info("RunDiffusion: extension shutdown.")
        if self._panel is not None:
            self._panel.destroy()
            self._panel = None
