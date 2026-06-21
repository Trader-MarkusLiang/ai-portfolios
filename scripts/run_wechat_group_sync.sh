#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GROUP_NAME="${GROUP_NAME:-🈲言-2六便士AI吟诗}"

cd "$ROOT"

python3 scripts/capture_wechat_group_visible.py "$GROUP_NAME"
python3 scripts/process_wechat_group_inbox.py

if [[ "${NO_GIT:-0}" == "1" ]]; then
  exit 0
fi

git add data/wechat_groups/summaries/
if ! git diff --cached --quiet; then
  git commit -m "chore(wechat): sync group summary [skip ci]"
  git push origin main
else
  echo "[OK] no group summary changes to commit"
fi
