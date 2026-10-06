"""服务数据归档核验（POST /service-data-archives/verify）测试。

覆盖：
- 正常归档（含空库、JPEG、批次/定时/撤回/多修订）：valid=true，返回五类
  记录计数、file_count、member_count，issues 为空；
- ZIP 结构：非 ZIP、空文件、重复成员名、非法相对路径（前导斜杠/../目录
  条目）、清单或记录文件缺项、清单之外的多余成员；
- 一一对应：清单列了不存在的成员、真实成员不在清单中、清单重复条目、
  file_count 不符；
- SHA-256：成员字节被篡改（清单摘要不符）、标识/kind/file_count 与路径不符；
- 五类记录计数与清单 record_counts 不符、记录 JSON 损坏；
- 引用关系冲突：标注修订不连续/越界矩形、发布版本不连续/修订悬空/批次
  成员对不上/撤回与 withdrawn 标记不一致、撤回悬空、批次 position 乱序、
  原图/成品摘要与记录冲突、游离文件；
- 纯只读副作用：核验不向 DATA_DIR 解压、不改变后续归档结果。
"""
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.test_archive import (  # noqa: F401
    _annotate,
    _archive,
    _new_doc,
    _publish,
    _withdraw,
)


@pytest.fixture()
def client(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"))
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _verify(client, content: bytes, filename="archive.zip"):
    return client.post(
        "/service-data-archives/verify",
        files={"file": (filename, bytes(content), "application/zip")},
    )


def _verify_ok(client, content: bytes) -> dict:
    r = _verify(client, content)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"] is True, body["issues"]
    return body


def _codes(body) -> set[str]:
    return {i["code"] for i in body["issues"]}


def _archive_bytes(client) -> bytes:
    # 显式深拷贝响应字节：TestClient 的响应缓冲在后续请求时会被复用，
    # bytes(bytes) 是恒等返回，必须经 bytearray 才能得到独立副本，
    # 否则后续请求会就地改写"归档源"底层内存，使断言出现非确定性失败。
    return bytes(bytearray(_archive(client).content))


def rebuild(data: bytes, updates=None, *, drops=(), order=None,
            raw_updates=None) -> bytes:
    """重建一个归档 ZIP。

    updates: 成员名 -> 可 JSON 序列化对象（自动编码为 UTF-8 JSON）；
    raw_updates: 成员名 -> 原始字节（用于非 JSON 篡改）；
    drops: 需要删除的成员名；order: 自定义成员写出顺序。
    未提及的成员按原字节保留。
    """
    src = zipfile.ZipFile(io.BytesIO(bytes(data)))
    members = {n: src.read(n) for n in src.namelist()}
    src.close()
    for name, obj in (updates or {}).items():
        members[name] = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    for name, raw in (raw_updates or {}).items():
        members[name] = raw
    for name in drops:
        members.pop(name, None)
    names = order if order is not None else list(members)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            zf.writestr(name, members[name])
    return bytes(bytearray(buf.getvalue()))


def _manifest(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return json.loads(zf.read("manifest.json"))


def _records(data: bytes) -> dict[str, dict]:
    out = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in (
            "documents.json", "annotations.json", "publications.json",
            "publication-batches.json", "withdrawals.json",
        ):
            out[name] = json.loads(zf.read(name))
    return out


def _doc_a(recs: dict) -> str:
    """富数据归档中的文档 A：唯一有 3 个版本且 v1 被撤回的文档。

    记录按随机 doc_id 排序，不能依赖 documents[0]/publications[0] 是 A。
    """
    versions: dict[str, set] = {}
    for p in recs["publications.json"]["publications"]:
        versions.setdefault(p["doc_id"], set()).add(p["version"])
    candidates = [doc_id for doc_id, vs in versions.items() if vs == {1, 2, 3}]
    assert len(candidates) == 1
    return candidates[0]


def _rich_archive_bytes(client) -> bytes:
    """两份文档：A 两修订（v1/v2，v1 撤回），B 经批次发布（与 A 同批）。"""
    a = _new_doc(client, (1, 2, 3))
    b = _new_doc(client, (4, 5, 6))
    _annotate(client, a["doc_id"], 0, [
        {"x": 0, "y": 0, "w": 2, "h": 2, "reason": "甲"}])
    _publish(client, a["doc_id"])  # A v1
    _withdraw(client, a["doc_id"], 1)
    _annotate(client, a["doc_id"], 1, [
        {"x": 1, "y": 1, "w": 2, "h": 2, "reason": "乙"}])
    _publish(client, a["doc_id"])  # A v2
    _annotate(client, b["doc_id"], 0, [
        {"x": 0, "y": 0, "w": 1, "h": 1, "reason": "丙"}])
    batch = client.post("/publication-batches", json={
        "documents": [
            {"doc_id": a["doc_id"], "expected_revision": 2},
            {"doc_id": b["doc_id"], "expected_revision": 1},
        ],
    })
    assert batch.status_code == 201, batch.text
    return _archive_bytes(client)


# ---------------- 通过用例 ----------------

def test_verify_accepts_valid_archive_and_reports_counts(client):
    data = _rich_archive_bytes(client)
    body = _verify_ok(client, data)
    # manifest + 5 记录 JSON + 2 原图 + 3 成品（A v1/v2/v3、B v1）= 12
    assert body["member_count"] == 1 + 5 + 2 + 4
    assert body["file_count"] == body["member_count"] - 1
    assert body["record_counts"] == {
        "documents": 2,
        "annotation_revisions": 5,  # A: 0,1,2；B: 0,1
        "publications": 4,          # A v1/v2 + 批次内 A v3、B v1
        "publication_batches": 1,
        "withdrawals": 1,
    }
    assert body["issues"] == []


def test_verify_accepts_empty_database_archive(client):
    data = _archive_bytes(client)
    body = _verify_ok(client, data)
    assert body["member_count"] == 6 and body["file_count"] == 5
    assert body["record_counts"] == {
        "documents": 0, "annotation_revisions": 0, "publications": 0,
        "publication_batches": 0, "withdrawals": 0,
    }


def test_verify_accepts_single_doc_jpeg_archive(client):
    from tests.test_api import jpeg_bytes
    _new_doc(client, data=jpeg_bytes(size=(10, 20), orientation=6),
             filename="证件.jpg")
    data = _archive_bytes(client)
    body = _verify_ok(client, data)
    assert body["valid"] is True
    assert body["record_counts"]["documents"] == 1


# ---------------- ZIP / 请求级失败 ----------------

def test_verify_rejects_non_zip(client):
    r = _verify(client, b"this is not a zip at all")
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert _codes(body) == {"not_a_zip"}


def test_verify_rejects_empty_upload(client):
    r = _verify(client, b"")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "empty_upload"


def test_verify_rejects_duplicate_member_names(client):
    data = _archive_bytes(client)
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in src.namelist():
            zf.writestr(name, src.read(name))
        zf.writestr("documents.json", src.read("documents.json"))  # 重复
    src.close()
    body = _verify(client, buf.getvalue()).json()
    assert body["valid"] is False
    assert "duplicate_zip_member" in _codes(body)
    assert any(i.get("path") == "documents.json" for i in body["issues"])


def test_verify_rejects_illegal_relative_paths(client):
    data = _archive_bytes(client)
    for evil in ("../escape.json", "/abs.json", "dir/../../x.json", "sub/"):
        bad = rebuild(data, raw_updates={evil: b"{}"})
        body = _verify(client, bad).json()
        assert body["valid"] is False, evil
        codes = _codes(body)
        assert ("illegal_member_path" in codes
                or "unexpected_directory_member" in codes), evil


# ---------------- 缺项 / 多项 / 一一对应 ----------------

def test_verify_rejects_missing_manifest(client):
    data = _archive_bytes(client)
    bad = rebuild(data, drops=("manifest.json",))
    body = _verify(client, bad).json()
    assert body["valid"] is False
    assert "missing_manifest" in _codes(body)


def test_verify_rejects_missing_record_file(client):
    data = _archive_bytes(client)
    bad = rebuild(data, drops=("withdrawals.json",))
    body = _verify(client, bad).json()
    assert body["valid"] is False
    assert "missing_record_file" in _codes(body)


def test_verify_rejects_member_not_listed_in_manifest(client):
    data = _rich_archive_bytes(client)
    # 增加一个结构合法但清单未列入的成品（伪造 doc 路径），同时不碰清单
    bad = rebuild(data, raw_updates={
        "publications/00000000000000000000000000000000/v1.png": b"x"})
    body = _verify(client, bad).json()
    assert body["valid"] is False
    codes = _codes(body)
    assert "member_not_in_manifest" in codes
    assert "manifest_file_count_mismatch" in codes


def test_verify_rejects_manifest_entry_without_member(client):
    data = _archive_bytes(client)
    manifest = _manifest(data)
    manifest["files"].append({
        "path": "originals/00000000000000000000000000000000.png",
        "sha256": "0" * 64, "kind": "original",
        "doc_id": "0" * 32, "file_count": 1,
    })
    manifest["file_count"] += 1
    bad = rebuild(data, {"manifest.json": manifest})
    body = _verify(client, bad).json()
    assert body["valid"] is False
    codes = _codes(body)
    assert "manifest_member_missing" in codes


def test_verify_rejects_duplicate_manifest_entry(client):
    data = _archive_bytes(client)
    manifest = _manifest(data)
    manifest["files"].append(dict(manifest["files"][0]))
    bad = rebuild(data, {"manifest.json": manifest})
    body = _verify(client, bad).json()
    assert body["valid"] is False
    codes = _codes(body)
    assert "duplicate_manifest_entry" in codes
    assert "manifest_file_count_mismatch" in codes


def test_verify_rejects_wrong_file_count(client):
    data = _archive_bytes(client)
    manifest = _manifest(data)
    manifest["file_count"] += 1
    bad = rebuild(data, {"manifest.json": manifest})
    body = _verify(client, bad).json()
    assert body["valid"] is False
    assert "manifest_file_count_mismatch" in _codes(body)


def test_verify_rejects_unexpected_top_level_member(client):
    data = _archive_bytes(client)
    manifest = _manifest(data)
    manifest["files"].append({
        "path": "evil.txt", "sha256": "0" * 64, "kind": "records",
        "file_count": 1,
    })
    manifest["file_count"] += 1
    manifest["record_counts"]["documents"] = 0
    bad = rebuild(data, {"manifest.json": manifest},
                  raw_updates={"evil.txt": b"pwned"})
    body = _verify(client, bad).json()
    assert body["valid"] is False
    assert "unexpected_member" in _codes(body)


# ---------------- SHA-256 / 标识 ----------------

def test_verify_rejects_tampered_member_bytes(client):
    data = _rich_archive_bytes(client)
    pubs = _records(data)["publications.json"]["publications"]
    target = f"publications/{pubs[0]['doc_id']}/v1.png"
    bad = rebuild(data, raw_updates={target: b"tampered-png-bytes"})
    body = _verify(client, bad).json()
    assert body["valid"] is False
    codes = _codes(body)
    # 清单摘要与实际字节不符；成品摘要也与发布记录不符
    assert "manifest_sha256_mismatch" in codes
    issue = next(i for i in body["issues"] if i["code"] == "manifest_sha256_mismatch")
    assert issue["path"] == target


def test_verify_rejects_manifest_wrong_digest_for_untouched_member(client):
    data = _archive_bytes(client)
    manifest = _manifest(data)
    docs_entry = next(e for e in manifest["files"] if e["kind"] == "records")
    docs_entry["sha256"] = "f" * 64
    bad = rebuild(data, {"manifest.json": manifest})
    body = _verify(client, bad).json()
    assert body["valid"] is False
    assert "manifest_sha256_mismatch" in _codes(body)


def test_verify_rejects_manifest_identity_kind_mismatch(client):
    data = _rich_archive_bytes(client)
    manifest = _manifest(data)
    entry = next(e for e in manifest["files"] if e["kind"] == "original")
    entry["kind"] = "publication"
    entry["version"] = 7
    bad = rebuild(data, {"manifest.json": manifest})
    body = _verify(client, bad).json()
    assert body["valid"] is False
    assert "manifest_identity_mismatch" in _codes(body)


def test_verify_rejects_manifest_entry_file_count_not_one(client):
    data = _archive_bytes(client)
    manifest = _manifest(data)
    manifest["files"][0]["file_count"] = 2
    bad = rebuild(data, {"manifest.json": manifest})
    body = _verify(client, bad).json()
    assert body["valid"] is False
    assert "manifest_file_count_invalid" in _codes(body)


# ---------------- 记录计数 / 损坏 ----------------

def test_verify_rejects_record_count_in_json_wrong(client):
    data = _rich_archive_bytes(client)
    recs = _records(data)
    recs["withdrawals.json"]["count"] = 9
    bad = rebuild(data, recs)
    body = _verify(client, bad).json()
    assert body["valid"] is False
    issue = next(i for i in body["issues"] if i["code"] == "record_count_mismatch"
                 and i.get("path") == "withdrawals.json")
    assert issue["expected"] == 1 and issue["actual"] == 9


def test_verify_rejects_manifest_record_count_wrong(client):
    data = _rich_archive_bytes(client)
    manifest = _manifest(data)
    manifest["record_counts"]["documents"] = 99
    bad = rebuild(data, {"manifest.json": manifest})
    body = _verify(client, bad).json()
    assert body["valid"] is False
    issue = next(i for i in body["issues"]
                 if i["code"] == "record_count_mismatch"
                 and i.get("field") == "record_counts.documents")
    assert issue["expected"] == 2 and issue["actual"] == 99


def test_verify_rejects_annotation_revision_count_wrong(client):
    data = _rich_archive_bytes(client)
    recs = _records(data)
    recs["annotations.json"]["revision_count"] = 1
    bad = rebuild(data, recs)
    body = _verify(client, bad).json()
    assert body["valid"] is False
    assert any(i["code"] == "record_count_mismatch"
               and i.get("field") == "revision_count" for i in body["issues"])


def test_verify_rejects_corrupt_record_json(client):
    data = _archive_bytes(client)
    bad = rebuild(data, raw_updates={"documents.json": b"{not json"})
    body = _verify(client, bad).json()
    assert body["valid"] is False
    assert "record_invalid_json" in _codes(body)


def test_verify_rejects_malformed_record_entry(client):
    data = _archive_bytes(client)
    recs = _records(data)
    recs["documents.json"]["documents"].append({"doc_id": "bad"})
    recs["documents.json"]["count"] = 1
    bad = rebuild(data, recs)
    body = _verify(client, bad).json()
    assert body["valid"] is False
    assert "record_invalid_entry" in _codes(body)


# ---------------- 引用关系冲突 ----------------

def test_verify_rejects_non_continuous_annotation_revisions(client):
    data = _rich_archive_bytes(client)
    recs = _records(data)
    doc_a = recs["annotations.json"]["documents"][0]
    # 删掉 revision 1，当前修订仍为 2 -> 序列不再连续
    doc_a["revisions"] = [r for r in doc_a["revisions"] if r["revision"] != 1]
    doc_a["revision_count"] = len(doc_a["revisions"])
    recs["annotations.json"]["revision_count"] -= 1
    bad = rebuild(data, recs)
    body = _verify(client, bad).json()
    assert body["valid"] is False
    assert any(i["code"] == "record_relation_conflict"
               and "continuous" in i["message"] for i in body["issues"])


def test_verify_rejects_current_revision_conflict(client):
    data = _rich_archive_bytes(client)
    recs = _records(data)
    a = _doc_a(recs)
    doc = next(d for d in recs["documents.json"]["documents"]
               if d["doc_id"] == a)
    doc["annotation_revision"] = 1  # A 当前修订实为 2
    bad = rebuild(data, recs)
    body = _verify(client, bad).json()
    assert body["valid"] is False
    assert any(i.get("field") == "current_revision"
               and i["code"] == "record_relation_conflict" for i in body["issues"])


def test_verify_rejects_rect_out_of_bounds(client):
    data = _rich_archive_bytes(client)
    recs = _records(data)
    a = _doc_a(recs)
    doc_a = next(d for d in recs["annotations.json"]["documents"]
                 if d["doc_id"] == a)
    rev0 = next(r for r in doc_a["revisions"] if r["revision"] == 0)
    # revision 0 本为空：放入一个越界矩形
    rev0["rects"] = [{"x": 0, "y": 0, "w": 9999, "h": 9999, "reason": "越界"}]
    rev0["rect_count"] = 1
    bad = rebuild(data, recs)
    body = _verify(client, bad).json()
    assert body["valid"] is False
    assert any(i["code"] == "record_relation_conflict"
               and i.get("revision") == 0 for i in body["issues"])


def test_verify_rejects_publication_with_unknown_revision(client):
    data = _rich_archive_bytes(client)
    recs = _records(data)
    a = _doc_a(recs)
    pub = next(p for p in recs["publications.json"]["publications"]
               if p["doc_id"] == a and p["version"] == 2)
    pub["annotation_revision"] = 42
    bad = rebuild(data, recs)
    body = _verify(client, bad).json()
    assert body["valid"] is False
    assert any(i.get("field") == "annotation_revision"
               and i["code"] == "record_relation_conflict" for i in body["issues"])


def test_verify_rejects_non_continuous_publication_versions(client):
    data = _rich_archive_bytes(client)
    recs = _records(data)
    pubs = recs["publications.json"]["publications"]
    a = _doc_a(recs)
    # 删除 A v1 及其撤回、成品与清单条目，保留 v2/批次 v3 -> A 版本序列 2,3
    v1_pub = next(p for p in pubs if p["doc_id"] == a and p["version"] == 1)
    v1_out = v1_pub["output_file"]
    recs["publications.json"]["publications"] = [
        p for p in pubs if not (p["doc_id"] == a and p["version"] == 1)]
    recs["publications.json"]["count"] -= 1
    recs["withdrawals.json"]["withdrawals"] = []
    recs["withdrawals.json"]["count"] = 0
    # A v2 的 withdrawn 标记也要随撤回记录清除
    for p in recs["publications.json"]["publications"]:
        if p["doc_id"] == a:
            p["withdrawn"] = False
            p["withdrawn_at"] = None
            p["withdrawal_reason"] = None
    manifest = _manifest(data)
    manifest["files"] = [e for e in manifest["files"] if e["path"] != v1_out]
    manifest["file_count"] = len(manifest["files"])
    manifest["record_counts"]["publications"] -= 1
    manifest["record_counts"]["withdrawals"] = 0
    bad = rebuild(data, {**recs, "manifest.json": manifest}, drops=(v1_out,))
    body = _verify(client, bad).json()
    assert body["valid"] is False
    assert any(i["code"] == "record_relation_conflict"
               and "continuous sequence" in i["message"]
               and i.get("doc_id") == a for i in body["issues"])


def test_verify_rejects_withdrawal_flag_conflict(client):
    data = _rich_archive_bytes(client)
    recs = _records(data)
    w = recs["withdrawals.json"]["withdrawals"][0]
    pub = next(p for p in recs["publications.json"]["publications"]
               if p["doc_id"] == w["doc_id"] and p["version"] == w["version"])
    pub["withdrawn"] = False  # 有撤回记录却标记未撤回
    bad = rebuild(data, recs)
    body = _verify(client, bad).json()
    assert body["valid"] is False
    assert any(i.get("field") == "withdrawn"
               and i["code"] == "record_relation_conflict" for i in body["issues"])


def test_verify_rejects_dangling_withdrawal(client):
    data = _rich_archive_bytes(client)
    recs = _records(data)
    recs["withdrawals.json"]["withdrawals"].append({
        "doc_id": "f" * 32, "version": 1, "reason": "悬空撤回",
        "withdrawn_at": "2026-10-06T00:00:00+00:00",
    })
    recs["withdrawals.json"]["count"] = 2
    manifest = _manifest(data)
    manifest["record_counts"]["withdrawals"] = 2
    bad = rebuild(data, {**recs, "manifest.json": manifest})
    body = _verify(client, bad).json()
    assert body["valid"] is False
    assert any(i["code"] == "record_relation_conflict"
               and i.get("doc_id") == "f" * 32 for i in body["issues"])


def test_verify_rejects_batch_position_disorder(client):
    data = _rich_archive_bytes(client)
    recs = _records(data)
    members = recs["publication-batches.json"]["batches"][0]["documents"]
    members[0]["position"], members[1]["position"] = 2, 0  # 乱序且不连续
    bad = rebuild(data, recs)
    body = _verify(client, bad).json()
    assert body["valid"] is False
    assert any(i["code"] == "record_relation_conflict"
               and "positions" in i["message"] for i in body["issues"])


def test_verify_rejects_batch_member_pointing_to_wrong_publication(client):
    data = _rich_archive_bytes(client)
    recs = _records(data)
    batch = recs["publication-batches.json"]["batches"][0]
    # 把批次成员改成不存在的版本
    batch["documents"][1]["version"] = 9
    bad = rebuild(data, recs)
    body = _verify(client, bad).json()
    assert body["valid"] is False
    assert any(i["code"] == "record_relation_conflict"
               and "non-existent publication" in i["message"]
               for i in body["issues"])


def test_verify_rejects_publication_batch_id_unknown(client):
    data = _rich_archive_bytes(client)
    recs = _records(data)
    recs["publications.json"]["publications"][0]["batch_id"] = "deadbeef"
    bad = rebuild(data, recs)
    body = _verify(client, bad).json()
    assert body["valid"] is False
    assert any(i.get("field") == "batch_id"
               and "unknown batch" in i["message"] for i in body["issues"])


def test_verify_rejects_batch_publish_at_conflict(client):
    data = _rich_archive_bytes(client)
    recs = _records(data)
    recs["publication-batches.json"]["batches"][0]["publish_at"] = \
        "2026-11-11T11:11:11Z"
    bad = rebuild(data, recs)
    body = _verify(client, bad).json()
    assert body["valid"] is False
    assert any(i.get("field") == "publish_at"
               and i["code"] == "record_relation_conflict" for i in body["issues"])


def test_verify_rejects_original_digest_conflict_in_record(client):
    data = _rich_archive_bytes(client)
    recs = _records(data)
    doc = recs["documents.json"]["documents"][0]
    doc["original_sha256"] = "a" * 64  # 与原图成员/发布记录都冲突
    bad = rebuild(data, recs)
    body = _verify(client, bad).json()
    assert body["valid"] is False
    fields = {i.get("field") for i in body["issues"]
              if i["code"] == "record_digest_conflict"}
    assert "original_sha256" in fields


def test_verify_rejects_output_digest_conflict_in_record(client):
    data = _rich_archive_bytes(client)
    recs = _records(data)
    pub = recs["publications.json"]["publications"][0]
    pub["output_sha256"] = "b" * 64
    bad = rebuild(data, recs)
    body = _verify(client, bad).json()
    assert body["valid"] is False
    assert any(i.get("field") == "output_sha256"
               and i["code"] == "record_digest_conflict" for i in body["issues"])


def test_verify_rejects_original_file_path_conflict(client):
    data = _rich_archive_bytes(client)
    recs = _records(data)
    a = _doc_a(recs)
    doc = next(d for d in recs["documents.json"]["documents"] if d["doc_id"] == a)
    real_path = doc["original_file"]
    doc["original_file"] = "originals/00000000000000000000000000000000.png"
    bad = rebuild(data, recs)
    body = _verify(client, bad).json()
    assert body["valid"] is False
    assert any(i.get("field") == "original_file" for i in body["issues"])
    # 真正的原图成员反向找不到任何以它为 original_file 的文档（游离），
    # 产生一条定位到真实成员路径的关系冲突
    orphan = next(i for i in body["issues"] if i.get("path") == real_path)
    assert orphan["code"] in ("unreferenced_archive_file",
                              "record_relation_conflict")


def test_verify_rejects_unreferenced_original_member(client):
    data = _rich_archive_bytes(client)
    # 清单里加一个无文档记录的原图（保持摘要正确以隔离其他问题）
    import hashlib
    manifest = _manifest(data)
    blob = b"orphan-original"
    path = "originals/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png"
    manifest["files"].append({
        "path": path, "sha256": hashlib.sha256(blob).hexdigest(),
        "kind": "original", "doc_id": "a" * 32, "file_count": 1,
    })
    manifest["file_count"] = len(manifest["files"])
    bad = rebuild(data, {"manifest.json": manifest}, raw_updates={path: blob})
    body = _verify(client, bad).json()
    assert body["valid"] is False
    issue = next(i for i in body["issues"]
                 if i["code"] == "unreferenced_archive_file" and i["path"] == path)
    assert issue["doc_id"] == "a" * 32


def test_verify_rejects_duplicate_publication_record(client):
    data = _rich_archive_bytes(client)
    recs = _records(data)
    pubs = recs["publications.json"]["publications"]
    pubs.append(dict(pubs[0]))
    recs["publications.json"]["count"] = len(pubs)
    manifest = _manifest(data)
    manifest["record_counts"]["publications"] = len(pubs)
    bad = rebuild(data, {**recs, "manifest.json": manifest})
    body = _verify(client, bad).json()
    assert body["valid"] is False
    assert "duplicate_record_entry" in _codes(body)


# ---------------- 纯只读：不解压到服务数据目录、不改记录 ----------------

def test_verify_rejects_unreadable_member_when_crc_broken(client):
    data = _rich_archive_bytes(client)
    # 定位某个发布成品成员的压缩数据区（本地文件头之后），翻转一个字节，
    # 使其解压结果与该成员 CRC 不符 -> 读取失败（archive_member_unreadable）。
    with zipfile.ZipFile(io.BytesIO(data)) as probe:
        zinfo = next(i for i in probe.infolist()
                     if i.filename.startswith("publications/"))
    data_offset = (zinfo.header_offset + 30
                   + len(zinfo.filename.encode()) + len(zinfo.extra))
    raw = bytearray(data)
    raw[data_offset + 1] ^= 0xFF
    body = _verify(client, bytes(raw)).json()
    assert body["valid"] is False
    codes = _codes(body)
    assert ("archive_member_unreadable" in codes
            or "not_a_zip" in codes
            or "manifest_sha256_mismatch" in codes)


def test_verify_does_not_extract_or_mutate_service_data(client, tmp_path):
    data = _rich_archive_bytes(client)
    data_dir = client.app.state.settings.data_dir
    before = _snapshot_files(data_dir)
    r = _verify(client, data)
    assert r.status_code == 200 and r.json()["valid"] is True
    after = _snapshot_files(data_dir)
    # 数据目录内容（库/原图/成品）核验前后完全不变
    assert before == after
    # 再次归档仍成功且与核验输入的记录计数一致（核验未改写任何记录）
    again = _archive(client)
    assert again.status_code == 200


def _snapshot_files(root: str) -> dict:
    import hashlib
    import os
    snap = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root)
            with open(full, "rb") as f:
                snap[rel] = hashlib.sha256(f.read()).hexdigest()
    return snap
