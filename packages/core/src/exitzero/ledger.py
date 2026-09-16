"""Append-only per-run receipts; no source, environment or command output."""
import json
from pathlib import Path
import tempfile

from .files import safe_path


def persist(root: Path, receipt: dict) -> str:
    directory = safe_path(root, ".exitzero/runs")
    directory.mkdir(parents=True, exist_ok=True)
    relative = f".exitzero/runs/{receipt['run_id']}.json"
    destination = safe_path(root, relative)
    receipt["receipt"] = relative
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=directory, prefix=".pending-", encoding="utf-8", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(receipt, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return relative
