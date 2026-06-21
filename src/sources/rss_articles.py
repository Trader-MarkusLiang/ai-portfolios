"""Generic RSS/Atom article source."""

from __future__ import annotations

import calendar
import email.utils as eut
import hashlib
import re
from html import unescape
from xml.etree import ElementTree as ET

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
}

TIMEOUT_S = 15


class RSSArticleError(RuntimeError):
    pass


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", unescape(text))
    return text.strip()


def _parse_time(value: str) -> tuple[str, float]:
    value = (value or "").strip()
    if not value:
        return "", 0.0
    parsed = eut.parsedate_tz(value)
    if parsed:
        ts = float(calendar.timegm(parsed[:9])) - (parsed[9] or 0)
        return value, ts
    try:
        from datetime import datetime

        normalized = value.replace("Z", "+00:00")
        dt_value = datetime.fromisoformat(normalized)
        return value, dt_value.timestamp()
    except ValueError:
        return value, 0.0


def _stable_id(url: str, title: str) -> str:
    return hashlib.sha1(f"{url}|{title}".encode("utf-8")).hexdigest()


def _child_text(node: ET.Element, names: tuple[str, ...]) -> str:
    for name in names:
        found = node.find(name)
        if found is not None and found.text:
            return found.text.strip()
    for child in node:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag in names and child.text:
            return child.text.strip()
    return ""


def _atom_link(node: ET.Element) -> str:
    for child in node:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "link":
            href = child.attrib.get("href", "").strip()
            rel = child.attrib.get("rel", "").strip()
            if href and rel in {"", "alternate"}:
                return href
    return ""


def _parse_rss(root: ET.Element, source_name: str, kind: str) -> list[dict]:
    channel = root.find("channel")
    if channel is None:
        return []
    articles: list[dict] = []
    for item in channel.findall("item"):
        title = _strip_html(_child_text(item, ("title",)))
        link = _child_text(item, ("link", "guid"))
        desc = _strip_html(_child_text(item, ("description", "summary")))
        published, published_ts = _parse_time(_child_text(item, ("pubDate", "published", "updated")))
        text = f"{title}。{desc}" if desc and desc != title else title
        articles.append(
            {
                "id": _stable_id(link, title),
                "text": text,
                "url": link,
                "createdAt": published,
                "createdAtTs": published_ts,
                "source": f"rss:{kind}",
                "contentType": "article",
                "sourceName": source_name,
                "title": title,
            }
        )
    return articles


def _parse_atom(root: ET.Element, source_name: str, kind: str) -> list[dict]:
    articles: list[dict] = []
    for entry in root.findall("{*}entry"):
        title = _strip_html(_child_text(entry, ("title",)))
        link = _atom_link(entry) or _child_text(entry, ("id",))
        summary = _strip_html(_child_text(entry, ("summary", "content")))
        published, published_ts = _parse_time(_child_text(entry, ("published", "updated")))
        text = f"{title}。{summary}" if summary and summary != title else title
        articles.append(
            {
                "id": _stable_id(link, title),
                "text": text,
                "url": link,
                "createdAt": published,
                "createdAtTs": published_ts,
                "source": f"rss:{kind}",
                "contentType": "article",
                "sourceName": source_name,
                "title": title,
            }
        )
    return articles


def fetch_articles(source: dict, timeout: float = TIMEOUT_S) -> list[dict]:
    name = (source.get("name") or "").strip()
    url = (source.get("rss_url") or "").strip()
    kind = (source.get("kind") or "article").strip()
    if not name or not url:
        return []

    try:
        response = requests.get(url, headers=HEADERS, timeout=timeout)
    except requests.RequestException as exc:
        raise RSSArticleError(f"{name}: {exc.__class__.__name__}") from exc
    if not response.ok:
        raise RSSArticleError(f"{name}: HTTP {response.status_code}")

    try:
        root = ET.fromstring(response.text)
    except ET.ParseError as exc:
        raise RSSArticleError(f"{name}: parse error {exc}") from exc

    root_tag = root.tag.rsplit("}", 1)[-1].lower()
    if root_tag == "rss":
        return _parse_rss(root, name, kind)
    if root_tag == "feed":
        return _parse_atom(root, name, kind)
    raise RSSArticleError(f"{name}: unsupported feed")
