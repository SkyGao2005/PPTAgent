# PPTAgent 二次开发 —— 模板上传解析与模板化 PPT 生成

基于 [PPTAgent](https://github.com/icip-cas/PPTAgent) 二次开发，在原项目"自由生成"模式基础上新增**模板上传解析、模板注册管理和模板化 PPT 生成**能力。

## 项目概述

本项目通过以下核心模块实现了从"用户上传自定义 PPTX 模板"到"基于该模板生成风格统一的幻灯片"的完整链路：

- **模板 API 服务器** — FastAPI 服务，提供模板上传、解析、查询和删除等 REST 接口
- **模板解析服务** — 将离线命令行脚本重构为可调用服务，包含文件校验、页面渲染、图片标注、布局聚类和内容模式提取等 7 阶段流程
- **模板注册中心** — 线程安全的模板注册表，支持 SHA256 去重、清单持久化、服务重启后自动恢复
- **MCP 服务器** — 基于 FastMCP，提供 `list_templates`、`set_template`、`create_slide`、`write_slide`、`generate_slide` 等工具，LLM Agent 交互式完成幻灯片生成
- **Web UI 模板选择** — Gradio 界面新增模板下拉框，支持"自由生成"和"模板"两种模式切换
- **Docker 沙箱镜像** — 适配中国网络环境的沙箱构建文件

## 系统架构

系统由三个独立进程组成，通过 MCP 协议和 REST API 通信：

- **模板 API 服务器**（端口 8080）：FastAPI 应用，管理模板上传、解析和查询
- **Web UI**（端口 7861）：Gradio 应用，提供用户交互界面，通过 MCP 协议与 PPTAgent MCP Server 通信
- **PPTAgent MCP Server**：FastMCP 服务，提供幻灯片生成工具，LLM Agent 通过工具调用完成模板选择→布局选择→内容生成→幻灯片编辑→保存的完整流程

### 模板解析流程（7 阶段）

1. **文件校验** — 检查 ZIP 完整性、加密状态、幻灯片数量（5%）
2. **PPT 规范化** — 复制 `original.pptx` → `source.pptx`，创建 Presentation 对象（10%）
3. **页面渲染** — 渲染原始页和空布局页（35%）
4. **图片标注** — 视觉模型为图片生成描述，失败回退到 `semantic_name`（55%）
5. **布局聚类** — ViT 模型提取图像嵌入，余弦相似度聚类相似布局（55%）
6. **内容模式提取** — LLM 分析每类布局的元素组成（80%）
7. **缩略图生成** — 取第一页生成 320×240 缩略图（100%）

### 模板化生成流程

1. LLM 调用 `list_templates` 查看可用模板
2. LLM 调用 `set_template("template-name")` 加载模板数据
3. `PPTGen.set_reference()` 将模板布局转换为 Layout 对象，deepcopy source.pptx 作为画布
4. LLM 调用 `create_slide(layout)` 选择布局 → `write_slide(elements)` 填充内容 → `generate_slide()` 生成幻灯片
5. 每张幻灯片通过 deepcopy 模板页 + 替换文字/图片实现，保留模板背景、配色和装饰
6. 最后调用 `save_generated_slides(path)` 保存完整 PPTX

## 环境依赖

### 运行环境

| 依赖 | 说明 |
|---|---|
| 操作系统 | Linux（推荐 WSL2 Ubuntu） |
| Python | >= 3.11（推荐 3.13） |
| Docker | 用于沙箱容器（可选） |
| LibreOffice | 用于 .ppt → .pptx 转换（可选） |
| Playwright | 用于 HTML → PPTX 转换 |

### 核心 Python 依赖

`fastapi[all]`、`uvicorn`、`gradio>=5.47.2`、`fastmcp>=2.10.0`、`openai>=1.108.2`、`python-pptx>=0.6.21`、`pptagent-pptx>=0.0.1`、`transformers<4.50.0`、`torch`、`torchvision`、`pillow`、`playwright>=1.55.0`、`docker>=7.1.0`、`aiohttp>=3.9.0`、`aiometer`、`fasttext>=0.9.3`、`modelscope>=1.35.1`

### 安装步骤

```bash
# 1. 克隆/复制项目到本地
git clone <repo-url>
cd PPTAgent

# 2. 创建虚拟环境并安装依赖
python3 -m venv .venv && source .venv/bin/activate
uv pip install -e ".[full]"

# 3. 安装 Playwright
playwright install chromium

# 4. 配置 LLM（编辑 deeppresenter/config.yaml）
# research_agent:
#   model: "your-model"
#   base_url: "https://your-api-endpoint/v1"
#   api_key: "your-api-key"

# 5. 配置 MCP
cp deeppresenter/mcp.json.example deeppresenter/mcp.json
```

## 启动方式

### Web UI（模板化生成）

```bash
cd <项目目录>
source .venv/bin/activate
python webui.py
# 访问 http://localhost:7861
# 选择输出类型为"模版 (templates)"，选择模板后上传文档即可生成
```

### 模板 API 服务器（模板管理）

```bash
python -m deeppresenter.server.app --port 8080
# API 文档: http://localhost:8080/docs
# 健康检查: http://localhost:8080/health
```

### 上传模板

```bash
curl -X POST http://localhost:8080/api/templates -F "file=@template.pptx"
```

上传后通过 `GET /api/templates/{id}/events` 订阅 SSE 进度，等待 `status` 变为 `ready` 后即可在 Web UI 中选择使用。

## API 参考

模板 API 服务器默认运行在端口 8080，提供 7 个端点：

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/templates` | 上传模板（.pptx），SHA256 去重 |
| `GET` | `/api/templates` | 模板列表（状态、页数、宽高比等摘要） |
| `GET` | `/api/templates/{id}` | 模板详情（元素列表、布局数等完整信息） |
| `GET` | `/api/templates/{id}/events` | SSE 实时解析进度推送 |
| `POST` | `/api/templates/{id}/retry` | 重新解析失败的模板 |
| `DELETE` | `/api/templates/{id}` | 删除模板及关联缓存 |
| `GET` | `/health` | 健康检查（服务状态 + 模板统计） |

### SSE 进度事件

| 事件 | 说明 |
|---|---|
| `template.parse_started` | 解析开始 |
| `template.parse_progress` | 阶段进度（含 `stage` 和 `progress` 字段） |
| `template.ready` | 解析完成 |
| `template.failed` | 解析失败（含 `error` 字段） |

模板状态：`parsing`（解析中）、`ready`（可用）、`failed`（失败，可重试）

## 关键 Bug 修复

| Bug | 影响 | 根因 | 修复 |
|---|---|---|---|
| `set_reference()` 所有模板崩溃 | `set_template` 100% 失败 | dict comprehension 对 `functional_keys`(list) 执行 `**` 解包 | 添加 key 过滤 `if k not in ("functional_keys", "language")` |
| `_hide_small_pics()` None caption | 含装饰图的模板加载失败 | `image_stats` 为空 → `caption=None` → `remove_item(None)` | 跳过 caption 为 None 的图片 |
| `image_stats` 空序列化 | 模板重载后图片 caption 丢失 | finally 块 fallback caption 未同步回 `image_labler` | 序列化前同步 caption |
| 占位符语义名丢失 | 所有 Placeholder 显示为 "placeholder" | `auto_shape_type` 为 None → 统一赋值 | 优先读取 `placeholder_format.type.name`（TITLE/SUBTITLE/BODY） |
| Qwen/DeepSeek 结构化输出失败 | 非 OpenAI 模型 `parse()` 报错 | Qwen/DeepSeek 不支持 `response_format` 参数 | 捕获异常后使用 `create()` + 内联 JSON Schema |
| Python 3.13 ExceptionGroup | TaskGroup 异常未被捕获 | `ExceptionGroup` 不继承 `Exception` | 改用 `BaseException` 捕获 |
| aiometer 协程错误 | layout_induct 崩溃 | `run_all` 需要 callable 而非 coroutine | 使用 `functools.partial` 包装 |
| HuggingFace 下载不可达 | ViT 模型加载失败 | 中国网络无法访问 huggingface.co | 支持本地模型路径（`model_base` 参数） |
| Docker 构建网络超时 | 沙箱镜像构建失败 | apt/npm/Playwright 默认源不可达 | 添加国内镜像源 + 重试逻辑 |

### v1.1 模板排版优化

| 修复项 | 说明 | 涉及文件 |
|---|---|---|
| 文本自动适配 (normAutofit) | 为每个文本形状添加 PowerPoint 原生 `normAutofit` 标记，实现溢出自动缩小字号；作为 POST_PROCESS 闭包在所有文本操作后执行 | `shapes.py`（新增 `_set_norm_autofit`）、`presentation.py` |
| 装饰形状自动背景化 | 识别无文本内容的 FreeShape（平行四边形、三角形、图标、连接线等），移到背景层（`shape_idx=-1`），确保渲染在文字下方；蓝色模板诊断出 55 个需移动的装饰形状 | `pptgen.py`（新增 `_hide_decorative_shapes`） |
| 建议字符数下限 | `suggested_characters` 设置 20 字符下限，防止模板短占位文本（如"标题"=2 字）误导 LLM 的容量预期 | `layout.py` |
| 图片水平居中 | 替换图片时新增水平居中计算，解决原有仅垂直居中的问题 | `apis.py` |
| 模板发现与加载优化 | 扩展 `list_templates()` 扫描路径（WORKSPACE + DP 缓存）；MCP 工具每次调用前热加载；`_initialized` 持久化防止 LLM 重选模板时选错 | `mcp_server.py` |
| 模板选择提示词优化 | 中英文提示词新增"必须使用用户指定的完全相同模板名"的强制指令 | `PPTAgent.yaml` |

## 目录结构

```
PPTAgent/
├── deeppresenter/
│   ├── server/                          # 模板 API 服务器（新增）
│   │   ├── app.py                       # FastAPI 入口
│   │   ├── routes/templates.py          # REST 端点（6 个）
│   │   ├── services/
│   │   │   ├── template_service.py      # 模板解析服务
│   │   │   └── template_registry.py     # 模板注册中心
│   │   └── models/templates.py          # 数据模型
│   ├── agents/                          # Agent 配置
│   │   └── env.py
│   ├── roles/PPTAgent.yaml              # PPTAgent 角色提示词
│   ├── utils/
│   │   ├── constants.py
│   │   └── typings.py
│   ├── mcp.json                         # MCP 配置
│   └── docker/SandBox.Dockerfile        # Docker 沙箱
├── pptagent/
│   ├── pptgen.py                        # PPT 生成核心
│   ├── induct.py                        # 模板诱导
│   ├── apis.py                          # 幻灯片编辑 API
│   ├── mcp_server.py                    # MCP 服务器（新增）
│   ├── multimodal.py                    # 图片标注
│   ├── model_utils.py                   # 模型工具
│   ├── llms.py                          # LLM 封装
│   ├── presentation/
│   │   ├── shapes.py                    # 形状解析
│   │   ├── presentation.py             # 演示文稿模型
│   │   └── layout.py                    # 布局模型
│   └── templates/                       # 内置模板
│       ├── default/
│       ├── blue-template/
│       ├── beamer/
│       ├── cip/
│       ├── hit/
│       ├── thu/
│       └── ucas/
├── webui.py                             # Web UI
├── README.md
└── pyproject.toml
```

## 模板目录约定

解析完成的模板存储在以下路径，系统自动扫描：

```
{WORKSPACE}/templates/<template_id>/
~/.cache/deeppresenter/workspace/templates_api/templates/<template_id>/

文件结构:
  manifest.json          # 模板元信息
  original.pptx          # 原始上传文件
  source.pptx            # 规范化后的 PPTX
  pptx.pptx              # 原始副本
  slide_induction.json   # 布局和内容模式
  image_stats.json       # 图片描述数据
  slide_images/          # 原始页渲染图
  template_images/       # 空布局页渲染图
  thumbnails/            # 缩略图
```

`manifest.json` 字段：`template_id`、`name`、`status`、`source_hash`、`aspect_ratio`、`slide_count`、`layout_count`、`thumbnail`、`created_at`、`error`
