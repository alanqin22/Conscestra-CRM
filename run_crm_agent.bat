@echo off

cd /d "%~dp0"

call venv\Scripts\activate.bat

REM Public tunnel so Telnyx can reach the local SDR voice/SMS webhooks.
REM Reserved domain -> URL never changes, no need to repoint Telnyx.
start "ngrok" ngrok http 8000 --domain=postuterine-lorna-predatorily.ngrok-free.dev

python main.py

pause