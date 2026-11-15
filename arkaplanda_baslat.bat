@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ===================================================
echo   Borsa Istanbul (BIST) Ajani - Arka Plan Baslatici
echo ===================================================

if not exist ".venv\Scripts\pythonw.exe" (
    echo [!] .venv\Scripts\pythonw.exe bulunamadi!
    echo Lutfen once baslat.bat dosyasini calistirip kurulumu tamamlayin.
    pause
    exit /b 1
)

if not exist ".env" (
    if exist ".env.example" (
        echo [!] .env dosyasi bulunamadi! .env.example kopyalaniyor...
        copy ".env.example" ".env" >nul
    ) else (
        echo [!] .env dosyasi bulunamadi!
    )
    echo Lutfen .env dosyasini acip TELEGRAM_BOT_TOKEN ve GEMINI_API_KEY degerlerini girin.
    pause
    exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" main.py
echo [OK] Borsa Ajani arka planda sessizce baslatildi.
echo Durdurmak icin durdur.bat dosyasina cift tiklayabilirsiniz.
ping -n 3 127.0.0.1 >nul
endlocal
