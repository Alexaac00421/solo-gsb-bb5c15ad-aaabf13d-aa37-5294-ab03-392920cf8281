"""版本撤回测试：撤回/查询、公开读取 410、幂等重复、列表标识、并发一致性。"""
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


def _annotate(client, doc_id, revision, rects=None):
    rects = rects if rects is not None else [
        {"x": 1, "y": 1, "w": 5, "h": 5, "reason": "敏感"}
    ]
    return client.put(
        f"/documents/{doc_id}/annotations",
        json={"revision": revision, "rects": rects},
    )


def _publish(client, doc_id):
    r = client.post(f"/documents/{doc_id}/publish")
    assert r.status_code == 201
    return r.json()


def _withdraw(client, doc_id, version, reason="遮盖遗漏：右下角工号未遮盖"):
    return client.post(
        f"/documents/{doc_id}/publications/{version}/withdraw",
        json={"reason": reason},
    )


def _db(client):
    settings = client.app.state.settings
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return settings, conn


def _withdrawal_count(client):
    _, db = _db(client)
    n = db.execute("SELECT COUNT(*) c FROM publication_withdrawals").fetchone()["c"]
    db.close()
    return n


# ---------------- 撤回成功与公开面封锁 ----------------

def test_withdraw_success_and_public_reads_blocked(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _annotate(client, doc_id, 0)
    pub = _publish(client, doc_id)

    # 撤回前公开读取正常
    assert client.get(f"/publications/{doc_id}/1").status_code == 200
    assert client.get(f"/publications/{doc_id}/1/image").status_code == 200

    r = _withdraw(client, doc_id, 1)
    assert r.status_code == 201
    body = r.json()
    assert body["doc_id"] == doc_id
    assert body["version"] == 1
    assert body["withdrawn"] is True
    assert body["withdrawn_at"]
    assert body["withdrawal_reason"] == "遮盖遗漏：右下角工号未遮盖"

    # 撤回后新发起的公开元数据与图片读取均返回已撤回错误，
    # 不返回成品和遮盖详情
    for path in (f"/publications/{doc_id}/1", f"/publications/{doc_id}/1/image"):
        r = client.get(path)
        assert r.status_code == 410
        detail = r.json()["detail"]
        assert detail["code"] == "publication_withdrawn"
        assert "redactions" not in r.json()
        assert "output_sha256" not in detail

    # 成品文件保留在磁盘上：撤回生效前已开始的下载允许完成
    settings, db = _db(client)
    row = db.execute(
        "SELECT output_path FROM publications WHERE doc_id=? AND version=1",
        (doc_id,),
    ).fetchone()
    db.close()
    assert os.path.exists(row["output_path"])
    assert hashlib.sha256(open(row["output_path"], "rb").read()).hexdigest() \
        == pub["output_sha256"]


def test_withdraw_unknown_document_and_version(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _publish(client, doc_id)

    r = _withdraw(client, "0" * 32, 1)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "document_not_found"

    r = _withdraw(client, doc_id, 99)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "unknown_version"

    # 失败不得留下部分撤回记录
    assert _withdrawal_count(client) == 0
    assert client.get(f"/publications/{doc_id}/1").status_code == 200


def test_withdraw_empty_reason_rejected(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _publish(client, doc_id)

    for bad in ("", "   "):
        r = _withdraw(client, doc_id, 1, reason=bad)
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "reason_required"

    # 空原因被拒绝后版本仍可公开读取，且无撤回记录
    assert _withdrawal_count(client) == 0
    assert client.get(f"/publications/{doc_id}/1").status_code == 200


# ---------------- 幂等：重复撤回返回首次记录 ----------------

def test_repeat_withdraw_returns_first_record(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _publish(client, doc_id)

    first = _withdraw(client, doc_id, 1, reason="首次原因").json()

    # 重复撤回（即使携带不同原因）返回首次记录，不改写原因
    r = _withdraw(client, doc_id, 1, reason="另一条原因不应生效")
    assert r.status_code == 200  # 区别于首次的 201
    assert r.json() == first
    assert r.json()["withdrawal_reason"] == "首次原因"

    r = _withdraw(client, doc_id, 1)
    assert r.status_code == 200 and r.json() == first
    assert _withdrawal_count(client) == 1


def test_get_withdrawal_endpoint(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _publish(client, doc_id)

    # 未知文档/版本按现有不存在错误返回
    r = client.get(f"/documents/{'0' * 32}/publications/1/withdrawal")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "document_not_found"
    r = client.get(f"/documents/{doc_id}/publications/99/withdrawal")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "unknown_version"

    # 未撤回版本
    r = client.get(f"/documents/{doc_id}/publications/1/withdrawal")
    assert r.status_code == 200
    assert r.json() == {
        "doc_id": doc_id,
        "version": 1,
        "withdrawn": False,
        "withdrawn_at": None,
        "withdrawal_reason": None,
    }

    # 撤回后可查询撤回时间与原因
    done = _withdraw(client, doc_id, 1, reason="查询用原因").json()
    r = client.get(f"/documents/{doc_id}/publications/1/withdrawal")
    assert r.status_code == 200
    assert r.json() == done


# ---------------- 列表与详情标识撤回状态（原字段及顺序保留） ----------------

def test_document_detail_and_publication_list_mark_withdrawal(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _annotate(client, doc_id, 0)
    _publish(client, doc_id)                       # v1
    _annotate(client, doc_id, 1, rects=[])
    _publish(client, doc_id)                       # v2
    first = _withdraw(client, doc_id, 1).json()

    # 文档详情：发布列表保留原字段及顺序，并标识撤回状态
    pubs = client.get(f"/documents/{doc_id}").json()["publications"]
    assert [p["version"] for p in pubs] == [1, 2]
    assert list(pubs[0].keys())[:4] == [
        "version", "annotation_revision", "output_sha256", "created_at"]
    assert pubs[0]["withdrawn"] is True
    assert pubs[0]["withdrawn_at"] == first["withdrawn_at"]
    assert pubs[0]["withdrawal_reason"] == first["withdrawal_reason"]
    assert pubs[1]["withdrawn"] is False
    assert pubs[1]["withdrawn_at"] is None
    assert pubs[1]["withdrawal_reason"] is None

    # 发布列表：同样保留原字段及顺序并标识撤回状态
    pubs = client.get(f"/documents/{doc_id}/publications").json()["publications"]
    assert list(pubs[0].keys())[:5] == [
        "version", "annotation_revision", "original_sha256",
        "output_sha256", "created_at"]
    assert [p["withdrawn"] for p in pubs] == [True, False]
    assert pubs[0]["withdrawal_reason"] == first["withdrawal_reason"]


def test_withdrawal_does_not_delete_record_or_reuse_version(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _publish(client, doc_id)                       # v1
    _withdraw(client, doc_id, 1)

    # 撤回不删除发布记录：列表仍包含 v1；后续发布续号而不复用 v1
    p2 = _publish(client, doc_id)
    assert p2["version"] == 2
    pubs = client.get(f"/documents/{doc_id}/publications").json()["publications"]
    assert [p["version"] for p in pubs] == [1, 2]
    assert [p["withdrawn"] for p in pubs] == [True, False]

    # 未撤回的后续版本仍按原规则公开可读
    assert client.get(f"/publications/{doc_id}/2").status_code == 200
    assert client.get(f"/publications/{doc_id}/2/image").status_code == 200
    assert client.get(f"/publications/{doc_id}/1").status_code == 410


# ---------------- 批次场景：批次查询标识、幂等重放不恢复公开访问 ----------------

def test_batch_query_marks_withdrawal_and_replay_cannot_restore(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes(color=(10, 200, 10))).json()["doc_id"]
    body = {
        "idempotency_key": "k-withdraw",
        "documents": [
            {"doc_id": a, "expected_revision": 0},
            {"doc_id": b, "expected_revision": 0},
        ],
    }
    r1 = client.post("/publication-batches", json=body)
    assert r1.status_code == 201
    first = r1.json()
    batch_id = first["batch_id"]

    _withdraw(client, a, 1, reason="批次内发现遮盖遗漏")

    # 批次查询保留原字段及顺序，并标识撤回状态
    got = client.get(f"/publication-batches/{batch_id}")
    assert got.status_code == 200
    docs = got.json()["documents"]
    assert [d["doc_id"] for d in docs] == [a, b]
    assert list(docs[0].keys())[:6] == [
        "position", "doc_id", "version", "annotation_revision",
        "original_sha256", "output_sha256"]
    assert [d["withdrawn"] for d in docs] == [True, False]
    assert docs[0]["withdrawal_reason"] == "批次内发现遮盖遗漏"
    assert docs[1]["withdrawn_at"] is None

    # 批次幂等键重放继续返回首次保存的响应（不含撤回字段）
    r2 = client.post("/publication-batches", json=body)
    assert r2.status_code == 200
    assert r2.json() == first

    # 但重放不能恢复撤回版本的公开访问；未撤回版本不受影响
    assert client.get(f"/publications/{a}/1").status_code == 410
    assert client.get(f"/publications/{a}/1/image").status_code == 410
    assert client.get(f"/publications/{b}/1").status_code == 200
    assert client.get(f"/publications/{b}/1/image").status_code == 200


# ---------------- 并发：撤回与发布状态一致 ----------------

def test_concurrent_withdraw_same_version_only_one_creates(client):
    doc_id = upload(client, png_bytes(size=(50, 50))).json()["doc_id"]
    _publish(client, doc_id)
    results = []

    def withdrawer(k):
        r = _withdraw(client, doc_id, 1, reason=f"原因-{k}")
        results.append((r.status_code, r.json()))

    threads = [threading.Thread(target=withdrawer, args=(k,)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 恰好一个首次撤回（201），其余返回首次记录（200），内容全部一致
    assert sorted(s for s, _ in results) == [200] * 7 + [201]
    bodies = {str(b) for _, b in results}
    assert len(bodies) == 1
    assert _withdrawal_count(client) == 1
    assert client.get(f"/publications/{doc_id}/1").status_code == 410


def test_concurrent_withdraw_and_publish_consistency(client):
    doc_id = upload(client, png_bytes(size=(60, 60))).json()["doc_id"]
    _publish(client, doc_id)  # v1：撤回目标
    errors = []

    def withdrawer():
        for _ in range(6):
            r = _withdraw(client, doc_id, 1)
            if r.status_code not in (200, 201):
                errors.append(f"withdraw: {r.status_code} {r.text}")

    def publisher():
        for _ in range(6):
            r = client.post(f"/documents/{doc_id}/publish")
            if r.status_code != 201:
                errors.append(f"publish: {r.status_code} {r.text}")

    threads = ([threading.Thread(target=withdrawer) for _ in range(2)]
               + [threading.Thread(target=publisher) for _ in range(2)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    # 状态一致：v1 恰好一条撤回记录；版本号连续（撤回未删除、未复用）
    assert _withdrawal_count(client) == 1
    _, db = _db(client)
    versions = [r["version"] for r in db.execute(
        "SELECT version FROM publications WHERE doc_id=? ORDER BY version",
        (doc_id,))]
    db.close()
    assert versions == list(range(1, 13 + 1))

    # v1 已撤回；后续版本公开可读
    assert client.get(f"/publications/{doc_id}/1").status_code == 410
    for v in (2, 13):
        assert client.get(f"/publications/{doc_id}/{v}").status_code == 200
    pubs = client.get(f"/documents/{doc_id}/publications").json()["publications"]
    assert [p["withdrawn"] for p in pubs] == [True] + [False] * 12
