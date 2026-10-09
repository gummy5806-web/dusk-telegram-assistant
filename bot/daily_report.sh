#!/bin/bash
cd /root/dusk-vault
git pull --rebase 2>/dev/null

source .env

REPORT=$(source bot/venv/bin/activate && claude --print -p "Generate a concise daily report for Telegram. Include: tasks completed today, pending tasks by priority, top 3 suggestions for tomorrow. Format for mobile reading. Keep under 2000 chars." 2>/dev/null)

curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage"   -d chat_id="${TELEGRAM_USER_ID}"   -d text="📊 Daily Report $(date +%Y-%m-%d)

${REPORT}" > /dev/null
