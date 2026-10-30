"""Resolve middleware paths relative to this application's ASGI mount."""

from starlette.types import Scope


def route_path(scope: Scope) -> str:
    """Match router paths under mounts and reverse-proxy root paths alike.

    Some servers already supply an application-relative path. Strip root_path
    only when it is a complete path prefix, so /api never strips /api-other.
    """
    path: str = scope["path"]
    root_path = scope.get("root_path", "")
    if root_path and (path == root_path or path.startswith(root_path + "/")):
        return path[len(root_path) :]
    return path
