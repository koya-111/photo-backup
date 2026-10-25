# photo-backup

Android スマホの写真・動画を PC に高画質でバックアップし、スマホへ戻すための軽量版を自動生成する Windows 向けツールです。スマホの空き容量を確保することを目的としています。

## 特徴

- **原本を保全**: 取り込んだファイルは無劣化のまま `Original` へコピー
- **軽量版を自動生成**: 長辺 1200px / JPEG 品質 80 に縮小して `Mobile` へ出力（画質は画面で変更可）
- **保存先を個別指定**: 原本・軽量版・ログ／重複DBをそれぞれ別フォルダ・別ドライブに置ける
- **重複スキップ（任意）**: 画面のスイッチでオンにすると重複をスキップ。方式は「pHash（見た目）」「ファイル名」「両方」から選べ、pHash は閾値もその場で調整できる。既定はオフ
- **撮影日時で整理**: `YYYY\MM` などの階層へ自動振り分け（写真は EXIF、動画は ffprobe → ファイル名 → 更新日時）
- **サブフォルダ対応**: `DCIM\Camera` ごとコピーしても再帰的に処理
- **動画圧縮（任意）**: FFmpeg の libx265 / libx264 で MP4 に圧縮。進捗率を表示し、音声が AAC なら再エンコードしません
- **並列処理**: 画像はスレッド並列で処理でき、大量の写真でも時間を短縮
- **HEIC のまま出力**: HEIC 写真は HDR・色域を保ったまま Mobile に置けます
- **GUI 完結**: Tkinter 製。進捗とログをその場で確認でき、処理は「中止」ボタンでいつでも止められる

## 動作環境

- Windows 10 / 11
- Python 3.10 以上
- FFmpeg（動画圧縮を使う場合のみ。`libx265` を含むビルド）

## セットアップ

```powershell
pip install -r requirements.txt
```

## 起動

- ソースから: `photo_backup.bat` をダブルクリック
- exe 版: `dist\PhotoBackup\PhotoBackup.exe` をダブルクリック（下記「exe にする」参照）

## ファイル構成

| ファイル | 役割 |
|---|---|
| `photo_backup.py` | 本体（GUI とバックアップ処理） |
| `photo_backup.bat` | 起動用ランチャー（Python を自動検出） |
| `requirements.txt` | 依存ライブラリ |
| `requirements-build.txt` | exe 作成用（PyInstaller） |
| `photo_backup.spec` | PyInstaller 設定（フォルダ配布版・推奨） |
| `photo_backup_onefile.spec` | PyInstaller 設定（1 ファイル版） |
| `build_exe.bat` | exe をビルドするスクリプト |
| `使い方.md` | 詳しい利用手順・設定一覧・トラブルシューティング |

## exe にする

Python が入っていない PC でも動くよう、1 つのフォルダにまとめられます。

```powershell
pip install -r requirements-build.txt
```

その後 `build_exe.bat` をダブルクリックします（または `python -m PyInstaller --noconfirm --clean photo_backup.spec`）。

- 出力: `dist\PhotoBackup\PhotoBackup.exe`（**フォルダごと**コピーして使います）
- `config.json` は **exe と同じフォルダ**に作られます。書き込みできない場所（`C:\Program Files` 直下など）に置くと設定を保存できません
- `config_profiles.json` / `processed.json` も同じ考え方で保存されます
- **FFmpeg は同梱されません**。動画圧縮を使う場合は別途導入し、`ffmpeg_path` を設定してください
- 1 ファイルにまとめたい場合は `photo_backup_onefile.spec` でビルドします。初回起動に数秒かかり、一時フォルダへ書き込めない環境では起動できません

## 既定のフォルダ

| 用途 | パス |
|---|---|
| 取り込み | `C:\PhotoImport` |
| 原本 | `C:\PhotoBackup\Original` |
| 軽量版 | `C:\PhotoBackup\Mobile` |
| ログ / 重複DB | `C:\PhotoBackup\backup_log.txt` / `hashes.json` |

Original / Mobile / ログ・DB の 3 つは画面から個別に指定できます（別ドライブ可）。変更内容は `config.json` に保存されます（`.gitignore` 済み）。

## 圧縮の程度

画面の「圧縮の程度」で調整できます。既定は従来どおり **長辺 1200px / JPEG 品質 80 / 動画 CRF 28**。

| プリセット | 長辺 | JPEG 品質 | 動画 CRF |
|---|---|---|---|
| 標準（現行設定） | 1200 px | 80 | 28 |
| 高画質（大きめ） | 1920 px | 90 | 23 |
| 容量優先（小さめ） | 1080 px | 70 | 32 |

## 注意

- 既定では取り込みフォルダのファイルを**削除しません**（結果を確認してから手動で片付ける運用）。自動削除したい場合は「処理後に取り込みフォルダから削除する」を ON にしてください。
- 重複判定は「構図が似ている」ことを基準にします。連写やほぼ同じ構図の撮り直しは重複扱いになることがあります。そのため重複スキップしたファイルは既定で取り込みフォルダに残します。詳しくは `使い方.md` の「6-4. 重複判定の注意」を参照してください。
- 動画の圧縮を OFF にすると、動画はバックアップも圧縮もされません。
