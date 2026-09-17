"""Small, versioned contracts shared by core and plugins."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Any

API_VERSION = 1
HOOK_SLOTS = frozenset({"PreToolUse", "PostToolUse", "pre-commit", "CI"})


@dataclass(frozen=True)
class Finding:
    rule: str
    message: str
    path: str | None = None
    line: int | None = None
    severity: str = "error"


@dataclass(frozen=True)
class CheckSpec:
    id: str
    kind: str
    paths: tuple[str, ...] = ()
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Context:
    root: Path
    policy: dict[str, Any]
    policy_path: Path


CheckHandler = Callable[[Context, CheckSpec], list[Finding]]
InputHandler = Callable[[Context, CheckSpec], list[str] | tuple[str, ...]]
LintHandler = Callable[[Context], list[Finding]]
HookHandler = Callable[[Context, str], list[Finding]]


@dataclass
class Registry:
    """Plugins call register(); duplicate names and unsupported slots fail closed."""

    checks: dict[str, CheckHandler] = field(default_factory=dict)
    linters: dict[str, LintHandler] = field(default_factory=dict)
    hooks: dict[str, list[HookHandler]] = field(default_factory=dict)
    commands: dict[str, Callable[[Context, list[str]], int]] = field(default_factory=dict)

    check_inputs: dict[str, InputHandler] = field(default_factory=dict)

    def add_check(self, name: str, handler: CheckHandler, *, inputs: InputHandler | None = None) -> None:
        if inputs is not None and not callable(inputs):
            raise ValueError("Invalid plugin input registration")
        self._add(self.checks, name, handler)
        if inputs is not None:
            self.check_inputs[name] = inputs

    def add_linter(self, name: str, handler: LintHandler) -> None:
        self._add(self.linters, name, handler)

    def add_hook(self, slot: str, handler: HookHandler) -> None:
        if slot not in HOOK_SLOTS:
            raise ValueError("Unsupported hook slot")
        self.hooks.setdefault(slot, []).append(handler)

    def add_command(self, name: str, handler: Callable[[Context, list[str]], int]) -> None:
        if name in {"init", "check", "lint-config", "hooks", "report", "plugin"}:
            raise ValueError("Reserved CLI command")
        self._add(self.commands, name, handler)

    @staticmethod
    def _add(target: dict, name: str, handler: Callable) -> None:
        if not isinstance(name, str) or not name or name in target or not callable(handler):
            raise ValueError("Invalid or duplicate plugin registration")
        target[name] = handler
