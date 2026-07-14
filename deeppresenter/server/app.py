"""DeepPresenter FastAPI 服务入口。

启动方式::

    uvicorn deeppresenter.server.app:app --host 0.0.0.0 --port 8000

或通过环境变量指定工作区根目录::

    DEEPPRESENTER_WORKSPACE_BASE=/tmp/pptagent uvicorn deeppresenter.server.app:app
"""

import os
from pathlib import Path

from fastapi import FastAPI

from deeppresenter.server.routes.tasks import router as tasks_router
from deeppresenter.server.services.task_manager import TaskManager

WORKSPACE_BASE = Path(
    os.getenv(
        "DEEPPRESENTER_WORKSPACE_BASE",
        str(Path.home() / ".cache" / "deeppresenter"),
    )
)


def create_app(workspace_base: Path | None = None) -> FastAPI:
    """创建并配置 FastAPI 应用。

    Args:
        workspace_base: 任务工作区根目录，默认使用环境变量或 ~/.cache/deeppresenter
    """
    app = FastAPI(
        title="DeepPresenter API",
        description="PPTAgent 任务编排与实时进度服务",
        version="0.1.0",
    )

    base = workspace_base or WORKSPACE_BASE
    base.mkdir(parents=True, exist_ok=True)

    manager = TaskManager(workspace_base=base)
    app.state.task_manager = manager

    app.include_router(tasks_router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


# 模块级 app 实例，供 uvicorn 直接使用
app = create_app()
