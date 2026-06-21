# ai-portfolios / x-market-brief

每天 1 次自动读取 X/Twitter 重点 KOL 的**新增推文**，生成中文投资情报日报，并推送到 Discord 频道。

详细方案见 [`x_mcp_github_actions_investment_brief_plan.md`](./x_mcp_github_actions_investment_brief_plan.md)。

## 抓取策略

- **新关注的账号**（`data/last_seen.json` 里无记录）：回溯过去 7 天的推文。
- **已追踪的账号**：只抓 id 大于 `last_tweet_id` 的增量推文（≈ 过去 24 小时）。
- 每次运行结束写回 `data/last_seen.json` 并 commit 回仓库，作为下次运行的基线。

## 触发时间

- 默认每天**北京时间 07:00**自动跑（GitHub Actions cron `0 23 * * *` UTC）。
- 也可在 Actions 页面手动 `Run workflow`。

## 链路

```
twitterapi.io (read-only) → Python (增量过滤) → reports/*.md → Discord Webhook
                                            ↓
                                   data/last_seen.json (commit 回仓)
```

## 本地运行

1. 装依赖：
   ```bash
   python3.11 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. 复制 `.env.example` 为 `.env`，填入 `TWITTERAPI_IO_KEY` 和 `DISCORD_WEBHOOK_URL`。
3. 验证 key：
   ```bash
   python scripts/ping_twitterapi.py elonmusk
   ```
4. 生成日报：
   ```bash
   python -m src.main
   ```
5. 推送到 Discord（可选）：
   ```bash
   python scripts/send_discord.py reports/report_YYYYMMDD.md --title "Local Test"
   ```

> macOS 本地若开了 Clash/类似代理（默认 `127.0.0.1:7893`），可能需要 `export HTTPS_PROXY=http://127.0.0.1:7893` 才能直连 `api.twitterapi.io`。GitHub Actions 不需要代理。

## GitHub Actions Secrets

仓库 Settings → Secrets and variables → Actions：

- `TWITTERAPI_IO_KEY`
- `DISCORD_WEBHOOK_URL`

## KOL 配置

编辑 `config/kol_accounts.yaml`，`handle` 字段写真实 X 用户名（不带 `@`）。
没填 handle 的条目会被跳过。

## twitterapi.io 免费层注意

- QPS：1 次 / 5 秒。代码层面已用 5.5 秒间隔节流并自动重试 429。
- 单次抓取：新账号 100 条、已追踪账号 40 条；按 5 个 KOL 算每天约 5 次请求，远低于额度。

## 目录结构

```
config/   监控配置（KOL / 关键词 / 股票池）
src/      主流程（抓取、增量过滤、报告生成）
scripts/  小工具（ping、Discord 推送）
prompts/  LLM prompt（后续阶段）
data/     last_seen.json + 原始抓取 raw_*.json（raw 被 .gitignore 忽略）
reports/  生成的 markdown 报告（被 .gitignore 忽略）
.github/workflows/  定时任务
```

## 后续路线

- 接入 LLM（OpenAI）做中文总结、情绪打分、分类。
- 加入关键词搜索（光模块 / 800G / CPO / 算力）+ 个股代码搜索。
- 预警规则（高/中/低优先级）。
