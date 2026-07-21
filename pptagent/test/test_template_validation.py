"""A 组单元测试 —— 校验、manifest、registry、pop 修复。

这些测试不调用任何 LLM，不访问网络，pytest 秒级跑完。
使用方法: pytest pptagent/test/test_template_validation.py -v
"""

import io
import json
import zipfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def valid_pptx_bytes():
    """构造一个最小合法 PPTX 文件（有效 ZIP 含至少 1 个 slide）"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"></Types>')
        zf.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"></Relationships>')
        zf.writestr("ppt/presentation.xml", '<?xml version="1.0"?><p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"></p:presentation>')
        # Minimal slide — python-pptx may still reject this in practice,
        # but it is a structurally valid ZIP/PPTX.
        zf.writestr("ppt/slides/slide1.xml", '<?xml version="1.0"?><p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"></p:sld>')
        zf.writestr("ppt/_rels/presentation.xml.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/></Relationships>')
        zf.writestr("ppt/slideMasters/slideMaster1.xml", '<?xml version="1.0"?><p:sldMaster xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"></p:sldMaster>')
        zf.writestr("ppt/slideLayouts/slideLayout1.xml", '<?xml version="1.0"?><p:sldLayout xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"></p:sldLayout>')
        zf.writestr("ppt/theme/theme1.xml", '<?xml version="1.0"?><a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"></a:theme>')
    return buf.getvalue()


# =============================================================================
# 1. 文件校验
# =============================================================================


class TestFileValidation:
    """P0-2: 校验扩展名、ZIP/PPTX 可读性、加密/损坏检测"""

    def test_reject_wrong_extension(self):
        """非 .pptx 扩展名应拒收"""
        from deeppresenter.server.routes.templates import ALLOWED_EXTENSIONS

        assert ".pptx" in ALLOWED_EXTENSIONS
        assert ".ppt" not in ALLOWED_EXTENSIONS
        assert ".exe" not in ALLOWED_EXTENSIONS
        assert ".pdf" not in ALLOWED_EXTENSIONS

    def test_reject_non_zip_content(self):
        """非 ZIP 内容应拒收"""
        from deeppresenter.server.services.template_service import validate_pptx

        result = validate_pptx(b"this is not a zip file at all")
        assert result.valid is False
        assert result.error is not None
        assert result.error["code"] == "CORRUPTED_FILE"

    def test_reject_corrupted_zip(self):
        """损坏的 ZIP 应拒收"""
        from deeppresenter.server.services.template_service import validate_pptx

        # 构造一个看起来像 ZIP 但实际损坏的数据
        corrupt = b"PK\x03\x04" + b"\x00" * 200
        result = validate_pptx(corrupt)
        assert result.valid is False

    def test_reject_empty_bytes(self):
        """空内容应拒收"""
        from deeppresenter.server.services.template_service import validate_pptx

        result = validate_pptx(b"")
        assert result.valid is False


# =============================================================================
# 2. 文件名清洗
# =============================================================================


class TestFilenameSanitization:
    """A1: 文件名清洗、路径穿越防护"""

    def test_remove_path_traversal_unix(self):
        from deeppresenter.server.services.template_service import sanitize_filename

        result = sanitize_filename("../../../etc/passwd.pptx")
        assert "etc" not in result or ".." not in result
        # At minimum, no slashes should remain in output
        assert "/" not in result
        assert "\\" not in result

    def test_remove_path_traversal_windows(self):
        from deeppresenter.server.services.template_service import sanitize_filename

        result = sanitize_filename("..\\..\\windows\\system32.pptx")
        assert "\\" not in result
        assert ".." not in result

    def test_handle_chinese_and_spaces(self):
        from deeppresenter.server.services.template_service import sanitize_filename

        name = sanitize_filename("我的 商务模板 (1).pptx")
        assert ".." not in name
        assert "/" not in name
        assert len(name) > 0
        # Chinese characters should be preserved
        assert "我的" in name or "商务" in name or "模板" in name

    def test_handle_special_chars(self):
        from deeppresenter.server.services.template_service import sanitize_filename

        result = sanitize_filename("test<>:*?.pptx")
        assert "<" not in result
        assert ">" not in result
        assert ":" not in result
        assert "*" not in result
        assert "?" not in result

    def test_empty_name_fallback(self):
        from deeppresenter.server.services.template_service import sanitize_filename

        assert sanitize_filename("") == "untitled"
        assert sanitize_filename(".pptx") == "untitled"
        assert sanitize_filename("   ") == "untitled"

    def test_truncate_long_name(self):
        from deeppresenter.server.services.template_service import sanitize_filename

        name = sanitize_filename("a" * 300)
        assert len(name) <= 100


# =============================================================================
# 3. Manifest 模型
# =============================================================================


class TestTemplateManifest:
    """A1: manifest.json 模型定义与持久化"""

    def test_manifest_defaults(self):
        from deeppresenter.server.models.templates import TemplateManifest, TemplateStatus

        m = TemplateManifest(template_id="test", name="test")
        assert m.status == TemplateStatus.PARSING
        assert m.source_hash == ""
        assert m.slide_count == 0
        assert m.layout_count == 0
        assert m.error is None

    def test_manifest_roundtrip(self, tmp_path):
        from deeppresenter.server.models.templates import TemplateManifest

        m = TemplateManifest(
            template_id="abc123",
            name="测试模板",
            source_hash="sha256:def456",
            slide_count=10,
            aspect_ratio="16:9",
            layout_count=5,
        )
        m.save(tmp_path)
        loaded = TemplateManifest.load(tmp_path)
        assert loaded.template_id == m.template_id
        assert loaded.name == m.name
        assert loaded.source_hash == m.source_hash
        assert loaded.slide_count == 10
        assert loaded.layout_count == 5

    def test_manifest_json_serializable(self):
        from deeppresenter.server.models.templates import TemplateManifest

        m = TemplateManifest(template_id="t1", name="test", fonts=["Arial", "微软雅黑"])
        d = m.model_dump()
        assert json.dumps(d)  # 不抛异常

    def test_manifest_status_values(self):
        from deeppresenter.server.models.templates import TemplateStatus

        assert TemplateStatus.PARSING.value == "parsing"
        assert TemplateStatus.READY.value == "ready"
        assert TemplateStatus.FAILED.value == "failed"

    def test_manifest_error_codes(self):
        from deeppresenter.server.models.templates import TemplateErrorCode

        assert TemplateErrorCode.FILE_TOO_LARGE == "FILE_TOO_LARGE"
        assert TemplateErrorCode.ENCRYPTED_FILE == "ENCRYPTED_FILE"
        assert TemplateErrorCode.CORRUPTED_FILE == "CORRUPTED_FILE"


# =============================================================================
# 4. TemplateRegistry
# =============================================================================


class TestTemplateRegistry:
    """P0-6/A2: 注册中心 —— 增删查改、hash 去重、缓存保护"""

    def test_register_and_get(self, tmp_path):
        from deeppresenter.server.services.template_registry import TemplateRegistry
        from deeppresenter.server.models.templates import TemplateManifest

        registry = TemplateRegistry(tmp_path)
        m = TemplateManifest(template_id="t1", name="test")
        registry.register(m)
        assert registry.get("t1") == m
        assert registry.get("nonexistent") is None

    def test_list_all_filters_failed(self, tmp_path):
        from deeppresenter.server.services.template_registry import TemplateRegistry
        from deeppresenter.server.models.templates import (
            TemplateManifest,
            TemplateStatus,
        )

        registry = TemplateRegistry(tmp_path)
        registry.register(
            TemplateManifest(template_id="t1", name="ok", status=TemplateStatus.READY)
        )
        registry.register(
            TemplateManifest(template_id="t2", name="fail", status=TemplateStatus.FAILED)
        )

        # 默认不过滤掉 parsing 的，但过滤掉 failed 的
        all_list = registry.list_all(include_failed=False)
        ids = {m.template_id for m in all_list}
        assert "t1" in ids
        assert "t2" not in ids

        # include_failed=True 应返回全部
        all_with_failed = registry.list_all(include_failed=True)
        assert len(all_with_failed) == 2

    def test_find_by_hash(self, tmp_path):
        from deeppresenter.server.services.template_registry import TemplateRegistry
        from deeppresenter.server.models.templates import TemplateManifest

        registry = TemplateRegistry(tmp_path)
        m = TemplateManifest(template_id="t1", source_hash="abc123")
        registry.register(m)
        assert registry.find_by_hash("abc123") == m
        assert registry.find_by_hash("notfound") is None

    def test_unregister_removes_from_index(self, tmp_path):
        from deeppresenter.server.services.template_registry import TemplateRegistry
        from deeppresenter.server.models.templates import TemplateManifest

        registry = TemplateRegistry(tmp_path)
        m = TemplateManifest(template_id="t1", source_hash="abc")
        registry.register(m)
        registry.unregister("t1")
        assert registry.get("t1") is None
        assert registry.find_by_hash("abc") is None

    def test_cache_returns_deepcopy(self, tmp_path):
        """P0-9: 缓存返回 deepcopy，外部 pop() 不影响源数据"""
        from deeppresenter.server.services.template_registry import TemplateRegistry

        registry = TemplateRegistry(tmp_path)
        data = {
            "slide_induction": {
                "language": {"lid": "zh"},
                "functional_keys": ["opening", "ending"],
                "title:text": {"template_id": 1, "slides": [1]},
            }
        }
        registry.cache_template("t1", data)

        # 模拟 pptgen 修改前的破坏性行为
        cached = registry.get_cached("t1")
        cached["slide_induction"].pop("language")
        cached["slide_induction"].pop("functional_keys")

        # 源数据不应被影响
        fresh = registry.get_cached("t1")
        assert "language" in fresh["slide_induction"]
        assert "functional_keys" in fresh["slide_induction"]

    def test_restart_recovery(self, tmp_path):
        """服务重启后从 manifest.json 恢复模板列表"""
        from deeppresenter.server.services.template_registry import TemplateRegistry
        from deeppresenter.server.models.templates import (
            TemplateManifest,
            TemplateStatus,
        )

        # 模拟已存在的模板目录
        template_dir = tmp_path / "templates" / "abc"
        template_dir.mkdir(parents=True)
        m = TemplateManifest(
            template_id="abc", name="recovered", status=TemplateStatus.READY
        )
        m.save(template_dir)

        # 新建 registry —— 应自动加载
        registry = TemplateRegistry(tmp_path)
        recovered = registry.get("abc")
        assert recovered is not None
        assert recovered.name == "recovered"
        assert recovered.status == TemplateStatus.READY

    def test_restart_skips_invalid_manifest(self, tmp_path):
        """损坏的 manifest.json 不阻塞启动"""
        from deeppresenter.server.services.template_registry import TemplateRegistry

        template_dir = tmp_path / "templates" / "bad"
        template_dir.mkdir(parents=True)
        (template_dir / "manifest.json").write_text("not valid json {{{", encoding="utf-8")

        # 不应抛异常
        registry = TemplateRegistry(tmp_path)
        assert registry.get("bad") is None

    def test_update_manifest(self, tmp_path):
        from deeppresenter.server.services.template_registry import TemplateRegistry
        from deeppresenter.server.models.templates import (
            TemplateManifest,
            TemplateStatus,
        )

        registry = TemplateRegistry(tmp_path)
        m = TemplateManifest(template_id="t1", name="test", status=TemplateStatus.PARSING)
        registry.register(m)

        # 模拟解析完成
        m.status = TemplateStatus.READY
        m.slide_count = 10
        registry.update_manifest(m)

        updated = registry.get("t1")
        assert updated.status == TemplateStatus.READY
        assert updated.slide_count == 10

    def test_invalidate_cache(self, tmp_path):
        from deeppresenter.server.services.template_registry import TemplateRegistry

        registry = TemplateRegistry(tmp_path)
        registry.cache_template("t1", {"data": 1})
        assert registry.get_cached("t1") is not None

        registry.invalidate_cache("t1")
        assert registry.get_cached("t1") is None

    def test_hash_index_updated_on_reregister(self, tmp_path):
        from deeppresenter.server.services.template_registry import TemplateRegistry
        from deeppresenter.server.models.templates import TemplateManifest

        registry = TemplateRegistry(tmp_path)
        m1 = TemplateManifest(template_id="t1", source_hash="abc")
        m2 = TemplateManifest(template_id="t2", source_hash="abc")
        registry.register(m1)
        registry.register(m2)

        # hash 索引应指向最新的
        assert registry.find_by_hash("abc").template_id == "t2"


# =============================================================================
# 5. pop() 修复验证 (P0-9)
# =============================================================================


class TestPopFix:
    """验证 pptgen.set_reference() 不再破坏传入的字典"""

    def test_set_reference_does_not_mutate_input(self):
        """set_reference 使用 .get() 而非 .pop() —— 参数不被修改"""
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

        # 绕过 __post_init__ 中的 _hire_staffs
        gen._initialized = False
        gen.staffs = {}

        gen.set_reference(slide_induction=slide_induction, presentation=mock_prs)

        # 验证参数未被修改
        assert slide_induction == original, (
            f"set_reference 破坏了传入的字典！\n"
            f"  原始: {original}\n"
            f"  修改后: {slide_induction}"
        )

    def test_set_reference_twice_no_error(self):
        """连续两次 set_reference 同一数据不应报错"""
        from pptagent.pptgen import PPTGen

        slide_induction = {
            "language": {"lid": "en"},
            "functional_keys": ["opening"],
            "title:text": {
                "template_id": 1,
                "slides": [1],
                "content_schema": {},
            },
        }

        mock_prs = MagicMock()
        mock_prs.slides = []

        gen = PPTGen(language_model=MagicMock(), vision_model=MagicMock())
        gen._initialized = False
        gen.staffs = {}

        # 第一次
        gen.set_reference(
            slide_induction=deepcopy(slide_induction), presentation=mock_prs
        )
        # 第二次 —— 不应 KeyError
        gen.set_reference(
            slide_induction=deepcopy(slide_induction), presentation=mock_prs
        )

    def test_set_reference_five_times_no_error(self):
        """连续五次 set_reference 同一数据不应报错"""
        from pptagent.pptgen import PPTGen

        slide_induction = {
            "language": {"lid": "zh"},
            "functional_keys": ["opening", "toc", "ending"],
            "title:text": {
                "template_id": 1,
                "slides": [1],
                "content_schema": {},
            },
            "content:image": {
                "template_id": 2,
                "slides": [2],
                "content_schema": {},
            },
        }

        mock_prs = MagicMock()
        mock_prs.slides = []

        gen = PPTGen(language_model=MagicMock(), vision_model=MagicMock())
        gen._initialized = False
        gen.staffs = {}

        for _ in range(5):
            gen.set_reference(
                slide_induction=deepcopy(slide_induction), presentation=mock_prs
            )


# =============================================================================
# 6. GenerationEvent
# =============================================================================


class TestGenerationEvent:
    def test_event_creation(self):
        from deeppresenter.server.models.templates import GenerationEvent

        event = GenerationEvent(
            event="template.parse_progress",
            template_id="test",
            progress=0.5,
            stage="layout_clustered",
        )
        d = event.model_dump()
        assert d["event"] == "template.parse_progress"
        assert d["progress"] == 0.5
        assert "timestamp" in d

    def test_event_with_error(self):
        from deeppresenter.server.models.templates import GenerationEvent

        event = GenerationEvent(
            event="template.failed",
            template_id="test",
            error="Something went wrong",
        )
        assert event.error == "Something went wrong"
        assert event.progress == 0.0


# =============================================================================
# 7. Progress stages
# =============================================================================


class TestProgressStages:
    def test_stages_order(self):
        """进度阶段应单调递增"""
        from deeppresenter.server.services.template_service import PROGRESS_STAGES

        prev = -1.0
        for pct, _ in PROGRESS_STAGES:
            assert pct > prev, f"Progress stages must be monotonic: {pct} <= {prev}"
            prev = pct

    def test_stages_end_at_one(self):
        from deeppresenter.server.services.template_service import PROGRESS_STAGES

        assert PROGRESS_STAGES[-1][0] == 1.0
        assert PROGRESS_STAGES[-1][1] == "completed"


# =============================================================================
# 8. TemplateSettings
# =============================================================================


class TestTemplateSettings:
    def test_defaults(self):
        from deeppresenter.server.models.templates import TemplateSettings

        settings = TemplateSettings()
        assert settings.max_file_size == 50 * 1024 * 1024
        assert settings.max_slide_count == 100
        assert ".pptx" in settings.allowed_extensions
        assert settings.induction_timeout == 600
