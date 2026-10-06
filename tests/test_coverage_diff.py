"""重发影像前遮盖范围比较（POST /documents/{id}/coverage-diff）测试。

覆盖：
- 新增/失去像素数与占旋正后图像的比例、零差异；
- 重叠矩形只计一次、边界相接不算重叠；
- 失去遮盖水平段（半开区间）列表与"相邻段区间完全相同才合并"；
- 基准取发布时冻结的标注，后续标注修改不影响比较；
- 拒绝场景：未知文档/版本、未到公开时间、已撤回（不泄露基准遮盖详情）、
  修订号过期；
- 纯只读：不生成版本、不改动标注/撤回记录；
- 同一读取快照：比较进行中并发提交的标注修改不被本次比较看到；
- 纯几何（imaging.coverage_diff）：区间并集/相减/合并的关键性质。
"""
import threading

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.config import Settings
from app.imaging import coverage_diff
from app.main import create_app
from tests.test_api import png_bytes, upload  # noqa: E402,F401  复用构造器与助手


@pytest.fixture()
def client(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"))
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _new_doc(client, color=(10, 20, 30), size=(40, 30), filename="img.png"):
    return upload(client, png_bytes(size=size, color=color), filename).json()["doc_id"]


def _annotate(client, doc_id, revision, rects):
    r = client.put(
        f"/documents/{doc_id}/annotations",
        json={"revision": revision, "rects": rects},
    )
    assert r.status_code == 200, r.text
    return r.json()["revision"]


def _publish(client, doc_id, **kwargs):
    r = client.post(f"/documents/{doc_id}/publish", json=kwargs or None)
    assert r.status_code == 201, r.text
    return r.json()


def _diff(client, doc_id, revision, baseline_version):
    return client.post(
        f"/documents/{doc_id}/coverage-diff",
        json={"revision": revision, "baseline_version": baseline_version},
    )


# ---------------- 基本：新增/失去像素数与比例 ----------------

def test_coverage_diff_added_lost_counts_and_ratios(client):
    # 旋正后 40x30 = 1200 像素
    d = _new_doc(client, size=(40, 30))
    _annotate(client, d, 0, [{"x": 0, "y": 0, "w": 20, "h": 10, "reason": "旧"}])
    p = _publish(client, d)
    # 当前标注右移 10 列：基准覆盖 x[0,20)，当前覆盖 x[10,30)
    _annotate(client, d, 1, [{"x": 10, "y": 0, "w": 20, "h": 10, "reason": "新"}])

    r = _diff(client, d, 2, 1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["doc_id"] == d
    assert body["current_revision"] == 2
    assert body["baseline_version"] == 1
    assert body["baseline_annotation_revision"] == p["annotation_revision"]
    assert body["width"] == 40 and body["height"] == 30
    assert body["total_pixels"] == 1200
    assert body["added_pixels"] == 100      # x[20,30) × 10 行
    assert body["lost_pixels"] == 100       # x[0,10) × 10 行
    assert body["added_ratio"] == pytest.approx(100 / 1200)
    assert body["lost_ratio"] == pytest.approx(100 / 1200)
    # 失去遮盖水平段：y[0,10) 内失去 x[0,10)（半开区间，端点不含）
    assert body["lost_segments"] == [
        {"y0": 0, "y1": 10, "intervals": [{"x0": 0, "x1": 10}]}
    ]


def test_coverage_diff_identical_coverage_is_zero(client):
    d = _new_doc(client, size=(40, 30))
    _annotate(client, d, 0, [{"x": 5, "y": 5, "w": 8, "h": 6, "reason": "甲"}])
    _publish(client, d)
    # 原样再标注一次（修订号变化、遮盖集合不变）后比较 v1
    _annotate(client, d, 1, [{"x": 5, "y": 5, "w": 8, "h": 6, "reason": "甲"}])

    r = _diff(client, d, 2, 1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added_pixels"] == 0
    assert body["lost_pixels"] == 0
    assert body["added_ratio"] == 0.0
    assert body["lost_ratio"] == 0.0
    assert body["lost_segments"] == []


def test_coverage_diff_empty_current_lists_all_baseline(client):
    d = _new_doc(client, size=(40, 30))
    _annotate(client, d, 0, [{"x": 0, "y": 0, "w": 10, "h": 20, "reason": "密"}])
    _publish(client, d)
    _annotate(client, d, 1, [])  # 当前标注清空

    r = _diff(client, d, 2, 1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added_pixels"] == 0
    assert body["lost_pixels"] == 200
    assert body["lost_ratio"] == pytest.approx(200 / 1200)
    assert body["lost_segments"] == [
        {"y0": 0, "y1": 20, "intervals": [{"x0": 0, "x1": 10}]}
    ]


def test_coverage_diff_baseline_without_rects(client):
    # 基准发布时没有任何遮盖：当前全为新增，没有失去段
    d = _new_doc(client, size=(40, 30))
    _publish(client, d)  # v1：无标注
    _annotate(client, d, 0, [{"x": 30, "y": 20, "w": 10, "h": 10, "reason": "新"}])

    r = _diff(client, d, 1, 1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added_pixels"] == 100
    assert body["lost_pixels"] == 0
    assert body["lost_segments"] == []
    assert body["baseline_annotation_revision"] == 0


def test_coverage_diff_multiple_lost_intervals_sorted(client):
    d = _new_doc(client, size=(40, 30))
    _annotate(client, d, 0, [
        {"x": 0, "y": 0, "w": 10, "h": 20, "reason": "左"},
        {"x": 10, "y": 0, "w": 10, "h": 20, "reason": "中"},
        {"x": 20, "y": 0, "w": 10, "h": 20, "reason": "右"},
    ])
    _publish(client, d)
    # 当前只保留中间：左右两块都失去（同一水平段内两个区间，按 x 升序）
    _annotate(client, d, 1, [
        {"x": 10, "y": 0, "w": 10, "h": 20, "reason": "中"},
    ])

    body = _diff(client, d, 2, 1).json()
    assert body["lost_pixels"] == 400
    assert body["added_pixels"] == 0
    assert body["lost_segments"] == [{
        "y0": 0, "y1": 20,
        "intervals": [{"x0": 0, "x1": 10}, {"x0": 20, "x1": 30}],
    }]


# ---------------- 重叠只计一次、边界相接不算重叠 ----------------

def test_coverage_diff_overlapping_rectangles_counted_once(client):
    d = _new_doc(client, size=(40, 30))
    _publish(client, d)  # 空基准
    # 两个当前矩形在 2 列 × 10 行上重叠；并集 = 120 + 100 - 20 = 200
    _annotate(client, d, 0, [
        {"x": 0, "y": 0, "w": 12, "h": 10, "reason": "甲"},
        {"x": 10, "y": 0, "w": 10, "h": 10, "reason": "乙"},
    ])

    body = _diff(client, d, 1, 1).json()
    assert body["added_pixels"] == 200


def test_coverage_diff_nested_rectangles_counted_once(client):
    d = _new_doc(client, size=(40, 30))
    _publish(client, d)
    _annotate(client, d, 0, [
        {"x": 0, "y": 0, "w": 10, "h": 10, "reason": "外"},
        {"x": 2, "y": 2, "w": 3, "h": 3, "reason": "内"},
    ])
    body = _diff(client, d, 1, 1).json()
    assert body["added_pixels"] == 100  # 内矩形完全在外矩形内


def test_coverage_diff_edge_touching_is_not_overlap(client):
    d = _new_doc(client, size=(40, 30))
    _publish(client, d)
    # [0,10) 与 [10,20) 仅在像素边界 x=10 处相接：不共享像素，合计 200
    _annotate(client, d, 0, [
        {"x": 0, "y": 0, "w": 10, "h": 10, "reason": "甲"},
        {"x": 10, "y": 0, "w": 10, "h": 10, "reason": "乙"},
    ])
    body = _diff(client, d, 1, 1).json()
    assert body["added_pixels"] == 200


def test_coverage_diff_edge_touching_between_current_and_baseline(client):
    # 当前 [10,20) 与基准 [0,10) 相接但不重叠：全部 100 新增、100 失去
    d = _new_doc(client, size=(40, 30))
    _annotate(client, d, 0, [{"x": 0, "y": 0, "w": 10, "h": 10, "reason": "旧"}])
    _publish(client, d)
    _annotate(client, d, 1, [{"x": 10, "y": 0, "w": 10, "h": 10, "reason": "新"}])

    body = _diff(client, d, 2, 1).json()
    assert body["added_pixels"] == 100
    assert body["lost_pixels"] == 100


# ---------------- 水平段合并 ----------------

def test_lost_segments_merge_when_identical_across_adjacent_bands(client):
    d = _new_doc(client, size=(40, 30))
    # 基准是两个上下相接的矩形（y[0,10)、y[10,20)），当前清空：
    # 两个水平条带失去的 x 区间完全相同 -> 合并成 y[0,20) 一段
    _annotate(client, d, 0, [
        {"x": 0, "y": 0, "w": 5, "h": 10, "reason": "上"},
        {"x": 0, "y": 10, "w": 5, "h": 10, "reason": "下"},
    ])
    _publish(client, d)
    _annotate(client, d, 1, [])

    body = _diff(client, d, 2, 1).json()
    assert body["lost_pixels"] == 100
    assert body["lost_segments"] == [
        {"y0": 0, "y1": 20, "intervals": [{"x0": 0, "x1": 5}]}
    ]


def test_lost_segments_not_merged_when_intervals_differ(client):
    d = _new_doc(client, size=(40, 30))
    # 基准 20x20；当前上半段（y[0,10)）只遮 x[5,15)，下半段（y[10,20)）全遮：
    # 上半失去两段 x[0,5)、x[15,20)，下半无失去 -> 只输出一个段
    _annotate(client, d, 0, [{"x": 0, "y": 0, "w": 20, "h": 20, "reason": "整"}])
    _publish(client, d)
    _annotate(client, d, 1, [
        {"x": 5, "y": 0, "w": 10, "h": 10, "reason": "中上"},
        {"x": 0, "y": 10, "w": 20, "h": 10, "reason": "中下"},
    ])

    body = _diff(client, d, 2, 1).json()
    assert body["lost_pixels"] == 100  # (5 + 5) * 10
    assert body["lost_segments"] == [{
        "y0": 0, "y1": 10,
        "intervals": [{"x0": 0, "x1": 5}, {"x0": 15, "x1": 20}],
    }]


def test_lost_segments_adjacent_same_after_inner_split_merge(client):
    # 基准在 y[0,30) 上 x[0,20) 全遮；当前只在中间 y[10,20) 多遮 x[5,15)。
    # 上、下两段失去区间完全相同（x[0,20) 整段），须跨中间隔？不相邻——
    # 中间段区间不同，因此输出三段，且上/下段各自独立（中间不并入）。
    d = _new_doc(client, size=(40, 30))
    _annotate(client, d, 0, [{"x": 0, "y": 0, "w": 20, "h": 30, "reason": "整"}])
    _publish(client, d)
    _annotate(client, d, 1, [
        {"x": 5, "y": 10, "w": 10, "h": 10, "reason": "中"},
    ])

    body = _diff(client, d, 2, 1).json()
    assert body["lost_pixels"] == 20 * 20 + 10 * 10  # 400 + 100
    assert body["lost_segments"] == [
        {"y0": 0, "y1": 10, "intervals": [{"x0": 0, "x1": 20}]},
        {"y0": 10, "y1": 20,
         "intervals": [{"x0": 0, "x1": 5}, {"x0": 15, "x1": 20}]},
        {"y0": 20, "y1": 30, "intervals": [{"x0": 0, "x1": 20}]},
    ]


# ---------------- 基准冻结：不受后续标注/发布影响 ----------------

def test_baseline_uses_frozen_publication_revision(client):
    d = _new_doc(client, size=(40, 30))
    _annotate(client, d, 0, [{"x": 0, "y": 0, "w": 10, "h": 10, "reason": "v1"}])
    _publish(client, d)                      # v1 冻结 revision 1
    # v2 完全不同的遮盖并发布
    _annotate(client, d, 1, [{"x": 30, "y": 20, "w": 10, "h": 10, "reason": "v2"}])
    _publish(client, d)                      # v2 冻结 revision 2
    # 当前再改成第三套
    _annotate(client, d, 2, [{"x": 0, "y": 20, "w": 10, "h": 10, "reason": "cur"}])

    # 以 v1 为基准：新增=当前 100（与 v1 矩形不相交），失去=v1 的 100
    body = _diff(client, d, 3, 1).json()
    assert body["baseline_annotation_revision"] == 1
    assert body["added_pixels"] == 100
    assert body["lost_pixels"] == 100
    assert body["lost_segments"] == [
        {"y0": 0, "y1": 10, "intervals": [{"x0": 0, "x1": 10}]}
    ]

    # 以 v2 为基准：当前矩形与 v2 仅在 y 边界 x 区间相接？
    # v2 覆盖 x[30,40) y[20,30)，当前 x[0,10) y[20,30)：不重叠
    body = _diff(client, d, 3, 2).json()
    assert body["baseline_annotation_revision"] == 2
    assert body["added_pixels"] == 100
    assert body["lost_pixels"] == 100


def test_diff_against_older_version_after_many_edits(client):
    # 基准发布后任意修改当前标注，比较结果始终对照冻结时的矩形
    d = _new_doc(client, size=(40, 30))
    _annotate(client, d, 0, [{"x": 1, "y": 1, "w": 8, "h": 8, "reason": "底"}])
    _publish(client, d)
    _annotate(client, d, 1, [{"x": 1, "y": 1, "w": 9, "h": 9, "reason": "改1"}])
    _annotate(client, d, 2, [{"x": 1, "y": 1, "w": 7, "h": 7, "reason": "改2"}])

    body = _diff(client, d, 3, 1).json()
    # 当前 7x7=49 完全落在基准 8x8=64 内：新增 0，失去 64-49=15（右侧 1 列
    # x[8,9) 7 像素 + 底行 y[8,9) x[1,8) 7 像素 + 角点 1 像素）
    assert body["added_pixels"] == 0
    assert body["lost_pixels"] == 15
    seg_intervals = [
        (s["y0"], s["y1"], [(i["x0"], i["x1"]) for i in s["intervals"]])
        for s in body["lost_segments"]
    ]
    assert seg_intervals == [
        (1, 8, [(8, 9)]),
        (8, 9, [(1, 9)]),
    ]


# ---------------- 拒绝场景 ----------------

def test_diff_unknown_document_404(client):
    r = _diff(client, "deadbeef" * 4, 0, 1)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "document_not_found"


def test_diff_unknown_version_404_without_baseline_details(client):
    d = _new_doc(client)
    r = _diff(client, d, 0, 99)
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "unknown_version"
    assert detail["doc_id"] == d and detail["version"] == 99
    # 不泄露基准遮盖详情
    assert "annotation_revision" not in detail
    assert "segments" not in detail


def test_diff_not_yet_public_version_rejected_no_details(client):
    future = "2026-10-20T08:00:00Z"
    d = _new_doc(client)
    _publish(client, d, publish_at=future)

    r = _diff(client, d, 0, 1)
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "publication_not_yet_public"
    assert detail["doc_id"] == d and detail["version"] == 1
    # 错误体不含基准遮盖坐标/原因/摘要
    serialized = str(detail)
    assert "x" not in detail and "segments" not in detail
    assert "output_sha256" not in serialized


def test_diff_withdrawn_version_rejected_no_details(client):
    d = _new_doc(client)
    _annotate(client, d, 0, [{"x": 0, "y": 0, "w": 5, "h": 5, "reason": "密"}])
    _publish(client, d)
    w = client.post(
        f"/documents/{d}/publications/1/withdraw",
        json={"reason": "遮盖遗漏"},
    )
    assert w.status_code == 201

    r = _diff(client, d, 1, 1)
    assert r.status_code == 410
    detail = r.json()["detail"]
    assert detail["code"] == "publication_withdrawn"
    assert detail["doc_id"] == d and detail["version"] == 1
    # 不附撤回原因、遮盖坐标或成品摘要
    serialized = str(detail)
    assert "遮盖遗漏" not in serialized
    assert "segments" not in detail
    assert "output_sha256" not in serialized


def test_diff_withdrawal_takes_priority_over_schedule(client):
    # 已撤回且未到公开时间：410 优先于 404
    future = "2026-10-20T08:00:00Z"
    d = _new_doc(client)
    _publish(client, d, publish_at=future)
    assert client.post(
        f"/documents/{d}/publications/1/withdraw",
        json={"reason": "遗漏"},
    ).status_code == 201

    r = _diff(client, d, 0, 1)
    assert r.status_code == 410
    assert r.json()["detail"]["code"] == "publication_withdrawn"


def test_diff_stale_revision_rejected(client):
    d = _new_doc(client)
    _annotate(client, d, 0, [{"x": 0, "y": 0, "w": 4, "h": 4, "reason": "甲"}])
    _publish(client, d)
    # 当前修订号为 1，调用方仍提交 0
    r = _diff(client, d, 0, 1)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "stale_revision"
    assert detail["current_revision"] == 1
    assert detail["supplied_revision"] == 0
    # 修订过期时不返回任何比较结果/基准详情
    assert "lost_segments" not in detail


def test_diff_baseline_checked_before_revision_does_not_leak(client):
    # 基准不可见（已撤回）时，即使修订号也过期，仍按基准门控拒绝（410），
    # 不借 stale_revision 泄露基准存在之外的遮盖详情
    d = _new_doc(client)
    _annotate(client, d, 0, [{"x": 0, "y": 0, "w": 4, "h": 4, "reason": "甲"}])
    _publish(client, d)
    assert client.post(
        f"/documents/{d}/publications/1/withdraw",
        json={"reason": "遗漏"},
    ).status_code == 201
    _annotate(client, d, 1, [])  # 让提交的修订号 0 过期

    r = _diff(client, d, 0, 1)
    assert r.status_code == 410
    assert r.json()["detail"]["code"] == "publication_withdrawn"


# ---------------- 纯只读 ----------------

def test_diff_is_read_only_creates_nothing(client):
    d = _new_doc(client, size=(40, 30))
    _annotate(client, d, 0, [{"x": 0, "y": 0, "w": 10, "h": 10, "reason": "甲"}])
    pub = _publish(client, d)
    _annotate(client, d, 1, [{"x": 5, "y": 5, "w": 10, "h": 10, "reason": "乙"}])

    settings = client.app.state.settings
    import sqlite3
    db = sqlite3.connect(settings.db_path)
    before = {
        "pubs": db.execute("SELECT COUNT(*) FROM publications").fetchone()[0],
        "revs": db.execute("SELECT COUNT(*) FROM annotation_rects").fetchone()[0],
        "doc_rev": db.execute(
            "SELECT annotation_revision FROM documents WHERE id=?", (d,)
        ).fetchone()[0],
        "withdrawals": db.execute(
            "SELECT COUNT(*) FROM publication_withdrawals"
        ).fetchone()[0],
        "batches": db.execute("SELECT COUNT(*) FROM publication_batches").fetchone()[0],
    }
    db.close()

    r = _diff(client, d, 2, 1)
    assert r.status_code == 200, r.text

    db = sqlite3.connect(settings.db_path)
    after = {
        "pubs": db.execute("SELECT COUNT(*) FROM publications").fetchone()[0],
        "revs": db.execute("SELECT COUNT(*) FROM annotation_rects").fetchone()[0],
        "doc_rev": db.execute(
            "SELECT annotation_revision FROM documents WHERE id=?", (d,)
        ).fetchone()[0],
        "withdrawals": db.execute(
            "SELECT COUNT(*) FROM publication_withdrawals"
        ).fetchone()[0],
        "batches": db.execute("SELECT COUNT(*) FROM publication_batches").fetchone()[0],
    }
    db.close()
    assert before == after
    assert pub["version"] == 1  # 未产生新版本


def test_diff_does_not_mutify_on_repeated_calls(client):
    d = _new_doc(client, size=(40, 30))
    _annotate(client, d, 0, [{"x": 0, "y": 0, "w": 12, "h": 8, "reason": "甲"}])
    _publish(client, d)
    _annotate(client, d, 1, [{"x": 4, "y": 0, "w": 12, "h": 8, "reason": "乙"}])

    first = _diff(client, d, 2, 1).json()
    second = _diff(client, d, 2, 1).json()
    assert first == second
    # 交叠 8x8=64：新增 x[16,20)? 不——当前 x[4,16)，基准 x[0,12)
    assert first["added_pixels"] == 4 * 8
    assert first["lost_pixels"] == 4 * 8


# ---------------- 同一读取快照：并发标注修改不影响进行中的比较 ----------------

def test_diff_uses_single_read_snapshot_against_concurrent_annotation(client):
    d = _new_doc(client, size=(40, 30))
    _annotate(client, d, 0, [{"x": 0, "y": 0, "w": 10, "h": 10, "reason": "底"}])
    _publish(client, d)
    # 当前修订 1：仍是 v1 的同一矩形（差异为 0）

    revision_checked = threading.Event()
    release = threading.Event()
    outcome = {}

    def hook():
        revision_checked.set()
        release.wait(timeout=10)

    def differ():
        outcome["r"] = _diff(client, d, 1, 1)

    main_module.COVERAGE_DIFF_SNAPSHOT_HOOK = hook
    try:
        t = threading.Thread(target=differ)
        t.start()
        assert revision_checked.wait(timeout=10)
        # 比较进行中（快照已建立、矩形尚未读取）并发提交标注修改
        _annotate(client, d, 1, [])  # 修订 1 -> 2，当前遮盖清空
        release.set()
        t.join(timeout=20)
    finally:
        main_module.COVERAGE_DIFF_SNAPSHOT_HOOK = None

    assert "r" in outcome
    # 本次比较基于发起时的快照：仍读到修订 1（与基准相同），结果为零差异；
    # 而不是读到并发提交的修订 2（那样会报失去 100 像素或修订过期）。
    assert outcome["r"].status_code == 200, outcome["r"].text
    body = outcome["r"].json()
    assert body["current_revision"] == 1
    assert body["added_pixels"] == 0
    assert body["lost_pixels"] == 0

    # 比较结束后重新发起（不带钩子）：看到修订 2，需重新提交修订号
    stale = _diff(client, d, 1, 1)
    assert stale.status_code == 409
    fresh = _diff(client, d, 2, 1)
    assert fresh.json()["lost_pixels"] == 100


# ---------------- 请求体校验（与既有端点一致的 422） ----------------

@pytest.mark.parametrize("payload", [
    {"revision": -1, "baseline_version": 1},
    {"revision": 0, "baseline_version": 0},
    {"revision": 0},
    {"baseline_version": 1},
    {},
])
def test_diff_request_validation(client, payload):
    d = _new_doc(client)
    r = client.post(f"/documents/{d}/coverage-diff", json=payload)
    assert r.status_code == 422, r.text


# ---------------- 纯几何单元测试 ----------------

def test_geometry_half_open_intervals_and_band_merge():
    # 基准：上下相接的两个矩形，当前空：失去区间相邻完全相同 -> 合并
    d = coverage_diff(
        [],
        [{"x": 0, "y": 0, "w": 5, "h": 10, "reason": "a"},
         {"x": 0, "y": 10, "w": 5, "h": 10, "reason": "b"}],
        40, 30,
    )
    assert d == {
        "added_pixels": 0,
        "lost_pixels": 100,
        "lost_segments": [
            {"y0": 0, "y1": 20, "intervals": [{"x0": 0, "x1": 5}]}
        ],
    }


def test_geometry_complex_subtraction():
    # 当前在基准中部开"窗"并向右扩展：失去两列、新增右侧两列
    d = coverage_diff(
        [{"x": 8, "y": 0, "w": 14, "h": 20, "reason": "cur"}],  # x[8,22)
        [{"x": 0, "y": 0, "w": 20, "h": 20, "reason": "base"}],  # x[0,20)
        40, 30,
    )
    assert d["added_pixels"] == 2 * 20
    assert d["lost_pixels"] == 8 * 20
    assert d["lost_segments"] == [{
        "y0": 0, "y1": 20,
        "intervals": [{"x0": 0, "x1": 8}],
    }]
