"""Typed public interface to Wright Arctic's compiled Rust engine."""

from ._core import NativeDocument as NativeDocument
from ._core import hello as hello
from ._core import native_create as native_create
from ._core import native_merge as native_merge
from ._core import native_restore as native_restore

__all__ = ["NativeDocument", "hello", "native_create", "native_merge", "native_restore"]
