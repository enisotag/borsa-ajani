@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ===================================================
echo   Borsa Istanbul (BIST) Analist & Portfoy Ajani
echo ===================================================

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Sanal ortam olusturuluyor...
    python -m venv .venv
    echo [2/3] Gerekli kutuphaneler yukleniyor...
    .venv\Scripts\pip install -r requirements.txt
)

if not exist ".env" (
    echo [!] .env dosyasi bulunamadi! .env.example kopyalaniyor...
    copy .env.example .env >nul
    echo Lutfen .env dosyasini acip TELEGRAM_BOT_TOKEN ve GEMINI_API_KEY degerlerini girin.
    pause
    exit /b 1
)

echo [3/3] Borsa Ajani baslatiliyor... (Durdurmak icin Ctrl+C)
.venv\Scripts\python.exe main.py
pause
