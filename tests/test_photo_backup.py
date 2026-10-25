# -*- coding: utf-8 -*-
"""photo_backup.py のスモークテスト。

GUI を開かずに、重複判定・日時推定・取り込み列挙・画像処理の一連を検証する。
    python tests/test_photo_backup.py
"""

import json
import os
import queue
import random
import shutil
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import photo_backup as pb
from PIL import Image, ImageDraw


def make_image(path, seed, size=(1600, 1200), exif_dt="2021:05:04 12:34:56"):
    """構造の異なるテスト用 JPEG を作る(seed ごとに画模様が変わる)。

    pHash は色ではなく構造を見るため、単色違いでは重複判定の検証にならない。
    乱数で図形を散らして、seed が違えばハッシュも大きく変わるようにする。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rnd = random.Random(seed)
    img = Image.new("RGB", size, (rnd.randrange(256), rnd.randrange(256),
                                  rnd.randrange(256)))
    draw = ImageDraw.Draw(img)
    for _ in range(24):
        x1, y1 = rnd.randrange(size[0]), rnd.randrange(size[1])
        x2, y2 = x1 + rnd.randrange(80, 600), y1 + rnd.randrange(80, 600)
        color = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
        if rnd.random() < 0.5:
            draw.ellipse([x1, y1, x2, y2], fill=color)
        else:
            draw.rectangle([x1, y1, x2, y2], fill=color)
    exif = Image.Exif()
    exif[0x9003] = exif_dt
    img.save(path, "JPEG", quality=95, exif=exif.tobytes())


def test_hash_store(tmp):
    path = os.path.join(tmp, "hashes.json")
    store = pb.HashStore(path)
    assert not store.is_duplicate("0000000000000000", 0), "空DBで重複判定された"

    store.add("0000000000000000")
    assert store.is_duplicate("0000000000000000", 0), "同一ハッシュを重複と判定できない"
    assert store.is_duplicate("0000000000000001", 1), "距離1がしきい値1で重複にならない"
    assert not store.is_duplicate("00000000000000ff", 7), "距離8がしきい値7で重複になった"
    assert not store.is_duplicate("ffffffffffffffff", 5), "全ビット反転を重複と誤判定した"

    store.save()
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["hashes"] == ["0000000000000000"], "保存形式が不正"
    assert pb.HashStore(path).is_duplicate("0000000000000000", 0), "再読込後に照合できない"
    print("  HashStore: OK")


def test_datetime_from_filename():
    cases = {
        "IMG_20240115_123456.jpg": datetime(2024, 1, 15, 12, 34, 56),
        "VID_20240115_123456.mp4": datetime(2024, 1, 15, 12, 34, 56),
        "PXL_2024-01-15-12-34-56-123.mp4": datetime(2024, 1, 15, 12, 34, 56),
        "screenshot.png": None,
        "20241399_999999.jpg": None,
    }
    for name, expected in cases.items():
        assert pb.datetime_from_filename(name) == expected, name
    print("  datetime_from_filename: OK")


def test_collect(tmp):
    intake = os.path.join(tmp, "intake")
    os.makedirs(os.path.join(intake, "DCIM", "Camera"), exist_ok=True)
    make_image(os.path.join(intake, "top.jpg"), 1)
    make_image(os.path.join(intake, "DCIM", "Camera", "sub.jpg"), 2)
    with open(os.path.join(intake, "DCIM", "Camera", "note.txt"), "w") as f:
        f.write("x")

    cfg = dict(pb.DEFAULT_CONFIG, intake_dir=intake,
               original_dir=os.path.join(tmp, "orig"),
               mobile_dir=os.path.join(tmp, "mob"),
               work_dir=os.path.join(tmp, "work"))
    found = pb.collect_intake_files(cfg)
    assert len(found) == 2, f"サブフォルダを含む列挙が不正: {found}"

    cfg["include_subfolders"] = False
    assert len(pb.collect_intake_files(cfg)) == 1, "サブフォルダ除外が効いていない"

    # 出力先が取り込みフォルダ内にあっても、自分の出力は再処理しない
    cfg["include_subfolders"] = True
    cfg["original_dir"] = os.path.join(intake, "Original")
    os.makedirs(cfg["original_dir"], exist_ok=True)
    make_image(os.path.join(cfg["original_dir"], "already.jpg"), 9)
    found = pb.collect_intake_files(cfg)
    assert all("Original" not in p for p in found), f"出力先を再処理している: {found}"
    print("  collect_intake_files: OK")


def test_config_migration(tmp):
    """旧形式(backup_root)の config.json を新形式へ読み替えられるか。"""
    old_script_dir = pb.SCRIPT_DIR
    pb.SCRIPT_DIR = tmp
    try:
        with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"intake_dir": "D:/in", "backup_root": "D:/bak",
                       "layout": "YYYY", "jpeg_quality": 75}, f)

        cfg = pb.load_config()
        assert cfg["original_dir"] == os.path.normpath("D:/bak/Original"), cfg["original_dir"]
        assert cfg["mobile_dir"] == os.path.normpath("D:/bak/Mobile"), cfg["mobile_dir"]
        assert cfg["work_dir"] == os.path.normpath("D:/bak"), cfg["work_dir"]
        assert cfg["jpeg_quality"] == 75, "既存の設定値が引き継がれていない"
        assert cfg["image_long_side"] == 1200, "既定値が補完されていない"

        # 新形式はそのまま読める(上書き保存後に再読込)
        pb.save_config(cfg)
        assert "backup_root" not in json.load(
            open(os.path.join(tmp, "config.json"), encoding="utf-8")), \
            "旧キーが保存され続けている"
        again = pb.load_config()
        assert again["original_dir"] == cfg["original_dir"], "保存した保存先が戻らない"
    finally:
        pb.SCRIPT_DIR = old_script_dir
    print("  load_config (旧形式互換): OK")


def test_end_to_end(tmp):
    intake = os.path.join(tmp, "e2e_intake")
    orig = os.path.join(tmp, "e2e_orig")
    mob = os.path.join(tmp, "e2e_mob")
    work = os.path.join(tmp, "e2e_work")
    make_image(os.path.join(intake, "DCIM", "Camera", "a.jpg"), 3)
    # 同じ画像の複製(重複としてスキップされる想定)
    shutil.copy2(os.path.join(intake, "DCIM", "Camera", "a.jpg"),
                 os.path.join(intake, "DCIM", "Camera", "a_copy.jpg"))
    # 別の画像(処理される想定)
    make_image(os.path.join(intake, "DCIM", "Camera", "b.jpg"), 4)

    cfg = dict(pb.DEFAULT_CONFIG, intake_dir=intake,
               original_dir=orig, mobile_dir=mob, work_dir=work,
               include_subfolders=True, delete_after_process=True,
               enable_dedup=True)
    q = queue.Queue()
    pb.run_processing(cfg, q)

    messages = []
    while not q.empty():
        messages.append(q.get())
    kinds = [m[0] for m in messages]
    assert "done" in kinds, "完了通知が無い"

    summary = [m for m in messages if m[0] == "done"][0][2]
    assert "画像 2 枚" in summary, f"処理枚数が不正: {summary}"
    assert "重複スキップ 1 件" in summary, f"重複判定が不正: {summary}"

    original_files = []
    mobile_files = []
    for root, _, names in os.walk(orig):
        original_files += [os.path.join(root, n) for n in names]
    for root, _, names in os.walk(mob):
        mobile_files += [os.path.join(root, n) for n in names]

    assert len(original_files) == 2, f"原本の数が不正: {original_files}"
    assert len(mobile_files) == 2, f"軽量版の数が不正: {mobile_files}"
    assert all(p.lower().endswith(".jpg") for p in mobile_files), "Mobile が JPEG でない"

    # EXIF の撮影日時で YYYY\MM に整理されているか
    assert any(os.sep + "2021" + os.sep + "05" + os.sep in p for p in original_files), \
        f"撮影日時のフォルダ整理が不正: {original_files}"

    # 軽量版は長辺 1200px 以下
    with Image.open(mobile_files[0]) as im:
        assert max(im.size) <= 1200, f"リサイズされていない: {im.size}"

    # ログと重複DBは work_dir に置かれる
    assert os.path.exists(os.path.join(work, "hashes.json")), "重複DBが work_dir に無い"
    assert os.path.exists(os.path.join(work, "backup_log.txt")), "ログが work_dir に無い"
    assert not os.path.exists(os.path.join(work, "Original")), "work_dir に Original が出来ている"

    # 既定では取り込み元が空にならず、削除したものだけが消える
    remaining = [n for _, _, names in os.walk(intake) for n in names]
    assert remaining == ["a_copy.jpg"], f"取り込み元の残り方が不正: {remaining}"
    assert os.path.exists(os.path.join(intake, "DCIM")), "空でないフォルダが消えている"

    with open(os.path.join(work, "hashes.json"), encoding="utf-8") as f:
        assert len(json.load(f)["hashes"]) == 2, "ハッシュDBの件数が不正"
    print("  end-to-end: OK")


def test_delete_defaults(tmp):
    """削除は既定でオフ。重複スキップ分は delete_duplicates を有効にしない限り残る。"""
    assert pb.DEFAULT_CONFIG["delete_after_process"] is False, "削除の既定がオンになっている"
    assert pb.DEFAULT_CONFIG["delete_duplicates"] is False, "重複削除の既定がオンになっている"

    intake = os.path.join(tmp, "del_intake")
    make_image(os.path.join(intake, "x.jpg"), 11)
    shutil.copy2(os.path.join(intake, "x.jpg"), os.path.join(intake, "x_copy.jpg"))

    # delete_after_process=True でも重複は残す(既定)
    cfg = dict(pb.DEFAULT_CONFIG, intake_dir=intake,
               original_dir=os.path.join(tmp, "del_orig"),
               mobile_dir=os.path.join(tmp, "del_mob"),
               work_dir=os.path.join(tmp, "del_work"),
               delete_after_process=True, enable_dedup=True)
    pb.run_processing(cfg, queue.Queue())
    left = sorted(os.listdir(intake))
    assert left == ["x_copy.jpg"], f"重複が削除された: {left}"

    # delete_duplicates=True なら重複も削除する
    cfg["delete_duplicates"] = True
    pb.run_processing(cfg, queue.Queue())
    assert os.listdir(intake) == [], "delete_duplicates=True で重複が残った"
    print("  delete defaults: OK")


def test_safety(tmp):
    """A1 コピー検証 / A2 削除 / A3 重複の退避 / A4 空き容量。"""
    src = os.path.join(tmp, "safety_src")
    os.makedirs(src, exist_ok=True)
    make_image(os.path.join(src, "s.jpg"), 51)

    # A1: .part を残さずコピーできる
    dest = os.path.join(tmp, "safety_dest.jpg")
    pb.copy_verified(os.path.join(src, "s.jpg"), dest)
    assert os.path.exists(dest), "コピーされていない"
    assert not os.path.exists(dest + ".part"), ".part が残っている"
    assert os.path.getsize(dest) == os.path.getsize(os.path.join(src, "s.jpg"))

    # A2: 削除(ゴミ箱が無い環境では通常削除にフォールバック)
    assert pb.delete_file(dest, use_recycle_bin=True) in ("trash", "deleted")
    assert not os.path.exists(dest), "削除されていない"

    # A4: 空き容量が取得できる
    assert (pb.free_space_gb(tmp) or 0) > 0, "空き容量を取得できない"

    # A3: 重複を Duplicates へ退避する
    intake = os.path.join(tmp, "safety_intake")
    make_image(os.path.join(intake, "dup.jpg"), 52)
    shutil.copy2(os.path.join(intake, "dup.jpg"),
                 os.path.join(intake, "dup_copy.jpg"))
    work = os.path.join(tmp, "safety_work")
    cfg = dict(pb.DEFAULT_CONFIG, intake_dir=intake,
               original_dir=os.path.join(tmp, "safety_orig"),
               mobile_dir=os.path.join(tmp, "safety_mob"),
               work_dir=work, enable_dedup=True, phash_threshold=0,
               duplicate_action="move")
    pb.run_processing(cfg, queue.Queue())
    left = sorted(os.listdir(intake))
    assert left == ["dup.jpg"], f"元ファイルが消えた: {left}"
    moved = os.listdir(os.path.join(work, "Duplicates"))
    assert moved == ["dup_copy.jpg"], f"Duplicates の中身が不正: {moved}"
    print("  safety: OK")


def test_cancel(tmp):
    """中止要求を出すと、残りのファイルを処理せずに止まる。"""
    import threading

    intake = os.path.join(tmp, "cancel_intake")
    for i in range(3):
        make_image(os.path.join(intake, f"c{i}.jpg"), 30 + i)
    before = sorted(os.listdir(intake))

    cfg = dict(pb.DEFAULT_CONFIG, intake_dir=intake,
               original_dir=os.path.join(tmp, "cancel_orig"),
               mobile_dir=os.path.join(tmp, "cancel_mob"),
               work_dir=os.path.join(tmp, "cancel_work"))

    # 開始前に中止要求を立てておく
    event = threading.Event()
    event.set()
    q = queue.Queue()
    pb.run_processing(cfg, q, event)

    msgs = []
    while not q.empty():
        msgs.append(q.get())
    done = [m for m in msgs if m[0] == "done"][0]
    summary = done[2]
    assert "中止しました" in summary, f"中止の表示が無い: {summary}"
    assert done[3] is True, "中止フラグが立っていない"
    assert "画像 0 枚" in summary, f"中止なのに処理された: {summary}"

    # 取り込み元は一切変更されない
    assert sorted(os.listdir(intake)) == before, "中止したのにファイルが動いた"
    assert any("中止要求" in m[1] for m in msgs if m[0] == "log"), "中止ログが無い"

    # 中止していない通常実行では中止フラグが False
    q = queue.Queue()
    pb.run_processing(dict(cfg), q, threading.Event())
    msgs = []
    while not q.empty():
        msgs.append(q.get())
    assert [m for m in msgs if m[0] == "done"][0][3] is False, "通常実行で中止扱いになった"
    print("  cancel: OK")


def test_name_dedup(tmp):
    """ファイル名方式の重複判定。中身が違っても同名ならスキップする。"""
    intake = os.path.join(tmp, "name_intake")
    cfg = dict(pb.DEFAULT_CONFIG, intake_dir=intake,
               original_dir=os.path.join(tmp, "name_orig"),
               mobile_dir=os.path.join(tmp, "name_mob"),
               work_dir=os.path.join(tmp, "name_work"),
               enable_dedup=True, dedup_mode="name")

    make_image(os.path.join(intake, "IMG_0001.jpg"), 41)
    q = queue.Queue()
    pb.run_processing(dict(cfg), q)
    msgs = []
    while not q.empty():
        msgs.append(q.get())
    assert "画像 1 枚" in [m for m in msgs if m[0] == "done"][0][2], "1回目が処理されていない"

    # 中身を差し替えても、同名なら「重複」としてスキップされる
    os.remove(os.path.join(intake, "IMG_0001.jpg"))
    make_image(os.path.join(intake, "IMG_0001.jpg"), 42)
    q = queue.Queue()
    pb.run_processing(dict(cfg), q)
    msgs = []
    while not q.empty():
        msgs.append(q.get())
    summary = [m for m in msgs if m[0] == "done"][0][2]
    assert "画像 0 枚" in summary, f"同名なのに処理された: {summary}"
    assert "うち同名 1 件" in summary, f"同名判定の記録が無い: {summary}"
    assert any("同名ファイルあり" in m[1] for m in msgs if m[0] == "log"), "理由の記録が無い"

    # 名前が違えば、中身が同じでも「ファイル名方式」ではスキップしない
    os.remove(os.path.join(intake, "IMG_0001.jpg"))
    make_image(os.path.join(intake, "IMG_0002.jpg"), 42)
    q = queue.Queue()
    pb.run_processing(dict(cfg), q)
    msgs = []
    while not q.empty():
        msgs.append(q.get())
    assert "画像 1 枚" in [m for m in msgs if m[0] == "done"][0][2], "別名なのにスキップされた"
    print("  name dedup: OK")


def test_ops(tmp):
    """B1 事前確認 / B2 台帳 / B4 ログローテーション / B5 プロファイル。"""
    intake = os.path.join(tmp, "ops_intake")
    make_image(os.path.join(intake, "o.jpg"), 61)
    work = os.path.join(tmp, "ops_work")
    cfg = dict(pb.DEFAULT_CONFIG, intake_dir=intake,
               original_dir=os.path.join(tmp, "ops_orig"),
               mobile_dir=os.path.join(tmp, "ops_mob"),
               work_dir=work)

    # B1: 事前確認はフォルダを作らず、件数を返す
    info = pb.analyze_intake(cfg)
    assert info["total"] == 1 and info["images"] == 1, info
    assert not os.path.exists(cfg["original_dir"]), "事前確認でフォルダが作られた"

    # 処理して台帳に載る
    pb.run_processing(dict(cfg), queue.Queue())
    assert os.path.exists(os.path.join(work, "processed.json")), "台帳が作られていない"

    # B2: 台帳方式は「同じファイル」を重複として弾く
    cfg_ledger = dict(cfg, enable_dedup=True, dedup_mode="ledger")
    q = queue.Queue()
    pb.run_processing(dict(cfg_ledger), q)
    msgs = []
    while not q.empty():
        msgs.append(q.get())
    assert "重複スキップ 1 件" in [m for m in msgs if m[0] == "done"][0][2], "台帳で弾けていない"

    # 中身もサイズも変えれば、同名でも処理される（名前方式との違い）
    os.remove(os.path.join(intake, "o.jpg"))
    make_image(os.path.join(intake, "o.jpg"), 62, size=(1200, 900))
    q = queue.Queue()
    pb.run_processing(dict(cfg_ledger), q)
    msgs = []
    while not q.empty():
        msgs.append(q.get())
    assert "画像 1 枚" in [m for m in msgs if m[0] == "done"][0][2], "同名の別ファイルまで弾いた"

    # B4: 大きくなったログは退避される
    log = os.path.join(work, "backup_log.txt")
    with open(log, "w", encoding="utf-8") as f:
        f.write("x" * (6 * 1024 * 1024))
    assert pb.rotate_log(cfg), "ログがローテーションされない"
    assert not os.path.exists(log), "元のログが残っている"
    assert [n for n in os.listdir(work) if n.startswith("backup_log.")], "退避ファイルが無い"

    # B5: プロファイルの保存と読み込み
    old = pb.SCRIPT_DIR
    pb.SCRIPT_DIR = os.path.join(tmp, "ops_script")
    os.makedirs(pb.SCRIPT_DIR, exist_ok=True)
    try:
        assert pb.load_profiles() == {}, "初期状態でプロファイルがある"
        assert pb.save_profiles({"A": {"jpeg_quality": 70}}), "保存に失敗"
        assert pb.load_profiles()["A"]["jpeg_quality"] == 70, "読み込みに失敗"
    finally:
        pb.SCRIPT_DIR = old
    print("  ops: OK")


def test_dedup_switch(tmp):
    """重複スキップは既定オフ。オンのときだけ重複をスキップする。"""
    assert pb.DEFAULT_CONFIG["enable_dedup"] is False, "重複スキップの既定がオンになっている"

    intake = os.path.join(tmp, "dedup_intake")
    make_image(os.path.join(intake, "d.jpg"), 21)
    shutil.copy2(os.path.join(intake, "d.jpg"), os.path.join(intake, "d_copy.jpg"))

    base = dict(pb.DEFAULT_CONFIG, intake_dir=intake,
                original_dir=os.path.join(tmp, "dedup_orig"),
                mobile_dir=os.path.join(tmp, "dedup_mob"),
                work_dir=os.path.join(tmp, "dedup_work"))

    # オフ(既定): 同じ画像でも 2 枚とも処理される
    q = queue.Queue()
    pb.run_processing(dict(base), q)
    msgs = []
    while not q.empty():
        msgs.append(q.get())
    summary = [m for m in msgs if m[0] == "done"][0][2]
    assert "画像 2 枚" in summary, f"重複スキップがオフなのにスキップされた: {summary}"
    assert "重複スキップ 0 件" in summary, summary
    assert any("重複スキップ: 無効" in m[1] for m in msgs if m[0] == "log"), "無効の記録が無い"

    # オン: 2 枚目がスキップされる（履歴の影響を避けるため DB は新しい場所を使う）
    cfg = dict(base, work_dir=os.path.join(tmp, "dedup_work2"), enable_dedup=True)
    q = queue.Queue()
    pb.run_processing(cfg, q)
    msgs = []
    while not q.empty():
        msgs.append(q.get())
    summary = [m for m in msgs if m[0] == "done"][0][2]
    assert "重複スキップ 1 件" in summary, f"重複スキップが効いていない: {summary}"
    assert any("重複スキップ: 有効" in m[1] for m in msgs if m[0] == "log"), "有効の記録が無い"
    print("  dedup switch: OK")


def test_compression_settings(tmp):
    """長辺・JPEG 品質の指定が出力に反映されるか。"""
    intake = os.path.join(tmp, "comp_intake")
    orig = os.path.join(tmp, "comp_orig")
    mob = os.path.join(tmp, "comp_mob")
    make_image(os.path.join(intake, "c.jpg"), 7, size=(3000, 2000))

    cfg = dict(pb.DEFAULT_CONFIG, intake_dir=intake, original_dir=orig,
               mobile_dir=mob, work_dir=os.path.join(tmp, "comp_work"),
               image_long_side=800, jpeg_quality=50, delete_after_process=True)
    pb.run_processing(cfg, queue.Queue())

    out = [os.path.join(r, n) for r, _, ns in os.walk(mob) for n in ns]
    assert len(out) == 1, out
    with Image.open(out[0]) as im:
        assert max(im.size) == 800, f"長辺指定が反映されていない: {im.size}"
    print("  compression settings: OK")


def main():
    if pb.missing_required_packages():
        print("依存不足:", pb.missing_required_packages())
        return 1
    tmp = tempfile.mkdtemp(prefix="photobackup_test_")
    try:
        test_hash_store(tmp)
        test_datetime_from_filename()
        test_collect(tmp)
        test_config_migration(tmp)
        test_end_to_end(tmp)
        test_compression_settings(tmp)
        test_delete_defaults(tmp)
        test_safety(tmp)
        test_ops(tmp)
        test_cancel(tmp)
        test_dedup_switch(tmp)
        test_name_dedup(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("すべて成功")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

