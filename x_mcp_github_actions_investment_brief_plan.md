# X/Twitter 只读 MCP + GitHub Actions 投资情报系统设计与执行方案

> 目标：用最低维护成本，实现“每天两次自动读取重点 X/Twitter KOL 与关键词内容，进行市场情绪、消息面、个股关联分析，并推送到 Telegram / 邮件”。

---

## 1. 项目定位

### 1.1 当前阶段目标

本项目不是做高频交易系统，也不是 24 小时实时监听系统，而是一个轻量级投资情报日报系统。

核心目标：

1. 每天自动运行 2 次。
2. 读取重点 KOL、关键词、个股相关推文。
3. 聚合 AI 基础设施、美股成长股、光模块、数据中心、电力、存储等主题信息。
4. 生成结构化中文简报。
5. 通过 Telegram / 邮件自动推送。
6. 全流程尽量使用 GitHub Actions 免费定时任务，不先买 VPS。

### 1.2 不做什么

第一阶段不做：

- 不接入写入权限，不发推、不点赞、不转发。
- 不读取私信。
- 不做 5 分钟级别实时监控。
- 不搭建 PostgreSQL / Redis / Web 后台。
- 不做复杂前端 Dashboard。
- 不依赖本地 Codex 长期开机运行。

---

## 2. 推荐总体架构

```text
GitHub Repository
    ↓
GitHub Actions 定时触发，每天 2 次
    ↓
Python 脚本
    ↓
只读 Twitter/X MCP 或第三方 X 数据接口
    ↓
抓取 KOL / 关键词 / 个股相关内容
    ↓
去重、清洗、分类、情绪打分
    ↓
LLM 总结
    ↓
Telegram / Email 推送
```

### 2.1 Codex 的角色

Codex 不负责长期运行。

Codex 负责：

- 初始化仓库。
- 编写 Python 脚本。
- 配置 GitHub Actions。
- 调试 MCP / API 调用。
- 修改 prompt。
- 优化报告模板。

长期自动运行由 GitHub Actions 完成。

---

## 3. 技术选型

### 3.1 调度层：GitHub Actions

选择原因：

- 支持 cron 定时触发。
- 不需要 VPS。
- 适合每天 2 次这种轻量任务。
- 可以安全保存密钥到 GitHub Secrets。

GitHub Actions 的 `schedule` 使用 POSIX cron，默认按 UTC 时间运行。官方文档说明 scheduled workflows 运行在默认分支最新 commit 上，并且最短间隔是 5 分钟；本项目只需要每天 2 次，远低于限制。

参考：

- https://docs.github.com/actions/using-workflows/workflow-syntax-for-github-actions
- https://docs.github.com/actions/using-workflows/events-that-trigger-workflows
- https://docs.github.com/en/actions/concepts/billing-and-usage

### 3.2 数据层：只读 Twitter/X MCP

优先选择只读型 MCP，而不是官方 X API 直接接入。

候选：

1. twitterapi.io MCP Server
   - 12 个 read-only tools。
   - 支持搜索推文、读取用户资料、读取用户最近推文、读取关注/粉丝等。
   - 不需要标准 X developer account。
   - 但需要 twitterapi.io 的 API Key。

参考：

- https://twitterapi.io/twitter-mcp-server
- https://github.com/kaitoInfra/twitterapi-io-mcp-server

2. Octolens
   - 更适合关键词、品牌、行业、跨平台舆情监控。
   - 可作为第二阶段补充，用于市场情绪与突发消息预警。
   - 不一定适合作为“读取指定 KOL 推文”的唯一数据源。

3. 其他社区 MCP
   - 只选择 read-only 或可关闭写入权限的方案。
   - 避免第一阶段接入发帖、点赞、转推能力。

### 3.3 存储层：第一阶段不用数据库

第一阶段只用本地文件即可：

```text
data/
  raw_tweets_YYYYMMDD_HHMM.json
  normalized_items_YYYYMMDD_HHMM.json
  last_seen.json
reports/
  report_YYYYMMDD_morning.md
  report_YYYYMMDD_evening.md
```

GitHub Actions 每次运行后可以把报告作为 artifact 保存，也可以直接推送到 Telegram。

后续如果需要历史趋势分析，再升级为：

```text
SQLite → PostgreSQL
```

### 3.4 LLM 层

第一阶段推荐：

- OpenAI API：生成质量更稳定，适合最终中文简报。
- 本地 Qwen：可作为第二阶段，用于初筛和分类，但 GitHub Actions 云端环境无法直接调用你本地 Mac 模型，除非你开放接口。

建议先用 OpenAI API 或其他稳定 API 完成 MVP。

---

## 4. 每日运行时间设计

用户需求：每天触发 2 次。

建议时间按英国时间 / 伦敦时间设计：

```text
08:10 Europe/London：早报 / 美股盘后总结
20:10 Europe/London：晚报 / 美股盘前准备
```

GitHub Actions cron 默认使用 UTC。

如果不配置 timezone，则需要换算：

- 英国夏令时 BST = UTC+1。
- 2026 年 6 月在英国为 BST。
- 08:10 London ≈ 07:10 UTC。
- 20:10 London ≈ 19:10 UTC。

推荐 cron：

```yaml
on:
  schedule:
    - cron: '10 7,19 * * *'
  workflow_dispatch:
```

说明：

- `10 7,19 * * *` 表示每天 UTC 07:10 和 19:10 执行。
- 加 `workflow_dispatch` 方便手动测试。
- 避免整点运行，减少 GitHub Actions 高峰延迟。

---

## 5. 监控对象设计

### 5.1 KOL 监控列表

第一批重点 KOL：

```yaml
kol_accounts:
  - name: Serenity
    handle: "待补充"
    weight: 1.2
    tags: ["AI", "market", "infra"]

  - name: 华尔街观察Xtrader
    handle: "待补充"
    weight: 1.1
    tags: ["market", "macro", "US stocks"]

  - name: 潘驴邓晓闲缺一
    handle: "待补充"
    weight: 1.0
    tags: ["market sentiment", "growth stocks"]

  - name: 库哥
    handle: "待补充"
    weight: 1.0
    tags: ["US stocks", "AI"]

  - name: 李志 | Rational Investing
    handle: "待补充"
    weight: 1.2
    tags: ["fundamental", "AI infra", "valuation"]
```

Codex 实现时需要用户补充准确 X handle。

### 5.2 股票池

第一阶段重点股票池：

```yaml
tickers:
  mega_cap_ai:
    - NVDA
    - AVGO
    - TSM
    - ARM
    - AMD
    - MSFT
    - GOOGL
    - AMZN
    - META

  ai_infrastructure:
    - ANET
    - VRT
    - DELL
    - SMCI
    - MU
    - MRVL

  optical_networking:
    - AAOI
    - COHR
    - LITE
    - CIEN
    - INOD

  power_energy:
    - CEG
    - VST
    - NRG
    - ETN
    - PWR
```

后续可把用户真实持仓和自选股池加入 `config/watchlist.yaml`。

### 5.3 关键词池

```yaml
keywords:
  ai_infra:
    - AI infrastructure
    - datacenter
    - data center
    - inference
    - training cluster
    - GPU cluster
    - rack scale
    - liquid cooling

  optical:
    - optical module
    - 800G
    - 1.6T
    - CPO
    - silicon photonics
    - transceiver

  compute:
    - Blackwell
    - Rubin
    - HBM
    - CoWoS
    - ASIC
    - TPU
    - Ethernet AI fabric

  market_sentiment:
    - AI bubble
    - capex
    - guidance
    - earnings revision
    - order backlog
    - hyperscaler capex
```

---

## 6. 报告输出设计

### 6.1 早报模板

```markdown
# AI 基建与美股成长股早报

生成时间：{{timestamp}}
数据区间：{{time_window}}

## 1. 总体市场情绪

- 综合情绪：{{bullish / neutral / bearish}}
- 情绪分数：{{score}} / 100
- 相比上次：{{change}}
- 主要驱动：{{drivers}}

## 2. 重要 KOL 观点变化

| KOL | 核心观点 | 涉及方向 | 情绪 | 重要性 |
|---|---|---|---|---|
| {{kol}} | {{summary}} | {{tags}} | {{sentiment}} | {{importance}} |

## 3. 个股相关信号

| 标的 | 消息/观点 | 来源 | 方向 | 风险等级 |
|---|---|---|---|---|
| NVDA | {{summary}} | {{source}} | {{positive/negative}} | {{risk}} |

## 4. 今日重点观察

1. {{watch_item_1}}
2. {{watch_item_2}}
3. {{watch_item_3}}

## 5. 预警

{{alerts}}
```

### 6.2 晚报模板

```markdown
# AI 基建与美股成长股晚报 / 盘前准备

生成时间：{{timestamp}}

## 1. 盘前核心结论

{{executive_summary}}

## 2. 热度变化

| 主题 | 当前热度 | 变化 | 说明 |
|---|---:|---:|---|
| 光模块 | {{score}} | {{change}} | {{reason}} |
| 数据中心电力 | {{score}} | {{change}} | {{reason}} |

## 3. 重点标的异动信号

{{ticker_signals}}

## 4. KOL 分歧点

{{disagreement}}

## 5. 操作层面提醒

仅做信息提示，不构成投资建议：

- {{risk_1}}
- {{risk_2}}
```

---

## 7. 预警规则设计

### 7.1 高优先级预警

触发条件任一满足：

1. 同一标的在 12 小时内被 3 个以上重点 KOL 提及。
2. 同一主题情绪分数较上次变化超过 25 分。
3. 出现订单、财报、指引、监管、制裁、供应链中断等关键词。
4. 用户持仓标的出现明显负面情绪聚集。
5. 关键词同时命中：ticker + guidance / cut / delay / cancel / investigation。

### 7.2 中优先级预警

1. 某主题讨论量较过去 7 天均值提升 50%。
2. 单个高权重 KOL 明确改变观点。
3. 某股票从低热度突然进入多个 KOL 讨论。

### 7.3 低优先级记录

1. 普通新闻复述。
2. 单一账号无证据观点。
3. 重复情绪表达。

---

## 8. 推荐仓库结构

```text
x-market-brief-agent/
  README.md
  requirements.txt
  .env.example

  config/
    kol_accounts.yaml
    watchlist.yaml
    keywords.yaml
    report_config.yaml

  src/
    main.py
    fetch_x_data.py
    normalize.py
    deduplicate.py
    classify.py
    sentiment.py
    summarize.py
    alert_rules.py
    send_telegram.py
    send_email.py
    utils.py

  prompts/
    system_prompt.md
    morning_report_prompt.md
    evening_report_prompt.md
    alert_prompt.md

  data/
    .gitkeep

  reports/
    .gitkeep

  .github/
    workflows/
      market-brief.yml
```

---

## 9. 环境变量设计

在 GitHub Repository → Settings → Secrets and variables → Actions 中配置：

```text
TWITTERAPI_IO_KEY=xxx
OPENAI_API_KEY=xxx
TELEGRAM_BOT_TOKEN=xxx
TELEGRAM_CHAT_ID=xxx
REPORT_MODE=telegram
```

可选：

```text
EMAIL_SMTP_HOST=xxx
EMAIL_SMTP_PORT=587
EMAIL_USERNAME=xxx
EMAIL_PASSWORD=xxx
EMAIL_TO=xxx
```

`.env.example`：

```env
TWITTERAPI_IO_KEY=
OPENAI_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
REPORT_MODE=telegram
```

---

## 10. GitHub Actions 工作流示例

文件路径：

```text
.github/workflows/market-brief.yml
```

内容：

```yaml
name: X Market Brief

on:
  schedule:
    # UTC 07:10 and 19:10. In UK summer time, roughly 08:10 and 20:10 London time.
    - cron: '10 7,19 * * *'
  workflow_dispatch:
    inputs:
      report_type:
        description: 'morning or evening or auto'
        required: false
        default: 'auto'

jobs:
  market-brief:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt

      - name: Run market brief
        env:
          TWITTERAPI_IO_KEY: ${{ secrets.TWITTERAPI_IO_KEY }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          REPORT_TYPE: ${{ github.event.inputs.report_type || 'auto' }}
        run: |
          python src/main.py

      - name: Upload report artifact
        uses: actions/upload-artifact@v4
        with:
          name: market-brief-report
          path: reports/
```

---

## 11. Python 执行流程设计

`src/main.py` 逻辑：

```python
from fetch_x_data import fetch_all_sources
from normalize import normalize_items
from deduplicate import deduplicate_items
from classify import classify_items
from sentiment import score_sentiment
from alert_rules import generate_alerts
from summarize import generate_report
from send_telegram import send_telegram_message


def main():
    raw_items = fetch_all_sources()
    normalized = normalize_items(raw_items)
    unique_items = deduplicate_items(normalized)
    classified = classify_items(unique_items)
    scored = score_sentiment(classified)
    alerts = generate_alerts(scored)
    report = generate_report(scored, alerts)
    send_telegram_message(report)


if __name__ == "__main__":
    main()
```

---

## 12. Prompt 设计

### 12.1 系统 Prompt

```markdown
你是一个面向美股 AI 基础设施投资研究的中文分析助手。
你的任务是从 X/Twitter KOL、关键词和个股相关信息中，提炼市场情绪、消息面变化、行业趋势和潜在风险。

要求：
1. 不提供确定性投资建议。
2. 明确区分事实、观点、传闻、推测。
3. 对每条重要结论给出来源摘要。
4. 重点关注 AI 基础设施、光模块、数据中心、电力、存储、GPU、ASIC、HBM、CPO 等方向。
5. 输出中文，结论优先，简洁但有判断力。
```

### 12.2 报告生成 Prompt

```markdown
请基于以下结构化数据生成一份中文投资情报简报。

数据：
{{items}}

预警：
{{alerts}}

输出要求：
1. 先给 5 条以内核心结论。
2. 再给市场情绪评分。
3. 按主题分类总结。
4. 标出涉及股票代码。
5. 标出高风险或高关注消息。
6. 最后给“下一次重点观察”。
7. 不要使用夸张语言，不要做买卖建议。
```

---

## 13. Telegram 推送格式

```text
【AI基建市场简报】
时间：2026-xx-xx 08:10 London

核心结论：
1. ...
2. ...
3. ...

情绪：🟢 偏乐观 / 🟡 中性 / 🔴 偏谨慎

重点标的：NVDA / AVGO / TSM / AAOI / VRT

高优先级预警：
- ...

详情：
...
```

---

## 14. Codex 执行任务清单

### 阶段 1：初始化项目

- [ ] 创建 GitHub 仓库 `x-market-brief-agent`。
- [ ] 创建上述目录结构。
- [ ] 编写 `requirements.txt`。
- [ ] 创建 `.env.example`。
- [ ] 创建 `config/*.yaml`。

### 阶段 2：数据读取

- [ ] 接入只读 Twitter/X MCP 或 twitterapi.io API。
- [ ] 实现 `fetch_x_data.py`。
- [ ] 支持读取指定 KOL 最近推文。
- [ ] 支持关键词搜索。
- [ ] 支持按股票代码搜索。

### 阶段 3：清洗与分类

- [ ] 实现去重。
- [ ] 实现语言识别。
- [ ] 实现 ticker/tag 匹配。
- [ ] 实现 KOL 权重。
- [ ] 实现主题分类。

### 阶段 4：总结与预警

- [ ] 接入 LLM。
- [ ] 实现早报和晚报模板。
- [ ] 实现高/中/低优先级预警规则。
- [ ] 输出 Markdown 报告。

### 阶段 5：推送

- [ ] 创建 Telegram Bot。
- [ ] 获取 `TELEGRAM_CHAT_ID`。
- [ ] 实现 `send_telegram.py`。
- [ ] 支持失败重试。

### 阶段 6：GitHub Actions

- [ ] 添加 `.github/workflows/market-brief.yml`。
- [ ] 配置 GitHub Secrets。
- [ ] 手动触发测试。
- [ ] 检查 artifact 报告。
- [ ] 检查 Telegram 是否收到消息。

---

## 15. 风险与注意事项

1. GitHub Actions schedule 不是严格 SLA，可能有延迟；所以不要依赖它做秒级交易预警。
2. 不要把 API Key 写入代码仓库。
3. 不要接入 X 写入权限。
4. 不要第一阶段抓太多账号，建议先从 20–50 个开始。
5. 数据源成本需要关注，尤其是第三方 X 数据接口的调用次数。
6. LLM 生成内容需要标注“仅供研究，不构成投资建议”。
7. 对单一 KOL 观点不要过度放大，必须结合多源确认。

---

## 16. 第一版最小可行产品 MVP

MVP 只做以下功能：

1. 每天两次 GitHub Actions 触发。
2. 读取 5–20 个重点 KOL 最近推文。
3. 搜索 10–20 个关键词。
4. 汇总为中文 Markdown。
5. 发送到 Telegram。

不做数据库，不做网页，不做复杂回测。

MVP 验收标准：

- 手动触发 GitHub Actions 能正常运行。
- 每天自动运行 2 次。
- Telegram 能收到中文简报。
- 简报中包含：核心结论、KOL观点、个股信号、情绪评分、风险预警。
- 无 X 写入权限。

---

## 17. 后续升级路线

### 第二阶段

- 加 SQLite 保存历史数据。
- 统计 7 日/30 日主题热度变化。
- 加入用户持仓权重。
- 加入更多 KOL。
- 加入 Octolens 做跨平台预警。

### 第三阶段

- 部署 VPS。
- PostgreSQL + Docker。
- 每小时监控。
- Web Dashboard。
- 本地 Qwen 初筛 + GPT 深度总结。

---

## 18. 给 Codex 的实现提示

Codex 实现时优先保证：

1. 能跑通，而不是一开始做复杂。
2. 所有密钥走 GitHub Secrets。
3. 所有配置写到 `config/`。
4. 所有 prompt 写到 `prompts/`。
5. 报告必须保存到 `reports/`。
6. Telegram 推送失败时要打印错误日志。
7. X 数据读取必须只读。
8. 先支持手动触发，再启用定时触发。

推荐第一条 Codex 指令：

```text
请根据 docs/x_mcp_github_actions_investment_brief_plan.md 的方案，创建一个 Python 项目，实现 GitHub Actions 每天两次触发，读取只读 X/Twitter 数据源，生成 AI 基建与美股成长股中文简报，并通过 Telegram 推送。先实现 MVP，不要引入数据库，不要接入写入权限。
```

---

## 19. 结论

当前用户需求是每天两次读取 X/Twitter 重点博主与市场信息，进行总结和预警。

最优第一阶段方案是：

```text
GitHub Actions
+ 只读 Twitter/X MCP
+ Python 脚本
+ LLM 总结
+ Telegram 推送
```

这套方案成本低、维护少、实现快，适合交给 Codex 直接搭建 MVP。

