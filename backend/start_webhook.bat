@echo off
REM start_webhook.bat - Windows launcher for DeerFlow webhook receiver + ngrok tunnel
REM
REM Usage:
REM   start_webhook.bat
REM
REM Prereqs (one-time):
REM   1. Install ngrok (already shipped via Microsoft Store in WindowsApps)
REM   2. Create account:         https://dashboard.ngrok.com  -> copy authtoken
REM   3. Register authtoken:     ngrok config add-authtoken <token>
REM   4. Claim free static domain in ngrok dashboard -> Domains -> + New Domain
REM   5. Put the full domain in deer-flow/backend/.env as NGROK_PUBLIC_DOMAIN=<full-domain>
REM
REM Note: zrok was the original choice but its OpenZiti mTLS data plane is blocked by Zscaler
REM       SSL inspection on this corp network. ngrok uses standard HTTPS and works.

setlocal
set "SCRIPT_DIR=%~dp0"
set "VENV_PY=%SCRIPT_DIR%.venv\Scripts\python.exe"
if not defined WEBHOOK_PORT set "WEBHOOK_PORT=8080"

if not exist "%VENV_PY%" (
  echo ERROR: venv python not found at %VENV_PY%
  exit /b 1
)

where ngrok >nul 2>&1
if errorlevel 1 (
  echo ERROR: ngrok not on PATH.
  echo Install via Microsoft Store or download from https://ngrok.com/download
  exit /b 1
)

REM Load NGROK_PUBLIC_DOMAIN from .env if not already set
if not defined NGROK_PUBLIC_DOMAIN (
  for /f "usebackq tokens=1,* delims==" %%a in ("%SCRIPT_DIR%.env") do (
    if /i "%%a"=="NGROK_PUBLIC_DOMAIN" set "NGROK_PUBLIC_DOMAIN=%%b"
  )
)

if not defined NGROK_PUBLIC_DOMAIN (
  echo ERROR: NGROK_PUBLIC_DOMAIN not set.
  echo Claim a free static domain at https://dashboard.ngrok.com/domains
  echo Then add to deer-flow/backend/.env as NGROK_PUBLIC_DOMAIN=<full-domain>
  exit /b 1
)

echo Starting uvicorn on port %WEBHOOK_PORT% ...
start "deerflow-webhook-uvicorn" /D "%SCRIPT_DIR%" "%VENV_PY%" -m uvicorn webhook_receiver:app --host 0.0.0.0 --port %WEBHOOK_PORT% --log-level info

echo Waiting 3s for uvicorn to bind...
timeout /t 3 /nobreak >nul

echo Starting ngrok tunnel to %NGROK_PUBLIC_DOMAIN% ...
echo ===============================================================
echo   Public URL:     https://%NGROK_PUBLIC_DOMAIN%
echo   Gmail endpoint: https://%NGROK_PUBLIC_DOMAIN%/webhook/gmail
echo   Slack endpoint: https://%NGROK_PUBLIC_DOMAIN%/webhook/slack
echo   Health check:   https://%NGROK_PUBLIC_DOMAIN%/health
echo   Local inspect:  http://127.0.0.1:4040
echo ===============================================================
ngrok http --url=%NGROK_PUBLIC_DOMAIN% %WEBHOOK_PORT%

endlocal
