"""AIClient registry + factory.

Agent code requests clients by name::

    from pilot.agent.ai_client import get_client
    client = get_client("bedrock")         # explicit
    client = get_client()                  # default from config

The registry is populated by `register_client(name, factory)` calls,
which adapter modules make on import. Adapters are *not* imported
eagerly so missing provider SDKs (boto3, groq SDK, etc.) don't break
the whole package — each adapter is loaded on first request.
"""

from __future__ import annotations

import importlib
import os
from typing import Callable

from pilot.agent.ai_client.base import AIClient

# name -> factory callable. Factories take no arguments and return an
# AIClient instance, configured from environment / config file.
_FACTORIES: dict[str, Callable[[], AIClient]] = {}

# Lazy import paths for built-in adapters. Keys are adapter names;
# values are dotted module paths that, when imported, register
# themselves via register_client.
_BUILTIN_ADAPTERS: dict[str, str] = {
    "bedrock": "pilot.agent.ai_client.adapters.bedrock",
    "groq": "pilot.agent.ai_client.adapters.groq",
    "openai": "pilot.agent.ai_client.adapters.openai",
    "mock": "pilot.agent.ai_client.adapters.mock",
}

_INSTANCES: dict[str, AIClient] = {}


def register_client(name: str, factory: Callable[[], AIClient]) -> None:
    """Register a factory that builds an AIClient for ``name``.

    Adapter modules call this at import time. Calling with an existing
    name replaces the factory (useful for tests / swap-in of org-specific
    adapters).
    """
    _FACTORIES[name] = factory
    # Drop any cached instance so the next get_client() rebuilds.
    _INSTANCES.pop(name, None)


def _ensure_builtin_loaded(name: str) -> None:
    """Import a built-in adapter module if we know about it.

    Does nothing if the name isn't a built-in (caller is expected to
    have registered it manually) or if the import fails — the caller
    will get a clean KeyError from the outer get_client.
    """
    module_path = _BUILTIN_ADAPTERS.get(name)
    if module_path is None:
        return
    try:
        importlib.import_module(module_path)
    except ImportError:
        # Provider SDK not installed; the subsequent KeyError in
        # get_client will make the diagnostic clear.
        pass


def get_client(name: str | None = None) -> AIClient:
    """Return an AIClient for ``name``.

    If ``name`` is None, uses the value of the ``CURATIONPILOT_AI_CLIENT``
    environment variable, defaulting to "openai".

    Clients are cached per name; the same call returns the same
    instance. Call :func:`reset_clients` in tests.
    """
    if name is None:
        name = os.environ.get("CURATIONPILOT_AI_CLIENT", "openai")

    if name in _INSTANCES:
        return _INSTANCES[name]

    if name not in _FACTORIES:
        _ensure_builtin_loaded(name)

    if name not in _FACTORIES:
        available = sorted(set(_FACTORIES) | set(_BUILTIN_ADAPTERS))
        raise KeyError(
            f"No AIClient registered for '{name}'. Available: {available}. "
            "Built-in adapters may require their provider SDK; confirm "
            "the adapter module imported cleanly (check for missing "
            "optional dependencies like boto3, groq, openai)."
        )

    client = _FACTORIES[name]()
    _INSTANCES[name] = client
    return client


def reset_clients() -> None:
    """Drop all cached client instances. Intended for tests."""
    _INSTANCES.clear()


def list_registered() -> list[str]:
    """Return the names of all registered adapters."""
    return sorted(_FACTORIES.keys())


def list_available() -> list[str]:
    """Return names of adapters that can be used.

    Includes both already-registered adapters and built-in adapters
    whose provider SDKs appear importable.
    """
    names = set(_FACTORIES.keys())
    for name, module_path in _BUILTIN_ADAPTERS.items():
        try:
            importlib.import_module(module_path)
            names.add(name)
        except ImportError:
            continue
    return sorted(names)
