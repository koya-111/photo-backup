@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

rem Python の場所を探す(python / py ランチャーのどちらか)
set "PY=python"
where python >nul 2>nul
if errorlevel 1 (
    where py >nul 2>nul
    if errorlevel 1 (
        echo [エラー] Python が見つかりません。
        echo Python 3.10 以上をインストールしてから再度実行してください。
        pause
        exit /b 1
    )
    set "PY=py -3"
)

%PY% photo_backup.py
if errorlevel 1 (
    echo.
    echo 上記にエラーが表示されている場合は、内容を確認してください。
    pause
)

endlocal
