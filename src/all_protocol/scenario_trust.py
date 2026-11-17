"""Local permission to run checked Python manifests; never exported with projects."""
import json
import os
from pathlib import Path


def _path():
    return Path(os.environ.get("ALL_PROTOCOL_SCENARIO_TRUST_DIR", os.environ.get("LOCALAPPDATA", str(Path.home())))) / "AllProtocol" / "scenario-trust.json"


def _read():
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) and all(isinstance(s, str) for s in data) else set()
    except (OSError, ValueError):
        return set()


def is_trusted(extension):
    return extension.mode != "python" or extension.checked_digest in _read()


def trust(extension):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    records = _read() | {extension.checked_digest}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(sorted(records)), encoding="utf-8")
    temporary.replace(path)
