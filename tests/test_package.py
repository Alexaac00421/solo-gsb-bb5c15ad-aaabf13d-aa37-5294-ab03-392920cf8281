"""离线交付包测试：ZIP（各版 PNG + JSON 清单）。

覆盖：
- 成功交付：顺序、清单字段、成品 SHA-256 与遮盖数、包内文件名不取上传名、
  同文档多版本、manifest 与包内内容一致；
- 整包拒绝：数量越界、(doc_id, version) 重复、未知文档/版本、未到公开时间、
  已撤回（不泄露成品与遮盖详情），错误均带 position；
- 成品缺失/损坏不交付部分包；
- 资格确认与撤回串行：打包先确认则本次交付完成，撤回先生效则整包拒绝。
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
from PIL import Image
from fastapi.testclient import TestClient

from app import main as main_module
from app.config import Settings
from app.main import create_app
from tests.test_api import png_bytes, upload  # noqa: E402,F401  复用构造器与助手


@pytest.fixture()
def client(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"))
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _new_doc(client, color=(10, 20, 30), filename="img.png"):
    return upload(client, png_bytes(color=color), filename).json()["doc_id"]


def _annotate(client, doc_id, revision, rects):
    r = client.put(
        f"/documents/{doc_id}/annotations",
        json={"revision": revision, "rects": rects},
    )
    assert r.status_code == 200, r.text


def _publish(client, doc_id, **kwargs):
    r = client.post(f"/documents/{doc_id}/publish", json=kwargs or None)
    assert r.status_code == 201, r.text
    return r.json()


def _withdraw(client, doc_id, version, reason="遮盖遗漏：右下角工号未遮盖"):
    return client.post(
        f"/documents/{doc_id}/publications/{version}/withdraw",
        json={"reason": reason},
    )


def _package(client, items):
    return client.post("/publication-packages", json={"items": items})


def _unzip(response) -> zipfile.ZipFile:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    return zipfile.ZipFile(io.BytesIO(response.content))


# ---------------- 成功交付 ----------------

def test_package_success_order_manifest_and_content(client):
    d1 = _new_doc(client, (10, 20, 30))
    d2 = _new_doc(client, (40, 50, 60))
    _annotate(client, d1, 0, [{"x": 0, "y": 0, "w": 4, "h": 4, "reason": "甲"}])
    _annotate(client, d2, 0, [
        {"x": 0, "y": 0, "w": 3, "h": 3, "reason": "乙"},
        {"x": 10, "y": 10, "w": 2, "h": 2, "reason": "丙"},
    ])
    p1 = _publish(client, d1)
    p2 = _publish(client, d2)

    # 请求顺序为 d2(v1), d1(v1) —— 清单与包内次序必须跟随请求，而非文档顺序
    zf = _unzip(_package(client, [
        {"doc_id": d2, "version": 1},
        {"doc_id": d1, "version": 1},
    ]))
    assert zf.namelist() == [
        f"01_{d2}_v1.png", f"02_{d1}_v1.png", "manifest.json"]

    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["count"] == 2
    assert manifest["created_at"]
    assert [e["doc_id"] for e in manifest["items"]] == [d2, d1]

    e2, e1 = manifest["items"]
    assert e2 == {
        "position": 0, "doc_id": d2, "version": 1,
        "output_sha256": p2["output_sha256"],
        "redaction_count": 2, "file": f"01_{d2}_v1.png",
    }
    assert e1 == {
        "position": 1, "doc_id": d1, "version": 1,
        "output_sha256": p1["output_sha256"],
        "redaction_count": 1, "file": f"02_{d1}_v1.png",
    }

    # 包内 PNG 即发布成品：字节摘要与清单一致，可正常解码
    for entry, expected_digest in ((e2, p2["output_sha256"]),
                                   (e1, p1["output_sha256"])):
        raw = zf.read(entry["file"])
        assert hashlib.sha256(raw).hexdigest() == expected_digest
        img = Image.open(io.BytesIO(raw))
        assert img.format == "PNG" and img.size == (40, 30)


def test_package_accepts_same_document_distinct_versions(client):
    # 互不重复以 (doc_id, version) 对计：同一文档的不同版本可以同包
    d = _new_doc(client)
    _annotate(client, d, 0, [{"x": 0, "y": 0, "w": 4, "h": 4, "reason": "甲"}])
    p1 = _publish(client, d)
    _annotate(client, d, 1, [])
    p2 = _publish(client, d)

    zf = _unzip(_package(client, [
        {"doc_id": d, "version": 2},
        {"doc_id": d, "version": 1},
    ]))
    manifest = json.loads(zf.read("manifest.json"))
    assert [e["version"] for e in manifest["items"]] == [2, 1]
    assert [e["redaction_count"] for e in manifest["items"]] == [0, 1]
    assert manifest["items"][0]["output_sha256"] == p2["output_sha256"]
    assert manifest["items"][1]["output_sha256"] == p1["output_sha256"]


def test_package_inner_filenames_never_come_from_upload_names(client):
    # 上传名含路径片段/空格/中文，包内文件名仍只由位置、服务端文档ID、版本号生成
    d = _new_doc(client, (1, 2, 3), filename="../../etc/evil name 证件.png")
    e = _new_doc(client, (4, 5, 6))
    _publish(client, d)
    _publish(client, e)

    zf = _unzip(_package(client, [
        {"doc_id": d, "version": 1},
        {"doc_id": e, "version": 1},
    ]))
    names = zf.namelist()
    for name in names:
        # 扁平结构，无目录穿越
        assert "/" not in name and "\\" not in name
    assert f"01_{d}_v1.png" in names
    assert f"02_{e}_v1.png" in names
    joined = "\n".join(names)
    assert "evil" not in joined and "证件" not in joined


def test_package_size_bounds_2_and_20(client):
    docs = []
    for _ in range(20):
        d = _new_doc(client)
        docs.append(_publish(client, d))

    for n in (0, 1):
        r = _package(client, [{"doc_id": docs[0]["doc_id"], "version": 1}]
                     if n == 1 else [])
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert detail["code"] == "invalid_package_size"
        assert detail["count"] == n

    r = _package(client, [{"doc_id": docs[i]["doc_id"], "version": 1}
                          for i in range(20)])
    assert r.status_code == 200, r.text

    extra = _new_doc(client)
    _publish(client, extra)
    r = _package(client, [{"doc_id": docs[i]["doc_id"], "version": 1}
                          for i in range(20)]
                 + [{"doc_id": extra, "version": 1}])
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "invalid_package_size"
    assert detail["count"] == 21


# ---------------- 重复 ----------------

def test_package_duplicate_doc_version_pair_rejected(client):
    d = _new_doc(client)
    _annotate(client, d, 0, [{"x": 0, "y": 0, "w": 1, "h": 1, "reason": "密"}])
    _publish(client, d)
    _annotate(client, d, 1, [])
    _publish(client, d)

    r = _package(client, [
        {"doc_id": d, "version": 1},
        {"doc_id": d, "version": 1},
    ])
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "duplicate_item_in_package"
    assert detail["position"] == 1
    assert detail["doc_id"] == d and detail["version"] == 1


# ---------------- 未知版本 / 未到公开时间 / 已撤回：整包拒绝 ----------------

def test_package_unknown_document_and_version_rejected_with_position(client):
    d = _new_doc(client)
    _publish(client, d)
    other = _new_doc(client)
    _publish(client, other)

    r = _package(client, [
        {"doc_id": d, "version": 1},
        {"doc_id": "deadbeef" * 4, "version": 1},
    ])
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "document_not_found"
    assert detail["position"] == 1
    serialized = json.dumps(detail, ensure_ascii=False)
    assert "output_sha256" not in serialized
    assert "redaction_count" not in serialized

    r = _package(client, [
        {"doc_id": d, "version": 99},
        {"doc_id": other, "version": 1},
    ])
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "unknown_version"
    assert detail["position"] == 0 and detail["version"] == 99


def test_package_not_yet_public_version_rejects_whole_package(client):
    future = "2026-10-20T08:00:00Z"  # 晚于 today 2026-10-05，且在 30 天窗口内
    d = _new_doc(client)
    _publish(client, d)
    sched = _new_doc(client, (7, 8, 9))
    _publish(client, sched, publish_at=future)

    r = _package(client, [
        {"doc_id": d, "version": 1},
        {"doc_id": sched, "version": 1},
    ])
    assert r.status_code == 404
    detail = r.json()["detail"]
    # 与公开读取一致：不泄露成品/遮盖详情
    assert detail["code"] == "publication_not_yet_public"
    assert detail["position"] == 1
    serialized = json.dumps(detail, ensure_ascii=False)
    assert "output_sha256" not in serialized
    assert "redaction_count" not in serialized
    # 响应不是 ZIP，没有任何条目被部分交付
    assert r.headers["content-type"] != "application/zip"


def test_package_withdrawn_version_rejects_whole_package_no_details(client):
    d1 = _new_doc(client)
    d2 = _new_doc(client, (9, 9, 9))
    _annotate(client, d1, 0, [{"x": 0, "y": 0, "w": 1, "h": 1, "reason": "密"}])
    _publish(client, d1)
    _publish(client, d2)
    assert _withdraw(client, d1, 1).status_code == 201

    r = _package(client, [
        {"doc_id": d2, "version": 1},
        {"doc_id": d1, "version": 1},
    ])
    assert r.status_code == 410
    detail = r.json()["detail"]
    assert detail["code"] == "publication_withdrawn"
    assert detail["position"] == 1 and detail["doc_id"] == d1
    # 不附成品摘要、遮盖数或撤回原因
    serialized = json.dumps(detail, ensure_ascii=False)
    assert "redaction_count" not in serialized
    assert "output_sha256" not in serialized
    assert "遮盖遗漏" not in serialized
    assert r.headers["content-type"] != "application/zip"


# ---------------- 成品缺失/损坏：不交付部分包 ----------------

def test_package_missing_artifact_on_disk_delivers_nothing(client):
    d1 = _new_doc(client)
    d2 = _new_doc(client, (5, 6, 7))
    _publish(client, d1)
    _publish(client, d2)

    settings = client.app.state.settings
    db = sqlite3.connect(settings.db_path)
    db.row_factory = sqlite3.Row
    path = db.execute(
        "SELECT output_path FROM publications WHERE doc_id = ? AND version = 1",
        (d1,),
    ).fetchone()["output_path"]
    db.close()
    os.unlink(path)  # 模拟成品读盘失败（文件缺失）

    r = _package(client, [
        {"doc_id": d1, "version": 1},
        {"doc_id": d2, "version": 1},
    ])
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["code"] == "artifact_missing"
    assert detail["position"] == 0
    assert r.headers["content-type"] != "application/zip"


def test_package_corrupt_artifact_fails_integrity_and_delivers_nothing(client):
    d1 = _new_doc(client)
    d2 = _new_doc(client, (5, 6, 7))
    _publish(client, d1)
    _publish(client, d2)

    settings = client.app.state.settings
    db = sqlite3.connect(settings.db_path)
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT output_path FROM publications WHERE doc_id = ? AND version = 1",
        (d1,),
    ).fetchone()
    with open(row["output_path"], "wb") as f:
        f.write(b"not-the-original-artifact")
    db.close()

    r = _package(client, [
        {"doc_id": d1, "version": 1},
        {"doc_id": d2, "version": 1},
    ])
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["code"] == "artifact_corrupt"
    assert detail["position"] == 0
    assert r.headers["content-type"] != "application/zip"


# ---------------- 资格确认与撤回串行 ----------------

def test_package_confirmed_first_completes_even_if_withdraw_waits(client):
    """打包在写锁内先完成资格确认：并发撤回被写锁挡到打包事务提交之后，
    本次交付照常完成；撤回随后生效，此后新发起的整包被拒。

    可观察的串行证据：撤回请求在打包事务放行之前**已经发起但尚未返回**，
    它只能在打包事务提交后才完成。"""
    d = _new_doc(client)
    e = _new_doc(client, (2, 3, 4))
    _publish(client, d)
    _publish(client, e)

    eligibility_done = threading.Event()
    withdraw_started = threading.Event()
    withdraw_returned = threading.Event()
    release = threading.Event()
    outcomes = {}

    def hook():
        eligibility_done.set()
        release.wait(timeout=10)

    def packer():
        outcomes["package"] = _package(client, [
            {"doc_id": d, "version": 1},
            {"doc_id": e, "version": 1},
        ])

    def withdrawer():
        assert eligibility_done.wait(timeout=10)
        withdraw_started.set()
        outcomes["withdraw"] = _withdraw(client, d, 1)
        withdraw_returned.set()

    main_module.PACKAGE_ELIGIBILITY_HOOK = hook
    try:
        t_pack = threading.Thread(target=packer)
        t_pack.start()
        assert eligibility_done.wait(timeout=10)
        t_withdraw = threading.Thread(target=withdrawer)
        t_withdraw.start()

        # 撤回请求已发起，且在写锁上排队等待打包事务
        assert withdraw_started.wait(timeout=10)
        assert not withdraw_returned.wait(timeout=0.5), (
            "撤回在打包事务提交前就完成了：资格确认未与撤回串行")

        # 放行打包：打包先交付，撤回随后才完成
        release.set()
        assert withdraw_returned.wait(timeout=20), "撤回一直未完成（写锁可能泄漏）"
        t_pack.join(timeout=20)
        t_withdraw.join(timeout=20)
    finally:
        main_module.PACKAGE_ELIGIBILITY_HOOK = None

    assert "package" in outcomes and "withdraw" in outcomes
    assert outcomes["package"].status_code == 200, outcomes["package"].text
    zf = zipfile.ZipFile(io.BytesIO(outcomes["package"].content))
    assert f"01_{d}_v1.png" in zf.namelist()
    assert outcomes["withdraw"].status_code in (200, 201)

    # 撤回随后生效（成品文件保留），此后新发起的整包必须拒绝
    after = _package(client, [
        {"doc_id": d, "version": 1},
        {"doc_id": e, "version": 1},
    ])
    assert after.status_code == 410
    assert after.json()["detail"]["code"] == "publication_withdrawn"


def test_withdraw_committed_first_rejects_package(client):
    """撤回先生效：随后的打包资格确认看到撤回记录，整包拒绝。"""
    d = _new_doc(client)
    e = _new_doc(client, (2, 3, 4))
    _publish(client, d)
    _publish(client, e)
    assert _withdraw(client, d, 1).status_code == 201

    r = _package(client, [
        {"doc_id": d, "version": 1},
        {"doc_id": e, "version": 1},
    ])
    assert r.status_code == 410
    assert r.json()["detail"]["code"] == "publication_withdrawn"


def test_package_temp_zip_is_cleaned_up_after_delivery(client):
    d = _new_doc(client)
    e = _new_doc(client, (2, 3, 4))
    _publish(client, d)
    _publish(client, e)
    r = _package(client, [
        {"doc_id": d, "version": 1},
        {"doc_id": e, "version": 1},
    ])
    assert r.status_code == 200
    # BackgroundTasks 在 TestClient 响应返回后执行
    leftovers = [n for n in os.listdir(tempfile.gettempdir())
                 if n.startswith("pub-pkg-")]
    assert leftovers == []
