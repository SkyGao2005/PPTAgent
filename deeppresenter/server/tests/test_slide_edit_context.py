"""Slide edits must run under the same contract the page was generated with."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deeppresenter.server.services.design_context import (
    load_design_context,
    render_context_prompt,
)
from deeppresenter.server.services.slide_editing import HtmlSlideEditService

LAYOUT_CSS = """/* Layout scaffold for s007 (content/columns).
 * .tpl-* rules are the template's page chrome.
 */
:root{--template-accent1:#005EA4;--template-text1:#0D0D0D}
.slide .tpl-01{position:absolute;left:0;top:0;width:20%;height:6%;background:#005EA4} /* layout / band */
.slide .r-r001{position:absolute;left:5%;top:4%;width:60%;height:12%;background-image:url(../assets/logo.png)} /* logo / image -- painted from the template asset; emit an empty div */
.r-r002{position:absolute;left:5%;top:20%;width:80%;font-size:4vh;color:#0D0D0D} /* title / text */
"""

SLIDE_HTML = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="global.css">
<style>@import url('../template_context/refs/s007/layout.css');</style></head>
<body><div class="slide"><div class="tpl-01"></div><div class="r-r001"></div>
<h1 class="r-r002">优势学科</h1></div></body></html>"""

MANUSCRIPT = "# 第一页\n\n封面内容\n\n---\n\n# 优势学科不是孤岛\n\n八个双一流学科。\n"


def _workspace(root: Path) -> Path:
    slides = root / "slides"
    refs = root / "template_context" / "refs" / "s007"
    slides.mkdir(parents=True)
    refs.mkdir(parents=True)
    (refs / "layout.css").write_text(LAYOUT_CSS, encoding="utf-8")
    (slides / "global.css").write_text(".content{margin:0}", encoding="utf-8")
    (slides / "slide_02.html").write_text(SLIDE_HTML, encoding="utf-8")
    (root / "template_context" / "theme.css").write_text(
        ":root{--template-dk1:#000000;--template-accent1:#005EA4}", encoding="utf-8"
    )
    (root / "manuscript.md").write_text(MANUSCRIPT, encoding="utf-8")
    (root / "intermediate_output.json").write_text(
        json.dumps({"manuscript": str(root / "manuscript.md")}), encoding="utf-8"
    )
    (slides / "design_plan.json").write_text(
        json.dumps({"slides": [{"slide": 2, "reference_ids": ["s007"]}]}),
        encoding="utf-8",
    )
    return root


def test_context_reassembles_scaffold_manuscript_and_palette(tmp_path: Path) -> None:
    root = _workspace(tmp_path / "task")

    context = load_design_context(root, root / "slides" / "slide_02.html", 2)

    assert context.has_template
    # The scaffold is picked by the rules it declares, not by its filename,
    # so `global.css` never stands in for the template contract.
    assert context.scaffold_path.parent.name == "s007"
    assert "Layout scaffold for s007" in context.scaffold_css
    # Hard tier: chrome plus the region painted from a template asset. Only
    # the latter paints imagery; the band is a plain fill.
    assert context.hard_classes == ["tpl-01", "r-r001"]
    assert context.artwork_classes == ["r-r001"]
    assert context.palette["--template-accent1"] == "#005EA4"
    assert context.plan_entry["reference_ids"] == ["s007"]
    # Page 2 of the manuscript, not page 1.
    assert "优势学科不是孤岛" in context.manuscript
    assert "封面内容" not in context.manuscript

    prompt = render_context_prompt(context, "把标题改短")
    for expected in ("layout.css", "--template-accent1", "优势学科不是孤岛", ".tpl-01"):
        assert expected in prompt


def test_context_without_a_template_stays_empty(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    (root / "slides").mkdir(parents=True)
    page = root / "slides" / "slide_01.html"
    page.write_text("<html><body><h1>标题</h1></body></html>", encoding="utf-8")

    context = load_design_context(root, page, 1)

    assert not context.has_template
    assert context.hard_classes == []
    assert context.manuscript is None
    assert context.design_system_css is None


def _free_workspace(root: Path) -> Path:
    """A freely generated deck: `global.css` is the whole design system."""

    slides = root / "slides"
    slides.mkdir(parents=True)
    (slides / "global.css").write_text(
        ":root{--ink:#1F2933}\n.card{border-radius:8px;padding:24px}", encoding="utf-8"
    )
    (slides / "slide_02.html").write_text(
        '<!doctype html><html><head><link rel="stylesheet" href="global.css">'
        '</head><body><div class="slide"><h1>优势学科</h1>'
        '<div class="card"><p>正文</p></div></div></body></html>',
        encoding="utf-8",
    )
    (root / "manuscript.md").write_text(MANUSCRIPT, encoding="utf-8")
    (root / "intermediate_output.json").write_text(
        json.dumps({"manuscript": str(root / "manuscript.md")}), encoding="utf-8"
    )
    return root


def test_free_generation_gets_its_design_system(tmp_path: Path) -> None:
    """Without a template, `global.css` is what every other page obeys."""

    root = _free_workspace(tmp_path / "free")

    context = load_design_context(root, root / "slides" / "slide_02.html", 2)

    assert not context.has_template
    assert context.design_system_css is not None
    assert ".card{border-radius:8px" in context.design_system_css
    assert context.stylesheet_refs == ["global.css"]

    prompt = render_context_prompt(context, "把标题改短")
    # The deck's actual rules reach the model, not merely the <link> tag.
    assert "--ink:#1F2933" in prompt
    assert "优势学科不是孤岛" in prompt


class _StubLLM:
    """Returns one canned reply in the role file's output format."""

    def __init__(self, html: str, summary: str = "已修改") -> None:
        self.html = html
        self.summary = summary
        self.prompts: list[str] = []

    async def run(self, messages, **_):
        self.prompts.append(messages[-1]["content"])

        class _Message:
            content = f"SUMMARY: {self.summary}\n```html\n{self.html}\n```"

        class _Choice:
            message = _Message()

        class _Response:
            choices = [_Choice()]

        return _Response()


class _Preview:
    """Minimal PreviewService stand-in that records what it rendered."""

    def __init__(self, root: Path, source_rel: str) -> None:
        self.root = root
        self.artifact = type(
            "Artifact",
            (),
            {
                "mode": "html",
                "index": 2,
                "revision": 1,
                "source_path": source_rel,
                "preview_path": "slides/s/revisions/1/preview.png",
                "updated_at": "now",
            },
        )()
        self.rendered: list[Path] = []

    def get_slide(self, *_):
        return self.artifact

    async def render_html_slide(self, _task, html_path, *, revision=None, **__):
        self.rendered.append(Path(html_path))
        self.artifact.revision = revision or 1
        self.artifact.preview_path = f"slides/s/revisions/{self.artifact.revision}/preview.png"
        return self.artifact


def _service(root: Path, llm) -> HtmlSlideEditService:
    return HtmlSlideEditService(
        root.parent, _Preview(root, "slides/slide_02.html"), root.name, "sld", llm=llm
    )


@pytest.mark.asyncio
async def test_edit_writes_back_to_the_canonical_page(tmp_path: Path) -> None:
    """A revision rendered from a nested copy loses every relative reference."""

    root = _workspace(tmp_path / "task")
    edited = SLIDE_HTML.replace("优势学科", "完整工程链")
    service = _service(root, _StubLLM(edited))

    result = await service.chat("把标题改成完整工程链")

    assert result.status == "success"
    assert result.revision == 2
    canonical = root / "slides" / "slide_02.html"
    # The page itself changed, so the export re-renders the edit too.
    assert "完整工程链" in canonical.read_text(encoding="utf-8")
    # ...and it is the canonical path that was rendered, beside its CSS.
    assert service.preview_service.rendered == [canonical]
    assert (root / "slides" / "sld" / "revisions" / "1" / "source.html").exists()

    # The model was handed the scaffold and the manuscript, not just the HTML.
    prompt = service.llm.prompts[0]
    assert "Layout scaffold for s007" in prompt and "优势学科不是孤岛" in prompt


@pytest.mark.asyncio
async def test_edit_that_drops_template_chrome_is_rolled_back(tmp_path: Path) -> None:
    root = _workspace(tmp_path / "task")
    without_chrome = SLIDE_HTML.replace('<div class="tpl-01"></div>', "")
    service = _service(root, _StubLLM(without_chrome))

    result = await service.chat("把左上角那个蓝条去掉")

    assert result.status == "failed"
    assert result.error == "template_contract_violation"
    assert ".tpl-01" in result.message
    canonical = (root / "slides" / "slide_02.html").read_text(encoding="utf-8")
    assert '<div class="tpl-01"></div>' in canonical
    assert not (root / "slides" / "sld" / "revisions" / "2" / "source.html").exists()


@pytest.mark.asyncio
async def test_edit_that_invents_a_colour_is_rolled_back(tmp_path: Path) -> None:
    root = _workspace(tmp_path / "task")
    recoloured = SLIDE_HTML.replace("<h1 ", '<h1 style="color:#dbeafe" ')
    service = _service(root, _StubLLM(recoloured))

    result = await service.chat("把标题换成蓝色科技风")

    assert result.status == "failed"
    assert "#dbeafe" in result.message
    assert "#dbeafe" not in (root / "slides" / "slide_02.html").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_undo_restores_the_snapshot_into_the_canonical_page(tmp_path: Path) -> None:
    root = _workspace(tmp_path / "task")
    service = _service(root, _StubLLM(SLIDE_HTML.replace("优势学科", "完整工程链")))
    await service.chat("改标题")

    result = await service.undo()

    assert result.status == "success"
    assert result.revision == 1
    assert "优势学科" in (root / "slides" / "slide_02.html").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_free_generation_edits_apply_without_template_checks(tmp_path: Path) -> None:
    """A new colour is drift, not a contract breach, when there is no palette."""

    root = _free_workspace(tmp_path / "free")
    page = root / "slides" / "slide_02.html"
    edited = page.read_text(encoding="utf-8").replace(
        "<h1>优势学科</h1>", '<h1 style="color:#C0392B">完整工程链</h1>'
    )
    service = HtmlSlideEditService(
        root.parent, _Preview(root, "slides/slide_02.html"), root.name, "sld",
        llm=_StubLLM(edited),
    )

    result = await service.chat("把标题改成完整工程链并强调一下")

    assert result.status == "success"
    assert "完整工程链" in page.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_dropping_a_stylesheet_reference_is_rolled_back(tmp_path: Path) -> None:
    """Markup survives while the styling vanishes, so classes alone miss it."""

    root = _free_workspace(tmp_path / "free")
    page = root / "slides" / "slide_02.html"
    stripped = page.read_text(encoding="utf-8").replace(
        '<link rel="stylesheet" href="global.css">', ""
    )
    service = HtmlSlideEditService(
        root.parent, _Preview(root, "slides/slide_02.html"), root.name, "sld",
        llm=_StubLLM(stripped),
    )

    result = await service.chat("把这一页的样式清理一下")

    assert result.status == "failed"
    assert "global.css" in result.message
    assert 'href="global.css"' in page.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_dropping_the_template_import_is_rolled_back(tmp_path: Path) -> None:
    """The `.tpl-*` divs still parse, so only the reference check catches this."""

    root = _workspace(tmp_path / "task")
    page = root / "slides" / "slide_02.html"
    stripped = page.read_text(encoding="utf-8").replace(
        "@import url('../template_context/refs/s007/layout.css');", ""
    )
    service = _service(root, _StubLLM(stripped))

    result = await service.chat("去掉多余的样式引用")

    assert result.status == "failed"
    assert "layout.css" in result.message
    assert "layout.css" in page.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_without_a_model_only_literal_text_edits_apply(tmp_path: Path) -> None:
    """The old fallbacks injected an off-template palette and a floating note."""

    root = _workspace(tmp_path / "task")
    service = _service(root, None)

    result = await service.chat("把配色换成蓝色科技风")

    assert result.status == "no_change"
    page = (root / "slides" / "slide_02.html").read_text(encoding="utf-8")
    assert "c2-html-edit-style" not in page and "c2-edit-note" not in page
