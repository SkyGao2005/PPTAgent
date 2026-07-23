"""Resolve effective DrawingML text styles through the PPTX inheritance chain.

python-pptx reports only explicitly overridden run properties, so a template
that defines its typography on the layout, master, or theme yields nothing but
``None``. This module walks the chain PowerPoint itself uses --
shape -> layout placeholder -> master placeholder -> master text styles ->
theme -- so the IR records the style a viewer actually sees.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

from lxml import etree

_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS = {"a": _A, "p": _P}

# Placeholder types that inherit the master's title text style.
_TITLE_PLACEHOLDERS = frozenset({"TITLE", "CENTER_TITLE"})
# Placeholder types that inherit the master's body text style.
_BODY_PLACEHOLDERS = frozenset({"BODY", "SUBTITLE", "OBJECT", "CONTENT", "VERTICAL_BODY"})


@dataclass(frozen=True, slots=True)
class ThemePalette:
    """Theme colour scheme and major/minor latin typefaces."""

    colors: dict[str, str] = field(default_factory=dict)
    major_latin: str | None = None
    minor_latin: str | None = None

    def color(self, token: str) -> str | None:
        return self.colors.get(token)


@dataclass(frozen=True, slots=True)
class ResolvedTextStyle:
    """Effective run properties for one placeholder level."""

    font_family: str | None = None
    size_pt: float | None = None
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    color: str | None = None
    alignment: str | None = None

    def merge(self, weaker: "ResolvedTextStyle") -> "ResolvedTextStyle":
        """Fill unset fields from a less specific level of the chain."""

        return ResolvedTextStyle(
            **{
                name: (
                    getattr(self, name)
                    if getattr(self, name) is not None
                    else getattr(weaker, name)
                )
                for name in ResolvedTextStyle.__slots__
            }
        )


def _hex(value: str | None) -> str | None:
    if not value or len(value) != 6:
        return None
    try:
        int(value, 16)
    except ValueError:
        return None
    return f"#{value.upper()}"


def _scale_channel(channel: int, modifiers: dict[str, float]) -> int:
    """Apply the common luminance modifiers to one 0-255 channel."""

    value = channel / 255
    if "shade" in modifiers:
        value *= modifiers["shade"]
    if "tint" in modifiers:
        value = value * modifiers["tint"] + (1 - modifiers["tint"])
    if "lumMod" in modifiers:
        value *= modifiers["lumMod"]
    if "lumOff" in modifiers:
        value += modifiers["lumOff"]
    return max(0, min(255, round(value * 255)))


def _apply_modifiers(color: str, node: etree._Element) -> str:
    modifiers = {
        etree.QName(child).localname: int(child.get("val", "0")) / 100_000
        for child in node
        if etree.QName(child).localname
        in {"lumMod", "lumOff", "shade", "tint", "alpha"}
    }
    if not modifiers:
        return color
    channels = [int(color[index : index + 2], 16) for index in (1, 3, 5)]
    scaled = [_scale_channel(channel, modifiers) for channel in channels]
    result = "#{:02X}{:02X}{:02X}".format(*scaled)
    # Transparency is what lets a tinted panel show the artwork underneath.
    # CSS spells it as two more hex digits.
    alpha = modifiers.get("alpha")
    if alpha is not None and alpha < 1:
        result += f"{max(0, min(255, round(alpha * 255))):02X}"
    return result


# The handful of preset colour names a template actually reaches for. The full
# list is large and unused; an unknown name falls through to the next child
# rather than resolving to something invented.
_PRESET_COLORS = {
    "black": "#000000",
    "white": "#FFFFFF",
    "gray": "#808080",
    "grey": "#808080",
    "red": "#FF0000",
    "green": "#008000",
    "blue": "#0000FF",
    "yellow": "#FFFF00",
}


def resolve_color(
    container: etree._Element | None,
    palette: ThemePalette,
) -> str | None:
    """Resolve the first colour child of ``container`` to a hex string."""

    if container is None:
        return None
    for child in container:
        name = etree.QName(child).localname
        if name == "srgbClr":
            base = _hex(child.get("val"))
        elif name == "sysClr":
            base = _hex(child.get("lastClr"))
        elif name == "schemeClr":
            base = palette.color(child.get("val", ""))
        elif name == "prstClr":
            base = _PRESET_COLORS.get(str(child.get("val", "")).lower())
        else:
            continue
        if base is not None:
            return _apply_modifiers(base, child)
    return None


def parse_theme(blob: bytes, color_map: dict[str, str] | None = None) -> ThemePalette:
    """Parse a theme part into a palette, applying the master's colour map."""

    root = etree.fromstring(blob)
    colors: dict[str, str] = {}
    scheme = root.find(".//a:clrScheme", NS)
    if scheme is not None:
        for child in scheme:
            token = etree.QName(child).localname
            value = resolve_color(child, ThemePalette())
            if value is not None:
                colors[token] = value
    # <p:clrMap> aliases bg1/tx1/bg2/tx2 onto the dk*/lt* slots.
    for alias, target in (color_map or {}).items():
        if target in colors:
            colors[alias] = colors[target]
    fonts = root.find(".//a:fontScheme", NS)

    def latin(kind: str) -> str | None:
        if fonts is None:
            return None
        node = fonts.find(f"a:{kind}/a:latin", NS)
        typeface = node.get("typeface") if node is not None else None
        return typeface or None

    return ThemePalette(
        colors=colors,
        major_latin=latin("majorFont"),
        minor_latin=latin("minorFont"),
    )


def theme_for_master(master: Any) -> ThemePalette:
    """Load the palette backing one slide master."""

    color_map = {}
    mapping = master.element.find(f".//{{{_P}}}clrMap")
    if mapping is not None:
        color_map = dict(mapping.attrib)
    for rel in master.part.rels.values():
        if rel.reltype.endswith("/theme"):
            return parse_theme(rel.target_part.blob, color_map)
    return ThemePalette()


def _placeholder_type(shape: Any) -> str:
    # ``placeholder_format`` raises on ordinary shapes, so gate on the flag.
    if not getattr(shape, "is_placeholder", False):
        return ""
    placeholder = shape.placeholder_format
    return str(getattr(placeholder.type, "name", placeholder.type) or "")


def _placeholder_index(shape: Any) -> int | None:
    if not getattr(shape, "is_placeholder", False):
        return None
    return shape.placeholder_format.idx


def _style_from_properties(
    run_properties: etree._Element | None,
    paragraph_properties: etree._Element | None,
    palette: ThemePalette,
) -> ResolvedTextStyle:
    alignment = (
        paragraph_properties.get("algn") if paragraph_properties is not None else None
    )
    if run_properties is None:
        return ResolvedTextStyle(alignment=alignment)
    size = run_properties.get("sz")
    latin = run_properties.find("a:latin", NS)
    typeface = latin.get("typeface") if latin is not None else None
    if typeface == "+mj-lt":
        typeface = palette.major_latin
    elif typeface == "+mn-lt":
        typeface = palette.minor_latin
    underline = run_properties.get("u")
    return ResolvedTextStyle(
        font_family=typeface or None,
        size_pt=float(size) / 100 if size else None,
        bold=_flag(run_properties.get("b")),
        italic=_flag(run_properties.get("i")),
        underline=None if underline is None else underline != "none",
        color=resolve_color(run_properties.find("a:solidFill", NS), palette),
        alignment=alignment,
    )


def _flag(value: str | None) -> bool | None:
    if value is None:
        return None
    return value in {"1", "true"}


def _list_style_levels(
    element: etree._Element,
    level: int,
    palette: ThemePalette,
) -> ResolvedTextStyle:
    """Read a shape's own ``<a:lstStyle>`` defaults for one indent level."""

    body = element.find(f".//{{{_P}}}txBody")
    if body is None:
        body = element.find(f".//{{{_A}}}txBody")
    if body is None:
        return ResolvedTextStyle()
    list_style = body.find("a:lstStyle", NS)
    if list_style is None:
        return ResolvedTextStyle()
    paragraph_properties = list_style.find(f"a:lvl{level}pPr", NS)
    if paragraph_properties is None:
        return ResolvedTextStyle()
    return _style_from_properties(
        paragraph_properties.find("a:defRPr", NS),
        paragraph_properties,
        palette,
    )


class StyleResolver:
    """Resolve effective text styles for the shapes of one slide."""

    def __init__(self, slide: Any) -> None:
        self.layout = getattr(slide, "slide_layout", None)
        self.master = getattr(self.layout, "slide_master", None)
        self.palette = (
            theme_for_master(self.master) if self.master is not None else ThemePalette()
        )

    def _matching_placeholder(self, owner: Any, index: int | None, kind: str) -> Any:
        if owner is None or index is None:
            return None
        for shape in owner.placeholders:
            if _placeholder_index(shape) == index:
                return shape
        # Masters index placeholders by role rather than by slide index.
        for shape in owner.placeholders:
            if _placeholder_type(shape) == kind:
                return shape
        return None

    def _master_text_style(self, kind: str, level: int) -> ResolvedTextStyle:
        if self.master is None:
            return ResolvedTextStyle()
        if kind in _TITLE_PLACEHOLDERS:
            name = "titleStyle"
        elif kind in _BODY_PLACEHOLDERS:
            name = "bodyStyle"
        else:
            name = "otherStyle"
        paragraph_properties = self.master.element.find(
            f".//p:txStyles/p:{name}/a:lvl{level}pPr", NS
        )
        if paragraph_properties is None:
            return ResolvedTextStyle()
        return _style_from_properties(
            paragraph_properties.find("a:defRPr", NS),
            paragraph_properties,
            self.palette,
        )

    def _chain(self, shape: Any, level: int) -> Iterator[ResolvedTextStyle]:
        """Yield styles from most to least specific."""

        kind = _placeholder_type(shape)
        index = _placeholder_index(shape)
        yield _list_style_levels(shape.element, level, self.palette)
        layout_placeholder = self._matching_placeholder(self.layout, index, kind)
        if layout_placeholder is not None:
            yield _list_style_levels(layout_placeholder.element, level, self.palette)
        master_placeholder = self._matching_placeholder(self.master, index, kind)
        if master_placeholder is not None:
            yield _list_style_levels(master_placeholder.element, level, self.palette)
        yield self._master_text_style(kind, level)

    def inherited_style(self, shape: Any, level: int = 1) -> ResolvedTextStyle:
        """Effective style for ``shape`` at one indent level, ignoring runs."""

        resolved = ResolvedTextStyle()
        for candidate in self._chain(shape, max(1, min(level, 9))):
            resolved = resolved.merge(candidate)
        if resolved.font_family is None:
            resolved = resolved.merge(
                ResolvedTextStyle(font_family=self.palette.minor_latin)
            )
        return resolved
