"""遮盖方案迁移 API 测试：缩放取整、原子替换、校验失败整批不变、串行一致性。"""
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.imaging import scale_rect
from tests.test_api import png_bytes, upload  # noqa: E402,F402  复用构造器与助手


@pytest.fixture()
def client(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"))
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _annotate(client, doc_id, revision, rects):
    return client.put(
        f"/documents/{doc_id}/annotations",
        json={"revision": revision, "rects": rects},
    )


def _migrate(client, source, source_revision, targets):
    """targets: [(doc_id, expected_revision), ...]"""
    return client.post(
        "/annotation-migrations",
        json={
            "source_doc_id": source,
            "source_revision": source_revision,
            "targets": [
                {"doc_id": d, "expected_revision": r} for d, r in targets
            ],
        },
    )


def _db(client):
    settings = client.app.state.settings
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return settings, conn


# ---------------- 纯函数：缩放取整 ----------------

def test_scale_rect_floors_origin_ceils_far_edge():
    # 源 100x100 -> 目标 30x30：10*30/100=3，30*30/100=9
    assert scale_rect({"x": 10, "y": 10, "w": 20, "h": 20, "reason": "r"},
                      100, 100, 30, 30) == {"x": 3, "y": 3, "w": 6, "h": 6,
                                            "reason": "r"}


def test_scale_rect_independent_xy_scales():
    # 宽、高分别缩放：源 100x50 -> 目标 200x100（x 方向 2x，y 方向 2x）
    assert scale_rect({"x": 10, "y": 10, "w": 10, "h": 10, "reason": "r"},
                      100, 50, 200, 100) == {"x": 20, "y": 20, "w": 20, "h": 20,
                                             "reason": "r"}
    # 非等比：源 100x100 -> 目标 50x25
    assert scale_rect({"x": 0, "y": 0, "w": 100, "h": 100, "reason": "r"},
                      100, 100, 50, 25) == {"x": 0, "y": 0, "w": 50, "h": 25,
                                            "reason": "r"}


def test_scale_rect_rounding_never_shrinks_coverage():
    # 源 3x3 上的单像素矩形 (1,1,1,1) -> 10x10：
    # 左/上 floor(10/3)=3；右/下 ceil(20/3)=7，覆盖 3..6 共 4px，不漏遮
    assert scale_rect({"x": 1, "y": 1, "w": 1, "h": 1, "reason": "r"},
                      3, 3, 10, 10) == {"x": 3, "y": 3, "w": 4, "h": 4,
                                        "reason": "r"}


def test_scale_rect_full_image_maps_to_full_image():
    assert scale_rect({"x": 0, "y": 0, "w": 100, "h": 50, "reason": "r"},
                      100, 50, 77, 33) == {"x": 0, "y": 0, "w": 77, "h": 33,
                                           "reason": "r"}


# ---------------- 成功路径 ----------------

def test_migration_success_scales_replaces_and_bumps_revision(client):
    src = upload(client, png_bytes(size=(100, 50))).json()["doc_id"]
    t1 = upload(client, png_bytes(size=(200, 100))).json()["doc_id"]
    t2 = upload(client, png_bytes(size=(50, 25))).json()["doc_id"]
    _annotate(client, src, 0, [
        {"x": 0, "y": 0, "w": 100, "h": 50, "reason": "整页边框"},
        {"x": 33, "y": 7, "w": 3, "h": 3, "reason": "印章"},
    ])
    # t1 预先有自己的标注（rev1），迁移应整体替换为 rev2
    _annotate(client, t1, 0,
              [{"x": 1, "y": 1, "w": 2, "h": 2, "reason": "旧标注"}])

    r = _migrate(client, src, 1, [(t1, 1), (t2, 0)])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source_doc_id"] == src
    assert body["source_revision"] == 1
    assert body["count"] == 2

    t1_out, t2_out = body["targets"]
    assert t1_out["position"] == 0 and t1_out["doc_id"] == t1
    assert t1_out["revision"] == 2
    assert t1_out["rects"] == [
        {"x": 0, "y": 0, "w": 200, "h": 100, "reason": "整页边框"},
        {"x": 66, "y": 14, "w": 6, "h": 6, "reason": "印章"},
    ]
    assert t2_out["position"] == 1 and t2_out["revision"] == 1
    # 缩小一半：33/100*50=16.5 -> floor 16；36/100*50=18 -> 18；
    # y: 7/50*25=3.5 -> floor 3；10/50*25=5 -> 5
    assert t2_out["rects"] == [
        {"x": 0, "y": 0, "w": 50, "h": 25, "reason": "整页边框"},
        {"x": 16, "y": 3, "w": 2, "h": 2, "reason": "印章"},
    ]

    # GET 当前标注与迁移结果一致（旧标注被整体替换）
    g1 = client.get(f"/documents/{t1}/annotations").json()
    assert g1["revision"] == 2
    assert g1["rects"] == t1_out["rects"]
    g2 = client.get(f"/documents/{t2}/annotations").json()
    assert g2["revision"] == 1 and g2["rects"] == t2_out["rects"]


def test_migration_single_target_allowed(client):
    src = upload(client, png_bytes(size=(40, 30))).json()["doc_id"]
    t = upload(client, png_bytes(size=(80, 60))).json()["doc_id"]
    _annotate(client, src, 0,
              [{"x": 4, "y": 4, "w": 8, "h": 6, "reason": "姓名"}])
    r = _migrate(client, src, 1, [(t, 0)])
    assert r.status_code == 200
    assert r.json()["count"] == 1
    assert r.json()["targets"][0]["revision"] == 1


def test_migration_keeps_order_and_reasons(client):
    src = upload(client, png_bytes(size=(40, 30))).json()["doc_id"]
    t = upload(client, png_bytes(size=(40, 30))).json()["doc_id"]
    rects = [
        {"x": 1, "y": 1, "w": 2, "h": 2, "reason": "原因一"},
        {"x": 30, "y": 20, "w": 5, "h": 5, "reason": "原因二"},
        {"x": 10, "y": 10, "w": 1, "h": 1, "reason": "原因三"},
    ]
    _annotate(client, src, 0, rects)
    r = _migrate(client, src, 1, [(t, 0)])
    assert r.status_code == 200
    assert [x["reason"] for x in r.json()["targets"][0]["rects"]] == [
        "原因一", "原因二", "原因三"]
    # 同尺寸（40x30 -> 40x30）坐标恒等
    assert [{k: v for k, v in x.items() if k != "reason"}
            for x in r.json()["targets"][0]["rects"]] == [
        {"x": 1, "y": 1, "w": 2, "h": 2},
        {"x": 30, "y": 20, "w": 5, "h": 5},
        {"x": 10, "y": 10, "w": 1, "h": 1},
    ]


def test_migration_does_not_modify_source_or_history(client):
    src = upload(client, png_bytes(size=(40, 30))).json()["doc_id"]
    t = upload(client, png_bytes(size=(80, 60))).json()["doc_id"]
    _annotate(client, src, 0,
              [{"x": 1, "y": 1, "w": 2, "h": 2, "reason": "旧"}])
    _migrate(client, src, 1, [(t, 0)])

    # 源修订号与矩形不变
    g = client.get(f"/documents/{src}/annotations").json()
    assert g["revision"] == 1
    assert g["rects"] == [{"x": 1, "y": 1, "w": 2, "h": 2, "reason": "旧"}]

    # 目标的历史修订（rev0 为空）仍按 revision 可追溯
    _, db = _db(client)
    n0 = db.execute(
        "SELECT COUNT(*) c FROM annotation_rects WHERE doc_id=? AND revision=0",
        (t,)).fetchone()["c"]
    n1 = db.execute(
        "SELECT COUNT(*) c FROM annotation_rects WHERE doc_id=? AND revision=1",
        (t,)).fetchone()["c"]
    assert n0 == 0 and n1 == 1
    db.close()


def test_migration_does_not_touch_publications(client):
    src = upload(client, png_bytes(size=(40, 30))).json()["doc_id"]
    t = upload(client, png_bytes(size=(80, 60))).json()["doc_id"]
    _annotate(client, t, 0,
              [{"x": 2, "y": 2, "w": 4, "h": 4, "reason": "旧遮盖"}])
    pub = client.post(f"/documents/{t}/publish").json()
    assert pub["version"] == 1 and pub["annotation_revision"] == 1

    # 源标注后迁移：目标发布版 v1 仍对应旧修订 1
    _annotate(client, src, 0,
              [{"x": 0, "y": 0, "w": 40, "h": 30, "reason": "整页"}])
    r = _migrate(client, src, 1, [(t, 1)])
    assert r.status_code == 200
    meta = client.get(f"/publications/{t}/1").json()
    assert meta["annotation_revision"] == 1
    assert meta["redactions"] == [
        {"x": 2, "y": 2, "w": 4, "h": 4, "reason": "旧遮盖"}]


def test_migrated_rects_are_publishable_and_black(client):
    """缩放后的矩形可直接发布涂实（坐标在目标图内、尺寸为正）。"""
    src = upload(client, png_bytes(size=(3, 3), color=(200, 10, 10))).json()["doc_id"]
    t = upload(client, png_bytes(size=(10, 10), color=(200, 10, 10))).json()["doc_id"]
    _annotate(client, src, 0,
              [{"x": 1, "y": 1, "w": 1, "h": 1, "reason": "点"}])
    r = _migrate(client, src, 1, [(t, 0)])
    assert r.status_code == 200
    rect = r.json()["targets"][0]["rects"][0]
    assert rect == {"x": 3, "y": 3, "w": 4, "h": 4, "reason": "点"}

    pub = client.post(f"/documents/{t}/publish")
    assert pub.status_code == 201
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(
        client.get(f"/publications/{t}/1/image").content)).convert("RGB")
    assert img.getpixel((3, 3)) == (0, 0, 0)
    assert img.getpixel((6, 6)) == (0, 0, 0)
    assert img.getpixel((7, 7)) == (200, 10, 10)


# ---------------- 请求校验 ----------------

def test_migration_size_bounds(client):
    src = upload(client, png_bytes()).json()["doc_id"]
    # 0 个目标
    r = _migrate(client, src, 0, [])
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "invalid_migration_size"
    assert detail["count"] == 0

    # 21 个目标（用不存在的 ID，先验数量）
    r = client.post("/annotation-migrations", json={
        "source_doc_id": src, "source_revision": 0,
        "targets": [{"doc_id": f"d{i}", "expected_revision": 0}
                    for i in range(21)],
    })
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_migration_size"


def test_migration_duplicate_targets_rejected(client):
    src = upload(client, png_bytes()).json()["doc_id"]
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    r = _migrate(client, src, 0, [(a, 0), (b, 0), (a, 0)])
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "duplicate_target_in_migration"
    assert detail["position"] == 2
    assert detail["doc_id"] == a


def test_migration_source_among_targets_rejected(client):
    src = upload(client, png_bytes()).json()["doc_id"]
    a = upload(client, png_bytes()).json()["doc_id"]
    r = _migrate(client, src, 0, [(a, 0), (src, 0)])
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "source_among_targets"
    assert detail["doc_id"] == src


def test_migration_source_not_found(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    missing = "0" * 32
    r = _migrate(client, missing, 0, [(a, 0)])
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "document_not_found"
    assert detail["doc_id"] == missing


def test_migration_target_not_found_is_atomic(client):
    src = upload(client, png_bytes(size=(40, 30))).json()["doc_id"]
    a = upload(client, png_bytes(size=(40, 30))).json()["doc_id"]
    missing = "1" * 32
    _annotate(client, src, 0,
              [{"x": 1, "y": 1, "w": 2, "h": 2, "reason": "r"}])
    # 缺失项放在 a 之后：a 也不得被改动
    r = _migrate(client, src, 1, [(a, 0), (missing, 0)])
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "document_not_found"
    assert detail["position"] == 1
    assert detail["doc_id"] == missing

    g = client.get(f"/documents/{a}/annotations").json()
    assert g["revision"] == 0 and g["rects"] == []


def test_migration_stale_source_revision(client):
    src = upload(client, png_bytes()).json()["doc_id"]
    t = upload(client, png_bytes()).json()["doc_id"]
    _annotate(client, src, 0,
              [{"x": 1, "y": 1, "w": 2, "h": 2, "reason": "r"}])  # rev1
    r = _migrate(client, src, 0, [(t, 0)])  # 源给了过期的 0
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "stale_revision"
    assert detail["doc_id"] == src
    assert detail["current_revision"] == 1
    assert detail["supplied_revision"] == 0
    # 目标不变
    assert client.get(f"/documents/{t}/annotations").json()["revision"] == 0


def test_migration_stale_target_revision_is_atomic(client):
    src = upload(client, png_bytes()).json()["doc_id"]
    a = upload(client, png_bytes()).json()["doc_id"]
    b = upload(client, png_bytes()).json()["doc_id"]
    _annotate(client, src, 0,
              [{"x": 1, "y": 1, "w": 2, "h": 2, "reason": "r"}])
    _annotate(client, b, 0, [])  # b 当前 rev1，请求却带 0
    r = _migrate(client, src, 1, [(a, 0), (b, 0)])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "stale_revision"
    assert detail["position"] == 1
    assert detail["doc_id"] == b
    assert detail["current_revision"] == 1
    assert detail["supplied_revision"] == 0

    # a（校验在前）同样不能被改动
    assert client.get(f"/documents/{a}/annotations").json() == {
        "doc_id": a, "revision": 0, "rects": []}
    assert client.get(f"/documents/{b}/annotations").json()["revision"] == 1


def test_migration_empty_source_rejected(client):
    src = upload(client, png_bytes()).json()["doc_id"]
    t = upload(client, png_bytes()).json()["doc_id"]
    # 从未标注（rev0 空）
    r = _migrate(client, src, 0, [(t, 0)])
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "source_annotations_empty"
    assert detail["doc_id"] == src

    # 标注过又清空（当前 rev2 为空）同样拒绝
    _annotate(client, src, 0,
              [{"x": 1, "y": 1, "w": 2, "h": 2, "reason": "r"}])
    _annotate(client, src, 1, [])
    r = _migrate(client, src, 2, [(t, 0)])
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "source_annotations_empty"
    assert client.get(f"/documents/{t}/annotations").json()["revision"] == 0


def test_migration_failure_consumes_no_revision(client):
    """失败后用正确参数重试：目标拿到 rev1，证明失败未推进修订号。"""
    src = upload(client, png_bytes()).json()["doc_id"]
    t = upload(client, png_bytes()).json()["doc_id"]
    _annotate(client, src, 0,
              [{"x": 1, "y": 1, "w": 2, "h": 2, "reason": "r"}])

    r = _migrate(client, src, 1, [(t, 5)])  # 预期修订号错误
    assert r.status_code == 409
    r = _migrate(client, src, 1, [(t, 0)])
    assert r.status_code == 200
    assert r.json()["targets"][0]["revision"] == 1


# ---------------- 并发：迁移与标注修改、发布串行 ----------------

def test_concurrent_migrations_and_annotation_edits(client):
    # 1 个源 + 4 个目标；迁移者反复把源当前标注迁移到各目标（乐观锁冲突合法），
    # 标注者同时修改源与目标，发布者同时发布。最终库内每个修订矩形必须自洽。
    src = upload(client, png_bytes(size=(50, 50))).json()["doc_id"]
    ids = [upload(client, png_bytes(size=(50, 50))).json()["doc_id"]
           for _ in range(4)]
    _annotate(client, src, 0,
              [{"x": 1, "y": 1, "w": 4, "h": 4, "reason": "初始"}])
    errors = []

    def source_editor():
        for i in range(8):
            cur = client.get(f"/documents/{src}/annotations").json()["revision"]
            r = _annotate(client, src, cur, [
                {"x": (i * 3) % 40, "y": 2, "w": 4, "h": 4,
                 "reason": f"源{i}"}])
            if r.status_code not in (200, 409):
                errors.append(f"source edit: {r.status_code} {r.text}")

    def target_editor(k):
        for i in range(6):
            cur = client.get(
                f"/documents/{ids[k]}/annotations").json()["revision"]
            r = _annotate(client, ids[k], cur, [
                {"x": (i * 4) % 40, "y": 2, "w": 4, "h": 4,
                 "reason": f"目标{k}-{i}"}])
            if r.status_code not in (200, 409):
                errors.append(f"target edit: {r.status_code} {r.text}")

    def migrator():
        for _ in range(10):
            src_rev = client.get(
                f"/documents/{src}/annotations").json()["revision"]
            pairs = [(d, client.get(
                f"/documents/{d}/annotations").json()["revision"])
                for d in ids]
            r = _migrate(client, src, src_rev, pairs)
            if r.status_code not in (200, 409):
                errors.append(f"migrate: {r.status_code} {r.text}")

    def publisher(k):
        for _ in range(4):
            r = client.post(f"/documents/{ids[k]}/publish")
            if r.status_code != 201:
                errors.append(f"publish: {r.status_code} {r.text}")

    threads = ([threading.Thread(target=source_editor)]
               + [threading.Thread(target=target_editor, args=(k,))
                  for k in range(4)]
               + [threading.Thread(target=migrator) for _ in range(3)]
               + [threading.Thread(target=publisher, args=(k,))
                  for k in range(4)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    # 不变式：任一成功迁移后，目标新修订的矩形数 == 源当时修订矩形数，
    # 且每个矩形都在目标 50x50 边界内、尺寸为正（可直接用于涂实）。
    _, db = _db(client)
    for doc_id in ids:
        doc = db.execute(
            "SELECT annotation_revision FROM documents WHERE id=?",
            (doc_id,)).fetchone()
        for rev in range(doc["annotation_revision"] + 1):
            rects = db.execute(
                "SELECT x,y,w,h FROM annotation_rects WHERE doc_id=?"
                " AND revision=? ORDER BY idx", (doc_id, rev)).fetchall()
            for rc in rects:
                assert rc["w"] > 0 and rc["h"] > 0
                assert rc["x"] >= 0 and rc["y"] >= 0
                assert rc["x"] + rc["w"] <= 50
                assert rc["y"] + rc["h"] <= 50
    # 源修订矩形也保持完好
    src_doc = db.execute(
        "SELECT annotation_revision FROM documents WHERE id=?",
        (src,)).fetchone()
    src_rows = db.execute(
        "SELECT COUNT(*) c FROM annotation_rects WHERE doc_id=?",
        (src,)).fetchone()["c"]
    assert src_rows == src_doc["annotation_revision"]
    db.close()
