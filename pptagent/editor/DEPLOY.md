# PPTAgent Editor 模块 — 部署手册

## 环境要求

| 项目 | 最低版本 |
|------|---------|
| 操作系统 | Linux (Ubuntu 20.04+) / macOS |
| Python | 3.11+ |
| Node.js | 18+（仅导出 PPTX 需要） |
| Docker | 24+（仅沙箱执行需要） |
| 磁盘空间 | 2 GB（含 Docker 镜像 + 依赖） |

## 安装步骤

### 1. 获取代码

```bash
git clone https://github.com/SkyGao2005/PPTAgent.git
cd PPTAgent
git checkout feat/slide-chat-editing
```

### 2. 创建虚拟环境

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e .
```

### 3. 安装外部依赖（按需）

```bash
# 仅导出 PPTX 需要
npm install --prefix deeppresenter/html2pptx

# 仅网页抓取 + PDF 转换需要
playwright install-deps && playwright install chromium

# 仅沙箱执行需要
docker pull forceless/deeppresenter-sandbox
docker tag forceless/deeppresenter-sandbox deeppresenter-sandbox
```

### 4. 配置 LLM API Key

```bash
cp deeppresenter/config.yaml.example deeppresenter/config.yaml
cp deeppresenter/mcp.json.example deeppresenter/mcp.json
```

编辑 `deeppresenter/config.yaml`，修改三个模型的 `api_key`：

```yaml
research_agent:
  base_url: "https://api.deepseek.com"
  model: "deepseek-chat"
  api_key: "sk-你的key"
```

### 5. 验证安装

```bash
PYTHONPATH=. python3 -c "from pptagent.editor import SlideEditor, SlideEditService; print('OK')"
```

## 启动服务

### WebUI 模式（开发调试）

```bash
python3 webui.py
# 浏览器打开 http://localhost:7861
```

### API 模式（前端对接）

在 `webui.py` 或独立的 `main.py` 中挂载 `edit_router`：

```python
from fastapi import FastAPI
from pptagent.editor.api import edit_router

app = FastAPI()
app.include_router(edit_router)
```

启动后可用端点：

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/tasks/{id}/slides/{id}/chat` | 发送自然语言编辑指令 |
| GET | `/api/tasks/{id}/slides/{id}/revisions` | 查看版本历史 |
| POST | `/api/tasks/{id}/slides/{id}/revisions/{n}/apply` | 切换到历史版本 |
| POST | `/api/tasks/{id}/slides/{id}/undo` | 撤销上一次编辑 |
| POST | `/api/tasks/{id}/slides/{id}/retry` | 重试上次失败操作 |
| GET | `/api/tasks/{id}/slides/{id}/events` | SSE 事件流 |

### 测试套件

```bash
# 基础功能集成测试（10项）
PYTHONPATH=. python3 pptagent/editor/test_integration.py

# 对话编辑方向 D 测试（6项，不需要 LLM Key）
PYTHONPATH=. python3 pptagent/editor/test_d_direction.py
```

## 常见问题

**Q: 导入报 `ModuleNotFoundError: No module named 'pptagent.editor'`**

A: 先执行 `uv pip install -e .` 注册包；或设置 `PYTHONPATH=.`。

**Q: `unoconvert/soffice is not installed`**

A: 这不影响核心功能，仅是 PPTX → 图片预览不可用。如需修复：`sudo apt install -y unoconv`。

**Q: 导出 PPTX 时报 `browserType.launch` 错误**

A: Node.js 版 Playwright 未安装浏览器：`cd deeppresenter/html2pptx && npx playwright install chromium`。

**Q: `fasttext-language-id` 模型下载失败（国内网络）**

A: 使用 ModelScope 预下载：
```bash
modelscope download forceless/fasttext-language-id
```
或手动将 `lid.176.bin` 复制到 `~/.cache/huggingface/hub/models--julien-c--fasttext-language-id/snapshots/master/`。

**Q: Docker 沙箱报连接失败**

A: 确认 Docker Desktop 已启动（Windows）或 `sudo service docker start`（Linux）。确保 `deeppresenter-sandbox` 镜像已 pull 并 tag。

**Q: 编辑操作无效果**

A: 确认 `config.yaml` 中 LLM API Key 有效。可用 `test_d_direction.py` 验证（不需要真实 LLM）。
