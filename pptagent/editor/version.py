"""Version management for PPT presentations.

Supports git-like operations: commit, checkout, log, diff, branch, and merge.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pptagent.presentation.presentation import Presentation
from pptagent.editor.snapshot import PresentationSnapshot


@dataclass
class VersionNode:
    """A single version in the version tree."""

    version_id: str
    parent_id: str | None  # None for the root (first) version
    snapshot: PresentationSnapshot
    message: str
    tags: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)


class VersionManager:
    """Manage presentation versions with branching support.

    Typical usage::

        vm = VersionManager()
        vm.commit(prs, "Initial generation")
        # ... user edits some slides ...
        vm.commit(prs, "Fixed typos on slide 3")
        vm.log()                     # list all versions
        vm.checkout(v1, prs)         # go back to v1
        vm.diff("v1", "v2")          # see what changed
    """

    def __init__(self, storage_dir: Path | None = None):
        self._versions: dict[str, VersionNode] = {}
        self._branches: dict[str, str] = defaultdict(str)  # name -> version_id
        self._current_id: str | None = None
        self._branch: str = "main"
        self._storage_dir = storage_dir

    # ── commit ────────────────────────────────────────────────

    def commit(
        self,
        prs: Presentation,
        message: str,
        tags: list[str] | None = None,
    ) -> VersionNode:
        """Save the current presentation state as a new version."""
        version_id = uuid.uuid4().hex[:8]
        snapshot = PresentationSnapshot.capture(prs, version_tag=version_id)
        node = VersionNode(
            version_id=version_id,
            parent_id=self._current_id,
            snapshot=snapshot,
            message=message,
            tags=tags or [],
        )
        self._versions[version_id] = node
        self._branches[self._branch] = version_id
        self._current_id = version_id
        return node

    # ── checkout ──────────────────────────────────────────────

    def checkout(self, version_id: str, prs: Presentation) -> None:
        """Restore the presentation to a specific version."""
        if version_id not in self._versions:
            raise KeyError(f"Version '{version_id}' not found")
        node = self._versions[version_id]
        node.snapshot.restore(prs)
        self._current_id = version_id

    # ── log ───────────────────────────────────────────────────

    def log(self) -> list[dict[str, Any]]:
        """Return the version chain from root to current, newest first."""
        chain = []
        current_id = self._current_id
        while current_id is not None:
            node = self._versions[current_id]
            chain.append({
                "version_id": node.version_id,
                "message": node.message,
                "tags": node.tags,
                "created_at": node.created_at.isoformat(),
                "parent_id": node.parent_id,
            })
            current_id = node.parent_id
        return chain

    # ── diff ──────────────────────────────────────────────────

    def diff(self, v1: str, v2: str) -> dict[str, Any]:
        """Compare two versions and return changed slide indices."""
        n1 = self._versions.get(v1)
        n2 = self._versions.get(v2)
        if n1 is None or n2 is None:
            raise KeyError(f"Version not found: {v1 if n1 is None else v2}")
        changed = n1.snapshot.changed_slides(n2.snapshot)
        return {
            "v1": v1,
            "v2": v2,
            "same_content": n1.snapshot.is_same_content(n2.snapshot),
            "changed_slides": changed,
        }

    # ── branch ────────────────────────────────────────────────

    def branch(self, name: str) -> str:
        """Create a new branch from the current version."""
        if self._current_id is None:
            raise RuntimeError("No commits yet; cannot branch.")
        self._branches[name] = self._current_id
        self._branch = name
        return self._current_id

    def switch_branch(self, name: str) -> str:
        """Switch to a different branch."""
        if name not in self._branches:
            raise KeyError(f"Branch '{name}' not found")
        self._branch = name
        self._current_id = self._branches[name]
        return self._current_id

    # ── properties ────────────────────────────────────────────

    @property
    def current_version_id(self) -> str | None:
        return self._current_id

    @property
    def current_branch(self) -> str:
        return self._branch

    def __len__(self) -> int:
        return len(self._versions)

    def __contains__(self, version_id: str) -> bool:
        return version_id in self._versions
