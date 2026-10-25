# -*- coding: utf-8 -*-
"""
Android スマホ写真 バックアップ＆軽量化・同期ツール
====================================================

スマホの DCIM から取り込んだ写真・動画を PC 側で
  (1) 高画質バックアップ(Original) と
  (2) スマホ閲覧用の低画質版(Mobile)
に振り分けます。

使い方の概要:
  1) スマホから PC の「取り込みフォルダ」(既定 C:\\PhotoImport) に
     写真・動画をコピーする(MTP / Windows の写真インポート)。
  2) 本ツールを起動し、必要に応じて設定を変更して「実行」を押す。
  3) 処理後、Mobile フォルダの中身をスマホに戻すと容量が削減される。

依存ライブラリ:
    pip install -r requirements.txt
    (Pillow / pillow-heif / ImageHash。requirements.txt も同梱)
動画圧縮には FFmpeg(libx265 を含むビルド) が別途必要です。
不足がある場合は起動時にメッセージで案内します。

設定は初回起動時(および実行時)に config.json として自動保存されます。
"""

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, scrolledtext, ttk

# ---------------------------------------------------------------- 依存ライブラリ
# 不足していても import 時点では落とさず、起動時にまとめて案内する。
try:
    from PIL import Image, ImageFile, ImageOps

    PILLOW_AVAILABLE = True
except Exception:
    Image = ImageFile = ImageOps = None
    PILLOW_AVAILABLE = False

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_AVAILABLE = True
except Exception:
    HEIF_AVAILABLE = False

try:
    import imagehash

    IMAGEHASH_AVAILABLE = True
except Exception:
    imagehash = None
    IMAGEHASH_AVAILABLE = False

try:
    import numpy as np

    NUMPY_AVAILABLE = True
except Exception:
    np = None
    NUMPY_AVAILABLE = False

if PILLOW_AVAILABLE:
    # 破損気味の画像でも読み込めるよう許容(途中欠損でも処理を継続する)
    ImageFile.LOAD_TRUNCATED_IMAGES = True

# (pip パッケージ名, 導入済みか) — 必須依存
REQUIRED_PACKAGES = [
    ("Pillow", PILLOW_AVAILABLE),
    ("ImageHash", IMAGEHASH_AVAILABLE),
]
OPTIONAL_PACKAGES = [
    ("pillow-heif", HEIF_AVAILABLE, "HEIC/HEIF 形式の写真を扱えません。"),
]


def missing_required_packages():
    return [name for name, ok in REQUIRED_PACKAGES if not ok]


def dependency_message():
    """依存関係の不足を説明するメッセージを返す(問題なしなら None)。"""
    lines = []
    missing = missing_required_packages()
    if missing:
        lines.append("必須ライブラリが不足しています: " + ", ".join(missing))
        lines.append("")
        lines.append("コマンドプロンプト / PowerShell で次を実行してください。")
        lines.append("")
        lines.append("    pip install -r requirements.txt")
        lines.append("")
    for name, ok, note in OPTIONAL_PACKAGES:
        if not ok:
            lines.append(f"{name} が未導入のため、{note}")
            lines.append(f"    pip install {name}")
            lines.append("")
    return "\n".join(lines).rstrip() if lines else None

APP_NAME = "PhotoBackup"

# ---------------------------------------------------------------- 設定の既定値
DEFAULT_CONFIG = {
    "intake_dir": r"C:\PhotoImport",           # 取り込みフォルダ
    "original_dir": r"C:\PhotoBackup\Original",  # 原本の保存先
    "mobile_dir": r"C:\PhotoBackup\Mobile",      # スマホ戻し用の保存先
    "work_dir": r"C:\PhotoBackup",             # ログ・重複判定DBの保存先
    "layout": "YYYY\\MM",                      # 整理方法: flat / YYYY / YYYY\MM / YYYY\MM\DD
    "include_subfolders": True,                # 取り込みフォルダのサブフォルダも対象にするか
    "delete_after_process": False,             # 処理後に取り込みフォルダから削除するか(既定は残す)
    "delete_duplicates": False,                # 重複スキップしたファイルも取り込みから削除するか
    "compress_video": False,                   # 動画も圧縮するか
    "enable_dedup": False,                     # 重複スキップを行うか(既定はオフ)
    "phash_threshold": 5,                      # pHash ハミング距離のしきい値(これ以下で重複扱い)
    "image_long_side": 1200,                   # スマホ戻し用画像の長辺ピクセル
    "jpeg_quality": 80,                        # JPEG 品質(1〜100)
    "video_crf": 28,                           # 動画圧縮の CRF(小さいほど高画質・大容量)
    "video_preset": "medium",                  # libx265 のプリセット
    "ffmpeg_path": "ffmpeg",                   # ffmpeg 実行パス(PATH に入っていれば "ffmpeg")
}

# 圧縮の程度のプリセット(長辺px, JPEG品質, 動画CRF)
COMPRESSION_PRESETS = {
    "標準（現行設定）": (1200, 80, 28),
    "高画質（大きめ）": (1920, 90, 23),
    "容量優先（小さめ）": (1080, 70, 32),
}
DEFAULT_PRESET = "標準（現行設定）"

# 整理方法(キー) → GUI 表示ラベル
LAYOUT_LABELS = {
    "flat": "フラット(整理なし)",
    "YYYY": "年ごと (YYYY)",
    "YYYY\\MM": "年/月ごと (YYYY\\MM)",
    "YYYY\\MM\\DD": "年/月/日ごと (YYYY\\MM\\DD)",
}

# 整理方法(キー) → strftime 形式
LAYOUT_STRFTIME = {
    "flat": "",
    "YYYY": "%Y",
    "YYYY\\MM": "%Y\\%m",
    "YYYY\\MM\\DD": "%Y\\%m\\%d",
}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
VIDEO_EXTS = {".mp4", ".mov"}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- 設定の読み書き
def config_path():
    return os.path.join(SCRIPT_DIR, "config.json")


def normalize_dir(path, fallback=""):
    """入力されたパスを整形する(引用符・環境変数・~ を解決)。"""
    if not path:
        return fallback
    p = str(path).strip().strip('"').strip("'")
    if not p:
        return fallback
    p = os.path.expandvars(os.path.expanduser(p))
    return os.path.normpath(p)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    p = config_path()
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k in DEFAULT_CONFIG:
                    if k in data:
                        cfg[k] = data[k]
                # 旧形式(backup_root ひとつ)からの移行
                root = data.get("backup_root")
                if root:
                    if "original_dir" not in data:
                        cfg["original_dir"] = os.path.join(root, "Original")
                    if "mobile_dir" not in data:
                        cfg["mobile_dir"] = os.path.join(root, "Mobile")
                    if "work_dir" not in data:
                        cfg["work_dir"] = root
        except Exception:
            pass
    for key in ("intake_dir", "original_dir", "mobile_dir", "work_dir"):
        cfg[key] = normalize_dir(cfg.get(key), DEFAULT_CONFIG[key])
    return cfg


def save_config(cfg):
    data = {k: cfg[k] for k in DEFAULT_CONFIG if k in cfg}
    try:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------- パス派生
def original_dir(cfg):
    return cfg.get("original_dir") or os.path.join(DEFAULT_CONFIG["work_dir"], "Original")


def mobile_dir(cfg):
    return cfg.get("mobile_dir") or os.path.join(DEFAULT_CONFIG["work_dir"], "Mobile")


def work_dir(cfg):
    return cfg.get("work_dir") or DEFAULT_CONFIG["work_dir"]


def log_path(cfg):
    return os.path.join(work_dir(cfg), "backup_log.txt")


def hash_db_path(cfg):
    return os.path.join(work_dir(cfg), "hashes.json")


def output_dirs(cfg):
    """出力・管理用フォルダの一覧(取り込み元の除外判定に使う)。"""
    return [original_dir(cfg), mobile_dir(cfg), work_dir(cfg)]


def layout_subfolder(dt, layout):
    fmt = LAYOUT_STRFTIME.get(layout, "%Y\\%m")
    return dt.strftime(fmt) if fmt else ""


def unique_path(directory, filename):
    """同名ファイルが既にあれば 末尾に _1, _2 ... を付けて衝突を回避する。"""
    base, ext = os.path.splitext(filename)
    p = os.path.join(directory, filename)
    i = 1
    while os.path.exists(p):
        p = os.path.join(directory, f"{base}_{i}{ext}")
        i += 1
    return p


# ---------------------------------------------------------------- pHash の保存・照合
def _popcount(value):
    """64bit 整数の 1 のビット数(ハミング重み)。"""
    return bin(value).count("1")


class HashStore:
    """登録済み pHash を hashes.json に保持し、重複判定を行う。

    ハッシュは 64bit 整数として保持する。numpy があれば XOR と popcount を
    ベクトル化して一括計算するため、数万件規模でも判定コストを抑えられる
    (numpy 未導入時は純 Python のループにフォールバック)。
    """

    def __init__(self, path):
        self.path = path
        self._hashes = []   # list[int]
        self._array = None  # numpy 配列のキャッシュ(遅延構築)
        self._load()

    @staticmethod
    def _to_int(text):
        t = str(text).strip().lower()
        if not t or not re.fullmatch(r"[0-9a-f]+", t):
            raise ValueError(f"不正なハッシュ: {text!r}")
        return int(t, 16)

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("hashes", []) if isinstance(data, dict) else data
            for h in items:
                try:
                    self._hashes.append(self._to_int(h))
                except Exception:
                    continue
        except Exception:
            self._hashes = []
        self._array = None

    def _as_array(self):
        if not NUMPY_AVAILABLE:
            return None
        if self._array is None:
            if self._hashes:
                self._array = np.array(self._hashes, dtype=np.uint64)
            else:
                self._array = np.empty(0, dtype=np.uint64)
        return self._array

    def is_duplicate(self, candidate, threshold):
        c = candidate if isinstance(candidate, int) else self._to_int(candidate)
        if not self._hashes:
            return False

        arr = self._as_array()
        if arr is None:
            # numpy が無い環境向けのフォールバック
            return any(_popcount(c ^ h) <= threshold for h in self._hashes)

        # uint64 の XOR をバイト列に展開し、1要素あたり 64bit の 1 の数を数える
        xor_bytes = np.bitwise_xor(arr, np.uint64(c)).view(np.uint8)
        counts = np.unpackbits(xor_bytes).reshape(-1, 64).sum(axis=1)
        return bool((counts <= threshold).any())

    def add(self, candidate):
        c = candidate if isinstance(candidate, int) else self._to_int(candidate)
        self._hashes.append(c)
        self._array = None

    def save(self):
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(
                    {"version": 1, "hashes": [f"{h:016x}" for h in self._hashes]},
                    f,
                    ensure_ascii=False,
                )
        except Exception:
            pass


# ---------------------------------------------------------------- 撮影日時の取得
def get_photo_datetime(im, src):
    try:
        exif = im.getexif()
        s = exif.get(0x9003) or exif.get(0x0132)  # DateTimeOriginal / DateTime
        if s:
            return datetime.strptime(str(s).strip(), "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    return datetime.fromtimestamp(os.path.getmtime(src))


# ---------------------------------------------------------------- 画像処理
def process_image(src, cfg, hash_store, stats):
    with Image.open(src) as im:
        im.load()
        exif = im.getexif()
        icc = im.info.get("icc_profile")
        dt = get_photo_datetime(im, src)

        # 向き(Orientation)を適用して、以後は正立画像として扱う
        try:
            oriented = ImageOps.exif_transpose(im)
        except Exception:
            oriented = im
        if oriented is im:
            oriented = im.copy()

        # pHash は向き適用後の画像から算出(向きの違いで別物扱いされないように)
        ph = imagehash.phash(oriented.convert("RGB"), hash_size=8)

    # 重複スキップは既定でオフ。オンのときだけ判定する。
    # オフでもハッシュは記録しておき、後から有効にしたときに活かせるようにする。
    if cfg.get("enable_dedup", False) and hash_store.is_duplicate(ph, cfg["phash_threshold"]):
        return "duplicate"

    # 1) 元画像を Original へコピー
    dest_orig_dir = os.path.join(original_dir(cfg), layout_subfolder(dt, cfg["layout"]))
    os.makedirs(dest_orig_dir, exist_ok=True)
    dest_orig = unique_path(dest_orig_dir, os.path.basename(src))
    shutil.copy2(src, dest_orig)

    # 2) リサイズして Mobile へ出力
    img = oriented
    if img.mode in ("RGBA", "LA", "P"):
        # アルファ(透過)がある場合は白背景に合成
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    img.thumbnail((cfg["image_long_side"], cfg["image_long_side"]), Image.LANCZOS)

    try:
        exif[0x0112] = 1  # 向き適用済みなので Orientation を「正立」に正規化
    except Exception:
        pass

    dest_mobile_dir = os.path.join(mobile_dir(cfg), layout_subfolder(dt, cfg["layout"]))
    os.makedirs(dest_mobile_dir, exist_ok=True)
    base, _ = os.path.splitext(os.path.basename(src))
    dest_mobile = unique_path(dest_mobile_dir, base + ".jpg")

    save_kwargs = {"quality": cfg["jpeg_quality"], "optimize": True}
    try:
        exif_bytes = exif.tobytes()
        if exif_bytes:
            save_kwargs["exif"] = exif_bytes  # 撮影日時・GPS などを可能な限り保持
    except Exception:
        pass
    if icc:
        save_kwargs["icc_profile"] = icc  # 色プロファイルを保持
    img.save(dest_mobile, "JPEG", **save_kwargs)

    hash_store.add(ph)

    stats["images"] += 1
    stats["orig_bytes"] += os.path.getsize(src)
    stats["new_bytes"] += os.path.getsize(dest_mobile)
    return "ok"


# ---------------------------------------------------------------- 動画の撮影日時
# ファイル名に埋め込まれた日時(Pixel / Xperia / 旧 Android の命名規則)
_FILENAME_DATE_PATTERNS = (
    # 20240115_123456 / 20240115-123456
    re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})[_-]?(\d{2})(\d{2})(\d{2})(?!\d)"),
    # 2024-01-15 12.34.56 / 2024-01-15_12-34-56
    re.compile(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})[ T_-](\d{2})[.:-](\d{2})[.:-](\d{2})"),
)


def datetime_from_filename(name):
    """ファイル名から日時を推定する。見つからなければ None。"""
    stem = os.path.splitext(os.path.basename(name))[0]
    for pattern in _FILENAME_DATE_PATTERNS:
        m = pattern.search(stem)
        if not m:
            continue
        try:
            return datetime.strptime("".join(m.groups()), "%Y%m%d%H%M%S")
        except ValueError:
            continue
    return None


def resolve_ffprobe(ffmpeg_path):
    """ffmpeg のパス設定から ffprobe の実行パスを推定する。"""
    if not ffmpeg_path:
        return None
    base = os.path.basename(ffmpeg_path)
    if "ffmpeg" in base.lower():
        candidate = os.path.join(os.path.dirname(ffmpeg_path), base.replace("ffmpeg", "ffprobe"))
        if os.path.exists(candidate):
            return candidate
        return "ffprobe"
    return "ffprobe"


def probe_video_datetime(src, ffmpeg_path):
    """ffprobe の creation_time から撮影日時を取得する(取れなければ None)。"""
    ffprobe = resolve_ffprobe(ffmpeg_path)
    if not ffprobe:
        return None
    cmd = [
        ffprobe, "-v", "error",
        "-show_entries", "format_tags=creation_time:stream_tags=creation_time",
        "-of", "default=noprint_wrappers=1:nokey=1",
        src,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        value = line.strip()
        if not value:
            continue
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    return None


def get_video_datetime(src, ffmpeg_path=None):
    """動画の撮影日時を メタデータ → ファイル名 → 更新日時 の順で推定する。"""
    dt = probe_video_datetime(src, ffmpeg_path)
    if dt:
        return dt
    dt = datetime_from_filename(src)
    if dt:
        return dt
    return datetime.fromtimestamp(os.path.getmtime(src))


class Cancelled(Exception):
    """ユーザーが中止を要求したときに送出する。"""


# ---------------------------------------------------------------- 動画処理
def process_video(src, cfg, stats, cancel_event=None):
    dt = get_video_datetime(src, cfg.get("ffmpeg_path"))

    if cancel_event is not None and cancel_event.is_set():
        raise Cancelled()

    # 元動画を Original へコピー
    dest_orig_dir = os.path.join(original_dir(cfg), layout_subfolder(dt, cfg["layout"]))
    os.makedirs(dest_orig_dir, exist_ok=True)
    dest_orig = unique_path(dest_orig_dir, os.path.basename(src))
    shutil.copy2(src, dest_orig)

    # 圧縮して Mobile へ出力(コンテナは MP4 に統一)
    dest_mobile_dir = os.path.join(mobile_dir(cfg), layout_subfolder(dt, cfg["layout"]))
    os.makedirs(dest_mobile_dir, exist_ok=True)
    base, _ = os.path.splitext(os.path.basename(src))
    dest_mobile = unique_path(dest_mobile_dir, base + ".mp4")

    cmd = [
        cfg["ffmpeg_path"], "-y", "-i", src,
        "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx265",
        "-crf", str(cfg["video_crf"]),
        "-preset", cfg["video_preset"],
        "-tag:v", "hvc1",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-nostats", "-loglevel", "error",
        dest_mobile,
    ]
    # 中止要求にすぐ反応できるよう、完了を待たずに一定間隔で状態を見る
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True)
    try:
        while True:
            try:
                _, err = proc.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if cancel_event is not None and cancel_event.is_set():
                    proc.kill()
                    proc.communicate()
                    raise Cancelled()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    if proc.returncode != 0:
        if cancel_event is not None and cancel_event.is_set():
            _remove_quietly(dest_mobile)
            raise Cancelled()
        tail = (err or "").strip()[-500:]
        raise RuntimeError(f"FFmpeg が失敗: {tail}")

    stats["videos"] += 1
    stats["orig_bytes"] += os.path.getsize(src)
    stats["new_bytes"] += os.path.getsize(dest_mobile)
    return "ok"


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def check_ffmpeg(ffmpeg_path):
    try:
        subprocess.run([ffmpeg_path, "-version"], capture_output=True, timeout=15)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- ログ出力
_log_lock = threading.Lock()


def emit_log(q, log_file, level, msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg}"
    try:
        q.put(("log", line))
    except Exception:
        pass
    try:
        with _log_lock:
            d = os.path.dirname(log_file)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 処理対象の列挙
def collect_intake_files(cfg, log=None):
    """取り込みフォルダから処理対象ファイルを列挙する。

    include_subfolders が True ならサブフォルダも再帰的に走査する。
    出力先(Original / Mobile / ログ・DB)が取り込みフォルダの中にある場合は、
    自分が出力したファイルを再処理しないよう除外する。
    """
    intake = os.path.abspath(cfg["intake_dir"])
    excluded = [os.path.abspath(d) for d in output_dirs(cfg) if d]
    nested = [d for d in excluded
              if d != intake and d.startswith(intake + os.sep)]

    def is_candidate(path):
        ext = os.path.splitext(path)[1].lower()
        if ext in IMAGE_EXTS:
            return True
        return bool(cfg["compress_video"] and ext in VIDEO_EXTS)

    def is_excluded(path):
        p = os.path.abspath(path)
        return any(p == d or p.startswith(d + os.sep) for d in excluded)

    paths = []
    if cfg.get("include_subfolders", True):
        for root, dirs, names in os.walk(intake):
            if is_excluded(root):
                dirs[:] = []
                continue
            dirs.sort()
            for name in sorted(names):
                p = os.path.join(root, name)
                if os.path.isfile(p) and is_candidate(p) and not is_excluded(p):
                    paths.append(p)
    else:
        for name in sorted(os.listdir(intake)):
            p = os.path.join(intake, name)
            if os.path.isfile(p) and is_candidate(p) and not is_excluded(p):
                paths.append(p)

    if nested and log:
        log("WARN", "出力先が取り込みフォルダ内にあります: "
                    + ", ".join(nested)
                    + "（出力済みファイルは処理対象から除外します）")
    return paths


def remove_empty_subdirs(intake):
    """取り込みフォルダ配下の空フォルダを削除する(取り込みフォルダ自体は残す)。"""
    removed = 0
    for root, dirs, _ in os.walk(intake, topdown=False):
        if os.path.abspath(root) == os.path.abspath(intake):
            continue
        try:
            if not os.listdir(root):
                os.rmdir(root)
                removed += 1
        except OSError:
            continue
    return removed


# ---------------------------------------------------------------- 処理本体(別スレッドで実行)
def run_processing(cfg, q, cancel_event=None):
    log_file = log_path(cfg)
    cancelled = False

    def log(level, msg):
        emit_log(q, log_file, level, msg)

    def is_cancelled():
        return cancel_event is not None and cancel_event.is_set()

    for d in (original_dir(cfg), mobile_dir(cfg), work_dir(cfg)):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception as e:
            log("ERROR", f"フォルダを作成できません: {d} ({e})")

    intake = cfg["intake_dir"]
    if not os.path.isdir(intake):
        msg = f"取り込みフォルダが見つかりません: {intake}"
        log("ERROR", msg)
        q.put(("done", None, msg))
        return

    # 処理対象の列挙(サブフォルダ対応)
    files = collect_intake_files(cfg, log)

    # FFmpeg の事前確認(動画を処理する場合のみ)
    video_files = [p for p in files if os.path.splitext(p)[1].lower() in VIDEO_EXTS]
    if video_files and not check_ffmpeg(cfg["ffmpeg_path"]):
        log("ERROR", "FFmpeg が見つかりません。PATH に追加するか config.json の ffmpeg_path を設定してください。")
        files = [p for p in files if os.path.splitext(p)[1].lower() not in VIDEO_EXTS]

    if not files:
        msg = "処理対象のファイルがありません。"
        log("INFO", msg)
        q.put(("done", None, msg))
        return

    total = len(files)
    q.put(("start", total))
    log("INFO", f"処理開始: {total} 件 ({intake})")
    if cfg.get("enable_dedup", False):
        log("INFO", f"重複スキップ: 有効（pHash 距離 {cfg['phash_threshold']} 以下で重複）")
    else:
        log("INFO", "重複スキップ: 無効")

    hash_store = HashStore(hash_db_path(cfg))
    stats = {"images": 0, "videos": 0, "orig_bytes": 0, "new_bytes": 0}
    skipped_dup = 0
    errors = 0

    for i, p in enumerate(files, 1):
        if is_cancelled():
            cancelled = True
            log("WARN", "中止要求を受け付けたため、残りのファイルを処理せず停止しました。")
            break
        name = os.path.basename(p)
        ext = os.path.splitext(name)[1].lower()
        keep_in_intake = False
        try:
            if ext in IMAGE_EXTS:
                result = process_image(p, cfg, hash_store, stats)
                if result == "duplicate":
                    skipped_dup += 1
                    # 重複と判定したファイルは原本も Mobile も作られていない。
                    # 誤判定だった場合に取り返しがつかないため、既定では残す。
                    if not cfg.get("delete_duplicates", False):
                        keep_in_intake = True
                        log("INFO", f"重複スキップ（取り込みに残します）: {name}")
                    else:
                        log("INFO", f"重複スキップ: {name}")
                else:
                    log("INFO", f"画像処理: {name}")
            else:
                process_video(p, cfg, stats, cancel_event)
                log("INFO", f"動画圧縮: {name}")

            # 成功したら、設定に応じて取り込み元から削除
            if cfg["delete_after_process"] and not keep_in_intake:
                try:
                    os.remove(p)
                    log("INFO", f"取り込みから削除: {name}")
                except OSError as e:
                    log("WARN", f"削除に失敗: {name} ({e})")
        except Cancelled:
            cancelled = True
            log("WARN", f"中止により処理を中断しました: {name}")
            break
        except Exception as e:
            errors += 1
            log("ERROR", f"処理失敗: {name} ({e})")

        q.put(("progress", i, total))

    hash_store.save()

    # サブフォルダを取り込み元ごと整理した場合、空になったフォルダを片付ける
    if cfg.get("include_subfolders", True) and cfg["delete_after_process"] and not cancelled:
        removed_dirs = remove_empty_subdirs(intake)
        if removed_dirs:
            log("INFO", f"空フォルダを削除: {removed_dirs} 件")

    orig_mb = stats["orig_bytes"] / (1024 * 1024)
    new_mb = stats["new_bytes"] / (1024 * 1024)
    reduction = (1 - stats["new_bytes"] / stats["orig_bytes"]) * 100 \
        if stats["orig_bytes"] > 0 else 0.0

    summary = (
        f"画像 {stats['images']} 枚 / 動画 {stats['videos']} 件 を処理"
        f"（重複スキップ {skipped_dup} 件 / エラー {errors} 件）\n"
        f"元 {orig_mb:.1f} MB → {new_mb:.1f} MB（{reduction:.1f}% 削減）"
    )
    if cancelled:
        summary = "中止しました（ここまでの結果）\n" + summary
    log("INFO", summary.replace("\n", " / "))
    q.put(("done", stats, summary, cancelled))


# ---------------------------------------------------------------- GUI
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Android 写真 バックアップ＆軽量化ツール")
        self.cfg = load_config()
        self.queue = queue.Queue()
        self.running = False
        self.cancel_event = threading.Event()
        self._build_ui()
        self._load_cfg_to_ui()
        self.after(100, self._poll_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if not HEIF_AVAILABLE:
            self._append_log("注意: pillow-heif が未導入のため HEIC を扱えません。"
                             "「pip install pillow-heif」を実行してください。")
    # --- UI 構築 ---
    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}
        self.minsize(780, 640)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(6, weight=1)

        # 取り込みフォルダ
        f_intake = ttk.Frame(self)
        f_intake.grid(row=0, column=0, sticky="ew", **pad)
        f_intake.columnconfigure(1, weight=1)
        ttk.Label(f_intake, text="取り込みフォルダ:").grid(row=0, column=0, sticky="w")
        self.intake_var = tk.StringVar()
        ttk.Entry(f_intake, textvariable=self.intake_var).grid(
            row=0, column=1, sticky="ew", padx=6)
        ttk.Button(f_intake, text="参照...", width=8,
                   command=lambda: self._browse(self.intake_var,
                                                "取り込みフォルダを選択")).grid(row=0, column=2)

        # 保存先(Original / Mobile / ログ・DB を個別指定)
        f_dest = ttk.LabelFrame(self, text="保存先")
        f_dest.grid(row=1, column=0, sticky="ew", **pad)
        f_dest.columnconfigure(1, weight=1)
        self.original_var = tk.StringVar()
        self.mobile_var = tk.StringVar()
        self.work_var = tk.StringVar()
        for i, (label, var, title) in enumerate((
            ("Original（原本）:", self.original_var, "Original の保存先を選択"),
            ("Mobile（スマホ戻し用）:", self.mobile_var, "Mobile の保存先を選択"),
            ("ログ・重複DB:", self.work_var, "ログと重複判定DBの保存先を選択"),
        )):
            ttk.Label(f_dest, text=label).grid(
                row=i, column=0, sticky="w", padx=6, pady=2)
            ttk.Entry(f_dest, textvariable=var).grid(
                row=i, column=1, sticky="ew", padx=6, pady=2)
            ttk.Button(f_dest, text="参照...", width=8,
                       command=lambda v=var, t=title: self._browse(v, t)).grid(
                row=i, column=2, padx=(0, 6), pady=2)

        # 整理・動作
        f_set = ttk.LabelFrame(self, text="整理と動作")
        f_set.grid(row=2, column=0, sticky="ew", **pad)
        f_set.columnconfigure(1, weight=1)
        ttk.Label(f_set, text="整理方法:").grid(row=0, column=0, sticky="w", padx=6)
        self.layout_combo = ttk.Combobox(
            f_set, state="readonly", width=24, values=list(LAYOUT_LABELS.values()))
        self.layout_combo.grid(row=0, column=1, sticky="w", padx=6, pady=2)

        # 重複スキップ(オン/オフ + 判定しきい値)
        f_dedup = ttk.Frame(f_set)
        f_dedup.grid(row=1, column=0, columnspan=2, sticky="w", padx=6, pady=2)
        self.dedup_var = tk.BooleanVar(value=False)
        self.dedup_check = ttk.Checkbutton(
            f_dedup, text="重複スキップを有効にする", variable=self.dedup_var,
            command=self._toggle_dedup)
        self.dedup_check.pack(side="left")
        ttk.Label(f_dedup, text="　判定の pHash 閾値(小さいほど厳密):").pack(side="left")
        self.threshold_var = tk.StringVar(value="5")
        self.threshold_spin = ttk.Spinbox(
            f_dedup, from_=0, to=20, width=5, textvariable=self.threshold_var)
        self.threshold_spin.pack(side="left", padx=4)

        self.delete_var = tk.BooleanVar(value=False)
        self.subdir_var = tk.BooleanVar(value=True)
        self.video_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f_set, text="処理後に取り込みフォルダから削除する",
                        variable=self.delete_var).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=6)
        ttk.Checkbutton(f_set, text="サブフォルダも対象にする(DCIM\\Camera などの階層に対応)",
                        variable=self.subdir_var).grid(
            row=3, column=0, columnspan=2, sticky="w", padx=6)
        ttk.Checkbutton(f_set, text="動画も圧縮する(要 FFmpeg)",
                        variable=self.video_var).grid(
            row=4, column=0, columnspan=2, sticky="w", padx=6)

        # 圧縮の程度
        f_comp = ttk.LabelFrame(self, text="圧縮の程度")
        f_comp.grid(row=3, column=0, sticky="ew", **pad)
        f_comp.columnconfigure(3, weight=1)

        ttk.Label(f_comp, text="プリセット:").grid(row=0, column=0, sticky="w", padx=6)
        self.preset_combo = ttk.Combobox(
            f_comp, state="readonly", width=18,
            values=list(COMPRESSION_PRESETS) + ["カスタム"])
        self.preset_combo.grid(row=0, column=1, sticky="w", padx=6, pady=2)
        self.preset_combo.bind("<<ComboboxSelected>>", self._apply_preset)

        f_num = ttk.Frame(f_comp)
        f_num.grid(row=1, column=0, columnspan=4, sticky="ew", padx=6, pady=2)
        self.long_side_var = tk.StringVar(value="1200")
        self.quality_var = tk.StringVar(value="80")
        self.crf_var = tk.StringVar(value="28")
        ttk.Label(f_num, text="画像の長辺:").pack(side="left")
        ttk.Spinbox(f_num, from_=320, to=6000, increment=40, width=7,
                    textvariable=self.long_side_var).pack(side="left", padx=(4, 12))
        ttk.Label(f_num, text="px　JPEG 品質:").pack(side="left")
        ttk.Spinbox(f_num, from_=30, to=100, increment=5, width=5,
                    textvariable=self.quality_var).pack(side="left", padx=(4, 12))
        ttk.Label(f_num, text="動画 CRF:").pack(side="left")
        ttk.Spinbox(f_num, from_=0, to=51, increment=1, width=5,
                    textvariable=self.crf_var).pack(side="left", padx=4)

        ttk.Label(f_comp, foreground="#555",
                  text="長辺を小さく・品質を下げる・CRF を上げるほど容量が減ります"
                       "（動画 CRF は 18〜32 が目安）。").grid(
            row=2, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 4))

        # 実行
        f_run = ttk.Frame(self)
        f_run.grid(row=4, column=0, sticky="ew", **pad)
        self.run_btn = ttk.Button(f_run, text="実行", command=self._start)
        self.run_btn.pack(side="left")
        self.cancel_btn = ttk.Button(f_run, text="中止", width=8,
                                     command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(6, 0))
        self.progress = ttk.Progressbar(f_run, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)

        self.result_var = tk.StringVar(value="フォルダを確認して「実行」を押してください。")
        ttk.Label(self, textvariable=self.result_var, wraplength=760,
                  justify="left").grid(row=5, column=0, sticky="ew", **pad)

        self.log_text = scrolledtext.ScrolledText(self, height=10, state="disabled")
        self.log_text.grid(row=6, column=0, sticky="nsew", **pad)

    # --- UI ヘルパ ---
    def _append_log(self, text):
        self.log_text.config(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _browse(self, var, title):
        initial = var.get() or None
        if initial and not os.path.isdir(initial):
            initial = os.path.dirname(initial) or None
        d = filedialog.askdirectory(initialdir=initial, title=title)
        if d:
            var.set(os.path.normpath(d))

    def _toggle_dedup(self):
        """重複スキップがオフのときは、しきい値の入力欄を操作できないようにする。"""
        state = "normal" if self.dedup_var.get() else "disabled"
        try:
            self.threshold_spin.config(state=state)
        except Exception:
            pass

    def _apply_preset(self, _event=None):
        values = COMPRESSION_PRESETS.get(self.preset_combo.get())
        if not values:
            return  # カスタムは現在値を維持
        long_side, quality, crf = values
        self.long_side_var.set(str(long_side))
        self.quality_var.set(str(quality))
        self.crf_var.set(str(crf))

    @staticmethod
    def _layout_key_from_label(label):
        for k, v in LAYOUT_LABELS.items():
            if v == label:
                return k
        return "YYYY\\MM"

    @staticmethod
    def _layout_label_from_key(key):
        return LAYOUT_LABELS.get(key, LAYOUT_LABELS["YYYY\\MM"])

    @staticmethod
    def _int_or(value, default, low, high):
        try:
            n = int(float(value))
        except Exception:
            return default
        return max(low, min(high, n))

    def _load_cfg_to_ui(self):
        self.intake_var.set(self.cfg["intake_dir"])
        self.original_var.set(self.cfg["original_dir"])
        self.mobile_var.set(self.cfg["mobile_dir"])
        self.work_var.set(self.cfg["work_dir"])
        self.layout_combo.set(self._layout_label_from_key(self.cfg["layout"]))
        self.delete_var.set(bool(self.cfg["delete_after_process"]))
        self.subdir_var.set(bool(self.cfg.get("include_subfolders", True)))
        self.video_var.set(bool(self.cfg["compress_video"]))
        self.dedup_var.set(bool(self.cfg.get("enable_dedup", False)))
        self.threshold_var.set(str(self.cfg["phash_threshold"]))
        self._toggle_dedup()
        self.long_side_var.set(str(self.cfg["image_long_side"]))
        self.quality_var.set(str(self.cfg["jpeg_quality"]))
        self.crf_var.set(str(self.cfg["video_crf"]))

        current = (self.cfg["image_long_side"], self.cfg["jpeg_quality"],
                   self.cfg["video_crf"])
        preset = next((n for n, v in COMPRESSION_PRESETS.items() if v == current), "カスタム")
        self.preset_combo.set(preset)

    def _save_ui_to_cfg(self):
        self.cfg["intake_dir"] = normalize_dir(
            self.intake_var.get(), DEFAULT_CONFIG["intake_dir"])
        self.cfg["original_dir"] = normalize_dir(
            self.original_var.get(), DEFAULT_CONFIG["original_dir"])
        self.cfg["mobile_dir"] = normalize_dir(
            self.mobile_var.get(), DEFAULT_CONFIG["mobile_dir"])
        self.cfg["work_dir"] = normalize_dir(
            self.work_var.get(), DEFAULT_CONFIG["work_dir"])
        self.cfg["layout"] = self._layout_key_from_label(self.layout_combo.get())
        self.cfg["delete_after_process"] = bool(self.delete_var.get())
        self.cfg["include_subfolders"] = bool(self.subdir_var.get())
        self.cfg["compress_video"] = bool(self.video_var.get())
        self.cfg["enable_dedup"] = bool(self.dedup_var.get())
        self.cfg["phash_threshold"] = self._int_or(
            self.threshold_var.get(), DEFAULT_CONFIG["phash_threshold"], 0, 20)
        self.cfg["image_long_side"] = self._int_or(
            self.long_side_var.get(), DEFAULT_CONFIG["image_long_side"], 320, 6000)
        self.cfg["jpeg_quality"] = self._int_or(
            self.quality_var.get(), DEFAULT_CONFIG["jpeg_quality"], 30, 100)
        self.cfg["video_crf"] = self._int_or(
            self.crf_var.get(), DEFAULT_CONFIG["video_crf"], 0, 51)

    # --- 実行 ---
    def _validate_paths(self):
        """保存先の組み合わせを検証し、問題があればメッセージを返す。"""
        intake = os.path.abspath(self.cfg["intake_dir"])
        orig = os.path.abspath(self.cfg["original_dir"])
        mob = os.path.abspath(self.cfg["mobile_dir"])
        work = os.path.abspath(self.cfg["work_dir"])

        if not os.path.isdir(intake):
            return f"取り込みフォルダが見つかりません:\n{self.cfg['intake_dir']}"
        if orig == mob:
            return "Original と Mobile に同じフォルダは指定できません。"
        if orig == intake or mob == intake or work == intake:
            return "取り込みフォルダと同じ場所は保存先に指定できません。"
        for name, path in (("Original", orig), ("Mobile", mob)):
            other = mob if name == "Original" else orig
            if path.startswith(other + os.sep) or other.startswith(path + os.sep):
                return (f"{name} ともう一方の保存先が入れ子になっています。\n"
                        "別のフォルダを指定してください。")
        return None

    def _start(self):
        if self.running:
            return
        self._save_ui_to_cfg()
        save_config(self.cfg)

        error = self._validate_paths()
        if error:
            messagebox.showerror("設定を確認してください", error)
            return

        self.running = True
        self.cancel_event.clear()
        self.run_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.progress["value"] = 0
        self.result_var.set("処理中...")
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

        t = threading.Thread(target=run_processing,
                             args=(self.cfg, self.queue, self.cancel_event),
                             daemon=True)
        t.start()

    # --- 中止 ---
    def _cancel(self):
        if not self.running:
            return
        self.cancel_event.set()
        self.cancel_btn.config(state="disabled")
        self.result_var.set("中止しています...（実行中のファイルが終わり次第停止します）")
        self._append_log("中止を要求しました。")

    # --- queue を監視して UI を更新 ---
    def _poll_queue(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                kind = msg[0]
                if kind == "start":
                    self.progress["maximum"] = max(1, msg[1])
                    self.progress["value"] = 0
                elif kind == "progress":
                    self.progress["value"] = msg[1]
                    self.result_var.set(f"処理中... {msg[1]}/{msg[2]}")
                elif kind == "log":
                    self._append_log(msg[1])
                elif kind == "done":
                    self.result_var.set(msg[2])
                    self.running = False
                    self.run_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
                    self.cancel_event.clear()
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _on_close(self):
        if self.running:
            if not messagebox.askyesno(
                    "確認", "処理中です。中止して終了してもよろしいですか？"):
                return
            self.cancel_event.set()
            self.after(300, self.destroy)
            return
        self._save_ui_to_cfg()
        save_config(self.cfg)
        self.destroy()


def show_dependency_error(message):
    """コンソールと GUI の両方に依存不足を伝える(コンソールのみでも動くようにする)。"""
    print(message, flush=True)
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("起動できません", message)
        root.destroy()
    except Exception:
        pass


def main():
    missing = missing_required_packages()
    if missing:
        show_dependency_error(dependency_message())
        return 1

    warning = dependency_message()
    app = App()
    if warning:
        app._append_log(warning)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
