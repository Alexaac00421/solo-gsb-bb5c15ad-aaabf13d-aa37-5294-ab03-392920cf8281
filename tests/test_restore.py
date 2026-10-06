"""服务数据归档恢复（POST /service-data-archives/restore）测试。

覆盖：
- 成功恢复：文档/各修订标注/发布/批次/撤回与对应文件整体写入，ID、版本、
  摘要、时间原样保留，本机路径按目标 DATA_DIR 重建；恢复计数正确；既有
  查询与公开接口（含定时公开 404->200、撤回 410）按恢复记录工作；恢复后
  业务可继续（修订号/版本号顺延、撤回幂等）；
- 空归档恢复到空实例；
- 拒绝：无效归档（400 invalid_archive 附 issues）、非 ZIP、空文件、超限、
  非空实例（库非空/目录有业务文件）、目标文件冲突、写盘失败、写库失败——
  均不留可查询的部分记录或本次文件；
- 批次幂等键不推断恢复：旧键在目标实例按新键处理；
- 并发：恢复与现有写操作、恢复与恢复之间串行判定，并发写入不能绕过空实例
  判定；临时文件清理。
"""
import hashlib
import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app import main as main_module
from app.config import Settings
from app.main import create_app
from tests.test_api import jpeg_bytes, png_bytes, upload  # noqa: F401
from tests.test_archive import _annotate, _publish  # noqa: F401
from tests.test_archive_verify import rebuild


@pytest.fixture()
def client(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"))
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _restore(client, content: bytes, filename="archive.zip"):
    return client.post(
        "/service-data-archives/restore",
        files={"file": (filename, bytes(content), "application/zip")},
    )


def _archive_bytes(client) -> bytes:
    r = client.post("/service-data-archives")
    assert r.status_code == 200, r.text
    # 显式深拷贝：TestClient 的响应缓冲会被后续请求复用
    return bytes(bytearray(r.content))


def _db_counts(client) -> dict:
    settings = client.app.state.settings
    conn = sqlite3.connect(settings.db_path)
    try:
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in (
                "documents", "annotation_rects", "publications",
                "publication_batches", "batch_publications",
                "batch_idempotency", "publication_withdrawals",
            )
        }
    finally:
        conn.close()


def _db_rows(client, sql, args=()):
    settings = client.app.state.settings
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _build_rich_archive(tmp_path) -> tuple[bytes, dict]:
    """在独立的源实例上造一份富数据（多修订/定时/批次+幂等键/撤回/JPEG）
    并取归档字节；返回 (zip_bytes, 源侧关键数据)。"""
    settings = Settings(data_dir=str(tmp_path / "src-data"))
    with TestClient(create_app(settings)) as src:
        a = upload(src, png_bytes(color=(10, 20, 30)), "证件.png").json()
        b = upload(src, jpeg_bytes(size=(10, 20), orientation=6), "b.jpg").json()
        c = upload(src, png_bytes(color=(1, 2, 3)), "c.png").json()

        a_rects_v2 = [
            {"x": 1, "y": 1, "w": 5, "h": 5, "reason": "人脸"},
            {"x": 10, "y": 10, "w": 3, "h": 3, "reason": "工号"},
        ]
        _annotate(src, a["doc_id"], 0, [
            {"x": 0, "y": 0, "w": 4, "h": 4, "reason": "身份证号"}])
        _annotate(src, a["doc_id"], 1, a_rects_v2)
        pub_a1 = _publish(src, a["doc_id"])  # v1 @ rev2，立即公开
        publish_at = (datetime.now(timezone.utc) + timedelta(hours=24))
        pub_a2 = _publish(  # v2 @ rev2，定时公开
            src, a["doc_id"],
            publish_at=publish_at.isoformat().replace("+00:00", "Z"),
        )
        withdraw_a1 = src.post(
            f"/documents/{a['doc_id']}/publications/1/withdraw",
            json={"reason": "遮盖遗漏：右下角工号未遮盖"},
        ).json()

        b_rects = [{"x": 2, "y": 2, "w": 6, "h": 6, "reason": "车牌"}]
        _annotate(src, b["doc_id"], 0, b_rects)
        batch = src.post("/publication-batches", json={
            "documents": [
                {"doc_id": b["doc_id"], "expected_revision": 1},
                {"doc_id": c["doc_id"], "expected_revision": 0},
            ],
            "idempotency_key": "ops-batch-key-1",
        })
        assert batch.status_code == 201, batch.text

        archive = _archive_bytes(src)
    meta = {
        "a": a, "b": b, "c": c,
        "a_rects_v2": a_rects_v2,
        "b_rects": b_rects,
        "pub_a1": pub_a1,
        "pub_a2": pub_a2,
        "withdraw_a1": withdraw_a1,
        "batch": batch.json(),
        "idempotency_key": "ops-batch-key-1",
    }
    return archive, meta


# ---------------- 成功恢复 ----------------

def test_restore_round_trip_preserves_records_files_and_public_rules(
    client, tmp_path
):
    archive, meta = _build_rich_archive(tmp_path)
    a, b, c = meta["a"], meta["b"], meta["c"]

    r = _restore(client, archive)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restored"] is True
    assert body["restored_at"]
    assert body["record_counts"] == {
        "documents": 3,
        "annotation_revisions": 6,  # A: 0..2，B: 0..1，C: 0
        "publications": 4,          # A v1/v2 + B v1 + C v1
        "publication_batches": 1,
        "withdrawals": 1,
    }
    assert body["file_counts"] == {"originals": 3, "publications": 4}

    # 文档元数据（上传名、媒体类型、旋正尺寸、摘要、创建时间、当前修订）保留
    da = client.get(f"/documents/{a['doc_id']}").json()
    assert da["filename"] == "证件.png"
    assert da["media_type"] == "image/png"
    assert da["created_at"] == a["created_at"]
    assert da["annotation_revision"] == 2
    assert da["original_sha256"] == a["original_sha256"]
    db_ = client.get(f"/documents/{b['doc_id']}").json()
    assert db_["media_type"] == "image/jpeg"
    assert db_["width"] == 20 and db_["height"] == 10  # EXIF 旋正后尺寸

    # 发布列表携带原公开时间与撤回状态
    pubs = {p["version"]: p for p in da["publications"]}
    assert pubs[1]["created_at"] == meta["pub_a1"]["created_at"]
    assert pubs[1]["publish_at"] is None
    assert pubs[1]["withdrawn"] is True
    assert pubs[1]["withdrawn_at"] == meta["withdraw_a1"]["withdrawn_at"]
    assert pubs[1]["withdrawal_reason"] == "遮盖遗漏：右下角工号未遮盖"
    assert pubs[2]["publish_at"] == meta["pub_a2"]["publish_at"]
    assert pubs[2]["withdrawn"] is False

    # 当前标注（最新修订）与历史修订冻结的遮盖都可用
    ann = client.get(f"/documents/{a['doc_id']}/annotations").json()
    assert ann["revision"] == 2
    assert ann["rects"] == meta["a_rects_v2"]

    # 公开接口按恢复记录与原撤回/定时规则工作：撤回 410、未到点 404、正常 200
    assert client.get(f"/publications/{a['doc_id']}/1").status_code == 410
    assert client.get(f"/publications/{a['doc_id']}/1/image").status_code == 410
    not_yet = client.get(f"/publications/{a['doc_id']}/2")
    assert not_yet.status_code == 404
    assert not_yet.json()["detail"]["code"] == "publication_not_yet_public"
    pb = client.get(f"/publications/{b['doc_id']}/1").json()
    assert pb["redactions"] == meta["b_rects"]
    batch_b = next(
        d for d in meta["batch"]["documents"] if d["doc_id"] == b["doc_id"]
    )
    assert pb["output_sha256"] == batch_b["output_sha256"]
    img = client.get(f"/publications/{b['doc_id']}/1/image")
    assert img.status_code == 200
    assert hashlib.sha256(img.content).hexdigest() == batch_b["output_sha256"]

    # 撤回查询返回首次撤回记录
    w = client.get(f"/documents/{a['doc_id']}/publications/1/withdrawal").json()
    assert w["withdrawn"] is True
    assert w["withdrawn_at"] == meta["withdraw_a1"]["withdrawn_at"]
    assert w["withdrawal_reason"] == "遮盖遗漏：右下角工号未遮盖"

    # 批次关系（成员顺序、创建时间、公开时间）保留
    batch = client.get(
        f"/publication-batches/{meta['batch']['batch_id']}"
    ).json()
    assert batch["created_at"] == meta["batch"]["created_at"]
    assert batch["publish_at"] is None
    assert [(d["position"], d["doc_id"], d["version"])
            for d in batch["documents"]] == [
        (0, b["doc_id"], 1), (1, c["doc_id"], 1)]

    # 本机路径按目标 DATA_DIR 重建（不残留源实例路径），文件字节摘要一致
    settings = client.app.state.settings
    data_root = os.path.abspath(settings.data_dir)
    rows = _db_rows(client, "SELECT id, original_path FROM documents")
    assert len(rows) == 3
    for row in rows:
        assert row["original_path"].startswith(data_root + os.sep)
        assert "src-data" not in row["original_path"]
        assert os.path.exists(row["original_path"])
    orig_a = os.path.join(settings.originals_dir, a["doc_id"] + ".png")
    with open(orig_a, "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == a["original_sha256"]
    # JPEG 原图按 media_type 恢复 .jpg 扩展名
    assert os.path.exists(
        os.path.join(settings.originals_dir, b["doc_id"] + ".jpg"))
    out_rows = _db_rows(
        client, "SELECT doc_id, version, output_path, output_sha256"
        " FROM publications")
    assert len(out_rows) == 4
    for row in out_rows:
        assert row["output_path"].startswith(data_root + os.sep)
        with open(row["output_path"], "rb") as f:
            assert hashlib.sha256(f.read()).hexdigest() == row["output_sha256"]


def test_restore_empty_archive_into_empty_instance(client, tmp_path):
    settings = Settings(data_dir=str(tmp_path / "src-data"))
    with TestClient(create_app(settings)) as src:
        archive = _archive_bytes(src)
    r = _restore(client, archive)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restored"] is True
    assert body["record_counts"] == {
        "documents": 0, "annotation_revisions": 0, "publications": 0,
        "publication_batches": 0, "withdrawals": 0,
    }
    assert body["file_counts"] == {"originals": 0, "publications": 0}
    assert all(v == 0 for v in _db_counts(client).values())


def test_restore_preserves_scheduled_publication_timing(tmp_path, monkeypatch):
    clock = {"now": datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(main_mod, "_now_dt", lambda: clock["now"])

    src_settings = Settings(data_dir=str(tmp_path / "src-data"))
    with TestClient(create_app(src_settings)) as src:
        d = upload(src, png_bytes(), "s.png").json()
        _annotate(src, d["doc_id"], 0, [
            {"x": 0, "y": 0, "w": 2, "h": 2, "reason": "r"}])
        target = clock["now"] + timedelta(hours=12)
        pub = _publish(
            src, d["doc_id"],
            publish_at=target.isoformat().replace("+00:00", "Z"))
        archive = _archive_bytes(src)

    dst_settings = Settings(data_dir=str(tmp_path / "dst-data"))
    with TestClient(create_app(dst_settings)) as dst:
        r = _restore(dst, archive)
        assert r.status_code == 200, r.text
        # 到点前：与公开读取规则一致 404，不泄露版本内容
        before = dst.get(f"/publications/{d['doc_id']}/1")
        assert before.status_code == 404
        assert before.json()["detail"]["code"] == "publication_not_yet_public"
        # 时钟越过公开时间：无需任何补操作即可读，publish_at 原样保留
        clock["now"] += timedelta(hours=13)
        after = dst.get(f"/publications/{d['doc_id']}/1")
        assert after.status_code == 200
        assert after.json()["publish_at"] == pub["publish_at"]
        img = dst.get(f"/publications/{d['doc_id']}/1/image")
        assert img.status_code == 200
        assert hashlib.sha256(img.content).hexdigest() == pub["output_sha256"]


def test_restored_instance_continues_sequences(client, tmp_path):
    archive, meta = _build_rich_archive(tmp_path)
    assert _restore(client, archive).status_code == 200
    a, b = meta["a"], meta["b"]

    # 标注修订号顺延：恢复的当前修订 2 -> 3
    r = client.put(
        f"/documents/{a['doc_id']}/annotations",
        json={"revision": 2, "rects": [
            {"x": 0, "y": 0, "w": 1, "h": 1, "reason": "补充"}]},
    )
    assert r.status_code == 200 and r.json()["revision"] == 3
    # 版本号顺延：A 已有 v1/v2 -> 新发布 v3
    r = client.post(f"/documents/{a['doc_id']}/publish")
    assert r.status_code == 201 and r.json()["version"] == 3
    # 恢复的历史版本可撤回，且撤回幂等（重复撤回返回首次记录）
    r = client.post(
        f"/documents/{b['doc_id']}/publications/1/withdraw",
        json={"reason": "复查下架"},
    )
    assert r.status_code == 201
    again = client.post(
        f"/documents/{b['doc_id']}/publications/1/withdraw",
        json={"reason": "不会改写首次原因"},
    )
    assert again.status_code == 200
    assert again.json()["withdrawal_reason"] == "复查下架"


def test_restore_does_not_reconstruct_batch_idempotency_keys(client, tmp_path):
    archive, meta = _build_rich_archive(tmp_path)
    assert _restore(client, archive).status_code == 200
    # 归档不含幂等键：目标实例的幂等表保持为空，不推断恢复
    assert _db_counts(client)["batch_idempotency"] == 0
    # 旧键重放不会被识别为重复：按首次请求处理，生成新批次与新版本
    r = client.post("/publication-batches", json={
        "documents": [
            {"doc_id": meta["b"]["doc_id"], "expected_revision": 1},
            {"doc_id": meta["c"]["doc_id"], "expected_revision": 0},
        ],
        "idempotency_key": meta["idempotency_key"],
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["batch_id"] != meta["batch"]["batch_id"]
    assert {d["version"] for d in body["documents"]} == {2}  # B、C 各自 v2


# ---------------- 拒绝：无效归档 / 请求本身无效 ----------------

def test_restore_rejects_tampered_archive_and_leaves_nothing(client, tmp_path):
    archive, meta = _build_rich_archive(tmp_path)
    victim = f"publications/{meta['a']['doc_id']}/v1.png"
    tampered = rebuild(archive, raw_updates={victim: b"tampered-bytes"})

    r = _restore(client, tampered)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "invalid_archive"
    assert any(
        i["code"] == "manifest_sha256_mismatch" for i in detail["issues"])
    # 不留可查询的部分记录或本次文件
    assert all(v == 0 for v in _db_counts(client).values())
    settings = client.app.state.settings
    assert os.listdir(settings.originals_dir) == []
    assert os.listdir(settings.publications_dir) == []


def test_restore_rejects_non_zip(client):
    r = _restore(client, b"this is not a zip archive")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "invalid_archive"
    assert detail["issues"][0]["code"] == "not_a_zip"
    assert all(v == 0 for v in _db_counts(client).values())


def test_restore_rejects_empty_upload(client):
    r = _restore(client, b"")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "empty_upload"


def test_restore_rejects_oversized_upload(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"), max_restore_bytes=64)
    with TestClient(create_app(settings)) as c:
        r = _restore(c, b"x" * 1024)
    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "payload_too_large"


# ---------------- 拒绝：非空实例 ----------------

def test_restore_rejected_when_instance_has_records(client, tmp_path):
    archive, meta = _build_rich_archive(tmp_path)
    existing = upload(client, png_bytes(color=(7, 7, 7)), "existing.png").json()

    r = _restore(client, archive)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "instance_not_empty"
    # 未写入任何归档文件/记录：目录只有既有文件，库只有既有文档
    settings = client.app.state.settings
    assert sorted(os.listdir(settings.originals_dir)) == [
        existing["doc_id"] + ".png"]
    assert os.listdir(settings.publications_dir) == []
    counts = _db_counts(client)
    assert counts["documents"] == 1
    assert all(v == 0 for k, v in counts.items() if k != "documents")
    # 既有文档不受影响
    assert client.get(f"/documents/{existing['doc_id']}").status_code == 200


@pytest.mark.parametrize("which", ["originals", "publications"])
def test_restore_rejected_when_business_files_present(client, tmp_path, which):
    archive, meta = _build_rich_archive(tmp_path)
    settings = client.app.state.settings
    stray_dir = (settings.originals_dir if which == "originals"
                 else settings.publications_dir)
    with open(os.path.join(stray_dir, "stray-leftover.png"), "wb") as f:
        f.write(b"leftover")

    r = _restore(client, archive)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "instance_not_empty"
    assert detail["directory"] == which
    # 库仍为空，归档文件一个也没写入
    assert all(v == 0 for v in _db_counts(client).values())
    assert os.listdir(stray_dir) == ["stray-leftover.png"]


def test_restore_rejected_when_target_file_conflicts(client, tmp_path):
    archive, meta = _build_rich_archive(tmp_path)
    settings = client.app.state.settings
    victim = meta["a"]["doc_id"] + ".png"

    def hook():
        # 空实例判定通过后，模拟目标路径上已存在同名文件（并发残留）
        with open(os.path.join(settings.originals_dir, victim), "wb") as f:
            f.write(b"conflicting-bytes")

    main_module.RESTORE_EMPTY_CHECK_HOOK = hook
    try:
        r = _restore(client, archive)
    finally:
        main_module.RESTORE_EMPTY_CHECK_HOOK = None
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "restore_target_conflict"
    assert detail["doc_id"] == meta["a"]["doc_id"]
    # 冲突前已写的本次文件全部清理：只剩冲突文件本身；库仍为空
    assert sorted(os.listdir(settings.originals_dir)) == [victim]
    assert os.listdir(settings.publications_dir) == []
    assert all(v == 0 for v in _db_counts(client).values())


# ---------------- 拒绝：写入失败（整体回滚 + 清理本次文件） ----------------

def test_restore_file_write_failure_cleans_up(client, tmp_path):
    archive, meta = _build_rich_archive(tmp_path)
    settings = client.app.state.settings

    def hook():
        # 让 originals 目录路径变成一个普通文件 -> 原图落盘必然失败
        os.rmdir(settings.originals_dir)
        with open(settings.originals_dir, "wb") as f:
            f.write(b"blocker")

    main_module.RESTORE_EMPTY_CHECK_HOOK = hook
    try:
        r = _restore(client, archive)
    finally:
        main_module.RESTORE_EMPTY_CHECK_HOOK = None
    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "restore_file_write_failed"
    # 记录整体回滚，本次文件无一残留
    assert all(v == 0 for v in _db_counts(client).values())
    assert os.listdir(settings.publications_dir) == []


def test_restore_db_write_failure_rolls_back_and_cleans_files(
    client, tmp_path
):
    archive, meta = _build_rich_archive(tmp_path)
    # 用触发器让 publications 写入在库内失败（RAISE 即 sqlite3.Error）
    settings = client.app.state.settings
    conn = sqlite3.connect(settings.db_path)
    conn.execute(
        "CREATE TRIGGER fail_publications BEFORE INSERT ON publications"
        " BEGIN SELECT RAISE(FAIL, 'simulated database failure'); END"
    )
    conn.commit()
    conn.close()

    r = _restore(client, archive)
    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "restore_write_failed"
    # 记录整体回滚；本次落盘的原图/成品与新建目录全部清理
    assert all(v == 0 for v in _db_counts(client).values())
    settings = client.app.state.settings
    assert os.listdir(settings.originals_dir) == []
    assert os.listdir(settings.publications_dir) == []


# ---------------- 并发：空实例判定与现有写操作、其他恢复串行 ----------------

def test_restore_empty_check_serialized_with_concurrent_upload(client, tmp_path):
    archive, meta = _build_rich_archive(tmp_path)
    checked = threading.Event()
    release = threading.Event()
    outcomes: dict = {}

    def hook():
        # 空实例判定已通过、写锁在握：此刻并发上传只能等本事务提交
        checked.set()
        release.wait(timeout=10)

    def restorer():
        outcomes["restore"] = _restore(client, archive)

    def uploader():
        assert checked.wait(timeout=10)
        outcomes["upload"] = upload(client, png_bytes(color=(9, 9, 9)), "late.png")

    main_module.RESTORE_EMPTY_CHECK_HOOK = hook
    try:
        t_restore = threading.Thread(target=restorer)
        t_restore.start()
        assert checked.wait(timeout=10)
        t_upload = threading.Thread(target=uploader)
        t_upload.start()
        time.sleep(0.5)  # 让上传抵达数据库写锁
        assert "upload" not in outcomes  # 被恢复事务的写锁挡住，无法绕过判定
        release.set()
        t_restore.join(timeout=20)
        t_upload.join(timeout=20)
    finally:
        main_module.RESTORE_EMPTY_CHECK_HOOK = None

    assert outcomes["restore"].status_code == 200
    assert outcomes["upload"].status_code == 201
    # 恢复数据与并发上传都完整存在（上传在恢复提交后落库）
    counts = _db_counts(client)
    assert counts["documents"] == 4
    late_id = outcomes["upload"].json()["doc_id"]
    assert client.get(f"/documents/{late_id}").status_code == 200
    assert client.get(f"/documents/{meta['a']['doc_id']}").status_code == 200


def test_concurrent_restores_exactly_one_succeeds(client, tmp_path):
    archive, meta = _build_rich_archive(tmp_path)
    checked = threading.Event()
    release = threading.Event()
    outcomes: list = []

    def hook():
        checked.set()
        release.wait(timeout=10)

    def restorer():
        outcomes.append(_restore(client, archive))

    main_module.RESTORE_EMPTY_CHECK_HOOK = hook
    try:
        t1 = threading.Thread(target=restorer)
        t1.start()
        assert checked.wait(timeout=10)
        t2 = threading.Thread(target=restorer)
        t2.start()
        time.sleep(0.5)  # 第二次恢复阻塞在写锁上
        release.set()
        t1.join(timeout=20)
        t2.join(timeout=20)
    finally:
        main_module.RESTORE_EMPTY_CHECK_HOOK = None

    codes = sorted(r.status_code for r in outcomes)
    assert codes == [200, 409]
    loser = next(r for r in outcomes if r.status_code == 409)
    assert loser.json()["detail"]["code"] == "instance_not_empty"
    # 最终库中恰好一份归档数据
    counts = _db_counts(client)
    assert counts["documents"] == 3
    assert counts["publications"] == 4


# ---------------- 临时文件清理 ----------------

def test_restore_temp_upload_cleaned_up(client, tmp_path):
    archive, meta = _build_rich_archive(tmp_path)
    assert _restore(client, archive).status_code == 200
    # 失败路径（非 ZIP）同样清理
    assert _restore(client, b"not a zip").status_code == 400
    leftovers = [n for n in os.listdir(tempfile.gettempdir())
                 if n.startswith("svc-restore-")]
    assert leftovers == []
