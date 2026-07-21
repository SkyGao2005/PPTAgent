"""模板注册中心 —— 线程安全，支持动态加载/卸载。

职责：
- 管理所有模板的 manifest 索引
- 提供模板缓存（presentation + slide_induction）
- 支持 hash 去重查找
- 服务重启后从 manifest 文件恢复
"""

import json
import logging
import threading
from copy import deepcopy
from pathlib import Path
from typing import Optional

from deeppresenter.server.models.templates import TemplateManifest, TemplateStatus

# Fallback logger: prefer deeppresenter's logger, use stdlib logging otherwise.
# This makes the module importable even when the full deeppresenter stack
# isn't installed (useful for unit testing).
try:
    from deeppresenter.utils.log import get_logger as _dp_get_logger
    logger = _dp_get_logger()
except (ImportError, TypeError):
    logger = logging.getLogger(__name__)


class TemplateRegistry:
    """
    模板注册中心

    特性:
    - 线程安全（所有公共方法持锁）
    - hash → template_id 反向索引（去重）
    - 缓存返回 deepcopy（防止 pptgen.pop() 破坏源数据）
    - 服务重启后自动从 manifest.json 恢复
    """

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.templates_dir = self.workspace / "templates"
        self.templates_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

        # manifest 索引: template_id → TemplateManifest
        self._manifests: dict[str, TemplateManifest] = {}
        # 解析缓存: template_id → {"presentation": ..., "slide_induction": ..., "config": ...}
        self._cache: dict[str, dict] = {}
        # hash → template_id 反向索引
        self._hash_index: dict[str, str] = {}

        self._load_all_from_disk()

    # ── 注册 / 注销 ──────────────────────────────────────────

    def register(self, manifest: TemplateManifest) -> None:
        """注册模板并建立 hash 索引"""
        with self._lock:
            self._manifests[manifest.template_id] = manifest
            if manifest.source_hash:
                self._hash_index[manifest.source_hash] = manifest.template_id
            logger.debug(
                f"Registered template {manifest.template_id} "
                f"(status={manifest.status.value}, hash={manifest.source_hash[:12]}...)"
            )

    def unregister(self, template_id: str) -> None:
        """注销模板，清理索引和缓存"""
        with self._lock:
            self._manifests.pop(template_id, None)
            self._cache.pop(template_id, None)
            # 清理 hash 索引
            for h, tid in list(self._hash_index.items()):
                if tid == template_id:
                    del self._hash_index[h]
            logger.debug(f"Unregistered template {template_id}")

    # ── 查询 ──────────────────────────────────────────────────

    def get(self, template_id: str) -> Optional[TemplateManifest]:
        """获取模板 manifest"""
        with self._lock:
            return self._manifests.get(template_id)

    def list_all(self, include_failed: bool = False) -> list[TemplateManifest]:
        """列出所有模板（默认过滤已失败的）"""
        with self._lock:
            result = list(self._manifests.values())
            if not include_failed:
                result = [m for m in result if m.status != TemplateStatus.FAILED]
            return result

    def find_by_hash(self, file_hash: str) -> Optional[TemplateManifest]:
        """通过文件 hash 查找已有模板（去重）"""
        with self._lock:
            tid = self._hash_index.get(file_hash)
            if tid:
                return self._manifests.get(tid)
            return None

    # ── 缓存管理 ──────────────────────────────────────────────

    def cache_template(self, template_id: str, data: dict) -> None:
        """缓存已解析的模板数据（presentation + slide_induction 等）"""
        with self._lock:
            self._cache[template_id] = data
            logger.debug(f"Cached template {template_id}")

    def get_cached(self, template_id: str) -> Optional[dict]:
        """
        获取缓存的模板数据 —— 返回 deepcopy 防止破坏性修改

        pptgen.set_reference() 内部曾经对 slide_induction 执行 pop()，
        虽然已修复，但此处返回 deepcopy 作为双保险。
        """
        with self._lock:
            data = self._cache.get(template_id)
            return deepcopy(data) if data else None

    def invalidate_cache(self, template_id: str) -> None:
        """使缓存失效"""
        with self._lock:
            self._cache.pop(template_id, None)
            logger.debug(f"Invalidated cache for template {template_id}")

    def update_manifest(self, manifest: TemplateManifest) -> None:
        """更新 manifest（解析完成后调用）"""
        with self._lock:
            self._manifests[manifest.template_id] = manifest
            logger.debug(
                f"Updated manifest for {manifest.template_id}: status={manifest.status.value}"
            )

    # ── 持久化恢复 ────────────────────────────────────────────

    def _load_all_from_disk(self) -> None:
        """服务重启时从 manifest.json 恢复所有模板"""
        if not self.templates_dir.exists():
            logger.info(f"Templates directory not found: {self.templates_dir}")
            return

        count = 0
        for template_dir in self.templates_dir.iterdir():
            if not template_dir.is_dir():
                continue
            manifest_path = template_dir / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = TemplateManifest(**json.loads(manifest_path.read_text(encoding="utf-8")))
                self.register(manifest)
                count += 1
            except Exception as e:
                logger.warning(f"Failed to load manifest from {manifest_path}: {e}")

        logger.info(f"TemplateRegistry: loaded {count} templates from disk, "
                     f"workspace={self.workspace}")
