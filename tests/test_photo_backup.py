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
               backup_root=os.path.join(tmp, "backup"))
    found = pb.collect_intake_files(cfg)
    assert len(found) == 2, f"サブフォルダを含む列挙が不正: {found}"

    cfg["include_subfolders"] = False
    assert len(pb.collect_intake_files(cfg)) == 1, "サブフォルダ除外が効いていない"
    print("  collect_intake_files: OK")


def test_end_to_end(tmp):
    intake = os.path.join(tmp, "e2e_intake")
    backup = os.path.join(tmp, "e2e_backup")
    make_image(os.path.join(intake, "DCIM", "Camera", "a.jpg"), 3)
    # 同じ画像の複製(重複としてスキップされる想定)
    shutil.copy2(os.path.join(intake, "DCIM", "Camera", "a.jpg"),
                 os.path.join(intake, "DCIM", "Camera", "a_copy.jpg"))
    # 別の画像(処理される想定)
    make_image(os.path.join(intake, "DCIM", "Camera", "b.jpg"), 4)

    cfg = dict(pb.DEFAULT_CONFIG, intake_dir=intake, backup_root=backup,
               include_subfolders=True, delete_after_process=True)
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
    for root, _, names in os.walk(os.path.join(backup, "Original")):
        original_files += [os.path.join(root, n) for n in names]
    for root, _, names in os.walk(os.path.join(backup, "Mobile")):
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

    # 既定では取り込み元が空になり、空フォルダも片付く
    remaining = [n for _, _, names in os.walk(intake) for n in names]
    assert not remaining, f"取り込み元にファイルが残っている: {remaining}"
    assert not os.path.exists(os.path.join(intake, "DCIM")), "空フォルダが残っている"

    # 2 回目の実行では対象なし(重複DBが保存されている)
    with open(os.path.join(backup, "hashes.json"), encoding="utf-8") as f:
        assert len(json.load(f)["hashes"]) == 2, "ハッシュDBの件数が不正"
    print("  end-to-end: OK")


def main():
    if pb.missing_required_packages():
        print("依存不足:", pb.missing_required_packages())
        return 1
    tmp = tempfile.mkdtemp(prefix="photobackup_test_")
    try:
        test_hash_store(tmp)
        test_datetime_from_filename()
        test_collect(tmp)
        test_end_to_end(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("すべて成功")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

