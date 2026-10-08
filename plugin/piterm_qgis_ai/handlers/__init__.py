"""Handler hub: one object combining every command domain.

``build_hub()`` returns the hub with its session managers wired up; the plugin
then constructs the server around it. Keeping the hub plain (no QObject) makes
dispatch unit-testable outside the Qt event loop.
"""

from __future__ import annotations

from .base import HandlerBase
from .system import SystemHandlers
from .project import ProjectHandlers
from .layers import LayerHandlers
from .features import FeatureHandlers
from .raster import RasterHandlers
from .processing import ProcessingHandlers, JobHandlers
from .canvas import CanvasHandlers
from .style import StyleHandlers
from .layout import LayoutHandlers
from .session import SessionHandlers
from .io import IoHandlers
from .connections import ConnectionHandlers
from .plugins import PluginHandlers
from .expression import ExpressionHandlers
from .python_exec import PythonHandlers
from .ui import UiHandlers


class HandlerHub(SystemHandlers, ProjectHandlers, LayerHandlers,
                 FeatureHandlers, RasterHandlers, ProcessingHandlers,
                 JobHandlers, CanvasHandlers, StyleHandlers, LayoutHandlers,
                 SessionHandlers, IoHandlers, ConnectionHandlers,
                 PluginHandlers, ExpressionHandlers, PythonHandlers,
                 UiHandlers, HandlerBase):
    """All domain mixins; HandlerBase last so it never shadows anything."""


def build_hub(**managers):
    """Create the hub and attach shared managers (journal, jobs, cursors,
    checkpoints) supplied by the plugin."""
    hub = HandlerHub()
    for name, value in managers.items():
        setattr(hub, name, value)
    return hub
