from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag

GLOBAL_DROP = ("script", "style", "head", "noscript", "iframe", "svg", "template", "link", "meta")
LOCAL_DROP = ("nav", "footer", "aside", "form")
COOKIE_HINTS = re.compile(r"cookie|consent|datenschutz-banner|cookiebot", re.IGNORECASE)


@dataclass(frozen=True)
class Extraction:
    text: str
    content_hash: str
    matched_selector: str | None
    used_fallback: bool


def _drop_cookie_banners(node: Tag) -> None:
    for el in list(node.find_all(True)):
        ident = " ".join(filter(None, [el.get("id") or "", " ".join(el.get("class") or [])]))
        if ident and COOKIE_HINTS.search(ident):
            el.decompose()


def _annotate_hrefs(node: Tag) -> None:
    """Make link targets visible to the text-diff.

    Without this, two visually identical links pointing to different URLs would
    look the same to ``get_text()``. We append the href right after the link's
    text so changes in destination show up in the unified diff.
    """
    for a in node.find_all("a"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "#", "mailto:void")):
            continue
        a.append(f" ⟶ {href}")


def _normalize(text: str) -> str:
    lines = []
    for raw in text.splitlines():
        cleaned = re.sub(r"[ \t ]+", " ", raw).strip()
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _looks_json(content: str, content_type: str | None) -> bool:
    if content_type and "json" in content_type.lower():
        return True
    stripped = (content or "").lstrip()
    if not stripped or stripped[0] not in "{[":
        return False
    try:
        json.loads(stripped)
        return True
    except json.JSONDecodeError:
        return False


def _extract_json(content: str) -> Extraction:
    try:
        obj = json.loads(content)
        text = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)
    except json.JSONDecodeError:
        text = (content or "").strip()
    text = _normalize(text)
    return Extraction(
        text=text,
        content_hash=_hash(text),
        matched_selector="(json:api)",
        used_fallback=False,
    )


def _extract_html(content: str, selectors: list[str]) -> Extraction:
    soup = BeautifulSoup(content or "", "lxml")

    for tag_name in GLOBAL_DROP:
        for el in soup.find_all(tag_name):
            el.decompose()

    matched: str | None = None
    node: Tag | None = None
    for sel in selectors or []:
        try:
            candidate = soup.select_one(sel)
        except Exception:
            candidate = None
        if candidate is not None:
            node = candidate
            matched = sel
            break

    used_fallback = node is None
    if node is None:
        node = soup.body or soup

    _drop_cookie_banners(node)
    for tag_name in LOCAL_DROP:
        for el in node.find_all(tag_name):
            el.decompose()

    _annotate_hrefs(node)
    raw_text = node.get_text("\n", strip=True)
    text = _normalize(raw_text)
    return Extraction(
        text=text,
        content_hash=_hash(text),
        matched_selector=matched,
        used_fallback=used_fallback,
    )


def extract_relevant(
    content: str,
    selectors: list[str],
    content_type: str | None = None,
    test_suffix: str = "",
) -> Extraction:
    """Convert raw response body to canonical, diff-friendly text.

    JSON responses (detected by content-type or leading ``{``/``[``) get
    pretty-printed with sorted keys so the diff is stable. HTML responses go
    through the selector pipeline and have ``<script>/<style>/<head>``,
    cookie banners, navs and footers stripped.

    ``test_suffix`` (from Settings) is appended verbatim before hashing so a
    user can trigger an artificial change to validate the notification
    pipeline end-to-end. Leave empty to disable.
    """
    if _looks_json(content, content_type):
        e = _extract_json(content)
    else:
        e = _extract_html(content, selectors)
    suffix = (test_suffix or "").strip()
    if not suffix:
        return e
    text = (e.text + "\n[test:" + suffix + "]").strip()
    return Extraction(
        text=text,
        content_hash=_hash(text),
        matched_selector=e.matched_selector,
        used_fallback=e.used_fallback,
    )
