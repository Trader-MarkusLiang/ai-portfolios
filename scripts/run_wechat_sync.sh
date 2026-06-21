#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! curl -fsS --max-time 5 http://127.0.0.1:4010 >/dev/null; then
  echo "ERROR: WeWe RSS is not reachable at http://127.0.0.1:4010" >&2
  echo "Start it with: scripts/start_wewe_rss.sh" >&2
  exit 1
fi

/usr/bin/python3 scripts/sync_wechat_articles.py "$@"
