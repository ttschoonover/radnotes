"""Markdown to HTML for the preview pane. Raw HTML in notes is not rendered."""
from __future__ import annotations

from urllib.parse import quote

from markdown_it import MarkdownIt
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.tasklists import tasklists_plugin

from .store import FENCE_RE, HEADING_RE, TAG_RE, WIKILINK_RE

_md = (
    MarkdownIt("commonmark", {"html": False, "typographer": True})
    .enable(["table", "strikethrough", "replacements", "smartquotes"])
    .use(tasklists_plugin)
    .use(footnote_plugin)
)


def _escape_label(text: str) -> str:
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def render(text: str, resolve=None) -> str:
    """Render markdown. ``resolve(target) -> path | None`` turns [[links]]
    into #note: links (existing notes) or #new: links (notes to create)."""

    def wikilink(m) -> str:
        target = m.group(1).strip()
        label = (m.group(3) or target).strip()
        rel = resolve(target) if resolve else None
        href = f"#note:{quote(rel)}" if rel else f"#new:{quote(target)}"
        return f"[{_escape_label(label)}]({href})"

    out, fenced = [], False
    for line in text.split("\n"):
        if FENCE_RE.match(line):
            fenced = not fenced
        elif not fenced and not line.startswith("    "):
            line = WIKILINK_RE.sub(wikilink, line)
            if not HEADING_RE.match(line):
                line = TAG_RE.sub(lambda m: f"[#{m.group(1)}](#tag:{quote(m.group(1))})", line)
        out.append(line)
    return _md.render("\n".join(out))
