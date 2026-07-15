"""The engine backend must pass the SAME locked conformance suite as every other backend."""

import tempfile

from parsica_spirit.enrich import run_storage_conformance

from parsica_spirit.engine.seam import EngineBackend


def test_engine_backend_is_conformant():
    run_storage_conformance(lambda: EngineBackend(tempfile.mkdtemp(prefix="parsica_engine_")))
