"""服务数据归档恢复：把核验通过的归档 ZIP 解析为可落库的结构化负载。

本模块只做"解析 + 本机路径规划"，不做落盘/写库：

- 从归档 ZIP 读取五类记录 JSON（documents/annotations/publications/
  publication-batches/withdrawals），重建各表行——**ID、版本、摘要与时间
  原样保留**，本机文件路径（``original_path``/``output_path``）按当前
  数据目录重新计算；
- 给出引用文件（原图/发布成品）的落盘计划：归档成员 -> 本机绝对路径；
- 批次幂等键（``batch_idempotency``）不在归档内容中，**绝不推断恢复**。

调用前归档必须已通过 :mod:`app.archive_verify` 的完整核验；本模块对其
消费的字段仍做最小自洽校验（缺失/类型不符即抛
:class:`RestorePayloadInvalid`），绝不静默写入残缺记录。恢复编排
（上传暂存、核验、空实例判定、写事务、文件落盘与失败清理）在
:mod:`app.main` 的路由中完成。
"""
from __future__ import annotations

import json
import os
import zipfile
from dataclasses import dataclass
from typing import Any

from .config import Settings

# 与归档端一致的 media_type -> 扩展名映射（扩展名只由库内 media_type 决定，
# 绝不取自上传文件名）
MEDIA_TYPE_EXT = {"image/png": ".png", "image/jpeg": ".jpg"}


class RestorePayloadInvalid(Exception):
    """归档记录缺少恢复必需字段或类型不符（现有核验未覆盖的字段）。

    ``issues`` 与核验结论同形态：``[{"code", "message", ...定位字段}]``，
    路由据此返回 ``400 invalid_archive``，不写入任何记录或文件。
    """

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        super().__init__(issues[0]["message"] if issues else "invalid archive")
        self.issues = issues


@dataclass
class RestoreFile:
    """一个待落盘的引用文件（原图或发布成品）。"""

    archive_member: str  # 归档 ZIP 内相对路径
    target_relpath: str  # 数据目录内相对路径（错误定位用，不泄露绝对路径）
    target_path: str     # 本机绝对路径（排他创建的目标）


@dataclass
class RestorePayload:
    """恢复所需的全部记录行与文件落盘计划。"""

    documents: list[dict]          # documents 表行（original_path 已重建）
    annotation_rects: list[tuple]  # annotation_rects 表行
    publications: list[dict]       # publications 表行（output_path 已重建）
    batches: list[dict]            # publication_batches 表行
    batch_members: list[dict]      # batch_publications 表行
    withdrawals: list[dict]        # publication_withdrawals 表行
    files: list[RestoreFile]       # 原图 + 成品落盘计划（与记录一一对应）
    counts: dict[str, int]         # 各类恢复计数（响应用）


def _invalid(message: str, **where: Any) -> None:
    issue: dict[str, Any] = {
        "code": "record_invalid_entry", "message": message,
    }
    issue.update(where)
    raise RestorePayloadInvalid([issue])


def _need_str(obj: Any, field_name: str, **where: Any) -> str:
    if not isinstance(obj, dict) or not isinstance(obj.get(field_name), str):
        _invalid(
            f"record entry is missing a string field '{field_name}'",
            field=field_name, **where,
        )
    return obj[field_name]


def _need_opt_str(obj: Any, field_name: str, **where: Any) -> str | None:
    value = obj.get(field_name) if isinstance(obj, dict) else None
    if value is not None and not isinstance(value, str):
        _invalid(
            f"record entry field '{field_name}' must be a string or null",
            field=field_name, **where,
        )
    return value


def _need_int(obj: Any, field_name: str, **where: Any) -> int:
    value = obj.get(field_name) if isinstance(obj, dict) else None
    if not isinstance(value, int) or isinstance(value, bool):
        _invalid(
            f"record entry is missing an integer field '{field_name}'",
            field=field_name, **where,
        )
    return value


def _need_list(obj: Any, field_name: str, **where: Any) -> list:
    value = obj.get(field_name) if isinstance(obj, dict) else None
    if not isinstance(value, list):
        _invalid(
            f"record entry is missing a list field '{field_name}'",
            field=field_name, **where,
        )
    return value


def _read_record(zf: zipfile.ZipFile, name: str) -> dict:
    try:
        payload = json.loads(zf.read(name).decode("utf-8"))
    except Exception as exc:
        _invalid(f"record file '{name}' cannot be parsed: {exc}", path=name)
    if not isinstance(payload, dict):
        _invalid(f"record file '{name}' must be a JSON object", path=name)
    return payload


def build_restore_payload(
    zf: zipfile.ZipFile, settings: Settings
) -> RestorePayload:
    """把核验通过的归档 ZIP 解析为恢复负载（记录行 + 文件落盘计划）。

    只读取 ZIP、不触碰数据库与数据目录；本机文件路径按 ``settings`` 的
    数据目录重建（``originals/{doc_id}{ext}``、
    ``publications/{doc_id}/v{version}.png``），扩展名只由记录中的
    media_type 决定。
    """
    docs_payload = _read_record(zf, "documents.json")
    anns_payload = _read_record(zf, "annotations.json")
    pubs_payload = _read_record(zf, "publications.json")
    batches_payload = _read_record(zf, "publication-batches.json")
    withdrawals_payload = _read_record(zf, "withdrawals.json")

    files: list[RestoreFile] = []

    # ---- 文档（含本机原图路径重建）----
    documents: list[dict] = []
    for i, d in enumerate(
        _need_list(docs_payload, "documents", path="documents.json")
    ):
        where = {"path": "documents.json", "index": i}
        doc_id = _need_str(d, "doc_id", **where)
        media_type = _need_str(d, "media_type", **where)
        ext = MEDIA_TYPE_EXT.get(media_type)
        if ext is None:
            _invalid(
                f"document '{doc_id}' has an unsupported media_type"
                f" {media_type!r}",
                field="media_type", doc_id=doc_id, **where,
            )
        member = f"originals/{doc_id}{ext}"
        documents.append(
            {
                "id": doc_id,
                "filename": _need_str(d, "filename", **where),
                "media_type": media_type,
                "original_path": os.path.abspath(
                    os.path.join(settings.originals_dir, doc_id + ext)
                ),
                "original_sha256": _need_str(d, "original_sha256", **where),
                "width": _need_int(d, "width", **where),
                "height": _need_int(d, "height", **where),
                "annotation_revision": _need_int(
                    d, "annotation_revision", **where
                ),
                "created_at": _need_str(d, "created_at", **where),
            }
        )
        files.append(
            RestoreFile(member, member, documents[-1]["original_path"])
        )

    # ---- 各修订标注（矩形顺序即 idx）----
    annotation_rects: list[tuple] = []
    revision_total = 0
    for i, ad in enumerate(
        _need_list(anns_payload, "documents", path="annotations.json")
    ):
        where = {"path": "annotations.json", "index": i}
        doc_id = _need_str(ad, "doc_id", **where)
        revisions = _need_list(ad, "revisions", **where)
        revision_total += len(revisions)
        for j, rev in enumerate(revisions):
            rwhere = {**where, "revision_index": j}
            revision = _need_int(rev, "revision", **rwhere)
            for idx, rect in enumerate(_need_list(rev, "rects", **rwhere)):
                annotation_rects.append(
                    (
                        doc_id,
                        revision,
                        idx,
                        _need_int(rect, "x", **rwhere),
                        _need_int(rect, "y", **rwhere),
                        _need_int(rect, "w", **rwhere),
                        _need_int(rect, "h", **rwhere),
                        _need_str(rect, "reason", **rwhere),
                    )
                )

    # ---- 发布（含本机成品路径重建）----
    publications: list[dict] = []
    pubs_by_pair: dict[tuple[str, int], dict] = {}
    for i, p in enumerate(
        _need_list(pubs_payload, "publications", path="publications.json")
    ):
        where = {"path": "publications.json", "index": i}
        doc_id = _need_str(p, "doc_id", **where)
        version = _need_int(p, "version", **where)
        member = f"publications/{doc_id}/v{version}.png"
        row = {
            "doc_id": doc_id,
            "version": version,
            "annotation_revision": _need_int(
                p, "annotation_revision", **where
            ),
            "original_sha256": _need_str(p, "original_sha256", **where),
            "output_sha256": _need_str(p, "output_sha256", **where),
            "output_path": os.path.abspath(
                os.path.join(
                    settings.publications_dir, doc_id, f"v{version}.png"
                )
            ),
            "created_at": _need_str(p, "created_at", **where),
            "batch_id": _need_opt_str(p, "batch_id", **where),
            "publish_at": _need_opt_str(p, "publish_at", **where),
        }
        publications.append(row)
        pubs_by_pair[(doc_id, version)] = row
        files.append(RestoreFile(member, member, row["output_path"]))

    # ---- 批次与成员（成员行的摘要/修订号取自对应发布记录）----
    batches: list[dict] = []
    batch_members: list[dict] = []
    for i, b in enumerate(
        _need_list(batches_payload, "batches", path="publication-batches.json")
    ):
        where = {"path": "publication-batches.json", "index": i}
        batch_id = _need_str(b, "batch_id", **where)
        batches.append(
            {
                "id": batch_id,
                "created_at": _need_str(b, "created_at", **where),
                "publish_at": _need_opt_str(b, "publish_at", **where),
            }
        )
        for m in _need_list(b, "documents", **where):
            doc_id = _need_str(m, "doc_id", **where)
            version = _need_int(m, "version", **where)
            pub = pubs_by_pair.get((doc_id, version))
            if pub is None:
                _invalid(
                    f"batch '{batch_id}' member {doc_id} v{version} references"
                    " a non-existent publication",
                    doc_id=doc_id, version=version, **where,
                )
            batch_members.append(
                {
                    "batch_id": batch_id,
                    "position": _need_int(m, "position", **where),
                    "doc_id": doc_id,
                    "version": version,
                    "annotation_revision": pub["annotation_revision"],
                    "original_sha256": pub["original_sha256"],
                    "output_sha256": pub["output_sha256"],
                }
            )

    # ---- 撤回记录 ----
    withdrawals: list[dict] = []
    for i, w in enumerate(
        _need_list(withdrawals_payload, "withdrawals", path="withdrawals.json")
    ):
        where = {"path": "withdrawals.json", "index": i}
        withdrawals.append(
            {
                "doc_id": _need_str(w, "doc_id", **where),
                "version": _need_int(w, "version", **where),
                "reason": _need_str(w, "reason", **where),
                "withdrawn_at": _need_str(w, "withdrawn_at", **where),
            }
        )

    counts = {
        "documents": len(documents),
        "annotation_revisions": revision_total,
        "publications": len(publications),
        "publication_batches": len(batches),
        "withdrawals": len(withdrawals),
        "original_files": len(documents),
        "publication_files": len(publications),
    }
    return RestorePayload(
        documents=documents,
        annotation_rects=annotation_rects,
        publications=publications,
        batches=batches,
        batch_members=batch_members,
        withdrawals=withdrawals,
        files=files,
        counts=counts,
    )
