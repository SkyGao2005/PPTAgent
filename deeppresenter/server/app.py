"""A 组模板管理 API 服务器

FastAPI 入口 —— 启动模板管理的 6 个 REST 端点 + SSE 进度推送。
与 Gradio Web UI (webui.py) 并行运行，共享同一套配置。

启动方式:
    python -m deeppresenter.server.app          # 默认端口 8080
    python -m deeppresenter.server.app --port 8080
    python -m deeppresenter.server.app --config /path/to/config.yaml

端点:
    POST   /api/templates                     上传模板并创建解析任务
    GET    /api/templates                     获取可用及解析中的模板
    GET    /api/templates/{template_id}        获取模板详情和解析状态
    GET    /api/templates/{template_id}/events 订阅模板解析 SSE 事件
    POST   /api/templates/{template_id}/retry  重试失败解析
    DELETE /api/templates/{template_id}        删除用户模板及缓存
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from deeppresenter.server.models.templates import TemplateManifest, TemplateSettings
from deeppresenter.server.routes.templates import router as templates_router
from deeppresenter.server.services.template_registry import TemplateRegistry
from deeppresenter.server.services.template_service import TemplateInductionService
from deeppresenter.utils.config import DeepPresenterConfig
from deeppresenter.utils.constants import WORKSPACE_BASE

logger = logging.getLogger("deeppresenter.server")


def create_app(config: DeepPresenterConfig) -> FastAPI:
    """创建 FastAPI 应用，注入所有依赖"""

    app = FastAPI(
        title="PPTAgent Template Manager (A组)",
        version="1.0.0",
        description="模板上传、解析、进度推送、热加载 API",
    )

    workspace = WORKSPACE_BASE / "templates_api"

    # ── 初始化 TemplateRegistry ──
    registry = TemplateRegistry(workspace)
    app.state.template_registry = registry

    # ── 初始化 TemplateSettings ──
    settings = TemplateSettings()
    app.state.template_settings = settings

    # ── 构建 AsyncLLM 实例 ──
    # pptagent.llms.AsyncLLM 的构造函数: AsyncLLM(model, base_url, api_key, timeout)
    from pptagent.llms import AsyncLLM

    language_model = AsyncLLM(
        model=config.research_agent.model,
        base_url=config.research_agent.base_url,
        api_key=config.research_agent.api_key,
    )

    # vision_model: 优先用专门的 vision_model，否则用 design_agent
    vision_cfg = config.vision_model or config.design_agent
    vision_model = AsyncLLM(
        model=vision_cfg.model,
        base_url=vision_cfg.base_url,
        api_key=vision_cfg.api_key,
    )

    # ── 初始化图像模型（布局聚类用）──
    from pptagent.model_utils import get_image_model

    # 优先用本地下载的模型，否则让 HF 下载
    local_model = "/home/awa_subaru/.cache/huggingface/hub/models--google--vit-base-patch16-224-in21k/snapshots/main"
    import os as _os
    if _os.path.isdir(local_model):
        print(f"Loading image model from local: {local_model}")
        image_models = get_image_model("cpu", model_base=local_model)
    else:
        print("Loading image model (google/vit-base-patch16-224-in21k from HF)...")
        image_models = get_image_model("cpu")
    print("Image model loaded.")

    # ── 初始化 TemplateInductionService ──
    induction_service = TemplateInductionService(
        language_model=language_model,
        vision_model=vision_model,
        image_models=image_models,
        workspace=workspace,
        settings=settings,
    )
    app.state.induction_service = induction_service

    # ── 挂载路由 ──
    app.include_router(templates_router)

    @app.get("/health")
    async def health():
        """健康检查 + 模板统计"""
        manifests = registry.list_all(include_failed=True)
        ready = sum(1 for m in manifests if m.status.value == "ready")
        parsing = sum(1 for m in manifests if m.status.value == "parsing")
        failed = sum(1 for m in manifests if m.status.value == "failed")
        return {
            "status": "ok",
            "workspace": str(workspace),
            "templates": {
                "total": len(manifests),
                "ready": ready,
                "parsing": parsing,
                "failed": failed,
            },
        }

    logger.info(f"Template API server created. Workspace: {workspace}")
    logger.info(f"Loaded {len(registry.list_all(include_failed=True))} "
                f"templates from disk.")
    return app


def main():
    parser = argparse.ArgumentParser(description="A组 模板管理 API 服务器")
    parser.add_argument("--port", type=int, default=8080, help="服务端口 (默认 8080)")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    parser.add_argument("--config", default=None, help="config.yaml 路径")
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    args = parser.parse_args()

    print(f"Loading config...")
    config = DeepPresenterConfig.load_from_file(args.config)

    print(f"Creating app...")
    app = create_app(config)

    print(f"\n{'='*60}")
    print(f"  A组 模板管理 API 服务")
    print(f"  地址: http://{args.host}:{args.port}")
    print(f"  文档: http://localhost:{args.port}/docs")
    print(f"  健康检查: http://localhost:{args.port}/health")
    print(f"{'='*60}")
    print(f"\n  API 端点:")
    print(f"  POST   /api/templates                    上传模板")
    print(f"  GET    /api/templates                    模板列表")
    print(f"  GET    /api/templates/{{id}}              模板详情")
    print(f"  GET    /api/templates/{{id}}/events        SSE 进度")
    print(f"  POST   /api/templates/{{id}}/retry        重试解析")
    print(f"  DELETE /api/templates/{{id}}              删除模板")
    print(f"\n{'='*60}\n")

    uvicorn.run(app, host=args.host, port=args.port, reload=args.reload,
                log_level="info")


if __name__ == "__main__":
    main()
