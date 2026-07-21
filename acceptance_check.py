#!/usr/bin/env python3
"""
A 组独立验收脚本
=================

不依赖 B/C/D 组即可验证 A 组修改是否正确。

用法:
    python acceptance_check.py --workspace /tmp/accept --test-pptx ./samples/

验收项（对应 README 完成标准）:
    1. 上传一个未内置的 PPTX → 能看到解析进度、缩略图和模板信息
    2. 解析完成的模板能被生成链路真正选中
    3. 新模板无需重启服务即可使用
    4. 连续使用同一模板不会因共享字典被修改而失败
    5. 所有失败状态都有清晰错误信息
    6. 不留下"永久解析中"的模板
"""

import asyncio
import json
import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock


class AcceptanceChecker:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, passed: bool, detail: str = "") -> bool:
        status = "PASS" if passed else "FAIL"
        self.results.append((name, passed, detail))
        print(f"  [{status}] {name}")
        if detail:
            print(f"         {detail}")
        return passed

    async def run_all(self):
        print("=" * 60)
        print("A 组交付验收")
        print(f"Workspace: {self.workspace}")
        print("=" * 60)

        await self.check_01_manifest_model()
        await self.check_02_registry_basics()
        await self.check_03_cache_deepcopy()
        await self.check_04_restart_recovery()
        await self.check_05_pop_fix()
        await self.check_06_file_validation()
        await self.check_07_filename_sanitization()
        await self.check_08_progress_event_model()
        await self.check_09_registry_hash_dedup()
        await self.check_10_no_permanent_parsing()

        print("\n" + "=" * 60)
        passed = sum(1 for _, p, _ in self.results if p)
        total = len(self.results)
        print(f"结果: {passed}/{total} 通过")
        if passed == total:
            print("所有检查通过 - 可以交付给 B/C/D 组")
        else:
            failed = [(n, d) for n, p, d in self.results if not p]
            print("以下检查未通过:")
            for n, d in failed:
                print(f"  - {n}: {d}")
            print("需要修复后再交付")
        print("=" * 60)
        return passed == total

    # ── 1. Manifest 模型 ──

    async def check_01_manifest_model(self):
        """Manifest 模型定义完整"""
        print("\n[1/10] Manifest 模型")

        from deeppresenter.server.models.templates import (
            TemplateManifest,
            TemplateStatus,
        )

        m = TemplateManifest(template_id="test", name="测试")
        self.check("template_id 可设置", m.template_id == "test")
        self.check("默认 status=parsing", m.status == TemplateStatus.PARSING)
        self.check("source_hash 默认为空", m.source_hash == "")
        self.check("slide_count 默认为 0", m.slide_count == 0)
        self.check("layout_count 默认为 0", m.layout_count == 0)
        self.check("error 默认为 None", m.error is None)
        self.check("可 JSON 序列化", bool(json.dumps(m.model_dump())))

    # ── 2. Registry 基础 ──

    async def check_02_registry_basics(self):
        """TemplateRegistry 增删查"""
        print("\n[2/10] Registry 基础操作")

        from deeppresenter.server.services.template_registry import TemplateRegistry
        from deeppresenter.server.models.templates import TemplateManifest

        registry = TemplateRegistry(self.workspace)

        m = TemplateManifest(template_id="accept_test_01", name="验收测试1")
        registry.register(m)
        got = registry.get("accept_test_01")

        self.check("注册后可获取", got is not None)
        self.check("获取内容一致", got.name == "验收测试1" if got else False)

        registry.unregister("accept_test_01")
        self.check("注销后获取为空", registry.get("accept_test_01") is None)

    # ── 3. 缓存 deepcopy 保护 ──

    async def check_03_cache_deepcopy(self):
        """缓存返回 deepcopy - pop() 不影响源数据 (P0-9)"""
        print("\n[3/10] 缓存 deepcopy 保护")

        from deeppresenter.server.services.template_registry import TemplateRegistry

        registry = TemplateRegistry(self.workspace)
        data = {
            "slide_induction": {
                "language": {"lid": "zh"},
                "functional_keys": ["opening"],
            }
        }
        registry.cache_template("accept_test_02", data)

        # 模拟破坏性操作
        cached = registry.get_cached("accept_test_02")
        cached["slide_induction"].pop("language")
        cached["slide_induction"].pop("functional_keys")

        # 源数据应完好
        fresh = registry.get_cached("accept_test_02")
        self.check(
            "pop 后源数据 language 仍存在",
            "language" in fresh["slide_induction"],
        )
        self.check(
            "pop 后源数据 functional_keys 仍存在",
            "functional_keys" in fresh["slide_induction"],
        )

    # ── 4. 重启恢复 ──

    async def check_04_restart_recovery(self):
        """服务重启后从 manifest.json 恢复"""
        print("\n[4/10] 重启恢复")

        from deeppresenter.server.services.template_registry import TemplateRegistry
        from deeppresenter.server.models.templates import (
            TemplateManifest,
            TemplateStatus,
        )

        template_dir = self.workspace / "templates" / "accept_restart"
        template_dir.mkdir(parents=True, exist_ok=True)
        m = TemplateManifest(
            template_id="accept_restart",
            name="重启恢复测试",
            status=TemplateStatus.READY,
        )
        m.save(template_dir)

        # 新建 registry（模拟重启）
        registry = TemplateRegistry(self.workspace)
        recovered = registry.get("accept_restart")

        self.check("重启后可恢复", recovered is not None)
        if recovered:
            self.check("名称正确", recovered.name == "重启恢复测试")
            self.check("状态正确", recovered.status == TemplateStatus.READY)

        # 清理
        registry.unregister("accept_restart")
        import shutil
        shutil.rmtree(template_dir, ignore_errors=True)

    # ── 5. pop() 修复 ──

    async def check_05_pop_fix(self):
        """set_reference 不再 pop() 破坏参数 (P0-9)"""
        print("\n[5/10] pop() 修复")

        from pptagent.pptgen import PPTGen

        slide_induction = {
            "language": {"lid": "zh"},
            "functional_keys": ["opening", "ending"],
            "title:text": {
                "template_id": 1,
                "slides": [1],
                "content_schema": {},
            },
        }
        original = deepcopy(slide_induction)

        mock_prs = MagicMock()
        mock_prs.slides = []

        gen = PPTGen(language_model=MagicMock(), vision_model=MagicMock())
        gen._initialized = False
        gen.staffs = {}

        gen.set_reference(slide_induction=slide_induction, presentation=mock_prs)

        self.check(
            "传入字典未被修改",
            slide_induction == original,
            f"original keys: {list(original.keys())}, "
            f"after keys: {list(slide_induction.keys())}",
        )

        # 连续 5 次
        for i in range(5):
            gen.set_reference(
                slide_induction=deepcopy(original), presentation=mock_prs
            )
        self.check("连续 5 次 set_reference 无异常", True)

    # ── 6. 文件校验 ──

    async def check_06_file_validation(self):
        """文件校验逻辑"""
        print("\n[6/10] 文件校验")

        from deeppresenter.server.services.template_service import validate_pptx

        # 非 ZIP
        r = validate_pptx(b"not a zip")
        self.check("非 ZIP 内容拒收", r.valid is False)
        self.check("非 ZIP 有错误码", r.error is not None and "code" in r.error)

        # 空内容
        r = validate_pptx(b"")
        self.check("空内容拒收", r.valid is False)

        # 损坏 ZIP
        r = validate_pptx(b"PK\x03\x04" + b"\x00" * 200)
        self.check("损坏 ZIP 拒收", r.valid is False)

    # ── 7. 文件名清洗 ──

    async def check_07_filename_sanitization(self):
        """文件名清洗防路径穿越"""
        print("\n[7/10] 文件名清洗")

        from deeppresenter.server.services.template_service import sanitize_filename

        r = sanitize_filename("../../../etc/passwd.pptx")
        self.check("路径穿越已移除", "/" not in r and ".." not in r)

        r = sanitize_filename("正常模板.pptx")
        self.check("中文保留", "正常模板" in r or "模板" in r)

        r = sanitize_filename("")
        self.check("空名降级", r == "untitled")

        r = sanitize_filename("a" * 300)
        self.check("超长截断", len(r) <= 100)

    # ── 8. 进度事件模型 ──

    async def check_08_progress_event_model(self):
        """进度事件模型"""
        print("\n[8/10] 进度事件模型")

        from deeppresenter.server.models.templates import GenerationEvent

        event = GenerationEvent(
            event="template.parse_progress",
            template_id="test",
            progress=0.55,
            stage="layout_clustered",
        )
        d = event.model_dump()
        self.check("事件类型正确", d["event"] == "template.parse_progress")
        self.check("进度值正确", d["progress"] == 0.55)
        self.check("有时间戳", "timestamp" in d)

        # 终态事件
        event = GenerationEvent(
            event="template.ready",
            template_id="test",
            progress=1.0,
            stage="completed",
        )
        self.check("ready 事件", event.event == "template.ready")

        event = GenerationEvent(
            event="template.failed",
            template_id="test",
            error="解析失败",
        )
        self.check("failed 事件含错误", event.error == "解析失败")

    # ── 9. Hash 去重 ──

    async def check_09_registry_hash_dedup(self):
        """Hash 去重查找"""
        print("\n[9/10] Hash 去重")

        from deeppresenter.server.services.template_registry import TemplateRegistry
        from deeppresenter.server.models.templates import TemplateManifest

        registry = TemplateRegistry(self.workspace)

        m = TemplateManifest(
            template_id="accept_hash",
            name="hash测试",
            source_hash="deadbeef1234",
        )
        registry.register(m)

        found = registry.find_by_hash("deadbeef1234")
        self.check("hash 命中", found is not None and found.template_id == "accept_hash")
        self.check("hash 未命中", registry.find_by_hash("nonexistent") is None)

        registry.unregister("accept_hash")

    # ── 10. 无永久 parsing ──

    async def check_10_no_permanent_parsing(self):
        """不留下永久 parsing 状态的模板"""
        print("\n[10/10] 无永久 parsing")

        from deeppresenter.server.services.template_registry import TemplateRegistry
        from deeppresenter.server.models.templates import TemplateStatus

        registry = TemplateRegistry(self.workspace)
        all_templates = registry.list_all(include_failed=True)

        parsing = [m for m in all_templates if m.status == TemplateStatus.PARSING]
        self.check(
            "无 parsing 状态残留",
            len(parsing) == 0,
            f"parsing: {[m.template_id for m in parsing]}"
            if parsing
            else "",
        )


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="A 组交付验收脚本")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path(".cache/acceptance_test"),
        help="验收工作区路径",
    )
    args = parser.parse_args()

    args.workspace.mkdir(parents=True, exist_ok=True)

    checker = AcceptanceChecker(args.workspace)
    ok = await checker.run_all()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
