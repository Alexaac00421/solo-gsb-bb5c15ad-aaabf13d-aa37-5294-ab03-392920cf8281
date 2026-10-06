"""批次发布幂等键测试：重放、冲突、失败不占键、并发最多一次、兼容性。"""
import hashlib
import io
import os
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


def _batch(client, pairs, key=None):
    """pairs: [(doc_id, expected_revision), ...]；key 为可选幂等键。"""
    body = {"documents": [{"doc_id": d, "expected_revision": r} for d, r in pairs]}
    if key is not None:
        body["idempotency_key"] = key
    return client.post("/publication-batches", json=body)


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


def _count(client, table, where="", args=()):
    _, db = _db(client)
    n = db.execute(f"SELECT COUNT(*) c FROM {table} {where}", args).fetchone()["c"]
    db.close()
    return n


# ---------------- 重放：相同键 + 相同内容 ----------------

def test_retry_same_key_same_content_replays_original(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes(color=(10, 200, 10))).json()["doc_id"]
    _annotate(client, a, 0)  # a -> rev1

    r1 = _batch(client, [(a, 1), (b, 0)], key="batch-key-001")
    assert r1.status_code == 201, r1.text
    first = r1.json()

    # 首次成功后标注继续变化、甚至发生单份发布：重放不得重新校验/渲染
    _annotate(client, a, 1,
              rects=[{"x": 10, "y": 10, "w": 4, "h": 4, "reason": "新标注"}])
    client.post(f"/documents/{a}/publish")  # a 单份 v2

    r2 = _batch(client, [(a, 1), (b, 0)], key="batch-key-001")
    assert r2.status_code == 200, r2.text  # 重放以 200 区别于首次 201
    assert r2.json() == first  # 原批次标识、各文档版本与摘要完全一致
    assert r2.json()["documents"][0]["version"] == 1  # 仍是原批次固化的 v1

    # 没有产生任何新版本/新批次
    assert _count(client, "publications", "WHERE doc_id=?", (a,)) == 2  # v1 批次 + v2 单份
    assert _count(client, "publications", "WHERE doc_id=?", (b,)) == 1
    assert _count(client, "publication_batches") == 1
    assert _count(client, "batch_idempotency") == 1

    # 重放响应中的摘要与公开读取到的成品一致（成品未被重渲染覆盖）
    item_a = first["documents"][0]
    img = client.get(f"/publications/{a}/{item_a['version']}/image").content
    assert hashlib.sha256(img).hexdigest() == item_a["output_sha256"]

    # 多次重放幂等
    r3 = _batch(client, [(a, 1), (b, 0)], key="batch-key-001")
    assert r3.status_code == 200 and r3.json() == first


def test_replay_binds_ordered_doc_ids_and_revisions(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    r1 = _batch(client, [(a, 0), (b, 0)], key="k-order")
    assert r1.status_code == 201

    # 顺序不同 => 内容不同 => 冲突（键绑定按顺序排列的文档 ID 与修订号）
    r = _batch(client, [(b, 0), (a, 0)], key="k-order")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "idempotency_key_conflict"
    # 修订号不同 => 冲突
    r = _batch(client, [(a, 0), (b, 5)], key="k-order")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "idempotency_key_conflict"
    # 文档集合不同 => 冲突
    c = upload(client, png_bytes()).json()["doc_id"]
    r = _batch(client, [(a, 0), (c, 0)], key="k-order")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "idempotency_key_conflict"


# ---------------- 冲突：相同键 + 不同内容 ----------------

def test_same_key_different_content_conflicts_and_original_intact(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    first = _batch(client, [(a, 0), (b, 0)], key="k-conflict").json()

    r = _batch(client, [(a, 0), (b, 9)], key="k-conflict")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "idempotency_key_conflict"

    # 原批次不受影响：批次查询与公开读取保持原样
    got = client.get(f"/publication-batches/{first['batch_id']}")
    assert got.status_code == 200
    assert [d["doc_id"] for d in got.json()["documents"]] == [a, b]
    assert _count(client, "publications") == 2
    assert _count(client, "publication_batches") == 1
    # 冲突请求自身没有留下任何版本
    assert _count(client, "batch_idempotency") == 1


# ---------------- 失败不占键：可用原键重试 ----------------

def test_stale_revision_failure_does_not_occupy_key(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    _annotate(client, a, 0)  # a 当前 rev1

    # 携带过期修订号：整批 409，键不得被占用
    r = _batch(client, [(a, 0), (b, 0)], key="k-retry")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "stale_revision"
    assert _count(client, "batch_idempotency") == 0
    assert _count(client, "publications") == 0

    # 修正修订号后以原键重试：作为首次请求成功（内容不同也不构成冲突）
    r = _batch(client, [(a, 1), (b, 0)], key="k-retry")
    assert r.status_code == 201, r.text
    assert [d["version"] for d in r.json()["documents"]] == [1, 1]
    assert _count(client, "batch_idempotency") == 1


def test_invalid_request_does_not_occupy_key(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    # 数量不合法（1 份）携带键 -> 400，不占键
    r = _batch(client, [(a, 0)], key="k-invalid")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_batch_size"
    # 重复文档携带键 -> 400，不占键
    r = _batch(client, [(a, 0), (a, 0)], key="k-invalid")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "duplicate_document_in_batch"
    assert _count(client, "batch_idempotency") == 0

    r = _batch(client, [(a, 0), (b, 0)], key="k-invalid")
    assert r.status_code == 201


def test_original_unavailable_does_not_occupy_key(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    settings, db = _db(client)
    path_b = db.execute(
        "SELECT original_path FROM documents WHERE id=?", (b,)).fetchone()[0]
    db.close()
    os.unlink(path_b)  # b 原图不可读

    r = _batch(client, [(a, 0), (b, 0)], key="k-io")
    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "original_unavailable"
    assert _count(client, "batch_idempotency") == 0
    assert _count(client, "publications") == 0

    # 修复原图后以原键重试：成功且版本从 1 开始
    Image.new("RGB", (40, 30), (10, 200, 10)).save(path_b, "PNG")
    r = _batch(client, [(a, 0), (b, 0)], key="k-io")
    assert r.status_code == 201, r.text
    assert [d["version"] for d in r.json()["documents"]] == [1, 1]
    assert not os.path.exists(os.path.join(settings.publications_dir, a, "v2.png"))


def test_artifact_write_failure_does_not_occupy_key(client, monkeypatch):
    from PIL import Image as PILImage

    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]

    real_save = PILImage.Image.save
    calls = {"n": 0}

    def flaky_save(self, fp, *args, **kwargs):
        if isinstance(fp, str) and "/v" in fp and fp.endswith(".png"):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("simulated disk failure")
        return real_save(self, fp, *args, **kwargs)

    monkeypatch.setattr(PILImage.Image, "save", flaky_save)
    r = _batch(client, [(a, 0), (b, 0)], key="k-disk")
    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "artifact_write_failed"
    assert _count(client, "batch_idempotency") == 0
    assert _count(client, "publications") == 0

    monkeypatch.undo()  # 恢复落盘
    r = _batch(client, [(a, 0), (b, 0)], key="k-disk")
    assert r.status_code == 201, r.text
    assert [d["version"] for d in r.json()["documents"]] == [1, 1]


# ---------------- 并发：相同键重试最多发布一次 ----------------

def test_concurrent_same_key_retries_publish_at_most_once(client):
    ids = [upload(client, png_bytes(size=(50, 50))).json()["doc_id"]
           for _ in range(3)]
    pairs = [(d, 0) for d in ids]
    results = []

    def retry():
        r = _batch(client, pairs, key="k-concurrent")
        results.append((r.status_code, r.json()))

    threads = [threading.Thread(target=retry) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 恰好一个首次成功（201），其余为重放（200），批次标识全部一致
    assert sorted(s for s, _ in results) == [200] * 7 + [201]
    batch_ids = {body["batch_id"] for _, body in results}
    assert len(batch_ids) == 1
    for _, body in results:
        assert body["documents"] == results[0][1]["documents"]

    # 只发布了一次：每份文档各 1 个版本，仅 1 个批次、1 条键记录
    for d in ids:
        assert _count(client, "publications", "WHERE doc_id=?", (d,)) == 1
    assert _count(client, "publication_batches") == 1
    assert _count(client, "batch_idempotency") == 1


# ---------------- 兼容性：未带键请求行为不变 ----------------

def test_requests_without_key_publish_each_time(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]

    r1 = _batch(client, [(a, 0), (b, 0)])
    r2 = _batch(client, [(a, 0), (b, 0)])  # 未带键：每次独立发布
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["batch_id"] != r2.json()["batch_id"]
    assert [d["version"] for d in r1.json()["documents"]] == [1, 1]
    assert [d["version"] for d in r2.json()["documents"]] == [2, 2]
    assert _count(client, "batch_idempotency") == 0


def test_keyed_and_unkeyed_batches_share_version_sequence(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]

    r1 = _batch(client, [(a, 0), (b, 0)], key="k-mix")
    assert [d["version"] for d in r1.json()["documents"]] == [1, 1]
    r2 = _batch(client, [(a, 0), (b, 0)])  # 无键：续号
    assert [d["version"] for d in r2.json()["documents"]] == [2, 2]
    # 有键重放：仍返回 v1 批次的原结果
    r3 = _batch(client, [(a, 0), (b, 0)], key="k-mix")
    assert r3.status_code == 200
    assert r3.json() == r1.json()

    # 带键批次与既有公开读取契约一致
    batch_id = r1.json()["batch_id"]
    got = client.get(f"/publication-batches/{batch_id}")
    assert got.status_code == 200
    assert [d["doc_id"] for d in got.json()["documents"]] == [a, b]
    meta = client.get(f"/publications/{a}/1").json()
    assert meta["output_sha256"] == r1.json()["documents"][0]["output_sha256"]
