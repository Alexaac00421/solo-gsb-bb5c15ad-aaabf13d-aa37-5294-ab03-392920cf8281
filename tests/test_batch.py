"""批次发布 API 测试：原子性、修订号检查、顺序、不可变性、并发一致性。"""
import hashlib
import io
import sqlite3
import threading

import pytest
from PIL import Image
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.test_api import png_bytes, upload  # noqa: E402,F401  复用构造器与助手


@pytest.fixture()
def client(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"))
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _batch(client, pairs):
    """pairs: [(doc_id, expected_revision), ...]，按顺序构造批次请求。"""
    return client.post(
        "/publication-batches",
        json={
            "documents": [
                {"doc_id": d, "expected_revision": r} for d, r in pairs
            ]
        },
    )


def _annotate(client, doc_id, revision, rects=None):
    rects = rects if rects is not None else [
        {"x": 1, "y": 1, "w": 5, "h": 5, "reason": "敏感"}
    ]
    return client.put(
        f"/documents/{doc_id}/annotations",
        json={"revision": revision, "rects": rects},
    )


def _db(client):
    settings = client.app.state.settings
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return settings, conn


# ---------------- 成功路径 ----------------

def test_batch_publish_success_and_order(client):
    ids = [upload(client, png_bytes(color=c)).json()["doc_id"]
           for c in ((200, 10, 10), (10, 200, 10), (10, 10, 200))]
    # 让三份文档处于不同修订号
    _annotate(client, ids[0], 0)          # -> revision 1
    _annotate(client, ids[1], 0, rects=[])  # -> revision 1（清空也是新修订）

    r = _batch(client, [(ids[0], 1), (ids[1], 1), (ids[2], 0)])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["count"] == 3
    batch_id = body["batch_id"]
    docs = body["documents"]

    # 返回顺序与请求一致；每份各自从 1 开始占版本号
    assert [d["doc_id"] for d in docs] == ids
    assert [d["version"] for d in docs] == [1, 1, 1]
    assert [d["annotation_revision"] for d in docs] == [1, 1, 0]
    for d, color_doc in zip(docs, ids):
        assert d["output_sha256"] and d["original_sha256"]
        assert d["created_at"]

    # 按批次标识查询，顺序仍与请求一致
    got = client.get(f"/publication-batches/{batch_id}")
    assert got.status_code == 200
    assert [d["doc_id"] for d in got.json()["documents"]] == ids

    # 批次产物与单份发布走同一公开读取面，且内容兼容
    for d in docs:
        meta = client.get(
            f"/publications/{d['doc_id']}/{d['version']}"
        ).json()
        assert meta["annotation_revision"] == d["annotation_revision"]
        img = client.get(
            f"/publications/{d['doc_id']}/{d['version']}/image"
        )
        assert img.status_code == 200
        assert img.headers["content-type"] == "image/png"
        assert hashlib.sha256(img.content).hexdigest() == d["output_sha256"]


def test_batch_publish_renders_current_annotations_black(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]  # 40x30 红底
    _annotate(client, doc_id, 0,
              rects=[{"x": 5, "y": 5, "w": 10, "h": 10, "reason": "人脸"}])
    other = upload(client, png_bytes()).json()["doc_id"]
    r = _batch(client, [(doc_id, 1), (other, 0)])
    assert r.status_code == 201
    version = r.json()["documents"][0]["version"]
    img = Image.open(io.BytesIO(
        client.get(f"/publications/{doc_id}/{version}/image").content))
    assert img.format == "PNG"
    assert img.size == (40, 30)
    assert img.info.get("exif") is None  # 元数据剥离
    px = img.convert("RGB")
    assert px.getpixel((7, 7)) == (0, 0, 0)
    assert px.getpixel((15, 15)) == (200, 10, 10)


def test_batch_id_unknown(client):
    r = client.get("/publication-batches/deadbeef")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "batch_not_found"


# ---------------- 请求校验 ----------------

def test_batch_size_bounds(client):
    ids = [upload(client, png_bytes()).json()["doc_id"] for _ in range(3)]
    # 1 份：不足下限
    r = _batch(client, [(ids[0], 0)])
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "invalid_batch_size"
    assert detail["count"] == 1
    # 0 份
    r = client.post("/publication-batches", json={"documents": []})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_batch_size"

    # 21 份：超过上限（复用 3 个 ID 触发前先验数量，故不依赖重复检查顺序）
    r = client.post(
        "/publication-batches",
        json={"documents": [
            {"doc_id": f"d{i}", "expected_revision": 0} for i in range(21)
        ]},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_batch_size"


def test_batch_duplicate_documents_rejected(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    r = _batch(client, [(a, 0), (b, 0), (a, 0)])
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "duplicate_document_in_batch"
    assert detail["position"] == 2
    assert detail["doc_id"] == a


# ---------------- 整批失败：不留部分版本、不占版本号 ----------------

def test_batch_missing_document_fails_atomically(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    missing = "0" * 32
    # 缺失项放在中间，确保前面的文档也不能被发布
    r = _batch(client, [(a, 0), (missing, 0)])
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "document_not_found"
    assert detail["position"] == 1
    assert detail["doc_id"] == missing

    _assert_no_publications(client, [a])
    # 批次号也不应存在
    assert client.get(f"/publication-batches/{missing}").status_code == 404
    # 未被推进：下次单份发布仍是 v1
    _assert_next_single_version_is(client, a, 1)


def test_batch_stale_revision_fails_atomically(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    _annotate(client, a, 0)  # a 当前修订号 1
    r = _batch(client, [(a, 0), (b, 0)])  # a 携带过期的 0
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "stale_revision"
    assert detail["position"] == 0
    assert detail["doc_id"] == a
    assert detail["current_revision"] == 1
    assert detail["supplied_revision"] == 0

    _assert_no_publications(client, [a, b])

    # 修正修订号后整批成功；版本号从 1 开始，证明失败没有占用任何版本
    r = _batch(client, [(a, 1), (b, 0)])
    assert r.status_code == 201
    assert [d["version"] for d in r.json()["documents"]] == [1, 1]


def test_batch_original_unreadable_fails_atomically(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    settings, db = _db(client)
    # 让 b 的原图路径不可读
    row_b = db.execute("SELECT original_path FROM documents WHERE id=?", (b,)).fetchone()
    db.close()
    import os
    os.unlink(row_b["original_path"])

    r = _batch(client, [(a, 0), (b, 0)])
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["code"] == "original_unavailable"
    assert detail["position"] == 1
    assert detail["doc_id"] == b

    _assert_no_publications(client, [a, b])
    # 磁盘上也不能留下 a 的半成品成品文件
    assert not os.path.exists(
        os.path.join(settings.publications_dir, a, "v1.png"))

    # 修正后重试：a 的版本号仍为 1
    Image.new("RGB", (40, 30), (200, 10, 10)).save(row_b["original_path"], "PNG")
    r = _batch(client, [(a, 0), (b, 0)])
    assert r.status_code == 201
    assert [d["version"] for d in r.json()["documents"]] == [1, 1]


def test_batch_artifact_write_failure_rolls_back_and_cleans_files(client, monkeypatch):
    import app.main as main_mod
    from PIL import Image as PILImage

    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    settings, db = _db(client)
    db.close()

    real_save = PILImage.Image.save
    calls = {"n": 0}

    def flaky_save(self, fp, *args, **kwargs):
        # 只在落盘发布成品（fp 为路径且以 v*.png 结尾）时，第二次保存失败
        if isinstance(fp, str) and "/v" in fp and fp.endswith(".png"):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("simulated disk failure")
        return real_save(self, fp, *args, **kwargs)

    monkeypatch.setattr(PILImage.Image, "save", flaky_save)
    r = _batch(client, [(a, 0), (b, 0)])
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["code"] == "artifact_write_failed"
    assert detail["position"] == 1
    assert detail["doc_id"] == b
    assert detail["version"] == 1

    _assert_no_publications(client, [a, b])
    import os
    # a 已落盘的成品必须随事务回滚被清理；b 的半成品也不能残留
    assert not os.path.exists(
        os.path.join(settings.publications_dir, a, "v1.png"))
    assert not os.path.exists(
        os.path.join(settings.publications_dir, b, "v1.png"))
    # 没有批次记录，版本号未被占用
    _, db = _db(client)
    assert db.execute("SELECT COUNT(*) c FROM publication_batches").fetchone()["c"] == 0
    db.close()
    _assert_next_single_version_is(client, a, 1)


def _assert_no_publications(client, doc_ids):
    """失败批次不得留下任何 publications 记录（版本计数因此不会被推进）。"""
    _settings, db = _db(client)
    for doc_id in doc_ids:
        n = db.execute(
            "SELECT COUNT(*) c FROM publications WHERE doc_id=?", (doc_id,)
        ).fetchone()["c"]
        assert n == 0
    assert db.execute("SELECT COUNT(*) c FROM publication_batches").fetchone()["c"] == 0
    db.close()


def _assert_next_single_version_is(client, doc_id, expected):
    r = client.post(f"/documents/{doc_id}/publish")
    assert r.status_code == 201
    assert r.json()["version"] == expected


# ---------------- 与单份发布交错：版本号连续一致 ----------------

def test_batch_versions_interleave_with_single_publish(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    client.post(f"/documents/{a}/publish")  # a v1（单份）

    r = _batch(client, [(a, 0), (b, 0)])
    assert r.status_code == 201
    docs = r.json()["documents"]
    assert [d["version"] for d in docs] == [2, 1]  # a 续号，b 从 1 开始

    # 文档维度的发布列表同时可见单份与批次版本
    pubs = client.get(f"/documents/{a}/publications").json()["publications"]
    assert [p["version"] for p in pubs] == [1, 2]


# ---------------- 已发布版本不可变 ----------------

def test_batch_publications_are_immutable(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    _annotate(client, a, 0,
              rects=[{"x": 0, "y": 0, "w": 8, "h": 8, "reason": "旧标注"}])
    body = _batch(client, [(a, 1), (b, 0)]).json()
    a_item = body["documents"][0]
    frozen_digest = a_item["output_sha256"]

    # 批次之后修改 a 的标注，再单独发布
    _annotate(client, a, 1,
              rects=[{"x": 30, "y": 20, "w": 5, "h": 5, "reason": "新标注"}])
    client.post(f"/documents/{a}/publish")

    # 批次产生的版本保持原修订、原摘要
    img = client.get(f"/publications/{a}/{a_item['version']}/image").content
    assert hashlib.sha256(img).hexdigest() == frozen_digest
    meta = client.get(f"/publications/{a}/{a_item['version']}").json()
    assert meta["annotation_revision"] == 1
    assert meta["redactions"] == [
        {"x": 0, "y": 0, "w": 8, "h": 8, "reason": "旧标注"}]


# ---------------- 并发：批次与标注、单份发布、其他批次 ----------------

def test_concurrent_batches_with_annotations_and_single_publish(client):
    n_docs = 4
    ids = [upload(client, png_bytes(size=(50, 50))).json()["doc_id"]
           for _ in range(n_docs)]
    errors = []

    def annotator(k):
        for i in range(8):
            cur = client.get(
                f"/documents/{ids[k]}/annotations").json()["revision"]
            r = client.put(
                f"/documents/{ids[k]}/annotations",
                json={"revision": cur,
                      "rects": [{"x": (i * 4) % 40, "y": 2, "w": 4, "h": 4,
                                 "reason": f"区{k}-{i}"}]})
            if r.status_code not in (200, 409):
                errors.append(f"annotate: {r.status_code} {r.text}")

    def single_publisher(k):
        for _ in range(5):
            r = client.post(f"/documents/{ids[k]}/publish")
            if r.status_code != 201:
                errors.append(f"single publish: {r.status_code} {r.text}")

    def batch_publisher():
        for _ in range(6):
            # 总是先读取各文档当前修订号；读取后若又被标注者抢先，
            # 服务端返回 409——这是合法的乐观锁冲突，整批无副作用。
            pairs = [(d, client.get(
                f"/documents/{d}/annotations").json()["revision"])
                for d in ids]
            r = _batch(client, pairs)
            if r.status_code in (201, 409):
                continue
            errors.append(f"batch: {r.status_code} {r.text}")

    threads = ([threading.Thread(target=annotator, args=(k,))
                for k in range(n_docs)]
               + [threading.Thread(target=single_publisher, args=(k,))
                  for k in range(n_docs)]
               + [threading.Thread(target=batch_publisher) for _ in range(3)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    # 不变式 1：每份文档的版本号连续且不重复
    settings, db = _db(client)
    for doc_id in ids:
        versions = [r["version"] for r in db.execute(
            "SELECT version FROM publications WHERE doc_id=? ORDER BY version",
            (doc_id,))]
        assert versions == list(range(1, len(versions) + 1)), versions

    # 不变式 2：每条发布记录的摘要 == 按其修订号重渲染的摘要
    for doc_id in ids:
        doc = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        with open(doc["original_path"], "rb") as f:
            from app.imaging import load_oriented, render_publication
            oriented, _ = load_oriented(f.read())
        for p in db.execute(
                "SELECT * FROM publications WHERE doc_id=?", (doc_id,)):
            rects = db.execute(
                "SELECT x,y,w,h,reason FROM annotation_rects"
                " WHERE doc_id=? AND revision=? ORDER BY idx",
                (doc_id, p["annotation_revision"])).fetchall()
            buf = io.BytesIO()
            render_publication(oriented, rects).save(buf, "PNG")
            assert hashlib.sha256(buf.getvalue()).hexdigest() == p["output_sha256"]

    # 不变式 3：所有批次记录的条目都能在 publications 找到对应版本，
    # 且每份文档公开版本序列连续（批次失败未留洞）
    batches = db.execute("SELECT id FROM publication_batches").fetchall()
    for b in batches:
        rows = db.execute(
            "SELECT doc_id, version FROM batch_publications"
            " WHERE batch_id=? ORDER BY position", (b["id"],)).fetchall()
        assert 2 <= len(rows) <= 4
        for r in rows:
            hit = db.execute(
                "SELECT 1 FROM publications WHERE doc_id=? AND version=?",
                (r["doc_id"], r["version"])).fetchone()
            assert hit is not None
    db.close()

    # 不变式 4：批次查询顺序 == 请求顺序（成功批次返回的 documents 即顺序）
    # 由成功路径用例覆盖；这里再抽查一批
    settings2, db2 = _db(client)
    first = db2.execute("SELECT id FROM publication_batches LIMIT 1").fetchone()
    if first:
        positions = [r["position"] for r in db2.execute(
            "SELECT position FROM batch_publications WHERE batch_id=?"
            " ORDER BY position", (first["id"],))]
        assert positions == list(range(len(positions)))
    db2.close()
