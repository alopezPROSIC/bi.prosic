@echo off
rem Asistente de configuracion de PROSIC BI (Windows). Crea .env y opcionalmente levanta docker compose.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
