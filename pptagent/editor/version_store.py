"""Persistent storage for version history.

Stores version metadata and snapshot data as JSON files
so versions survive process restarts.
"""

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from pptagent.editor.snapshot import PresentationSnapshot
from pptagent.editor.version import VersionNode, VersionManager


def _version_to_dict(node: VersionNode) -> dict[str, Any]:
    """Serialize a VersionNode's metadata to a plain dict."""
    return {
        "version_id": node.version_id,
        "parent_id": node.parent_id,
        "message": node.message,
        "tags": node.tags,
        "created_at": node.created_at.isoformat(),
        "checksum": node.snapshot.checksum,
        "num_slides": len(node.snapshot.slides),
    }


class VersionStore:
    """Disk-backed storage for VersionManager versions.

    Directory layout::

        versions/
        ├── index.json            # list of all version metadata
        └── snapshots/
            ├── <version_id>.json   # full snapshot data
            └── ...
    """

    def __init__(self, storage_dir: Path | str):
        if isinstance(storage_dir, str):
            storage_dir = Path(storage_dir)
        self._dir = storage_dir
        self._snapshots_dir = self._dir / "snapshots"
        self._index_file = self._dir / "index.json"
        self._snapshots_dir.mkdir(parents=True, exist_ok=True)

    # ── save ──────────────────────────────────────────────────

    def save(self, node: VersionNode) -> None:
        """Persist a version node to disk (metadata + JSON snapshot data)."""
        # 1. Save snapshot as JSON
        snapshot_path = self._snapshots_dir / f"{node.version_id}.json"
        with open(snapshot_path, "w", encoding="utf-8") as f:
            json.dump(node.snapshot.to_dict(), f, ensure_ascii=False, indent=2)

        # 2. Update index
        index = self._read_index()
        index = [e for e in index if e["version_id"] != node.version_id]
        index.append(_version_to_dict(node))
        self._write_index(index)

    # ── load ──────────────────────────────────────────────────

    def get(self, version_id: str) -> VersionNode | None:
        """Load a single version from disk. Returns None if not found."""
        index = self._read_index()
        entry = next((e for e in index if e["version_id"] == version_id), None)
        if entry is None:
            return None

        snapshot_path = self._snapshots_dir / f"{version_id}.json"
        if not snapshot_path.exists():
            return None

        with open(snapshot_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        snapshot = PresentationSnapshot.from_dict(data)

        return VersionNode(
            version_id=entry["version_id"],
            parent_id=entry["parent_id"],
            snapshot=snapshot,
            message=entry["message"],
            tags=entry["tags"],
            created_at=datetime.fromisoformat(entry["created_at"]),
        )

    # ── list ──────────────────────────────────────────────────

    def list_versions(self) -> list[dict[str, Any]]:
        """Return all version metadata sorted by creation time (newest first).

        Does NOT load snapshot data — fast even with many versions.
        """
        index = self._read_index()
        index.sort(key=lambda e: e["created_at"], reverse=True)
        return index

    # ── delete ────────────────────────────────────────────────

    def delete(self, version_id: str) -> bool:
        """Delete a version from disk. Returns True if deleted."""
        snapshot_path = self._snapshots_dir / f"{version_id}.json"
        deleted = False
        if snapshot_path.exists():
            snapshot_path.unlink()
            deleted = True

        index = self._read_index()
        new_index = [e for e in index if e["version_id"] != version_id]
        if len(new_index) != len(index):
            deleted = True
        self._write_index(new_index)
        return deleted

    # ── helpers ───────────────────────────────────────────────

    def _read_index(self) -> list[dict[str, Any]]:
        if not self._index_file.exists():
            return []
        with open(self._index_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_index(self, index: list[dict[str, Any]]) -> None:
        with open(self._index_file, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)

    # ── restore from disk ─────────────────────────────────────

    def load_manager(self) -> "VersionManager":
        """Reconstruct a VersionManager with all stored versions."""
        vm = VersionManager(storage_dir=self._dir)
        index = self._read_index()

        if not index:
            return vm

        # Sort oldest first to build the chain
        index.sort(key=lambda e: e["created_at"])

        for entry in index:
            node = self.get(entry["version_id"])
            if node is not None:
                vm._versions[entry["version_id"]] = node

        # Set current to the latest
        if index:
            vm._current_id = index[-1]["version_id"]

        return vm

    def clear(self) -> None:
        """Remove all stored versions."""
        for json_file in self._snapshots_dir.glob("*.json"):
            json_file.unlink()
        if self._index_file.exists():
            self._index_file.unlink()
