"""
app/utils/adf.py
================
Atlassian Document Format → simple HTML, for Plane's description_html field.

Plane sanitizes description_html server-side, so only a small, safe tag set
is emitted (p, h1-h3, strong, em, code, a, ul/ol/li, pre, blockquote, br, hr,
table/tr/td). Every piece of text is HTML-escaped. Unknown node types degrade
to their children's text rather than being dropped.
"""

from __future__ import annotations

import re
from html import escape

_HEADING_MAX = 3

# Confluence page links look like .../pages/<id>/... or ...?pageId=<id>
_CONF_PAGE_ID = re.compile(r"/pages/(\d+)|pageId=(\d+)")
_HREF = re.compile(r'href="([^"]*)"')


def adf_to_html(node: dict | None) -> str:
    """Convert an ADF document (or any node) to simple HTML. '' for None."""
    if not node:
        return ""
    return _render(node)


def html_links_conf_page_ids(html: str) -> set[str]:
    """Confluence page ids referenced by links in *html* (for the link pass)."""
    ids: set[str] = set()
    for href in _HREF.findall(html or ""):
        m = _CONF_PAGE_ID.search(href)
        if m:
            ids.add(m.group(1) or m.group(2))
    return ids


def rewrite_conf_links(html: str, conf_id_to_url: dict[str, str]) -> str:
    """Rewrite links pointing to Confluence pages to their migrated Plane page
    URLs. conf_id_to_url maps a Confluence page id → Plane page URL."""
    def repl(m):
        href = m.group(1)
        cm = _CONF_PAGE_ID.search(href)
        if cm:
            cid = cm.group(1) or cm.group(2)
            if cid in conf_id_to_url:
                return f'href="{escape(conf_id_to_url[cid])}"'
        return m.group(0)
    return _HREF.sub(repl, html or "")


def build_subpages_html(children: list[tuple[str, str]]) -> str:
    """'Sub-pages' link list for the bottom of a parent page. *children* is a
    list of (url, title)."""
    if not children:
        return ""
    items = "".join(
        f'<li><a href="{escape(url)}">{escape(title)}</a></li>' for url, title in children
    )
    return f"<hr><h3>Sub-pages</h3><ul>{items}</ul>"


def render_custom_fields_table(rows: list[tuple[str, str]]) -> str:
    """Render [(label, value), ...] as the custom-fields appendix table."""
    if not rows:
        return ""
    body = "".join(
        f"<tr><td><strong>{escape(label)}</strong></td><td>{escape(value)}</td></tr>"
        for label, value in rows
    )
    return f"<h3>Jira fields</h3><table><tbody>{body}</tbody></table>"


def _children(node: dict) -> str:
    return "".join(_render(child) for child in node.get("content", []))


def _render(node: dict) -> str:
    ntype = node.get("type", "")

    if ntype == "text":
        text = escape(node.get("text", ""))
        href = None
        for mark in node.get("marks", []):
            mtype = mark.get("type")
            if mtype == "strong":
                text = f"<strong>{text}</strong>"
            elif mtype == "em":
                text = f"<em>{text}</em>"
            elif mtype == "code":
                text = f"<code>{text}</code>"
            elif mtype == "strike":
                text = f"<s>{text}</s>"
            elif mtype == "link":
                href = mark.get("attrs", {}).get("href", "")
        if href and href.startswith(("http://", "https://")):
            text = f'<a href="{escape(href)}">{text}</a>'
        return text

    if ntype == "paragraph":
        inner = _children(node)
        return f"<p>{inner}</p>" if inner else ""
    if ntype == "heading":
        level = min(int(node.get("attrs", {}).get("level", 1) or 1), _HEADING_MAX)
        return f"<h{level}>{_children(node)}</h{level}>"
    if ntype == "bulletList":
        return f"<ul>{_children(node)}</ul>"
    if ntype == "orderedList":
        return f"<ol>{_children(node)}</ol>"
    if ntype == "listItem":
        return f"<li>{_children(node)}</li>"
    if ntype == "codeBlock":
        return f"<pre><code>{_children(node)}</code></pre>"
    if ntype == "blockquote":
        return f"<blockquote>{_children(node)}</blockquote>"
    if ntype == "hardBreak":
        return "<br/>"
    if ntype == "rule":
        return "<hr/>"
    if ntype == "table":
        return f"<table><tbody>{_children(node)}</tbody></table>"
    if ntype == "tableRow":
        return f"<tr>{_children(node)}</tr>"
    if ntype in ("tableCell", "tableHeader"):
        return f"<td>{_children(node)}</td>"
    if ntype == "mention":
        return escape("@" + node.get("attrs", {}).get("text", "").lstrip("@"))
    if ntype == "emoji":
        return escape(node.get("attrs", {}).get("text", ""))

    # ── Confluence-specific nodes ─────────────────────────────────────
    if ntype == "panel":  # info/note/warning/success boxes
        return f"<blockquote>{_children(node)}</blockquote>"
    if ntype in ("expand", "nestedExpand"):
        title = escape(node.get("attrs", {}).get("title", "") or "Details")
        return f"<p><strong>{title}</strong></p>{_children(node)}"
    if ntype == "taskList":
        return f"<ul>{_children(node)}</ul>"
    if ntype == "taskItem":
        mark = "☑ " if node.get("attrs", {}).get("state") == "DONE" else "☐ "
        return f"<li>{mark}{_children(node)}</li>"
    if ntype == "status":
        return escape(node.get("attrs", {}).get("text", ""))
    if ntype == "date":
        return escape(node.get("attrs", {}).get("timestamp", ""))
    if ntype == "inlineCard":  # Confluence link/smart-link → keep the URL
        url = node.get("attrs", {}).get("url", "")
        return f'<a href="{escape(url)}">{escape(url)}</a>' if url else ""

    # doc, mediaGroup, media, extension, unknown… → render children only
    return _children(node)
