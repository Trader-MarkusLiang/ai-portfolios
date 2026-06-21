"""Sync local WeWe RSS articles into the git repository.

This script is intended for local Codex automations because WeWe RSS
currently runs on this Mac at http://127.0.0.1:4010.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "article_sources.yaml"
OUT_DIR = ROOT / "data" / "wechat_articles"
INDEX_JSON = OUT_DIR / "index.json"
INDEX_MD = OUT_DIR / "index.md"
SH = ZoneInfo("Asia/Shanghai")

if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))
    from src.sources.rss_articles import build_manual_articles, fetch_articles  # type: ignore
else:
    from src.sources.rss_articles import build_manual_articles, fetch_articles


def _slug(value: str) -> str:
    value = re.sub(r"\s+", "-", value.strip())
    value = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_.-]", "", value)
    return value[:80] or "article"


def _load_sources() -> list[dict]:
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    sources = []
    for item in data.get("article_sources") or []:
        if not item.get("enabled"):
            continue
        if item.get("kind") != "wechat":
            continue
        if not (item.get("rss_url") or "").strip() and not (item.get("manual_articles") or []):
            continue
        sources.append(dict(item))
    return sources


def _load_existing() -> dict[str, dict]:
    if not INDEX_JSON.exists():
        return {}
    try:
        items = json.loads(INDEX_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(items, list):
        return {}
    return {str(item.get("url") or item.get("id")): item for item in items if isinstance(item, dict)}


def _article_record(source_name: str, article: dict, synced_at: str) -> dict:
    url = article.get("url") or ""
    return {
        "id": str(article.get("id") or url or ""),
        "key": str(url or article.get("id") or ""),
        "sourceName": source_name,
        "title": article.get("title") or "",
        "url": url,
        "createdAt": article.get("createdAt") or "",
        "createdAtTs": float(article.get("createdAtTs") or 0),
        "text": article.get("text") or "",
        "syncedAt": synced_at,
    }


def _write_article_file(record: dict) -> None:
    source = _slug(record["sourceName"])
    title = _slug(record["title"] or record["id"])
    date = "unknown-date"
    if record.get("createdAtTs"):
        date = dt.datetime.fromtimestamp(record["createdAtTs"], SH).strftime("%Y%m%d")
    path = OUT_DIR / source / f"{date}_{title}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"source: {record['sourceName']}",
        f"title: {record['title']}",
        f"url: {record['url']}",
        f"createdAt: {record['createdAt']}",
        f"syncedAt: {record['syncedAt']}",
        "---",
        "",
        f"# {record['title'] or record['sourceName']}",
        "",
        f"来源：{record['sourceName']}",
        f"链接：{record['url']}",
        "",
        record.get("text") or "",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_indexes(records: list[dict]) -> None:
    records.sort(key=lambda item: (item.get("createdAtTs") or 0, item.get("syncedAt") or ""), reverse=True)
    INDEX_JSON.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 微信公众号文章同步索引", ""]
    for item in records:
        title = item.get("title") or item.get("url") or item.get("id")
        lines.append(f"- [{title}]({item.get('url')})｜{item.get('sourceName')}｜{item.get('createdAt')}")
    INDEX_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _git(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True, check=check)


def _sync_git(new_count: int) -> None:
    status = _git(["status", "--porcelain", "--untracked-files=no"], check=False)
    if status.stdout.strip():
        print("[WARN] working tree has local tracked changes; skip auto git sync", file=sys.stderr)
        return
    _git(["add", "data/wechat_articles/", "config/article_sources.yaml"])
    diff = _git(["diff", "--cached", "--quiet"], check=False)
    if diff.returncode == 0:
        print("[OK] no article changes to commit")
        return
    _git(["commit", "-m", f"chore(wechat): sync {new_count} articles [skip ci]"])
    _git(["pull", "--rebase", "origin", "main"])
    _git(["push", "origin", "main"])
    print("[OK] committed and pushed article sync")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    existing = _load_existing()
    synced_at = dt.datetime.now(SH).isoformat()
    errors: list[str] = []

    for source in _load_sources():
        source_name = source.get("name") or "unknown"
        articles: list[dict] = []
        try:
            articles.extend(fetch_articles(source))
        except Exception as exc:
            errors.append(f"{source_name}: {type(exc).__name__}: {exc}")
        seen_urls = {article.get("url") for article in articles if article.get("url")}
        for article in build_manual_articles(source):
            if article.get("url") not in seen_urls:
                articles.append(article)
                seen_urls.add(article.get("url"))
        for article in articles:
            record = _article_record(source_name, article, synced_at)
            key = record.get("key") or record.get("id")
            if not key:
                continue
            old = existing.get(key)
            if old:
                old.update({k: v for k, v in record.items() if v and k != "syncedAt"})
            else:
                existing[key] = record
                _write_article_file(record)

    records = list(existing.values())
    _write_indexes(records)
    new_count = sum(1 for item in records if item.get("syncedAt") == synced_at)
    print(f"[OK] total articles: {len(records)}; touched this run: {new_count}")
    for error in errors:
        print(f"[WARN] {error}", file=sys.stderr)

    if "--no-git" not in sys.argv:
        _sync_git(new_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
