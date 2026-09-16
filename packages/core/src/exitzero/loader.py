"""Load only policy-selected plugins, by entry point or explicit module name."""
import importlib
from importlib.metadata import entry_points

from .api import API_VERSION, Registry


def discover(names: list[str]) -> Registry:
    registry = Registry()
    available = entry_points(group="exitzero.plugins")
    for name in names:
        matches = [entry for entry in available if entry.name == name]
        if len(matches) > 1:
            raise ValueError("Ambiguous plugin entry point")
        plugin = matches[0].load() if matches else importlib.import_module(name)
        if getattr(plugin, "API_VERSION", None) != API_VERSION:
            raise ValueError("Unsupported plugin API version")
        plugin.register(registry)
    return registry
