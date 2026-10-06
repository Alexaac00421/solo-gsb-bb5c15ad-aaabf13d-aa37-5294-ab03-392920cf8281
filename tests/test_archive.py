"""服务数据归档测试：同一数据库快照（文档/标注/发布/撤回 + 引用文件）ZIP。

覆盖：
- 成功归档：相对路径结构、记录 JSON 内容、原图与成品字节摘要、manifest
  清单（SHA-256、文档/版本标识、文件数）、空库、JPEG 扩展名不取上传名；
- 只对应一个一致时点：归档期间并发提交的标注/发布对本次归档不可见，
  且归档为纯只读、不阻塞并发写入；
- 全有或全无：任一原图/成品缺失、读盘失败、摘要不符即整单拒绝
  （不是 ZIP，不交付部分归档）；
- ZIP 完整生成后才响应；失败/成功后临时文件均被清理。
"""
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.config import Settings
from app.main import create_app
from tests.test_api import jpeg_bytes, png_bytes, upload  # noqa: E402,F401


@pytest.fixture()
def client(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"))
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _new_doc(client, color=(10, 20, 30), filename="img.png", data=None):
    payload = data if data is not None else png_bytes(color=color)
    return upload(client, payload, filename).json()


def _annotate(client, doc_id, revision, rects):
    r = client.put(
        f"/documents/{doc_id}/annotations",
        json={"revision": revision, "rects": rects},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _publish(client, doc_id, **kwargs):
    r = client.post(f"/documents/{doc_id}/publish", json=kwargs or None)
    assert r.status_code == 201, r.text
    return r.json()


def _withdraw(client, doc_id, version, reason="遮盖遗漏：右下角工号未遮盖"):
    return client.post(
        f"/documents/{doc_id}/publications/{version}/withdraw",
        json={"reason": reason},
    )


def _archive(client):
    return client.post("/service-data-archives")


def _unzip(response) -> zipfile.ZipFile:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    return zipfile.ZipFile(io.BytesIO(response.content))


# ---------------- 成功归档 ----------------

def test_archive_contains_relative_paths_records_and_files(client):
    doc = _new_doc(client, (10, 20, 30), filename="证件.png")
    _annotate(client, doc["doc_id"], 0, [
        {"x": 0, "y": 0, "w": 4, "h": 4, "reason": "甲"},
    ])
    pub = _publish(client, doc["doc_id"])
    assert _withdraw(client, doc["doc_id"], 1).status_code == 201

    zf = _unzip(_archive(client))
    names = zf.namelist()

    # 顶层记录 JSON + originals/ 与 publications/ 下的引用文件 + 清单
    expected = {
        "documents.json",
        "annotations.json",
        "publications.json",
        "publication-batches.json",
        "withdrawals.json",
        f"originals/{doc['doc_id']}.png",
        f"publications/{doc['doc_id']}/v1.png",
        "manifest.json",
    }
    assert set(names) == expected
    # 清单始终最后写入
    assert names[-1] == "manifest.json"
    # 全部为相对路径：无盘符、无前导斜杠、无服务端绝对路径痕迹
    for n in names:
        assert not n.startswith("/") and ":" not in n and ".." not in n

    documents = json.loads(zf.read("documents.json"))
    assert documents["count"] == 1
    d = documents["documents"][0]
    assert d["doc_id"] == doc["doc_id"]
    assert d["filename"] == "证件.png"  # 上传名仅记录在 JSON，不进入文件名
    assert d["original_sha256"] == doc["original_sha256"]
    assert d["original_file"] == f"originals/{doc['doc_id']}.png"
    assert d["annotation_revision"] == 1

    annotations = json.loads(zf.read("annotations.json"))
    assert annotations["document_count"] == 1
    assert annotations["revision_count"] == 2  # revision 0（空）与 1（有矩形）
    doc_ann = annotations["documents"][0]
    assert doc_ann["current_revision"] == 1
    revs = doc_ann["revisions"]
    assert [r["revision"] for r in revs] == [0, 1]
    assert revs[0]["rect_count"] == 0 and revs[0]["rects"] == []
    assert revs[1]["rect_count"] == 1
    assert revs[1]["rects"][0]["reason"] == "甲"

    publications = json.loads(zf.read("publications.json"))
    assert publications["count"] == 1
    p = publications["publications"][0]
    assert p["doc_id"] == doc["doc_id"] and p["version"] == 1
    assert p["annotation_revision"] == 1
    assert p["output_sha256"] == pub["output_sha256"]
    assert p["output_file"] == f"publications/{doc['doc_id']}/v1.png"
    assert p["withdrawn"] is True and p["withdrawn_at"]
    assert p["withdrawal_reason"] == "遮盖遗漏：右下角工号未遮盖"

    withdrawals = json.loads(zf.read("withdrawals.json"))
    assert withdrawals["count"] == 1
    w = withdrawals["withdrawals"][0]
    assert w == {
        "doc_id": doc["doc_id"], "version": 1,
        "reason": "遮盖遗漏：右下角工号未遮盖",
        "withdrawn_at": p["withdrawn_at"],
    }

    # 引用文件字节与快照摘要一致
    orig_bytes = zf.read(f"originals/{doc['doc_id']}.png")
    assert hashlib.sha256(orig_bytes).hexdigest() == doc["original_sha256"]
    out_bytes = zf.read(f"publications/{doc['doc_id']}/v1.png")
    assert hashlib.sha256(out_bytes).hexdigest() == pub["output_sha256"]


def test_archive_manifest_lists_sha256_identifiers_and_file_count(client):
    d1 = _new_doc(client, (1, 2, 3))
    d2 = _new_doc(client, (4, 5, 6))
    _publish(client, d1["doc_id"])
    _publish(client, d2["doc_id"])

    zf = _unzip(_archive(client))
    manifest = json.loads(zf.read("manifest.json"))

    assert manifest["record_counts"] == {
        "documents": 2,
        "annotation_revisions": 2,  # 每份文档仅 revision 0
        "publications": 2,
        "publication_batches": 0,
        "withdrawals": 0,
    }
    # 2 原图 + 2 成品 + 5 个记录 JSON = 9（manifest.json 自身不计入）
    assert manifest["file_count"] == 9
    assert len(manifest["files"]) == 9

    paths = [e["path"] for e in manifest["files"]]
    assert paths == sorted(paths)  # 按相对路径排序，可复现

    by_path = {e["path"]: e for e in manifest["files"]}
    # 每个条目都带 SHA-256 与文件数
    for e in manifest["files"]:
        assert len(e["sha256"]) == 64 and e["file_count"] == 1
        # 清单摘要与包内实际字节一致
        assert e["sha256"] == hashlib.sha256(zf.read(e["path"])).hexdigest()

    o1 = by_path[f"originals/{d1['doc_id']}.png"]
    assert o1["kind"] == "original" and o1["doc_id"] == d1["doc_id"]
    assert "version" not in o1
    pub2 = by_path[f"publications/{d2['doc_id']}/v1.png"]
    assert pub2["kind"] == "publication"
    assert pub2["doc_id"] == d2["doc_id"] and pub2["version"] == 1

    docs_json = by_path["documents.json"]
    assert docs_json["kind"] == "records" and "doc_id" not in docs_json


def test_archive_includes_batches_and_all_publication_states(client):
    # 批次（含批次元数据与成员）、定时公开版本与撤回版本都属"已提交数据"，
    # 归档不做公开面门控，全部收录。
    a = _new_doc(client, (1, 1, 1))
    b = _new_doc(client, (2, 2, 2))
    _annotate(client, a["doc_id"], 0, [
        {"x": 0, "y": 0, "w": 1, "h": 1, "reason": "x"}])
    _annotate(client, b["doc_id"], 0, [
        {"x": 0, "y": 0, "w": 1, "h": 1, "reason": "y"}])
    batch = client.post("/publication-batches", json={
        "documents": [
            {"doc_id": a["doc_id"], "expected_revision": 1},
            {"doc_id": b["doc_id"], "expected_revision": 1},
        ],
        "publish_at": "2026-10-20T08:00:00Z",
    })
    assert batch.status_code == 201, batch.text
    batch_id = batch.json()["batch_id"]

    zf = _unzip(_archive(client))
    batches = json.loads(zf.read("publication-batches.json"))
    assert batches["count"] == 1
    be = batches["batches"][0]
    assert be["batch_id"] == batch_id
    assert be["publish_at"] == "2026-10-20T08:00:00Z"
    assert be["documents"] == [
        {"position": 0, "doc_id": a["doc_id"], "version": 1},
        {"position": 1, "doc_id": b["doc_id"], "version": 1},
    ]

    publications = json.loads(zf.read("publications.json"))
    by_pub = {(p["doc_id"], p["version"]): p
              for p in publications["publications"]}
    pa = by_pub[(a["doc_id"], 1)]
    assert pa["batch_id"] == batch_id
    assert pa["publish_at"] == "2026-10-20T08:00:00Z"
    assert pa["withdrawn"] is False
    # 未到公开时间的成品仍被归档（归档不是公开读取）
    assert f"publications/{a['doc_id']}/v1.png" in zf.namelist()


def test_archive_jpeg_original_extension_comes_from_media_type(client):
    doc = _new_doc(client, data=jpeg_bytes(size=(10, 20), orientation=6),
                   filename="../../etc/evil name.jpg")
    zf = _unzip(_archive(client))
    names = zf.namelist()
    # 扩展名按 media_type 映射为 .jpg；文件名只有服务端 doc_id，无穿越/上传名
    assert f"originals/{doc['doc_id']}.jpg" in names
    joined = "\n".join(names)
    assert "evil" not in joined and ".." not in joined
    d = json.loads(zf.read("documents.json"))["documents"][0]
    assert d["original_file"] == f"originals/{doc['doc_id']}.jpg"
    assert d["width"] == 20 and d["height"] == 10  # EXIF 旋正后尺寸


def test_archive_empty_database_is_a_valid_archive(client):
    zf = _unzip(_archive(client))
    assert set(zf.namelist()) == {
        "documents.json", "annotations.json", "publications.json",
        "publication-batches.json", "withdrawals.json", "manifest.json",
    }
    for name, key in (
        ("documents.json", "documents"),
        ("publications.json", "publications"),
        ("publication-batches.json", "batches"),
        ("withdrawals.json", "withdrawals"),
    ):
        payload = json.loads(zf.read(name))
        assert payload["count"] == 0 and payload[key] == []
    annotations = json.loads(zf.read("annotations.json"))
    assert annotations == {"document_count": 0, "revision_count": 0,
                           "documents": []}
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["file_count"] == 5  # 仅 5 个记录 JSON
    assert manifest["record_counts"] == {
        "documents": 0, "annotation_revisions": 0, "publications": 0,
        "publication_batches": 0, "withdrawals": 0,
    }


# ---------------- 同一一致时点：并发写入继续，归档只见快照 ----------------

def test_archive_uses_one_snapshot_and_does_not_block_concurrent_writes(client):
    d = _new_doc(client)
    _annotate(client, d["doc_id"], 0, [
        {"x": 0, "y": 0, "w": 2, "h": 2, "reason": "r0"}])
    _publish(client, d["doc_id"])  # v1 @ revision 1

    snapshot_read = threading.Event()
    release = threading.Event()
    writer_returned = threading.Event()
    outcomes: dict = {}

    def hook():
        # 快照记录已读、引用文件尚未校验：此刻从另一线程提交新标注+新发布
        snapshot_read.set()
        release.wait(timeout=10)

    def archiver():
        outcomes["archive"] = _archive(client)

    def writer():
        assert snapshot_read.wait(timeout=10)
        # 归档进行中并发写入必须能继续（归档不持写锁）
        _annotate(client, d["doc_id"], 1, [
            {"x": 3, "y": 3, "w": 2, "h": 2, "reason": "r1"}])
        outcomes["publish"] = _publish(client, d["doc_id"])  # v2 @ rev 2
        writer_returned.set()

    main_module.ARCHIVE_SNAPSHOT_HOOK = hook
    try:
        t_arc = threading.Thread(target=archiver)
        t_arc.start()
        assert snapshot_read.wait(timeout=10)

        t_writer = threading.Thread(target=writer)
        t_writer.start()
        # 归档尚未放行时，并发写入就已完成 -> 未被归档阻塞
        assert writer_returned.wait(timeout=10), "归档期间并发写入被阻塞"

        release.set()
        t_arc.join(timeout=20)
        t_writer.join(timeout=20)
    finally:
        main_module.ARCHIVE_SNAPSHOT_HOOK = None

    resp = outcomes["archive"]
    zf = _unzip(resp)

    # 归档只对应读快照建立的时点：只含 revision 0/1 与 v1，
    # 看不到归档期间提交的 revision 2 与 v2
    doc = json.loads(zf.read("documents.json"))["documents"][0]
    assert doc["annotation_revision"] == 1
    annotations = json.loads(zf.read("annotations.json"))["documents"][0]
    assert annotations["current_revision"] == 1
    assert [r["revision"] for r in annotations["revisions"]] == [0, 1]
    publications = json.loads(zf.read("publications.json"))
    assert [p["version"] for p in publications["publications"]] == [1]
    names = zf.namelist()
    assert f"publications/{d['doc_id']}/v1.png" in names
    assert f"publications/{d['doc_id']}/v2.png" not in names

    # 并发写入本身确实已提交（随后的新归档能看到）
    later = _unzip(_archive(client))
    later_doc = json.loads(later.read("documents.json"))["documents"][0]
    assert later_doc["annotation_revision"] == 2
    assert f"publications/{d['doc_id']}/v2.png" in later.namelist()


# ---------------- 全有或全无：引用文件缺失/损坏/读盘失败 ----------------

def test_archive_rejected_when_original_missing(client):
    d1 = _new_doc(client, (1, 2, 3))
    d2 = _new_doc(client, (4, 5, 6))
    _publish(client, d1["doc_id"])
    _publish(client, d2["doc_id"])
    os.unlink(os.path.join(client.app.state.settings.originals_dir,
                           f"{d1['doc_id']}.png"))

    r = _archive(client)
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["code"] == "archive_original_missing"
    assert detail["doc_id"] == d1["doc_id"]
    assert r.headers["content-type"] != "application/zip"


def test_archive_rejected_when_original_digest_mismatch(client):
    d1 = _new_doc(client, (1, 2, 3))
    d2 = _new_doc(client, (4, 5, 6))
    _publish(client, d1["doc_id"])
    _publish(client, d2["doc_id"])
    with open(os.path.join(client.app.state.settings.originals_dir,
                           f"{d1['doc_id']}.png"), "wb") as f:
        f.write(b"tampered-original-bytes")

    r = _archive(client)
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["code"] == "archive_original_corrupt"
    assert detail["doc_id"] == d1["doc_id"]
    assert r.headers["content-type"] != "application/zip"


def test_archive_rejected_when_artifact_missing(client):
    d1 = _new_doc(client, (1, 2, 3))
    d2 = _new_doc(client, (4, 5, 6))
    _publish(client, d1["doc_id"])
    _publish(client, d2["doc_id"])

    settings = client.app.state.settings
    db = sqlite3.connect(settings.db_path)
    db.row_factory = sqlite3.Row
    out_path = db.execute(
        "SELECT output_path FROM publications WHERE doc_id = ? AND version = 1",
        (d1["doc_id"],),
    ).fetchone()["output_path"]
    db.close()
    os.unlink(out_path)

    r = _archive(client)
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["code"] == "archive_artifact_missing"
    assert detail["doc_id"] == d1["doc_id"] and detail["version"] == 1
    assert r.headers["content-type"] != "application/zip"


def test_archive_rejected_when_artifact_digest_mismatch(client):
    d1 = _new_doc(client, (1, 2, 3))
    _publish(client, d1["doc_id"])
    settings = client.app.state.settings
    db = sqlite3.connect(settings.db_path)
    db.row_factory = sqlite3.Row
    out_path = db.execute(
        "SELECT output_path FROM publications WHERE doc_id = ? AND version = 1",
        (d1["doc_id"],),
    ).fetchone()["output_path"]
    db.close()
    with open(out_path, "wb") as f:
        f.write(b"not-the-rendered-artifact")

    r = _archive(client)
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["code"] == "archive_artifact_corrupt"
    assert detail["doc_id"] == d1["doc_id"] and detail["version"] == 1
    assert r.headers["content-type"] != "application/zip"


def test_archive_zip_write_failure_cleans_temp_file(client, monkeypatch):
    d = _new_doc(client)
    _publish(client, d["doc_id"])

    real_writestr = zipfile.ZipFile.writestr

    def boom(self, *args, **kwargs):
        raise OSError("simulated disk failure while writing the ZIP")

    monkeypatch.setattr(zipfile.ZipFile, "writestr", boom)
    try:
        r = _archive(client)
        assert r.status_code == 500
        assert r.headers["content-type"] != "application/zip"
    finally:
        monkeypatch.setattr(zipfile.ZipFile, "writestr", real_writestr)

    leftovers = [n for n in os.listdir(tempfile.gettempdir())
                 if n.startswith("svc-archive-")]
    assert leftovers == []


# ---------------- 临时文件清理与下载文件名 ----------------

def test_archive_temp_zip_cleaned_up_after_delivery(client):
    d = _new_doc(client)
    _publish(client, d["doc_id"])
    r = _archive(client)
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert cd.startswith('attachment; filename="service-data-archive-')
    assert cd.endswith('.zip"')
    leftovers = [n for n in os.listdir(tempfile.gettempdir())
                 if n.startswith("svc-archive-")]
    assert leftovers == []
