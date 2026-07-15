"""Coverage for MigrationManager: detect_version() must catch (OSError,
ValueError) and warn rather than swallow corrupt-file errors and silently
treat a corrupted store as fresh.

detect_version() now catches (OSError, ValueError) explicitly and logs a
warning instead of swallowing everything. These tests pin:

  1. A fresh/empty workspace detects as "none" and needs no migration.
  2. A corrupt/truncated memory_index.json does NOT raise, still detects as
     "none", and now emits a warning (asserted via caplog).
  3. validate_schema() / core.validate_data() do not crash on a corrupt store.

Deterministic: no clocks, randomness, sleeps, or network.
"""
import logging

from parsica_spirit.engine import MemorySystem, MigrationManager

# A truncated JSON object - valid-looking prefix, unterminated string.
_CORRUPT_INDEX = '{"version": "0.4.0", "total_shards": 3, "shards": [{"filename": "shard_0'


# -- 1. Fresh / empty workspace -----------------------------------------------

def test_fresh_workspace_detects_none(tmp_path):
    mm = MigrationManager(str(tmp_path))
    assert mm.detect_version() == "none"
    assert mm.needs_migration() is False
    # validate_schema is happy with an empty store.
    assert mm.validate_schema()["status"] == "valid"


# -- 2. Corrupt index is not fatal and warns ----------------------------------

def test_corrupt_index_detects_none_without_raising(tmp_path, caplog):
    (tmp_path / "memory_index.json").write_text(_CORRUPT_INDEX, encoding="utf-8")
    mm = MigrationManager(str(tmp_path))

    with caplog.at_level(logging.WARNING, logger="parsica_spirit.engine.migration"):
        version = mm.detect_version()  # must not raise

    assert version == "none", "a corrupt index must not be reported as a real version"
    assert mm.needs_migration() is False
    # The corruption is surfaced, not silently swallowed.
    assert any("could not read" in msg for msg in caplog.messages), caplog.messages


def test_corrupt_index_needs_migration_is_false(tmp_path):
    # Independent of caplog: a corrupt store never claims a migration path.
    (tmp_path / "memory_index.json").write_text(_CORRUPT_INDEX, encoding="utf-8")
    mm = MigrationManager(str(tmp_path))
    assert mm.needs_migration() is False


# -- 3. Validation does not crash on a corrupt store --------------------------

def test_validate_schema_survives_corrupt_index(tmp_path):
    (tmp_path / "memory_index.json").write_text(_CORRUPT_INDEX, encoding="utf-8")
    mm = MigrationManager(str(tmp_path))
    result = mm.validate_schema()  # must not raise
    assert isinstance(result, dict) and "status" in result


def test_core_validate_data_survives_corrupt_index(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    mem = MemorySystem(str(ws), agent_name="tester")
    # Drop a corrupt index into the live workspace, then validate through the
    # public core delegation (validate_data -> MigrationManager.validate_schema).
    (ws / "memory_index.json").write_text(_CORRUPT_INDEX, encoding="utf-8")
    result = mem.validate_data()  # must not raise
    assert isinstance(result, dict) and "status" in result
