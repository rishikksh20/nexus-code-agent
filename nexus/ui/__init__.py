"""nexus.ui — Terminal UI package.

All Rich-based console interactions, theming, and output formatting for the
Nexus agent framework are centralised here.  Import :class:`TerminalUI` in
place of ``rich.console.Console`` anywhere inside the nexus package.
"""

from nexus.ui.terminal import NEXUS_THEME, TerminalUI

__all__ = ["NEXUS_THEME", "TerminalUI"]
