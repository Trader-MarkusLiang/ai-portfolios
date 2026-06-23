"""LLM summarizer.

Uses an OpenAI-compatible client (default: Volcengine Ark coding endpoint)
to turn normalized monitor items into a Chinese investment-research brief.

Env vars:
  ARK_API_KEY     required
  ARK_BASE_URL    default https://ark.cn-beijing.volces.com/api/coding/v3
  ARK_MODEL       default kimi-k2.6
  ARK_TIMEOUT_SECONDS default 75
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
DEFAULT_TIMEOUT_SECONDS = 75.0

MAX_ITEMS = 24
MAX_RETRY_ITEMS = 14
MAX_TEXT_LEN = 260
MAX_ARCHIVE_TEXT_LEN = 900
MAX_RETRY_TEXT_LEN = 180
MAX_PAYLOAD_CHARS = 7000
MAX_RETRY_PAYLOAD_CHARS = 3600
MAX_OUTPUT_TOKENS = 3200
REQUIRED_SECTIONS = (
    "## 市场动量总览",
    "## 动量变化",
    "## 风险雷达",
    "## 交易策略",
    "## 证据链摘录",
    "## 明日验证",
)

SYSTEM_PROMPT = "你是中文买方投研助手。直接输出最终报告，不输出思考过程。"

USER_TEMPLATE = """请基于下面 JSON 生成中文《全球投资动能监控》日报。目标是从全球信息中提炼对中国国内资本市场有领先意义的市场动量、风险和交易策略。不要复述流水账，要做交叉整合。`contextRole=latest` 是本次最新抓取，`contextRole=recent` 是最近几天滚动上下文；请用 recent 作为基准，判断 latest 是否强化、削弱或反转原有动量。控制在 650-900 中文字，必须完整收尾。

固定结构：
## 市场动量总览
- 2到3条最重要动量结论；写清方向、证据强度[A/B/C/D]、国内映射。

## 动量变化
- 2到3条“强化 / 降温 / 反转 / 待确认”变化。

## 风险雷达
- 2到3条；说明风险触发条件、若判断错会错在哪里。

## 交易策略
| 策略主题 | 国内映射 | 仓位态度 | 触发条件 | 风控信号 |
|---|---|---|---|---|

## 证据链摘录
- 4到6条精选证据。格式：`[强度] 来源：一句话摘要；链接：URL`。没有链接则写本地归档。

## 明日验证
| 要验证什么 | 为什么重要 | 观察指标/来源 |
|---|---|---|

---
仅供研究，不构成投资建议。

JSON:
{{items}}
"""

RETRY_TEMPLATE = """请基于 JSON 生成一份 450-700 字中文投资分析日报。只输出 Markdown 正文，不要推理过程，必须完整收尾。每个小节最多 3 行。

结构必须包含：
## 市场动量总览
## 动量变化
## 风险雷达
## 交易策略
## 证据链摘录
## 明日验证
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
    if item.get("contextRole") == "latest":
        priority += 40_000
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
        "contextRole": item.get("contextRole") or "latest",
        "contextDate": item.get("contextDate") or "",
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


def _timeout_seconds() -> float:
    raw = (os.environ.get("ARK_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        return max(10.0, min(float(raw), 180.0))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


def _has_required_sections(content: str) -> bool:
    return all(section in content for section in REQUIRED_SECTIONS)


def _normalize_report(content: str) -> str:
    content = content.strip()
    if len(content) < 350:
        return ""
    if not _has_required_sections(content):
        return ""
    if "仅供研究，不构成投资建议" not in content:
        content = content.rstrip() + "\n\n---\n仅供研究，不构成投资建议。"
    return content


def _ensure_evidence_links(content: str, records: list[dict[str, Any]]) -> str:
    evidence_header = "## 证据链摘录"
    next_header = "## 明日验证"
    if evidence_header not in content or "链接：" in content:
        return content
    start = content.find(evidence_header)
    end = content.find(next_header, start)
    if end == -1:
        return content
    lines = _evidence_lines(records, limit=4)
    insert = evidence_header + "\n" + "\n".join(lines) + "\n"
    return content[:start] + insert + content[end:]


def _is_complete_report(content: str) -> bool:
    if len(content) < 350:
        return False
    if "仅供研究，不构成投资建议" not in content:
        return False
    return _has_required_sections(content)


def summarize(items: list[dict[str, Any]]) -> str:
    if OpenAI is None:
        raise LLMError("openai package not installed")
    api_key = os.environ.get("ARK_API_KEY", "").strip()
    if not api_key:
        raise LLMError("ARK_API_KEY is not set")

    base_url = (os.environ.get("ARK_BASE_URL") or DEFAULT_BASE_URL).strip()
    model = (os.environ.get("ARK_MODEL") or DEFAULT_MODEL).strip()
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=_timeout_seconds())

    packed = _pack(
        items,
        max_items=MAX_ITEMS,
        max_text_len=MAX_TEXT_LEN,
        max_payload_chars=MAX_PAYLOAD_CHARS,
    )
    retry_packed = _pack(
        items,
        max_items=MAX_RETRY_ITEMS,
        max_text_len=MAX_RETRY_TEXT_LEN,
        max_payload_chars=MAX_RETRY_PAYLOAD_CHARS,
    )
    try:
        content, resp = _call_model(client, model, USER_TEMPLATE, packed)
    except Exception as exc:  # network / auth / model errors
        first_error = f"{type(exc).__name__}: {exc}"
        try:
            content, retry_resp = _call_model(client, model, RETRY_TEMPLATE, retry_packed)
        except Exception as retry_exc:
            raise LLMError(
                f"{first_error}; retry_error={type(retry_exc).__name__}: {retry_exc}; "
                f"packed_items={len(packed)}; retry_items={len(retry_packed)}"
            ) from retry_exc
        normalized = _normalize_report(content)
        if normalized:
            return _ensure_evidence_links(normalized, retry_packed)
        detail = "empty response from model" if not content else "incomplete response from model"
        raise LLMError(
            f"{first_error}; retry_{detail}; retry_finish={_finish_reason(retry_resp)}; "
            f"retry_response_id={_response_id(retry_resp)}; packed_items={len(packed)}; "
            f"retry_items={len(retry_packed)}"
        )

    normalized = _normalize_report(content)
    if normalized:
        return _ensure_evidence_links(normalized, packed)

    try:
        content, retry_resp = _call_model(client, model, RETRY_TEMPLATE, retry_packed)
    except Exception as exc:
        raise LLMError(
            f"empty response from model; first_finish={_finish_reason(resp)}; "
            f"first_response_id={_response_id(resp)}; retry_error={type(exc).__name__}: {exc}"
        ) from exc
    normalized = _normalize_report(content)
    if normalized:
        return _ensure_evidence_links(normalized, retry_packed)
    detail = "empty response from model" if not content else "incomplete response from model"
    raise LLMError(
        f"{detail}; first_finish={_finish_reason(resp)}; retry_finish={_finish_reason(retry_resp)}; "
        f"first_response_id={_response_id(resp)}; retry_response_id={_response_id(retry_resp)}; "
        f"packed_items={len(packed)}; retry_items={len(retry_packed)}"
    )


THEME_KEYWORDS = {
    "AI算力链": ("AI", "算力", "GPU", "ASIC", "TPU", "CPO", "光模块", "数据中心"),
    "存储/HBM": ("DRAM", "HBM", "CXL", "存储", "美光", "海力士", "Siri", "苹果"),
    "半导体设备材料": ("半导体", "先进封装", "CoPoS", "台积电", "MLCC", "封装"),
    "港股/中概成长": ("港股", "恒生", "中概", "03121", "03119", "加仓", "加cang"),
    "机器人/端侧AI": ("机器人", "具身", "端侧", "手机", "Siri", "苹果AI"),
    "宏观风险": ("降息", "通胀", "美元", "利率", "关税", "地缘", "风险"),
}


def _theme_counts(records: list[dict[str, Any]], role: str | None = None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        if role and record.get("contextRole") != role:
            continue
        text = f"{record.get('title') or ''} {record.get('text') or ''}"
        for theme, keywords in THEME_KEYWORDS.items():
            if any(keyword in text for keyword in keywords):
                counts[theme] = counts.get(theme, 0) + 1
    return counts


def _top_themes(records: list[dict[str, Any]]) -> list[tuple[str, int]]:
    counts = _theme_counts(records)
    return sorted(counts.items(), key=lambda item: item[1], reverse=True)[:4] or [("暂无高一致性主题", 0)]


def _evidence_lines(records: list[dict[str, Any]], limit: int = 6) -> list[str]:
    selected = sorted(
        records,
        key=lambda record: (
            1 if record.get("contextRole") == "latest" else 0,
            _to_int(record.get("heat")),
            len(record.get("text") or ""),
        ),
        reverse=True,
    )[:limit]
    lines: list[str] = []
    for record in selected:
        source = record.get("source") or "未知来源"
        text = (record.get("text") or record.get("title") or "").strip()
        if len(text) > 90:
            text = text[:90] + "…"
        url = record.get("url") or "本地归档"
        strength = "B" if record.get("contextRole") == "latest" else "C"
        lines.append(f"- [{strength}] {source}：{text}；链接：{url}")
    return lines or ["- [D] 暂无可用证据；链接：本地归档"]


def fallback_summary(items: list[dict[str, Any]], error: str) -> str:
    records = _pack(
        items,
        max_items=MAX_ITEMS,
        max_text_len=MAX_TEXT_LEN,
        max_payload_chars=MAX_PAYLOAD_CHARS,
    )
    latest_counts = _theme_counts(records, "latest")
    recent_counts = _theme_counts(records, "recent")
    top_themes = _top_themes(records)
    primary_theme = top_themes[0][0]
    secondary_theme = top_themes[1][0] if len(top_themes) > 1 else "相关产业链"
    evidence = _evidence_lines(records)

    changes: list[str] = []
    for theme, count in top_themes:
        latest_count = latest_counts.get(theme, 0)
        recent_count = recent_counts.get(theme, 0)
        if latest_count and recent_count:
            label = "强化"
        elif latest_count:
            label = "新出现/待确认"
        elif recent_count:
            label = "延续但本次新增不足"
        else:
            label = "待确认"
        changes.append(f"- {label}：{theme} 出现 {count} 条相关信号；需要用成交量、产业新闻和公司公告继续验证。")

    change_text = "\n".join(changes[:4])
    evidence_text = "\n".join(evidence)
    escaped_error = error.replace("\n", " ")[:220]

    return f"""> ⚠️ LLM 调用失败，以下为规则引擎生成的动量简报；失败原因：{escaped_error}

## 市场动量总览
- 当前最集中的线索是 **{primary_theme}**，信号来自最新抓取与最近上下文的交叉出现，适合作为国内资本市场的优先观察方向，证据强度 B/C。
- 第二层线索是 **{secondary_theme}**，更适合等待价格、订单、政策或产业事件确认后再提高权重，证据强度 C。
- 若新增信息较少，本报告优先使用最近几天的上下文判断动量延续性，避免因为单日空窗误判趋势消失。

## 动量变化
{change_text}

## 风险雷达
- 模型链路异常会降低文本理解深度，本版只做关键词和来源强度聚合，不能替代完整投研判断。
- 若热门主题只停留在观点层、缺少订单/价格/业绩验证，容易形成情绪交易后的回撤。
- 对国内映射要重点防范“海外叙事强、A股兑现弱”的错配，尤其是拥挤赛道和短期涨幅过高标的。

## 交易策略
| 策略主题 | 国内映射 | 仓位态度 | 触发条件 | 风控信号 |
|---|---|---|---|---|
| {primary_theme} | 产业链龙头、ETF、核心供应商 | 观察/小仓试探 | 多来源继续强化且价格放量 | 证据减少或高位放量回落 |
| {secondary_theme} | 相关港股/A股映射 | 等确认 | 出现订单、价格、财报或政策催化 | 主题热度下降且无基本面跟进 |

## 证据链摘录
{evidence_text}

## 明日验证
| 要验证什么 | 为什么重要 | 观察指标/来源 |
|---|---|---|
| 主题是否继续跨来源出现 | 判断动量是否延续 | X/Nitter、微信公众号、微信群归档 |
| 国内映射是否有资金响应 | 判断能否转化为交易机会 | A股/港股成交额、强弱排序、板块涨跌 |
| 是否出现反向证据 | 防止单边叙事误导 | 价格回撤、公司澄清、宏观或监管冲击 |

---
仅供研究，不构成投资建议。"""
