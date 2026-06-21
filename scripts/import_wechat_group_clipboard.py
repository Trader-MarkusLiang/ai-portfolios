"""Import copied Mac WeChat group messages from clipboard into local inbox.

Usage:
  1. In WeChat, open the whitelisted group and copy selected messages.
  2. Run: python3 scripts/import_wechat_group_clipboard.py "🈲言-2六便士AI吟诗"

This avoids WeChat database/Hook access. Raw group messages stay local by default.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "wechat_groups.yaml"
INBOX_DIR = ROOT / "data" / "wechat_groups" / "inbox"
PROCESSED_DIR = ROOT / "data" / "wechat_groups" / "processed"
SH = ZoneInfo("Asia/Shanghai")


def _load_groups() -> dict[str, dict]:
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    groups = {}
    for item in data.get("wechat_groups") or []:
        if item.get("enabled"):
            groups[item.get("name") or ""] = dict(item)
    return groups


def _clipboard_text() -> str:
    result = subprocess.run(["pbpaste"], text=True, capture_output=True, check=True)
    return result.stdout.strip()


def _message_id(group_name: str, text: str) -> str:
    return hashlib.sha1(f"{group_name}\n{text}".encode("utf-8")).hexdigest()


def _load_seen(group_dir: Path) -> set[str]:
    seen_path = group_dir / "seen.json"
    if not seen_path.exists():
        return set()
    try:
        data = json.loads(seen_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return set(data if isinstance(data, list) else [])


def _save_seen(group_dir: Path, seen: set[str]) -> None:
    (group_dir / "seen.json").write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")


def import_text(group_name: str, text: str) -> Path | None:
    group_dir = INBOX_DIR / group_name
    group_dir.mkdir(parents=True, exist_ok=True)
    seen = _load_seen(group_dir)
    msg_id = _message_id(group_name, text)
    if msg_id in seen:
        print("[OK] duplicate clipboard content, skipped")
        return None
    now = dt.datetime.now(SH)
    path = group_dir / f"{now.strftime('%Y%m%d_%H%M%S')}_{msg_id[:10]}.md"
    path.write_text(
        "\n".join(
            [
                "---",
                f"group: {group_name}",
                f"importedAt: {now.isoformat()}",
                f"messageId: {msg_id}",
                "---",
                "",
                text,
                "",
            ]
        ),
        encoding="utf-8",
    )
    seen.add(msg_id)
    _save_seen(group_dir, seen)
    print(f"[OK] imported -> {path}")
    return path


def main() -> int:
    groups = _load_groups()
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/import_wechat_group_clipboard.py <group-name>", file=sys.stderr)
        print("Enabled groups:", ", ".join(groups), file=sys.stderr)
        return 2
    group_name = sys.argv[1]
    if group_name not in groups:
        print(f"ERROR: group is not enabled: {group_name}", file=sys.stderr)
        return 2
    text = _clipboard_text()
    if not text:
        print("ERROR: clipboard is empty", file=sys.stderr)
        return 2
    import_text(group_name, text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
