"""Convert local WeChat group inbox messages into report-ready summaries."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment]

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parent.parent
INBOX_DIR = ROOT / "data" / "wechat_groups" / "inbox"
PROCESSED_DIR = ROOT / "data" / "wechat_groups" / "processed"
SUMMARY_DIR = ROOT / "data" / "wechat_groups" / "summaries"
INDEX_JSON = PROCESSED_DIR / "index.json"
INDEX_MD = PROCESSED_DIR / "index.md"
SH = ZoneInfo("Asia/Shanghai")

KEYWORDS = [
    "AI", "算力", "存储", "半导体", "光模块", "机器人", "新能源", "港股", "A股", "美股",
    "加仓", "减仓", "买入", "卖出", "调研", "订单", "业绩", "估值", "风险", "流动性",
]

DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/coding/v3"
DEFAULT_MODEL = "kimi-k2.6"

if load_dotenv is not None:
    load_dotenv(ROOT / ".env")


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    meta = {}
    for line in text[4:end].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return meta, text[end + 5 :].strip()


def _extract_keywords(text: str) -> list[str]:
    return [kw for kw in KEYWORDS if kw.lower() in text.lower()]


def _extract_tickers(text: str) -> list[str]:
    return sorted(set(re.findall(r"(?<![A-Za-z])\$?[A-Z]{2,6}(?![A-Za-z])", text)))[:20]


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _load_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(INBOX_DIR.glob("*/*.md")):
        meta, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
        if not body:
            continue
        preview = re.sub(r"\s+", " ", body).strip()[:300]
        records.append(
            {
                "group": meta.get("group") or path.parent.name,
                "messageId": meta.get("messageId") or path.stem,
                "importedAt": meta.get("importedAt") or "",
                "path": path.as_posix(),
                "keywords": _extract_keywords(body),
                "tickers": _extract_tickers(body),
                "preview": preview,
                "body": body,
            }
        )
    return records


def _write_index(records: list[dict[str, Any]]) -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    public_records = [{k: v for k, v in item.items() if k != "body"} for item in records]
    INDEX_JSON.write_text(json.dumps(public_records, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 微信投资群消息索引", ""]
    for item in public_records:
        tags = ", ".join(_dedupe(item["keywords"] + item["tickers"]))
        lines.append(f"- {item['importedAt']}｜{item['group']}｜{tags}｜{item['preview']}")
    INDEX_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _today_records(records: list[dict[str, Any]], now: dt.datetime) -> list[dict[str, Any]]:
    today = now.date()
    out = []
    for item in records:
        imported_at = item.get("importedAt") or ""
        try:
            item_date = dt.datetime.fromisoformat(imported_at).date()
        except ValueError:
            item_date = today
        if item_date == today:
            out.append(item)
    return out


def _fallback_summary(records: list[dict[str, Any]], now: dt.datetime) -> str:
    groups = _dedupe([item["group"] for item in records])
    keywords = _dedupe([kw for item in records for kw in item["keywords"]])
    tickers = _dedupe([ticker for item in records for ticker in item["tickers"]])
    lines = [
        "# 微信投资群情报摘要",
        "",
        f"- 群组：{', '.join(groups) or '无'}",
        f"- 消息批次：{len(records)}",
        f"- 关键词：{', '.join(keywords) or '无'}",
        f"- 股票/代码线索：{', '.join(tickers) or '无'}",
        "",
        "## 可用于日报的结构化素材",
        "",
        "当前环境未启用群聊 LLM 摘要，以下为规则提取的消息要点，供主日报模型二次整合。",
        "",
        "## 消息摘录",
        "",
    ]
    for item in records[:30]:
        tags = ", ".join(_dedupe(item["keywords"] + item["tickers"]))
        lines.append(f"- {item['importedAt']}｜{item['group']}｜{tags}｜{item['preview']}")
    lines += [
        "",
        "## 使用边界",
        "",
        "- 群聊内容是投资线索，不是交易结论；需结合公开信息、价格行为和基本面验证。",
        f"- 生成时间：{now.isoformat()}",
        "",
    ]
    return "\n".join(lines)


def _llm_summary(records: list[dict[str, Any]], now: dt.datetime) -> str | None:
    if OpenAI is None:
        return None
    api_key = os.environ.get("ARK_API_KEY", "").strip()
    if not api_key:
        return None
    base_url = (os.environ.get("ARK_BASE_URL") or DEFAULT_BASE_URL).strip()
    model = (os.environ.get("ARK_MODEL") or DEFAULT_MODEL).strip()
    payload = [
        {
            "group": item["group"],
            "importedAt": item["importedAt"],
            "keywords": item["keywords"],
            "tickers": item["tickers"],
            "text": item["body"][:1600],
        }
        for item in records[:40]
    ]
    prompt = (
        "你是买方研究助理。请把以下微信群投资讨论整理成可并入《全球投资动能监控》的中文 Markdown 素材。"
        "要求：1）不要使用代码围栏；2）只输出结构化摘要，不逐字转录；3）控制在 700 字以内；"
        "4）必须包含：最大公约数、A股/港股/中概映射、证据链摘录、待验证问题；"
        "5）不要给确定性交易指令。\n\n"
        f"生成时间：{now.isoformat()}\n"
        f"群聊素材 JSON：{json.dumps(payload, ensure_ascii=False)}"
    )
    client = OpenAI(api_key=api_key, base_url=base_url)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "你擅长把非公开投研讨论整理为审慎、可验证的投资情报。"},
            {"role": "user", "content": prompt},
        ],
        max_tokens=1200,
        temperature=0.2,
    )
    content = (response.choices[0].message.content or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:markdown)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content).strip()
    return content or None


def _write_daily_summary(records: list[dict[str, Any]], now: dt.datetime) -> Path | None:
    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    todays_records = _today_records(records, now)
    if not todays_records:
        return None
    try:
        summary = _llm_summary(todays_records, now)
    except Exception as exc:
        print(f"[WARN] group LLM summary failed: {type(exc).__name__}: {exc}")
        summary = None
    summary = summary or _fallback_summary(todays_records, now)
    path = SUMMARY_DIR / f"{now.strftime('%Y-%m-%d')}_微信投资群情报摘要.md"
    front_matter = "\n".join(
        [
            "---",
            f"date: {now.date().isoformat()}",
            f"generated_at: {now.isoformat()}",
            'title: "微信投资群情报摘要"',
            f"message_batches: {len(todays_records)}",
            "---",
            "",
        ]
    )
    path.write_text(front_matter + summary.strip() + "\n", encoding="utf-8")
    return path


def main() -> int:
    now = dt.datetime.now(SH)
    records = _load_records()
    _write_index(records)
    summary_path = _write_daily_summary(records, now)
    print(f"[OK] processed group messages: {len(records)}")
    if summary_path:
        print(f"[OK] summary -> {summary_path}")
    else:
        print("[OK] summary skipped: no messages imported today")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
