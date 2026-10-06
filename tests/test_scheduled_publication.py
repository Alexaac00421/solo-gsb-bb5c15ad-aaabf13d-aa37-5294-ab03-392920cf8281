"""定时公开（统一 UTC 公开时间）测试。

覆盖：单份/批次的未来时间发布、到点前后公开面 404→可读、版本号在发布当下
即占用、无效时间不留任何痕迹、批次幂等内容纳入公开时间、撤回优先于公开
时间，以及列表/详情返回 publish_at 且既有字段保持兼容。

时钟通过 monkeypatch ``app.main._now_dt`` 注入，避免真实等待。
"""
import io
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image
from fastapi.testclient import TestClient

import app.main as main_mod
from app.config import Settings
from app.main import create_app
from tests.test_api import png_bytes, upload  # noqa: E402,F402  复用构造器与助手


@pytest.fixture()
def client(tmp_path, monkeypatch):
    settings = Settings(data_dir=str(tmp_path / "data"))
    clock = {"now": datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(main_mod, "_now_dt", lambda: clock["now"])
    app = create_app(settings)
    with TestClient(app) as c:
        c.app.state.clock = clock
        yield c


def _advance(client, **delta):
    client.app.state.clock["now"] += timedelta(**delta)


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _annotate(client, doc_id, revision=0, rects=None):
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


# ---------------- 单份：到点前 404、到点后可读 ----------------

def test_scheduled_single_publish_blocks_until_time(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _annotate(client, doc_id, 0)
    target = client.app.state.clock["now"] + timedelta(hours=2)

    r = client.post(
        f"/documents/{doc_id}/publish", json={"publish_at": _iso(target)}
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["publish_at"] == _iso(target)

    # 发布当下即固化成品并占用正常版本号：下次发布拿到 v2
    r2 = client.post(f"/documents/{doc_id}/publish")
    assert r2.status_code == 201
    assert r2.json()["version"] == 2
    assert r2.json()["publish_at"] is None  # 省略时立即公开

    # 到点前：详情与图片均 404，不泄露遮盖信息、摘要或文件
    for path in (f"/publications/{doc_id}/1",
                 f"/publications/{doc_id}/1/image"):
        r = client.get(path)
        assert r.status_code == 404
        detail = r.json()["detail"]
        assert detail["code"] == "publication_not_yet_public"
        assert "redactions" not in r.text
        assert "output_sha256" not in detail

    # 时间到达，无需再次操作即可读取（元数据+成品图）
    _advance(client, hours=2, seconds=1)
    meta = client.get(f"/publications/{doc_id}/1")
    assert meta.status_code == 200
    assert meta.json()["redactions"] == [
        {"x": 1, "y": 1, "w": 5, "h": 5, "reason": "敏感"}]
    assert meta.json()["publish_at"] == _iso(target)
    img = client.get(f"/publications/{doc_id}/1/image")
    assert img.status_code == 200
    assert img.headers["content-type"] == "image/png"
    px = Image.open(io.BytesIO(img.content)).convert("RGB")
    assert px.getpixel((3, 3)) == (0, 0, 0)


def test_scheduled_image_is_immutable_after_release(client):
    # 定时发布的版本固化发布当下的标注，后续标注变化不影响到点后的成品
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _annotate(client, doc_id, 0,
              rects=[{"x": 2, "y": 2, "w": 4, "h": 4, "reason": "旧"}])
    target = client.app.state.clock["now"] + timedelta(minutes=30)
    p = client.post(
        f"/documents/{doc_id}/publish", json={"publish_at": _iso(target)}
    ).json()

    _annotate(client, doc_id, 1,
              rects=[{"x": 30, "y": 20, "w": 6, "h": 6, "reason": "新"}])
    _advance(client, minutes=31)
    img = client.get(f"/publications/{doc_id}/1/image").content
    import hashlib
    assert hashlib.sha256(img).hexdigest() == p["output_sha256"]
    meta = client.get(f"/publications/{doc_id}/1").json()
    assert meta["annotation_revision"] == 1
    assert meta["redactions"] == [
        {"x": 2, "y": 2, "w": 4, "h": 4, "reason": "旧"}]


def test_lists_report_publish_at_with_original_fields(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _annotate(client, doc_id, 0)
    target = client.app.state.clock["now"] + timedelta(hours=1)
    client.post(f"/documents/{doc_id}/publish",
                json={"publish_at": _iso(target)})
    client.post(f"/documents/{doc_id}/publish")  # v2 立即公开

    pubs = client.get(f"/documents/{doc_id}").json()["publications"]
    assert list(pubs[0].keys())[:4] == [
        "version", "annotation_revision", "output_sha256", "created_at"]
    assert pubs[0]["publish_at"] == _iso(target)
    assert pubs[1]["publish_at"] is None
    # 撤回三字段仍在末尾，兼容既有顺序约定
    assert list(pubs[0].keys())[-3:] == [
        "withdrawn", "withdrawn_at", "withdrawal_reason"]

    pubs = client.get(f"/documents/{doc_id}/publications").json()["publications"]
    assert list(pubs[0].keys())[:5] == [
        "version", "annotation_revision", "original_sha256",
        "output_sha256", "created_at"]
    assert [p["publish_at"] for p in pubs] == [_iso(target), None]


# ---------------- 无效时间：400 且不留版本/文件 ----------------

@pytest.mark.parametrize("bad", [
    "2026-10-05T11:59:00Z",          # 过去
    "2026-10-05T12:00:00Z",          # 恰为当前（必须严格晚于）
    "not-a-time",
    "2026-10-05T12:30:00",           # 朴素时间：无时区偏移
    "2026-11-06T12:00:01Z",          # 超过 30 天（晚 32 天）
    "2026-10-05",                    # 仅日期
])
def test_invalid_publish_time_single_rejected(client, bad):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    r = client.post(f"/documents/{doc_id}/publish", json={"publish_at": bad})
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "invalid_publish_time"

    # 不留版本：下次发布仍为 v1；磁盘无成品文件
    ok = client.post(f"/documents/{doc_id}/publish")
    assert ok.status_code == 201 and ok.json()["version"] == 1
    settings, db = _db(client)
    n = db.execute(
        "SELECT COUNT(*) c FROM publications WHERE doc_id=?", (doc_id,)
    ).fetchone()["c"]
    db.close()
    assert n == 1
    assert not os.path.exists(os.path.join(
        settings.publications_dir, doc_id, "v2.png"))


def test_offset_notation_normalized_to_utc(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    # 2026-10-05T15:00:00+03:00 == 12:00:00Z，但当前是 12:00Z -> 恰为当前 -> 拒绝
    r = client.post(
        f"/documents/{doc_id}/publish",
        json={"publish_at": "2026-10-05T15:00:00+03:00"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_publish_time"

    target = client.app.state.clock["now"] + timedelta(hours=3)
    r = client.post(
        f"/documents/{doc_id}/publish",
        json={"publish_at": "2026-10-05T18:00:00+03:00"})
    assert r.status_code == 201
    assert r.json()["publish_at"] == _iso(target)  # 规范化为 UTC


def test_omitted_body_still_publishes_immediately(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    # 完全不带请求体（既有调用方式）
    r = client.post(f"/documents/{doc_id}/publish")
    assert r.status_code == 201
    assert r.json()["publish_at"] is None
    assert client.get(f"/publications/{doc_id}/1").status_code == 200
    # 显式空 JSON 体
    r = client.post(f"/documents/{doc_id}/publish", json={})
    assert r.status_code == 201 and r.json()["version"] == 2
    # 显式 null
    r = client.post(f"/documents/{doc_id}/publish", json={"publish_at": None})
    assert r.status_code == 201 and r.json()["version"] == 3


# ---------------- 批次：统一公开时间 ----------------

def _batch(client, pairs, publish_at=..., key=None):
    body = {"documents": [
        {"doc_id": d, "expected_revision": r} for d, r in pairs]}
    if publish_at is not ...:
        body["publish_at"] = publish_at
    if key is not None:
        body["idempotency_key"] = key
    return client.post("/publication-batches", json=body)


def test_scheduled_batch_blocks_then_releases_all(client):
    a = upload(client, png_bytes(color=(200, 10, 10))).json()["doc_id"]
    b = upload(client, png_bytes(color=(10, 200, 10))).json()["doc_id"]
    _annotate(client, a, 0)
    target = client.app.state.clock["now"] + timedelta(hours=5)

    r = _batch(client, [(a, 1), (b, 0)], publish_at=_iso(target))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["publish_at"] == _iso(target)
    assert [d["version"] for d in body["documents"]] == [1, 1]

    # 批次查询返回公开时间；各条目同样带 publish_at，撤回字段保留
    got = client.get(f"/publication-batches/{body['batch_id']}").json()
    assert got["publish_at"] == _iso(target)
    for d in got["documents"]:
        assert d["publish_at"] == _iso(target)
        assert d["withdrawn"] is False

    # 到点前全部 404
    for d in (a, b):
        assert client.get(f"/publications/{d}/1").status_code == 404
        assert client.get(f"/publications/{d}/1/image").status_code == 404

    _advance(client, hours=5, seconds=1)
    for d in (a, b):
        assert client.get(f"/publications/{d}/1").status_code == 200
        assert client.get(f"/publications/{d}/1/image").status_code == 200


@pytest.mark.parametrize("bad", [
    "2026-10-05T11:00:00Z",       # 过去
    "2026-11-05T12:00:01Z",       # 超过 30 天 1 秒（30 天整点允许）
    "garbage",
    "2026-10-05T12:30:00",        # 朴素时间：无时区偏移
])
def test_batch_invalid_publish_time_leaves_nothing(client, bad):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    r = _batch(client, [(a, 0), (b, 0)], publish_at=bad, key="k-bad-time")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_publish_time"

    settings, db = _db(client)
    assert db.execute("SELECT COUNT(*) c FROM publications").fetchone()["c"] == 0
    assert db.execute(
        "SELECT COUNT(*) c FROM publication_batches").fetchone()["c"] == 0
    assert db.execute(
        "SELECT COUNT(*) c FROM batch_idempotency").fetchone()["c"] == 0
    db.close()
    for d in (a, b):
        assert not os.path.exists(os.path.join(
            settings.publications_dir, d, "v1.png"))

    # 不占版本：修正时间后重试，两份都从 v1 开始
    target = client.app.state.clock["now"] + timedelta(days=1)
    r = _batch(client, [(a, 0), (b, 0)],
               publish_at=_iso(target), key="k-bad-time")
    assert r.status_code == 201
    assert [d["version"] for d in r.json()["documents"]] == [1, 1]


def test_batch_exactly_30_day_boundary_accepted(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    edge = client.app.state.clock["now"] + timedelta(days=30)
    r = _batch(client, [(a, 0), (b, 0)], publish_at=_iso(edge))
    assert r.status_code == 201, r.text  # 恰好 30 天允许
    assert client.get(f"/publications/{a}/1").status_code == 404


def test_scheduled_batch_occupies_normal_versions_and_immediate_continues(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    target = client.app.state.clock["now"] + timedelta(hours=1)

    r = _batch(client, [(a, 0), (b, 0)], publish_at=_iso(target))
    assert r.status_code == 201
    assert [d["version"] for d in r.json()["documents"]] == [1, 1]

    # 定时批次已占用 v1：定时版本到点前，后续立即发布续号为 v2 且立即可读
    nxt = client.post(f"/documents/{a}/publish").json()
    assert nxt["version"] == 2 and nxt["publish_at"] is None
    assert client.get(f"/publications/{a}/2").status_code == 200
    assert client.get(f"/publications/{a}/1").status_code == 404

    # 到点后 v1 也可读，两份成品内容各自不可变
    import hashlib
    _advance(client, hours=1, seconds=1)
    v1 = r.json()["documents"][0]
    img = client.get(f"/publications/{a}/1/image").content
    assert hashlib.sha256(img).hexdigest() == v1["output_sha256"]
    assert client.get(f"/publications/{a}/1").status_code == 200


def test_batch_other_failure_with_schedule_leaves_nothing(client):
    # 定时批次中任一文档失败（修订过期）：不留版本/批次，也不占幂等键
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    _annotate(client, a, 0)  # a 当前 rev1，请求给 0 -> 过期
    target = client.app.state.clock["now"] + timedelta(hours=1)
    r = _batch(client, [(a, 0), (b, 0)],
               publish_at=_iso(target), key="k-stale-scheduled")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "stale_revision"
    _, db = _db(client)
    for table in ("publications", "publication_batches", "batch_idempotency"):
        assert db.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] == 0
    db.close()


# ---------------- 批次幂等：公开时间纳入内容 ----------------

def test_scheduled_batch_replay_after_time_skips_revalidation(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes(color=(10, 200, 10))).json()["doc_id"]
    target = client.app.state.clock["now"] + timedelta(hours=1)

    r1 = _batch(client, [(a, 0), (b, 0)],
                publish_at=_iso(target), key="k-clock")
    assert r1.status_code == 201
    first = r1.json()

    # 到点之后（公开时间已成"过去"）原样重发：不重新校验时间窗口，
    # 直接返回首次结果（HTTP 200，逐字段一致）
    _advance(client, hours=2)
    r2 = _batch(client, [(a, 0), (b, 0)],
                publish_at=_iso(target), key="k-clock")
    assert r2.status_code == 200
    assert r2.json() == first
    assert r2.json()["publish_at"] == _iso(target)
    _, db = _db(client)
    assert db.execute(
        "SELECT COUNT(*) c FROM publication_batches").fetchone()["c"] == 1
    db.close()


def test_same_key_different_publish_time_conflicts(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    t1 = client.app.state.clock["now"] + timedelta(hours=1)
    first = _batch(client, [(a, 0), (b, 0)],
                   publish_at=_iso(t1), key="k-time").json()

    # 同键、文档内容相同，仅公开时间不同 -> 409 冲突
    t2 = client.app.state.clock["now"] + timedelta(hours=2)
    r = _batch(client, [(a, 0), (b, 0)],
               publish_at=_iso(t2), key="k-time")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "idempotency_key_conflict"

    # 一边定时、一边立即（null）也属于内容不同 -> 冲突
    r = _batch(client, [(a, 0), (b, 0)], publish_at=None, key="k-time")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "idempotency_key_conflict"

    # 首次：定时立即发布省略 publish_at 的请求，同键同样冲突
    r = _batch(client, [(a, 0), (b, 0)], key="k-time")
    assert r.status_code == 409

    # 原批次不受影响，仍是首次的公开时间
    got = client.get(f"/publication-batches/{first['batch_id']}").json()
    assert got["publish_at"] == _iso(t1)
    _, db = _db(client)
    assert db.execute("SELECT COUNT(*) c FROM publications").fetchone()["c"] == 2
    db.close()


def test_scheduled_and_immediate_batches_share_version_sequence(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    t1 = client.app.state.clock["now"] + timedelta(hours=1)
    r1 = _batch(client, [(a, 0), (b, 0)], publish_at=_iso(t1))
    assert [d["version"] for d in r1.json()["documents"]] == [1, 1]
    # 未带公开时间的批次立即公开，续号 v2
    r2 = _batch(client, [(a, 0), (b, 0)])
    assert r2.status_code == 201
    assert [d["version"] for d in r2.json()["documents"]] == [2, 2]
    assert client.get(f"/publications/{a}/2").status_code == 200
    assert client.get(f"/publications/{a}/1").status_code == 404


# ---------------- 永久撤回优先于公开时间 ----------------

def test_withdrawal_takes_precedence_over_publish_time(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _annotate(client, doc_id, 0)
    target = client.app.state.clock["now"] + timedelta(hours=3)
    client.post(f"/documents/{doc_id}/publish",
                json={"publish_at": _iso(target)})

    # 公开时间前撤回：到点前本应 404，但撤回优先 -> 410
    r = client.post(
        f"/documents/{doc_id}/publications/1/withdraw",
        json={"reason": "定时版本发现遮盖遗漏"})
    assert r.status_code == 201

    assert client.get(f"/publications/{doc_id}/1").status_code == 410
    assert client.get(f"/publications/{doc_id}/1/image").status_code == 410

    # 时间到达也不得恢复访问
    _advance(client, hours=4)
    assert client.get(f"/publications/{doc_id}/1").status_code == 410
    assert client.get(f"/publications/{doc_id}/1/image").status_code == 410

    # 撤回状态仍在列表/批次式查询中可见；定时 v1 不复用，续发为 v2
    pubs = client.get(f"/documents/{doc_id}/publications").json()["publications"]
    assert pubs[0]["withdrawn"] is True
    assert pubs[0]["publish_at"] == _iso(target)
    nxt = client.post(f"/documents/{doc_id}/publish")
    assert nxt.json()["version"] == 2


def test_batch_scheduled_withdraw_before_release_stays_gone(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes(color=(10, 200, 10))).json()["doc_id"]
    target = client.app.state.clock["now"] + timedelta(hours=2)
    body = {
        "idempotency_key": "k-scheduled-withdraw",
        "publish_at": _iso(target),
        "documents": [
            {"doc_id": a, "expected_revision": 0},
            {"doc_id": b, "expected_revision": 0},
        ],
    }
    first = client.post("/publication-batches", json=body).json()

    client.post(f"/documents/{a}/publications/1/withdraw",
                json={"reason": "批次定时版本遮盖遗漏"})
    assert client.get(f"/publications/{a}/1").status_code == 410

    _advance(client, hours=3)
    assert client.get(f"/publications/{a}/1").status_code == 410
    assert client.get(f"/publications/{a}/1/image").status_code == 410
    # 未撤回的同批文档到点正常可读
    assert client.get(f"/publications/{b}/1").status_code == 200

    # 幂等重放仍原样返回首次响应，但不恢复访问
    r = client.post("/publication-batches", json=body)
    assert r.status_code == 200 and r.json() == first
    assert client.get(f"/publications/{a}/1").status_code == 410
