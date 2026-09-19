"""Supported import surface for plugins beyond ``exitzero.api``.

Plugins may rely on the names re-exported here plus the ``exitzero.api``
contract.  Importing any other core module bypasses the versioned plugin
contract and is not covered by ``API_VERSION`` compatibility.
"""

from .files import (is_sensitive, match_path, safe_path, select_files,
                    sha256_file, validate_relative, write_atomic)
from .hooks import cursor_hook_error, lint_installed
from .policy import BEGIN, END, render_agents
from .runner import run as run_gate

__all__ = [
    "BEGIN", "END",
    "cursor_hook_error", "is_sensitive", "lint_installed", "match_path",
    "render_agents", "run_gate", "safe_path", "select_files", "sha256_file",
    "validate_relative", "write_atomic",
]
