@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Antigravity Bot Launcher

:loop
cls
echo ========================================================
echo   Antigravity Bot 起動中...
echo   (コードが更新された場合は、このウィンドウで Ctrl+C を押し、
echo    再度キーを押して再起動してください)
echo ========================================================
echo.

".venv\Scripts\python.exe" main.py

echo.
echo ========================================================
echo   プログラムが停止しました。
echo   何かキーを押すと再起動します。
echo   (終了する場合はこのウィンドウを閉じてください)
echo ========================================================
pause > nul
goto loop
