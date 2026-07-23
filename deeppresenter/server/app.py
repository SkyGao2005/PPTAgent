"""DeepPresenter FastAPI 服务入口。

启动方式::

    uvicorn deeppresenter.server.app:app --host 0.0.0.0 --port 8000

或通过环境变量指定工作区根目录::

    DEEPPRESENTER_WORKSPACE_BASE=/tmp/pptagent uvicorn deeppresenter.server.app:app

开发联调时可使用占位执行器快速验证 API/SSE 链路::

    DEEPPRESENTER_SERVER_PLACEHOLDER=1 uvicorn deeppresenter.server.app:app
"""

from __future__ import annotations

import asyncio
import os
from functools import partial
from pathlib import Path
from typing import Optional

from fastapi import FastAPI

from deeppresenter.server.models.templates import TemplateSettings
from deeppresenter.server.routes.attachments import router as attachments_router
from deeppresenter.server.routes.outlines import router as outlines_router
from deeppresenter.server.routes.slide_editing import router as slide_editing_router
from deeppresenter.server.routes.templates import router as templates_router
from deeppresenter.server.routes.tasks import router as tasks_router
from deeppresenter.server.services.outline_service import OutlineService
from deeppresenter.server.services.task_manager import TaskManager
from deeppresenter.server.services.template_catalog import bundled_templates_root
from deeppresenter.server.services.template_registry import TemplateRegistry
from deeppresenter.server.services.template_service import TemplateInductionService
from deeppresenter.templates.context import TemplateContextProvider
from deeppresenter.templates.store import TemplateStore

WORKSPACE_BASE = Path(
    os.getenv(
        "DEEPPRESENTER_WORKSPACE_BASE",
        str(Path.home() / ".cache" / "deeppresenter"),
    )
)


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _build_template_induction_service(
    config_path: str | None,
    workspace: Path,
    settings: TemplateSettings,
) -> TemplateInductionService:
    """Build the independent Template IR compiler, with per-page VLM labels."""
    from deeppresenter.templates import (
        DeepPresenterStructuredVLMClient,
        DeterministicAnnotator,
        LibreOfficeRenderer,
        TemplateCompiler,
        VLMAnnotator,
    )
    from deeppresenter.utils.config import DeepPresenterConfig

    config = DeepPresenterConfig.load_from_file(config_path)
    vision_model = config.vision_model or config.design_agent
    annotator = DeterministicAnnotator()
    if vision_model.is_multimodal:
        annotator = VLMAnnotator(
            DeepPresenterStructuredVLMClient(vision_model),
            model_id=vision_model.model_name,
        )

    return TemplateInductionService(
        compiler=TemplateCompiler(
            renderer=LibreOfficeRenderer(required=True),
            annotator=annotator,
        ),
        workspace=workspace,
        settings=settings,
    )


def _design_context_budget(config_path: str | None):
    """Read the design agent's context budget, falling back to the defaults."""

    from deeppresenter.utils.config import ContextBudgetConfig, DeepPresenterConfig

    try:
        config = DeepPresenterConfig.load_from_file(config_path)
    except (OSError, ValueError):
        # Template listing must work before a model is configured.
        return ContextBudgetConfig()
    return config.design_agent


def create_app(
    workspace_base: Path | None = None,
    *,
    use_placeholder: Optional[bool] = None,
    config_path: Optional[str] = None,
) -> FastAPI:
    """创建并配置 FastAPI 应用。

    Args:
        workspace_base: 任务工作区根目录，默认使用环境变量或 ~/.cache/deeppresenter
        use_placeholder: 是否使用占位执行器，默认由环境变量控制
        config_path: DeepPresenter 配置文件路径，默认读取 DEEPPRESENTER_CONFIG_FILE
    """
    app = FastAPI(
        title="DeepPresenter API",
        description="DeepPresenter HTML generation and Template IR service",
        version="0.2.0",
    )

    base = workspace_base or WORKSPACE_BASE
    base.mkdir(parents=True, exist_ok=True)

    if use_placeholder is None:
        use_placeholder = _env_flag("DEEPPRESENTER_SERVER_PLACEHOLDER", default=False)
    resolved_config_path = config_path or os.getenv("DEEPPRESENTER_CONFIG_FILE")
    try:
        max_concurrent = int(os.getenv("DEEPPRESENTER_MAX_CONCURRENT_TASKS", "2"))
    except ValueError:
        max_concurrent = 2

    template_workspace = base / "templates_api"
    template_settings = TemplateSettings()
    app.state.template_registry = TemplateRegistry(template_workspace)
    app.state.template_settings = template_settings
    template_store = TemplateStore(
        [app.state.template_registry.templates_dir, bundled_templates_root()]
    )
    # Bound template projections by the model that will actually read them, so
    # config.yaml budgets take effect instead of only the shared defaults.
    template_context_provider = TemplateContextProvider(
        template_store,
        _design_context_budget(resolved_config_path),
    )
    app.state.template_store = template_store
    app.state.template_context_provider = template_context_provider

    manager = TaskManager(
        workspace_base=base,
        use_placeholder=use_placeholder,
        config_path=resolved_config_path,
        max_concurrent=max_concurrent,
        template_context_provider=template_context_provider,
    )
    app.state.task_manager = manager
    app.state.outline_service = OutlineService(
        workspace_base=base,
        task_manager=manager,
        use_placeholder=use_placeholder,
        config_path=resolved_config_path,
        template_context_provider=template_context_provider,
    )

    app.state.induction_service = None
    app.state.template_service_lock = asyncio.Lock()
    app.state.template_service_factory = partial(
        _build_template_induction_service,
        resolved_config_path,
        template_workspace,
        template_settings,
    )

    @app.on_event("startup")
    async def _restore_tasks():
        """服务启动时从磁盘恢复任务快照，孤儿 running 标记为 failed。"""
        restored = await TaskManager.restore_snapshots(base)
        # 将恢复的快照/总线合并到当前 manager
        for tid, snap in restored._snapshots.items():
            if tid not in manager._snapshots:
                manager._snapshots[tid] = snap
        for tid, bus in restored._buses.items():
            if tid not in manager._buses:
                manager._buses[tid] = bus
        for tid, reporter in restored._reporters.items():
            if tid not in manager._reporters:
                manager._reporters[tid] = reporter
        for tid, ps in restored._preview_services.items():
            if tid not in manager._preview_services:
                manager._preview_services[tid] = ps
        for tid, evt in restored._cancel_events.items():
            if tid not in manager._cancel_events:
                manager._cancel_events[tid] = evt

    app.include_router(outlines_router)
    app.include_router(tasks_router)
    app.include_router(attachments_router)
    app.include_router(slide_editing_router)
    app.include_router(templates_router)

    @app.get("/health")
    async def health():
        templates = app.state.template_registry.list_all(include_failed=True)
        return {"status": "ok", "templates": len(templates)}

    return app


# 模块级 app 实例，供 uvicorn 直接使用
app = create_app()
