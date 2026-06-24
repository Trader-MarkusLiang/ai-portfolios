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
import re
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
DEFAULT_TIMEOUT_SECONDS = 45.0

MAX_ITEMS = 10
MAX_RETRY_ITEMS = 6
MAX_TEXT_LEN = 140
MAX_ARCHIVE_TEXT_LEN = 900
MAX_RETRY_TEXT_LEN = 100
MAX_PAYLOAD_CHARS = 2600
MAX_RETRY_PAYLOAD_CHARS = 1600
MAX_OUTPUT_TOKENS = 4096
REQUIRED_SECTIONS = (
    "## 一页决策看板",
    "## 核心结论",
    "## 市场动量图谱",
    "## 主线逻辑链",
    "## 机会矩阵",
    "## 风险雷达",
    "## 证据链摘录",
    "## 明日验证清单",
)

SYSTEM_PROMPT = "你是中文买方投研助手。只输出可解析 JSON，不输出思考过程，不输出 Markdown。"

USER_TEMPLATE = """基于 JSON 输出极简投资判断 JSON。latest 是本次新增，recent 是近几天上下文。只做跨来源整合，不复述流水账。短句，禁止 Markdown，禁止解释。

输出 schema，key 必须使用英文短 key：
{"one":{"temp":"偏热/中性/偏冷/风险升温","risk":"低/中/高","conclusion":"一句话","note":"给普通投资者一句话"},
"calls":[{"t":"主题","d":"强化/降温/反转/待确认","e":"A/B/C/D","map":"国内映射","act":"进攻/观察/等待/回避"}],
"mom":[{"t":"主题","chg":"动量变化","drv":"核心驱动","watch":"观察项"}],
"logic":[{"sig":"原始信号","mech":"产业/资金逻辑","map":"国内映射","con":"结论"}],
"opp":[{"t":"主题","ben":"受益方向","e":"A/B/C/D","crowd":"低/中/高","strat":"仓位态度","bad":"证伪信号"}],
"risk":[{"r":"风险","trig":"触发","imp":"影响","resp":"应对"}],
"ev":[{"s":"A/B/C/D","src":"来源","sum":"证据摘要","url":"URL或本地归档"}],
"next":[{"it":"验证事项","why":"重要性","src":"观察指标/来源"}]}

数量：calls 2-3；mom 3；logic 2；opp 3；risk 2；ev 4；next 3。

JSON:
{{items}}
"""

RETRY_TEMPLATE = """基于 JSON 输出极简投资判断 JSON，只用英文短 key，不要 Markdown。
schema: {"one":{"temp":"","risk":"","conclusion":"","note":""},"calls":[{"t":"","d":"","e":"","map":"","act":""}],"mom":[{"t":"","chg":"","drv":"","watch":""}],"logic":[{"sig":"","mech":"","map":"","con":""}],"opp":[{"t":"","ben":"","e":"","crowd":"","strat":"","bad":""}],"risk":[{"r":"","trig":"","imp":"","resp":""}],"ev":[{"s":"","src":"","sum":"","url":""}],"next":[{"it":"","why":"","src":""}]}
数量：calls 2；mom 2；logic 1；opp 2；risk 2；ev 3；next 3。

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


def _create_completion(client: Any, request: dict[str, Any]) -> Any:
    try:
        return client.chat.completions.create(**request)
    except Exception as exc:
        message = str(exc).lower()
        if "response_format" not in request or "response_format" not in message:
            raise
        fallback = dict(request)
        fallback.pop("response_format", None)
        return client.chat.completions.create(**fallback)


def _call_model(client: Any, model: str, user_template: str, packed: list[dict[str, Any]]) -> tuple[str, Any]:
    payload = json.dumps(packed, ensure_ascii=False, separators=(",", ":"))
    user_prompt = user_template.replace("{{items}}", payload)
    request = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0.2,
    }
    if (os.environ.get("LLM_JSON_MODE") or "1").strip().lower() not in {"0", "false", "no", "off"}:
        request["response_format"] = {"type": "json_object"}
    resp = _create_completion(client, request)
    return (resp.choices[0].message.content or "").strip(), resp


def _safe_text(value: Any, default: str = "待确认") -> str:
    text = str(value or "").strip()
    return text or default


def _safe_rows(value: Any, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows = [row for row in value if isinstance(row, dict)]
    return rows[:limit]


def _json_from_model(content: str) -> dict[str, Any]:
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return {}
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _valid_report_data(data: dict[str, Any]) -> bool:
    required = (
        "one_page",
        "key_calls",
        "momentum_table",
        "logic_chains",
        "opportunity_matrix",
        "risk_radar",
        "evidence",
        "tomorrow",
    )
    if not all(key in data for key in required):
        return False
    return bool(_safe_rows(data.get("key_calls"), 3) and _safe_rows(data.get("evidence"), 6))


def _coerce_report_data(data: dict[str, Any]) -> dict[str, Any]:
    key_calls_raw = data.get("key_calls") or data.get("calls")
    momentum_raw = data.get("momentum_table") or data.get("mom")
    logic_raw = data.get("logic_chains") or data.get("logic")
    opportunities_raw = data.get("opportunity_matrix") or data.get("opp")
    risks_raw = data.get("risk_radar") or data.get("risk")
    evidence_raw = data.get("evidence") or data.get("ev")
    tomorrow_raw = data.get("tomorrow") or data.get("next")
    key_calls_raw = key_calls_raw if isinstance(key_calls_raw, list) else []
    momentum_raw = momentum_raw if isinstance(momentum_raw, list) else []
    logic_raw = logic_raw if isinstance(logic_raw, list) else []
    opportunities_raw = opportunities_raw if isinstance(opportunities_raw, list) else []
    risks_raw = risks_raw if isinstance(risks_raw, list) else []
    evidence_raw = evidence_raw if isinstance(evidence_raw, list) else []
    tomorrow_raw = tomorrow_raw if isinstance(tomorrow_raw, list) else []

    def first_text(value: Any) -> str:
        if isinstance(value, dict):
            return _safe_text(value.get("theme") or value.get("t") or value.get("target") or value.get("name") or value.get("risk") or value.get("r"))
        return _safe_text(value)

    key_calls: list[dict[str, Any]] = []
    for row in key_calls_raw[:3]:
        if isinstance(row, dict):
            key_calls.append(
                {
                    "theme": row.get("theme") or row.get("t"),
                    "direction": row.get("direction") or row.get("d") or "待确认",
                    "evidence": row.get("evidence") or row.get("e") or "C",
                    "china_map": row.get("china_map") or row.get("map") or "A股/港股相关产业链",
                    "action": row.get("action") or row.get("act") or "观察",
                }
            )
        else:
            key_calls.append(
                {
                    "theme": first_text(row),
                    "direction": "待确认",
                    "evidence": "C",
                    "china_map": "A股/港股相关产业链",
                    "action": "观察",
                }
            )

    momentum: list[dict[str, Any]] = []
    for row in momentum_raw[:5]:
        if isinstance(row, dict):
            momentum.append(
                {
                    "theme": row.get("theme") or row.get("t") or row.get("target") or row.get("name"),
                    "change": row.get("change") or row.get("chg") or row.get("trend") or "待确认",
                    "drivers": row.get("drivers") or row.get("drv") or row.get("logic") or row.get("reason"),
                    "watch": row.get("watch") or row.get("next") or "资金响应与基本面验证",
                }
            )

    logic: list[dict[str, Any]] = []
    for row in logic_raw[:3]:
        if isinstance(row, dict):
            logic.append(
                {
                    "signal": row.get("signal") or row.get("sig") or row.get("name") or row.get("theme") or row.get("t"),
                    "mechanism": row.get("mechanism") or row.get("mech") or row.get("chain") or row.get("logic"),
                    "china_map": row.get("china_map") or row.get("map") or "A股/港股相关产业链",
                    "conclusion": row.get("conclusion") or row.get("con") or "进入观察池，等待验证",
                }
            )

    opportunities: list[dict[str, Any]] = []
    for row in opportunities_raw[:5]:
        if isinstance(row, dict):
            opportunities.append(
                {
                    "theme": row.get("theme") or row.get("t") or row.get("target"),
                    "beneficiary": row.get("beneficiary") or row.get("ben") or row.get("target") or "相关产业链",
                    "evidence": row.get("evidence") or row.get("e") or row.get("conviction") or "C",
                    "crowding": row.get("crowding") or row.get("crowd") or "中",
                    "strategy": row.get("strategy") or row.get("strat") or row.get("time_horizon") or "观察",
                    "invalid": row.get("invalid") or row.get("bad") or "主题热度下降且缺少基本面跟进",
                }
            )

    risks: list[dict[str, Any]] = []
    for row in risks_raw[:3]:
        if isinstance(row, dict):
            risks.append(
                {
                    "risk": row.get("risk") or row.get("r"),
                    "trigger": row.get("trigger") or row.get("trig") or row.get("severity") or "待确认",
                    "impact": row.get("impact") or row.get("imp") or "可能造成波动放大",
                    "response": row.get("response") or row.get("resp") or "降低仓位，等待验证",
                }
            )

    evidence: list[dict[str, Any]] = []
    for row in evidence_raw[:6]:
        if isinstance(row, dict):
            evidence.append(
                {
                    "strength": row.get("strength") or row.get("s") or "C",
                    "source": row.get("source") or row.get("src") or "模型提炼",
                    "summary": row.get("summary") or row.get("sum"),
                    "url": row.get("url") or "本地归档",
                }
            )
        else:
            evidence.append({"strength": "C", "source": "模型提炼", "summary": first_text(row), "url": "本地归档"})

    tomorrow: list[dict[str, Any]] = []
    for row in tomorrow_raw[:3]:
        if isinstance(row, dict):
            tomorrow.append(
                {
                    "item": row.get("item") or row.get("it"),
                    "why": row.get("why") or "验证动量是否延续",
                    "source": row.get("source") or row.get("src") or "市场数据/信息源",
                }
            )
        else:
            tomorrow.append({"item": first_text(row), "why": "验证动量是否延续", "source": "市场数据/信息源"})

    one_page = data.get("one_page") or data.get("one")
    one_page = one_page if isinstance(one_page, dict) else {}
    if one_page:
        one_page = {
            "market_temperature": one_page.get("market_temperature") or one_page.get("temp"),
            "risk_level": one_page.get("risk_level") or one_page.get("risk"),
            "core_conclusion": one_page.get("core_conclusion") or one_page.get("conclusion"),
            "investor_note": one_page.get("investor_note") or one_page.get("note"),
        }
    if not one_page:
        one_page = {
            "market_temperature": "中性偏热",
            "risk_level": "中",
            "core_conclusion": first_text(key_calls[0] if key_calls else "暂无强共识主线"),
            "investor_note": "先看证据是否连续强化，不因单条消息追高。",
        }

    coerced = dict(data)
    coerced.update(
        {
            "one_page": one_page,
            "key_calls": key_calls,
            "momentum_table": momentum,
            "logic_chains": logic,
            "opportunity_matrix": opportunities,
            "risk_radar": risks,
            "evidence": evidence,
            "tomorrow": tomorrow,
        }
    )
    return coerced


def _as_markdown_link(url: Any) -> str:
    text = str(url or "").strip()
    if not text:
        return "本地归档"
    if text.startswith(("http://", "https://")):
        return f"[打开]({text})"
    return text


def _render_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        cells = [cell.replace("\n", " ").replace("|", "/").strip() or "待确认" for cell in row]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _render_report_data(data: dict[str, Any]) -> str:
    one_page = data.get("one_page") if isinstance(data.get("one_page"), dict) else {}
    key_calls = _safe_rows(data.get("key_calls"), 3)
    momentum = _safe_rows(data.get("momentum_table"), 5)
    logic = _safe_rows(data.get("logic_chains"), 3)
    opportunities = _safe_rows(data.get("opportunity_matrix"), 5)
    risks = _safe_rows(data.get("risk_radar"), 3)
    evidence = _safe_rows(data.get("evidence"), 6)
    tomorrow = _safe_rows(data.get("tomorrow"), 3)

    lines: list[str] = [
        "## 一页决策看板",
        "",
        "| 指标 | 判断 |",
        "|---|---|",
        f"| 市场温度 | {_safe_text(one_page.get('market_temperature'))} |",
        f"| 风险等级 | {_safe_text(one_page.get('risk_level'))} |",
        f"| 核心结论 | {_safe_text(one_page.get('core_conclusion'))} |",
        f"| 普通投资者提示 | {_safe_text(one_page.get('investor_note'))} |",
        "",
        "## 核心结论",
        "",
    ]
    for row in key_calls:
        lines.append(
            f"- [{_safe_text(row.get('evidence'), 'C')}] {_safe_text(row.get('theme'))}"
            f"：{_safe_text(row.get('direction'))}；国内映射：{_safe_text(row.get('china_map'))}"
            f"；行动：{_safe_text(row.get('action'))}。"
        )

    lines += [
        "",
        "## 市场动量图谱",
        "",
        *_render_table(
            ["主题", "动量变化", "核心驱动", "下一步观察"],
            [
                [
                    _safe_text(row.get("theme")),
                    _safe_text(row.get("change")),
                    _safe_text(row.get("drivers")),
                    _safe_text(row.get("watch")),
                ]
                for row in momentum
            ],
        ),
        "",
        "## 主线逻辑链",
        "",
        *_render_table(
            ["原始信号", "产业/资金逻辑", "国内映射", "投资结论"],
            [
                [
                    _safe_text(row.get("signal")),
                    _safe_text(row.get("mechanism")),
                    _safe_text(row.get("china_map")),
                    _safe_text(row.get("conclusion")),
                ]
                for row in logic
            ],
        ),
        "",
        "## 机会矩阵",
        "",
        *_render_table(
            ["主题", "受益方向", "证据", "拥挤度", "仓位态度", "证伪信号"],
            [
                [
                    _safe_text(row.get("theme")),
                    _safe_text(row.get("beneficiary")),
                    _safe_text(row.get("evidence"), "C"),
                    _safe_text(row.get("crowding")),
                    _safe_text(row.get("strategy")),
                    _safe_text(row.get("invalid")),
                ]
                for row in opportunities
            ],
        ),
        "",
        "## 风险雷达",
        "",
        *_render_table(
            ["风险", "触发条件", "影响", "应对"],
            [
                [
                    _safe_text(row.get("risk")),
                    _safe_text(row.get("trigger")),
                    _safe_text(row.get("impact")),
                    _safe_text(row.get("response")),
                ]
                for row in risks
            ],
        ),
        "",
        "## 证据链摘录",
        "",
    ]
    for row in evidence:
        lines.append(
            f"- [{_safe_text(row.get('strength'), 'C')}] {_safe_text(row.get('source'), '未知来源')}"
            f"：{_safe_text(row.get('summary'))}；链接：{_as_markdown_link(row.get('url'))}"
        )

    lines += [
        "",
        "## 明日验证清单",
        "",
        *_render_table(
            ["验证事项", "为什么重要", "观察指标/来源"],
            [
                [
                    _safe_text(row.get("item")),
                    _safe_text(row.get("why")),
                    _safe_text(row.get("source")),
                ]
                for row in tomorrow
            ],
        ),
        "",
        "---",
        "仅供研究，不构成投资建议。",
    ]
    return "\n".join(lines)


def _normalize_model_report(content: str) -> str:
    data = _json_from_model(content)
    if data:
        data = _coerce_report_data(data)
    if _valid_report_data(data):
        return _render_report_data(data)
    return _normalize_report(content)


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
    next_header = "## 明日验证清单"
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
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=_timeout_seconds(), max_retries=0)

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
        normalized = _normalize_model_report(content)
        if normalized:
            return _ensure_evidence_links(normalized, retry_packed)
        detail = "empty response from model" if not content else "incomplete response from model"
        raise LLMError(
            f"{first_error}; retry_{detail}; retry_finish={_finish_reason(retry_resp)}; "
            f"retry_response_id={_response_id(retry_resp)}; packed_items={len(packed)}; "
            f"retry_items={len(retry_packed)}"
        )

    normalized = _normalize_model_report(content)
    if normalized:
        return _ensure_evidence_links(normalized, packed)

    try:
        content, retry_resp = _call_model(client, model, RETRY_TEMPLATE, retry_packed)
    except Exception as exc:
        raise LLMError(
            f"empty response from model; first_finish={_finish_reason(resp)}; "
            f"first_response_id={_response_id(resp)}; retry_error={type(exc).__name__}: {exc}"
        ) from exc
    normalized = _normalize_model_report(content)
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

    return f"""> ⚠️ LLM 调用失败，以下为规则引擎生成的产品化简报；失败原因：{escaped_error}

## 一页决策看板

| 指标 | 判断 |
|---|---|
| 市场温度 | 中性偏热 |
| 风险等级 | 中 |
| 核心结论 | {primary_theme} 是当前最集中的跨来源线索，{secondary_theme} 是第二观察方向。 |
| 普通投资者提示 | 先看动量是否持续，不因单条消息追高；等待成交量、订单或公告验证。 |

## 核心结论

- [B] {primary_theme}：多来源交叉出现，适合作为国内资本市场优先观察方向；行动：观察/小仓试探。
- [C] {secondary_theme}：需要等待价格、订单、财报或政策催化确认；行动：等待确认。
- [C] 若新增信息较少，本报告使用最近几天上下文判断动量延续，避免单日空窗误判趋势消失。

## 市场动量图谱

| 主题 | 动量变化 | 核心驱动 | 下一步观察 |
|---|---|---|---|
| {primary_theme} | 强化/待确认 | 最新信息与滚动上下文共同出现 | 是否继续跨来源出现并获得资金响应 |
| {secondary_theme} | 待确认 | 主题热度进入观察区 | 是否出现订单、价格或财报催化 |

## 主线逻辑链

| 原始信号 | 产业/资金逻辑 | 国内映射 | 投资结论 |
|---|---|---|---|
| {primary_theme} 相关信息密集出现 | 多来源共振提升主题可信度 | A股/港股产业链龙头、ETF、核心供应商 | 可进入重点观察池 |
| {secondary_theme} 相关信息延续 | 需要基本面证据确认 | 相关设备、材料、应用链 | 等确认后再提高权重 |

## 机会矩阵

| 主题 | 受益方向 | 证据 | 拥挤度 | 仓位态度 | 证伪信号 |
|---|---|---|---|---|---|
| {primary_theme} | 产业链龙头、ETF、核心供应商 | B/C | 中 | 观察/小仓试探 | 证据减少或高位放量回落 |
| {secondary_theme} | 相关港股/A股映射 | C | 中 | 等确认 | 主题热度下降且无基本面跟进 |

## 风险雷达

| 风险 | 触发条件 | 影响 | 应对 |
|---|---|---|---|
| 模型链路异常 | LLM 调用失败或返回不完整 | 分析深度下降 | 使用规则版，只做观察不做重仓依据 |
| 主题拥挤 | 热点只停留在观点层且涨幅过大 | 情绪交易后回撤 | 等订单/价格/业绩验证 |
| 映射错配 | 海外叙事强但国内兑现弱 | A股/港股跟涨失败 | 盯成交额、强弱排序和公司公告 |

## 证据链摘录
{evidence_text}

## 明日验证清单
| 要验证什么 | 为什么重要 | 观察指标/来源 |
|---|---|---|
| 主题是否继续跨来源出现 | 判断动量是否延续 | X/Nitter、微信公众号、微信群归档 |
| 国内映射是否有资金响应 | 判断能否转化为交易机会 | A股/港股成交额、强弱排序、板块涨跌 |
| 是否出现反向证据 | 防止单边叙事误导 | 价格回撤、公司澄清、宏观或监管冲击 |

---
仅供研究，不构成投资建议。"""
