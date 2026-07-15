"""The in-memory backend must pass the storage conformance suite."""

from parsica_spirit.backends.memory import InMemoryBackend
from parsica_spirit.conformance import run_storage_conformance


def test_inmemory_backend_is_conformant():
    run_storage_conformance(InMemoryBackend)
