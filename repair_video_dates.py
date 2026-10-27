# -*- coding: utf-8 -*-
"""既存の Mobile 動画の撮影日時を修復する（再エンコードなし）。

photo_backup.py の v1.0.1 以前が出力した MP4 は、圧縮時に
MP4 の作成日時（creation_time / ExifTool の Create Date）が失われ、
ファイルの更新日時も「今」になっていました。そのためスマホの
ギャラリーでは、動画だけが追加順に先頭へ並んでしまいます。

このスクリプトは動画を再エンコードせず（ストリームコピー）、
次の順で撮影日時を決めて creation_time と更新日時を書き換えます。

  1. Original フォルダにある同名の動画のメタデータ
  2. すでに書かれている creation_time
  3. ファイル名の日時（20240115_123456 など）
  4. ファイルの更新日時

使い方（PowerShell）:

    # Mobile の隣に Original がある場合（既定で自動検出）
    python repair_video_dates.py "C:\\PhotoBackup\\Mobile"

    # 保存先を分けている場合
    python repair_video_dates.py "D:\\Mobile" --original "D:\\Original"

    # 変更内容の確認だけ（書き換えない）
    python repair_video_dates.py "C:\\PhotoBackup\\Mobile" --dry-run

修復したファイルは、あらためてスマホへコピーし直してください。
ギャラリーが日時をキャッシュしている場合は、端末の再起動や
フォルダの移動で反映されることがあります。
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import photo_backup as pb  # noqa: E402  (パス追加後に読み込む)

VIDEO_EXTS = (".mp4", ".mov")


def index_originals(root):
    """Original 配下の動画を「拡張子を除いた名前（小文字）」で引けるようにする。"""
    index = {}
    if not root or not os.path.isdir(root):
        return index
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            if os.path.splitext(name)[1].lower() not in VIDEO_EXTS:
                continue
            stem = os.path.splitext(name)[0].lower()
            index.setdefault(stem, []).append(os.path.join(dirpath, name))
    return index


def find_original(path, mobile_root, original_root, originals):
    """対応する Original の動画を探す（同じ階層を優先し、曖昧なら None）。"""
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    candidates = originals.get(stem)
    if not candidates:
        return None
    rel = os.path.relpath(path, mobile_root).replace("\\", "/").lower()
    same = [c for c in candidates
            if os.path.relpath(c, original_root).replace("\\", "/").lower() == rel]
    if len(same) == 1:
        return same[0]
    if len(candidates) == 1:
        return candidates[0]
    return None


def decide_datetime(path, mobile_root, original_root, originals, ffmpeg):
    """(撮影日時, 根拠) を返す。決められなければ (None, 理由)。"""
    original = find_original(path, mobile_root, original_root, originals)
    if original:
        dt = pb.probe_video_datetime(original, ffmpeg)
        if dt:
            return dt, "Original のメタデータ"
    dt = pb.probe_video_datetime(path, ffmpeg)
    if dt:
        return dt, "既存の creation_time"
    dt = pb.datetime_from_filename(path)
    if dt:
        return dt, "ファイル名"
    try:
        return datetime_from_mtime(path), "ファイルの更新日時"
    except OSError:
        return None, "日時を判定できません"


def datetime_from_mtime(path):
    return datetime.fromtimestamp(os.path.getmtime(path))


def is_already_correct(path, dt, ffmpeg):
    """creation_time と更新日時が、決めた日時とほぼ一致しているか。"""
    current = pb.probe_video_datetime(path, ffmpeg)
    if current is None or abs((current - dt).total_seconds()) > 2:
        return False
    try:
        return abs(os.path.getmtime(path) - dt.timestamp()) <= 60
    except OSError:
        return False


def rewrite_creation_time(path, dt, ffmpeg):
    """ストリームコピーで creation_time を書き換える（画質は変わらない）。"""
    directory = os.path.dirname(path)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.repair.tmp.mp4")
    cmd = [
        ffmpeg, "-y", "-loglevel", "error", "-i", path,
        "-map", "0:v:0", "-map", "0:a?",
        "-map_metadata", "0",
        "-c", "copy",
        "-metadata", f"creation_time={pb.to_mp4_creation_time(dt)}",
        "-movflags", "+faststart",
        tmp,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "").strip()[-300:])
        os.replace(tmp, path)
    finally:
        pb._remove_quietly(tmp)
    pb.apply_media_timestamp(path, dt)


def collect_videos(root):
    paths = []
    for dirpath, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            if os.path.splitext(name)[1].lower() in VIDEO_EXTS:
                paths.append(os.path.join(dirpath, name))
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Mobile フォルダ内の動画の撮影日時を、再エンコードせずに修復します。")
    parser.add_argument("mobile", help="Mobile フォルダ（動画を書き換える対象）")
    parser.add_argument("--original", default=None,
                        help="Original フォルダ（撮影日時の参照元。既定は Mobile の隣を自動検出）")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg の実行パス")
    parser.add_argument("--dry-run", action="store_true", help="変更内容を表示するだけ")
    args = parser.parse_args(argv)

    mobile_root = os.path.abspath(args.mobile)
    if not os.path.isdir(mobile_root):
        print(f"Mobile フォルダが見つかりません: {mobile_root}")
        return 1

    original_root = args.original
    if not original_root:
        sibling = os.path.join(os.path.dirname(mobile_root), "Original")
        if os.path.isdir(sibling):
            original_root = sibling
    if original_root:
        original_root = os.path.abspath(original_root)

    ffmpeg = pb.find_ffmpeg({"ffmpeg_path": args.ffmpeg})
    if not ffmpeg:
        print(f"FFmpeg が見つかりません: {args.ffmpeg}")
        print("「--ffmpeg C:\\path\\to\\ffmpeg.exe」で指定してください。")
        return 1

    videos = collect_videos(mobile_root)
    if not videos:
        print("対象の動画がありません。")
        return 0

    originals = index_originals(original_root)
    print(f"対象: {len(videos)} 件")
    print(f"参照元: {original_root or '（Original なし。ファイル名と更新日時で判定）'}")
    if args.dry_run:
        print("※ --dry-run のため書き換えません")

    ok = skipped = failed = 0
    for path in videos:
        name = os.path.relpath(path, mobile_root)
        dt, reason = decide_datetime(path, mobile_root, original_root, originals, ffmpeg)
        if dt is None:
            failed += 1
            print(f"  [失敗] {name}: {reason}")
            continue
        if is_already_correct(path, dt, ffmpeg):
            skipped += 1
            print(f"  [済み] {name}: {dt:%Y-%m-%d %H:%M:%S}（{reason}）")
            continue
        if args.dry_run:
            ok += 1
            print(f"  [予定] {name}: {dt:%Y-%m-%d %H:%M:%S}（{reason}）")
            continue
        try:
            rewrite_creation_time(path, dt, ffmpeg)
            ok += 1
            print(f"  [修復] {name}: {dt:%Y-%m-%d %H:%M:%S}（{reason}）")
        except Exception as e:  # noqa: BLE001  (1 件の失敗で全体を止めない)
            failed += 1
            print(f"  [失敗] {name}: {e}")

    print(f"\n修復 {ok} 件 / 変更なし {skipped} 件 / 失敗 {failed} 件")
    if ok and not args.dry_run:
        print("スマホへコピーし直すと、ギャラリーで撮影日時の位置に並びます。")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
