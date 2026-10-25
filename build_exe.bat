@echo off
rem ============================================================
rem  PhotoBackup を単体の exe にまとめるビルド用スクリプト
rem  ・このファイルは CRLF(Windows 改行)で保存すること
rem  ・出力: dist\PhotoBackup.exe
rem ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

python -c "import PyInstaller" >nul 2>nul
if errorlevel 1 goto :no_pyinstaller

echo ビルドを開始します（数分かかります）...
python -m PyInstaller --noconfirm --clean photo_backup.spec
if errorlevel 1 goto :failed

echo.
echo 完了しました: "%~dp0dist\PhotoBackup.exe"
echo ※ FFmpeg は同梱していません。動画圧縮を使う場合は別途導入してください。
pause
endlocal
exit /b 0

:no_pyinstaller
echo [エラー] PyInstaller が見つかりません。
echo 次のコマンドで導入してください:
echo     pip install -r requirements-build.txt
pause
endlocal
exit /b 1

:failed
echo.
echo [エラー] ビルドに失敗しました。上のメッセージを確認してください。
pause
endlocal
exit /b 1
