"""
Migration System - Handle schema evolution and format upgrades.

Supports:
- v0.2.x -> v0.4.x migration (single file -> sharded)
- Schema validation and repair
- Backup creation before migration
- Rollback capabilities
"""

import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .entry import MemoryEntry
from .sharding import ShardManager

logger = logging.getLogger(__name__)


class MigrationError(Exception):
    """Raised when migration fails."""
    pass


class Migration:
    """Represents a single migration step."""
    
    def __init__(self, from_version: str, to_version: str, name: str):
        self.from_version = from_version
        self.to_version = to_version
        self.name = name
    
    def can_apply(self, current_version: str) -> bool:
        """Check if this migration can be applied."""
        return current_version == self.from_version
    
    def apply(self, workspace: str) -> bool:
        """Apply the migration. Return True if successful."""
        raise NotImplementedError
    
    def rollback(self, workspace: str) -> bool:
        """Rollback the migration. Return True if successful."""
        raise NotImplementedError


class V2ToV4Migration(Migration):
    """Migrate from v0.2.x single-file format to v0.4.x sharded format."""
    
    def __init__(self):
        super().__init__("0.2.x", "0.4.0", "Single-file to sharded migration")
    
    def can_apply(self, current_version: str) -> bool:
        """Check if this migration can be applied."""
        return current_version.startswith("0.2") or current_version.startswith("0.3")
    
    def apply(self, workspace: str) -> bool:
        """Migrate from single-file to sharded format."""
        metadata_path = os.path.join(workspace, "memory_metadata.json")
        
        # Check if old format exists
        if not os.path.exists(metadata_path):
            # No old data to migrate
            return True
        
        # Create backup
        backup_path = os.path.join(workspace, f"memory_metadata_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        shutil.copy2(metadata_path, backup_path)
        
        try:
            # Load old format
            with open(metadata_path) as f:
                data = json.load(f)
            
            memories = [MemoryEntry.from_dict(m) for m in data.get("memories", [])]
            
            if not memories:
                return True  # Nothing to migrate
            
            # Initialize shard manager
            shard_manager = ShardManager(workspace)
            
            # Group memories by shard
            shard_groups = shard_manager.shard_memories(memories)
            
            # Save each shard
            for shard_key, shard_memories in shard_groups.items():
                shard_manager.save_shard(shard_key, shard_memories)
                shard_manager.index.add_shard(shard_key, shard_memories)
            
            # Save the index
            shard_manager.index.save_index()
            
            # Create a migration marker
            migration_info = {
                "migrated_at": datetime.now().isoformat(),
                "from_version": data.get("version", "unknown"),
                "to_version": "0.4.0",
                "backup_file": backup_path,
                "migrated_memories": len(memories),
                "created_shards": len(shard_groups)
            }
            
            migration_path = os.path.join(workspace, "migration_v2_to_v4.json")
            from .utils import atomic_write_json
            atomic_write_json(migration_path, migration_info)
            
            # Archive the old metadata file (don't delete in case rollback is needed)
            old_metadata_archive = os.path.join(workspace, "memory_metadata_v2_archive.json")
            shutil.move(metadata_path, old_metadata_archive)
            
            return True
            
        except Exception as e:
            # Restore backup if migration failed
            if os.path.exists(backup_path):
                shutil.copy2(backup_path, metadata_path)
            raise MigrationError(f"Migration failed: {e}")
    
    def rollback(self, workspace: str) -> bool:
        """Rollback sharded format to single-file format."""
        migration_path = os.path.join(workspace, "migration_v2_to_v4.json")
        
        if not os.path.exists(migration_path):
            return False  # No migration to rollback
        
        try:
            # Load migration info
            with open(migration_path) as f:
                migration_info = json.load(f)
            
            backup_path = migration_info.get("backup_file")
            if not backup_path or not os.path.exists(backup_path):
                return False  # No backup to restore from
            
            # Restore the backup
            metadata_path = os.path.join(workspace, "memory_metadata.json")
            shutil.copy2(backup_path, metadata_path)
            
            # Remove sharded files
            shards_dir = os.path.join(workspace, "shards")
            if os.path.exists(shards_dir):
                shutil.rmtree(shards_dir)
            
            # Remove shard index
            index_path = os.path.join(workspace, "memory_index.json")
            if os.path.exists(index_path):
                os.remove(index_path)
            
            # Remove migration marker
            os.remove(migration_path)
            
            return True
            
        except Exception as e:
            raise MigrationError(f"Rollback failed: {e}")


class MigrationManager:
    """Manages schema migrations and version compatibility."""
    
    def __init__(self, workspace: str):
        self.workspace = workspace
        self.migrations = [
            V2ToV4Migration()
        ]
    
    def detect_version(self) -> str:
        """Detect the current data format version."""
        # Check for v0.4.x (sharded format)
        index_path = os.path.join(self.workspace, "memory_index.json")
        if os.path.exists(index_path):
            try:
                with open(index_path) as f:
                    data = json.load(f)
                return data.get("version", "0.4.0")
            except (OSError, ValueError) as exc:
                # corrupt/unreadable index: don't silently claim "fresh install" - log it
                logger.warning("could not read %s for version detection: %s", index_path, exc)
        
        # Check for v0.2.x/v0.3.x (single-file format)
        metadata_path = os.path.join(self.workspace, "memory_metadata.json")
        if os.path.exists(metadata_path):
            try:
                with open(metadata_path) as f:
                    data = json.load(f)
                return data.get("version", "0.2.0")
            except (OSError, ValueError) as exc:
                logger.warning("could not read %s for version detection: %s", metadata_path, exc)
        
        # No existing data
        return "none"
    
    def needs_migration(self, target_version: str = "0.4.0") -> bool:
        """Check if migration is needed to reach target version."""
        current_version = self.detect_version()
        
        if current_version == "none":
            return False  # Fresh install
        
        if current_version == target_version:
            return False  # Already at target version
        
        # Check if we have a migration path
        for migration in self.migrations:
            if migration.can_apply(current_version) and migration.to_version == target_version:
                return True
        
        return False
    
    def migrate(self, target_version: str = "0.4.0") -> Dict:
        """Perform migration to target version."""
        current_version = self.detect_version()
        
        if current_version == target_version:
            return {"status": "no_migration_needed", "version": current_version}
        
        if current_version == "none":
            return {"status": "fresh_install", "version": target_version}
        
        # Find applicable migration
        applicable_migration = None
        for migration in self.migrations:
            if migration.can_apply(current_version) and migration.to_version == target_version:
                applicable_migration = migration
                break
        
        if not applicable_migration:
            return {
                "status": "error", 
                "message": f"No migration path from {current_version} to {target_version}"
            }
        
        try:
            success = applicable_migration.apply(self.workspace)
            if success:
                return {
                    "status": "success",
                    "from_version": current_version,
                    "to_version": target_version,
                    "migration": applicable_migration.name
                }
            else:
                return {"status": "failed", "message": "Migration returned False"}
                
        except MigrationError as e:
            return {"status": "error", "message": str(e)}
    
    def rollback(self) -> Dict:
        """Rollback the most recent migration."""
        # Check for v2 to v4 migration
        migration_path = os.path.join(self.workspace, "migration_v2_to_v4.json")
        if os.path.exists(migration_path):
            migration = V2ToV4Migration()
            try:
                success = migration.rollback(self.workspace)
                if success:
                    return {"status": "success", "rollback": "v4 to v2"}
                else:
                    return {"status": "failed", "message": "Rollback returned False"}
            except MigrationError as e:
                return {"status": "error", "message": str(e)}
        
        return {"status": "no_migration_to_rollback"}
    
    def validate_schema(self) -> Dict:
        """Validate the current data schema."""
        current_version = self.detect_version()
        
        if current_version == "none":
            return {"status": "valid", "version": "none", "message": "No data to validate"}
        
        if current_version.startswith("0.4"):
            return self._validate_v4_schema()
        elif current_version.startswith("0.2") or current_version.startswith("0.3"):
            return self._validate_v2_schema()
        else:
            return {"status": "unknown_version", "version": current_version}
    
    def _validate_v4_schema(self) -> Dict:
        """Validate v0.4.x sharded format."""
        issues = []
        
        # Check if index exists
        index_path = os.path.join(self.workspace, "memory_index.json")
        if not os.path.exists(index_path):
            issues.append("Missing memory_index.json")
            return {"status": "invalid", "issues": issues}
        
        try:
            # Validate index format
            with open(index_path) as f:
                index_data = json.load(f)
            
            required_fields = ["version", "total_shards", "shards"]
            for field in required_fields:
                if field not in index_data:
                    issues.append(f"Missing required field in index: {field}")
            
            # Validate each shard file exists
            shards_dir = os.path.join(self.workspace, "shards")
            if not os.path.exists(shards_dir):
                issues.append("Missing shards directory")
            else:
                for shard_info in index_data.get("shards", []):
                    filename = shard_info.get("filename")
                    if filename:
                        shard_path = os.path.join(shards_dir, filename)
                        if not os.path.exists(shard_path):
                            issues.append(f"Missing shard file: {filename}")
            
        except json.JSONDecodeError:
            issues.append("Invalid JSON in memory_index.json")
        except Exception as e:
            issues.append(f"Error validating schema: {e}")
        
        if issues:
            return {"status": "invalid", "version": "0.4.x", "issues": issues}
        else:
            return {"status": "valid", "version": "0.4.x"}
    
    def _validate_v2_schema(self) -> Dict:
        """Validate v0.2.x single-file format."""
        issues = []
        metadata_path = os.path.join(self.workspace, "memory_metadata.json")
        
        if not os.path.exists(metadata_path):
            issues.append("Missing memory_metadata.json")
            return {"status": "invalid", "issues": issues}
        
        try:
            with open(metadata_path) as f:
                data = json.load(f)
            
            required_fields = ["version", "memories"]
            for field in required_fields:
                if field not in data:
                    issues.append(f"Missing required field: {field}")
            
            # Validate memory entries
            memories = data.get("memories", [])
            for i, memory_data in enumerate(memories):
                if "content" not in memory_data:
                    issues.append(f"Memory {i} missing content field")
                if "created" not in memory_data:
                    issues.append(f"Memory {i} missing created field")
                
                # Stop after 10 validation errors to avoid spam
                if len(issues) >= 10:
                    issues.append("... (more validation errors)")
                    break
        
        except json.JSONDecodeError:
            issues.append("Invalid JSON in memory_metadata.json")
        except Exception as e:
            issues.append(f"Error validating schema: {e}")
        
        if issues:
            return {"status": "invalid", "version": "0.2.x", "issues": issues}
        else:
            return {"status": "valid", "version": "0.2.x"}
    
    def get_migration_history(self) -> List[Dict]:
        """Get history of applied migrations."""
        history = []
        
        # Check for v2 to v4 migration
        migration_path = os.path.join(self.workspace, "migration_v2_to_v4.json")
        if os.path.exists(migration_path):
            try:
                with open(migration_path) as f:
                    migration_info = json.load(f)
                history.append(migration_info)
            except (OSError, ValueError) as exc:
                logger.warning("could not read migration history at %s: %s", migration_path, exc)
        
        return history