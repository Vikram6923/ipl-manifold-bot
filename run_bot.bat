@echo off
REM ─────────────────────────────────────────────────────────────────
REM  IPL 2026 Manifold Bot — Windows daily runner
REM
REM  Schedule with Windows Task Scheduler:
REM    1. Open Task Scheduler → Create Basic Task
REM    2. Trigger: Daily, 9:00 AM (or your preferred time)
REM    3. Action: Start a program → browse to this .bat file
REM    4. "Start in" folder: C:\Users\HP\OneDrive\Desktop\Betting Bot\Manifold betting bot
REM ─────────────────────────────────────────────────────────────────

cd /d "C:\Users\HP\OneDrive\Desktop\Betting Bot\Manifold betting bot"

echo [%date% %time%] Running IPL Manifold Bot... >> scheduler.log
python ipl_bot.py run >> scheduler.log 2>&1
echo [%date% %time%] Bot run complete. >> scheduler.log
