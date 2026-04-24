"""Concrete AIClient adapters.

Each adapter module must:
  1. Define a class implementing the AIClient Protocol.
  2. Call ``register_client(<name>, <factory>)`` at module import time,
     where ``<factory>`` is a zero-arg callable that returns a new
     client instance configured from env + config.

Adapters are loaded lazily by the registry, so missing provider SDKs
only break the adapter that needs them, not the whole agent package.
"""
