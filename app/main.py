"""HTTP API：上传、标注（乐观锁修订）、发布（事务内涂实）、撤回、公开读取、
离线交付包与服务数据归档（同一数据库快照）。"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import BackgroundTasks, Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from .archive_verify import verify_archive_path
from .config import Settings
from .db import init_db, read_tx, snapshot_tx, write_tx
from .imaging import (
    SUPPORTED_FORMATS,
    ImageDecodeError,
    UnsupportedFormatError,
    coverage_diff,
    load_oriented,
    render_publication,
    scale_rect,
)
from .models import (
    AnnotationsPut,
    BatchPublishRequest,
    CoverageDiffRequest,
    MigrationRequest,
    PackageRequest,
    PublishRequest,
    WithdrawRequest,
)

MEDIA_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg"}

# 定时公开的最大提前量：首次提交时公开时间不得晚于当前时刻 30 天
MAX_PUBLISH_DELAY = timedelta(days=30)

# 离线交付包"资格确认完毕（仍在写锁/写事务内）-> 读盘打包"之间的钩子。
# 生产中为空操作；并发测试借此在资格确认后插入撤回，验证串行顺序。
PACKAGE_ELIGIBILITY_HOOK: Any = None

# 遮盖范围比较"当前修订号校验完毕（读快照已建立）-> 读取矩形并比较"之间
# 的钩子。生产中为空操作；并发测试借此在同一只读快照事务内、矩形读取之前
# 提交标注修改，验证比较全程使用同一读取快照（不看到提交后的新标注）。
COVERAGE_DIFF_SNAPSHOT_HOOK: Any = None

# 服务数据归档"同一读快照内的记录读取与全部引用文件校验完毕 -> 写 ZIP"
# 之间的钩子。生产中为空操作；并发测试借此在归档期间提交标注/发布等写入，
# 验证归档全程对应同一个一致时点（不看到提交后的新数据）且不阻塞并发写入。
ARCHIVE_SNAPSHOT_HOOK: Any = None


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_dt() -> datetime:
    """当前时刻（UTC，aware）。单独成函数便于测试注入固定时钟。"""
    return datetime.now(timezone.utc)


def _stored_publish_at_dt(value: str) -> datetime:
    """解析库内规范化存储的公开时间（UTC ISO 8601，尾部 Z）。"""
    return datetime.fromisoformat(value)


def _parse_publish_at(raw: str) -> tuple[datetime, str]:
    """解析调用方指定的公开时间。

    仅接受带时区偏移的 RFC 3339/ISO 8601 字符串（尾部 ``Z`` 等同 +00:00），
    统一换算为 UTC；返回 (aware UTC datetime, 规范化时间字符串)。
    无法解析或为朴素时间（无偏移）均属无效时间，调用方据此返回 400。
    """
    text = raw.strip()
    if not text:
        _error(
            400,
            "invalid_publish_time",
            "publish_at must be an RFC 3339 / ISO 8601 date-time with a"
            " timezone offset (UTC recommended), e.g."
            " 2026-10-06T08:00:00Z; use null or omit the field to publish"
            " immediately",
        )
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        _error(
            400,
            "invalid_publish_time",
            "publish_at must be an RFC 3339 / ISO 8601 date-time with a"
            " timezone offset (UTC recommended), e.g."
            " 2026-10-06T08:00:00Z",
        )
    if dt.tzinfo is None or dt.utcoffset() is None:
        _error(
            400,
            "invalid_publish_time",
            "publish_at must carry a timezone offset; a naive local time is"
            " ambiguous, use e.g. 2026-10-06T08:00:00Z",
        )
    dt_utc = dt.astimezone(timezone.utc)
    # 规范化为统一的 UTC 文本（尾部 Z）；Python 3.11 的 fromisoformat 可直接读回
    normalized = dt_utc.isoformat().replace("+00:00", "Z")
    return dt_utc, normalized


def _check_publish_window(dt_utc: datetime) -> None:
    """首次提交的窗口校验：必须晚于当前时刻，且不超过 30 天。

    幂等键重放路径不调用本函数——首次结果原样返回，即使公开时间已过也不
    重新校验。
    """
    now = _now_dt()
    if dt_utc <= now:
        _error(
            400,
            "invalid_publish_time",
            "publish_at must be later than the current time",
        )
    if dt_utc - now > MAX_PUBLISH_DELAY:
        _error(
            400,
            "invalid_publish_time",
            "publish_at must be no more than 30 days in the future",
        )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _batch_request_hash(items: Any, publish_at: str | None) -> str:
    """幂等键绑定的内容摘要：按请求顺序排列的 (doc_id, expected_revision)
    列表，加上整批统一的公开时间（含"立即公开"的 null）。"""
    canonical = json.dumps(
        {
            "documents": [
                {"doc_id": it.doc_id, "expected_revision": it.expected_revision}
                for it in items
            ],
            "publish_at": publish_at,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cleanup_tempfile(path: str) -> None:
    """流式响应结束后删除临时 ZIP；任何失败都不影响已完成的交付。"""
    try:
        os.unlink(path)
    except OSError:
        pass


# 原图归档相对路径的扩展名只由库内 media_type 映射，绝不取自上传文件名
# （上传文件名可能携带路径片段或非 ASCII 字符）；media_type 仅两种合法值。
_ARCHIVE_ORIGINAL_EXT = {"image/png": ".png", "image/jpeg": ".jpg"}


def _archive_original_relpath(doc: Any) -> str:
    """原图在归档 ZIP 内的相对路径：``originals/{doc_id}{ext}``。

    doc_id 是服务端生成的 uuid hex，扩展名只由库内登记的 media_type 决定，
    因此该相对路径不可能包含目录穿越片段或上传文件名。
    """
    ext = _ARCHIVE_ORIGINAL_EXT.get(doc["media_type"])
    if ext is None:
        _error(
            500,
            "archive_unsupported_media_type",
            f"document '{doc['id']}' has an unsupported archived media type"
            f" {doc['media_type']!r}",
            doc_id=doc["id"],
        )
    return f"originals/{doc['id']}{ext}"


def _read_and_verify_archive_file(
    path: str,
    expected_sha256: str,
    *,
    kind: str,
    missing_code: str,
    read_code: str,
    corrupt_code: str,
    doc_id: str,
    version: int | None = None,
) -> tuple[bytes, str]:
    """读取归档引用文件（原图或发布成品）并复算 SHA-256。

    归档遵循"全有或全无"：文件缺失、无法读取或实际摘要与快照记录不符时
    抛出统一业务错误（调用方向上传播即整单拒绝），绝不返回部分数据。
    返回 (文件字节, 实际摘要)。
    """
    ident = {"doc_id": doc_id}
    if version is not None:
        ident["version"] = version
    if not os.path.exists(path):
        _error(
            500,
            missing_code,
            f"{kind} referenced by the snapshot for document '{doc_id}'"
            + (f" version {version}" if version is not None else "")
            + " is missing on disk",
            **ident,
        )
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as exc:
        _error(
            500,
            read_code,
            f"failed to read {kind} for document '{doc_id}'"
            + (f" version {version}" if version is not None else "")
            + f": {exc}",
            **ident,
        )
    actual_sha = _sha256_bytes(data)
    if actual_sha != expected_sha256:
        _error(
            500,
            corrupt_code,
            f"{kind} for document '{doc_id}'"
            + (f" version {version}" if version is not None else "")
            + " failed integrity check against the snapshot",
            **ident,
        )
    return data, actual_sha


def _error(status: int, code: str, message: str, **extra: Any) -> None:
    detail = {"code": code, "message": message}
    detail.update(extra)
    raise HTTPException(status_code=status, detail=detail)


def _rect_out(row: Any) -> dict:
    return {
        "x": row["x"],
        "y": row["y"],
        "w": row["w"],
        "h": row["h"],
        "reason": row["reason"],
    }


def _pub_entry(row: Any, keys: list[str]) -> dict:
    """发布记录条目：原字段按序输出，末尾依次追加公开时间与撤回状态字段。

    查询须以列别名带出 p_at / w_at / w_reason（见各列表端点）。
    """
    out = {k: row[k] for k in keys}
    out["publish_at"] = row["p_at"]
    out["withdrawn"] = row["w_at"] is not None
    out["withdrawn_at"] = row["w_at"]
    out["withdrawal_reason"] = row["w_reason"]
    return out


# 列表查询共用的撤回状态联表片段
_WITHDRAWAL_JOIN = (
    " LEFT JOIN publication_withdrawals w"
    " ON w.doc_id = {t}.doc_id AND w.version = {t}.version"
)
_WITHDRAWAL_COLS = ", w.withdrawn_at AS w_at, w.reason AS w_reason"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        os.makedirs(settings.originals_dir, exist_ok=True)
        os.makedirs(settings.publications_dir, exist_ok=True)
        init_db(settings.db_path)
        yield

    app = FastAPI(
        title="档案影像脱敏发布服务",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.settings = settings

    def _get_doc(conn: Any, doc_id: str) -> Any:
        row = conn.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if row is None:
            _error(404, "document_not_found", f"document '{doc_id}' does not exist")
        return row

    def _get_publication(
        conn: Any,
        doc_id: str,
        version: int,
        position: int | None = None,
    ) -> Any:
        """取发布记录；未知文档/版本按既有的不存在错误返回。

        离线交付包场景传入 ``position`` 时在错误体上附请求下标，
        使整包拒绝的原因可以定位到具体条目；不附任何成品或遮盖详情。
        """
        pub = conn.execute(
            "SELECT * FROM publications WHERE doc_id = ? AND version = ?",
            (doc_id, version),
        ).fetchone()
        if pub is None:
            exists = conn.execute(
                "SELECT 1 FROM documents WHERE id = ?", (doc_id,)
            ).fetchone()
            extra = {"version": version}
            if position is not None:
                extra["position"] = position
            if exists is None:
                _error(
                    404,
                    "document_not_found",
                    f"document '{doc_id}' does not exist",
                    doc_id=doc_id,
                    **extra,
                )
            _error(
                404,
                "unknown_version",
                f"document '{doc_id}' has no publication version {version}",
                doc_id=doc_id,
                **extra,
            )
        return pub

    def _ensure_not_withdrawn(
        conn: Any,
        doc_id: str,
        version: int,
        position: int | None = None,
    ) -> None:
        """已撤回版本对公开面不可见（410），不返回成品与遮盖详情。

        撤回只登记记录、不删除成品文件：生效前已开始的下载因此能够
        完成，只有新发起的公开读取被拒绝。
        """
        row = conn.execute(
            "SELECT 1 FROM publication_withdrawals"
            " WHERE doc_id = ? AND version = ?",
            (doc_id, version),
        ).fetchone()
        if row is not None:
            extra: dict[str, Any] = {"doc_id": doc_id, "version": version}
            if position is not None:
                extra["position"] = position
            _error(
                410,
                "publication_withdrawn",
                f"document '{doc_id}' publication version {version} has been"
                " withdrawn and is no longer publicly available",
                **extra,
            )

    def _ensure_publish_time_reached(
        pub: Any, position: int | None = None
    ) -> None:
        """未到公开时间的版本对公开面表现为 404（与未知版本一致）。

        既不返回成品，也不返回遮盖坐标/原因、摘要等任何版本信息；
        公开时间到达后无需任何额外操作即可读取。调用前必须先完成
        撤回检查——永久撤回优先于公开时间，撤回版本一律 410。
        """
        publish_at = pub["publish_at"]
        if publish_at is not None and _stored_publish_at_dt(
            publish_at
        ) > _now_dt():
            extra: dict[str, Any] = {
                "doc_id": pub["doc_id"],
                "version": pub["version"],
            }
            if position is not None:
                extra["position"] = position
            _error(
                404,
                "publication_not_yet_public",
                f"publication is scheduled to become public at {publish_at}",
                **extra,
            )

    # ---------------- 基本 ----------------

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    # ---------------- 档案上传 ----------------

    @app.post("/documents", status_code=201)
    async def upload_document(file: UploadFile = File(...)) -> dict:
        data = await file.read()
        if not data:
            _error(400, "empty_upload", "uploaded file is empty")
        if len(data) > settings.max_upload_bytes:
            _error(
                413,
                "payload_too_large",
                f"file is {len(data)} bytes, limit is {settings.max_upload_bytes}",
            )
        try:
            oriented, fmt = load_oriented(data)
        except ImageDecodeError as exc:
            _error(400, "image_decode_failed", f"cannot decode image: {exc}")
        except UnsupportedFormatError as exc:
            _error(
                415,
                "unsupported_media_type",
                f"only PNG and JPEG are supported, got format: {exc}",
            )

        doc_id = uuid.uuid4().hex
        digest = _sha256_bytes(data)
        original_path = os.path.abspath(
            os.path.join(settings.originals_dir, doc_id + SUPPORTED_FORMATS[fmt])
        )
        with open(original_path, "wb") as f:
            f.write(data)

        width, height = oriented.size
        now = _utcnow()
        try:
            with write_tx(settings.db_path) as conn:
                conn.execute(
                    "INSERT INTO documents (id, filename, media_type, original_path,"
                    " original_sha256, width, height, annotation_revision, created_at)"
                    " VALUES (?,?,?,?,?,?,?,0,?)",
                    (
                        doc_id,
                        file.filename or "",
                        MEDIA_TYPES[fmt],
                        original_path,
                        digest,
                        width,
                        height,
                        now,
                    ),
                )
        except Exception:
            if os.path.exists(original_path):
                os.unlink(original_path)
            raise
        return {
            "doc_id": doc_id,
            "filename": file.filename or "",
            "media_type": MEDIA_TYPES[fmt],
            "width": width,
            "height": height,
            "original_sha256": digest,
            "annotation_revision": 0,
            "created_at": now,
        }

    @app.get("/documents/{doc_id}")
    def get_document(doc_id: str) -> dict:
        with read_tx(settings.db_path) as conn:
            doc = _get_doc(conn, doc_id)
            pubs = conn.execute(
                "SELECT p.version, p.annotation_revision, p.output_sha256,"
                " p.created_at, p.publish_at AS p_at" + _WITHDRAWAL_COLS +
                " FROM publications p" + _WITHDRAWAL_JOIN.format(t="p") +
                " WHERE p.doc_id = ? ORDER BY p.version",
                (doc_id,),
            ).fetchall()
        return {
            "doc_id": doc["id"],
            "filename": doc["filename"],
            "media_type": doc["media_type"],
            "width": doc["width"],
            "height": doc["height"],
            "original_sha256": doc["original_sha256"],
            "annotation_revision": doc["annotation_revision"],
            "created_at": doc["created_at"],
            "publications": [
                _pub_entry(
                    p, ["version", "annotation_revision", "output_sha256",
                        "created_at"]
                )
                for p in pubs
            ],
        }

    # ---------------- 标注（修订号乐观锁） ----------------

    @app.get("/documents/{doc_id}/annotations")
    def get_annotations(doc_id: str) -> dict:
        with read_tx(settings.db_path) as conn:
            doc = _get_doc(conn, doc_id)
            rects = conn.execute(
                "SELECT x, y, w, h, reason FROM annotation_rects"
                " WHERE doc_id = ? AND revision = ? ORDER BY idx",
                (doc_id, doc["annotation_revision"]),
            ).fetchall()
        return {
            "doc_id": doc_id,
            "revision": doc["annotation_revision"],
            "rects": [_rect_out(r) for r in rects],
        }

    @app.put("/documents/{doc_id}/annotations")
    def put_annotations(doc_id: str, body: AnnotationsPut) -> dict:
        with write_tx(settings.db_path) as conn:
            doc = _get_doc(conn, doc_id)
            current = doc["annotation_revision"]
            if body.revision != current:
                _error(
                    409,
                    "stale_revision",
                    f"annotation revision mismatch: expected {current},"
                    f" got {body.revision}; re-fetch and retry",
                    current_revision=current,
                )
            width, height = doc["width"], doc["height"]
            for i, r in enumerate(body.rects):
                if r.w <= 0 or r.h <= 0:
                    _error(
                        400,
                        "empty_rectangle",
                        f"rect #{i} has non-positive size (w={r.w}, h={r.h})",
                    )
                if r.x < 0 or r.y < 0 or r.x + r.w > width or r.y + r.h > height:
                    _error(
                        400,
                        "rectangle_out_of_bounds",
                        f"rect #{i} (x={r.x}, y={r.y}, w={r.w}, h={r.h}) exceeds"
                        f" image bounds {width}x{height}",
                    )
                if not r.reason.strip():
                    _error(
                        400,
                        "reason_required",
                        f"rect #{i} must carry a non-empty reason",
                    )
            new_revision = current + 1
            for idx, r in enumerate(body.rects):
                conn.execute(
                    "INSERT INTO annotation_rects"
                    " (doc_id, revision, idx, x, y, w, h, reason)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        doc_id,
                        new_revision,
                        idx,
                        r.x,
                        r.y,
                        r.w,
                        r.h,
                        r.reason.strip(),
                    ),
                )
            conn.execute(
                "UPDATE documents SET annotation_revision = ? WHERE id = ?",
                (new_revision, doc_id),
            )
        return {
            "doc_id": doc_id,
            "revision": new_revision,
            "rects": [r.model_dump() for r in body.rects],
        }

    # ---------------- 遮盖方案迁移（同版式、不同尺寸，整批原子） ----------------

    @app.post("/annotation-migrations")
    def migrate_annotations(body: MigrationRequest) -> dict:
        """把源文档当前非空标注按旋正后宽高分别缩放，整体替换到多份目标文档。

        全部校验（数量、目标去重、源不作为目标、源/目标存在性、源与各目标
        修订号匹配、源当前标注非空）与对各目标的"读-缩放-替换-修订号 +1"
        都在同一个 ``BEGIN IMMEDIATE`` 写事务内完成，因此与并发的标注修改、
        发布（单份/批次）、撤回/打包等持写锁的操作互斥串行：任一校验失败，
        所有目标标注保持原样、修订号不变；全部通过时各目标同时进入新修订。
        源标注只被读取，源与任何既有发布版均不改动；各目标的历史标注修订
        仍按 (doc_id, revision) 追加保留，新矩形以新修订号写入。
        """
        targets = body.targets

        # 结构性校验不依赖库内状态，失败无需进入事务。
        if not 1 <= len(targets) <= 20:
            _error(
                400,
                "invalid_migration_size",
                f"migration must target between 1 and 20 documents,"
                f" got {len(targets)}",
                count=len(targets),
            )
        seen: set[str] = set()
        for pos, item in enumerate(targets):
            if item.doc_id in seen:
                _error(
                    400,
                    "duplicate_target_in_migration",
                    f"target document '{item.doc_id}' appears more than once"
                    " in the migration request",
                    position=pos,
                    doc_id=item.doc_id,
                )
            seen.add(item.doc_id)
        if body.source_doc_id in seen:
            _error(
                400,
                "source_among_targets",
                "the source document cannot also be a migration target",
                doc_id=body.source_doc_id,
            )

        result_targets: list[dict] = []
        with write_tx(settings.db_path) as conn:
            # 源存在性与修订号（乐观锁），随后读取其当前非空标注。
            source = conn.execute(
                "SELECT * FROM documents WHERE id = ?", (body.source_doc_id,)
            ).fetchone()
            if source is None:
                _error(
                    404,
                    "document_not_found",
                    f"source document '{body.source_doc_id}' does not exist",
                    doc_id=body.source_doc_id,
                )
            if body.source_revision != source["annotation_revision"]:
                _error(
                    409,
                    "stale_revision",
                    f"source annotation revision mismatch: expected"
                    f" {source['annotation_revision']}, got"
                    f" {body.source_revision}; re-fetch and retry",
                    doc_id=source["id"],
                    current_revision=source["annotation_revision"],
                    supplied_revision=body.source_revision,
                )
            src_rects = conn.execute(
                "SELECT x, y, w, h, reason FROM annotation_rects"
                " WHERE doc_id = ? AND revision = ? ORDER BY idx",
                (source["id"], source["annotation_revision"]),
            ).fetchall()
            if not src_rects:
                _error(
                    400,
                    "source_annotations_empty",
                    "the source document's current annotations contain no"
                    " rectangles; nothing to migrate",
                    doc_id=source["id"],
                    source_revision=source["annotation_revision"],
                )

            # 第一阶段：按请求顺序逐项目标校验（存在性 + 预期修订号），
            # 先定位失败项，再写入任何标注。
            prepared: list[tuple[Any, int, list[dict]]] = []
            for pos, item in enumerate(targets):
                doc = conn.execute(
                    "SELECT * FROM documents WHERE id = ?", (item.doc_id,)
                ).fetchone()
                if doc is None:
                    _error(
                        404,
                        "document_not_found",
                        f"document '{item.doc_id}' does not exist",
                        position=pos,
                        doc_id=item.doc_id,
                    )
                current = doc["annotation_revision"]
                if item.expected_revision != current:
                    _error(
                        409,
                        "stale_revision",
                        f"annotation revision mismatch for '{item.doc_id}':"
                        f" expected {current}, got {item.expected_revision};"
                        " re-fetch and retry",
                        position=pos,
                        doc_id=item.doc_id,
                        current_revision=current,
                        supplied_revision=item.expected_revision,
                    )
                # 宽、高方向分别按旋正后尺寸缩放：左上向下取整、
                # 右下向上取整并夹到目标图内，原因与顺序不变。
                mapped = [
                    scale_rect(
                        r,
                        source["width"], source["height"],
                        doc["width"], doc["height"],
                    )
                    for r in src_rects
                ]
                prepared.append((doc, current, mapped))

            # 第二阶段：全部校验通过，逐目标以新修订号追加矩形，
            # 再把当前修订号 +1（旧修订矩形永不改写）。
            for pos, (doc, current, mapped) in enumerate(prepared):
                new_revision = current + 1
                for idx, r in enumerate(mapped):
                    conn.execute(
                        "INSERT INTO annotation_rects"
                        " (doc_id, revision, idx, x, y, w, h, reason)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (
                            doc["id"], new_revision, idx,
                            r["x"], r["y"], r["w"], r["h"], r["reason"],
                        ),
                    )
                conn.execute(
                    "UPDATE documents SET annotation_revision = ? WHERE id = ?",
                    (new_revision, doc["id"]),
                )
                result_targets.append(
                    {
                        "position": pos,
                        "doc_id": doc["id"],
                        "revision": new_revision,  # 迁移后的当前修订号
                        "rects": mapped,           # 顺序与源标注一致
                    }
                )

        return {
            "source_doc_id": source["id"],
            "source_revision": source["annotation_revision"],
            "count": len(result_targets),
            "targets": result_targets,  # 顺序与请求一致
        }

    # ---------------- 重发前遮盖范围比较（纯只读，同一读取快照） ----------------

    @app.post("/documents/{doc_id}/coverage-diff")
    def compare_coverage(doc_id: str, body: CoverageDiffRequest) -> dict:
        """比较当前标注与指定已发布版本（基准）的遮盖像素集合。

        纯只读：不生成版本、不写任何改动记录。文档、当前修订矩形与基准
        发布冻结的矩形都在**同一个只读快照事务**（``snapshot_tx``）中读取，
        因此比较期间发生的标注修改/发布/撤回不会让两边读到不一致的状态；
        只读事务不获取写锁，不阻塞任何写入。

        拒绝顺序与公开读取一致，且拒绝时不返回任何基准遮盖详情：
        未知文档 404 document_not_found；未知版本 404 unknown_version；
        已撤回 410 publication_withdrawn（优先）；未到公开时间 404
        publication_not_yet_public；当前修订号过期 409 stale_revision。
        基准矩形取自发布行固化的 annotation_revision——历史修订按
        (doc_id, revision) 追加永不改写，故不受后续标注修改影响。
        """
        # 文档、基准发布行、撤回/公开时间、当前修订与两套矩形全部在同一
        # 只读快照中读取（延迟 BEGIN 以首条 SELECT 建立 WAL 读快照）。
        with snapshot_tx(settings.db_path) as conn:
            doc = _get_doc(conn, doc_id)
            pub = _get_publication(conn, doc_id, body.baseline_version)
            # 与公开读取相同的门控顺序：撤回优先（410），未到公开时间 404；
            # 门控未通过前不读取、不泄露该版本冻结的遮盖矩形。
            _ensure_not_withdrawn(conn, doc_id, body.baseline_version)
            _ensure_publish_time_reached(pub)
            current_revision = doc["annotation_revision"]
            if body.revision != current_revision:
                _error(
                    409,
                    "stale_revision",
                    f"annotation revision mismatch: expected {current_revision},"
                    f" got {body.revision}; re-fetch and retry",
                    current_revision=current_revision,
                    supplied_revision=body.revision,
                )

            # 测试钩子：当前修订已校验、快照已建立，但矩形尚未读取。
            if COVERAGE_DIFF_SNAPSHOT_HOOK is not None:
                COVERAGE_DIFF_SNAPSHOT_HOOK()

            current_rows = conn.execute(
                "SELECT x, y, w, h, reason FROM annotation_rects"
                " WHERE doc_id = ? AND revision = ? ORDER BY idx",
                (doc_id, current_revision),
            ).fetchall()
            # 基准必须是发布时冻结的标注：取发布行固化的修订号，
            # 该修订矩形追加存储、永不改写，后续标注变化不影响它。
            baseline_rows = conn.execute(
                "SELECT x, y, w, h, reason FROM annotation_rects"
                " WHERE doc_id = ? AND revision = ? ORDER BY idx",
                (doc_id, pub["annotation_revision"]),
            ).fetchall()

            diff = coverage_diff(
                current_rows, baseline_rows, doc["width"], doc["height"]
            )

        width, height = doc["width"], doc["height"]
        total = width * height  # 旋正后图像总像素数
        return {
            "doc_id": doc_id,
            "current_revision": current_revision,
            "baseline_version": pub["version"],
            "baseline_annotation_revision": pub["annotation_revision"],
            "width": width,
            "height": height,
            "total_pixels": total,
            "added_pixels": diff["added_pixels"],
            "added_ratio": diff["added_pixels"] / total,
            "lost_pixels": diff["lost_pixels"],
            "lost_ratio": diff["lost_pixels"] / total,
            "lost_segments": diff["lost_segments"],
        }

    # ---------------- 发布 ----------------

    @app.post("/documents/{doc_id}/publish", status_code=201)
    def publish_document(
        doc_id: str, body: PublishRequest | None = Body(default=None)
    ) -> dict:
        # 公开时间在进入事务前解析（格式无效即 400，无任何副作用）；
        # "晚于当前且不超过 30 天"的窗口校验在写事务内完成。
        publish_at: str | None = None
        publish_dt: datetime | None = None
        if body is not None and body.publish_at is not None:
            publish_dt, publish_at = _parse_publish_at(body.publish_at)

        output_path: str | None = None
        try:
            # 整个"窗口校验+读修订+读矩形+渲染+落盘+写记录"在一个写事务内，
            # 与标注修改互斥，保证成品与记录的修订号一致；定时版本同样在发布
            # 当下固化成品并占用正常版本号，只是公开时间到达前对外 404。
            with write_tx(settings.db_path) as conn:
                doc = _get_doc(conn, doc_id)
                if publish_dt is not None:
                    _check_publish_window(publish_dt)
                revision = doc["annotation_revision"]
                rects = conn.execute(
                    "SELECT x, y, w, h, reason FROM annotation_rects"
                    " WHERE doc_id = ? AND revision = ? ORDER BY idx",
                    (doc_id, revision),
                ).fetchall()
                version = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 AS v"
                    " FROM publications WHERE doc_id = ?",
                    (doc_id,),
                ).fetchone()["v"]

                try:
                    with open(doc["original_path"], "rb") as f:
                        original_bytes = f.read()
                    oriented, _ = load_oriented(original_bytes)
                except Exception as exc:
                    _error(
                        500,
                        "original_unavailable",
                        f"stored original cannot be decoded: {exc}",
                    )

                image = render_publication(oriented, rects)
                doc_pub_dir = os.path.join(settings.publications_dir, doc_id)
                os.makedirs(doc_pub_dir, exist_ok=True)
                output_path = os.path.abspath(
                    os.path.join(doc_pub_dir, f"v{version}.png")
                )
                image.save(output_path, format="PNG")  # 无元数据的独立 PNG
                output_sha256 = _sha256_file(output_path)

                now = _utcnow()
                conn.execute(
                    "INSERT INTO publications (doc_id, version, annotation_revision,"
                    " original_sha256, output_sha256, output_path, created_at,"
                    " publish_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        doc_id,
                        version,
                        revision,
                        doc["original_sha256"],
                        output_sha256,
                        output_path,
                        now,
                        publish_at,
                    ),
                )
        except Exception:
            # 事务回滚时清理可能已落盘的半成品文件
            if output_path and os.path.exists(output_path):
                try:
                    os.unlink(output_path)
                except OSError:
                    pass
            raise
        return {
            "doc_id": doc_id,
            "version": version,
            "annotation_revision": revision,
            "original_sha256": doc["original_sha256"],
            "output_sha256": output_sha256,
            "redaction_count": len(rects),
            "created_at": now,
            "publish_at": publish_at,  # null=立即公开；否则为统一 UTC 公开时间
        }

    @app.get("/documents/{doc_id}/publications")
    def list_publications(doc_id: str) -> dict:
        with read_tx(settings.db_path) as conn:
            _get_doc(conn, doc_id)
            pubs = conn.execute(
                "SELECT p.version, p.annotation_revision, p.original_sha256,"
                " p.output_sha256, p.created_at, p.publish_at AS p_at"
                + _WITHDRAWAL_COLS +
                " FROM publications p" + _WITHDRAWAL_JOIN.format(t="p") +
                " WHERE p.doc_id = ? ORDER BY p.version",
                (doc_id,),
            ).fetchall()
        return {
            "doc_id": doc_id,
            "publications": [
                _pub_entry(
                    p, ["version", "annotation_revision", "original_sha256",
                        "output_sha256", "created_at"]
                )
                for p in pubs
            ],
        }

    # ---------------- 批次发布（整批原子） ----------------

    @app.post("/publication-batches", status_code=201)
    def publish_batch(body: BatchPublishRequest) -> Any:
        items = body.documents
        key = body.idempotency_key
        # 整批统一的公开时间：先解析（格式无效直接 400，不进入事务、
        # 不占版本/批次/幂等键），规范化值同时纳入幂等内容摘要。
        publish_at: str | None = None
        publish_dt: datetime | None = None
        if body.publish_at is not None:
            publish_dt, publish_at = _parse_publish_at(body.publish_at)
        request_hash = (
            _batch_request_hash(items, publish_at) if key is not None else None
        )

        batch_id = uuid.uuid4().hex
        now = _utcnow()
        written_paths: list[str] = []
        # 整批校验 + 渲染 + 落盘 + 记录全部在同一个 BEGIN IMMEDIATE 事务中，
        # 与标注修改、单份发布、其他批次发布互斥串行；
        # 任一项失败即回滚全部 INSERT，并清理已落盘文件，不留下可公开的
        # 部分版本，MAX(version)+1 也不会被失败批次推进（记录从未提交）。
        # 幂等键的"查重 -> 发布 -> 登记"也在该事务内：并发重试被写锁串行化，
        # 最多发布一次；任何失败回滚时键记录一并撤销，键不被占用，可原键重试。
        try:
            with write_tx(settings.db_path) as conn:
                # 第零阶段：幂等键检查，先于一切业务校验——
                # 相同键+相同内容的重试直接返回首次成功结果，
                # 即使标注随后变化，也不再校验当前修订号、不重新渲染。
                if key is not None:
                    prior = conn.execute(
                        "SELECT request_hash, response FROM batch_idempotency"
                        " WHERE key = ?",
                        (key,),
                    ).fetchone()
                    if prior is not None:
                        if prior["request_hash"] == request_hash:
                            # 重放：原样返回持久化的首次成功响应（HTTP 200）
                            return Response(
                                content=prior["response"],
                                media_type="application/json",
                                status_code=200,
                            )
                        # 相同键+不同内容：明确冲突，原批次不受影响
                        _error(
                            409,
                            "idempotency_key_conflict",
                            f"idempotency key '{key}' was already used with"
                            " different batch content; the original batch is"
                            " unchanged",
                        )

                # 语义校验：公开时间窗口、数量与重复（失败即回滚，不占用
                # 幂等键、不涉及版本号）。窗口校验只在首次请求进行——
                # 重放已在上方直接返回，即使公开时间已过也不重新校验。
                if publish_dt is not None:
                    _check_publish_window(publish_dt)
                if not 2 <= len(items) <= 20:
                    _error(
                        400,
                        "invalid_batch_size",
                        f"batch must contain between 2 and 20 documents,"
                        f" got {len(items)}",
                        count=len(items),
                    )
                seen: set[str] = set()
                for pos, item in enumerate(items):
                    if item.doc_id in seen:
                        _error(
                            400,
                            "duplicate_document_in_batch",
                            f"document '{item.doc_id}' appears more than once"
                            " in the batch",
                            position=pos,
                            doc_id=item.doc_id,
                        )
                    seen.add(item.doc_id)

                # 第一阶段：按请求顺序逐份校验（存在性 + 修订号匹配），
                # 先定位失败项，再渲染任何成品。
                prepared: list[tuple[Any, int, Any]] = []
                for pos, item in enumerate(items):
                    doc = conn.execute(
                        "SELECT * FROM documents WHERE id = ?", (item.doc_id,)
                    ).fetchone()
                    if doc is None:
                        _error(
                            404,
                            "document_not_found",
                            f"document '{item.doc_id}' does not exist",
                            position=pos,
                            doc_id=item.doc_id,
                        )
                    revision = doc["annotation_revision"]
                    if item.expected_revision != revision:
                        _error(
                            409,
                            "stale_revision",
                            f"annotation revision mismatch for '{item.doc_id}':"
                            f" expected {revision}, got {item.expected_revision};"
                            " re-fetch and retry",
                            position=pos,
                            doc_id=item.doc_id,
                            current_revision=revision,
                            supplied_revision=item.expected_revision,
                        )
                    rects = conn.execute(
                        "SELECT x, y, w, h, reason FROM annotation_rects"
                        " WHERE doc_id = ? AND revision = ? ORDER BY idx",
                        (doc["id"], revision),
                    ).fetchall()
                    version = conn.execute(
                        "SELECT COALESCE(MAX(version), 0) + 1 AS v"
                        " FROM publications WHERE doc_id = ?",
                        (doc["id"],),
                    ).fetchone()["v"]
                    prepared.append((doc, version, revision, rects))

                # 第二阶段：全部校验通过后逐份渲染、落盘、登记
                result_items: list[dict] = []
                for pos, (doc, version, revision, rects) in enumerate(prepared):
                    try:
                        with open(doc["original_path"], "rb") as f:
                            original_bytes = f.read()
                        oriented, _ = load_oriented(original_bytes)
                    except Exception as exc:
                        _error(
                            500,
                            "original_unavailable",
                            f"stored original for '{doc['id']}' cannot be decoded:"
                            f" {exc}",
                            position=pos,
                            doc_id=doc["id"],
                        )

                    image = render_publication(oriented, rects)
                    doc_pub_dir = os.path.join(settings.publications_dir, doc["id"])
                    output_path = os.path.abspath(
                        os.path.join(doc_pub_dir, f"v{version}.png")
                    )
                    try:
                        os.makedirs(doc_pub_dir, exist_ok=True)
                        image.save(output_path, format="PNG")  # 无元数据独立 PNG
                        output_sha256 = _sha256_file(output_path)
                    except Exception as exc:
                        # 成品写入失败：清理可能已部分写入的文件
                        if os.path.exists(output_path):
                            try:
                                os.unlink(output_path)
                            except OSError:
                                pass
                        _error(
                            500,
                            "artifact_write_failed",
                            f"failed to write publication artifact for"
                            f" '{doc['id']}' version {version}: {exc}",
                            position=pos,
                            doc_id=doc["id"],
                            version=version,
                        )
                    written_paths.append(output_path)

                    conn.execute(
                        "INSERT INTO publications (doc_id, version, annotation_revision,"
                        " original_sha256, output_sha256, output_path, created_at,"
                        " batch_id, publish_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            doc["id"],
                            version,
                            revision,
                            doc["original_sha256"],
                            output_sha256,
                            output_path,
                            now,
                            batch_id,
                            publish_at,
                        ),
                    )
                    result_items.append(
                        {
                            "position": pos,
                            "doc_id": doc["id"],
                            "version": version,
                            "annotation_revision": revision,
                            "original_sha256": doc["original_sha256"],
                            "output_sha256": output_sha256,
                            "redaction_count": len(rects),
                            "created_at": now,
                        }
                    )

                conn.execute(
                    "INSERT INTO publication_batches (id, created_at, publish_at)"
                    " VALUES (?,?,?)",
                    (batch_id, now, publish_at),
                )
                for out in result_items:
                    conn.execute(
                        "INSERT INTO batch_publications (batch_id, position, doc_id,"
                        " version, annotation_revision, original_sha256, output_sha256)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (
                            batch_id,
                            out["position"],
                            out["doc_id"],
                            out["version"],
                            out["annotation_revision"],
                            out["original_sha256"],
                            out["output_sha256"],
                        ),
                    )

                response_body = {
                    "batch_id": batch_id,
                    "created_at": now,
                    "count": len(result_items),
                    "documents": result_items,  # 顺序与请求一致
                    "publish_at": publish_at,   # null=立即公开；否则为整批统一 UTC 公开时间
                }
                if key is not None:
                    # 首次成功：持久化键与完整成功结果，同事务提交
                    conn.execute(
                        "INSERT INTO batch_idempotency (key, request_hash, batch_id,"
                        " response, created_at) VALUES (?,?,?,?,?)",
                        (
                            key,
                            request_hash,
                            batch_id,
                            json.dumps(response_body, ensure_ascii=False),
                            now,
                        ),
                    )
        except Exception:
            # 事务回滚（不留下任何批次/版本/幂等键记录），并清理已落盘半成品
            for path in written_paths:
                if os.path.exists(path):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
            raise
        return response_body

    @app.get("/publication-batches/{batch_id}")
    def get_publication_batch(batch_id: str) -> dict:
        with read_tx(settings.db_path) as conn:
            batch = conn.execute(
                "SELECT id, created_at, publish_at FROM publication_batches"
                " WHERE id = ?",
                (batch_id,),
            ).fetchone()
            if batch is None:
                _error(
                    404,
                    "batch_not_found",
                    f"publication batch '{batch_id}' does not exist",
                )
            rows = conn.execute(
                "SELECT bp.position, bp.doc_id, bp.version, bp.annotation_revision,"
                " bp.original_sha256, bp.output_sha256, p.publish_at AS p_at"
                + _WITHDRAWAL_COLS +
                " FROM batch_publications bp"
                " JOIN publications p ON p.doc_id = bp.doc_id AND p.version = bp.version"
                + _WITHDRAWAL_JOIN.format(t="bp") +
                " WHERE bp.batch_id = ? ORDER BY bp.position",
                (batch_id,),
            ).fetchall()
        return {
            "batch_id": batch["id"],
            "created_at": batch["created_at"],
            "count": len(rows),
            "documents": [
                _pub_entry(
                    r, ["position", "doc_id", "version", "annotation_revision",
                        "original_sha256", "output_sha256"]
                )
                for r in rows
            ],  # 顺序与原请求一致
            "publish_at": batch["publish_at"],
        }

    # ---------------- 版本撤回（永久，幂等） ----------------

    def _withdrawal_out(doc_id: str, version: int, row: Any) -> dict:
        return {
            "doc_id": doc_id,
            "version": version,
            "withdrawn": row is not None,
            "withdrawn_at": row["withdrawn_at"] if row is not None else None,
            "withdrawal_reason": row["reason"] if row is not None else None,
        }

    @app.post(
        "/documents/{doc_id}/publications/{version}/withdraw",
        status_code=201,
    )
    def withdraw_publication(
        doc_id: str, version: int, body: WithdrawRequest, response: Response
    ) -> dict:
        # 存在性校验、原因校验、"查重 -> 插入"在同一个 BEGIN IMMEDIATE
        # 写事务内完成：与发布、标注修改、其他撤回互斥串行，并发重复撤回
        # 恰好一个创建记录（201），其余返回首次记录（200）；任何失败整体
        # 回滚，不留下部分撤回记录。撤回只插入 publication_withdrawals 行，
        # 不删除发布记录与成品文件，版本号也不会被后续发布复用。
        with write_tx(settings.db_path) as conn:
            _get_publication(conn, doc_id, version)  # 未知文档/版本 -> 404
            reason = body.reason.strip()
            if not reason:
                _error(
                    400,
                    "reason_required",
                    "withdrawal reason must be non-empty",
                )
            existing = conn.execute(
                "SELECT reason, withdrawn_at FROM publication_withdrawals"
                " WHERE doc_id = ? AND version = ?",
                (doc_id, version),
            ).fetchone()
            if existing is not None:
                # 重复撤回：返回首次记录，不改写原因与时间
                response.status_code = 200
                return _withdrawal_out(doc_id, version, existing)
            now = _utcnow()
            conn.execute(
                "INSERT INTO publication_withdrawals"
                " (doc_id, version, reason, withdrawn_at) VALUES (?,?,?,?)",
                (doc_id, version, reason, now),
            )
        return _withdrawal_out(
            doc_id, version, {"reason": reason, "withdrawn_at": now}
        )

    @app.get("/documents/{doc_id}/publications/{version}/withdrawal")
    def get_withdrawal(doc_id: str, version: int) -> dict:
        with read_tx(settings.db_path) as conn:
            _get_publication(conn, doc_id, version)  # 未知文档/版本 -> 404
            row = conn.execute(
                "SELECT reason, withdrawn_at FROM publication_withdrawals"
                " WHERE doc_id = ? AND version = ?",
                (doc_id, version),
            ).fetchone()
        return _withdrawal_out(doc_id, version, row)

    # ---------------- 公开读取（只暴露发布版，不暴露原图） ----------------

    @app.get("/publications/{doc_id}/{version}")
    def get_publication(doc_id: str, version: int) -> dict:
        with read_tx(settings.db_path) as conn:
            pub = _get_publication(conn, doc_id, version)
            # 永久撤回优先于公开时间：已撤回版本一律 410
            _ensure_not_withdrawn(conn, doc_id, version)
            # 未到公开时间：404，不返回遮盖信息、摘要等任何版本内容
            _ensure_publish_time_reached(pub)
            rects = conn.execute(
                "SELECT x, y, w, h, reason FROM annotation_rects"
                " WHERE doc_id = ? AND revision = ? ORDER BY idx",
                (doc_id, pub["annotation_revision"]),
            ).fetchall()
        return {
            "doc_id": doc_id,
            "version": pub["version"],
            "annotation_revision": pub["annotation_revision"],
            "original_sha256": pub["original_sha256"],
            "output_sha256": pub["output_sha256"],
            "created_at": pub["created_at"],
            "redactions": [_rect_out(r) for r in rects],
            "publish_at": pub["publish_at"],
        }

    @app.get("/publications/{doc_id}/{version}/image")
    def get_publication_image(doc_id: str, version: int) -> FileResponse:
        with read_tx(settings.db_path) as conn:
            pub = _get_publication(conn, doc_id, version)
            _ensure_not_withdrawn(conn, doc_id, version)
            _ensure_publish_time_reached(pub)
        path = pub["output_path"]
        if not os.path.exists(path):
            _error(
                500,
                "artifact_missing",
                f"published artifact for version {version} is missing on disk",
            )
        return FileResponse(
            path,
            media_type="image/png",
            filename=f"{doc_id}-v{version}.png",
        )

    # ---------------- 离线交付包（ZIP：各版 PNG + JSON 清单） ----------------

    @app.post("/publication-packages")
    def create_publication_package(
        body: PackageRequest,
        background_tasks: BackgroundTasks,
    ) -> FileResponse:
        """按请求顺序把 2～20 个互不重复的已公开脱敏版本打成 ZIP。

        资格确认（数量/重复、未知文档版本、撤回、公开时间）与撤回操作
        通过同一个 ``BEGIN IMMEDIATE`` 写锁串行：撤回先提交则本次整包
        拒绝，打包先确认则撤回被挡到事务提交之后，本次交付照常完成。
        任一资格项失败都不读取任何成品、不产出部分包；读盘/校验/打包
        过程中的任何失败同样不交付。包内 PNG 文件名只由请求位置、
        服务端文档 ID 与版本号生成，绝不取自上传文件名。
        """
        items = body.items

        # 结构性校验在事务外完成：数量与重复本身不泄露任何版本信息。
        if not 2 <= len(items) <= 20:
            _error(
                400,
                "invalid_package_size",
                f"package must contain between 2 and 20 items, got {len(items)}",
                count=len(items),
            )
        seen: set[tuple[str, int]] = set()
        for pos, item in enumerate(items):
            key = (item.doc_id, item.version)
            if key in seen:
                _error(
                    400,
                    "duplicate_item_in_package",
                    f"document '{item.doc_id}' version {item.version} appears more"
                    " than once in the package request",
                    position=pos,
                    doc_id=item.doc_id,
                    version=item.version,
                )
            seen.add(key)

        tmp_fd, tmp_path = tempfile.mkstemp(prefix="pub-pkg-", suffix=".zip")
        os.close(tmp_fd)
        try:
            # 资格确认 + 全部成品读盘/摘要校验 + ZIP 生成同在一个写事务内：
            # 与撤回（以及发布、标注修改）互斥串行，确认结果与包内容一致。
            with write_tx(settings.db_path) as conn:
                # 第一阶段：按请求顺序逐项资格确认，全部通过后才读任何成品。
                qualified: list[tuple[int, Any]] = []
                for pos, item in enumerate(items):
                    pub = _get_publication(
                        conn, item.doc_id, item.version, position=pos
                    )
                    # 永久撤回优先于公开时间（与单份公开读取的顺序一致）
                    _ensure_not_withdrawn(
                        conn, item.doc_id, item.version, position=pos
                    )
                    _ensure_publish_time_reached(pub, position=pos)
                    qualified.append((pos, pub))

                # 资格已在写锁内确认：并发撤回此刻无法生效，须等本事务提交。
                if PACKAGE_ELIGIBILITY_HOOK is not None:
                    PACKAGE_ELIGIBILITY_HOOK()

                # 第二阶段：逐项读盘并复算成品摘要；任何缺失/IO 失败/摘要
                # 不符都整包拒绝（不交付部分包）。先全部读入再写 ZIP，
                # 保证 ZIP 中绝不残留失败项。
                loaded: list[tuple[int, Any, bytes, str]] = []
                for pos, pub in qualified:
                    path = pub["output_path"]
                    if not os.path.exists(path):
                        _error(
                            500,
                            "artifact_missing",
                            f"published artifact for document '{pub['doc_id']}'"
                            f" version {pub['version']} is missing on disk",
                            position=pos,
                            doc_id=pub["doc_id"],
                            version=pub["version"],
                        )
                    try:
                        with open(path, "rb") as f:
                            artifact = f.read()
                    except OSError as exc:
                        _error(
                            500,
                            "artifact_read_failed",
                            f"failed to read published artifact for document"
                            f" '{pub['doc_id']}' version {pub['version']}: {exc}",
                            position=pos,
                            doc_id=pub["doc_id"],
                            version=pub["version"],
                        )
                    actual_sha = _sha256_bytes(artifact)
                    if actual_sha != pub["output_sha256"]:
                        _error(
                            500,
                            "artifact_corrupt",
                            f"published artifact for document '{pub['doc_id']}'"
                            f" version {pub['version']} failed integrity check",
                            position=pos,
                            doc_id=pub["doc_id"],
                            version=pub["version"],
                        )
                    rect_count = conn.execute(
                        "SELECT COUNT(*) AS c FROM annotation_rects"
                        " WHERE doc_id = ? AND revision = ?",
                        (pub["doc_id"], pub["annotation_revision"]),
                    ).fetchone()["c"]
                    loaded.append((pos, pub, artifact, actual_sha, rect_count))

                # 第三阶段：生成 ZIP。包内文件名只用请求位置 + 服务端文档 ID
                # + 版本号，绝不取自上传文件名。
                manifest_entries: list[dict] = []
                created_at = _utcnow()
                with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for pos, pub, artifact, actual_sha, rect_count in loaded:
                        arcname = f"{pos + 1:02d}_{pub['doc_id']}_v{pub['version']}.png"
                        zf.writestr(arcname, artifact)
                        manifest_entries.append(
                            {
                                "position": pos,
                                "doc_id": pub["doc_id"],
                                "version": pub["version"],
                                "output_sha256": actual_sha,
                                "redaction_count": rect_count,
                                "file": arcname,
                            }
                        )
                    manifest = {
                        "created_at": created_at,
                        "count": len(manifest_entries),
                        "items": manifest_entries,  # 顺序与请求一致
                    }
                    zf.writestr(
                        "manifest.json",
                        json.dumps(manifest, ensure_ascii=False, indent=2),
                    )
            # 事务已提交：本次交付资格锁定，即使撤回此刻生效也照常完成。
        except Exception:
            _cleanup_tempfile(tmp_path)
            raise
        background_tasks.add_task(_cleanup_tempfile, tmp_path)
        return FileResponse(
            tmp_path,
            media_type="application/zip",
            filename="publication-package.zip",
        )

    # ---------------- 服务数据归档（同一数据库快照，全有或全无 ZIP） ----------------

    @app.post("/service-data-archives")
    def create_service_data_archive(
        background_tasks: BackgroundTasks,
    ) -> FileResponse:
        """生成一次服务数据归档（运维请求，无请求体）。

        归档包含**同一个数据库快照**中的全部已提交数据：文档、各修订标注
        （含发布记录与撤回记录），以及该快照引用到的**原图**与**发布成品**。
        所有记录都在**同一个只读快照事务**（延迟 ``BEGIN`` 的 WAL 读快照）中
        读取：归档进行期间并发提交的标注修改/发布/撤回对本次读取不可见，
        归档因此**只对应一个一致时点**；事务纯只读、不持写锁，**并发写入可
        继续**，不会被归档阻塞。原图与成品落盘后不可变（原图按文档 ID 保存、
        成品按版本保存，发布/撤回均不改写文件），故快照记录与随后在快照事务
        内读取的文件字节必然同属一个时点。

        文件以**相对路径**组织（``originals/``、``publications/`` 加顶层
        JSON），不包含任何服务端绝对路径或上传文件名；另附 ``manifest.json``
        清单，逐文件列出 SHA-256、文档/版本标识与文件数。

        **全有或全无**：任一被引用文件缺失、读盘失败或实际摘要与快照记录不符
        都整单拒绝（不返回 ZIP、不提供部分归档）；ZIP 必须在临时文件上**完整
        生成并正常关闭**后才作为下载响应，任何失败都清理临时文件。
        """
        tmp_fd, tmp_path = tempfile.mkstemp(prefix="svc-archive-", suffix=".zip")
        os.close(tmp_fd)
        try:
            # 记录读取、文件读盘/摘要复算、ZIP 生成全部在同一只读快照事务内：
            # 与并发写入互不干扰（读快照固定），且不阻塞任何写入。
            with snapshot_tx(settings.db_path) as conn:
                docs = conn.execute(
                    "SELECT * FROM documents ORDER BY id"
                ).fetchall()
                pubs = conn.execute(
                    "SELECT * FROM publications"
                    " ORDER BY doc_id, version"
                ).fetchall()
                batches = conn.execute(
                    "SELECT * FROM publication_batches ORDER BY id"
                ).fetchall()
                withdrawals = conn.execute(
                    "SELECT * FROM publication_withdrawals"
                    " ORDER BY doc_id, version"
                ).fetchall()

                # 标注矩形按 (doc_id, revision, idx) 追加存储、永不改写：
                # 归档全部修订（含 revision=0 与已清空的修订），忠实复制快照。
                rect_rows = conn.execute(
                    "SELECT doc_id, revision, idx, x, y, w, h, reason"
                    " FROM annotation_rects ORDER BY doc_id, revision, idx"
                ).fetchall()
                rects_by_doc_rev: dict[tuple[str, int], list[dict]] = {}
                for r in rect_rows:
                    rects_by_doc_rev.setdefault(
                        (r["doc_id"], r["revision"]), []
                    ).append(_rect_out(r))

                # 每份文档的归档文档条目与归档内原图相对路径（不取上传文件名）。
                doc_entries: list[dict] = []
                doc_by_id: dict[str, Any] = {}
                for d in docs:
                    doc_by_id[d["id"]] = d
                    doc_entries.append(
                        {
                            "doc_id": d["id"],
                            "filename": d["filename"],
                            "media_type": d["media_type"],
                            "width": d["width"],
                            "height": d["height"],
                            "original_sha256": d["original_sha256"],
                            "original_file": _archive_original_relpath(d),
                            "annotation_revision": d["annotation_revision"],
                            "created_at": d["created_at"],
                        }
                    )

                # 每个被引用文档的各修订标注（矩形数即该修订文件数口径之一）。
                revision_counts: dict[str, int] = {}
                annotation_docs: list[dict] = []
                for d in docs:
                    revisions: list[dict] = []
                    for rev in range(d["annotation_revision"] + 1):
                        rects = rects_by_doc_rev.get((d["id"], rev), [])
                        revisions.append(
                            {"revision": rev, "rect_count": len(rects),
                             "rects": rects}
                        )
                    revision_counts[d["id"]] = len(revisions)
                    annotation_docs.append(
                        {"doc_id": d["id"],
                         "current_revision": d["annotation_revision"],
                         "revision_count": len(revisions),
                         "revisions": revisions}
                    )

                withdrawal_by_pub = {
                    (w["doc_id"], w["version"]): w for w in withdrawals
                }
                batch_pub_rows = conn.execute(
                    "SELECT batch_id, position, doc_id, version"
                    " FROM batch_publications ORDER BY batch_id, position"
                ).fetchall()
                members_by_batch: dict[str, list[dict]] = {}
                for bp in batch_pub_rows:
                    members_by_batch.setdefault(bp["batch_id"], []).append(
                        {"position": bp["position"], "doc_id": bp["doc_id"],
                         "version": bp["version"]}
                    )

                publication_entries: list[dict] = []
                for p in pubs:
                    w = withdrawal_by_pub.get((p["doc_id"], p["version"]))
                    publication_entries.append(
                        {
                            "doc_id": p["doc_id"],
                            "version": p["version"],
                            "annotation_revision": p["annotation_revision"],
                            "original_sha256": p["original_sha256"],
                            "output_sha256": p["output_sha256"],
                            "output_file":
                                f"publications/{p['doc_id']}/v{p['version']}.png",
                            "created_at": p["created_at"],
                            "batch_id": p["batch_id"],
                            "publish_at": p["publish_at"],
                            "withdrawn": w is not None,
                            "withdrawn_at": w["withdrawn_at"] if w else None,
                            "withdrawal_reason": w["reason"] if w else None,
                        }
                    )

                batch_entries = [
                    {
                        "batch_id": b["id"],
                        "created_at": b["created_at"],
                        "publish_at": b["publish_at"],
                        "documents": members_by_batch.get(b["id"], []),
                    }
                    for b in batches
                ]
                withdrawal_entries = [
                    {
                        "doc_id": w["doc_id"],
                        "version": w["version"],
                        "reason": w["reason"],
                        "withdrawn_at": w["withdrawn_at"],
                    }
                    for w in withdrawals
                ]

                # 测试钩子：快照记录已全部读出、引用文件尚未读盘。
                # 并发测试在此提交标注/发布等写入，验证归档不看到新数据、
                # 也不被写入阻塞。
                if ARCHIVE_SNAPSHOT_HOOK is not None:
                    ARCHIVE_SNAPSHOT_HOOK()

                # 引用文件读盘 + 摘要复算：先把全部字节读入并校验，再写 ZIP，
                # 任一文件缺失/读盘失败/摘要不符即整单拒绝，ZIP 中无残留失败项。
                # 清单条目按归档内相对路径排序，保证归档可逐字节复现。
                file_entries: list[dict] = []
                zip_members: list[tuple[str, bytes]] = []

                for d in docs:
                    relpath = _archive_original_relpath(d)
                    data, actual_sha = _read_and_verify_archive_file(
                        d["original_path"],
                        d["original_sha256"],
                        kind="stored original image",
                        missing_code="archive_original_missing",
                        read_code="archive_original_read_failed",
                        corrupt_code="archive_original_corrupt",
                        doc_id=d["id"],
                    )
                    file_entries.append(
                        {
                            "path": relpath,
                            "kind": "original",
                            "doc_id": d["id"],
                            "sha256": actual_sha,
                            "file_count": 1,
                        }
                    )
                    zip_members.append((relpath, data))

                for p in pubs:
                    relpath = f"publications/{p['doc_id']}/v{p['version']}.png"
                    data, actual_sha = _read_and_verify_archive_file(
                        p["output_path"],
                        p["output_sha256"],
                        kind="published artifact",
                        missing_code="archive_artifact_missing",
                        read_code="archive_artifact_read_failed",
                        corrupt_code="archive_artifact_corrupt",
                        doc_id=p["doc_id"],
                        version=p["version"],
                    )
                    file_entries.append(
                        {
                            "path": relpath,
                            "kind": "publication",
                            "doc_id": p["doc_id"],
                            "version": p["version"],
                            "sha256": actual_sha,
                            "file_count": 1,
                        }
                    )
                    zip_members.append((relpath, data))

                # 记录 JSON（与上面的快照读取同一事务，内容同属一个时点）。
                json_docs = {
                    "count": len(doc_entries),
                    "documents": doc_entries,
                }
                json_annotations = {
                    "document_count": len(annotation_docs),
                    "revision_count": sum(revision_counts.values()),
                    "documents": annotation_docs,
                }
                json_publications = {
                    "count": len(publication_entries),
                    "publications": publication_entries,
                }
                json_batches = {
                    "count": len(batch_entries),
                    "batches": batch_entries,
                }
                json_withdrawals = {
                    "count": len(withdrawal_entries),
                    "withdrawals": withdrawal_entries,
                }
                record_files = [
                    ("documents.json", json_docs),
                    ("annotations.json", json_annotations),
                    ("publications.json", json_publications),
                    ("publication-batches.json", json_batches),
                    ("withdrawals.json", json_withdrawals),
                ]
                for name, payload in record_files:
                    raw = json.dumps(payload, ensure_ascii=False, indent=2,
                                    sort_keys=False).encode("utf-8")
                    file_entries.append(
                        {
                            "path": name,
                            "kind": "records",
                            "sha256": _sha256_bytes(raw),
                            "file_count": 1,
                        }
                    )
                    zip_members.append((name, raw))

                file_entries.sort(key=lambda e: e["path"])
                zip_members.sort(key=lambda m: m[0])

                generated_at = _utcnow()
                manifest = {
                    "generated_at": generated_at,
                    "snapshot": {
                        # 归档只对应这一个一致时点；时间仅作人工标识，
                        # 真正的一致性由读快照保证。
                        "note": "all records and referenced files were read"
                                " from one read-only database snapshot",
                    },
                    "record_counts": {
                        "documents": len(doc_entries),
                        "annotation_revisions":
                            json_annotations["revision_count"],
                        "publications": len(publication_entries),
                        "publication_batches": len(batch_entries),
                        "withdrawals": len(withdrawal_entries),
                    },
                    # 归档内除清单外的文件总数（原图 + 成品 + 记录 JSON）
                    "file_count": len(file_entries),
                    "files": file_entries,
                }
                manifest_raw = json.dumps(
                    manifest, ensure_ascii=False, indent=2
                ).encode("utf-8")

                # 完整写出 ZIP 并正常关闭（写完中央目录）后才离开本块；
                # 写盘失败转换为统一业务错误（随外层 except 清理临时文件、
                # 整单拒绝），绝不交付一个未完整生成的 ZIP。
                try:
                    with zipfile.ZipFile(tmp_path, "w",
                                         zipfile.ZIP_DEFLATED) as zf:
                        for relpath, data in zip_members:
                            zf.writestr(relpath, data)
                        # 清单最后写入；它列出此前所有成员的 SHA-256 与文件数。
                        zf.writestr("manifest.json", manifest_raw)
                except OSError as exc:
                    _error(
                        500,
                        "archive_write_failed",
                        f"failed to fully write the archive ZIP: {exc}",
                    )
            # 事务以 rollback 结束（纯只读），ZIP 已在临时文件上完整生成。
        except Exception:
            _cleanup_tempfile(tmp_path)
            raise
        background_tasks.add_task(_cleanup_tempfile, tmp_path)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return FileResponse(
            tmp_path,
            media_type="application/zip",
            filename=f"service-data-archive-{stamp}.zip",
        )

    # ------------- 服务数据归档核验（运维交接前，纯只读、不落服务数据目录） -------------

    @app.post("/service-data-archives/verify")
    async def verify_service_data_archive(
        file: UploadFile = File(...),
    ) -> dict:
        """核验一个服务数据归档 ZIP 是否完整可信（交接前调用）。

        请求：``multipart/form-data``，字段名 ``file``，内容为本服务
        ``POST /service-data-archives`` 生成的归档 ZIP。

        核验**纯只读**：上传内容只流式写入**系统临时目录**中的临时文件
        （核验完成后立即删除），**绝不解压到 ``DATA_DIR``**，不打开/写入
        数据库，也不改写任何现有记录；全部结论只依据 ZIP 内的
        ``manifest.json``、五类记录 JSON 与引用成员本身。

        - 通过：``200 {"valid": true, "member_count", "record_counts",
          "file_count", "issues": []}``，``record_counts`` 给出五类记录计数；
        - 不可信：``200 {"valid": false, ..., "issues": [{code,message,...}]}``，
          每个问题附 ``path``/``index``/``doc_id``/``version``/``field`` 等
          可定位字段（缺项、重复项、摘要错误、关系冲突均整体判失败，
          且尽量一次性列全）；
        - 请求本身无效（空文件、超过 ``MAX_VERIFY_MB``、不是 ZIP）：
          分别返回 400/413 与统一错误体，不产生任何副作用。
        """
        # 流式落系统临时文件，避免大归档整体驻留内存；绝不写到 DATA_DIR。
        tmp_fd, tmp_path = tempfile.mkstemp(prefix="svc-verify-", suffix=".zip")
        total = 0
        too_large = False
        try:
            with os.fdopen(tmp_fd, "wb") as out:
                while True:
                    chunk = await file.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > settings.max_verify_bytes:
                        too_large = True
                        break
                    out.write(chunk)
            if too_large:
                _error(
                    413,
                    "payload_too_large",
                    f"archive is larger than the verification limit of"
                    f" {settings.max_verify_bytes} bytes",
                )
            if total == 0:
                _error(400, "empty_upload", "uploaded archive file is empty")
            result = verify_archive_path(tmp_path)
        finally:
            _cleanup_tempfile(tmp_path)
        # 核验不通过是正常的业务结论（HTTP 200 + valid=false），不是服务错误：
        # 调用方据 valid 与 issues 决定是否接收该归档。
        return result

    return app


app = create_app()
