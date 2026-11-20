# Devin bot: nightly crash → test → fix

Built for this repo (Dusk: Telegram assistant + Obsidian vault, Python, 67 pytest files).
Pitched to Poke for the Devin Bots launch on 2026-11-20 by the owner of this repository.

## Why

Most of Dusk runs unattended: reminder and focus schedulers, thought digests, the SQLite
delivery queue in `bot/runtime_store.py`. When one of those jobs crashes at night, nothing
is visible — a reminder simply never arrives. The bot closes that gap.

## What it does, every morning

1. Reads the overnight error log of the running bot.
2. Picks the newest unique crash (dedup by exception type + top frame in `bot/`).
3. Writes a pytest test that reproduces it and confirms the test fails on `main`.
4. Fixes it on a branch, runs the full suite.
5. Sends the PR link to Telegram with a 3-line summary: crash, cause, fix.

## Hard rules

- The new test must fail before the fix and pass after it. No red-to-green proof → no PR.
- Max 3 files changed. Over that → abort and report.
- Never edits `.env`, keys, deploy scripts or existing assertions.
- Never deletes or skips a test to get green.
- Never merges itself.
- Can't reproduce the crash → reports what it tried instead of guessing a fix.

## Success for month one

5 merged PRs that needed no manual edits, each with its regression test.
