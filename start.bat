@echo off
REM Start Discord, Telegram and FamiliarBot on Windows

echo Starting Castopia Bot (Discord + Telegram + FamiliarBot)...

if not exist .env (
    echo .env not found. Copy from .env.example:
    echo   copy .env.example .env
    exit /b 1
)

echo Starting Discord bot...
start "Discord Bot" python dsc/bot.py

timeout /t 2 /nobreak

echo Starting Telegram bot...
start "Telegram Bot" python tg/bot.py

timeout /t 2 /nobreak

echo Starting FamiliarBot...
start "FamiliarBot" python familiarbot/bot.py

echo Bots started in separate windows.
echo To stop them, close those windows.
pause
