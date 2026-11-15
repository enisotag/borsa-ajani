@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ===================================================
echo   Borsa Istanbul (BIST) Ajani - Arka Plan Durdurucu
echo ===================================================

powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $targets = @(Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and $_.CommandLine -and ($_.CommandLine -like '*borsa-ajani*' -or $_.CommandLine -like '*main.py*') }); $stopped = 0; foreach ($p in $targets) { try { $proc = Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue; if ($proc) { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; $stopped++ } } catch {} }; if ($stopped -eq 0) { Write-Host '[i] Calisan Borsa Ajani sureci bulunamadi.' } else { Write-Host ('[OK] ' + $stopped + ' adet Borsa Ajani sureci durduruldu.') } } catch { Write-Host ('[!] Surecler sorgulanirken hata olustu: ' + $_.Exception.Message); exit 1 }"

ping -n 3 127.0.0.1 >nul
endlocal
