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
    "intake_dir": r"C:\PhotoImport",       # 取り込みフォルダ
    "backup_root": r"C:\PhotoBackup",      # バックアップ親フォルダ
    "layout": "YYYY\\MM",                  # 整理方法: flat / YYYY / YYYY\MM / YYYY\MM\DD
    "include_subfolders": True,            # 取り込みフォルダのサブフォルダも対象にするか
    "delete_after_process": True,          # 処理後に取り込みフォルダから削除するか
    "compress_video": False,               # 動画も圧縮するか
    "phash_threshold": 5,                  # pHash ハミング距離のしきい値(これ以下で重複扱い)
    "image_long_side": 1200,               # スマホ戻し用画像の長辺ピクセル
    "jpeg_quality": 80,                    # JPEG 品質(1〜100)
    "video_crf": 28,                       # 動画圧縮の CRF(小さいほど高画質・大容量)
    "video_preset": "medium",              # libx265 のプリセット
    "ffmpeg_path": "ffmpeg",               # ffmpeg 実行パス(PATH に入っていれば "ffmpeg")
}

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
        except Exception:
            pass
    return cfg


def save_config(cfg):
    try:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------- パス派生
def original_dir(cfg):
    return os.path.join(cfg["backup_root"], "Original")


def mobile_dir(cfg):
    return os.path.join(cfg["backup_root"], "Mobile")


def log_path(cfg):
    return os.path.join(cfg["backup_root"], "backup_log.txt")


def hash_db_path(cfg):
    return os.path.join(cfg["backup_root"], "hashes.json")


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

    if hash_store.is_duplicate(ph, cfg["phash_threshold"]):
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


# ---------------------------------------------------------------- 動画処理
def process_video(src, cfg, stats):
    dt = get_video_datetime(src, cfg.get("ffmpeg_path"))

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
        dest_mobile,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-500:]
        raise RuntimeError(f"FFmpeg が失敗: {tail}")

    stats["videos"] += 1
    stats["orig_bytes"] += os.path.getsize(src)
    stats["new_bytes"] += os.path.getsize(dest_mobile)
    return "ok"


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
    バックアップ先(backup_root)が取り込みフォルダの中にある場合は、
    自分が出力したファイルを再処理しないよう除外する。
    """
    intake = os.path.abspath(cfg["intake_dir"])
    backup_root = os.path.abspath(cfg["backup_root"])
    backup_inside_intake = backup_root != intake and backup_root.startswith(
        intake + os.sep)

    def is_candidate(path):
        ext = os.path.splitext(path)[1].lower()
        if ext in IMAGE_EXTS:
            return True
        return bool(cfg["compress_video"] and ext in VIDEO_EXTS)

    def in_backup_tree(path):
        if not backup_inside_intake:
            return False
        p = os.path.abspath(path)
        return p == backup_root or p.startswith(backup_root + os.sep)

    paths = []
    if cfg.get("include_subfolders", True):
        for root, dirs, names in os.walk(intake):
            if in_backup_tree(root):
                dirs[:] = []
                continue
            dirs.sort()
            for name in sorted(names):
                p = os.path.join(root, name)
                if os.path.isfile(p) and is_candidate(p):
                    paths.append(p)
    else:
        for name in sorted(os.listdir(intake)):
            p = os.path.join(intake, name)
            if os.path.isfile(p) and is_candidate(p):
                paths.append(p)

    if backup_inside_intake and log:
        log("WARN", "バックアップ先が取り込みフォルダ内にあります。"
                    "出力済みファイルは処理対象から除外します。")
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
def run_processing(cfg, q):
    log_file = log_path(cfg)

    def log(level, msg):
        emit_log(q, log_file, level, msg)

    for d in (original_dir(cfg), mobile_dir(cfg)):
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

    hash_store = HashStore(hash_db_path(cfg))
    stats = {"images": 0, "videos": 0, "orig_bytes": 0, "new_bytes": 0}
    skipped_dup = 0
    errors = 0

    for i, p in enumerate(files, 1):
        name = os.path.basename(p)
        ext = os.path.splitext(name)[1].lower()
        try:
            if ext in IMAGE_EXTS:
                result = process_image(p, cfg, hash_store, stats)
                if result == "duplicate":
                    skipped_dup += 1
                    log("INFO", f"重複スキップ: {name}")
                else:
                    log("INFO", f"画像処理: {name}")
            else:
                process_video(p, cfg, stats)
                log("INFO", f"動画圧縮: {name}")

            # 成功(重複スキップ含む)したら、設定に応じて取り込み元から削除
            if cfg["delete_after_process"]:
                try:
                    os.remove(p)
                    log("INFO", f"取り込みから削除: {name}")
                except OSError as e:
                    log("WARN", f"削除に失敗: {name} ({e})")
        except Exception as e:
            errors += 1
            log("ERROR", f"処理失敗: {name} ({e})")

        q.put(("progress", i, total))

    hash_store.save()

    # サブフォルダを取り込み元ごと整理した場合、空になったフォルダを片付ける
    if cfg.get("include_subfolders", True) and cfg["delete_after_process"]:
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
    log("INFO", summary.replace("\n", " / "))
    q.put(("done", stats, summary))


# ---------------------------------------------------------------- GUI
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Android 写真 バックアップ＆軽量化ツール")
        self.cfg = load_config()
        self.queue = queue.Queue()
        self.running = False
        self._build_ui()
        self._load_cfg_to_ui()
        self.after(100, self._poll_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if not HEIF_AVAILABLE:
            self._append_log("注意: pillow-heif が未導入のため HEIC を扱えません。"
                             "「pip install pillow-heif」を実行してください。")
    # --- UI 構築 ---
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        f1 = ttk.Frame(self)
        f1.pack(fill="x", **pad)
        ttk.Label(f1, text="取り込みフォルダ:").pack(side="left")
        self.intake_var = tk.StringVar()
        ttk.Entry(f1, textvariable=self.intake_var).pack(
            side="left", fill="x", expand=True, padx=6)
        ttk.Button(f1, text="参照...", command=self._browse).pack(side="left")

        f2 = ttk.LabelFrame(self, text="設定")
        f2.pack(fill="x", **pad)

        row = ttk.Frame(f2)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="整理方法:").pack(side="left")
        self.layout_combo = ttk.Combobox(
            row, state="readonly", width=26, values=list(LAYOUT_LABELS.values()))
        self.layout_combo.pack(side="left", padx=6)

        self.delete_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(f2, text="処理後に取り込みフォルダから削除する",
                        variable=self.delete_var).pack(anchor="w")
        self.subdir_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(f2, text="サブフォルダも対象にする(DCIM\\Camera などの階層に対応)",
                        variable=self.subdir_var).pack(anchor="w")
        self.video_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f2, text="動画も圧縮する(要 FFmpeg)",
                        variable=self.video_var).pack(anchor="w")

        row2 = ttk.Frame(f2)
        row2.pack(fill="x", pady=2)
        ttk.Label(row2, text="pHash しきい値(小さいほど厳密):").pack(side="left")
        self.threshold_var = tk.StringVar(value="5")
        ttk.Spinbox(row2, from_=0, to=20, width=5,
                    textvariable=self.threshold_var).pack(side="left", padx=6)

        f3 = ttk.Frame(self)
        f3.pack(fill="x", **pad)
        self.run_btn = ttk.Button(f3, text="実行", command=self._start)
        self.run_btn.pack(side="left")
        self.progress = ttk.Progressbar(f3, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)

        self.result_var = tk.StringVar(value="フォルダを確認して「実行」を押してください。")
        ttk.Label(self, textvariable=self.result_var, wraplength=560,
                  justify="left").pack(fill="x", **pad)

        self.log_text = scrolledtext.ScrolledText(self, height=12, state="disabled")
        self.log_text.pack(fill="both", expand=True, **pad)

    # --- UI ヘルパ ---
    def _append_log(self, text):
        self.log_text.config(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.intake_var.get() or None,
                                    title="取り込みフォルダを選択")
        if d:
            self.intake_var.set(d)

    @staticmethod
    def _layout_key_from_label(label):
        for k, v in LAYOUT_LABELS.items():
            if v == label:
                return k
        return "YYYY\\MM"

    @staticmethod
    def _layout_label_from_key(key):
        return LAYOUT_LABELS.get(key, LAYOUT_LABELS["YYYY\\MM"])

    def _load_cfg_to_ui(self):
        self.intake_var.set(self.cfg["intake_dir"])
        self.layout_combo.set(self._layout_label_from_key(self.cfg["layout"]))
        self.delete_var.set(bool(self.cfg["delete_after_process"]))
        self.subdir_var.set(bool(self.cfg.get("include_subfolders", True)))
        self.video_var.set(bool(self.cfg["compress_video"]))
        self.threshold_var.set(str(self.cfg["phash_threshold"]))

    def _save_ui_to_cfg(self):
        self.cfg["intake_dir"] = self.intake_var.get().strip() or DEFAULT_CONFIG["intake_dir"]
        self.cfg["layout"] = self._layout_key_from_label(self.layout_combo.get())
        self.cfg["delete_after_process"] = bool(self.delete_var.get())
        self.cfg["include_subfolders"] = bool(self.subdir_var.get())
        self.cfg["compress_video"] = bool(self.video_var.get())
        try:
            self.cfg["phash_threshold"] = int(float(self.threshold_var.get()))
        except Exception:
            self.cfg["phash_threshold"] = DEFAULT_CONFIG["phash_threshold"]

    # --- 実行 ---
    def _start(self):
        if self.running:
            return
        self._save_ui_to_cfg()
        save_config(self.cfg)

        if not os.path.isdir(self.cfg["intake_dir"]):
            messagebox.showerror(
                "エラー", f"取り込みフォルダが見つかりません:\n{self.cfg['intake_dir']}")
            return

        self.running = True
        self.run_btn.config(state="disabled")
        self.progress["value"] = 0
        self.result_var.set("処理中...")
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

        t = threading.Thread(target=run_processing,
                             args=(self.cfg, self.queue), daemon=True)
        t.start()

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
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _on_close(self):
        if self.running:
            if not messagebox.askyesno("確認", "処理中です。終了してもよろしいですか？"):
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
