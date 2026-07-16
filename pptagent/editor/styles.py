"""Predefined style strategies for single-slide restyling.

Each strategy defines a color palette, font preferences, and
optional accent rules that can be applied to any slide.
"""

from dataclasses import dataclass, field
from pathlib import Path


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

    Supports built-in styles, custom user-defined styles,
    and file-based persistence.

    Usage::

        registry = StyleRegistry()
        strategy = registry.get("科技")
        editor.restyle(strategy)

        # Custom style
        custom = registry.create("我的风格",
            primary="FF5722", secondary="333333",
            background="FAFAFA", accent="FF9800",
            title_family="Arial", body_family="Arial",
            description="自定义橙色主题")
        registry.save_to_file("~/.pptagent/styles.json")
    """

    # Track built-in names so we don't accidentally delete them
    _BUILTIN_NAMES = frozenset(BUILTIN_STRATEGIES.keys())

    def __init__(self):
        self._strategies: dict[str, StyleStrategy] = dict(BUILTIN_STRATEGIES)

    # ── crud ──────────────────────────────────────────────────

    def register(self, strategy: StyleStrategy) -> None:
        """Register a strategy (overwrites if same name)."""
        self._strategies[strategy.name] = strategy

    def get(self, name: str) -> StyleStrategy:
        if name not in self._strategies:
            available = ", ".join(self._strategies.keys())
            raise KeyError(f"未找到风格「{name}」，可用: {available}")
        return self._strategies[name]

    def delete(self, name: str) -> bool:
        """Remove a custom style. Built-in styles cannot be deleted."""
        if name in self._BUILTIN_NAMES:
            raise ValueError(f"内置风格「{name}」不可删除")
        if name not in self._strategies:
            return False
        del self._strategies[name]
        return True

    def is_builtin(self, name: str) -> bool:
        return name in self._BUILTIN_NAMES

    def reset(self) -> None:
        """Restore to built-in defaults, removing all custom styles."""
        self._strategies = dict(BUILTIN_STRATEGIES)

    # ── create custom style ───────────────────────────────────

    def create(
        self,
        name: str,
        primary: str,
        secondary: str,
        background: str,
        accent: str,
        title_family: str | None = None,
        body_family: str | None = None,
        title_size: int | None = None,
        body_size: int | None = None,
        title_color: str | None = None,
        body_color: str | None = None,
        bold_titles: bool = True,
        description: str = "",
    ) -> StyleStrategy:
        """Create and register a custom style strategy.

        Args:
            name:        Unique style name.
            primary:     Hex colour for main emphasis (no #).
            secondary:   Hex colour for body text.
            background:  Hex colour for slide background.
            accent:      Hex colour for highlights.
            title_family: Font family for titles.
            body_family:  Font family for body text.
            title_size:   Font size for titles (pt).
            body_size:    Font size for body text (pt).
            title_color:  Override colour for titles.
            body_color:   Override colour for body text.
            bold_titles:  Whether titles are bold.
            description:  Human-readable description.

        Returns:
            The newly created StyleStrategy.
        """
        strategy = StyleStrategy(
            name=name,
            description=description or f"自定义风格: {name}",
            palette=ColorPalette(
                primary=primary,
                secondary=secondary,
                background=background,
                accent=accent,
                title_color=title_color or primary,
                body_color=body_color or secondary,
            ),
            fonts=FontPrefs(
                title_family=title_family,
                title_size=title_size,
                body_family=body_family,
                body_size=body_size,
                bold_titles=bold_titles,
            ),
        )
        self.register(strategy)
        return strategy

    # ── query ──────────────────────────────────────────────────

    def list_names(self) -> list[str]:
        return list(self._strategies.keys())

    def list_all(self) -> list[StyleStrategy]:
        return list(self._strategies.values())

    def list_custom(self) -> list[StyleStrategy]:
        return [s for k, s in self._strategies.items() if k not in self._BUILTIN_NAMES]

    def to_dict(self) -> dict:
        return {k: v.to_dict() for k, v in self._strategies.items()}

    def __len__(self) -> int:
        return len(self._strategies)

    # ── persistence ───────────────────────────────────────────

    def save_to_file(self, filepath: str | Path) -> None:
        """Save all styles (built-in + custom) to a JSON file."""
        import json
        from pathlib import Path
        path = Path(filepath).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    def load_from_file(self, filepath: str | Path) -> int:
        """Load custom styles from a JSON file. Returns count loaded."""
        import json
        from pathlib import Path
        path = Path(filepath).expanduser()
        if not path.exists():
            return 0

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        count = 0
        for name, d in data.items():
            if name in self._BUILTIN_NAMES:
                continue  # don't overwrite built-in
            try:
                self.create(
                    name=name,
                    primary=d["palette"]["primary"],
                    secondary=d["palette"]["secondary"],
                    background=d["palette"]["background"],
                    accent=d["palette"]["accent"],
                    title_color=d["palette"].get("title_color"),
                    body_color=d["palette"].get("body_color"),
                    title_family=d.get("fonts", {}).get("title_family"),
                    body_family=d.get("fonts", {}).get("body_family"),
                    title_size=d.get("fonts", {}).get("title_size"),
                    body_size=d.get("fonts", {}).get("body_size"),
                    bold_titles=d.get("fonts", {}).get("bold_titles", True),
                    description=d.get("description", ""),
                )
                count += 1
            except Exception:
                continue
        return count
