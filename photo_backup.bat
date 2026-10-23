@echo off
rem ============================================================
rem  Android 写真バックアップ＆軽量化ツール 起動用ランチャー
rem  このファイルは CRLF(Windows 改行)で保存すること。
rem  LF のみだと cmd.exe が正しく解釈できず起動しない。
rem ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist "%~dp0photo_backup.py" goto :no_script

rem --- Python を探す(実際に起動できるかまで確認する) ---
set "PY="
python -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=python"

if not defined PY (
    py -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)

if not defined PY goto :no_python

rem --- 実行 ---
%PY% "%~dp0photo_backup.py"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :failed

endlocal
exit /b 0

:no_script
echo [エラー] photo_backup.py が見つかりません。
echo この bat ファイルと同じフォルダに photo_backup.py を置いてください。
pause
endlocal
exit /b 1

:no_python
echo [エラー] 利用できる Python が見つかりません。
echo Python 3.10 以上をインストールし、インストーラの
echo 「Add Python to PATH」にチェックを入れてから再実行してください。
echo   https://www.python.org/downloads/
pause
endlocal
exit /b 1

:failed
echo.
echo [エラー] 終了コード %RC% で停止しました。上のメッセージを確認してください。
pause
endlocal
exit /b %RC%
