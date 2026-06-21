"""Capture visible Mac WeChat group messages through UI automation."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from import_wechat_group_clipboard import _load_groups, import_text  # noqa: E402


APPLESCRIPT = r'''
on run argv
  set groupName to item 1 of argv
  tell application "WeChat" to activate
  delay 1.0
  tell application "System Events"
    tell process "WeChat"
      set frontmost to true
      set theWindow to front window
      set {wx, wy} to position of theWindow
      set {ww, wh} to size of theWindow
    end tell

    set the clipboard to groupName
    click at {wx + 90, wy + 22}
    delay 0.2
    keystroke "a" using command down
    delay 0.1
    keystroke "v" using command down
    delay 0.8
    key code 36
    delay 1.0
    key code 53
    delay 0.3

    tell process "WeChat"
      set theWindow to front window
      set {wx, wy} to position of theWindow
      set {ww, wh} to size of theWindow
    end tell
    click at {wx + (ww div 2), wy + (wh div 2)}
    delay 0.2
    keystroke "a" using command down
    delay 0.2
    keystroke "c" using command down
    delay 0.8
  end tell
end run
'''


def _pbpaste() -> str:
    return subprocess.run(["pbpaste"], text=True, capture_output=True, check=True).stdout


def _pbcopy(text: str) -> None:
    subprocess.run(["pbcopy"], text=True, input=text, check=True)


def _run_osascript(group_name: str) -> None:
    result = subprocess.run(["osascript", "-e", APPLESCRIPT, group_name], text=True, capture_output=True)
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        if "不允许辅助访问" in message or "not allowed assistive access" in message:
            raise RuntimeError(
                "macOS blocked UI automation. Open 系统设置 → 隐私与安全性 → 辅助功能, "
                "then allow Terminal/Codex/osascript to control WeChat."
            )
        raise RuntimeError(message or f"osascript failed with exit code {result.returncode}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture visible Mac WeChat group messages.")
    parser.add_argument("group_name", help="Exact whitelisted WeChat group name.")
    parser.add_argument("--keep-clipboard", action="store_true", help="Do not restore previous clipboard content.")
    parser.add_argument("--min-chars", type=int, default=20, help="Minimum copied text length to import.")
    args = parser.parse_args()

    groups = _load_groups()
    if args.group_name not in groups:
        print(f"ERROR: group is not enabled: {args.group_name}", file=sys.stderr)
        print("Enabled groups:", ", ".join(groups), file=sys.stderr)
        return 2

    previous_clipboard = _pbpaste()
    try:
        _run_osascript(args.group_name)
        captured = _pbpaste().strip()
        if len(captured) < args.min_chars:
            print("ERROR: copied WeChat text is too short; capture likely failed", file=sys.stderr)
            return 1
        if captured == previous_clipboard.strip():
            print("ERROR: clipboard did not change; capture likely failed", file=sys.stderr)
            return 1
        import_text(args.group_name, captured)
        return 0
    finally:
        if not args.keep_clipboard:
            _pbcopy(previous_clipboard)


if __name__ == "__main__":
    raise SystemExit(main())
