"""Greeting behavior backed by the native engine."""

from engine import hello


def create_greeting(name: str) -> str:
    """Call the typed public engine interface."""
    return hello(name)
