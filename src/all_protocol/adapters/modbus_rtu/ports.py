"""Process-wide serial ownership shared by clients and servers on different loops."""
from threading import Lock
from .model import normalize_port

_guard = Lock()
_owners: dict[str, object] = {}


def acquire(port, owner):
    key = normalize_port(port)
    with _guard:
        if key in _owners and _owners[key] is not owner:
            raise OSError(f"{key} 已被本程序的其他客户端或服务端占用")
        _owners[key] = owner


def release(port, owner):
    key = normalize_port(port)
    with _guard:
        if _owners.get(key) is owner:
            del _owners[key]
