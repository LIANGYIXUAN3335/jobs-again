#!/bin/bash
# 鸭先知周报（owner 2026-09-09 拍板）：每周一 09:30（在 09:00 扫描后）。
#   1. 本地统计出周报 markdown（零 LLM 零联网）
#   2. headless Claude（opus，Max 订阅）联网验证/反驳内部信号，补写「本周风向」一节
#      —— owner 拍板用 opus：每周一次，质量优先（模型分层规则的周报例外）
#   3. 寄给 owner 自己（config trends.report_to；固定自寄，不是外发）
set -euo pipefail
cd /path/to/interview/workspace/jobscan
LOG=data/logs/trend_weekly.log
CLAUDE="$HOME/.local/bin/claude"
echo "===== $(date) weekly trend start =====" >>"$LOG"

REPORT=$(.venv/bin/python -m app.trends --report | tail -1)
echo "report: $REPORT" >>"$LOG"

# 联网验证（失败不拦邮件 —— 纯本地版照发，风向一节缺席而已）
env -u ANTHROPIC_API_KEY "$CLAUDE" -p "你是求职市场分析员。读 $REPORT（鸭先知周报，来自 210 家公司 9000+ 岗位 JD 的私有统计）。用 WebSearch 上网交叉验证其中最重要的 2-3 个信号（比如挂龄榜首的主题是否真缺人、新词雷达对应什么行业动向、发岗突变公司最近发生了什么），也留意报告没捕捉到的重要风向。然后用 Edit 在该文件末尾追加一节「## 本周风向（联网验证）」：300-500 字中文大白话，每个论断带来源链接，先说验证了什么/反驳了什么，最后给 1-2 条给求职者的具体行动建议（读者是湾区 6 年经验后端工程师）。改完即止。" \
    --model opus --allowedTools "Read" "Edit" "WebSearch" "WebFetch" >>"$LOG" 2>&1 \
    || echo "$(date) !! opus web validation failed — mailing local-only report" >>"$LOG"

.venv/bin/python -m app.trends --mail "$REPORT" >>"$LOG" 2>&1
echo "===== $(date) weekly trend done =====" >>"$LOG"
