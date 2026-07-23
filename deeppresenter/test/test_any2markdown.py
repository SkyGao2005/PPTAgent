from pathlib import Path

import pytest
from PIL import Image

from deeppresenter.tools import any2markdown


@pytest.mark.asyncio
async def test_mineru_images_remain_available_to_the_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "source.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    output_path = tmp_path / "parsed"

    async def fake_parse_pdf_online(
        _pdf_path: str,
        parsed_output: str,
        _token: str,
    ) -> None:
        parsed_root = Path(parsed_output)
        image_path = parsed_root / "images" / "chart.png"
        image_path.parent.mkdir(parents=True)
        Image.new("RGB", (640, 360), "white").save(image_path)
        (parsed_root / "content.md").write_text(
            "# Report\n\n![Revenue chart](images/chart.png)\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(any2markdown, "MINERU_API_KEY", "configured")
    monkeypatch.setattr(any2markdown, "MINERU_API_URL", None)
    monkeypatch.setattr(any2markdown, "parse_pdf_online", fake_parse_pdf_online)

    result = await any2markdown.convert_to_markdown.fn(
        file_path=str(pdf_path),
        output_folder=str(output_path),
    )

    markdown_path = Path(result["markdown_file"])
    image_path = (output_path / "images" / "chart.png").resolve()
    markdown = markdown_path.read_text(encoding="utf-8")
    assert f"![Revenue chart]({image_path})" in markdown
    assert f"- {image_path}: 640x360" in result["images"]
