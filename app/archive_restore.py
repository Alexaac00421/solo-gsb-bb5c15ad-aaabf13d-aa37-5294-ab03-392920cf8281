"""服务数据归档恢复计划：把**核验通过**的归档 ZIP 解析为记录与文件写入计划。

本模块只做**纯解析**：从 ZIP 读取五类记录 JSON、按归档相对路径列出引用文件，
不打开数据库、不写任何文件、不触碰 ``DATA_DIR``。调用方（路由）必须先通过
:mod:`app.archive_verify` 的完整核验，再在同一个写事务内完成空实例判定、
文件落盘与记录写入；本模块的输出即是该写事务的输入。

恢复语义（与归档端 :mod:`app.main` 的产物结构对应）：

- 文档、各修订标注、发布、批次与撤回记录原样保留 ID、版本号、摘要与时间；
- 原图/成品按归档相对路径（``originals/``、``publications/``）在当前
  ``DATA_DIR`` 下重建本机路径；
- 归档不包含批次幂等键（``batch_idempotency``），恢复**不推断**旧键。
"""
from __future__ import annotations

import json
import zipfile
from typing import Any

# 与归档端/核验端一致的五类记录文件（顶层相对路径）
RECORD_FILES: tuple[str, ...] = (
    "documents.json",
    "annotations.json",
    "publications.json",
    "publication-batches.json",
    "withdrawals.json",
)


def load_restore_plan(zip_path: str) -> dict[str, Any]:
    """从核验通过的归档 ZIP 读出全部记录与引用文件清单（纯数据，无副作用）。

    返回::

        {
          "documents": [...],        # documents.json -> documents
          "annotation_docs": [...],  # annotations.json -> documents（含 revisions）
          "publications": [...],     # publications.json -> publications
          "batches": [...],          # publication-batches.json -> batches
          "withdrawals": [...],      # withdrawals.json -> withdrawals
          "files": [                 # 记录引用的文件成员（由记录字段推导）
            {"path": "originals/<doc_id>.png", "kind": "original",
             "doc_id": ...},
            {"path": "publications/<doc_id>/v<n>.png", "kind": "publication",
             "doc_id": ..., "version": ...},
          ],
        }

    文件清单由记录中的 ``original_file``/``output_file`` 推导：核验已保证这些
    相对路径合法、与清单一一对应且摘要匹配，因此恢复按记录路径取成员即可。
    """
    with zipfile.ZipFile(zip_path) as zf:
        records = {
            name: json.loads(zf.read(name).decode("utf-8"))
            for name in RECORD_FILES
        }
    documents = records["documents.json"]["documents"]
    publications = records["publications.json"]["publications"]
    files: list[dict[str, Any]] = []
    for d in documents:
        files.append(
            {"path": d["original_file"], "kind": "original",
             "doc_id": d["doc_id"]}
        )
    for p in publications:
        files.append(
            {"path": p["output_file"], "kind": "publication",
             "doc_id": p["doc_id"], "version": p["version"]}
        )
    return {
        "documents": documents,
        "annotation_docs": records["annotations.json"]["documents"],
        "publications": publications,
        "batches": records["publication-batches.json"]["batches"],
        "withdrawals": records["withdrawals.json"]["withdrawals"],
        "files": files,
    }
