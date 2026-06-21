"""Daily incremental brief.

Logic:
- For each configured handle, look up its last seen tweet id in
  data/last_seen.json.
- New handle (no record): fetch ~7 days worth of tweets.
- Tracked handle: fetch the most recent batch and keep only tweets
  with id > last_seen_id (incremental).
- Render markdown report and update last_seen.json so the next run
  only sees fresh tweets.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
STATE_PATH = DATA_DIR / "last_seen.json"

SH = ZoneInfo("Asia/Shanghai")

NEW_ACCOUNT_LOOKBACK_DAYS = 7
NEW_ACCOUNT_FETCH_COUNT = 100   # pulled once per new account
TRACKED_FETCH_COUNT = 40        # enough to cover a daily window

if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))
    from src.fetch_x_data import (  # type: ignore
        TwitterAPIError,
        filter_new_tweets,
        get_user_last_tweets,
    )
    from src.state import load_state, save_state, update_handle  # type: ignore
else:
    from .fetch_x_data import TwitterAPIError, filter_new_tweets, get_user_last_tweets
    from .state import load_state, save_state, update_handle


def _load_handles() -> list[str]:
    path = CONFIG_DIR / "kol_accounts.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    handles: list[str] = []
    for item in data.get("kol_accounts", []) or []:
        handle = (item.get("handle") or "").strip()
        if handle:
            handles.append(handle)
    return handles


def _parse_created_at(s: str) -> dt.datetime | None:
    # twitterapi.io style: "Sun Jun 21 06:13:10 +0000 2026"
    if not s:
        return None
    try:
        return dt.datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        try:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None


def _fmt_tweet(t: dict) -> str:
    text = (t.get("text") or "").replace("\n", " ").strip()
    if len(text) > 260:
        text = text[:260] + "…"
    created = t.get("createdAt", "")
    url = t.get("url") or t.get("twitterUrl") or ""
    likes = t.get("likeCount", 0)
    rts = t.get("retweetCount", 0)
    return f"  - [{created}] {text}  (♥{likes} / 🔁{rts}) {url}"


def _render_markdown(now_sh: dt.datetime, results: list[dict]) -> str:
    lines = [
        "# AI 基建与美股成长股日报",
        "",
        f"生成时间：{now_sh.strftime('%Y-%m-%d %H:%M')} Asia/Shanghai",
        "窗口：过去 24 小时（新账号回溯 7 天）",
        "",
        "## KOL 新增推文",
        "",
    ]
    total_new = 0
    if not results:
        lines.append("- 当前未配置任何 KOL handle，请在 `config/kol_accounts.yaml` 中补充。")
    else:
        for item in results:
            handle = item.get("handle")
            if "error" in item:
                lines.append(f"- @{handle} : 抓取失败 ({item['error']})")
                continue
            new_tweets = item.get("new_tweets") or []
            mode = item.get("mode")
            total_new += len(new_tweets)
            tag = "新账号 / 回溯 7 天" if mode == "bootstrap" else "增量"
            lines.append(f"- @{handle} : 新增 {len(new_tweets)} 条（{tag}）")
            for t in new_tweets:
                lines.append(_fmt_tweet(t))
    lines += [
        "",
        f"合计新增：{total_new} 条。",
        "",
        "## 备注",
        "",
        "- 链路打通版本，尚未接入 LLM 总结。",
        "- 下一阶段：去重已通过 last_seen 完成，将加入分类、情绪、个股关联、预警与中文总结。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    now_sh = dt.datetime.now(SH)
    now_utc = dt.datetime.now(dt.timezone.utc)
    bootstrap_cutoff = now_utc - dt.timedelta(days=NEW_ACCOUNT_LOOKBACK_DAYS)

    DATA_DIR.mkdir(exist_ok=True)
    REPORTS_DIR.mkdir(exist_ok=True)

    handles = _load_handles()
    state = load_state(STATE_PATH)

    results: list[dict] = []
    for handle in handles:
        record = state.get(handle) or {}
        last_id = record.get("last_tweet_id")
        mode = "incremental" if last_id else "bootstrap"
        count = TRACKED_FETCH_COUNT if last_id else NEW_ACCOUNT_FETCH_COUNT

        try:
            raw = get_user_last_tweets(handle, count=count)
        except TwitterAPIError as exc:
            results.append({"handle": handle, "error": str(exc), "mode": mode})
            continue

        if last_id:
            new_tweets = filter_new_tweets(raw, last_id)
        else:
            # bootstrap: keep tweets within last N days
            new_tweets = []
            for t in raw:
                created = _parse_created_at(t.get("createdAt", ""))
                if created and created >= bootstrap_cutoff:
                    new_tweets.append(t)
            new_tweets.sort(key=lambda t: int(t.get("id") or 0))

        if new_tweets:
            newest_id = str(max(int(t.get("id") or 0) for t in new_tweets))
            update_handle(state, handle, newest_id, now_sh.isoformat())
        elif not last_id and raw:
            # no tweets in window but we did see the account; pin newest id
            newest_id = str(max(int(t.get("id") or 0) for t in raw))
            update_handle(state, handle, newest_id, now_sh.isoformat())

        results.append({"handle": handle, "mode": mode, "new_tweets": new_tweets, "fetched": len(raw)})

    save_state(STATE_PATH, state)

    raw_path = DATA_DIR / f"raw_{now_sh.strftime('%Y%m%d_%H%M')}.json"
    raw_path.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    md = _render_markdown(now_sh, results)
    report_path = REPORTS_DIR / f"report_{now_sh.strftime('%Y%m%d')}.md"
    report_path.write_text(md, encoding="utf-8")

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"report_path={report_path.as_posix()}\n")

    print(f"[OK] report -> {report_path}")
    print(f"[OK] raw    -> {raw_path}")
    print(f"[OK] state  -> {STATE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
