from __future__ import annotations
"""DeepPresenter FastAPI 服务入口。

启动方式::

    uvicorn deeppresenter.server.app:app --host 0.0.0.0 --port 8000

或通过环境变量指定工作区根目录::

    DEEPPRESENTER_WORKSPACE_BASE=/tmp/pptagent uvicorn deeppresenter.server.app:app

开发联调时可使用占位执行器快速验证 API/SSE 链路::

    DEEPPRESENTER_SERVER_PLACEHOLDER=1 uvicorn deeppresenter.server.app:app
"""

import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI

from deeppresenter.server.routes.tasks import router as tasks_router
from deeppresenter.server.services.task_manager import TaskManager

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
        description="PPTAgent 任务编排与实时进度服务",
        version="0.1.0",
    )

    base = workspace_base or WORKSPACE_BASE
    base.mkdir(parents=True, exist_ok=True)

    if use_placeholder is None:
        use_placeholder = _env_flag("DEEPPRESENTER_SERVER_PLACEHOLDER", default=False)
    resolved_config_path = config_path or os.getenv("DEEPPRESENTER_CONFIG_FILE")

    manager = TaskManager(
        workspace_base=base,
        use_placeholder=use_placeholder,
        config_path=resolved_config_path,
    )
    app.state.task_manager = manager

    app.include_router(tasks_router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


# 模块级 app 实例，供 uvicorn 直接使用
app = create_app()
