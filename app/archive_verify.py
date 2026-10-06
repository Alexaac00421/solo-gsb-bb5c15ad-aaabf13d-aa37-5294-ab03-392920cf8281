"""服务数据归档 ZIP 的交接前核验（纯只读、不落盘到服务数据目录）。

运维拿到本服务 ``POST /service-data-archives`` 产出的归档 ZIP 后，在交接前
调用核验入口（见 :func:`verify_archive_bytes` /
:func:`verify_archive_path`）判断归档是否**完整可信**。核验全程只读：

- **不解压到服务数据目录**：只在 ZIP 内部按成员名读取字节，不把任何成员
  写到磁盘（上传的 ZIP 自身由路由放在系统临时目录，核验后立即删除）；
- **不触碰数据库、不改写任何现有记录**：全部结论只依据 ZIP 内的
  ``manifest.json`` 与五类记录 JSON 及其引用成员；
- **失败可定位**：收集全部可定位问题（成员、清单路径、记录条目、
  doc_id/version 等），随统一结论返回；结构性损坏无法继续时才提前返回。

核验内容（与归档端 :mod:`app.main` 的产物结构严格对应）：

1. ZIP 结构可打开；成员名**唯一**；全部为规定的**相对路径**
   （无前导/盘符/反斜杠/``..``/空段）；
2. 五类记录 JSON 与 ``manifest.json`` 必须存在；其余成员只能落在
   ``originals/`` 与 ``publications/`` 下；
3. 清单与实际成员**一一对应**：清单条目不缺不重，每个路径都是真实成员，
   实际成员也都在清单中；``file_count`` 等于非清单成员数；
4. 逐项 **SHA-256** 匹配（清单值 == 包内该成员实际字节摘要），
   条目的 ``kind`` 与文档/版本标识、``file_count`` 与路径一致；
5. 五类记录 JSON 的计数（``count``/``revision_count``/
   ``document_count``）与各自数组长度、与清单 ``record_counts`` 一致；
6. 引用关系闭环：文档 ↔ 连续标注修订 ↔ 发布版本 ↔ 批次成员（顺序）↔
   撤回记录；原图与发布成品的摘要、相对路径也与记录一致，且无游离文件。
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from typing import Any

# 五类记录文件与清单的固定成员名（顶层相对路径）
RECORD_FILES: tuple[str, ...] = (
    "documents.json",
    "annotations.json",
    "publications.json",
    "publication-batches.json",
    "withdrawals.json",
)
MANIFEST_NAME = "manifest.json"

# 服务端文档 ID：uuid4().hex（32 位小写十六进制）；成品版本号：从 1 起整数
_DOC_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ORIGINAL_RE = re.compile(r"^originals/([0-9a-f]{32})\.(png|jpg)$")
_PUBLICATION_RE = re.compile(r"^publications/([0-9a-f]{32})/v([1-9][0-9]*)\.png$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

_ORIGINAL_EXT = {"image/png": ".png", "image/jpeg": ".jpg"}

# 记录文件 -> 清单 record_counts 使用的计数键
_RECORD_COUNT_KEY = {
    "documents.json": "documents",
    "annotations.json": "annotation_revisions",
    "publications.json": "publications",
    "publication-batches.json": "publication_batches",
    "withdrawals.json": "withdrawals",
}


class ArchiveVerifier:
    """对单个归档 ZIP 做一次性核验；通过 :meth:`run` 返回结论字典。"""

    def __init__(self, zf: zipfile.ZipFile) -> None:
        self.zf = zf
        self.issues: list[dict[str, Any]] = []
        # 供关系核验阶段使用的解析结果
        self.manifest: dict[str, Any] | None = None
        self.records: dict[str, Any] = {}
        # 已通过路径结构校验的清单条目（path -> entry）
        self.entries_by_path: dict[str, dict[str, Any]] = {}

    # ---------------- 基础设施 ----------------

    def _issue(self, code: str, message: str, **where: Any) -> None:
        """登记一条可定位问题。where 提供 path/doc_id/version/field 等定位字段。"""
        item: dict[str, Any] = {"code": code, "message": message}
        item.update(where)
        self.issues.append(item)

    @staticmethod
    def _is_relative(name: str) -> bool:
        """成员名是否为允许的相对路径：无反斜杠/盘符/前导斜杠/空段/'.'/'..'。"""
        if not name or name.startswith(("/", "\\")):
            return False
        if "\\" in name or ":" in name:
            return False
        parts = name.split("/")
        if any(part in ("", ".", "..") for part in parts):
            return False
        return True

    @staticmethod
    def _is_int(value: Any) -> bool:
        # bool 是 int 的子类型，须显式排除
        return isinstance(value, int) and not isinstance(value, bool)

    def _sha256(self, name: str) -> str | None:
        """读取成员字节并计算 SHA-256；读取/CRC 失败时登记并返回 None。"""
        try:
            data = self.zf.read(name)
        except Exception as exc:  # BadZipFile/KeyError/CRC 错误等
            self._issue(
                "archive_member_unreadable",
                f"zip member '{name}' cannot be read: {exc}",
                path=name,
            )
            return None
        return hashlib.sha256(data).hexdigest()

    # ---------------- 核验阶段 ----------------

    def run(self) -> dict[str, Any]:
        # 阶段 A：成员名唯一性与相对路径结构（结构问题尽量全量收集）
        infos = self.zf.infolist()
        names = [i.filename for i in infos]
        seen: set[str] = set()
        for name in names:
            if name in seen:
                self._issue(
                    "duplicate_zip_member",
                    f"zip contains more than one member named '{name}'",
                    path=name,
                )
            seen.add(name)
            if not self._is_relative(name):
                self._issue(
                    "illegal_member_path",
                    f"zip member name '{name}' is not a permitted relative path"
                    " (no leading slash, drive letter, backslash or '..' segments)",
                    path=name,
                )

        # 目录条目（名称以 / 结尾）不是文件成员，归档不应包含
        for name in names:
            if name.endswith("/"):
                self._issue(
                    "unexpected_directory_member",
                    f"zip contains a directory entry '{name}'; the archive stores"
                    " files only with flat relative paths",
                    path=name,
                )

        # 清单缺失则后续校验无从进行
        if MANIFEST_NAME not in seen:
            self._issue(
                "missing_manifest",
                "required top-level 'manifest.json' is absent from the archive",
                path=MANIFEST_NAME,
            )
            return self._result(names)

        # 五类记录文件缺失检查（后续解析阶段会跳过缺失项）
        for rf in RECORD_FILES:
            if rf not in seen:
                self._issue(
                    "missing_record_file",
                    f"required record file '{rf}' is absent from the archive",
                    path=rf,
                )

        # 阶段 B：解析清单（结构错误则无法按清单逐项核验，提前结束）
        if not self._load_manifest():
            return self._result(names)

        # 阶段 C：清单条目 vs 实际成员（一一对应、SHA-256、kind/标识/file_count）
        self._verify_entries(seen)

        # 阶段 D：非清单成员必须落在规定目录、且无清单之外的游离文件
        actual_files = {n for n in seen if not n.endswith("/")}
        for name in actual_files - {MANIFEST_NAME}:
            if name in RECORD_FILES:
                continue
            if _ORIGINAL_RE.match(name) or _PUBLICATION_RE.match(name):
                continue
            self._issue(
                "unexpected_member",
                f"zip member '{name}' is outside the permitted archive layout"
                " (top-level record JSON, originals/ or publications/)",
                path=name,
            )

        # 阶段 E：解析五类记录 JSON 并核对其内部计数
        records_ok = self._load_records()
        if records_ok:
            self._verify_record_counts()
            # 阶段 F：跨记录引用关系、原图/成品摘要与路径闭环
            self._verify_relations()

        return self._result(names)

    def _result(self, names: list[str]) -> dict[str, Any]:
        valid = not self.issues
        result: dict[str, Any] = {
            "valid": valid,
            "member_count": len(names),
            "issues": self.issues,
        }
        if valid:
            counts = self.manifest["record_counts"] if self.manifest else {}
            result["record_counts"] = counts
            result["file_count"] = self.manifest["file_count"] if self.manifest else None
        return result

    # ---------------- 阶段 B：清单 ----------------

    def _load_manifest(self) -> bool:
        raw = self._sha256(MANIFEST_NAME)
        if raw is None:
            return False
        try:
            text = self.zf.read(MANIFEST_NAME)
            manifest = json.loads(text.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._issue(
                "manifest_invalid_json",
                f"'{MANIFEST_NAME}' is not valid UTF-8 JSON: {exc}",
                path=MANIFEST_NAME,
            )
            return False
        if not isinstance(manifest, dict):
            self._issue(
                "manifest_invalid_structure",
                "'manifest.json' must be a JSON object",
                path=MANIFEST_NAME,
            )
            return False

        ok = True
        files = manifest.get("files")
        if not isinstance(files, list):
            self._issue(
                "manifest_invalid_structure",
                "'manifest.json' field 'files' must be a list",
                path=MANIFEST_NAME, field="files",
            )
            ok = False
        file_count = manifest.get("file_count")
        if not self._is_int(file_count) or file_count < 0:
            self._issue(
                "manifest_invalid_structure",
                "'manifest.json' field 'file_count' must be a non-negative integer",
                path=MANIFEST_NAME, field="file_count",
            )
            ok = False
        counts = manifest.get("record_counts")
        if not isinstance(counts, dict):
            self._issue(
                "manifest_invalid_structure",
                "'manifest.json' field 'record_counts' must be an object",
                path=MANIFEST_NAME, field="record_counts",
            )
            ok = False
        else:
            for key in _RECORD_COUNT_KEY.values():
                value = counts.get(key)
                if not self._is_int(value) or value < 0:
                    self._issue(
                        "manifest_invalid_structure",
                        f"record_counts['{key}'] must be a non-negative integer",
                        path=MANIFEST_NAME, field=f"record_counts.{key}",
                    )
                    ok = False
        if not ok:
            return False
        self.manifest = manifest
        return True

    # ---------------- 阶段 C：清单条目 ↔ 成员、SHA-256 ----------------

    def _verify_entries(self, actual_names: set[str]) -> None:
        assert self.manifest is not None
        manifest = self.manifest
        entries = manifest["files"]

        listed_paths: set[str] = set()
        for index, entry in enumerate(entries):
            loc = {"manifest_index": index}
            if not isinstance(entry, dict):
                self._issue(
                    "manifest_invalid_entry",
                    f"manifest files[{index}] must be an object",
                    **loc,
                )
                continue
            path = entry.get("path")
            if not isinstance(path, str) or not path:
                self._issue(
                    "manifest_invalid_entry",
                    f"manifest files[{index}] has no string 'path'",
                    **loc,
                )
                continue
            loc["path"] = path
            sha = entry.get("sha256")
            kind = entry.get("kind")
            entry_fc = entry.get("file_count")

            if path in listed_paths:
                self._issue(
                    "duplicate_manifest_entry",
                    f"manifest lists path '{path}' more than once",
                    **loc,
                )
            listed_paths.add(path)

            if path == MANIFEST_NAME:
                self._issue(
                    "manifest_lists_itself",
                    "manifest must not list 'manifest.json' in its own 'files'",
                    **loc,
                )
            if path not in actual_names:
                self._issue(
                    "manifest_member_missing",
                    f"manifest lists '{path}' but the zip has no such member",
                    **loc,
                )
            if not self._is_relative(path):
                self._issue(
                    "illegal_member_path",
                    f"manifest path '{path}' is not a permitted relative path",
                    **loc,
                )
            if not isinstance(sha, str) or not _HEX64_RE.match(sha):
                self._issue(
                    "manifest_invalid_entry",
                    f"manifest entry '{path}' has an invalid sha256",
                    field="sha256", **loc,
                )
            elif path in actual_names:
                actual_sha = self._sha256(path)
                if actual_sha is not None and actual_sha != sha.lower():
                    self._issue(
                        "manifest_sha256_mismatch",
                        f"sha256 in manifest for '{path}' ({sha}) does not match the"
                        f" actual member bytes ({actual_sha})",
                        expected=sha, actual=actual_sha, **loc,
                    )
            if kind not in ("original", "publication", "records"):
                self._issue(
                    "manifest_invalid_entry",
                    f"manifest entry '{path}' has invalid kind {kind!r}",
                    field="kind", **loc,
                )
            else:
                self._verify_entry_identity(entry, kind, **loc)
            if not self._is_int(entry_fc) or entry_fc != 1:
                self._issue(
                    "manifest_file_count_invalid",
                    f"manifest entry '{path}' file_count must be 1, got"
                    f" {entry_fc!r}",
                    field="file_count", **loc,
                )

            if isinstance(path, str):
                self.entries_by_path[path] = entry

        # 实际非清单成员必须都出现在清单中（反向一一对应）
        for name in actual_names - {MANIFEST_NAME}:
            if name not in listed_paths:
                self._issue(
                    "member_not_in_manifest",
                    f"zip member '{name}' is present but not listed in the manifest",
                    path=name,
                )

        # file_count == 非清单成员数 == 清单条目数
        non_manifest_members = len(actual_names - {MANIFEST_NAME})
        if manifest["file_count"] != non_manifest_members:
            self._issue(
                "manifest_file_count_mismatch",
                f"manifest file_count is {manifest['file_count']} but the archive"
                f" contains {non_manifest_members} members excluding manifest.json",
                field="file_count",
                expected=non_manifest_members,
                actual=manifest["file_count"],
            )
        if len(entries) != non_manifest_members:
            self._issue(
                "manifest_file_count_mismatch",
                f"manifest lists {len(entries)} file entries but the archive contains"
                f" {non_manifest_members} members excluding manifest.json",
                field="files",
                expected=non_manifest_members,
                actual=len(entries),
            )

    def _verify_entry_identity(self, entry: dict[str, Any], kind: str,
                               **loc: Any) -> None:
        """清单条目的 kind/标识与路径必须一致（original/publication/records）。"""
        path = entry["path"]
        doc_id = entry.get("doc_id")
        version = entry.get("version", None)

        def _bad(msg: str, field: str) -> None:
            self._issue("manifest_identity_mismatch", msg, field=field, **loc)

        if kind == "records":
            if path not in RECORD_FILES:
                _bad(f"entry '{path}' is marked 'records' but is not one of the five"
                     " top-level record files", "kind")
            if "doc_id" in entry or "version" in entry:
                _bad(f"records entry '{path}' must not carry doc_id/version",
                     "doc_id")
            return

        if kind == "original":
            m = _ORIGINAL_RE.match(path)
            if not m:
                _bad(f"entry '{path}' is marked 'original' but its path is not"
                     " originals/{doc_id}.png|.jpg", "kind")
            elif doc_id != m.group(1):
                _bad(f"original entry '{path}' doc_id {doc_id!r} does not match its"
                     f" path ({m.group(1)})", "doc_id")
            elif not isinstance(doc_id, str) or not _DOC_ID_RE.match(doc_id):
                _bad(f"original entry '{path}' has invalid doc_id {doc_id!r}",
                     "doc_id")
            if "version" in entry:
                _bad(f"original entry '{path}' must not carry a version", "version")
            return

        # publication
        m = _PUBLICATION_RE.match(path)
        if not m:
            _bad(f"entry '{path}' is marked 'publication' but its path is not"
                 " publications/{doc_id}/v{n}.png", "kind")
            return
        if doc_id != m.group(1):
            _bad(f"publication entry '{path}' doc_id {doc_id!r} does not match its"
                 f" path ({m.group(1)})", "doc_id")
        elif not isinstance(doc_id, str) or not _DOC_ID_RE.match(doc_id):
            _bad(f"publication entry '{path}' has invalid doc_id {doc_id!r}",
                 "doc_id")
        path_version = int(m.group(2))
        if not self._is_int(version) or version != path_version:
            _bad(f"publication entry '{path}' version {version!r} does not match its"
                 f" path (v{path_version})", "version")

    # ---------------- 阶段 E：记录 JSON 解析与计数 ----------------

    def _load_records(self) -> bool:
        ok = True
        for rf in RECORD_FILES:
            try:
                text = self.zf.read(rf)
                payload = json.loads(text.decode("utf-8"))
            except KeyError:
                # 缺失已在阶段 A 登记
                ok = False
                continue
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._issue(
                    "record_invalid_json",
                    f"record file '{rf}' is not valid UTF-8 JSON: {exc}",
                    path=rf,
                )
                ok = False
                continue
            if not isinstance(payload, dict):
                self._issue(
                    "record_invalid_structure",
                    f"record file '{rf}' must be a JSON object",
                    path=rf,
                )
                ok = False
                continue
            self.records[rf] = payload

        # 五个文件全部可解析为对象才进入结构/关系核验，避免在残缺数据上
        # 产生大量连带的伪问题（各文件的具体问题已分别登记，可定位）。
        if not ok or len(self.records) != len(RECORD_FILES):
            return False
        return self._validate_record_shapes()

    def _validate_record_shapes(self) -> bool:
        ok = True

        def _need_list(rf: str, key: str) -> list[Any] | None:
            value = self.records[rf].get(key)
            if not isinstance(value, list):
                self._issue(
                    "record_invalid_structure",
                    f"'{rf}' field '{key}' must be a list",
                    path=rf, field=key,
                )
                return None
            return value

        def _need_int(rf: str, key: str) -> bool:
            value = self.records[rf].get(key)
            if not self._is_int(value) or value < 0:
                self._issue(
                    "record_invalid_structure",
                    f"'{rf}' field '{key}' must be a non-negative integer",
                    path=rf, field=key,
                )
                return False
            return True

        docs = _need_list("documents.json", "documents")
        if _need_int("documents.json", "count"):
            pass
        ann_doc_list = _need_list("annotations.json", "documents")
        for key in ("document_count", "revision_count"):
            _need_int("annotations.json", key)
        pubs = _need_list("publications.json", "publications")
        _need_int("publications.json", "count")
        batches = _need_list("publication-batches.json", "batches")
        _need_int("publication-batches.json", "count")
        withdrawals = _need_list("withdrawals.json", "withdrawals")
        _need_int("withdrawals.json", "count")

        # ---- documents ----
        if docs is not None:
            for i, d in enumerate(docs):
                loc = {"path": "documents.json", "index": i,
                       "doc_id": d.get("doc_id") if isinstance(d, dict) else None}
                if not isinstance(d, dict):
                    self._issue("record_invalid_entry",
                                f"documents[{i}] must be an object", **loc)
                    ok = False
                    continue
                if not self._valid_doc_id(d.get("doc_id")):
                    self._issue("record_invalid_entry",
                                f"documents[{i}] has invalid doc_id",
                                field="doc_id", **loc)
                    ok = False
                for field in ("original_sha256",):
                    if not isinstance(d.get(field), str) or not _HEX64_RE.match(
                        d[field]
                    ):
                        self._issue("record_invalid_entry",
                                    f"documents[{i}] has invalid {field}",
                                    field=field, **loc)
                        ok = False
                if d.get("media_type") not in _ORIGINAL_EXT:
                    self._issue("record_invalid_entry",
                                f"documents[{i}] has invalid media_type",
                                field="media_type", **loc)
                    ok = False
                if not isinstance(d.get("original_file"), str):
                    self._issue("record_invalid_entry",
                                f"documents[{i}] has invalid original_file",
                                field="original_file", **loc)
                    ok = False
                if not self._is_int(d.get("annotation_revision")) or \
                        d["annotation_revision"] < 0:
                    self._issue("record_invalid_entry",
                                f"documents[{i}] has invalid annotation_revision",
                                field="annotation_revision", **loc)
                    ok = False
                for field in ("width", "height"):
                    if not self._is_int(d.get(field)) or d[field] <= 0:
                        self._issue("record_invalid_entry",
                                    f"documents[{i}] has invalid {field}",
                                    field=field, **loc)
                        ok = False

        # ---- annotations ----
        if ann_doc_list is not None:
            for i, ad in enumerate(ann_doc_list):
                loc = {"path": "annotations.json", "index": i,
                       "doc_id": ad.get("doc_id") if isinstance(ad, dict) else None}
                if not isinstance(ad, dict):
                    self._issue("record_invalid_entry",
                                f"annotation documents[{i}] must be an object",
                                **loc)
                    ok = False
                    continue
                if not self._valid_doc_id(ad.get("doc_id")):
                    self._issue("record_invalid_entry",
                                f"annotation documents[{i}] has invalid doc_id",
                                field="doc_id", **loc)
                    ok = False
                if not self._is_int(ad.get("current_revision")) or \
                        ad["current_revision"] < 0:
                    self._issue("record_invalid_entry",
                                f"annotation documents[{i}] has invalid"
                                " current_revision",
                                field="current_revision", **loc)
                    ok = False
                revisions = ad.get("revisions")
                if not isinstance(revisions, list):
                    self._issue("record_invalid_entry",
                                f"annotation documents[{i}] 'revisions' must be a"
                                " list", field="revisions", **loc)
                    ok = False
                    continue
                for j, rev in enumerate(revisions):
                    rloc = {**loc, "revision_index": j}
                    if not isinstance(rev, dict):
                        self._issue("record_invalid_entry",
                                    f"revisions[{j}] must be an object", **rloc)
                        ok = False
                        continue
                    if not self._is_int(rev.get("revision")) or rev["revision"] < 0:
                        self._issue("record_invalid_entry",
                                    f"revisions[{j}] has invalid revision number",
                                    field="revision", **rloc)
                        ok = False
                    rects = rev.get("rects")
                    rc = rev.get("rect_count")
                    if not isinstance(rects, list):
                        self._issue("record_invalid_entry",
                                    f"revisions[{j}] 'rects' must be a list",
                                    field="rects", **rloc)
                        ok = False
                    elif not self._is_int(rc) or rc != len(rects):
                        self._issue("record_invalid_entry",
                                    f"revisions[{j}] rect_count {rc!r} does not"
                                    f" match {len(rects)} rects",
                                    field="rect_count", **rloc)
                        ok = False

        # ---- publications ----
        if pubs is not None:
            for i, p in enumerate(pubs):
                loc = {"path": "publications.json", "index": i,
                       "doc_id": p.get("doc_id") if isinstance(p, dict) else None,
                       "version": p.get("version") if isinstance(p, dict) else None}
                if not isinstance(p, dict):
                    self._issue("record_invalid_entry",
                                f"publications[{i}] must be an object", **loc)
                    ok = False
                    continue
                if not self._valid_doc_id(p.get("doc_id")):
                    self._issue("record_invalid_entry",
                                f"publications[{i}] has invalid doc_id",
                                field="doc_id", **loc)
                    ok = False
                if not self._is_int(p.get("version")) or p["version"] < 1:
                    self._issue("record_invalid_entry",
                                f"publications[{i}] has invalid version",
                                field="version", **loc)
                    ok = False
                if not self._is_int(p.get("annotation_revision")) or \
                        p["annotation_revision"] < 0:
                    self._issue("record_invalid_entry",
                                f"publications[{i}] has invalid"
                                " annotation_revision",
                                field="annotation_revision", **loc)
                    ok = False
                for field in ("original_sha256", "output_sha256"):
                    if not isinstance(p.get(field), str) or not _HEX64_RE.match(
                        p[field]
                    ):
                        self._issue("record_invalid_entry",
                                    f"publications[{i}] has invalid {field}",
                                    field=field, **loc)
                        ok = False
                if not isinstance(p.get("output_file"), str):
                    self._issue("record_invalid_entry",
                                f"publications[{i}] has invalid output_file",
                                field="output_file", **loc)
                    ok = False
                if not isinstance(p.get("withdrawn"), bool):
                    self._issue("record_invalid_entry",
                                f"publications[{i}] 'withdrawn' must be boolean",
                                field="withdrawn", **loc)
                    ok = False

        # ---- batches ----
        if batches is not None:
            for i, b in enumerate(batches):
                loc = {"path": "publication-batches.json", "index": i,
                       "batch_id": b.get("batch_id") if isinstance(b, dict) else None}
                if not isinstance(b, dict):
                    self._issue("record_invalid_entry",
                                f"batches[{i}] must be an object", **loc)
                    ok = False
                    continue
                if not isinstance(b.get("batch_id"), str) or not b["batch_id"]:
                    self._issue("record_invalid_entry",
                                f"batches[{i}] has invalid batch_id",
                                field="batch_id", **loc)
                    ok = False
                members = b.get("documents")
                if not isinstance(members, list):
                    self._issue("record_invalid_entry",
                                f"batches[{i}] 'documents' must be a list",
                                field="documents", **loc)
                    ok = False
                    continue
                for j, m in enumerate(members):
                    mloc = {**loc, "position_index": j}
                    if not isinstance(m, dict):
                        self._issue("record_invalid_entry",
                                    f"batch documents[{j}] must be an object", **mloc)
                        ok = False
                        continue
                    if not self._is_int(m.get("position")) or m["position"] < 0:
                        self._issue("record_invalid_entry",
                                    f"batch documents[{j}] has invalid position",
                                    field="position", **mloc)
                        ok = False
                    if not self._valid_doc_id(m.get("doc_id")):
                        self._issue("record_invalid_entry",
                                    f"batch documents[{j}] has invalid doc_id",
                                    field="doc_id", **mloc)
                        ok = False
                    if not self._is_int(m.get("version")) or m["version"] < 1:
                        self._issue("record_invalid_entry",
                                    f"batch documents[{j}] has invalid version",
                                    field="version", **mloc)
                        ok = False

        # ---- withdrawals ----
        if withdrawals is not None:
            for i, w in enumerate(withdrawals):
                loc = {"path": "withdrawals.json", "index": i,
                       "doc_id": w.get("doc_id") if isinstance(w, dict) else None,
                       "version": w.get("version") if isinstance(w, dict) else None}
                if not isinstance(w, dict):
                    self._issue("record_invalid_entry",
                                f"withdrawals[{i}] must be an object", **loc)
                    ok = False
                    continue
                if not self._valid_doc_id(w.get("doc_id")):
                    self._issue("record_invalid_entry",
                                f"withdrawals[{i}] has invalid doc_id",
                                field="doc_id", **loc)
                    ok = False
                if not self._is_int(w.get("version")) or w["version"] < 1:
                    self._issue("record_invalid_entry",
                                f"withdrawals[{i}] has invalid version",
                                field="version", **loc)
                    ok = False
                if not isinstance(w.get("reason"), str) or not w["reason"].strip():
                    self._issue("record_invalid_entry",
                                f"withdrawals[{i}] has empty reason",
                                field="reason", **loc)
                    ok = False
                if not isinstance(w.get("withdrawn_at"), str) or \
                        not w["withdrawn_at"]:
                    self._issue("record_invalid_entry",
                                f"withdrawals[{i}] has empty withdrawn_at",
                                field="withdrawn_at", **loc)
                    ok = False

        return ok

    @staticmethod
    def _valid_doc_id(value: Any) -> bool:
        return isinstance(value, str) and bool(_DOC_ID_RE.match(value))

    def _verify_record_counts(self) -> None:
        """五个记录文件的内部计数、清单 record_counts 与数组长度一致。"""
        expected: dict[str, int] = {}

        docs = self.records["documents.json"].get("documents")
        anns = self.records["annotations.json"]
        pubs = self.records["publications.json"].get("publications")
        batches = self.records["publication-batches.json"].get("batches")
        withdrawals = self.records["withdrawals.json"].get("withdrawals")

        # 结构已在 _validate_record_shapes 中校验；防御性跳过非列表
        if isinstance(docs, list):
            expected["documents"] = len(docs)
        if isinstance(pubs, list):
            expected["publications"] = len(pubs)
        if isinstance(batches, list):
            expected["publication_batches"] = len(batches)
        if isinstance(withdrawals, list):
            expected["withdrawals"] = len(withdrawals)
        revision_total = 0
        ann_doc_list = anns.get("documents")
        if isinstance(ann_doc_list, list):
            for ad in ann_doc_list:
                revisions = ad.get("revisions") if isinstance(ad, dict) else None
                if isinstance(revisions, list):
                    revision_total += len(revisions)
        expected["annotation_revisions"] = revision_total

        # 记录文件自带计数 vs 数组长度
        self._check_declared_count("documents.json", "count",
                                   self.records["documents.json"].get("count"),
                                   len(docs) if isinstance(docs, list) else None)
        self._check_declared_count(
            "annotations.json", "document_count",
            anns.get("document_count"),
            len(ann_doc_list) if isinstance(ann_doc_list, list) else None)
        self._check_declared_count(
            "annotations.json", "revision_count",
            anns.get("revision_count"), revision_total)
        self._check_declared_count(
            "publications.json", "count",
            self.records["publications.json"].get("count"),
            len(pubs) if isinstance(pubs, list) else None)
        self._check_declared_count(
            "publication-batches.json", "count",
            self.records["publication-batches.json"].get("count"),
            len(batches) if isinstance(batches, list) else None)
        self._check_declared_count(
            "withdrawals.json", "count",
            self.records["withdrawals.json"].get("count"),
            len(withdrawals) if isinstance(withdrawals, list) else None)

        # 清单 record_counts vs 实际
        assert self.manifest is not None
        for key, actual in expected.items():
            declared = self.manifest["record_counts"].get(key)
            if declared != actual:
                self._issue(
                    "record_count_mismatch",
                    f"manifest record_counts['{key}'] is {declared} but the record"
                    f" files contain {actual} entries",
                    field=f"record_counts.{key}",
                    expected=actual, actual=declared,
                )

    def _check_declared_count(self, path: str, field: str, declared: Any,
                              actual: int | None) -> None:
        if actual is None or not self._is_int(declared):
            return
        if declared != actual:
            self._issue(
                "record_count_mismatch",
                f"'{path}' {field} is {declared} but its array has {actual} entries",
                path=path, field=field, expected=actual, actual=declared,
            )

    # ---------------- 阶段 F：跨记录引用关系闭环 ----------------

    def _verify_relations(self) -> None:
        docs_list = self.records["documents.json"]["documents"]
        ann_doc_list = self.records["annotations.json"]["documents"]
        pubs_list = self.records["publications.json"]["publications"]
        batches_list = self.records["publication-batches.json"]["batches"]
        withdrawals_list = self.records["withdrawals.json"]["withdrawals"]

        # ---- 文档：唯一性、标注文档对应、原图路径/摘要 ----
        docs_by_id: dict[str, dict[str, Any]] = {}
        for i, d in enumerate(docs_list):
            doc_id = d["doc_id"]
            if doc_id in docs_by_id:
                self._issue("duplicate_record_entry",
                            f"document '{doc_id}' appears more than once",
                            path="documents.json", index=i, doc_id=doc_id)
                continue
            docs_by_id[doc_id] = d

            expected_original = (
                f"originals/{doc_id}{_ORIGINAL_EXT[d['media_type']]}"
            )
            if d["original_file"] != expected_original:
                self._issue(
                    "record_relation_conflict",
                    f"document '{doc_id}' original_file {d['original_file']!r} does"
                    f" not match the required path {expected_original!r} for its"
                    " media type",
                    path="documents.json", index=i, doc_id=doc_id,
                    field="original_file",
                )
            entry = self.entries_by_path.get(expected_original)
            if entry is None:
                self._issue(
                    "referenced_file_missing",
                    f"document '{doc_id}' references original"
                    f" '{expected_original}' which is absent from the archive"
                    " manifest",
                    doc_id=doc_id, path=expected_original,
                )
            else:
                if entry.get("sha256") != d["original_sha256"]:
                    self._issue(
                        "record_digest_conflict",
                        f"document '{doc_id}' original_sha256"
                        f" {d['original_sha256'][:12]}… does not match the manifest"
                        f" digest of '{expected_original}'"
                        f" ({entry.get('sha256', '')[:12]}…)",
                        doc_id=doc_id, path=expected_original,
                        field="original_sha256",
                    )
                if entry.get("kind") != "original" or entry.get("doc_id") != doc_id:
                    self._issue(
                        "record_relation_conflict",
                        f"original '{expected_original}' manifest identity does not"
                        f" match document '{doc_id}'",
                        doc_id=doc_id, path=expected_original,
                    )

        # ---- 标注：每文档恰好一份、当前修订一致、修订从 0 连续且矩形在界内 ----
        ann_by_doc: dict[str, dict[str, Any]] = {}
        for i, ad in enumerate(ann_doc_list):
            doc_id = ad["doc_id"]
            if doc_id in ann_by_doc:
                self._issue(
                    "duplicate_record_entry",
                    f"annotation document '{doc_id}' appears more than once",
                    path="annotations.json", index=i, doc_id=doc_id,
                )
                continue
            ann_by_doc[doc_id] = ad
            if doc_id not in docs_by_id:
                self._issue(
                    "record_relation_conflict",
                    f"annotations reference unknown document '{doc_id}'",
                    path="annotations.json", index=i, doc_id=doc_id,
                )
                continue
            if ad["current_revision"] != docs_by_id[doc_id]["annotation_revision"]:
                self._issue(
                    "record_relation_conflict",
                    f"document '{doc_id}' current revision is"
                    f" {docs_by_id[doc_id]['annotation_revision']} but annotations"
                    f" record {ad['current_revision']}",
                    path="annotations.json", index=i, doc_id=doc_id,
                    field="current_revision",
                )
            revisions = ad["revisions"]
            revision_numbers = [r["revision"] for r in revisions]
            expected_range = list(range(ad["current_revision"] + 1))
            if revision_numbers != expected_range:
                self._issue(
                    "record_relation_conflict",
                    f"document '{doc_id}' annotation revisions"
                    f" {revision_numbers} are not the continuous sequence"
                    f" 0..{ad['current_revision']}",
                    path="annotations.json", index=i, doc_id=doc_id,
                )
            doc = docs_by_id[doc_id]
            for rev in revisions:
                for k, rect in enumerate(rev.get("rects", [])):
                    if not self._rect_in_bounds(rect, doc):
                        self._issue(
                            "record_relation_conflict",
                            f"document '{doc_id}' revision {rev['revision']}"
                            f" rect {k} is malformed or outside image bounds"
                            f" {doc['width']}x{doc['height']}",
                            path="annotations.json", doc_id=doc_id,
                            revision=rev["revision"], rect_index=k,
                        )

        for doc_id in docs_by_id:
            if doc_id not in ann_by_doc:
                self._issue(
                    "record_relation_conflict",
                    f"document '{doc_id}' has no annotations record (every document"
                    " must carry a continuous revision history from revision 0)",
                    path="annotations.json", doc_id=doc_id,
                )

        # ---- 发布：唯一、文档/修订存在、版本连续、原图/成品摘要与路径、撤回/批次 ----
        pubs_by_pair: dict[tuple[str, int], dict[str, Any]] = {}
        version_numbers_by_doc: dict[str, list[int]] = {}
        for i, p in enumerate(pubs_list):
            key = (p["doc_id"], p["version"])
            if key in pubs_by_pair:
                self._issue(
                    "duplicate_record_entry",
                    f"publication {p['doc_id']} v{p['version']} appears more than"
                    " once",
                    path="publications.json", index=i, doc_id=p["doc_id"],
                    version=p["version"],
                )
                continue
            pubs_by_pair[key] = p
            version_numbers_by_doc.setdefault(p["doc_id"], []).append(p["version"])
            loc = {"path": "publications.json", "index": i,
                   "doc_id": p["doc_id"], "version": p["version"]}
            doc = docs_by_id.get(p["doc_id"])
            if doc is None:
                self._issue(
                    "record_relation_conflict",
                    f"publication {p['doc_id']} v{p['version']} references unknown"
                    " document",
                    **loc,
                )
                continue
            ann = ann_by_doc.get(p["doc_id"])
            if ann is not None and (
                p["annotation_revision"] > ann["current_revision"]
                or p["annotation_revision"] not in {
                    r["revision"] for r in ann["revisions"]
                }
            ):
                self._issue(
                    "record_relation_conflict",
                    f"publication {p['doc_id']} v{p['version']} references"
                    f" annotation revision {p['annotation_revision']} which does"
                    f" not exist (current {ann['current_revision']})",
                    field="annotation_revision", **loc,
                )
            if p["original_sha256"] != doc["original_sha256"]:
                self._issue(
                    "record_digest_conflict",
                    f"publication {p['doc_id']} v{p['version']} original_sha256"
                    " differs from the document's original digest",
                    field="original_sha256", **loc,
                )
            expected_output = (
                f"publications/{p['doc_id']}/v{p['version']}.png"
            )
            if p["output_file"] != expected_output:
                self._issue(
                    "record_relation_conflict",
                    f"publication {p['doc_id']} v{p['version']} output_file"
                    f" {p['output_file']!r} does not match {expected_output!r}",
                    field="output_file", **loc,
                )
            entry = self.entries_by_path.get(expected_output)
            # 该类问题定位到具体成品成员（覆盖 loc 中默认的记录文件路径）
            out_loc = {**loc, "path": expected_output}
            if entry is None:
                self._issue(
                    "referenced_file_missing",
                    f"publication {p['doc_id']} v{p['version']} references output"
                    f" '{expected_output}' which is absent from the archive manifest",
                    **out_loc,
                )
            else:
                if entry.get("sha256") != p["output_sha256"]:
                    self._issue(
                        "record_digest_conflict",
                        f"publication {p['doc_id']} v{p['version']} output_sha256"
                        " does not match the manifest digest of"
                        f" '{expected_output}'",
                        field="output_sha256", **out_loc,
                    )
                if entry.get("kind") != "publication" or \
                        entry.get("doc_id") != p["doc_id"] or \
                        entry.get("version") != p["version"]:
                    self._issue(
                        "record_relation_conflict",
                        f"output '{expected_output}' manifest identity does not"
                        " match the publication record",
                        **out_loc,
                    )

        # 版本号在每个文档维度从 1 连续
        for doc_id, versions in version_numbers_by_doc.items():
            if sorted(versions) != list(range(1, len(versions) + 1)):
                self._issue(
                    "record_relation_conflict",
                    f"publication versions for document '{doc_id}' are"
                    f" {sorted(versions)}, not the continuous sequence"
                    f" 1..{len(versions)}",
                    path="publications.json", doc_id=doc_id,
                )

        # ---- 撤回：唯一、指向已发布版本；撤回标记双向一致 ----
        withdrawals_by_pair: dict[tuple[str, int], dict[str, Any]] = {}
        withdrawn_pairs: set[tuple[str, int]] = set()
        for i, w in enumerate(withdrawals_list):
            key = (w["doc_id"], w["version"])
            if key in withdrawals_by_pair:
                self._issue(
                    "duplicate_record_entry",
                    f"withdrawal {w['doc_id']} v{w['version']} appears more than"
                    " once",
                    path="withdrawals.json", index=i, doc_id=w["doc_id"],
                    version=w["version"],
                )
                continue
            withdrawals_by_pair[key] = w
            if key not in pubs_by_pair:
                self._issue(
                    "record_relation_conflict",
                    f"withdrawal {w['doc_id']} v{w['version']} references a"
                    " non-existent publication",
                    path="withdrawals.json", index=i, doc_id=w["doc_id"],
                    version=w["version"],
                )

        for key, p in pubs_by_pair.items():
            w = withdrawals_by_pair.get(key)
            if p["withdrawn"] != (w is not None):
                self._issue(
                    "record_relation_conflict",
                    f"publication {key[0]} v{key[1]} withdrawn flag is"
                    f" {p['withdrawn']} but a withdrawal record"
                    f" {'exists' if w else 'does not exist'}",
                    path="publications.json", doc_id=key[0], version=key[1],
                    field="withdrawn",
                )
            if w is not None:
                withdrawn_pairs.add(key)
                if p.get("withdrawn_at") != w["withdrawn_at"]:
                    self._issue(
                        "record_relation_conflict",
                        f"publication {key[0]} v{key[1]} withdrawn_at"
                        f" {p.get('withdrawn_at')!r} does not match the withdrawal"
                        f" record {w['withdrawn_at']!r}",
                        path="publications.json", doc_id=key[0], version=key[1],
                        field="withdrawn_at",
                    )
                if p.get("withdrawal_reason") != w["reason"]:
                    self._issue(
                        "record_relation_conflict",
                        f"publication {key[0]} v{key[1]} withdrawal_reason does"
                        " not match the withdrawal record",
                        path="publications.json", doc_id=key[0], version=key[1],
                        field="withdrawal_reason",
                    )

        # ---- 批次：唯一、成员 position 连续且与发布记录一一对应 ----
        batches_by_id: dict[str, dict[str, Any]] = {}
        batched_pairs: set[tuple[str, int]] = set()
        for i, b in enumerate(batches_list):
            batch_id = b["batch_id"]
            if batch_id in batches_by_id:
                self._issue(
                    "duplicate_record_entry",
                    f"batch '{batch_id}' appears more than once",
                    path="publication-batches.json", index=i, batch_id=batch_id,
                )
                continue
            batches_by_id[batch_id] = b
            members = b["documents"]
            positions = [m["position"] for m in members]
            if positions != list(range(len(members))):
                self._issue(
                    "record_relation_conflict",
                    f"batch '{batch_id}' member positions {positions} are not the"
                    " continuous sequence 0..n-1 in request order",
                    path="publication-batches.json", index=i, batch_id=batch_id,
                )
            member_pairs: set[tuple[str, int]] = set()
            for m in members:
                pair = (m["doc_id"], m["version"])
                if pair in member_pairs:
                    self._issue(
                        "record_relation_conflict",
                        f"batch '{batch_id}' lists {m['doc_id']} v{m['version']}"
                        " more than once",
                        path="publication-batches.json", batch_id=batch_id,
                        doc_id=m["doc_id"], version=m["version"],
                    )
                member_pairs.add(pair)
                p = pubs_by_pair.get(pair)
                if p is None:
                    self._issue(
                        "record_relation_conflict",
                        f"batch '{batch_id}' member {m['doc_id']} v{m['version']}"
                        " references a non-existent publication",
                        path="publication-batches.json", batch_id=batch_id,
                        doc_id=m["doc_id"], version=m["version"],
                    )
                    continue
                batched_pairs.add(pair)
                if p.get("batch_id") != batch_id:
                    self._issue(
                        "record_relation_conflict",
                        f"publication {m['doc_id']} v{m['version']} batch_id is"
                        f" {p.get('batch_id')!r}, not batch '{batch_id}' that lists"
                        " it",
                        path="publications.json", doc_id=m["doc_id"],
                        version=m["version"], field="batch_id",
                    )
                if p.get("publish_at") != b.get("publish_at"):
                    self._issue(
                        "record_relation_conflict",
                        f"publication {m['doc_id']} v{m['version']} publish_at"
                        f" {p.get('publish_at')!r} does not match its batch"
                        f" publish_at {b.get('publish_at')!r}",
                        path="publications.json", doc_id=m["doc_id"],
                        version=m["version"], field="publish_at",
                    )

        # 发布记录的 batch_id 反向指向存在的批次，且该批次确实包含它
        for key, p in pubs_by_pair.items():
            batch_id = p.get("batch_id")
            if batch_id is None:
                continue
            if batch_id not in batches_by_id:
                self._issue(
                    "record_relation_conflict",
                    f"publication {key[0]} v{key[1]} references unknown batch"
                    f" '{batch_id}'",
                    path="publications.json", doc_id=key[0], version=key[1],
                    field="batch_id",
                )
            elif key not in batched_pairs:
                self._issue(
                    "record_relation_conflict",
                    f"publication {key[0]} v{key[1]} claims batch '{batch_id}' but"
                    " that batch does not list it as a member",
                    path="publications.json", doc_id=key[0], version=key[1],
                    field="batch_id",
                )

        # ---- 反向闭环：清单中的原图/成品都必须被记录引用（无游离文件）----
        for path, entry in self.entries_by_path.items():
            kind = entry.get("kind")
            if kind == "original":
                doc_id = entry.get("doc_id")
                if doc_id not in docs_by_id:
                    self._issue(
                        "unreferenced_archive_file",
                        f"archive original '{path}' is not referenced by any"
                        " document record",
                        path=path, doc_id=doc_id,
                    )
                elif docs_by_id[doc_id]["original_file"] != path:
                    self._issue(
                        "record_relation_conflict",
                        f"archive original '{path}' is not the declared original_file"
                        f" ({docs_by_id[doc_id]['original_file']!r}) of document"
                        f" '{doc_id}'",
                        path=path, doc_id=doc_id,
                    )
            elif kind == "publication":
                pair = (entry.get("doc_id"), entry.get("version"))
                if pair not in pubs_by_pair:
                    self._issue(
                        "unreferenced_archive_file",
                        f"archive publication '{path}' is not referenced by any"
                        " publication record",
                        path=path, doc_id=entry.get("doc_id"),
                        version=entry.get("version"),
                    )
                elif pubs_by_pair[pair]["output_file"] != path:
                    self._issue(
                        "record_relation_conflict",
                        f"archive publication '{path}' is not the declared output_file"
                        f" ({pubs_by_pair[pair]['output_file']!r}) of publication"
                        f" {pair[0]} v{pair[1]}",
                        path=path, doc_id=pair[0], version=pair[1],
                    )

    @staticmethod
    def _rect_in_bounds(rect: Any, doc: dict[str, Any]) -> bool:
        """归档标注矩形的最小自洽性：整数、w/h>0、不越界、原因非空字符串。"""
        if not isinstance(rect, dict):
            return False
        for field in ("x", "y", "w", "h"):
            value = rect.get(field)
            if not ArchiveVerifier._is_int(value):
                return False
        if not isinstance(rect.get("reason"), str) or not rect["reason"].strip():
            return False
        x, y, w, h = rect["x"], rect["y"], rect["w"], rect["h"]
        if w <= 0 or h <= 0:
            return False
        if x < 0 or y < 0:
            return False
        return x + w <= doc["width"] and y + h <= doc["height"]


def verify_archive_bytes(data: bytes) -> dict[str, Any]:
    """核验内存中的归档 ZIP 字节，返回核验结论（见模块文档）。"""
    if not zipfile.is_zipfile(io.BytesIO(data)):
        return {
            "valid": False,
            "member_count": 0,
            "issues": [{
                "code": "not_a_zip",
                "message": "the uploaded content is not a valid ZIP archive"
                           " (bad or truncated central directory)",
            }],
        }
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        return {
            "valid": False,
            "member_count": 0,
            "issues": [{
                "code": "not_a_zip",
                "message": f"cannot open ZIP archive: {exc}",
            }],
        }
    with zf:
        return ArchiveVerifier(zf).run()


def verify_archive_path(path: str) -> dict[str, Any]:
    """核验磁盘上（如上传临时文件）的归档 ZIP；只读打开、不改动、不解压。"""
    if not zipfile.is_zipfile(path):
        return {
            "valid": False,
            "member_count": 0,
            "issues": [{
                "code": "not_a_zip",
                "message": "the uploaded file is not a valid ZIP archive",
            }],
        }
    with zipfile.ZipFile(path) as zf:
        return ArchiveVerifier(zf).run()
