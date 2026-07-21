"""Generate project documentation Word file."""
from docx import Document
from docx.shared import Inches, Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.style import WD_STYLE_TYPE
from datetime import datetime

doc = Document()

# ── Styles ──
style = doc.styles['Normal']
style.font.size = Pt(11)
style.font.name = 'Microsoft YaHei'
style.paragraph_format.space_after = Pt(6)
style.paragraph_format.line_spacing = 1.3

for level in range(1, 4):
    heading_style = doc.styles[f'Heading {level}']
    heading_style.font.name = 'Microsoft YaHei'

# ── Title Page ──
doc.add_paragraph()
doc.add_paragraph()
title = doc.add_paragraph()
title.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = title.add_run('PPTAgent 二次开发项目说明')
run.font.size = Pt(26)
run.font.bold = True
run.font.color.rgb = RGBColor(0x1A, 0x56, 0xDB)

subtitle = doc.add_paragraph()
subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = subtitle.add_run('模板上传解析与模板化 PPT 生成系统')
run.font.size = Pt(14)
run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)

doc.add_paragraph()
info = doc.add_paragraph()
info.alignment = WD_ALIGN_PARAGRAPH.CENTER
info.add_run(f'文档版本: 1.0').font.size = Pt(10)
info.add_run(f'\n日期: {datetime.now().strftime("%Y-%m-%d")}').font.size = Pt(10)
info.add_run('\n基于原项目: https://github.com/icip-cas/PPTAgent').font.size = Pt(10)

doc.add_page_break()

# ── Table of Contents ──
doc.add_heading('目录', level=1)
toc_items = [
    '1. 项目概述',
    '2. 相较于原项目的改进',
    '3. 新增 API 接口',
    '4. 系统架构',
    '5. 环境依赖',
    '6. 启动方式',
    '7. 关键 Bug 修复记录',
    '8. 目录结构',
]
for item in toc_items:
    p = doc.add_paragraph(item)
    p.paragraph_format.space_after = Pt(2)

doc.add_page_break()

# ── 1. 项目概述 ──
doc.add_heading('1. 项目概述', level=1)
doc.add_paragraph(
    '本项目基于开源项目 PPTAgent (https://github.com/icip-cas/PPTAgent) 进行二次开发，'
    '在原有"自由生成"模式基础上新增了模板化 PPT 生成能力。核心新增功能包括：'
)
bullets = [
    '模板上传与自动解析 API：用户上传自定义 PPTX 模板，系统自动进行布局聚类、内容模式提取，生成可复用的模板描述文件（slide_induction.json）。',
    '模板注册中心（TemplateRegistry）：支持模板热加载、去重检测、状态管理和持久化缓存。',
    'SSE 实时进度推送：模板解析过程通过 Server-Sent Events 向前端推送进度，支持 7 个阶段的进度展示。',
    '模板化 PPT 生成：在 Web UI 中选择已解析的模板，LLM 根据模板的布局和内容模式生成符合模板风格的幻灯片。',
    'MCP 服务器集成：通过 PPTAgent MCP Server 提供 set_template、create_slide 等工具，LLM Agent 可交互式调用完成幻灯片生成。',
]
for b in bullets:
    doc.add_paragraph(b, style='List Bullet')

# ── 2. 相较于原项目的改进 ──
doc.add_heading('2. 相较于原项目的改进', level=1)

doc.add_heading('2.1 新增功能模块', level=2)

improvements = [
    ('模板管理 API 服务器', 'deeppresenter/server/app.py',
     'FastAPI 服务，提供 6 个 REST 端点 + 1 个健康检查端点，支持模板上传、列表查询、详情获取、SSE 进度订阅、失败重试和删除。'),
    ('模板解析服务', 'deeppresenter/server/services/template_service.py',
     '将原项目离线命令行脚本重构为可调用服务，包含 7 阶段解析流程：文件校验→规范化→页面渲染→图片标注→布局聚类→内容模式提取→缩略图生成。支持超时保护、失败清理、进度回调。'),
    ('模板注册中心', 'deeppresenter/server/services/template_registry.py',
     '线程安全的模板注册表，支持 SHA256 哈希去重、清单持久化（manifest.json）、模板数据缓存、服务重启后自动恢复。'),
    ('MCP 服务器', 'pptagent/mcp_server.py',
     '基于 FastMCP 实现，提供 list_templates、set_template、create_slide、write_slide、generate_slide、save_generated_slides、reload_templates 等工具，支持模板热加载。'),
    ('Web UI 模板选择', 'webui.py',
     '在 Gradio 界面中新增模板下拉框，支持"自由生成"和"模板"两种模式切换，模板模式下可选择已解析的模板进行生成。'),
    ('Docker 沙箱镜像', 'deeppresenter/docker/SandBox.Dockerfile',
     '构建包含 Node.js、Playwright、LibreOffice 的沙箱镜像，适配中国网络环境（阿里云/清华镜像源）。'),
]

for name, location, desc in improvements:
    p = doc.add_paragraph()
    run = p.add_run(f'{name}')
    run.font.bold = True
    p.add_run(f'\n位置: {location}')
    p.add_run(f'\n说明: {desc}')

doc.add_heading('2.2 Bug 修复与兼容性改进', level=2)

fixes = [
    ('set_reference() 崩溃', 'pptagent/pptgen.py',
     'dict comprehension 遍历 slide_induction 所有 key，将 "functional_keys"（list）和 "language"（dict）错误地作为 Layout 构造参数，导致所有模板加载失败。修复：添加 key 过滤 if k not in ("functional_keys", "language")。'),
    ('_hide_small_pics() None caption', 'pptagent/pptgen.py',
     '装饰性图片标注失败时 caption 为 None，调用 layout.remove_item(None) 崩溃。修复：跳过 caption 为 None 的图片。'),
    ('image_stats 空字典', 'pptagent/multimodal.py',
     '图片标注失败后 image_stats.json 保存为空 {}，加载模板时无法恢复 caption。修复：apply_stats() 添加 semantic_name 回退逻辑。'),
    ('image_stats 序列化', 'deeppresenter/server/services/template_service.py',
     'finally 块中的 fallback caption 只写入内存对象，未同步回 image_labler.image_stats。修复：同步 caption 到 image_stats 后再序列化。'),
    ('占位符语义名丢失', 'pptagent/presentation/shapes.py',
     '所有 Placeholder 形状的 semantic_name 统一设为 "placeholder"，丢弃了 PPTX 自带的 TITLE/SUBTITLE/BODY 等标记。修复：优先读取 shape.placeholder_format.type.name。'),
    ('Qwen/DeepSeek 结构化输出', 'pptagent/llms.py',
     '非 OpenAI 模型不支持 parse() 方法。修复：捕获异常后使用 create() + 内联 JSON Schema 指令。'),
    ('Python 3.13 ExceptionGroup', 'deeppresenter/server/services/template_service.py',
     'ExceptionGroup 不继承 Exception，导致 TaskGroup 异常被漏过。修复：except Exception → except BaseException。'),
    ('aiometer 协程兼容', 'pptagent/induct.py',
     'aiometer.run_all 需要 callable 而非 coroutine。修复：使用 functools.partial 包装。'),
    ('幻灯片数量校验', 'pptagent/induct.py',
     'PPT 渲染图片数与幻灯片数不一致时断言失败。修复：添加 use_assert=False 选项。'),
    ('本地模型加载', 'pptagent/model_utils.py',
     'HuggingFace 下载不可达。修复：get_image_model() 添加 model_base 参数支持本地路径。'),
]

for name, location, desc in fixes:
    p = doc.add_paragraph()
    run = p.add_run(f'{name}')
    run.font.bold = True
    p.add_run(f'\n位置: {location}')
    p.add_run(f'\n说明: {desc}')

# ── 3. 新增 API 接口 ──
doc.add_heading('3. 新增 API 接口', level=1)

doc.add_paragraph(
    '模板管理 API 服务器默认运行在端口 8080，提供以下 7 个端点：'
)

# API table
table = doc.add_table(rows=8, cols=4)
table.style = 'Light Grid Accent 1'

headers = ['方法', '路径', '功能', '说明']
for i, h in enumerate(headers):
    cell = table.rows[0].cells[i]
    cell.text = h
    for paragraph in cell.paragraphs:
        for run in paragraph.runs:
            run.font.bold = True

api_data = [
    ['POST', '/api/templates', '上传模板', '接收 .pptx 文件，SHA256 去重，创建解析任务'],
    ['GET', '/api/templates', '模板列表', '返回所有模板的状态、页数、宽高比等摘要信息'],
    ['GET', '/api/templates/{id}', '模板详情', '返回指定模板的完整清单（元素列表、布局数等）'],
    ['GET', '/api/templates/{id}/events', 'SSE 进度', 'Server-Sent Events 实时推送解析进度'],
    ['POST', '/api/templates/{id}/retry', '重试解析', '对失败状态的模板重新触发解析'],
    ['DELETE', '/api/templates/{id}', '删除模板', '删除用户模板及关联缓存文件'],
    ['GET', '/health', '健康检查', '返回服务状态和模板统计（总数/就绪/解析中/失败）'],
]

for i, row_data in enumerate(api_data):
    for j, cell_text in enumerate(row_data):
        table.rows[i + 1].cells[j].text = cell_text

doc.add_paragraph()
doc.add_heading('3.1 SSE 进度事件', level=2)
doc.add_paragraph('解析过程通过 SSE 推送以下事件类型：')

events = [
    'template.parse_started — 解析开始',
    'template.parse_progress — 阶段进度（包含 stage 和 progress 字段）',
    'template.ready — 解析完成，模板可用',
    'template.failed — 解析失败（包含 error 字段）',
]
for e in events:
    doc.add_paragraph(e, style='List Bullet')

doc.add_paragraph()
doc.add_paragraph('进度阶段: file_validated (5%) → normalizing (10%) → structure_parsed (20%) → slides_rendered (35%) → layout_clustered (55%) → schema_extracted (80%) → completed (100%)')

doc.add_heading('3.2 模板状态', level=2)
states = [
    'parsing — 解析进行中',
    'ready — 解析完成，可用于生成',
    'failed — 解析失败（可重试）',
]
for s in states:
    doc.add_paragraph(s, style='List Bullet')

# ── 4. 系统架构 ──
doc.add_heading('4. 系统架构', level=1)

doc.add_paragraph(
    '系统由三个独立进程组成，通过 MCP 协议和 REST API 通信：'
)

doc.add_heading('4.1 进程架构', level=2)
arch_items = [
    '模板 API 服务器（端口 8080）: FastAPI 应用，管理模板上传、解析和查询。内部使用 TemplateRegistry 存储清单，TemplateInductionService 执行解析。',
    'Web UI（端口 7861）: Gradio 应用，提供用户交互界面。选择模板模式时通过 MCP 协议与 PPTAgent MCP Server 通信。',
    'PPTAgent MCP Server: FastMCP 服务，提供幻灯片生成工具。LLM Agent 通过工具调用完成模板选择→布局选择→内容生成→幻灯片编辑→保存的完整流程。',
]
for item in arch_items:
    doc.add_paragraph(item, style='List Bullet')

doc.add_heading('4.2 模板解析流程', level=2)
doc.add_paragraph('模板解析（TemplateInductionService）分为 7 个阶段：')
stages = [
    '文件校验 — 检查 ZIP 完整性、加密状态、幻灯片数量',
    'PPT 规范化 — 复制 original.pptx → source.pptx，创建 Presentation 对象',
    '页面渲染 — 渲染原始页（slide_images/）和空布局页（template_images/）',
    '图片标注 — 使用视觉模型为图片生成描述（失败时回退到 semantic_name）',
    '布局聚类 — 使用 ViT 模型提取图像嵌入，余弦相似度聚类相似布局',
    '内容模式提取 — LLM 分析每类布局的元素组成（名称、类型、建议字数）',
    '缩略图生成 — 取第一页生成 320×240 缩略图',
]
for i, s in enumerate(stages):
    doc.add_paragraph(f'{i+1}. {s}')

doc.add_heading('4.3 模板化生成流程', level=2)
doc.add_paragraph('用户选择模板后，PPT 生成流程：')
gen_steps = [
    'LLM 调用 list_templates 查看可用模板',
    'LLM 调用 set_template("template-name") 加载模板数据（slide_induction + source.pptx）',
    'PPTGen.set_reference() 将模板布局转换为 Layout 对象，deepcopy source.pptx 作为画布',
    'LLM 调用 create_slide(layout) 选择布局 → write_slide(elements) 填充内容 → generate_slide() 生成幻灯片',
    '每张幻灯片通过 deepcopy 模板页 + 替换文字/图片实现，保留模板背景、配色和装饰',
    '最后调用 save_generated_slides(path) 保存完整 PPTX',
]
for i, s in enumerate(gen_steps):
    doc.add_paragraph(f'{i+1}. {s}')

# ── 5. 环境依赖 ──
doc.add_heading('5. 环境依赖', level=1)

doc.add_heading('5.1 运行环境', level=2)
env_items = [
    '操作系统: Linux (推荐 WSL2 Ubuntu)',
    'Python: >= 3.11 (推荐 3.13)',
    'Docker: 用于沙箱容器（可选，模板化生成需要）',
    'LibreOffice: 用于 .ppt → .pptx 转换（可选）',
    'Playwright: 用于 HTML → PPTX 转换',
]
for item in env_items:
    doc.add_paragraph(item, style='List Bullet')

doc.add_heading('5.2 核心 Python 依赖', level=2)
core_deps = [
    ('fastapi[all]', '模板 API 服务器'),
    ('uvicorn', 'ASGI 服务器'),
    ('gradio>=5.47.2', 'Web UI'),
    ('fastmcp>=2.10.0', 'MCP 服务器框架'),
    ('openai>=1.108.2', 'LLM API 调用'),
    ('python-pptx>=0.6.21', 'PPTX 文件操作'),
    ('pptagent-pptx>=0.0.1', 'PPTX 增强解析'),
    ('transformers<4.50.0', 'ViT 图像模型（布局聚类）'),
    ('torch', '深度学习框架'),
    ('torchvision', '图像预处理'),
    ('pillow', '图像处理'),
    ('playwright>=1.55.0', '浏览器自动化'),
    ('docker>=7.1.0', 'Docker SDK'),
    ('aiohttp>=3.9.0', '异步 HTTP 客户端'),
    ('aiometer', '异步并发控制'),
    ('fasttext>=0.9.3', '语言检测'),
    ('modelscope>=1.35.1', '模型下载镜像'),
]
for name, purpose in core_deps:
    doc.add_paragraph(f'{name} — {purpose}', style='List Bullet')

doc.add_heading('5.3 LLM API 配置', level=2)
doc.add_paragraph('需要配置 config.yaml 文件，至少包含 research_agent 端点：')
doc.add_paragraph(
    'research_agent:\n'
    '  model: "qwen3.6-plus-2026-04-02"  # 或其他兼容 OpenAI API 的模型\n'
    '  base_url: "https://your-api-endpoint/v1"\n'
    '  api_key: "your-api-key"'
)

doc.add_heading('5.4 安装步骤', level=2)
steps = [
    '克隆/复制项目到本地',
    '创建虚拟环境: python3 -m venv .venv && source .venv/bin/activate',
    '安装依赖: uv pip install -e ".[full]"  # 或 pip install -e ".[full]"',
    '安装 Playwright: playwright install chromium',
    '构建 Docker 沙箱（可选）: docker build -f deeppresenter/docker/SandBox.Dockerfile -t deeppresenter-sandbox .',
    '配置 LLM: 编辑 deeppresenter/config.yaml 填入 API 信息',
    '配置 MCP: cp deeppresenter/mcp.json.example deeppresenter/mcp.json',
    '下载 ViT 模型（可选，用于布局聚类）: 从 HuggingFace 下载 google/vit-base-patch16-224-in21k 到 ~/.cache/huggingface/',
]
for i, s in enumerate(steps):
    doc.add_paragraph(f'{i+1}. {s}')

# ── 6. 启动方式 ──
doc.add_heading('6. 启动方式', level=1)

doc.add_heading('6.1 启动 Web UI（模板化生成）', level=2)
doc.add_paragraph('cd <项目目录>\nsource .venv/bin/activate\npython webui.py\n# 访问 http://localhost:7861')
doc.add_paragraph('选择输出类型为"模版 (templates)"，然后选择模板，上传 PDF/文档，点击发送。')

doc.add_heading('6.2 启动模板 API 服务器（模板管理）', level=2)
doc.add_paragraph('cd <项目目录>\nsource .venv/bin/activate\npython -m deeppresenter.server.app --port 8080\n# API 文档: http://localhost:8080/docs\n# 健康检查: http://localhost:8080/health')

doc.add_heading('6.3 上传模板', level=2)
doc.add_paragraph('curl -X POST http://localhost:8080/api/templates -F "file=@template.pptx"')
doc.add_paragraph('上传后通过 GET /api/templates/{id}/events 订阅 SSE 进度，等待 status 变为 ready。')

doc.add_heading('6.4 模板加载到生成系统', level=2)
doc.add_paragraph(
    '模板 API 解析完成后，模板存储在 WORKSPACE_BASE/templates_api/templates/<id>/。'
    '需要将模板目录复制或软链接到 pptagent/templates/<template-name>/ 才能在 Web UI 中使用。'
    '或者调用 MCP 工具 reload_templates 热加载。'
)

# ── 7. 关键 Bug 修复记录 ──
doc.add_heading('7. 关键 Bug 修复记录', level=1)

doc.add_paragraph('以下 Bug 在二次开发过程中发现并修复，均影响系统的核心功能。')

bug_table = doc.add_table(rows=11, cols=4)
bug_table.style = 'Light Grid Accent 1'
bug_headers = ['Bug', '影响', '根因', '修复']
for i, h in enumerate(bug_headers):
    bug_table.rows[0].cells[i].text = h
    for p in bug_table.rows[0].cells[i].paragraphs:
        for r in p.runs:
            r.font.bold = True

bugs = [
    ['set_reference() 所有模板崩溃', 'set_template 100% 失败，PPT 无法使用模板', 'dict comprehension 对 functional_keys(list) 执行 ** 解包', '添加 if k not in ("functional_keys", "language") 过滤'],
    ['_hide_small_pics() None caption', 'blue-template 等含装饰图的模板加载失败', 'image_stats 为空 → caption=None → remove_item(None)', '跳过 caption 为 None 的图片'],
    ['image_stats 空序列化', '模板重新加载后所有图片 caption 丢失', 'finally 块 fallback caption 未同步回 image_labler', '序列化前同步 caption 到 image_stats'],
    ['apply_stats 无回退', '空 image_stats 导致所有 shape.caption 为 None', 'apply_stats({}) 时 img_key 不在空 dict 中', '添加 semantic_name 回退逻辑'],
    ['占位符语义名丢失', '所有 Placeholder 形状显示为 "placeholder"', 'auto_shape_type 为 None → except 分支统一赋值', '优先读取 shape.placeholder_format.type.name'],
    ['Qwen 结构化输出失败', '非 OpenAI 模型调用 parse() 报错', 'Qwen/DeepSeek 不支持 response_format 参数', '捕获异常→重建 client→内联 JSON Schema'],
    ['Python 3.13 ExceptionGroup', 'TaskGroup 异常未被捕获', 'ExceptionGroup 不是 Exception 子类', '改用 BaseException 捕获'],
    ['aiometer 协程错误', 'layout_induct 崩溃', 'run_all 需要 callable 而非 coroutine', '使用 functools.partial 包装'],
    ['HuggingFace 下载不可达', 'ViT 模型加载失败', '中国网络无法访问 huggingface.co', '支持本地模型路径（model_base 参数）'],
    ['Docker 构建网络超时', '沙箱镜像构建失败', 'apt/npm/Playwright 默认源不可达', '添加阿里云/清华镜像源 + 重试逻辑'],
]

for i, row_data in enumerate(bugs):
    for j, cell_text in enumerate(row_data):
        bug_table.rows[i + 1].cells[j].text = cell_text

# ── 8. 目录结构 ──
doc.add_heading('8. 目录结构', level=1)

doc.add_paragraph('以下列出新增和修改的关键文件：')

structure = [
    ('deeppresenter/server/', '模板 API 服务器（新增）'),
    ('deeppresenter/server/app.py', 'FastAPI 入口'),
    ('deeppresenter/server/routes/templates.py', '6 个 REST 端点'),
    ('deeppresenter/server/services/template_service.py', '模板解析服务'),
    ('deeppresenter/server/services/template_registry.py', '模板注册中心'),
    ('deeppresenter/server/models/templates.py', '数据模型'),
    ('deeppresenter/roles/PPTAgent.yaml', 'PPTAgent 角色配置（修改）'),
    ('deeppresenter/docker/SandBox.Dockerfile', 'Docker 沙箱（修改）'),
    ('pptagent/mcp_server.py', 'MCP 服务器（新增）'),
    ('pptagent/pptgen.py', 'PPT 生成核心（修改）'),
    ('pptagent/induct.py', '模板诱导（修改）'),
    ('pptagent/llms.py', 'LLM 封装（修改）'),
    ('pptagent/multimodal.py', '图片标注（修改）'),
    ('pptagent/model_utils.py', '模型工具（修改）'),
    ('pptagent/presentation/shapes.py', '形状解析（修改）'),
    ('pptagent/presentation/presentation.py', '演示文稿模型（修改）'),
    ('pptagent/templates/blue-template/', '蓝色模板示例（新增）'),
    ('webui.py', 'Web UI（修改）'),
]

struct_table = doc.add_table(rows=len(structure) + 1, cols=2)
struct_table.style = 'Light Grid Accent 1'
struct_table.rows[0].cells[0].text = '文件/目录'
struct_table.rows[0].cells[1].text = '说明'
for p in struct_table.rows[0].cells[0].paragraphs:
    for r in p.runs:
        r.font.bold = True
for p in struct_table.rows[0].cells[1].paragraphs:
    for r in p.runs:
        r.font.bold = True

for i, (path, desc) in enumerate(structure):
    struct_table.rows[i + 1].cells[0].text = path
    struct_table.rows[i + 1].cells[1].text = desc

# ── Save ──
output_path = '/mnt/c/Users/Yang/Desktop/PPTAgent-feat-template-upload-parsing/PPTAgent二次开发项目说明.docx'
doc.save(output_path)
print(f'Document saved to: {output_path}')
