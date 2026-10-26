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

import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta, timezone
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

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

try:
    from send2trash import send2trash

    SEND2TRASH_AVAILABLE = True
except Exception:
    send2trash = None
    SEND2TRASH_AVAILABLE = False

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
    "datetime_mode": "exif_local",             # 撮影日時: exif_local / exif_utc_to_local
    "include_subfolders": True,                # 取り込みフォルダのサブフォルダも対象にするか
    "delete_after_process": False,             # 処理後に取り込みフォルダから削除するか(既定は残す)
    "delete_duplicates": False,                # (旧設定) 重複スキップしたファイルも削除するか
    "duplicate_action": "keep",                # 重複時の扱い: keep / move / delete
    "use_recycle_bin": True,                   # 削除はゴミ箱へ移動する(可能な場合)
    "compress_video": False,                   # 動画も圧縮するか
    "enable_dedup": False,                     # 重複スキップを行うか(既定はオフ)
    "dedup_mode": "phash",                     # 判定方法: phash / name / both
    "phash_threshold": 5,                      # pHash ハミング距離のしきい値(これ以下で重複扱い)
    "image_long_side": 1200,                   # スマホ戻し用画像の長辺ピクセル
    "jpeg_quality": 80,                        # JPEG 品質(1〜100)
    "video_crf": 28,                           # 動画圧縮の CRF(小さいほど高画質・大容量)
    "video_preset": "medium",                  # libx265 / libx264 のプリセット
    "video_codec": "hevc",                     # 動画コーデック: hevc / h264
    "mobile_format": "jpg",                    # Mobile の出力形式: jpg / heic(HEIC 入力のみ)
    "workers": 1,                              # 画像処理の並列数(1 で逐次)
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

if getattr(sys, "frozen", False):
    # exe 化した場合、__file__ は一時展開先を指すため exe の場所を使う
    SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- 設定の読み書き
def config_path():
    """設定ファイルの場所。環境変数 PHOTO_BACKUP_CONFIG で上書きできる。"""
    override = os.environ.get("PHOTO_BACKUP_CONFIG")
    if override:
        return os.path.normpath(override)
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


# ---------------------------------------------------------------- 安全なファイル操作
def copy_verified(src, dest):
    """サイズ検証つきのコピー。

    .part へ書いてサイズを確認してから確定させるため、容量不足などで
    書き込みが途中で切れても壊れたファイルが残らず、成功と誤認しない。
    """
    tmp = dest + ".part"
    try:
        shutil.copy2(src, tmp)
        expected = os.path.getsize(src)
        actual = os.path.getsize(tmp)
        if actual != expected:
            raise IOError(
                f"コピー後のサイズが一致しません（{actual} / {expected} バイト）")
        os.replace(tmp, dest)
        return dest
    finally:
        if os.path.exists(tmp):
            _remove_quietly(tmp)


def delete_file(path, use_recycle_bin=True):
    """ファイルを削除する。可能ならゴミ箱へ移動し、失敗したら通常削除する。

    戻り値は "trash" / "deleted"。
    """
    if use_recycle_bin and SEND2TRASH_AVAILABLE:
        try:
            send2trash(str(path))
            return "trash"
        except Exception:
            pass
    os.remove(path)
    return "deleted"


def free_space_gb(path):
    """保存先を含むドライブの空き容量(GB)。取得できなければ None。

    まだ存在しないフォルダでも、存在する親から判定する(下見で作成しないため)。
    """
    p = os.path.abspath(path)
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent
    try:
        return shutil.disk_usage(p).free / (1024 ** 3)
    except OSError:
        return None


def check_free_space(cfg, files, log):
    """処理対象の合計サイズと保存先の空き容量を比較して警告する。"""
    try:
        total = sum(os.path.getsize(f) for f in files)
    except OSError:
        return
    if total <= 0:
        return
    total_gb = total / (1024 ** 3)
    # Original は入力と同量、Mobile はおおよそ半分と見積もる
    for label, path, ratio in (("Original", original_dir(cfg), 1.0),
                               ("Mobile", mobile_dir(cfg), 0.5)):
        free = free_space_gb(path)
        if free is None:
            continue
        need = total_gb * ratio
        if free < need:
            log("WARN", f"{label} の空き容量が不足するおそれがあります"
                        f"（空き {free:.1f} GB / 必要目安 {need:.1f} GB）")


# ---------------------------------------------------------------- 重複したファイルの扱い
DUPLICATE_ACTIONS = {
    "keep": "取り込みに残す",
    "move": "Duplicates へ退避",
    "delete": "削除する",
}


def duplicate_dir(cfg):
    return os.path.join(work_dir(cfg), "Duplicates")


def move_to_duplicates(path, cfg):
    """重複したファイルを Duplicates フォルダへ移す。"""
    d = duplicate_dir(cfg)
    os.makedirs(d, exist_ok=True)
    dest = unique_path(d, os.path.basename(path))
    shutil.move(path, dest)
    return dest


def effective_duplicate_action(cfg):
    action = cfg.get("duplicate_action", "keep")
    # 旧設定(delete_duplicates)との互換
    if cfg.get("delete_duplicates") and action == "keep":
        action = "delete"
    return action if action in DUPLICATE_ACTIONS else "keep"


def handle_duplicate(path, cfg, label, log):
    """重複と判定したファイルを設定に従って処理する。

    取り込み元に残した場合は True、移動/削除した場合は False を返す。
    """
    name = os.path.basename(path)
    action = effective_duplicate_action(cfg)
    if action == "delete":
        try:
            delete_file(path, cfg.get("use_recycle_bin", True))
            log("INFO", f"重複スキップ（{label}・削除）: {name}")
            return False
        except OSError as e:
            log("WARN", f"重複ファイルを削除できません: {name} ({e})")
            return True
    if action == "move":
        try:
            dest = move_to_duplicates(path, cfg)
            log("INFO", f"重複スキップ（{label}・退避）: {name} → {dest}")
            return False
        except OSError as e:
            log("WARN", f"重複ファイルを退避できません: {name} ({e})")
            return True
    log("INFO", f"重複スキップ（{label}・取り込みに残します）: {name}")
    return True


# ---------------------------------------------------------------- 事前確認・ログ・プロファイル
def rotate_log(cfg, max_bytes=5 * 1024 * 1024):
    """ログが大きくなったら日付入りの名前で退避する。"""
    p = log_path(cfg)
    try:
        if os.path.exists(p) and os.path.getsize(p) > max_bytes:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            os.replace(p, os.path.join(work_dir(cfg), f"backup_log.{stamp}.txt"))
            return True
    except OSError:
        pass
    return False


def analyze_intake(cfg):
    """実行前の下見。ファイルの書き込みやフォルダ作成は行わない。"""
    files = collect_intake_files(cfg)
    images = [f for f in files if os.path.splitext(f)[1].lower() in IMAGE_EXTS]
    videos = [f for f in files if os.path.splitext(f)[1].lower() in VIDEO_EXTS]
    total = 0
    for f in files:
        try:
            total += os.path.getsize(f)
        except OSError:
            continue
    names = collect_existing_names(cfg)
    ledger = Ledger(ledger_path(cfg))
    name_hits = sum(1 for f in files
                    if os.path.splitext(os.path.basename(f))[0].lower() in names)
    ledger_hits = sum(1 for f in files if ledger.has(f))
    return {
        "total": len(files),
        "images": len(images),
        "videos": len(videos),
        "bytes": total,
        "name_hits": name_hits,
        "ledger_hits": ledger_hits,
        "free_original": free_space_gb(original_dir(cfg)),
        "free_mobile": free_space_gb(mobile_dir(cfg)),
    }


def profiles_path():
    return os.path.join(SCRIPT_DIR, "config_profiles.json")


def load_profiles():
    try:
        with open(profiles_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, dict)}
    except Exception:
        pass
    return {}


def save_profiles(profiles):
    try:
        with open(profiles_path(), "w", encoding="utf-8") as f:
            json.dump(profiles, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


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


# ---------------------------------------------------------------- 重複判定
DEDUP_MODES = {
    "phash": "pHash（見た目）",
    "name": "ファイル名",
    "ledger": "処理済み記録",
    "both": "すべて",
}


def collect_existing_names(cfg):
    """出力済みファイルの名前(拡張子を除く・小文字)を集める。

    ファイル名での重複判定に使う。保存先を走査するので、
    台帳ファイルを持たなくても既存の出力から復元できる。
    """
    names = set()
    for base in (original_dir(cfg), mobile_dir(cfg)):
        if not os.path.isdir(base):
            continue
        for _root, _dirs, files in os.walk(base):
            for name in files:
                names.add(os.path.splitext(name)[0].lower())
    return names


def ledger_path(cfg):
    return os.path.join(work_dir(cfg), "processed.json")


class Ledger:
    """処理済みファイルの台帳。

    「名前 + サイズ + 更新日時」を鍵にするので、同じ名前でも中身が違えば
    別ファイルとして扱える(連番がリセットされた別カメラの同名写真など)。
    """

    def __init__(self, path):
        self.path = path
        self._entries = {}
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("entries"), dict):
                self._entries = data["entries"]
        except Exception:
            self._entries = {}

    @staticmethod
    def key_for(path):
        st = os.stat(path)
        return f"{os.path.basename(path).lower()}|{st.st_size}|{int(st.st_mtime)}"

    def has(self, path):
        try:
            return self.key_for(path) in self._entries
        except OSError:
            return False

    def add(self, path):
        try:
            st = os.stat(path)
            self._entries[self.key_for(path)] = {
                "name": os.path.basename(path),
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                "path": path,
            }
        except OSError:
            pass

    def __len__(self):
        return len(self._entries)

    def save(self):
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "entries": self._entries},
                          f, ensure_ascii=False)
        except Exception:
            pass


class DuplicateChecker:
    """pHash・ファイル名・処理済み台帳の 3 通りで重複を判定する。

    mode:
        "phash"  … pHash のハミング距離だけで判定
        "name"   … 出力済みと同じファイル名なら重複
        "ledger" … 同じファイルを処理済みなら重複(名前+サイズ+更新日時)
        "both"   … 3 つすべてで判定
    """

    def __init__(self, cfg, log=None):
        self.enabled = bool(cfg.get("enable_dedup", False))
        self.mode = cfg.get("dedup_mode", "phash")
        if self.mode not in DEDUP_MODES:
            self.mode = "phash"
        self.threshold = cfg.get("phash_threshold", 5)
        self.use_phash = self.enabled and self.mode in ("phash", "both")
        self.use_name = self.enabled and self.mode in ("name", "both")
        self.use_ledger = self.enabled and self.mode in ("ledger", "both")
        self.store = HashStore(hash_db_path(cfg))
        self.ledger = Ledger(ledger_path(cfg))
        self.names = set()
        if self.use_name:
            self.names = collect_existing_names(cfg)
            if log:
                log("INFO", f"重複判定(ファイル名): 出力済み {len(self.names)} 件の名前を読み込みました")
        if self.use_ledger and log:
            log("INFO", f"重複判定(処理済み台帳): {len(self.ledger)} 件を読み込みました")

    @staticmethod
    def _stem(path):
        return os.path.splitext(os.path.basename(path))[0].lower()

    def check(self, ph, src):
        """重複なら理由("name"/"ledger"/"phash")を、そうでなければ None を返す。"""
        if not self.enabled:
            return None
        if self.use_name and self._stem(src) in self.names:
            return "name"
        if self.use_ledger and self.ledger.has(src):
            return "ledger"
        if self.use_phash and self.store.is_duplicate(ph, self.threshold):
            return "phash"
        return None

    def accept(self, ph, src):
        """処理したファイルを記録する(重複判定がオフでも履歴は残す)。"""
        self.store.add(ph)
        self.ledger.add(src)
        if self.use_name:
            self.names.add(self._stem(src))

    def save(self):
        self.store.save()
        self.ledger.save()


# ---------------------------------------------------------------- 撮影日時の取得
DATETIME_MODES = {
    "exif_local": "EXIF の現地時刻",
    "exif_utc_to_local": "端末のタイムゾーンに変換",
}

# EXIF のタイムゾーン表記("+09:00" など)
_OFFSET_RE = re.compile(r"([+-])(\d{2}):?(\d{2})")


def parse_exif_offset(value):
    """EXIF の OffsetTime("+09:00") を timezone に変換する。"""
    m = _OFFSET_RE.fullmatch(str(value).strip())
    if not m:
        return None
    sign = 1 if m.group(1) == "+" else -1
    return timezone(sign * timedelta(hours=int(m.group(2)),
                                     minutes=int(m.group(3))))


def get_photo_datetime(im, src, mode="exif_local"):
    """撮影日時を返す。

    mode:
        "exif_local"        … EXIF の現地時刻をそのまま使う(従来の動作)
        "exif_utc_to_local" … EXIF のタイムゾーンで UTC に直し、PC の時刻に変換
    """
    try:
        exif = im.getexif()
        s = exif.get(0x9003) or exif.get(0x0132)  # DateTimeOriginal / DateTime
        if s:
            dt = datetime.strptime(str(s).strip(), "%Y:%m:%d %H:%M:%S")
            if mode == "exif_utc_to_local":
                offset = exif.get(0x9011) or exif.get(0x9010)  # OffsetTimeOriginal / OffsetTime
                tz = parse_exif_offset(offset) if offset else None
                if tz is not None:
                    dt = dt.replace(tzinfo=tz).astimezone().replace(tzinfo=None)
            return dt
    except Exception:
        pass
    return datetime.fromtimestamp(os.path.getmtime(src))


# ---------------------------------------------------------------- 画像処理
def prepare_image(src, cfg):
    """画像を読み、pHash・撮影日時・縮小データを用意する(保存はしない)。

    並列処理でも安全なように、ファイルの書き込みは行わない。
    """
    with Image.open(src) as im:
        im.load()
        exif = im.getexif()
        icc = im.info.get("icc_profile")
        dt = get_photo_datetime(im, src, cfg.get("datetime_mode", "exif_local"))

        # 向き(Orientation)を適用して、以後は正立画像として扱う
        try:
            oriented = ImageOps.exif_transpose(im)
        except Exception:
            oriented = im
        if oriented is im:
            oriented = im.copy()

        # pHash は向き適用後の画像から算出(向きの違いで別物扱いされないように)
        ph = imagehash.phash(oriented.convert("RGB"), hash_size=8)

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

        save_kwargs = {"quality": cfg["jpeg_quality"], "optimize": True}
        try:
            exif_bytes = exif.tobytes()
            if exif_bytes:
                save_kwargs["exif"] = exif_bytes  # 撮影日時・GPS などを可能な限り保持
        except Exception:
            pass
        if icc:
            save_kwargs["icc_profile"] = icc  # 色プロファイルを保持

        data = None
        out_ext = ".jpg"
        # HEIC のまま出力(色域・HDR を保ちたい場合)
        if (cfg.get("mobile_format") == "heic" and HEIF_AVAILABLE
                and os.path.splitext(src)[1].lower() in (".heic", ".heif")):
            try:
                buf = io.BytesIO()
                img.save(buf, "HEIF", quality=cfg["jpeg_quality"])
                data = buf.getvalue()
                out_ext = ".heic"
            except Exception:
                data = None

        if data is None:
            buf = io.BytesIO()
            img.save(buf, "JPEG", **save_kwargs)
            data = buf.getvalue()

    return {"src": src, "phash": ph, "dt": dt, "data": data, "ext": out_ext}


def write_prepared(prep, cfg, checker, stats):
    """prepare_image の結果を実際に保存する。"""
    src = prep["src"]
    dt = prep["dt"]

    # 1) 元画像を Original へコピー
    dest_orig_dir = os.path.join(original_dir(cfg), layout_subfolder(dt, cfg["layout"]))
    os.makedirs(dest_orig_dir, exist_ok=True)
    copy_verified(src, unique_path(dest_orig_dir, os.path.basename(src)))

    # 2) 縮小済みデータを Mobile へ書き出し
    dest_mobile_dir = os.path.join(mobile_dir(cfg), layout_subfolder(dt, cfg["layout"]))
    os.makedirs(dest_mobile_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(src))[0]
    dest_mobile = unique_path(dest_mobile_dir, base + prep["ext"])
    with open(dest_mobile, "wb") as f:
        f.write(prep["data"])

    checker.accept(prep["phash"], src)

    stats["images"] += 1
    stats["orig_bytes"] += os.path.getsize(src)
    stats["new_bytes"] += os.path.getsize(dest_mobile)
    return dest_mobile


def process_image(src, cfg, checker, stats):
    """1 枚を処理する(重複判定込み)。"""
    prep = prepare_image(src, cfg)
    reason = checker.check(prep["phash"], src)
    if reason:
        return f"duplicate:{reason}"
    write_prepared(prep, cfg, checker, stats)
    return "ok"


def iter_prepared(files, cfg, workers, cancel_event=None):
    """prepare_image を順に返す。workers > 1 のときは先読みして並列実行する。"""
    if workers <= 1:
        for f in files:
            if cancel_event is not None and cancel_event.is_set():
                return
            try:
                yield prepare_image(f, cfg)
            except Exception as e:
                yield {"src": f, "error": str(e), "phash": None}
        return

    from concurrent.futures import ThreadPoolExecutor

    window = max(2, workers * 3)
    pending = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        it = iter(files)
        exhausted = False
        while True:
            while not exhausted and len(pending) < window:
                if cancel_event is not None and cancel_event.is_set():
                    exhausted = True
                    break
                try:
                    f = next(it)
                except StopIteration:
                    exhausted = True
                    break
                pending.append((f, ex.submit(prepare_image, f, cfg)))
            if not pending:
                return
            f, future = pending.pop(0)
            try:
                yield future.result()
            except Exception as e:
                yield {"src": f, "error": str(e), "phash": None}


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


# ---------------------------------------------------------------- 動画の追加情報
VIDEO_CODECS = {
    "hevc": "H.265（HEVC・高圧縮）",
    "h264": "H.264（互換性重視）",
}


def probe_streams(src, ffmpeg_path):
    """動画の長さと音声コーデックを ffprobe で調べる。"""
    info = {"duration": None, "audio": None}
    ffprobe = resolve_ffprobe(ffmpeg_path)
    if not ffprobe:
        return info
    base = [ffprobe, "-v", "error", "-show_entries"]
    try:
        proc = subprocess.run(
            base + ["format=duration:stream=codec_type,codec_name", "-of", "json", src],
            capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            data = json.loads(proc.stdout or "{}")
            try:
                info["duration"] = float(data.get("format", {}).get("duration"))
            except (TypeError, ValueError):
                info["duration"] = None
            for stream in data.get("streams", []):
                if stream.get("codec_type") == "audio":
                    info["audio"] = stream.get("codec_name")
                    break
    except Exception:
        pass
    return info


def _parse_out_time(value):
    """ffmpeg の -progress が出す HH:MM:SS.micro を秒に直す。"""
    try:
        h, m, s = value.strip().split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception:
        return None


# ---------------------------------------------------------------- 動画処理
def process_video(src, cfg, stats, cancel_event=None, progress_cb=None):
    dt = get_video_datetime(src, cfg.get("ffmpeg_path"))

    if cancel_event is not None and cancel_event.is_set():
        raise Cancelled()

    # 元動画を Original へコピー
    dest_orig_dir = os.path.join(original_dir(cfg), layout_subfolder(dt, cfg["layout"]))
    os.makedirs(dest_orig_dir, exist_ok=True)
    dest_orig = unique_path(dest_orig_dir, os.path.basename(src))
    copy_verified(src, dest_orig)

    # 圧縮して Mobile へ出力(コンテナは MP4 に統一)
    dest_mobile_dir = os.path.join(mobile_dir(cfg), layout_subfolder(dt, cfg["layout"]))
    os.makedirs(dest_mobile_dir, exist_ok=True)
    base, _ = os.path.splitext(os.path.basename(src))
    dest_mobile = unique_path(dest_mobile_dir, base + ".mp4")

    probe = probe_streams(src, cfg.get("ffmpeg_path"))
    duration = probe["duration"]
    audio = probe["audio"]
    # 音声が既に AAC なら再エンコードせずコピーする(劣化なし・高速)
    if audio == "aac":
        audio_args = ["-c:a", "copy"]
    else:
        audio_args = ["-c:a", "aac", "-b:a", "128k"]

    codec = cfg.get("video_codec", "hevc")
    if codec == "h264":
        video_args = ["-c:v", "libx264", "-crf", str(cfg["video_crf"]),
                      "-preset", cfg["video_preset"], "-tag:v", "avc1",
                      "-pix_fmt", "yuv420p"]
    else:
        video_args = ["-c:v", "libx265", "-crf", str(cfg["video_crf"]),
                      "-preset", cfg["video_preset"], "-tag:v", "hvc1"]

    cmd = ([cfg["ffmpeg_path"], "-y", "-i", src,
            "-map", "0:v:0", "-map", "0:a?"]
           + video_args + audio_args
           + ["-movflags", "+faststart",
              "-progress", "pipe:1", "-nostats", "-loglevel", "error",
              dest_mobile])
    # 中止要求にすぐ反応できるよう、完了を待たずに一定間隔で状態を見る
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    err_lines = []

    def read_stdout():
        for line in proc.stdout:
            if not line.startswith("out_time="):
                continue
            seconds = _parse_out_time(line.split("=", 1)[1])
            if seconds is None or not duration or duration <= 0:
                continue
            if progress_cb:
                try:
                    progress_cb(min(99.0, seconds / duration * 100.0))
                except Exception:
                    pass

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    try:
        while True:
            try:
                proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if cancel_event is not None and cancel_event.is_set():
                    proc.kill()
                    raise Cancelled()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        reader.join(timeout=2)
        tail_err = ""
        try:
            tail_err = (proc.stderr.read() or "")
        except Exception:
            pass
        try:
            proc.stdout.close()
            proc.stderr.close()
        except Exception:
            pass

    if proc.returncode != 0:
        if cancel_event is not None and cancel_event.is_set():
            _remove_quietly(dest_mobile)
            raise Cancelled()
        tail = tail_err.strip()[-500:]
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


# ---------------------------------------------------------------- FFmpeg の探索
COMMON_FFMPEG_PATHS = [
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
]


def ffmpeg_candidates(cfg):
    """設定・PATH・同梱フォルダ・よくある導入先から候補を並べる。"""
    configured = (cfg.get("ffmpeg_path") or "ffmpeg").strip().strip('"')
    found = [configured]
    located = shutil.which(configured) if configured else None
    if located:
        found.append(located)
    if not os.path.isabs(configured):
        found += [
            os.path.join(SCRIPT_DIR, "ffmpeg.exe"),
            os.path.join(SCRIPT_DIR, "bin", "ffmpeg.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""),
                         "Microsoft", "WinGet", "Links", "ffmpeg.exe"),
        ]
        found += COMMON_FFMPEG_PATHS
    # 重複を除く
    seen = set()
    unique = []
    for c in found:
        if not c or c.lower() in seen:
            continue
        seen.add(c.lower())
        unique.append(c)
    return unique


def find_ffmpeg(cfg):
    """実際に起動できる ffmpeg のパスを返す(見つからなければ None)。"""
    for candidate in ffmpeg_candidates(cfg):
        if check_ffmpeg(candidate):
            return candidate
    return None


# ---------------------------------------------------------------- 保存先の書き込み確認
def is_writable_dir(path):
    """実際にファイルを作って書き込み可否を確かめる。(可否, エラー文字列)"""
    probe = os.path.join(path, f".write_test_{os.getpid()}_{int(time.time() * 1000)}.tmp")
    try:
        os.makedirs(path, exist_ok=True)
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True, None
    except OSError as e:
        return False, str(e)


def check_writable_dirs(cfg):
    """保存先に書き込めるかを実際に試す。問題があれば [(名前, パス, 例外)] を返す。"""
    problems = []
    targets = (("Original", original_dir(cfg)),
               ("Mobile", mobile_dir(cfg)),
               ("ログ・重複DB", work_dir(cfg)))
    for label, path in targets:
        ok, err = is_writable_dir(path)
        if not ok:
            problems.append((label, path, err))
    return problems


def diagnose_environment(cfg):
    """書き込み不可の原因を切り分けるための情報を集める。"""
    import tempfile

    targets = [
        ("exe のフォルダ", SCRIPT_DIR),
        ("TEMP", tempfile.gettempdir()),
        ("Original", original_dir(cfg)),
        ("Mobile", mobile_dir(cfg)),
        ("ログ・重複DB", work_dir(cfg)),
        ("取り込みフォルダ", cfg.get("intake_dir", "")),
    ]
    results = []
    for label, path in targets:
        if not path:
            continue
        if os.path.isdir(path) and label == "取り込みフォルダ":
            ok, err = is_writable_dir(path)
        else:
            ok, err = is_writable_dir(path)
        results.append((label, path, ok, err))
    return {
        "user": os.environ.get("USERNAME", ""),
        "exe": sys.executable,
        "cwd": os.getcwd(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "results": results,
    }


def restricted_environment_hint(info):
    """TEMP には書けるのに保存先に書けない場合のヒントを返す。"""
    ok_map = {label: ok for label, _p, ok, _e in info["results"]}
    if ok_map.get("TEMP") and ok_map.get("exe のフォルダ"):
        if not any(ok_map.get(k) for k in ("Original", "Mobile", "ログ・重複DB")):
            return ("TEMP には書き込めるのに保存先には書き込めません。"
                    "エージェントやサンドボックス、仮想環境など制限された場所から"
                    "起動している可能性があります。エクスプローラーから直接 "
                    "PhotoBackup.exe をダブルクリックして起動してください。")
    return None


def format_diagnostics(info):
    lines = [
        f"ユーザー: {info['user']}",
        f"実行ファイル: {info['exe']}",
        f"作業フォルダ: {info['cwd']}",
        f"exe 版: {'はい' if info['frozen'] else 'いいえ（ソースから実行）'}",
        "",
        "書き込みテスト:",
    ]
    for label, path, ok, err in info["results"]:
        mark = "OK" if ok else "NG"
        line = f"  [{mark}] {label}: {path}"
        if err:
            line += f"  ({err})"
        lines.append(line)
    hint = restricted_environment_hint(info)
    if hint:
        lines += ["", "推定原因:", hint]
    return "\n".join(lines)


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
    rotate_log(cfg)

    def log(level, msg):
        emit_log(q, log_file, level, msg)

    def is_cancelled():
        return cancel_event is not None and cancel_event.is_set()

    # 保存先に書き込めるかを先に確認する(600 件全部失敗するのを防ぐ)
    problems = check_writable_dirs(cfg)
    if problems:
        for label, path, err in problems:
            log("ERROR", f"{label} に書き込めません: {path} ({err})")
        info = diagnose_environment(cfg)
        hint = restricted_environment_hint(info)
        if hint:
            msg = "保存先に書き込めないため処理を中止しました。\n" + hint
        else:
            msg = ("保存先に書き込めないため処理を中止しました。\n"
                   "・書き込みできるフォルダ（例 C:\\PhotoBackup）を指定してください\n"
                   "・デスクトップや OneDrive の同期フォルダは、セキュリティソフトに"
                   "よって書き込みを止められることがあります\n"
                   "・保存先のアクセス権をご確認ください")
        log("ERROR", msg.replace("\n", " / "))
        q.put(("done", None, msg, False))
        return

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
    if video_files:
        found = find_ffmpeg(cfg)
        if not found:
            log("ERROR", "FFmpeg が見つかりません。設定画面の「FFmpeg」で場所を指定するか、"
                         "PATH に追加してください。")
            files = [p for p in files if os.path.splitext(p)[1].lower() not in VIDEO_EXTS]
        else:
            cfg = dict(cfg, ffmpeg_path=found)
            log("INFO", f"FFmpeg: {found}")

    if not files:
        msg = "処理対象のファイルがありません。"
        log("INFO", msg)
        q.put(("done", None, msg))
        return

    total = len(files)
    q.put(("start", total))
    log("INFO", f"処理開始: {total} 件 ({intake})")
    check_free_space(cfg, files, log)

    checker = DuplicateChecker(cfg, log)
    if checker.enabled:
        detail = []
        if checker.use_phash:
            detail.append(f"pHash 距離 {checker.threshold} 以下")
        if checker.use_name:
            detail.append("ファイル名が一致")
        if checker.use_ledger:
            detail.append("処理済みのファイル")
        log("INFO", f"重複スキップ: 有効（{DEDUP_MODES[checker.mode]} / "
                    + " または ".join(detail) + "）")
    else:
        log("INFO", "重複スキップ: 無効")

    stats = {"images": 0, "videos": 0, "orig_bytes": 0, "new_bytes": 0}
    skipped_dup = 0
    skipped_name = 0
    errors = 0
    progress_done = 0

    image_files = [p for p in files if os.path.splitext(p)[1].lower() in IMAGE_EXTS]
    movie_files = [p for p in files if os.path.splitext(p)[1].lower() in VIDEO_EXTS]
    workers = max(1, int(cfg.get("workers", 1) or 1))
    if workers > 1:
        log("INFO", f"画像の並列処理: {workers} スレッド")

    # --- 画像 ---
    for prep in iter_prepared(image_files, cfg, workers, cancel_event):
        if is_cancelled():
            cancelled = True
            log("WARN", "中止要求を受け付けたため、残りのファイルを処理せず停止しました。")
            break
        src = prep["src"]
        name = os.path.basename(src)
        progress_done += 1
        try:
            if prep.get("error"):
                raise RuntimeError(prep["error"])
            reason = checker.check(prep["phash"], src)
            if reason:
                skipped_dup += 1
                if reason == "name":
                    skipped_name += 1
                label = {"name": "同名ファイルあり",
                         "ledger": "処理済み",
                         "phash": "類似画像"}.get(reason, "類似画像")
                handle_duplicate(src, cfg, label, log)
            else:
                write_prepared(prep, cfg, checker, stats)
                log("INFO", f"画像処理: {name}")
                if cfg["delete_after_process"]:
                    try:
                        delete_file(src, cfg.get("use_recycle_bin", True))
                        log("INFO", f"取り込みから削除: {name}")
                    except OSError as e:
                        log("WARN", f"削除に失敗: {name} ({e})")
        except Exception as e:
            errors += 1
            log("ERROR", f"処理失敗: {name} ({e})")

        q.put(("progress", progress_done, total))

    # --- 動画 ---
    for p in movie_files:
        if cancelled or is_cancelled():
            cancelled = True
            log("WARN", "中止要求を受け付けたため、残りのファイルを処理せず停止しました。")
            break
        name = os.path.basename(p)
        progress_done += 1
        try:
            last = {"pct": 0.0}

            def report(pct, _name=name, _last=last):
                if pct - _last["pct"] >= 2:
                    _last["pct"] = pct
                    q.put(("video_progress", pct, _name))

            process_video(p, cfg, stats, cancel_event, report)
            log("INFO", f"動画圧縮: {name}")
            if cfg["delete_after_process"]:
                try:
                    delete_file(p, cfg.get("use_recycle_bin", True))
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

        q.put(("progress", progress_done, total))

    # 1 件も処理せずに中止した場合も、その旨を残す
    if is_cancelled() and not cancelled:
        cancelled = True
        log("WARN", "中止要求を受け付けたため、残りのファイルを処理せず停止しました。")

    checker.save()

    # サブフォルダを取り込み元ごと整理した場合、空になったフォルダを片付ける
    if cfg.get("include_subfolders", True) and cfg["delete_after_process"] and not cancelled:
        removed_dirs = remove_empty_subdirs(intake)
        if removed_dirs:
            log("INFO", f"空フォルダを削除: {removed_dirs} 件")

    orig_mb = stats["orig_bytes"] / (1024 * 1024)
    new_mb = stats["new_bytes"] / (1024 * 1024)
    reduction = (1 - stats["new_bytes"] / stats["orig_bytes"]) * 100 \
        if stats["orig_bytes"] > 0 else 0.0

    dup_detail = f"重複スキップ {skipped_dup} 件"
    if skipped_name:
        dup_detail += f"（うち同名 {skipped_name} 件）"
    summary = (
        f"画像 {stats['images']} 枚 / 動画 {stats['videos']} 件 を処理"
        f"（{dup_detail} / エラー {errors} 件）\n"
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
        self.profiles = load_profiles()
        self._watch_signature = None
        self._last_watch_check = 0.0
        self._build_ui()
        self._load_cfg_to_ui()
        self.after(100, self._poll_queue)
        self.after(2000, self._watch_tick)
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

        # 設定プロファイル
        f_prof = ttk.Frame(f_dest)
        f_prof.grid(row=3, column=0, columnspan=3, sticky="w", padx=6, pady=(4, 2))
        ttk.Label(f_prof, text="プロファイル:").pack(side="left")
        self.profile_combo = ttk.Combobox(f_prof, state="readonly", width=24)
        self.profile_combo.pack(side="left", padx=4)
        self.profile_combo.bind("<<ComboboxSelected>>", self._apply_profile)
        self.profile_save_btn = ttk.Button(
            f_prof, text="保存...", width=8, command=self._save_profile)
        self.profile_save_btn.pack(side="left", padx=(8, 0))
        self.profile_del_btn = ttk.Button(
            f_prof, text="削除", width=6, command=self._delete_profile)
        self.profile_del_btn.pack(side="left", padx=4)

        # 整理・動作
        f_set = ttk.LabelFrame(self, text="整理と動作")
        f_set.grid(row=2, column=0, sticky="ew", **pad)
        f_set.columnconfigure(1, weight=1)
        ttk.Label(f_set, text="整理方法:").grid(row=0, column=0, sticky="w", padx=6)
        self.layout_combo = ttk.Combobox(
            f_set, state="readonly", width=24, values=list(LAYOUT_LABELS.values()))
        self.layout_combo.grid(row=0, column=1, sticky="w", padx=6, pady=2)

        # 撮影日時の扱い(タイムゾーン)
        f_dt = ttk.Frame(f_set)
        f_dt.grid(row=1, column=0, columnspan=2, sticky="w", padx=6, pady=2)
        ttk.Label(f_dt, text="撮影日時:").pack(side="left")
        self.datetime_combo = ttk.Combobox(
            f_dt, state="readonly", width=24, values=list(DATETIME_MODES.values()))
        self.datetime_combo.pack(side="left", padx=4)
        ttk.Label(f_dt, foreground="#555",
                  text="（海外で撮った写真を日本時間に直す場合は「端末のタイムゾーンに変換」）").pack(
            side="left")

        # 重複スキップ(オン/オフ + 判定方法 + pHash しきい値)
        f_dedup = ttk.Frame(f_set)
        f_dedup.grid(row=2, column=0, columnspan=2, sticky="w", padx=6, pady=2)
        self.dedup_var = tk.BooleanVar(value=False)
        self.dedup_check = ttk.Checkbutton(
            f_dedup, text="重複スキップを有効にする", variable=self.dedup_var,
            command=self._toggle_dedup)
        self.dedup_check.pack(side="left")
        ttk.Label(f_dedup, text="　方式:").pack(side="left")
        self.dedup_mode_combo = ttk.Combobox(
            f_dedup, state="readonly", width=13, values=list(DEDUP_MODES.values()))
        self.dedup_mode_combo.pack(side="left", padx=4)
        self.dedup_mode_combo.bind("<<ComboboxSelected>>", self._on_dedup_mode)
        ttk.Label(f_dedup, text="pHash 閾値:").pack(side="left", padx=(10, 0))
        self.threshold_var = tk.StringVar(value="5")
        self.threshold_spin = ttk.Spinbox(
            f_dedup, from_=0, to=20, width=5, textvariable=self.threshold_var)
        self.threshold_spin.pack(side="left", padx=4)

        # 重複と判定したファイルの扱い
        f_dupact = ttk.Frame(f_set)
        f_dupact.grid(row=3, column=0, columnspan=2, sticky="w", padx=6, pady=2)
        ttk.Label(f_dupact, text="　重複時の扱い:").pack(side="left")
        self.dup_action_combo = ttk.Combobox(
            f_dupact, state="readonly", width=18,
            values=list(DUPLICATE_ACTIONS.values()))
        self.dup_action_combo.pack(side="left", padx=4)

        self.delete_var = tk.BooleanVar(value=False)
        self.subdir_var = tk.BooleanVar(value=True)
        self.video_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f_set, text="処理後に取り込みフォルダから削除する",
                        variable=self.delete_var).grid(
            row=4, column=0, columnspan=2, sticky="w", padx=6)
        ttk.Checkbutton(f_set, text="サブフォルダも対象にする(DCIM\\Camera などの階層に対応)",
                        variable=self.subdir_var).grid(
            row=5, column=0, columnspan=2, sticky="w", padx=6)
        ttk.Checkbutton(f_set, text="動画も圧縮する(要 FFmpeg)",
                        variable=self.video_var).grid(
            row=6, column=0, columnspan=2, sticky="w", padx=6)

        # フォルダ監視
        f_watch = ttk.Frame(f_set)
        f_watch.grid(row=7, column=0, columnspan=2, sticky="w", padx=6, pady=(2, 4))
        self.watch_var = tk.BooleanVar(value=False)
        self.watch_check = ttk.Checkbutton(
            f_watch, text="取り込みフォルダを監視して自動実行",
            variable=self.watch_var, command=self._toggle_watch)
        self.watch_check.pack(side="left")
        ttk.Label(f_watch, text="　間隔:").pack(side="left")
        self.watch_interval_var = tk.StringVar(value="60")
        self.watch_spin = ttk.Spinbox(
            f_watch, from_=10, to=3600, increment=10, width=6, state="disabled",
            textvariable=self.watch_interval_var)
        self.watch_spin.pack(side="left", padx=4)
        ttk.Label(f_watch, text="秒").pack(side="left")

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

        f_opt = ttk.Frame(f_comp)
        f_opt.grid(row=2, column=0, columnspan=4, sticky="ew", padx=6, pady=2)
        self.video_codec_var = tk.StringVar(value="hevc")
        ttk.Label(f_opt, text="動画コーデック:").pack(side="left")
        self.codec_combo = ttk.Combobox(
            f_opt, state="readonly", width=20, values=list(VIDEO_CODECS.values()))
        self.codec_combo.pack(side="left", padx=(4, 12))
        self.heic_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f_opt, text="HEIC は HEIC のまま出力",
                        variable=self.heic_var).pack(side="left", padx=(0, 12))
        ttk.Label(f_opt, text="並列:").pack(side="left")
        self.workers_var = tk.StringVar(value="1")
        ttk.Spinbox(f_opt, from_=1, to=16, increment=1, width=4,
                    textvariable=self.workers_var).pack(side="left", padx=4)
        ttk.Label(f_opt, text="スレッド").pack(side="left")

        ttk.Label(f_comp, foreground="#555",
                  text="長辺を小さく・品質を下げる・CRF を上げるほど容量が減ります"
                       "（動画 CRF は 18〜32 が目安）。").grid(
            row=3, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 4))

        # FFmpeg の場所
        f_ff = ttk.Frame(f_comp)
        f_ff.grid(row=4, column=0, columnspan=4, sticky="ew", padx=6, pady=(0, 6))
        f_ff.columnconfigure(1, weight=1)
        ttk.Label(f_ff, text="FFmpeg:").grid(row=0, column=0, sticky="w")
        self.ffmpeg_var = tk.StringVar(value="ffmpeg")
        ttk.Entry(f_ff, textvariable=self.ffmpeg_var).grid(
            row=0, column=1, sticky="ew", padx=6)
        ttk.Button(f_ff, text="参照...", width=8,
                   command=self._browse_exe).grid(row=0, column=2)
        ttk.Button(f_ff, text="自動検出", width=10,
                   command=self._detect_ffmpeg).grid(row=0, column=3, padx=(6, 0))

        # 実行
        f_run = ttk.Frame(self)
        f_run.grid(row=4, column=0, sticky="ew", **pad)
        self.preview_btn = ttk.Button(f_run, text="事前確認", width=10,
                                      command=self._preview)
        self.preview_btn.pack(side="left")
        self.diagnose_btn = ttk.Button(f_run, text="診断", width=8,
                                       command=self._diagnose)
        self.diagnose_btn.pack(side="left", padx=(6, 0))
        self.run_btn = ttk.Button(f_run, text="実行", width=8, command=self._start)
        self.run_btn.pack(side="left", padx=(6, 0))
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

    def _browse_exe(self):
        current = self.ffmpeg_var.get()
        initial = os.path.dirname(current) if os.path.isabs(current) else ""
        path = filedialog.askopenfilename(
            title="ffmpeg.exe を選択",
            initialdir=initial or None,
            filetypes=[("ffmpeg", "ffmpeg.exe"), ("実行ファイル", "*.exe"), ("すべて", "*.*")])
        if path:
            self.ffmpeg_var.set(os.path.normpath(path))

    def _detect_ffmpeg(self):
        cfg = dict(self.cfg, ffmpeg_path=self.ffmpeg_var.get().strip())
        found = find_ffmpeg(cfg)
        if found:
            self.ffmpeg_var.set(found)
            self._append_log(f"FFmpeg を検出しました: {found}")
        else:
            messagebox.showwarning(
                "見つかりません",
                "FFmpeg を自動検出できませんでした。\n"
                "「参照...」から ffmpeg.exe を指定してください。\n"
                "（例: C:\\ffmpeg\\bin\\ffmpeg.exe）")

    # --- 事前確認 ---
    def _preview(self):
        if self.running:
            return
        self._save_ui_to_cfg()
        if not os.path.isdir(self.cfg["intake_dir"]):
            messagebox.showerror(
                "設定を確認してください",
                f"取り込みフォルダが見つかりません:\n{self.cfg['intake_dir']}")
            return
        try:
            info = analyze_intake(self.cfg)
        except Exception as e:
            messagebox.showerror("事前確認に失敗しました", str(e))
            return

        total_mb = info["bytes"] / (1024 * 1024)
        free_o = info["free_original"]
        free_m = info["free_mobile"]
        lines = [
            f"対象: {info['total']} 件（画像 {info['images']} / 動画 {info['videos']}）",
            f"合計サイズ: {total_mb:,.1f} MB",
            "",
            f"同名でスキップ見込み: {info['name_hits']} 件",
            f"処理済みでスキップ見込み: {info['ledger_hits']} 件",
        ]
        if info["videos"] and not check_ffmpeg(self.cfg["ffmpeg_path"]):
            lines.append("※ FFmpeg が見つからないため、動画は処理されません")
        lines.append("")
        if free_o is not None:
            lines.append(f"Original の空き: {free_o:,.1f} GB"
                         f"（必要目安 {total_mb / 1024:,.1f} GB）")
        if free_m is not None:
            lines.append(f"Mobile の空き: {free_m:,.1f} GB"
                         f"（必要目安 {total_mb / 1024 / 2:,.1f} GB）")
        lines += [
            "",
            "※ pHash での重複判定と圧縮後の容量は、実行するまで確定しません。",
        ]
        self._append_log("事前確認: " + " / ".join(
            x for x in lines[:2] if x))
        messagebox.showinfo("事前確認（書き込みは行いません）", "\n".join(lines))

    # --- 診断 ---
    def _diagnose(self):
        self._save_ui_to_cfg()
        try:
            info = diagnose_environment(self.cfg)
        except Exception as e:
            messagebox.showerror("診断に失敗しました", str(e))
            return
        text = format_diagnostics(info)
        self._append_log("診断: " + text.replace("\n", " / "))
        messagebox.showinfo("環境診断（書き込みテスト）", text)

    # --- フォルダ監視 ---
    def _toggle_watch(self):
        if self.watch_var.get():
            self._watch_signature = None
            self._last_watch_check = 0.0
            self.watch_spin.config(state="normal")
            self._append_log("取り込みフォルダの監視を開始しました。")
        else:
            self.watch_spin.config(state="disabled")
            self._append_log("取り込みフォルダの監視を停止しました。")

    def _watch_tick(self):
        try:
            if self.watch_var.get() and not self.running:
                interval = self._int_or(self.watch_interval_var.get(), 60, 10, 3600)
                now = time.time()
                if now - self._last_watch_check >= interval:
                    self._last_watch_check = now
                    cfg = dict(self.cfg)
                    cfg["intake_dir"] = normalize_dir(
                        self.intake_var.get(), DEFAULT_CONFIG["intake_dir"])
                    if os.path.isdir(cfg["intake_dir"]):
                        files = collect_intake_files(cfg)
                        signature = (len(files), max((f for f in files), default=""))
                        if files and signature != self._watch_signature:
                            self._watch_signature = signature
                            self._append_log(
                                f"監視: {len(files)} 件を検出したため処理を開始します。")
                            self._start()
        except Exception as e:
            self._append_log(f"監視中にエラー: {e}")
        self.after(5000, self._watch_tick)

    # --- プロファイル ---
    def _refresh_profiles(self):
        names = sorted(self.profiles)
        self.profile_combo.config(values=names)
        if self.profile_combo.get() not in names:
            self.profile_combo.set("")

    def _apply_profile(self, _event=None):
        name = self.profile_combo.get()
        data = self.profiles.get(name)
        if not data:
            return
        self._save_ui_to_cfg()          # 今の画面を保存してから切り替える
        cfg = dict(DEFAULT_CONFIG)
        cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
        for key in ("intake_dir", "original_dir", "mobile_dir", "work_dir"):
            cfg[key] = normalize_dir(cfg.get(key), DEFAULT_CONFIG[key])
        self.cfg = cfg
        self._load_cfg_to_ui()
        self._append_log(f"プロファイル「{name}」を読み込みました。")

    def _save_profile(self):
        default = self.profile_combo.get() or "新しいプロファイル"
        name = simpledialog.askstring("プロファイルの保存", "名前を入力してください:",
                                      initialvalue=default, parent=self)
        if not name:
            return
        self._save_ui_to_cfg()
        self.profiles[name.strip()] = {
            k: self.cfg[k] for k in DEFAULT_CONFIG if k in self.cfg}
        if save_profiles(self.profiles):
            self._refresh_profiles()
            self.profile_combo.set(name.strip())
            self._append_log(f"プロファイル「{name.strip()}」を保存しました。")
        else:
            messagebox.showerror("エラー", "プロファイルを保存できませんでした。")

    def _delete_profile(self):
        name = self.profile_combo.get()
        if not name or name not in self.profiles:
            return
        if not messagebox.askyesno("確認", f"プロファイル「{name}」を削除しますか？"):
            return
        del self.profiles[name]
        save_profiles(self.profiles)
        self._refresh_profiles()
        self._append_log(f"プロファイル「{name}」を削除しました。")

    def _on_dedup_mode(self, _event=None):
        self._toggle_dedup()

    def _toggle_dedup(self):
        """重複スキップがオフのときは、方式・しきい値・扱いを操作できないようにする。"""
        enabled = self.dedup_var.get()
        mode = self._dedup_mode_key()
        try:
            self.dedup_mode_combo.config(state="readonly" if enabled else "disabled")
            self.dup_action_combo.config(state="readonly" if enabled else "disabled")
            # ファイル名方式では pHash のしきい値を使わない
            spin_state = "normal" if (enabled and mode in ("phash", "both")) else "disabled"
            self.threshold_spin.config(state=spin_state)
        except Exception:
            pass

    def _dedup_mode_key(self):
        label = self.dedup_mode_combo.get()
        for key, text in DEDUP_MODES.items():
            if text == label:
                return key
        return "phash"

    def _dup_action_key(self):
        label = self.dup_action_combo.get()
        for key, text in DUPLICATE_ACTIONS.items():
            if text == label:
                return key
        return "keep"

    def _datetime_mode_key(self):
        label = self.datetime_combo.get()
        for key, text in DATETIME_MODES.items():
            if text == label:
                return key
        return "exif_local"

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
        mode = self.cfg.get("datetime_mode", "exif_local")
        self.datetime_combo.set(DATETIME_MODES.get(mode, DATETIME_MODES["exif_local"]))
        self.delete_var.set(bool(self.cfg["delete_after_process"]))
        self.subdir_var.set(bool(self.cfg.get("include_subfolders", True)))
        self.video_var.set(bool(self.cfg["compress_video"]))
        self.dedup_var.set(bool(self.cfg.get("enable_dedup", False)))
        mode = self.cfg.get("dedup_mode", "phash")
        self.dedup_mode_combo.set(DEDUP_MODES.get(mode, DEDUP_MODES["phash"]))
        action = self.cfg.get("duplicate_action", "keep")
        self.dup_action_combo.set(
            DUPLICATE_ACTIONS.get(action, DUPLICATE_ACTIONS["keep"]))
        self.threshold_var.set(str(self.cfg["phash_threshold"]))
        self._toggle_dedup()
        self.long_side_var.set(str(self.cfg["image_long_side"]))
        self.quality_var.set(str(self.cfg["jpeg_quality"]))
        self.crf_var.set(str(self.cfg["video_crf"]))
        self.video_codec_var.set(self.cfg.get("video_codec", "hevc"))
        self.codec_combo.set(VIDEO_CODECS.get(
            self.cfg.get("video_codec", "hevc"), VIDEO_CODECS["hevc"]))
        self.heic_var.set(self.cfg.get("mobile_format", "jpg") == "heic")
        self.workers_var.set(str(self.cfg.get("workers", 1)))
        self.ffmpeg_var.set(self.cfg.get("ffmpeg_path", "ffmpeg"))

        current = (self.cfg["image_long_side"], self.cfg["jpeg_quality"],
                   self.cfg["video_crf"])
        preset = next((n for n, v in COMPRESSION_PRESETS.items() if v == current), "カスタム")
        self.preset_combo.set(preset)
        self._refresh_profiles()
        self._toggle_dedup()

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
        self.cfg["datetime_mode"] = self._datetime_mode_key()
        self.cfg["delete_after_process"] = bool(self.delete_var.get())
        self.cfg["include_subfolders"] = bool(self.subdir_var.get())
        self.cfg["compress_video"] = bool(self.video_var.get())
        self.cfg["enable_dedup"] = bool(self.dedup_var.get())
        self.cfg["dedup_mode"] = self._dedup_mode_key()
        self.cfg["duplicate_action"] = self._dup_action_key()
        # 旧設定との互換のため合わせて更新する
        self.cfg["delete_duplicates"] = self.cfg["duplicate_action"] == "delete"
        self.cfg["phash_threshold"] = self._int_or(
            self.threshold_var.get(), DEFAULT_CONFIG["phash_threshold"], 0, 20)
        self.cfg["image_long_side"] = self._int_or(
            self.long_side_var.get(), DEFAULT_CONFIG["image_long_side"], 320, 6000)
        self.cfg["jpeg_quality"] = self._int_or(
            self.quality_var.get(), DEFAULT_CONFIG["jpeg_quality"], 30, 100)
        self.cfg["video_crf"] = self._int_or(
            self.crf_var.get(), DEFAULT_CONFIG["video_crf"], 0, 51)
        label = self.codec_combo.get()
        self.cfg["video_codec"] = next(
            (k for k, v in VIDEO_CODECS.items() if v == label), "hevc")
        self.cfg["mobile_format"] = "heic" if self.heic_var.get() else "jpg"
        self.cfg["workers"] = self._int_or(
            self.workers_var.get(), DEFAULT_CONFIG["workers"], 1, 16)
        self.cfg["ffmpeg_path"] = normalize_dir(
            self.ffmpeg_var.get(), DEFAULT_CONFIG["ffmpeg_path"])

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
                elif kind == "video_progress":
                    self.result_var.set(
                        f"動画を圧縮中... {msg[1]:.0f}%（{msg[2]}）")
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
