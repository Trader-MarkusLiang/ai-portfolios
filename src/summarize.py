"""LLM summarizer.

Uses an OpenAI-compatible client (default: Volcengine Ark coding endpoint)
to turn normalized monitor items into a Chinese investment-research brief.

Env vars:
  ARK_API_KEY     required
  ARK_BASE_URL    default https://ark.cn-beijing.volces.com/api/coding/v3
  ARK_MODEL       default kimi-k2.6
"""

from __future__ import annotations

import json
import os
from typing import Any

INVESTMENT_KEYWORDS = (
    "AI", "算力", "DRAM", "HBM", "CXL", "MLCC", "半导体", "存储", "光模块",
    "CPO", "CoPoS", "ASIC", "GPU", "TPU", "加仓", "加cang", "买入", "卖出",
    "03121", "03119", "台积电", "美光", "海力士", "苹果", "Siri",
)
NOISE_KEYWORDS = ("会员服务使用指南", "关于抄作业", "关于提问", "关于知识库", "SVIP服务", "需求调研")

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment]

DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/coding/v3"
DEFAULT_MODEL = "kimi-k2.6"

MAX_ITEMS = 32
MAX_RETRY_ITEMS = 18
MAX_TEXT_LEN = 260
MAX_ARCHIVE_TEXT_LEN = 900
MAX_RETRY_TEXT_LEN = 180
MAX_PAYLOAD_CHARS = 9000
MAX_RETRY_PAYLOAD_CHARS = 5200
MAX_OUTPUT_TOKENS = 4096
REQUIRED_SECTIONS = (
    "## 一页结论",
    "## 今日最大公约数",
    "## 机会线索矩阵",
    "## 分歧与风险",
    "## 证据链摘录",
    "## 明日行动清单",
)

SYSTEM_PROMPT = "你是中文买方投研助手。直接输出最终报告，不输出思考过程。"

USER_TEMPLATE = """请基于下面 JSON 生成中文《全球投资动能监控》日报。目标是从全球信息中提炼对中国国内资本市场有领先意义的投资线索。不要复述流水账，要做交叉整合。控制在 800-1100 中文字，必须完整收尾。

固定结构：
## 一页结论
- 3条最重要投资命题；每条写清：结论、证据强度[A/B/C/D]、对A股/港股/中国产业链的含义。

## 今日最大公约数
- 2到4条跨来源共同信号；若共识弱，明确说明。

## 机会线索矩阵
| 主题 | 国内映射 | 核心触发 | 证据强度 | 下一步验证 |
|---|---|---|---|---|

## 分歧与风险
- 2到4条；说明如果判断错，错在哪里。

## 证据链摘录
- 5到8条精选证据。格式：`[强度] 来源：一句话摘要；链接：URL`。没有链接则写本地归档。

## 明日行动清单
| 要验证什么 | 为什么重要 | 观察指标/来源 |
|---|---|---|

---
仅供研究，不构成投资建议。

JSON:
{{items}}
"""

RETRY_TEMPLATE = """请基于 JSON 生成一份 600-850 字中文投资分析日报。只输出 Markdown 正文，不要推理过程，必须完整收尾。

结构必须包含：
## 一页结论
## 今日最大公约数
## 机会线索矩阵
## 分歧与风险
## 证据链摘录
## 明日行动清单
---
仅供研究，不构成投资建议。

JSON:
{{items}}
"""


class LLMError(RuntimeError):
    pass


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _importance_score(item: dict[str, Any]) -> tuple[int, int]:
    content_type = item.get("contentType") or "tweet"
    text = item.get("text") or ""
    source = item.get("source") or ""
    engagement = _to_int(item.get("likeCount")) + _to_int(item.get("retweetCount")) * 3
    priority = 0
    if content_type == "wechat_group_archive":
        priority += 30_000
    elif content_type == "article":
        priority += 12_000
    if "wechat_group" in source:
        priority += 20_000
    if any(keyword in text for keyword in INVESTMENT_KEYWORDS):
        priority += 2_000
    return (priority + engagement, len(text))


def _keyword_score(text: str) -> int:
    score = sum(1 for keyword in INVESTMENT_KEYWORDS if keyword in text)
    score += sum(ch.isdigit() for ch in text) // 3
    if any(keyword in text for keyword in ("DRAM", "HBM", "CXL", "MLCC", "03121", "03119", "加仓", "加cang")):
        score += 6
    if any(keyword in text for keyword in NOISE_KEYWORDS):
        score -= 8
    return score


def _wechat_archive_excerpt(text: str, max_text_len: int) -> str:
    chunks = []
    text = text.replace("六便士：", "六便士:")
    for raw in text.split("六便士:")[1:]:
        chunk = raw.strip()
        if not chunk:
            continue
        if not any(keyword in chunk for keyword in INVESTMENT_KEYWORDS):
            continue
        score = _keyword_score(chunk)
        if score <= 0:
            continue
        chunks.append((score, "六便士: " + chunk))
    if not chunks:
        return text[:max_text_len]
    chunks.sort(key=lambda item: item[0], reverse=True)
    out: list[str] = []
    total = 0
    for _, chunk in chunks[:4]:
        clipped = chunk[:520]
        if total + len(clipped) > max_text_len and out:
            break
        out.append(clipped)
        total += len(clipped)
    return " …… ".join(out)[:max_text_len]


def _clip_text(item: dict[str, Any], max_text_len: int) -> str:
    text = " ".join((item.get("text") or "").strip().split())
    if item.get("contentType") == "wechat_group_archive":
        max_text_len = max(max_text_len, min(MAX_ARCHIVE_TEXT_LEN, max_text_len * 4))
        text = _wechat_archive_excerpt(text, max_text_len)
    if len(text) > max_text_len:
        return text[:max_text_len] + "…"
    return text


def _compact_record(item: dict[str, Any], max_text_len: int) -> dict[str, Any]:
    return {
        "source": item.get("kol") or item.get("handle") or "",
        "type": item.get("contentType") or "tweet",
        "title": item.get("title") or "",
        "text": _clip_text(item, max_text_len),
        "url": item.get("url") or "",
        "time": item.get("createdAt") or "",
        "heat": _to_int(item.get("likeCount")) + _to_int(item.get("retweetCount")) * 3,
    }


def _pack(items: list[dict[str, Any]], *, max_items: int, max_text_len: int, max_payload_chars: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen_ids: set[int] = set()

    for source in sorted({str(item.get("kol") or item.get("handle") or "") for item in items}):
        source_items = [item for item in items if str(item.get("kol") or item.get("handle") or "") == source]
        if not source_items:
            continue
        best = max(source_items, key=_importance_score)
        selected.append(best)
        seen_ids.add(id(best))

    for item in sorted(items, key=_importance_score, reverse=True):
        if len(selected) >= max_items:
            break
        if id(item) in seen_ids:
            continue
        selected.append(item)
        seen_ids.add(id(item))

    packed: list[dict[str, Any]] = []
    total_chars = 0
    for item in selected:
        record = _compact_record(item, max_text_len)
        record_chars = len(json.dumps(record, ensure_ascii=False))
        if packed and total_chars + record_chars > max_payload_chars:
            break
        packed.append(record)
        total_chars += record_chars
    return packed


def _finish_reason(resp: Any) -> str:
    try:
        return str(resp.choices[0].finish_reason or "")
    except Exception:
        return ""


def _response_id(resp: Any) -> str:
    return str(getattr(resp, "id", "") or "")


def _call_model(client: Any, model: str, user_template: str, packed: list[dict[str, Any]]) -> tuple[str, Any]:
    payload = json.dumps(packed, ensure_ascii=False, separators=(",", ":"))
    user_prompt = user_template.replace("{{items}}", payload)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=MAX_OUTPUT_TOKENS,
        temperature=0.2,
    )
    return (resp.choices[0].message.content or "").strip(), resp


def _is_complete_report(content: str) -> bool:
    if len(content) < 600:
        return False
    if "仅供研究，不构成投资建议" not in content:
        return False
    return all(section in content for section in REQUIRED_SECTIONS)


def summarize(items: list[dict[str, Any]]) -> str:
    if OpenAI is None:
        raise LLMError("openai package not installed")
    api_key = os.environ.get("ARK_API_KEY", "").strip()
    if not api_key:
        raise LLMError("ARK_API_KEY is not set")

    base_url = (os.environ.get("ARK_BASE_URL") or DEFAULT_BASE_URL).strip()
    model = (os.environ.get("ARK_MODEL") or DEFAULT_MODEL).strip()
    client = OpenAI(api_key=api_key, base_url=base_url)

    packed = _pack(
        items,
        max_items=MAX_ITEMS,
        max_text_len=MAX_TEXT_LEN,
        max_payload_chars=MAX_PAYLOAD_CHARS,
    )
    try:
        content, resp = _call_model(client, model, USER_TEMPLATE, packed)
    except Exception as exc:  # network / auth / model errors
        raise LLMError(f"{type(exc).__name__}: {exc}") from exc

    if _is_complete_report(content):
        return content

    retry_packed = _pack(
        items,
        max_items=MAX_RETRY_ITEMS,
        max_text_len=MAX_RETRY_TEXT_LEN,
        max_payload_chars=MAX_RETRY_PAYLOAD_CHARS,
    )
    try:
        content, retry_resp = _call_model(client, model, RETRY_TEMPLATE, retry_packed)
    except Exception as exc:
        raise LLMError(
            f"empty response from model; first_finish={_finish_reason(resp)}; "
            f"first_response_id={_response_id(resp)}; retry_error={type(exc).__name__}: {exc}"
        ) from exc
    if _is_complete_report(content):
        return content
    detail = "empty response from model" if not content else "incomplete response from model"
    raise LLMError(
        f"{detail}; first_finish={_finish_reason(resp)}; retry_finish={_finish_reason(retry_resp)}; "
        f"first_response_id={_response_id(resp)}; retry_response_id={_response_id(retry_resp)}; "
        f"packed_items={len(packed)}; retry_items={len(retry_packed)}"
    )
