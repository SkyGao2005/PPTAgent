from __future__ import annotations
"""模板相关 FastAPI 路由。

当前 C2 联调只需要让完整前端可加载系统模板卡片；真实模板上传/解析
属于其他模块，后续可在这些端点上替换为持久化实现。
"""

import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api/templates")

SYSTEM_TEMPLATES = [
    {
        "id": "obsidian",
        "name": "曜石商务",
        "description": "深空蓝与鎏金点缀，适合年度汇报与高层提案",
        "owner": "system",
        "status": "ready",
        "slides": 32,
        "ratio": "16:9",
        "layouts": ["封面", "目录", "章节页", "标题 + 要点", "双栏对比", "数据看板"],
        "palette": {
            "bg": "#101522",
            "surface": "#1A2233",
            "primary": "#E8B45A",
            "accent": "#8FA3CC",
            "ink": "#F2EEE6",
            "dark": True,
        },
    },
    {
        "id": "mist",
        "name": "晨雾极简",
        "description": "低饱和灰蓝与充足留白，适合策略与咨询场景",
        "owner": "system",
        "status": "ready",
        "slides": 26,
        "ratio": "16:9",
        "layouts": ["封面", "目录", "章节页", "标题 + 要点", "左文右图", "引言页"],
        "palette": {
            "bg": "#F4F6F8",
            "surface": "#FFFFFF",
            "primary": "#38506B",
            "accent": "#7FA6C9",
            "ink": "#22303E",
            "dark": False,
        },
    },
    {
        "id": "azure",
        "name": "学术深蓝",
        "description": "严谨的学术蓝配色，适合研究汇报与课题答辩",
        "owner": "system",
        "status": "ready",
        "slides": 28,
        "ratio": "4:3",
        "layouts": ["封面", "目录", "章节页", "标题 + 要点", "时间线", "封底"],
        "palette": {
            "bg": "#FFFFFF",
            "surface": "#F2F5FA",
            "primary": "#1D4E89",
            "accent": "#5B8DEF",
            "ink": "#1B2430",
            "dark": False,
        },
    },
    {
        "id": "jade",
        "name": "翡翠年报",
        "description": "沉稳墨绿与柔和灰白，适合 ESG 报告与年度总结",
        "owner": "system",
        "status": "ready",
        "slides": 30,
        "ratio": "16:9",
        "layouts": ["封面", "目录", "章节页", "双栏对比", "数据看板", "时间线"],
        "palette": {
            "bg": "#F5F7F4",
            "surface": "#FFFFFF",
            "primary": "#1E5C4A",
            "accent": "#8FBCA5",
            "ink": "#20302A",
            "dark": False,
        },
    },
]


@router.get("")
async def list_templates():
    """返回完整前端创建页需要展示的系统模板。"""
    return SYSTEM_TEMPLATES


@router.get("/events")
async def subscribe_template_events(last_seq: int = 0):
    """模板解析事件流占位。

    系统模板无需解析事件；保持 SSE 连接并发送心跳，避免前端反复 404。
    """

    async def _event_stream():
        while True:
            await asyncio.sleep(30)
            heartbeat = {
                "task_id": "templates",
                "seq": last_seq,
                "type": "heartbeat",
                "stage": None,
                "status": None,
                "progress": None,
                "message": "",
                "slide_id": None,
                "slide_index": None,
                "total_slides": None,
                "artifact_url": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "payload": {},
            }
            yield f"data: {json.dumps(heartbeat)}\n\n"

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
