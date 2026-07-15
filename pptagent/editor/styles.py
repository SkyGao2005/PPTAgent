"""Predefined style strategies for single-slide restyling.

Each strategy defines a color palette, font preferences, and
optional accent rules that can be applied to any slide.
"""

from dataclasses import dataclass, field


@dataclass
class ColorPalette:
    """A set of semantic colors for a slide."""
    primary: str       # main title / emphasis colour (hex, no #)
    secondary: str     # body text colour
    background: str    # slide background
    accent: str        # highlights / borders / icons
    title_color: str | None = None    # override for title specifically
    body_color: str | None = None     # override for body text specifically

    def to_dict(self) -> dict[str, str]:
        d = {
            "primary": self.primary,
            "secondary": self.secondary,
            "background": self.background,
            "accent": self.accent,
        }
        if self.title_color:
            d["title_color"] = self.title_color
        if self.body_color:
            d["body_color"] = self.body_color
        return d


@dataclass
class FontPrefs:
    """Font preferences for a style."""
    title_family: str | None = None
    title_size: int | None = None
    body_family: str | None = None
    body_size: int | None = None
    bold_titles: bool | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class StyleStrategy:
    """A named visual style that can be applied to slides."""
    name: str
    description: str
    palette: ColorPalette
    fonts: FontPrefs = field(default_factory=FontPrefs)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "palette": self.palette.to_dict(),
            "fonts": self.fonts.to_dict(),
        }


# ── built-in strategies ──────────────────────────────────────

BUILTIN_STRATEGIES = {
    "简约": StyleStrategy(
        name="简约",
        description="极简白底黑字风格，适合日常汇报",
        palette=ColorPalette(
            primary="333333",
            secondary="666666",
            background="FFFFFF",
            accent="999999",
            title_color="222222",
            body_color="555555",
        ),
        fonts=FontPrefs(
            title_family="Microsoft YaHei",
            title_size=36,
            body_family="Microsoft YaHei",
            body_size=18,
            bold_titles=True,
        ),
    ),
    "商务": StyleStrategy(
        name="商务",
        description="深蓝主调专业风格，适合企业汇报",
        palette=ColorPalette(
            primary="1A3C6D",
            secondary="4A4A4A",
            background="FAFAFA",
            accent="2B6CB0",
            title_color="1A3C6D",
            body_color="333333",
        ),
        fonts=FontPrefs(
            title_family="Arial",
            title_size=40,
            body_family="Arial",
            body_size=18,
            bold_titles=True,
        ),
    ),
    "科技": StyleStrategy(
        name="科技",
        description="深色背景 + 霓虹青配色，适合技术分享",
        palette=ColorPalette(
            primary="00D4AA",
            secondary="B0BEC5",
            background="0D1117",
            accent="58A6FF",
            title_color="00D4AA",
            body_color="C9D1D9",
        ),
        fonts=FontPrefs(
            title_family="Consolas",
            title_size=38,
            body_family="Consolas",
            body_size=16,
            bold_titles=True,
        ),
    ),
    "政务": StyleStrategy(
        name="政务",
        description="红色主调庄重风格，适合政府/党建汇报",
        palette=ColorPalette(
            primary="C41E3A",
            secondary="333333",
            background="FFFFFF",
            accent="DAA520",
            title_color="C41E3A",
            body_color="333333",
        ),
        fonts=FontPrefs(
            title_family="SimSun",
            title_size=36,
            body_family="SimSun",
            body_size=18,
            bold_titles=True,
        ),
    ),
}


class StyleRegistry:
    """Central registry for style strategies.

    Usage::

        registry = StyleRegistry()
        strategy = registry.get("科技")
        editor.restyle(strategy)
    """

    def __init__(self):
        self._strategies: dict[str, StyleStrategy] = dict(BUILTIN_STRATEGIES)

    def register(self, strategy: StyleStrategy) -> None:
        self._strategies[strategy.name] = strategy

    def get(self, name: str) -> StyleStrategy:
        if name not in self._strategies:
            available = ", ".join(self._strategies.keys())
            raise KeyError(f"未找到风格「{name}」，可用: {available}")
        return self._strategies[name]

    def list_names(self) -> list[str]:
        return list(self._strategies.keys())

    def list_all(self) -> list[StyleStrategy]:
        return list(self._strategies.values())

    def to_dict(self) -> dict:
        return {k: v.to_dict() for k, v in self._strategies.items()}
