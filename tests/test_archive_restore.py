"""服务数据归档恢复（POST /service-data-archives/restore）测试。

覆盖：
- 成功恢复：富数据归档（多文档/多修订/单份+批次+定时发布/撤回）整体恢复，
  各类恢复计数正确；ID、版本、摘要与时间保留；既有查询与公开接口按恢复
  记录及原定时公开、撤回规则工作；再归档的记录与原归档逐字节一致；
- 空归档恢复、JPEG 扩展名、恢复后续发版本号连续、旧批次幂等键不推断恢复；
- 明确拒绝：非 ZIP、空文件、超限、无效归档（含核验未覆盖字段的残缺记录）、
  非空实例（库非空/目录有业务文件）、目标文件冲突、写入失败——均不留下
  可查询的部分记录或本次文件；
- 串行：两个并发恢复至多一个成功；恢复与并发上传互斥，空实例判定不被绕过；
- 临时文件清理。
"""
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.config import Settings
from app.main import create_app
from tests.test_api import jpeg_bytes, png_bytes, upload  # noqa: E402,F401
from tests.test_archive import (  # noqa: E402,F401
    _annotate,
    _archive,
    _new_doc,
    _publish,
    _withdraw,
)
from tests.test_archive_verify import _manifest, _records, rebuild  # noqa: E402,F401


@pytest.fixture()
def pair(tmp_path):
    """源实例（造归档）与目标实例（空，恢复对象）。"""
    src_app = create_app(Settings(data_dir=str(tmp_path / "src")))
    dst_app = create_app(Settings(data_dir=str(tmp_path / "dst")))
    with TestClient(src_app) as src, TestClient(dst_app) as dst:
        yield src, dst


def _restore(client, content: bytes, filename="archive.zip"):
    return client.post(
        "/service-data-archives/restore",
        files={"file": (filename, bytes(content), "application/zip")},
    )


def _archive_bytes(client) -> bytes:
    # 显式深拷贝响应字节（TestClient 的响应缓冲会被后续请求复用）
    return bytes(bytearray(_archive(client).content))


def _future_publish_at(days=7) -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _rich_source(src) -> dict:
    """两份文档：A 三修订（v1 撤回、v2 立即公开、v3 批次定时），B 批次定时。"""
    a = _new_doc(src, (1, 2, 3), filename="证件A.png")
    b = _new_doc(src, (4, 5, 6), filename="证件B.png")
    _annotate(src, a["doc_id"], 0, [
        {"x": 0, "y": 0, "w": 2, "h": 2, "reason": "甲"}])
    _publish(src, a["doc_id"])                       # A v1（立即公开）
    assert _withdraw(src, a["doc_id"], 1).status_code == 201
    _annotate(src, a["doc_id"], 1, [
        {"x": 1, "y": 1, "w": 2, "h": 2, "reason": "乙"}])
    _publish(src, a["doc_id"])                       # A v2（立即公开）
    _annotate(src, b["doc_id"], 0, [
        {"x": 0, "y": 0, "w": 1, "h": 1, "reason": "丙"}])
    publish_at = _future_publish_at()
    batch = src.post("/publication-batches", json={
        "documents": [
            {"doc_id": a["doc_id"], "expected_revision": 2},
            {"doc_id": b["doc_id"], "expected_revision": 1},
        ],
        "idempotency_key": "handover-key-001",
        "publish_at": publish_at,
    })
    assert batch.status_code == 201, batch.text
    return {"a": a, "b": b, "batch": batch.json(), "publish_at": publish_at}


def _restore_ok(client, content: bytes) -> dict:
    r = _restore(client, content)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["restored"] is True and body["restored_at"]
    return body


# ---------------- 成功恢复 ----------------

def test_restore_rich_archive_counts_and_public_rules(pair):
    src, dst = pair
    ent = _rich_source(src)
    a, b = ent["a"], ent["b"]
    data = _archive_bytes(src)

    body = _restore_ok(dst, data)
    assert body["counts"] == {
        "documents": 2,
        "annotation_revisions": 5,   # A: 0,1,2；B: 0,1
        "publications": 4,           # A v1/v2 + 批次 A v3、B v1
        "publication_batches": 1,
        "withdrawals": 1,
        "original_files": 2,
        "publication_files": 4,
    }

    # 既有查询按恢复记录工作：文档详情与源实例逐项一致（ID/版本/摘要/时间）
    for doc_id in (a["doc_id"], b["doc_id"]):
        assert dst.get(f"/documents/{doc_id}").json() == \
            src.get(f"/documents/{doc_id}").json()
    ann = dst.get(f"/documents/{a['doc_id']}/annotations").json()
    assert ann["revision"] == 2
    assert [r["reason"] for r in ann["rects"]] == ["乙"]
    assert dst.get(f"/publication-batches/{ent['batch']['batch_id']}").json() == \
        src.get(f"/publication-batches/{ent['batch']['batch_id']}").json()
    assert dst.get(f"/documents/{a['doc_id']}/publications/1/withdrawal").json() \
        == src.get(f"/documents/{a['doc_id']}/publications/1/withdrawal").json()

    # 公开接口按原定时公开、撤回规则工作
    v2 = dst.get(f"/publications/{a['doc_id']}/2")
    assert v2.status_code == 200
    assert v2.json()["output_sha256"] == \
        src.get(f"/publications/{a['doc_id']}/2").json()["output_sha256"]
    assert dst.get(f"/publications/{a['doc_id']}/2/image").content == \
        src.get(f"/publications/{a['doc_id']}/2/image").content
    # 已撤回版本：410（不随恢复改变）
    r = dst.get(f"/publications/{a['doc_id']}/1")
    assert r.status_code == 410
    assert r.json()["detail"]["code"] == "publication_withdrawn"
    # 定时公开版本：到点前 404（批次内 A v3 与 B v1）
    for doc_id, version in ((a["doc_id"], 3), (b["doc_id"], 1)):
        r = dst.get(f"/publications/{doc_id}/{version}")
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "publication_not_yet_public"

    # 文件落盘：原图/成品按本机数据目录重建路径，摘要与记录一致
    settings = dst.app.state.settings
    for doc in (a, b):
        orig = os.path.join(settings.originals_dir, doc["doc_id"] + ".png")
        with open(orig, "rb") as f:
            assert hashlib.sha256(f.read()).hexdigest() == doc["original_sha256"]
    pubs = dst.get(f"/documents/{a['doc_id']}/publications").json()["publications"]
    for p in pubs:
        out = os.path.join(
            settings.publications_dir, a["doc_id"], f"v{p['version']}.png")
        with open(out, "rb") as f:
            assert hashlib.sha256(f.read()).hexdigest() == p["output_sha256"]


def test_restore_then_rearchive_yields_identical_records(pair):
    src, dst = pair
    _rich_source(src)
    data = _archive_bytes(src)
    _restore_ok(dst, data)

    # 恢复后的实例再次归档：通过现有核验，且五类记录 JSON 与引用文件字节
    # 与原归档逐一相同（本机绝对路径不进入归档）
    again = _archive_bytes(dst)
    v = dst.post("/service-data-archives/verify",
                 files={"file": ("again.zip", again, "application/zip")})
    assert v.json()["valid"] is True, v.json()["issues"]
    z1 = zipfile.ZipFile(io.BytesIO(data))
    z2 = zipfile.ZipFile(io.BytesIO(again))
    record_files = (
        "documents.json", "annotations.json", "publications.json",
        "publication-batches.json", "withdrawals.json",
    )
    for name in record_files:
        assert z1.read(name) == z2.read(name), name
    files1 = sorted(
        n for n in z1.namelist() if n.startswith(("originals/", "publications/")))
    files2 = sorted(
        n for n in z2.namelist() if n.startswith(("originals/", "publications/")))
    assert files1 == files2
    for name in files1:
        assert z1.read(name) == z2.read(name), name


def test_restore_empty_archive(pair):
    src, dst = pair
    body = _restore_ok(dst, _archive_bytes(src))
    assert body["counts"] == {
        "documents": 0,
        "annotation_revisions": 0,
        "publications": 0,
        "publication_batches": 0,
        "withdrawals": 0,
        "original_files": 0,
        "publication_files": 0,
    }


def test_restore_jpeg_original_extension(pair):
    src, dst = pair
    c = _new_doc(src, data=jpeg_bytes(size=(10, 20), orientation=6),
                 filename="证件.jpg")
    _publish(src, c["doc_id"])
    _restore_ok(dst, _archive_bytes(src))

    settings = dst.app.state.settings
    orig = os.path.join(settings.originals_dir, c["doc_id"] + ".jpg")
    with open(orig, "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == c["original_sha256"]
    doc = dst.get(f"/documents/{c['doc_id']}").json()
    assert doc["media_type"] == "image/jpeg"
    assert (doc["width"], doc["height"]) == (20, 10)  # EXIF 旋正后尺寸


def test_restore_keeps_scheduled_publication_clock_rules(pair, monkeypatch):
    src, dst = pair
    clock = {"now": datetime.now(timezone.utc)}
    monkeypatch.setattr(main_module, "_now_dt", lambda: clock["now"])

    a = _new_doc(src)
    b = _new_doc(src, (8, 8, 8))
    target = clock["now"] + timedelta(hours=2)
    publish_at = target.isoformat(timespec="seconds").replace("+00:00", "Z")
    for doc in (a, b):
        r = src.post(f"/documents/{doc['doc_id']}/publish",
                     json={"publish_at": publish_at})
        assert r.status_code == 201, r.text
    assert _withdraw(src, b["doc_id"], 1).status_code == 201  # 定时版本被撤回

    _restore_ok(dst, _archive_bytes(src))

    # 到点前：定时版本 404；已撤回的定时版本 410（撤回优先于公开时间）
    r = dst.get(f"/publications/{a['doc_id']}/1")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "publication_not_yet_public"
    assert dst.get(f"/publications/{b['doc_id']}/1").status_code == 410

    # 到点后：未撤回版本无需任何操作自动可读；撤回版本仍 410
    clock["now"] = target + timedelta(seconds=1)
    assert dst.get(f"/publications/{a['doc_id']}/1").status_code == 200
    assert dst.get(f"/publications/{a['doc_id']}/1/image").status_code == 200
    assert dst.get(f"/publications/{b['doc_id']}/1").status_code == 410


def test_restore_then_publish_continues_version_sequence(pair):
    src, dst = pair
    a = _new_doc(src)
    _publish(src, a["doc_id"])  # v1
    _restore_ok(dst, _archive_bytes(src))

    r = dst.post(f"/documents/{a['doc_id']}/publish")
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 2  # 版本号序列随恢复保留，续发不冲突


def test_restore_does_not_restore_batch_idempotency_keys(pair):
    src, dst = pair
    a = _new_doc(src, (1, 2, 3))
    b = _new_doc(src, (4, 5, 6))
    _annotate(src, a["doc_id"], 0, [
        {"x": 0, "y": 0, "w": 1, "h": 1, "reason": "甲"}])
    _annotate(src, b["doc_id"], 0, [
        {"x": 0, "y": 0, "w": 1, "h": 1, "reason": "乙"}])
    batch = src.post("/publication-batches", json={
        "documents": [
            {"doc_id": a["doc_id"], "expected_revision": 1},
            {"doc_id": b["doc_id"], "expected_revision": 1},
        ],
        "idempotency_key": "handover-key-001",
    })
    assert batch.status_code == 201, batch.text
    first_batch_id = batch.json()["batch_id"]

    _restore_ok(dst, _archive_bytes(src))

    # 归档不含幂等键：恢复后 batch_idempotency 为空，不推断恢复
    settings = dst.app.state.settings
    db = sqlite3.connect(settings.db_path)
    try:
        n = db.execute("SELECT COUNT(*) FROM batch_idempotency").fetchone()[0]
    finally:
        db.close()
    assert n == 0

    # 重放旧键按首次请求处理（201 新批次、新版本），而不是命中重放（200）
    replay = dst.post("/publication-batches", json={
        "documents": [
            {"doc_id": a["doc_id"], "expected_revision": 1},
            {"doc_id": b["doc_id"], "expected_revision": 1},
        ],
        "idempotency_key": "handover-key-001",
    })
    assert replay.status_code == 201, replay.text
    assert replay.json()["batch_id"] != first_batch_id
    assert sorted(d["version"] for d in replay.json()["documents"]) == [2, 2]


# ---------------- 请求级拒绝 ----------------

def test_restore_rejects_non_zip(pair):
    _src, dst = pair
    r = _restore(dst, b"this is not a zip at all")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "invalid_archive"
    assert any(i["code"] == "not_a_zip" for i in detail["issues"])


def test_restore_rejects_empty_upload(pair):
    _src, dst = pair
    r = _restore(dst, b"")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "empty_upload"


def test_restore_rejects_oversized_upload(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"), max_restore_bytes=64)
    app = create_app(settings)
    with TestClient(app) as client:
        r = _restore(client, b"z" * 1024)
    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "payload_too_large"


def test_restore_rejects_invalid_archive_and_leaves_no_trace(pair):
    src, dst = pair
    _rich_source(src)
    data = _archive_bytes(src)
    bad = rebuild(data, raw_updates={"documents.json": b"{not json"})

    r = _restore(dst, bad)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "invalid_archive"
    assert any(i["code"] == "record_invalid_json" for i in detail["issues"])

    # 不留下任何痕迹：实例仍是空实例，随后用有效归档恢复成功
    assert _restore(dst, data).status_code == 201


def test_restore_rejects_tampered_member_bytes(pair):
    src, dst = pair
    ent = _rich_source(src)
    data = _archive_bytes(src)
    target = f"publications/{ent['a']['doc_id']}/v2.png"
    bad = rebuild(data, raw_updates={target: b"tampered-artifact"})

    r = _restore(dst, bad)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "invalid_archive"
    assert any(i["code"] == "manifest_sha256_mismatch"
               for i in detail["issues"])
    assert _restore(dst, data).status_code == 201  # 实例未被污染


def test_restore_rejects_record_missing_consumed_field(pair):
    # 现有核验不检查 documents.created_at：核验通过，但恢复拒绝——
    # 绝不静默写入残缺记录
    src, dst = pair
    _new_doc(src)
    data = _archive_bytes(src)
    recs = _records(data)
    del recs["documents.json"]["documents"][0]["created_at"]
    modified = json.dumps(
        recs["documents.json"], ensure_ascii=False, indent=2).encode("utf-8")
    manifest = _manifest(data)
    entry = next(e for e in manifest["files"] if e["path"] == "documents.json")
    entry["sha256"] = hashlib.sha256(modified).hexdigest()
    bad = rebuild(data, {**recs, "manifest.json": manifest})

    v = dst.post("/service-data-archives/verify",
                 files={"file": ("a.zip", bad, "application/zip")})
    assert v.json()["valid"] is True  # 核验口径不变：该字段不在核验范围

    r = _restore(dst, bad)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "invalid_archive"
    assert any(i.get("field") == "created_at" for i in detail["issues"])
    assert _restore(dst, data).status_code == 201  # 未留痕迹


# ---------------- 非空实例拒绝 ----------------

def test_restore_rejects_non_empty_instance_database(pair):
    src, dst = pair
    ent = _rich_source(src)
    data = _archive_bytes(src)
    upload(dst, png_bytes(color=(7, 7, 7)))  # 目标实例已有业务数据

    r = _restore(dst, data)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "instance_not_empty"
    assert "documents" in detail["non_empty_tables"]

    # 源数据未写入：源实例的文档在目标上仍不存在
    assert dst.get(f"/documents/{ent['a']['doc_id']}").status_code == 404


def test_restore_rejects_stray_business_files(pair):
    src, dst = pair
    ent = _rich_source(src)
    data = _archive_bytes(src)
    # 业务库为空，但原图目录有遗留业务文件 -> 同样拒绝
    settings = dst.app.state.settings
    stray = os.path.join(settings.originals_dir, "leftover.png")
    with open(stray, "wb") as f:
        f.write(b"leftover-bytes")

    r = _restore(dst, data)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "instance_not_empty"
    assert detail["non_empty_dirs"] == ["originals"]
    assert detail["non_empty_tables"] == []
    assert dst.get(f"/documents/{ent['a']['doc_id']}").status_code == 404

    # 清理遗留文件后可正常恢复
    os.unlink(stray)
    assert _restore(dst, data).status_code == 201


# ---------------- 目标冲突 / 写入失败：全有或全无 ----------------

def test_restore_target_conflict_aborts_and_cleans_up(pair):
    src, dst = pair
    ent = _rich_source(src)
    data = _archive_bytes(src)
    settings = dst.app.state.settings
    docs = _records(data)["documents.json"]["documents"]
    first, second = docs[0]["doc_id"], docs[1]["doc_id"]

    # 空实例判定通过后（写锁内）制造目标冲突：第二份原图的目标路径已存在
    def hook():
        with open(os.path.join(settings.originals_dir, second + ".png"),
                  "wb") as f:
            f.write(b"pre-existing-content")

    main_module.RESTORE_EMPTY_CHECK_HOOK = hook
    try:
        r = _restore(dst, data)
    finally:
        main_module.RESTORE_EMPTY_CHECK_HOOK = None

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "restore_target_conflict"
    assert detail["path"] == f"originals/{second}.png"

    # 既有文件不被覆盖；本次已写的第一份原图被清理；库内无任何记录
    with open(os.path.join(settings.originals_dir, second + ".png"), "rb") as f:
        assert f.read() == b"pre-existing-content"
    assert not os.path.exists(
        os.path.join(settings.originals_dir, first + ".png"))
    assert dst.get(f"/documents/{ent['a']['doc_id']}").status_code == 404
    db = sqlite3.connect(settings.db_path)
    try:
        assert db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == 0
    finally:
        db.close()

    # 移除冲突文件后可正常恢复（无残留状态）
    os.unlink(os.path.join(settings.originals_dir, second + ".png"))
    assert _restore(dst, data).status_code == 201


def test_restore_write_failure_aborts_and_cleans_up(pair):
    src, dst = pair
    ent = _rich_source(src)
    data = _archive_bytes(src)
    settings = dst.app.state.settings
    pubs = _records(data)["publications.json"]["publications"]
    first_pub_doc = pubs[0]["doc_id"]

    # 空实例判定通过后，把首个成品的目标父目录占位为普通文件 -> 建目录失败
    blocker = os.path.join(settings.publications_dir, first_pub_doc)
    def hook():
        with open(blocker, "wb") as f:
            f.write(b"blocker")

    main_module.RESTORE_EMPTY_CHECK_HOOK = hook
    try:
        r = _restore(dst, data)
    finally:
        main_module.RESTORE_EMPTY_CHECK_HOOK = None

    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "restore_write_failed"

    # 先写的原图全部清理，库内无记录，占位文件不动
    docs = _records(data)["documents.json"]["documents"]
    for d in docs:
        assert not os.path.exists(
            os.path.join(settings.originals_dir, d["doc_id"] + ".png"))
    assert os.path.isfile(blocker)
    assert dst.get(f"/documents/{ent['a']['doc_id']}").status_code == 404

    os.unlink(blocker)
    assert _restore(dst, data).status_code == 201


# ---------------- 串行：并发恢复与并发写入 ----------------

def test_concurrent_restores_exactly_one_succeeds(pair):
    src, dst = pair
    _rich_source(src)
    data = _archive_bytes(src)
    outcomes: list = []

    def worker():
        outcomes.append(_restore(dst, data))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    codes = sorted(r.status_code for r in outcomes)
    assert codes == [201, 409]
    loser = next(r for r in outcomes if r.status_code == 409)
    assert loser.json()["detail"]["code"] == "instance_not_empty"

    # 最终状态恰为一次完整恢复
    docs = _records(data)["documents.json"]["documents"]
    for d in docs:
        assert dst.get(f"/documents/{d['doc_id']}").status_code == 200


def test_restore_serializes_with_concurrent_upload(pair):
    """恢复持写锁期间，并发上传的库写入被串行到恢复提交之后：

    空实例判定不被并发写入绕过——上传在恢复提交前不会完成；
    恢复提交后上传正常生效，两者数据并存。"""
    src, dst = pair
    ent = _rich_source(src)
    data = _archive_bytes(src)

    empty_checked = threading.Event()
    release = threading.Event()
    upload_returned = threading.Event()
    outcomes: dict = {}

    def hook():
        empty_checked.set()
        release.wait(timeout=10)

    def restorer():
        outcomes["restore"] = _restore(dst, data)

    def uploader():
        assert empty_checked.wait(timeout=10)
        outcomes["upload"] = upload(dst, png_bytes(color=(9, 9, 9)))
        upload_returned.set()

    main_module.RESTORE_EMPTY_CHECK_HOOK = hook
    try:
        t_r = threading.Thread(target=restorer)
        t_r.start()
        assert empty_checked.wait(timeout=10)
        t_u = threading.Thread(target=uploader)
        t_u.start()
        # 恢复持有写锁：并发上传不能抢先提交绕过空实例判定
        assert not upload_returned.wait(timeout=0.5), (
            "并发上传在恢复提交前就完成了：空实例判定被绕过")
        release.set()
        assert upload_returned.wait(timeout=20), "上传一直未完成（写锁可能泄漏）"
        t_r.join(timeout=20)
        t_u.join(timeout=20)
    finally:
        main_module.RESTORE_EMPTY_CHECK_HOOK = None

    assert outcomes["restore"].status_code == 201
    assert outcomes["upload"].status_code == 201
    uploaded_id = outcomes["upload"].json()["doc_id"]
    assert dst.get(f"/documents/{uploaded_id}").status_code == 200
    assert dst.get(f"/documents/{ent['a']['doc_id']}").status_code == 200


# ---------------- 临时文件清理 ----------------

def test_restore_temp_file_cleaned_up(pair):
    src, dst = pair
    _rich_source(src)
    data = _archive_bytes(src)
    assert _restore(dst, data).status_code == 201
    bad = rebuild(data, raw_updates={"documents.json": b"{broken"})
    assert _restore(dst, bad).status_code == 400
    leftovers = [n for n in os.listdir(tempfile.gettempdir())
                 if n.startswith("svc-restore-")]
    assert leftovers == []
