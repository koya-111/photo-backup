# photo-backup

Android スマホの写真・動画を PC に高画質でバックアップし、スマホへ戻すための軽量版を自動生成する Windows 向けツールです。スマホの空き容量を確保することを目的としています。

## 特徴

- **原本を保全**: 取り込んだファイルは無劣化のまま `Original` へコピー
- **軽量版を自動生成**: 長辺 1200px / JPEG 品質 80 に縮小して `Mobile` へ出力
- **重複を自動スキップ**: pHash（8×8）のハミング距離で判定。向き補正後に計算するため回転画像も検出
- **撮影日時で整理**: `YYYY\MM` などの階層へ自動振り分け（写真は EXIF、動画は ffprobe → ファイル名 → 更新日時）
- **サブフォルダ対応**: `DCIM\Camera` ごとコピーしても再帰的に処理
- **動画圧縮（任意）**: FFmpeg の libx265 で H.265 / MP4 に圧縮
- **GUI 完結**: Tkinter 製。進捗とログをその場で確認できる

## 動作環境

- Windows 10 / 11
- Python 3.10 以上
- FFmpeg（動画圧縮を使う場合のみ。`libx265` を含むビルド）

## セットアップ

```powershell
pip install -r requirements.txt
```

## 起動

`photo_backup.bat` をダブルクリックします。

## ファイル構成

| ファイル | 役割 |
|---|---|
| `photo_backup.py` | 本体（GUI とバックアップ処理） |
| `photo_backup.bat` | 起動用ランチャー（Python を自動検出） |
| `requirements.txt` | 依存ライブラリ |
| `使い方.md` | 詳しい利用手順・設定一覧・トラブルシューティング |

## 既定のフォルダ

| 用途 | パス |
|---|---|
| 取り込み | `C:\PhotoImport` |
| 原本 | `C:\PhotoBackup\Original` |
| 軽量版 | `C:\PhotoBackup\Mobile` |
| ログ / 重複DB | `C:\PhotoBackup\backup_log.txt` / `hashes.json` |

変更したい場合は、初回起動時に生成される `config.json` を編集してください（`config.json` は `.gitignore` 済み）。

## 注意

- 既定では処理後に取り込みフォルダから元ファイルを削除します。**初回は設定を確認してから実行**してください。
- 動画の圧縮を OFF にすると、動画はバックアップも圧縮もされません。
