#!/usr/bin/env python3
"""A 组修改验证脚本 —— 不依赖 pptagent 全部依赖，仅测试新建/修改模块。"""

import json
import os
import shutil
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}  --  {detail}")
    if detail and condition:
        print(f"         {detail}")


# =============================================================================
# Test 1: 模块导入
# =============================================================================
print("=== 1. 模块导入 ===")
try:
    from deeppresenter.server.models.templates import (
        TemplateManifest, TemplateStatus, TemplateErrorCode,
        TemplateSettings, GenerationEvent, TemplateErrorResponse,
        TemplateListResponse,
    )
    check("models/templates.py", True)
except Exception as e:
    check("models/templates.py", False, str(e))

try:
    from deeppresenter.server.services.template_registry import TemplateRegistry
    check("services/template_registry.py", True)
except Exception as e:
    check("services/template_registry.py", False, str(e))

try:
    from deeppresenter.server.services.template_service import (
        validate_pptx, sanitize_filename, PROGRESS_STAGES, PptxValidationResult,
    )
    check("services/template_service.py", True)
except Exception as e:
    check("services/template_service.py", False, str(e))

# =============================================================================
# Test 2: Manifest 模型
# =============================================================================
print("\n=== 2. Manifest 模型 ===")
m = TemplateManifest(template_id="t1", name="测试模板")
check("默认 status=parsing", m.status == TemplateStatus.PARSING)
check("默认 source_hash=''", m.source_hash == "")
check("默认 slide_count=0", m.slide_count == 0)
check("默认 error=None", m.error is None)

with tempfile.TemporaryDirectory() as td:
    m.save(Path(td))
    loaded = TemplateManifest.load(td)
    check("roundtrip template_id", loaded.template_id == m.template_id)
    check("roundtrip name", loaded.name == m.name)

# JSON 序列化
d = m.model_dump()
check("JSON 序列化", bool(json.dumps(d)))

# =============================================================================
# Test 3: 错误码
# =============================================================================
print("\n=== 3. 错误码 ===")
check("FILE_TOO_LARGE", TemplateErrorCode.FILE_TOO_LARGE == "FILE_TOO_LARGE")
check("ENCRYPTED_FILE", TemplateErrorCode.ENCRYPTED_FILE == "ENCRYPTED_FILE")
check("CORRUPTED_FILE", TemplateErrorCode.CORRUPTED_FILE == "CORRUPTED_FILE")
check("TEMPLATE_NOT_FOUND", TemplateErrorCode.TEMPLATE_NOT_FOUND == "TEMPLATE_NOT_FOUND")
check("INVALID_PAGE_COUNT", TemplateErrorCode.INVALID_PAGE_COUNT == "INVALID_PAGE_COUNT")
check("PARSE_FAILED", TemplateErrorCode.PARSE_FAILED == "PARSE_FAILED")

# =============================================================================
# Test 4: TemplateRegistry
# =============================================================================
print("\n=== 4. TemplateRegistry ===")
workspace = os.path.join(tempfile.gettempdir(), "pptagent_verify_registry")
if os.path.exists(workspace):
    shutil.rmtree(workspace, ignore_errors=True)

registry = TemplateRegistry(workspace)
m = TemplateManifest(template_id="test1", name="测试", source_hash="hash123")
registry.register(m)
check("register/get", registry.get("test1") is not None)
check("get nonexistent", registry.get("nonexistent") is None)

# hash 去重
check("hash 命中", registry.find_by_hash("hash123") is not None)
check("hash 未命中", registry.find_by_hash("notfound") is None)

# deepcopy 保护 (P0-9)
data = {"slide_induction": {"language": {"lid": "zh"}, "functional_keys": ["a"]}}
registry.cache_template("t1", data)
cached = registry.get_cached("t1")
cached["slide_induction"].pop("language")
cached["slide_induction"].pop("functional_keys")
fresh = registry.get_cached("t1")
check("cache deepcopy: language", "language" in fresh["slide_induction"])
check("cache deepcopy: functional_keys", "functional_keys" in fresh["slide_induction"])

# unregister
registry.unregister("test1")
check("unregister 后 get", registry.get("test1") is None)
check("unregister 后 hash", registry.find_by_hash("hash123") is None)

# list 过滤
registry.register(TemplateManifest(template_id="ok", name="ok", status=TemplateStatus.READY))
registry.register(TemplateManifest(template_id="fail", name="fail", status=TemplateStatus.FAILED))
all_list = registry.list_all(include_failed=False)
ids = {m.template_id for m in all_list}
check("list_all 过滤 failed", "ok" in ids and "fail" not in ids)
all_with = registry.list_all(include_failed=True)
check("list_all include_failed=True", len(all_with) >= 2)

# update_manifest
m2 = TemplateManifest(template_id="ok", name="updated", status=TemplateStatus.READY,
                      slide_count=100, layout_count=10)
registry.update_manifest(m2)
updated = registry.get("ok")
check("update_manifest name", updated.name == "updated")
check("update_manifest slide_count", updated.slide_count == 100)

# invalidate cache
registry.cache_template("ok", {"x": 1})
check("cache 存在", registry.get_cached("ok") is not None)
registry.invalidate_cache("ok")
check("invalidate 后缓存为空", registry.get_cached("ok") is None)

# hash 覆盖
registry.register(TemplateManifest(template_id="hash1", name="h1", source_hash="same"))
registry.register(TemplateManifest(template_id="hash2", name="h2", source_hash="same"))
found = registry.find_by_hash("same")
check("重复 hash 指向最新", found is not None and found.template_id == "hash2")

shutil.rmtree(workspace, ignore_errors=True)

# =============================================================================
# Test 5: 文件校验
# =============================================================================
print("\n=== 5. 文件校验 ===")
r = validate_pptx(b"not a zip file")
check("非 ZIP 拒收", r.valid is False)
check("非 ZIP 有错误码", r.error is not None and "code" in r.error)

r = validate_pptx(b"")
check("空内容拒收", r.valid is False)

r = validate_pptx(b"PK\x03\x04" + b"\x00" * 200)
check("损坏 ZIP 拒收", r.valid is False)

# =============================================================================
# Test 6: 文件名清洗
# =============================================================================
print("\n=== 6. 文件名清洗 ===")
r = sanitize_filename("../../../etc/passwd.pptx")
check("路径穿越移除: /", "/" not in r)
check("路径穿越移除: ..", ".." not in r)

r = sanitize_filename("正常模板.pptx")
check("中文保留且安全", ".." not in r and "/" not in r and len(r) > 0)

r = sanitize_filename("")
check("空名降级", r == "untitled")

r = sanitize_filename("a" * 300)
check("超长截断", len(r) <= 100)

r = sanitize_filename("test<>:*?.pptx")
check("特殊字符替换: <", "<" not in r)
check("特殊字符替换: >", ">" not in r)
check("特殊字符替换: :", ":" not in r)

r = sanitize_filename(".pptx")
check("仅扩展名降级", r == "untitled")

# =============================================================================
# Test 7: GenerationEvent
# =============================================================================
print("\n=== 7. GenerationEvent ===")
e = GenerationEvent(event="template.parse_progress", template_id="t1",
                    progress=0.55, stage="layout_clustered")
d = e.model_dump()
check("progress 值", d["progress"] == 0.55)
check("有时间戳", "timestamp" in d)

e = GenerationEvent(event="template.ready", template_id="t1", progress=1.0, stage="completed")
check("ready 事件", e.event == "template.ready")

e = GenerationEvent(event="template.failed", template_id="t1", error="解析失败")
check("failed 事件含 error", e.error == "解析失败")

# =============================================================================
# Test 8: 进度阶段
# =============================================================================
print("\n=== 8. 进度阶段 ===")
prev = -1.0
for pct, stage in PROGRESS_STAGES:
    check(f"阶段 {stage}: {pct} > {prev}", pct > prev)
    prev = pct
check("终点 = 1.0", PROGRESS_STAGES[-1][0] == 1.0)
check("终点 stage = completed", PROGRESS_STAGES[-1][1] == "completed")

# =============================================================================
# Test 9: TemplateSettings
# =============================================================================
print("\n=== 9. TemplateSettings ===")
s = TemplateSettings()
check("max_file_size=50MB", s.max_file_size == 50 * 1024 * 1024)
check("max_slide_count=100", s.max_slide_count == 100)
check(".pptx in allowed", ".pptx" in s.allowed_extensions)
check("induction_timeout=600", s.induction_timeout == 600)

# =============================================================================
# Test 10: 重启恢复
# =============================================================================
print("\n=== 10. 重启恢复 ===")
ws2 = os.path.join(tempfile.gettempdir(), "pptagent_verify_restart")
if os.path.exists(ws2):
    shutil.rmtree(ws2, ignore_errors=True)
os.makedirs(os.path.join(ws2, "templates", "recover_test"), exist_ok=True)
m = TemplateManifest(template_id="recover_test", name="恢复测试", status=TemplateStatus.READY)
m.save(os.path.join(ws2, "templates", "recover_test"))
reg2 = TemplateRegistry(ws2)
rec = reg2.get("recover_test")
check("重启恢复: 存在", rec is not None)
if rec:
    check("重启恢复: 名称正确", rec.name == "恢复测试")
    check("重启恢复: 状态正确", rec.status == TemplateStatus.READY)

# 损坏 manifest 不阻塞
os.makedirs(os.path.join(ws2, "templates", "bad"), exist_ok=True)
with open(os.path.join(ws2, "templates", "bad", "manifest.json"), "w") as f:
    f.write("not valid {{{")
reg3 = TemplateRegistry(ws2)
check("损坏 manifest 不阻塞", reg3.get("bad") is None)

shutil.rmtree(ws2, ignore_errors=True)

# =============================================================================
# 结果
# =============================================================================
print("\n" + "=" * 50)
print(f"结果: {passed} PASS, {failed} FAIL")
if failed == 0:
    print("全部通过! A 组修改可交付 B/C/D 组.")
else:
    print("存在失败项，需要修复.")
print("=" * 50)
sys.exit(0 if failed == 0 else 1)
