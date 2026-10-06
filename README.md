# 档案影像脱敏发布服务

档案员上传 PNG/JPEG 原图，在 **EXIF 旋正后的像素坐标系** 上标注敏感矩形及原因；
发布时把当前标注矩形不可逆涂实为纯黑，生成剥离元数据的独立 PNG，并固化
原图摘要、标注修订号与成品摘要。已发布成品不随后续标注变化而改变；
公开读取只暴露发布版与遮盖信息，永不返回原图。发布（单份或同批次）时可
指定统一的 **UTC 公开时间**：到点前公开版详情与图片一律 404，到点后自动
可读；省略时仍立即公开。已发布版本发现遮盖遗漏时，
可凭文档 ID、版本号与非空原因**永久撤回**：公开读取立即拒绝（优先于公开
时间），发布记录、成品文件与版本号序列全部保留。

技术栈：Python 3.11+ · FastAPI · Pillow · SQLite（WAL）

离线档案员还可以一次性请求 **2～20 个互不重复的已公开脱敏版本**，服务返回一个
**ZIP 交付包**：内含各版本的发布版 PNG 与一份 `manifest.json` 清单（按请求顺序记录
文档 ID、版本号、成品 SHA-256、遮盖数及包内文件名；包内文件名只由请求位置、服务端
文档 ID 与版本号生成，**绝不取自上传文件名**）。未知版本、未到公开时间或已撤回的
版本只要出现一项即**整包拒绝**且不泄露该项成品/遮盖详情；重复项、数量越界同样明确
报错。打包的资格确认与撤回**串行**：撤回先生效则拒绝，打包先确认则本次交付完成。

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

- 服务地址：http://localhost:8000
- 交互式 API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

数据持久化在名为 `redaction-data` 的卷中（SQLite 库、原图、发布成品）。

## 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

## 配置

| 环境变量        | 默认值   | 说明                                       |
| --------------- | -------- | ------------------------------------------ |
| `DATA_DIR`      | `./data`（容器内 `/data`） | SQLite 库、原图、发布成品的存放根目录 |
| `MAX_UPLOAD_MB` | `25`     | 单个上传文件大小上限（MB）                 |
| `MAX_VERIFY_MB` | `512`    | 归档核验上传 ZIP 大小上限（MB）；核验只读临时文件、不解压到 `DATA_DIR` |

监听地址/端口通过 uvicorn 参数（`--host`/`--port`）或 Compose 的 `ports` 映射调整。
首次启动时应用自动建目录、建表（初始化无需手工步骤）。

## API 一览

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/documents` | 上传原图（multipart 字段名 `file`），返回 `doc_id` 与旋正后宽高 |
| GET  | `/documents/{doc_id}` | 文档元数据与发布列表 |
| GET  | `/documents/{doc_id}/annotations` | 当前标注修订号与矩形 |
| PUT  | `/documents/{doc_id}/annotations` | 整体替换标注，须携带当前修订号 |
| POST | `/annotation-migrations` | **遮盖方案迁移**：源当前非空标注按旋正后宽高分别缩放，整体替换 1～20 份同版式不同尺寸目标（原子：全成功或全部不变） |
| POST | `/documents/{doc_id}/publish` | 按当前标注发布新版本（涂实+去元数据 PNG）；可选 JSON 体 `{"publish_at": "..."}` 指定 UTC 公开时间，省略立即公开 |
| GET  | `/documents/{doc_id}/publications` | 发布版本列表（含公开时间、撤回状态） |
| POST | `/documents/{doc_id}/publications/{version}/withdraw` | 凭非空原因**永久撤回**该版本（重复撤回返回首次记录） |
| GET  | `/documents/{doc_id}/publications/{version}/withdrawal` | 查询该版本的撤回状态、时间与原因 |
| POST | `/publication-batches` | **同批次发布 2～20 份文档**（原子：全成功或整批失败）；可选 `idempotency_key` 防重发、可选 `publish_at` 指定整批统一 UTC 公开时间 |
| GET  | `/publication-batches/{batch_id}` | 按批次标识查询清单（顺序与请求一致，含公开时间、撤回状态） |
| GET  | `/publications/{doc_id}/{version}` | 公开读取：版本元数据+遮盖坐标与原因（未到公开时间 404） |
| GET  | `/publications/{doc_id}/{version}/image` | 公开读取：发布版 PNG（未到公开时间 404） |
| POST | `/documents/{doc_id}/coverage-diff` | **重发前遮盖范围比较**（只读）：当前标注修订号 + 基准发布版本号 → 新增/失去遮盖像素数、占旋正后图像比例、失去遮盖水平段 |
| POST | `/publication-packages` | **离线交付包**：按请求顺序提交 2～20 个互不重复的 `(doc_id, version)`，成功返回 ZIP（各版 PNG + `manifest.json` 清单）；任一项未知/未到公开时间/已撤回即整包拒绝 |
| POST | `/service-data-archives` | **服务数据归档（运维）**：无请求体，返回同一数据库快照的 ZIP——文档/标注/发布/撤回记录 + 该快照引用的原图与发布成品（相对路径）+ `manifest.json`（SHA-256、文档/版本标识、文件数）；任一引用文件缺失或摘要不符即整单拒绝 |
| POST | `/service-data-archives/verify` | **归档核验（运维交接前，纯只读）**：上传归档 ZIP（multipart 字段 `file`），核对成员名唯一且均为规定相对路径、清单与实际成员一一对应、计数与 SHA-256 匹配，以及文档↔连续标注修订↔发布版本↔批次成员顺序↔撤回记录的引用关系与原图/成品摘要；通过返回各类记录计数，失败返回可定位原因并整体判失败。**不解压到服务数据目录、不改写任何现有记录** |

## 调用示例（curl）

```bash
BASE=http://localhost:8000

# 1) 上传原图 -> 记下 doc_id、width/height（EXIF 旋正后的坐标系）
DOC=$(curl -s -F "file=@scan.jpg" $BASE/documents)
DOC_ID=$(echo "$DOC" | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])")

# 2) 标注敏感矩形（x,y 为左上角，w/h 为宽高，坐标基于旋正后图像）
curl -s -X PUT $BASE/documents/$DOC_ID/annotations \
  -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":120,"y":80,"w":200,"h":40,"reason":"含身份证号"}]}'
# -> {"doc_id":"...","revision":1,...}

# 2b) 携带过期修订号会被拒绝：HTTP 409 {"detail":{"code":"stale_revision","current_revision":1,...}}

# 3) 发布 -> 固化 annotation_revision、original_sha256、output_sha256
curl -s -X POST $BASE/documents/$DOC_ID/publish
# -> {"version":1,"annotation_revision":1,"output_sha256":"...","publish_at":null,...}

# 3b) 也可指定统一的 UTC 公开时间（晚于当前时刻、不超过 30 天）：
#     发布当下即固化成品并占用版本号，但到点前公开读取返回 404
curl -s -X POST $BASE/documents/$DOC_ID/publish \
  -H 'Content-Type: application/json' \
  -d '{"publish_at":"2026-10-06T08:00:00Z"}'
# -> {"version":2,...,"publish_at":"2026-10-06T08:00:00Z"}

# 4) 公开读取（不含原图）
curl -s $BASE/publications/$DOC_ID/1          # 版本元数据 + 遮盖坐标/原因
curl -s -OJ $BASE/publications/$DOC_ID/1/image # 发布版 PNG

# 5) 发现遮盖遗漏：凭文档 ID + 版本号 + 非空原因永久撤回 v1
curl -s -X POST $BASE/documents/$DOC_ID/publications/1/withdraw \
  -H 'Content-Type: application/json' \
  -d '{"reason":"遮盖遗漏：右下角工号未遮盖"}'
# -> HTTP 201 {"withdrawn":true,"withdrawn_at":"...","withdrawal_reason":"...",...}
# 重复撤回 -> HTTP 200 返回首次记录，原因不被改写

# 6) 撤回后公开读取返回 410；详情/列表仍保留记录并标识撤回状态
curl -s $BASE/publications/$DOC_ID/1           # 410 publication_withdrawn
curl -s $BASE/documents/$DOC_ID/publications   # v1 条目 withdrawn=true
```

重发影像前，先比较**当前标注**与**指定已发布版本**的遮盖范围（纯只读，
不产生新版本、不改任何记录；基准取该版本发布时冻结的标注）：

```bash
# 请求：当前标注修订号（乐观锁，过期 409）+ 基准发布版本号
curl -s -X POST $BASE/documents/$DOC_ID/coverage-diff \
  -H 'Content-Type: application/json' \
  -d '{"revision":3,"baseline_version":1}'
# -> 200
# {"doc_id":"...","current_revision":3,"baseline_version":1,
#  "baseline_annotation_revision":1,"width":64,"height":48,
#  "total_pixels":3072,
#  "added_pixels":160,"added_ratio":0.052083333333333336,
#  "lost_pixels":96,"lost_ratio":0.03125,
#  "lost_segments":[{"y0":8,"y1":20,"intervals":[{"x0":8,"x1":16}]}]}
# 基准不可见时不泄露其遮盖详情：未知版本 404 unknown_version、
# 未到公开时间 404 publication_not_yet_public、已撤回 410 publication_withdrawn
```

完整可运行脚本：`examples/demo.sh`（单份流程）、`examples/demo_batch.sh`
（批次发布流程）、`examples/demo_idempotency.sh`（批次幂等键流程）、
`examples/demo_scheduled_publication.sh`（定时公开流程）、
`examples/demo_withdrawal.sh`（版本撤回流程）、
`examples/demo_migration.sh`（遮盖方案迁移流程）与
`examples/demo_coverage_diff.sh`（重发前遮盖范围比较流程）；
先启动服务，再执行 `bash examples/demo_scheduled_publication.sh`。

## 重发前遮盖范围比较（同一读取快照，纯只读）

档案员重发影像前，需要确认**当前标注**相对某个**已发布版本**多遮了什么、
漏遮了什么。调用 `POST /documents/{doc_id}/coverage-diff`，请求体携带
**当前标注修订号**与**基准发布版本号**：

```json
{"revision": 3, "baseline_version": 1}
```

- **比较口径**：两套矩形都在 **EXIF 旋正后的像素坐标系**上，按**半开像素
  区间** `[x, x+w) × [y, y+h)` 处理。**重叠矩形的同一像素只计一次**；
  **边界相接不算重叠**（如 `[0,10)` 与 `[10,20)` 不共享任何像素）。
  新增遮盖 = 当前像素并集 − 基准并集；失去遮盖 = 基准并集 − 当前并集。
- **返回内容**（`200`）：`added_pixels`/`lost_pixels`（像素数）、
  `added_ratio`/`lost_ratio`（各自占**旋正后图像总像素**
  `width*height` 的比例）、`total_pixels`，以及按**水平边界段**列出的
  失去遮盖区间 `lost_segments`：每段为像素行区间 `{"y0","y1"}`
  （半开 `[y0, y1)`），段内 `intervals` 按 x 升序列出失去遮盖的像素列
  区间 `{"x0","x1"}`（半开 `[x0, x1)`）。**相邻段的区间列表完全相同时
  合并为一段**，区间不同（哪怕只差一列）则不合并；没有失去遮盖的段不输出。
  另附 `baseline_annotation_revision` 标明基准实际冻结的标注修订。
- **基准必须是发布时冻结的标注**：基准矩形取自发布行固化的
  `annotation_revision`，历史修订矩形按 `(doc_id, revision)` 追加存储、
  永不改写——因此发布之后的任何标注修改、迁移或新发布都**不影响**本次
  比较结果；响应不含矩形原因，只报告像素集合差异。
- **同一读取快照**：文档、基准发布记录、撤回/公开时间与两套矩形全部在
  **同一个只读快照事务**（延迟 `BEGIN` 的 WAL 读快照）中读取，比较进行中
  并发提交的标注修改/发布/撤回不会让两边读到不一致状态。比较**纯只读**：
  不获取写锁、不生成版本、不写任何改动记录，可安全重复调用。
- **拒绝且不泄露基准遮盖详情**（沿用统一错误体与既有错误码，顺序与公开
  读取一致）：未知文档 `404 document_not_found`；基准版本不存在
  `404 unknown_version`；基准**未到公开时间** `404
  publication_not_yet_public`（与"版本不存在"同形态）；基准**已撤回**
  `410 publication_withdrawn`（撤回优先于公开时间，时间到达也不比）；
  提交的当前修订号不等于服务端当前修订号时 `409 stale_revision`（附
  `current_revision`/`supplied_revision`，重新拉取修订号后重试）。以上
  拒绝响应都不携带基准遮盖坐标、区间、摘要或撤回原因。

端到端示例（含新增/失去计算、区间合并及各类拒绝）：
`bash examples/demo_coverage_diff.sh`。

## 遮盖方案迁移（同版式、不同尺寸，整批原子）

档案员要把一份**已标注**影像的遮盖方案套用到**同版式、不同尺寸**的多份影像时，
调用 `POST /annotation-migrations`。请求体携带**源文档 ID、源当前标注修订号**，
以及按顺序列出的 **1～20 个不同目标文档 ID 与各自预期修订号**（源不能作为
目标）：

- **只用源当前非空标注**：读取源在 `source_revision`（必须等于其当前修订号）
  下的矩形；当前一个矩形都没有（从未标注或已清空）时返回
  `400 source_annotations_empty`，不迁移任何内容。
- **按宽、高分别缩放**：以各文档 **EXIF 旋正后的宽/高** 为基准，宽方向与
  高方向独立缩放；**左上边界向下取整、右下边界向上取整**（宁可多遮、绝不
  漏遮），再把结果**限制在目标图内**；矩形的**原因原样保留、顺序与源一致**。
  全部为整数运算，源矩形合法（w/h > 0）时迁移结果必然 w/h ≥ 1 且不越界。
- **整体替换 + 修订号 +1**：仅当源与**所有目标**都存在、且源修订号与各目标
  预期修订号**全部匹配**时，才在**同一个 `BEGIN IMMEDIATE` 事务**内把缩放后
  的矩形作为各目标的**新当前标注**整体写入（旧修订矩形按
  `(doc_id, revision)` 追加保留、永不改写），各目标修订号各加一。
- **任一校验失败则全部不变**：数量越界、目标重复、源混入目标、源/目标
  不存在、源或任一目标修订号不匹配、源标注为空——失败响应按统一错误体返回，
  目标侧问题附 `position`（从 0 起）/`doc_id`，修订过期另附
  `current_revision`/`supplied_revision`；**所有目标标注与修订号保持原样**，
  不产生部分迁移，可用修正后的参数重试。
- **源与既有发布版不变**：源标注只被读取（修订号、矩形均不改）；目标的历史
  发布版绑定的是各自旧修订矩形，迁移不影响任何已发布成品。迁移事务与并发的
  标注修改、单份/批次发布（及撤回、打包等持写锁的操作）**互斥串行**。

```bash
BASE=http://localhost:8000
curl -sS -X POST "$BASE/annotation-migrations" \
  -H 'Content-Type: application/json' \
  -d '{
    "source_doc_id": "SRC_DOC_ID",
    "source_revision": 1,
    "targets": [
      {"doc_id": "BIG_DOC_ID",   "expected_revision": 0},
      {"doc_id": "SMALL_DOC_ID", "expected_revision": 0}
    ]
  }'
```

成功 `200`：返回源文档 ID、所用源修订号，以及按**请求顺序**排列的各目标
新修订号与映射矩形（坐标基于各目标旋正后的图像）：

```json
{
  "source_doc_id": "…",
  "source_revision": 1,
  "count": 2,
  "targets": [
    {"position": 0, "doc_id": "BIG_DOC_ID", "revision": 1,
     "rects": [
       {"x": 0, "y": 0, "w": 200, "h": 100, "reason": "整页边框"},
       {"x": 66, "y": 14, "w": 6, "h": 6, "reason": "印章"}
     ]},
    {"position": 1, "doc_id": "SMALL_DOC_ID", "revision": 1,
     "rects": [
       {"x": 0, "y": 0, "w": 50, "h": 25, "reason": "整页边框"},
       {"x": 16, "y": 3, "w": 2, "h": 2, "reason": "印章"}
     ]}
  ]
}
```

上例源图 100x50、源矩形 `(33,7,3,3)`：放大到 200x100 得 `(66,14,6,6)`；
缩小到 50x25 时 x 方向左边界 `floor(33·50/100)=16`、右边界
`ceil(36·50/100)=18`（即 w=2），y 方向上边界 `floor(7·25/50)=3`、
下边界 `ceil(10·25/50)=5`（即 h=2）。迁移后可用
`GET /documents/{doc_id}/annotations` 核对，或直接
`POST /documents/{doc_id}/publish` 按迁移结果发布。

错误码：`invalid_migration_size`（400，目标数不在 1～20，附 `count`）、
`duplicate_target_in_migration`（400，附 `position`/`doc_id`）、
`source_among_targets`（400）、`source_annotations_empty`（400）、
`document_not_found`（404；源侧附 `doc_id`，目标侧附 `position`/`doc_id`）、
`stale_revision`（409；源侧附 `doc_id`/`current_revision`/
`supplied_revision`，目标侧另附 `position`）。

端到端示例：`bash examples/demo_migration.sh`。


## 定时公开（统一 UTC 公开时间 `publish_at`）

单份发布与同批次发布都可以指定**统一的 UTC 公开时间**（RFC 3339 / ISO 8601
日期时间，**必须带时区偏移**，推荐尾部 `Z` 表示 UTC，如
`2026-10-06T08:00:00Z`；带偏移的时间会被换算并规范化为 UTC）：

- **窗口（仅首次提交校验）**：公开时间必须**严格晚于当前时刻**，且距当前
  **不超过 30 天**；过去时间、等于当前时刻、超过 30 天、无法解析或不带
  时区偏移均返回 `400 invalid_publish_time`。省略请求体、省略 `publish_at`
  或显式传 `null` 时行为与之前完全一致：**立即公开**（响应 `publish_at`
  为 `null`）。
- **发布当下即固化**：定时发布与普通发布走同一条路径——在发布事务内渲染
  涂实成品、写盘并占用**正常版本号**（`MAX(version)+1`），版本不可变；
  后续标注变化不影响该版本，后续发布继续续号。
- **到点前对外 404**：公开时间到达前，
  `GET /publications/{doc}/{version}` 与 `.../image` 一律返回
  `404 publication_not_yet_public`——与"版本不存在"同形态，**不返回遮盖
  坐标/原因、摘要，也不返回成品文件**。时间到达后**无需再次操作**即可读取。
- **响应携带公开时间**：单份发布响应在原字段后追加 `publish_at`；批次发布
  与批次查询在顶层返回 `publish_at`，批次各条目、文档详情、发布列表的发布
  条目也在原字段之后追加 `publish_at`（原字段及顺序不变；立即公开为
  `null`）。
- **永久撤回优先**：对未到公开时间的版本撤回后，在到点前后都返回
  `410 publication_withdrawn`，**时间到达也不会恢复**；未撤回版本到点后
  正常可读。
- **失败不留痕迹**：公开时间无效（或批次中任一文档失败）时不插入
  `publications`/批次记录、不落盘成品（落盘的也会清理）、不占用版本号与
  幂等键，可修正后用原参数/原键重试。

```bash
BASE=http://localhost:8000
# 单份：12 小时后公开
curl -s -X POST $BASE/documents/$DOC_ID/publish \
  -H 'Content-Type: application/json' \
  -d '{"publish_at":"2026-10-06T01:00:00Z"}'
# -> 201 {"version":1,...,"created_at":"...","publish_at":"2026-10-06T01:00:00Z"}
# 到点前：
#   GET /publications/$DOC_ID/1        -> 404 publication_not_yet_public
#   GET /publications/$DOC_ID/1/image  -> 404 publication_not_yet_public
# 到点后同样的请求自动变为 200，无需任何额外操作。

# 整批统一公开时间（顶层 publish_at 对批次内每份文档生效）
curl -s -X POST $BASE/publication-batches \
  -H 'Content-Type: application/json' \
  -d '{
    "publish_at": "2026-10-06T01:00:00Z",
    "idempotency_key": "req-20261005-01",
    "documents": [
      {"doc_id": "DOC_ID_1", "expected_revision": 1},
      {"doc_id": "DOC_ID_2", "expected_revision": 1}
    ]
  }'
# -> 201 {"batch_id":"...","count":2,"documents":[...],"publish_at":"2026-10-06T01:00:00Z"}
```

端到端示例（含到点前后变化、无效时间、幂等重放、撤回优先）：
`bash examples/demo_scheduled_publication.sh`。

## 同批次发布（2～20 份，整批原子）

档案员把一批**已标注**影像作为同一批次公开。请求携带 2～20 个**不同**文档 ID
及各自**预期标注修订号**：仅当全部文档都存在、且预期修订号仍与服务端当前修订号
匹配时，才在同一写事务内为每份文档按当前标注生成去元数据、纯黑涂实的独立发布版。
任一文档无效、修订过期、原图无法读取或成品写入失败，**整批失败**：不留下任何可
公开的部分版本，不占用任何版本号，也不生成批次标识。批次内渲染与落盘与同时发生
的标注修改、单份发布、其他批次发布互斥串行；已发布版本仍不可变，单份发布与公开
读取的响应及行为保持兼容（批次产物与单份产物共用同一张 `publications` 表、
同一个 `/publications/...` 公开入口）。

```bash
BASE=http://localhost:8000
curl -sS -X POST $BASE/publication-batches \
  -H 'Content-Type: application/json' \
  -d '{
    "documents": [
      {"doc_id": "DOC_ID_1", "expected_revision": 1},
      {"doc_id": "DOC_ID_2", "expected_revision": 1},
      {"doc_id": "DOC_ID_3", "expected_revision": 0}
    ]
  }'
```

成功 `201`：返回批次标识、创建时间，以及按**请求顺序**排列的各文档结果
（版本、固化的标注修订号、原图/成品摘要、遮盖数量）：

```json
{
  "batch_id": "7bc8e9008700469fb1c896990b34e620",
  "created_at": "2026-10-04T19:18:21+00:00",
  "count": 3,
  "documents": [
    {"position": 0, "doc_id": "DOC_ID_1", "version": 1,
     "annotation_revision": 1, "original_sha256": "…",
     "output_sha256": "…", "redaction_count": 1, "created_at": "…"},
    {"position": 1, "doc_id": "DOC_ID_2", "version": 1, "…": "…"},
    {"position": 2, "doc_id": "DOC_ID_3", "version": 1, "…": "…"}
  ],
  "publish_at": null
}
```

请求顶层可加 `"publish_at": "2026-10-06T08:00:00Z"` 为整批指定统一的
UTC 公开时间（规则见上一节）；响应与批次查询顶层的 `publish_at` 即该时间
（立即公开为 `null`），批次内每份文档到点前均 404、到点后自动可读。

按批次标识查询（清单顺序与原请求一致）：

```bash
curl -sS $BASE/publication-batches/7bc8e9008700469fb1c896990b34e620
```

每份文档的版本号仍在该文档维度内按 `MAX(version)+1` 连续分配，与单份发布
共用序列（先发过 v1 的文档，在成功批次里拿到 v2）。失败响应在统一错误体外
附带可定位信息：`position`（请求中的下标，从 0 起）、`doc_id`，修订过期时
还附 `current_revision`/`supplied_revision`：

```bash
# HTTP 409
# {"detail":{"code":"stale_revision","message":"…",
#   "position":1,"doc_id":"DOC_ID_2",
#   "current_revision":2,"supplied_revision":1}}
```

## 批次幂等键（可选，防重发）

批量发布时网络中断可能使成功响应丢失，调用方无法判断批次是否已生效。
`POST /publication-batches` 接受可选的 **`idempotency_key`**（1～128 字符，
调用方保证**全局唯一**）；键与**按请求顺序排列**的 `(doc_id, expected_revision)`
列表及整批统一的 **`publish_at`**（含"立即公开"的 `null`）绑定：

- **首次成功（201）**：键与完整成功结果在同一事务内持久化。
- **相同键 + 相同内容重试（200）**：直接返回首次持久化的原批次标识、各文档
  版本与摘要（响应体与首次逐字节一致）——即使标注随后已变化、**即使首次
  指定的公开时间此刻已经过去**，也**不再校验时间窗口**、不再校验当前修订号、
  不重新渲染、不产生第二批版本。
- **相同键 + 不同内容（409 `idempotency_key_conflict`）**：明确冲突，
  文档集、修订号、顺序或 **`publish_at`**（定时 vs 立即、不同时间）任一
  不同都算；原批次不受任何影响，冲突请求自身也不留任何痕迹。
- **失败不占键**：任一校验失败（数量/重复/文档缺失/修订过期/
  **`publish_at` 不在允许窗口内或格式无效**）、原图读取失败
  或成品落盘失败都不会占用键，也不产生部分版本；修正后可用**原键**重试。
- **并发重试最多发布一次**：键的检查、发布、登记在同一写事务内串行化，
  并发相同键请求中恰好一个首次成功（201），其余为重放（200），批次标识一致。
- 未带键的请求行为与之前完全一致：每次调用独立发布。

```bash
BASE=http://localhost:8000
KEY="req-$(date +%s)"   # 调用方生成全局唯一键

# 首次发布（201）
curl -sS -X POST $BASE/publication-batches \
  -H 'Content-Type: application/json' \
  -d "{
    \"idempotency_key\": \"$KEY\",
    \"documents\": [
      {\"doc_id\": \"DOC_ID_1\", \"expected_revision\": 1},
      {\"doc_id\": \"DOC_ID_2\", \"expected_revision\": 1}
    ]
  }"

# 网络中断后原样重发同一请求 -> HTTP 200，返回首次的批次标识/版本/摘要，
# 不生成第二批版本
curl -sS -X POST $BASE/publication-batches \
  -H 'Content-Type: application/json' \
  -d "{
    \"idempotency_key\": \"$KEY\",
    \"documents\": [
      {\"doc_id\": \"DOC_ID_1\", \"expected_revision\": 1},
      {\"doc_id\": \"DOC_ID_2\", \"expected_revision\": 1}
    ]
  }"

# 相同键但内容不同（文档集、修订号、顺序或 publish_at 任一变化）
# -> HTTP 409 {"detail":{"code":"idempotency_key_conflict","message":"…"}}
```

端到端示例：`bash examples/demo_idempotency.sh`。

## 版本撤回（永久，幂等）

已发布版本发现遮盖遗漏时，档案员凭**文档 ID + 版本号 + 非空原因**永久撤回：

```bash
BASE=http://localhost:8000
curl -sS -X POST $BASE/documents/$DOC_ID/publications/1/withdraw \
  -H 'Content-Type: application/json' \
  -d '{"reason":"遮盖遗漏：右下角工号未遮盖"}'
# 首次撤回 HTTP 201：
# {"doc_id":"…","version":1,"withdrawn":true,
#  "withdrawn_at":"2026-10-05T…+00:00","withdrawal_reason":"遮盖遗漏：…"}
```

- **永久且保留记录**：撤回只登记撤回记录，不删除 `publications` 行、成品
  文件或批次记录，版本号不复用——后续单份/批次发布继续 `MAX(version)+1`。
- **公开面立即生效（优先于公开时间）**：撤回后新发起的
  `GET /publications/{doc}/{version}` 与 `.../image` 返回
  **410 `publication_withdrawn`**，不返回成品与遮盖详情；即使该版本带有
  尚未到达的公开时间，时间到达也**不会恢复访问**。成品文件保留在磁盘上，
  撤回生效前已开始的下载允许完成。
- **幂等**：重复撤回返回首次记录（HTTP 200），撤回时间与原因不被改写；
  并发撤回与发布在同一写事务内串行化，恰好一个请求创建记录，
  失败不留部分撤回记录。
- **可查询**：`GET /documents/{doc_id}/publications/{version}/withdrawal`
  返回该版本的撤回状态、时间与原因；文档详情、发布列表、批次查询的每个
  版本条目均在原字段之后追加 `publish_at` 及
  `withdrawn`/`withdrawn_at`/`withdrawal_reason`
  （原字段及顺序不变；未撤回为 `false`/`null`/`null`，立即公开的
  `publish_at` 为 `null`）。
- **错误**：未知文档 404 `document_not_found`、未知版本 404
  `unknown_version`、空白原因 400 `reason_required`。
- **批次幂等键重放**仍原样返回首次保存的响应（HTTP 200），
  但不能恢复已撤回版本的公开访问。

端到端示例：`bash examples/demo_withdrawal.sh`。


## 离线交付包（ZIP：各版 PNG + JSON 清单）

档案员需要把多份**已公开的脱敏版本**交给离线接收方时，调用
`POST /publication-packages`，请求体按顺序列出 **2～20 个互不重复的
`(doc_id, version)` 对**（同一文档的不同版本可以同包；重复以"文档 ID + 版本号"
对判定）：

```bash
BASE=http://localhost:8000
curl -sS -o pkg.zip -X POST "$BASE/publication-packages" \
  -H 'Content-Type: application/json' \
  -d '{
    "items": [
      {"doc_id": "DOC_ID_C", "version": 1},
      {"doc_id": "DOC_ID_A", "version": 2},
      {"doc_id": "DOC_ID_A", "version": 1},
      {"doc_id": "DOC_ID_B", "version": 1}
    ]
  }'
# -> 200，Content-Type: application/zip，Content-Disposition: publication-package.zip
```

成功 `200` 返回 ZIP（默认下载名 `publication-package.zip`），结构为扁平的
各版 PNG 加一份 `manifest.json`：

```
01_DOC_ID_C_v1.png
02_DOC_ID_A_v2.png
03_DOC_ID_A_v1.png
04_DOC_ID_B_v1.png
manifest.json
```

- **包内文件名**格式为 `{两位请求序号}_{doc_id}_v{version}.png`，只由**请求位置、
  服务端文档 ID 与版本号**生成，**绝不取自上传文件名**；扁平结构（无目录），
  不随上传名发生路径穿越。
- **`manifest.json` 按请求顺序**逐项记录 `position`（从 0 起）、`doc_id`、
  `version`、`output_sha256`（包内该 PNG 成品的实际 SHA-256，接收方可据此校验）、
  `redaction_count`（该版遮盖矩形数）与 `file`（包内文件名）；顶层另有
  `created_at` 与 `count`：

```json
{
  "created_at": "2026-10-05T16:58:25+00:00",
  "count": 4,
  "items": [
    {"position": 0, "doc_id": "DOC_ID_C", "version": 1,
     "output_sha256": "0c87…", "redaction_count": 0,
     "file": "01_DOC_ID_C_v1.png"},
    {"position": 1, "doc_id": "DOC_ID_A", "version": 2,
     "output_sha256": "…", "redaction_count": 1,
     "file": "02_DOC_ID_A_v2.png"}
  ]
}
```

- **整包拒绝（绝不交付部分包）**：任一资格项失败都不读取任何成品、不生成 ZIP，
  返回统一错误体并附 `position`/`doc_id`/`version` 定位（错误项本身不返回
  成品摘要、遮盖数或撤回原因）：
  - 数量不在 2～20：`400 invalid_package_size`（附 `count`）；
  - `(doc_id, version)` 对重复：`400 duplicate_item_in_package`；
  - 未知文档 `404 document_not_found` / 未知版本 `404 unknown_version`；
  - 未到公开时间：`404 publication_not_yet_public`（与单份公开读取同形态，
    不泄露版本内容）；
  - 已撤回：`410 publication_withdrawn`（撤回优先于公开时间，不泄露成品与
    遮盖详情）；
  - 成品在磁盘上缺失 `500 artifact_missing`、读盘失败
    `500 artifact_read_failed`、实际字节摘要与发布记录不符
    `500 artifact_corrupt`：同样整包拒绝，不交付部分包。
- **资格确认与撤回串行**：数量/重复的结构性检查之后，全部项目的资格确认
  （存在性、撤回、公开时间）、成品读盘与摘要复算、ZIP 生成都在**同一个
  `BEGIN IMMEDIATE` 写事务**内完成，与版本撤回（以及发布、标注修改）互斥串行：
  - 撤回请求**先提交生效**：本次打包的资格确认看到撤回记录，整包拒绝；
  - 打包**先完成资格确认**：并发撤回被写锁挡到打包事务提交之后，**本次交付
    照常完成**（包内保留该版成品）；撤回随后生效，只影响之后新发起的读取与打包。
  这与"撤回不删除成品文件、生效前已开始的下载允许完成"的既有契约一致。
- 交付 ZIP 为服务端临时文件，响应发送完成后自动清理；既有发布、撤回、定时公开
  与公开读取契约、存储结构与启动方式均无改动。

端到端示例（含成功交付校验与各类整包拒绝）：`bash examples/demo_package.sh`。


## 服务数据归档（同一数据库快照，全有或全无）

运维人员需要导出某一**一致时点**的全部服务数据时，调用
`POST /service-data-archives`（**无请求体、纯只读**），成功 `200` 返回
`application/zip`（下载名 `service-data-archive-<UTC时间戳>.zip`）：

```bash
BASE=http://localhost:8000
curl -sS -o archive.zip -X POST "$BASE/service-data-archives"
# -> 200，Content-Type: application/zip
unzip -l archive.zip
```

归档内容是**同一个数据库快照**中的**全部已提交数据**（不做公开面门控：
未到公开时间与已撤回的版本同样收录），并附该快照引用到的原图与发布成品：

```
documents.json               # 全部文档（元数据 + 原图相对路径/摘要 + 当前修订号）
annotations.json             # 每份文档从 revision 0 起的各修订矩形（含已清空修订）
publications.json            # 全部发布记录（含 batch_id/publish_at/撤回状态）
publication-batches.json     # 全部发布批次及其成员（按 position）
withdrawals.json             # 全部撤回记录（doc_id/version/原因/时间）
originals/{doc_id}.png|.jpg  # 该快照引用的原图（每文档一份）
publications/{doc_id}/v{n}.png  # 该快照引用的发布成品（每个发布版本一份）
manifest.json                # 清单（最后写入）
```

- **相对路径组织**：包内只有相对路径（`originals/`、`publications/` 与顶层
  JSON），**不含任何服务端绝对路径**；原图文件名只由**服务端文档 ID** 与库内
  `media_type` 映射的扩展名构成（PNG→`.png`、JPEG→`.jpg`），**绝不取自上传
  文件名**，因此无路径穿越；上传名仅作为字段保留在 `documents.json` 中。
- **`manifest.json` 清单**：顶层 `record_counts`（文档/标注修订/发布/批次/
  撤回记录数）、`file_count`（除清单外的文件总数 = 原图 + 成品 + 5 个记录
  JSON）、`generated_at`，以及按相对路径排序的 `files` 列表，逐项给出：
  - `path`：归档内相对路径；
  - `sha256`：**该文件在包内的实际 SHA-256**（接收方可逐项校验；清单摘要与
    包内字节由测试保证一致）；
  - `kind`：`original` / `publication` / `records`；
  - **文档或版本标识**：原图与发布成品带 `doc_id`，发布成品另带 `version`；
  - `file_count`：该条目对应的文件数（恒为 1）。
- **只对应一个一致时点**：全部记录读取都在**一个只读快照事务**（延迟
  `BEGIN` 的 WAL 读快照）中完成，归档进行期间并发提交的标注修改/发布/撤回对
  本次归档**不可见**；引用文件（原图、成品）落盘后不可变，文件读盘与摘要
  复算也在同一快照事务内进行。事务**纯只读、不持写锁**，因此**生成期间并发
  写入可继续**，互不阻塞；归档不会把归档期间的新提交混入。
- **全有或全无（不提供部分归档）**：先用快照读取全部记录，再把**所有**引用
  文件读入并复算 SHA-256（原图比对 `documents.original_sha256`、成品比对
  `publications.output_sha256`），**全部通过后才一次性写 ZIP**；任一文件
  缺失/读盘失败/摘要不符都整单拒绝（返回 JSON 错误而非 ZIP），不交付部分
  归档。空库也会生成一份只含 5 个记录 JSON 与清单的合法归档。
- **ZIP 完整生成后才响应**：ZIP 在服务端临时文件上完整写出并正常关闭（写完
  中央目录）后才作为下载响应；任何失败（含 ZIP 写盘失败 `archive_write_failed`）
  都会删除临时文件，成功响应发送完成后临时文件同样自动清理。

```bash
# 失败示例：某份原图在磁盘上缺失 -> HTTP 500 application/json，无 ZIP、无部分归档
curl -sS -X POST "$BASE/service-data-archives"
# {"detail":{"code":"archive_original_missing",
#   "message":"stored original image referenced by the snapshot for document '…' is missing on disk",
#   "doc_id":"…"}}
```

端到端示例（含批次定时发布、撤回、清单摘要校验与原图缺失整单拒绝）：
`DATA_DIR=./data bash examples/demo_archive.sh`。


## 服务数据归档核验（运维交接前，纯只读、不落服务数据目录）

运维接收本服务生成的归档 ZIP 后，在**交接前**调用
`POST /service-data-archives/verify` 判断归档是否**完整可信**。请求为
`multipart/form-data`，字段名 `file`，内容即 `POST /service-data-archives`
返回的 ZIP。核验全程**纯只读**：上传内容只流式写入**系统临时目录**的临时
文件（核验结束立即删除），**绝不解压到 `DATA_DIR`**，不打开/写入数据库，
也**不改写任何现有记录**；全部结论只依据 ZIP 自身的 `manifest.json`、五类
记录 JSON 与引用成员。

```bash
BASE=http://localhost:8000

# 先取得归档（无请求体）
curl -sS -o archive.zip -X POST "$BASE/service-data-archives"

# 交接前核验（上传 ZIP）
curl -sS -X POST "$BASE/service-data-archives/verify" \
  -F "file=@archive.zip;type=application/zip"
```

- **通过**（HTTP `200`，`valid: true`）：返回 ZIP 成员数与五类记录计数，
  `issues` 为空数组：

```json
{
  "valid": true,
  "member_count": 10,
  "file_count": 9,
  "record_counts": {
    "documents": 2,
    "annotation_revisions": 3,
    "publications": 2,
    "publication_batches": 0,
    "withdrawals": 1
  },
  "issues": []
}
```

- **不可信**（仍为 HTTP `200`，`valid: false`）：**整体判失败**，`issues`
  尽量一次性列全，每条都带可定位字段（`path` / `manifest_index` /
  `index` / `doc_id` / `version` / `batch_id` / `field` 等）。核验不通过是
  正常业务结论而非服务错误，因此用 200 + `valid:false` 表达，调用方据 `valid`
  决定是否接收：

```json
{
  "valid": false,
  "member_count": 10,
  "issues": [
    {"code": "manifest_sha256_mismatch",
     "message": "sha256 in manifest for '…' (4668…) does not match the actual member bytes (dd70…)",
     "path": "publications/<doc_id>/v1.png",
     "expected": "4668…", "actual": "dd70…", "manifest_index": 6}
  ]
}
```

- **请求本身无效**（与核验结论区分）：空文件 `400 empty_upload`；超过
  `MAX_VERIFY_MB`（默认 512MB）`413 payload_too_large`；内容不是 ZIP
  时为 HTTP `200` + `valid:false` + `not_a_zip`（调用方按统一字段判定即可）。

### 核验口径（任一不满足即整体失败）

1. **ZIP 结构**：是合法 ZIP；**成员名唯一**（重复报
   `duplicate_zip_member`）；全部为规定的**相对路径**——无前导 `/`、无盘符、
   无反斜杠、无 `..`/`.`/空段、无目录条目（`illegal_member_path` /
   `unexpected_directory_member`）；非清单成员只能位于 `originals/`、
   `publications/` 或为五个顶层记录 JSON（`unexpected_member`）。
2. **清单与成员一一对应**：`manifest.json` 与五类记录 JSON 必须存在
   （`missing_manifest` / `missing_record_file`）；清单每个 `path` 都是真实
   成员（`manifest_member_missing`），每个非清单成员也都在清单中
   （`member_not_in_manifest`）；清单内路径不重复（`duplicate_manifest_entry`）、
   不列自身（`manifest_lists_itself`）；`file_count` 与非清单成员数、清单条目
   数三者相等（`manifest_file_count_mismatch`）。
3. **SHA-256 与条目标识**：逐项复算成员实际 SHA-256 必须与清单 `sha256`
   一致（`manifest_sha256_mismatch`）；`kind` 合法，且 `kind`/`doc_id`/
   `version` 与成员路径一致（`manifest_identity_mismatch`：原图
   `originals/{doc_id}.png|.jpg`、成品 `publications/{doc_id}/v{n}.png`，
   记录文件不带文档/版本标识）；每个条目 `file_count` 恒为 1
   （`manifest_file_count_invalid`）。
4. **五类记录计数**：各记录文件自带计数（`count`/`document_count`/
   `revision_count`）与数组长度一致，并与清单 `record_counts` 一致
   （`record_count_mismatch`）；记录 JSON 无法解析或字段非法为
   `record_invalid_json` / `record_invalid_structure` / `record_invalid_entry`。
5. **引用关系闭环**（`record_relation_conflict`，摘要不符为
   `record_digest_conflict`，引用文件缺失为 `referenced_file_missing`，
   记录/文件重复为 `duplicate_record_entry`/`unreferenced_archive_file`）：
   - **文档 ↔ 原图**：每文档唯一；`original_file` 恰为按其 `media_type` 推导
     的 `originals/{doc_id}.png|.jpg`；该成员存在、`kind=original`、其摘要等于
     `original_sha256`；不存在无文档引用的游离原图。
   - **文档 ↔ 连续标注修订**：每文档恰好一份标注记录，其 `current_revision`
     等于文档当前修订号；修订号**从 0 起连续无缺**，每条修订的 `rect_count`
     等于矩形数；矩形为整数、w/h>0、不越出文档宽高、原因非空；发布记录引用的
     `annotation_revision` 必须是该文档已存在的修订号。
   - **发布版本 ↔ 成品与原图摘要**：`(doc_id, version)` 唯一，版本号在**文档
     维度从 1 起连续**；`output_file` 恰为 `publications/{doc_id}/v{n}.png`，
     该成员存在、`kind=publication`、版本标识一致、摘要等于 `output_sha256`；
     发布的 `original_sha256` 与文档一致；不存在无发布记录引用的游离成品。
   - **批次成员顺序**：每批次唯一；成员 `position` 从 0 起连续（即请求顺序）、
     成员不重复且都指向真实发布；发布行的 `batch_id` 与批次的成员集合**双向
     一致**；批次成员的 `publish_at` 与批次一致。
   - **撤回记录**：每条撤回都指向真实发布（无悬空撤回）；发布的
     `withdrawn`/`withdrawn_at`/`withdrawal_reason` 与撤回记录**双向一致**
     （有撤回记录必标记撤回，反之亦然）。

核验只读取上传 ZIP，不需要服务处于任何特定状态；它不依赖服务数据库，因此
运维可在交接现场、甚至脱离原服务实例的机器上对同一份 ZIP 反复核验。端到端
示例（含有效归档返回计数、篡改成品后整体失败与可定位原因）：
`BASE=http://localhost:8000 bash examples/demo_archive_verify.sh`。


## 错误响应

统一格式：`{"detail": {"code": "...", "message": "..."}}`

| HTTP | code | 触发场景 |
| ---- | ---- | -------- |
| 400 | `empty_upload` | 上传空文件 |
| 400 | `image_decode_failed` | 字节无法解码为图像（含截断/损坏） |
| 400 | `empty_rectangle` | 矩形宽或高 ≤ 0 |
| 400 | `rectangle_out_of_bounds` | 矩形越出旋正后图像边界 |
| 400 | `reason_required` | 矩形未填写原因；撤回原因为空白 |
| 400 | `invalid_migration_size` | 迁移目标数不在 1～20（附 `count`） |
| 400 | `duplicate_target_in_migration` | 迁移请求中目标文档 ID 重复（附 `position`/`doc_id`） |
| 400 | `source_among_targets` | 迁移源文档同时出现在目标列表中 |
| 400 | `source_annotations_empty` | 迁移源当前标注不含任何矩形（从未标注或已清空） |
| 400 | `invalid_publish_time` | `publish_at` 无法解析、不带时区偏移、不晚于当前时刻或晚于当前超过 30 天 |
| 404 | `document_not_found` | 文档不存在（批次场景附 `position`/`doc_id`） |
| 404 | `unknown_version` | 发布版本不存在 |
| 404 | `publication_not_yet_public` | 版本存在但公开时间尚未到达（详情与图片一致，不泄露任何版本内容） |
| 404 | `batch_not_found` | 批次标识不存在 |
| 410 | `publication_withdrawn` | 公开读取已撤回的版本（不返回成品与遮盖详情） |
| 400 | `invalid_batch_size` | 批次文档数不在 2～20（附 `count`） |
| 400 | `duplicate_document_in_batch` | 批次内文档 ID 重复（附 `position`/`doc_id`） |
| 409 | `stale_revision` | 标注修订号过期（响应附 `current_revision`；批次/迁移场景另附 `position`/`doc_id`/`supplied_revision`，迁移源侧附 `doc_id`；遮盖范围比较附 `current_revision`/`supplied_revision`） |
| 409 | `idempotency_key_conflict` | 幂等键已用于不同内容的批次请求（原批次不受影响） |
| 500 | `original_unavailable` | 存储的原图无法读取/解码（批次场景附 `position`/`doc_id`） |
| 500 | `artifact_write_failed` | 发布成品写入磁盘失败（附 `position`/`doc_id`/`version`） |
| 500 | `artifact_missing` | 发布成品在磁盘上缺失（单份图片读取；离线交付包附 `position`/`doc_id`/`version`） |
| 500 | `artifact_read_failed` | 离线交付包读成品文件失败（附 `position`/`doc_id`/`version`） |
| 500 | `artifact_corrupt` | 离线交付包中成品实际 SHA-256 与发布记录不符（附 `position`/`doc_id`/`version`） |
| 400 | `invalid_package_size` | 离线交付包条目数不在 2～20（附 `count`） |
| 400 | `duplicate_item_in_package` | 交付包内 `(doc_id, version)` 对重复（附 `position`/`doc_id`/`version`） |
| 500 | `archive_original_missing` | 服务数据归档引用的原图在磁盘上缺失（附 `doc_id`，整单拒绝） |
| 500 | `archive_original_read_failed` | 归档读取原图失败（附 `doc_id`，整单拒绝） |
| 500 | `archive_original_corrupt` | 归档原图实际 SHA-256 与快照记录不符（附 `doc_id`，整单拒绝） |
| 500 | `archive_artifact_missing` | 归档引用的发布成品在磁盘上缺失（附 `doc_id`/`version`，整单拒绝） |
| 500 | `archive_artifact_read_failed` | 归档读取发布成品失败（附 `doc_id`/`version`，整单拒绝） |
| 500 | `archive_artifact_corrupt` | 归档成品实际 SHA-256 与快照记录不符（附 `doc_id`/`version`，整单拒绝） |
| 500 | `archive_unsupported_media_type` | 归档文档的库内 media_type 不受支持（附 `doc_id`） |
| 500 | `archive_write_failed` | 归档 ZIP 写盘失败（临时文件已清理，整单拒绝） |
| 400 | `empty_upload` | 归档核验上传空文件（`/service-data-archives/verify`） |
| 413 | `payload_too_large` | 归档核验上传超过 `MAX_VERIFY_MB`（默认 512MB） |
| 200 | （核验业务结论，`valid:false`） | 归档不可信时 HTTP 仍为 200，原因在 `issues[]`：`not_a_zip`、`duplicate_zip_member`、`illegal_member_path`、`unexpected_directory_member`、`unexpected_member`、`missing_manifest`、`missing_record_file`、`manifest_invalid_json`、`manifest_invalid_structure`、`manifest_invalid_entry`、`manifest_member_missing`、`member_not_in_manifest`、`duplicate_manifest_entry`、`manifest_lists_itself`、`manifest_sha256_mismatch`、`manifest_identity_mismatch`、`manifest_file_count_invalid`、`manifest_file_count_mismatch`、`archive_member_unreadable`、`record_invalid_json`、`record_invalid_structure`、`record_invalid_entry`、`record_count_mismatch`、`duplicate_record_entry`、`record_relation_conflict`、`record_digest_conflict`、`referenced_file_missing`、`unreferenced_archive_file`（均附 `path`/`doc_id`/`version`/`field` 等定位字段） |
| 413 | `payload_too_large` | 超过上传大小上限 |
| 415 | `unsupported_media_type` | 非 PNG/JPEG（如 GIF） |

## 一致性设计

- **坐标系**：上传时以 `ImageOps.exif_transpose` 旋正，标注、涂实、发布全部在旋正后
  的像素坐标上进行，与查看器所见方向一致。
- **乐观锁**：`PUT /documents/{id}/annotations` 要求 `revision` 等于服务端当前修订号，
  在 `BEGIN IMMEDIATE` 事务内 check-and-set，过期即 409，不会丢失他人修改。
- **方案迁移**：`POST /annotation-migrations` 在**一个** `BEGIN IMMEDIATE` 事务内
  完成源修订号校验、源当前非空矩形读取，以及按请求顺序对各目标的存在性/预期修订号
  校验、按旋正后宽高分别整数缩放（左上 `floor`、右下 `ceil` 并夹到目标图内）、
  新修订矩形追加与修订号 +1；全部校验通过才写入，任一失败整体回滚，所有目标标注与
  修订号不变。迁移与标注修改、单份/批次发布（及撤回、打包）持同一写锁互斥串行；
  源标注只读不改，各目标历史修订矩形与已发布成品均不受影响。

- **发布事务**：读取当前修订号与矩形、渲染涂实、写盘、插入发布记录在同一写事务中完成；
  与标注修改互斥串行，因此成品文件与记录必然对应同一修订。
- **批次原子性**：`POST /publication-batches` 在**一个** `BEGIN IMMEDIATE` 事务内
  按请求顺序先完成全部"存在性 + 修订号"校验，全部通过后才逐份渲染落盘并登记
  `publications`/`publication_batches`/`batch_publications`；与标注修改、单份发布、
  其他批次发布互斥。任一文档不存在、修订号过期、原图不可读、成品写盘失败，事务整体
  回滚并清理已落盘文件——既不产生批次标识，也没有任何 `publications` 行，因此
  `MAX(version)+1` 不会被失败批次推进（不占版本号、不留部分版本）。成功批次的每份
  版本与单份发布共用 `publications` 序列；批次清单以 `position` 固化请求次序。
- **批次幂等**：携带 `idempotency_key` 时，键的"检查 → 发布 → 登记"与其余批次
  写入同在上述写事务内完成。首次成功即将键、请求内容摘要（有序文档 ID + 预期
  修订号 + 规范化后的 `publish_at` 的 SHA-256）与完整成功响应体落入
  `batch_idempotency` 表；重试命中同键同内容时直接返回持久化响应（HTTP 200，
  不再校验时间窗口/修订号、不重新渲染），命中同键不同内容（含不同公开时间）
  时 409 冲突且原批次不变；任何失败回滚会一并撤销键记录，键不被占用；
  并发同键请求被写锁串行化，最多发布一次。
- **不可变性**：标注矩形按 `(doc_id, revision)` 追加存储、历史修订永不改写；
  发布版 PNG 落盘后不改动，后续标注变化不影响已发布成品（批次产物同样如此）。
- **遮盖范围比较**：`POST /documents/{id}/coverage-diff` 全程在**一个只读
  快照事务**（延迟 `BEGIN`，首条 SELECT 建立 WAL 读快照）中读取文档、当前
  修订矩形与基准发布行固化修订（`publications.annotation_revision`）下的
  冻结矩形——比较期间并发提交的标注修改/发布/撤回对本次读取不可见，两边
  必然来自同一快照；事务纯只读、不持写锁、不产生版本或任何改动记录。像素
  集合以半开整数区间（`[x,x+w)×[y,y+h)`）计算：先按两套矩形的全部水平边
  切出水平条带，条带内分别求 x 区间并集再做集合差，因此**重叠像素只计一次、
  边界相接（共享零像素）不算重叠**；失去遮盖区间按水平段输出，相邻段区间
  列表完全相同才合并。基准版本与公开读取同门控（撤回 410 优先、未到公开
  时间 404、未知版本 404），门控未通过前不读取冻结矩形，拒绝时不泄露基准
  遮盖详情。
- **版本撤回**：撤回的"校验 → 查重 → 登记"在同一个 `BEGIN IMMEDIATE`
  写事务内完成，与标注修改、单份/批次发布、其他撤回互斥串行；撤回记录按
  `(doc_id, version)` 主键追加、永不改写——重复撤回（含并发）命中主键直接
  返回首次记录，恰好一个请求创建记录，失败整体回滚不留部分记录。撤回只插入
  `publication_withdrawals` 行：`publications` 记录、成品文件与版本号序列全部
  保留（后续发布继续 `MAX(version)+1`）；撤回生效后新发起的公开读取返回 410，
  生效前已开始的下载因成品文件保留而允许完成。
- **脱敏**：矩形区域以纯黑不可逆涂实；发布图重建为干净像素容器后另存为 PNG，
  不携带 EXIF/ICC/文本等任何原图元数据；无标注时也生成去元数据的发布版。
- **定时公开**：`publish_at` 在解析为 UTC 后随发布记录（批次还随
  `publication_batches`）持久化；成品在发布当下就已渲染落盘并占用正常版本号，
  "未到点"只是公开读取面的判定（`publish_at > 当前 UTC 时间` →
  404 `publication_not_yet_public`），因此到点无需任何补操作、无后台任务。
  该判定在撤回检查之后：已撤回版本恒为 410，时间到达不恢复。公开时间的
  窗口校验（晚于当前、≤30 天）只在首次请求的写事务内进行，幂等重放直接
  返回首次结果、不重新校验。
- **公开面**：`/publications/...` 只返回发布版成品、遮盖坐标与原因及摘要值，
  原图没有任何对外读取入口；未到公开时间的版本返回 404（不返回成品、遮盖
  详情与摘要），已撤回版本返回 410。
- **离线交付包**：`POST /publication-packages` 先在事务外拒绝数量越界
  （2～20）与 `(doc_id, version)` 重复；随后在**一个** `BEGIN IMMEDIATE`
  写事务内按请求顺序完成全部资格确认（存在性、撤回 410 优先、公开时间 404），
  全部通过后才逐项读盘、复算成品 SHA-256（与 `publications.output_sha256`
  不符即 `artifact_corrupt`），再一次性生成 ZIP——任一环节失败都整体回滚、
  删除临时文件，不交付任何部分包。资格确认、读盘与打包全程持有写锁，与版本
  撤回互斥串行：撤回先提交则打包拒绝，打包先确认则撤回排队至交付事务提交后
  才生效，本次交付完成；包内 PNG 文件名只由请求位置、服务端文档 ID 与版本号
  生成，不取自上传文件名，`manifest.json` 按请求顺序记录文档 ID、版本号、
  成品 SHA-256、遮盖数与包内文件名。
- **服务数据归档**：`POST /service-data-archives` 无请求体、纯只读。文档、
  各修订标注、发布、批次与撤回记录全部在**一个只读快照事务**（延迟 `BEGIN`
  的 WAL 读快照）中读取，归档期间并发提交的写入对本次不可见，因此归档**只
  对应一个一致时点**；快照事务不持写锁，**并发写入可继续**。原图与成品
  落盘后不可变（发布/撤回均不改写文件），其读盘与 SHA-256 复算在同一快照
  事务内完成。先读全部记录、再读入并校验**全部**引用文件（原图比对
  `documents.original_sha256`、成品比对 `publications.output_sha256`）、
  最后一次性写 ZIP——任一文件缺失/读盘失败/摘要不符或 ZIP 写盘失败都整单
  拒绝并删除临时文件，**不提供部分归档**；ZIP 完整生成并正常关闭后才作为
  下载响应，响应结束后清理临时文件。包内全部为相对路径（`originals/`、
  `publications/` 与顶层记录 JSON），原图文件名只由服务端文档 ID 与库内
  media_type 决定、不取上传名；`manifest.json` 逐文件列出 SHA-256、
  文档/版本标识与文件数（清单自身不计入 `file_count`）。

## 目录结构

```
app/
  main.py     # FastAPI 路由与事务编排
  db.py       # SQLite 模式与事务助手（WAL + BEGIN IMMEDIATE；只读快照事务）
  imaging.py  # 解码、EXIF 旋正、涂实、去元数据渲染、矩形缩放、遮盖范围比较
  models.py   # 请求体模型（含批次、迁移、遮盖比较请求）
  config.py   # 环境变量配置
  archive_verify.py  # 服务数据归档 ZIP 的交接前核验（纯只读，不落盘）
tests/test_api.py    # 16 个用例：含 EXIF、乐观锁、错误码、不可变性、并发一致性
tests/test_batch.py  # 12 个用例：批次成功/顺序、数量与重复、文档缺失/修订过期/
                     #   原图不可读/写盘失败的整批回滚、版本交错、不可变性、并发
tests/test_idempotency.py  # 10 个用例：幂等重放、内容绑定（含公开时间）、冲突、
                           #   各类失败不占键、并发同键最多发布一次、无键请求兼容
tests/test_withdrawal.py   # 10 个用例：撤回/查询、公开读取 410、幂等重复、
                           #   列表标识、版本号不复用、批次重放不恢复、并发一致性
tests/test_scheduled_publication.py  # 22 个用例：单份/批次定时发布、到点前后
                                     #   404→200、不可变、无效时间不留痕、时区
                                     #   规范化、幂等内容纳入公开时间、撤回优先
tests/test_package.py        # 13 个用例：交付包顺序/清单/摘要/遮盖数、文件名
                             #   不取上传名、同文档多版本、数量与重复、
                             #   未知/未到点/撤回整包拒绝不泄露、成品缺失损坏
                             #   不交付部分包、资格确认与撤回串行、临时文件清理
tests/test_archive.py        # 12 个用例：归档相对路径结构、记录 JSON、原图/成品
                             #   摘要与 manifest（SHA-256/标识/文件数）、空库、
                             #   JPEG 扩展名不取上传名、批次与定时/撤回版本全收录、
                             #   同一快照且并发写入不阻塞不混入、原图/成品缺失损坏
                             #   整单拒绝、ZIP 写盘失败与成功后临时文件清理
tests/test_archive_verify.py # 40 个用例：有效归档（含空库/JPEG/多修订/批次/撤回）
                             #   返回五类计数；非 ZIP/空文件/重复成员/非法相对路径；
                             #   清单与成员缺项/多项/重复/file_count；SHA-256 篡改与
                             #   kind/标识/file_count；记录计数与 JSON 损坏；标注修订
                             #   连续/越界、发布版本连续/修订悬空、批次顺序与双向引用、
                             #   撤回悬空/标记冲突、摘要冲突、游离文件；核验不解压不
                             #   改写服务数据
tests/test_migration.py      # 20 个用例：缩放取整（floor/ceil/独立宽高）、成功
                             #   迁移与修订号 +1、原因与顺序、源与历史/发布版
                             #   不变、数量/重复/源混入目标/缺失/修订过期/源为空
                             #   整批原子失败不推进修订号、迁移与标注发布并发串行
tests/test_coverage_diff.py  # 31 个用例：新增/失去像素数与占比、零差异、重叠
                             #   只计一次/相接不算重叠、水平段相邻相同才合并、
                             #   基准冻结不受后续修改影响、未知/未到点/撤回不
                             #   泄露详情、修订过期、纯只读、并发标注下同一快照、
                             #   纯几何区间并集/相减
examples/demo.sh             # 单份端到端 curl 示例
examples/demo_batch.sh       # 批次发布端到端 curl 示例
examples/demo_idempotency.sh # 批次幂等键端到端 curl 示例
examples/demo_scheduled_publication.sh  # 定时公开端到端 curl 示例
examples/demo_withdrawal.sh  # 版本撤回端到端 curl 示例
examples/demo_package.sh     # 离线交付包端到端 curl 示例
examples/demo_archive.sh     # 服务数据归档端到端 curl 示例
examples/demo_archive_verify.sh  # 归档核验（有效计数 + 篡改后可定位失败）端到端示例
examples/demo_migration.sh   # 遮盖方案迁移端到端 curl 示例
examples/demo_coverage_diff.sh  # 重发前遮盖范围比较端到端 curl 示例
Dockerfile / docker-compose.yml
```

## 本轮变动（服务数据归档核验）

- 新增 `POST /service-data-archives/verify`：运维在交接前上传本服务生成的
  归档 ZIP（`multipart/form-data`，字段名 `file`）核验其**完整可信**。
  核验**纯只读**：上传内容只落到**系统临时目录**（核验后立即删除），
  **绝不解压到 `DATA_DIR`**，不打开/写入数据库，也**不改写任何现有记录**；
  全部结论只依据 ZIP 内的 `manifest.json`、五类记录 JSON 与引用成员，可对同一
  ZIP 脱离原服务实例反复核验。
- **核验口径**：成员名唯一且均为规定相对路径；`manifest.json` 与实际成员
  **一一对应**、`file_count` 一致；逐项 **SHA-256** 匹配且 `kind`/文档版本
  标识/`file_count` 与路径一致；五类记录自带计数、数组长度与清单
  `record_counts` 一致；文档 ↔ **连续标注修订** ↔ **发布版本**（版本号连续、
  引用修订存在）↔ **批次成员顺序**（position 从 0 连续、双向引用）↔ **撤回
  记录**（无悬空、withdrawn 双向一致）引用闭环；**原图**与**发布成品**的
  相对路径、标识与摘要均与记录一致，且无游离文件。
- **结论表达**：通过 `200 {"valid":true,"member_count","file_count",
  "record_counts","issues":[]}`（返回五类记录计数）；不可信同样是 HTTP `200`
  但 `"valid":false`，`issues[]` 尽量一次性列全，每条带 `code` 与
  `path`/`manifest_index`/`index`/`doc_id`/`version`/`batch_id`/`field` 等
  **可定位字段**，**整体判失败**。空文件 `400 empty_upload`、超过
  `MAX_VERIFY_MB`（默认 512MB）`413 payload_too_large`、内容非 ZIP 为
  `valid:false` + `not_a_zip`。
- 新增模块 `app/archive_verify.py`（纯函数式核验逻辑，`verify_archive_bytes`
  /`verify_archive_path`，不解压、不触库）与测试
  `tests/test_archive_verify.py`（40 个用例）、示例
  `examples/demo_archive_verify.sh`；新增配置项 `MAX_VERIFY_MB`。**不新增任何
  数据表或列**，上传、标注、发布（含定时公开）、批次与幂等键、撤回、公开读取、
  离线交付包、服务数据归档的路由/请求/响应字段及启动方式（Docker Compose /
  uvicorn）全部保持兼容。

## 历史变动（服务数据归档）

- 新增 `POST /service-data-archives`：运维请求、**无请求体、纯只读**；成功
  `200` 返回 `application/zip`（下载名 `service-data-archive-<UTC时间戳>.zip`）。
  内容为**同一个数据库快照**中的文档、各修订标注、发布、批次与撤回记录（5 个
  顶层 JSON），以及该快照引用到的**原图**（`originals/{doc_id}.png|.jpg`）与
  **发布成品**（`publications/{doc_id}/v{n}.png`），全部以**相对路径**组织，
  不含服务端绝对路径；原图文件名只由服务端文档 ID 与库内 media_type 映射，
  **不取上传名**。另附 `manifest.json`：`record_counts`、`file_count`（不含
  清单自身）与逐文件的 `path`/`sha256`/`kind`/文档或版本标识/`file_count`。
- **只对应一个一致时点**：全部记录读取在**一个只读快照事务**（延迟 `BEGIN`
  的 WAL 读快照）内完成，归档期间并发提交的标注/发布/撤回不可见；事务不持
  写锁，**生成期间并发写入可继续**。原图与成品落盘后不可变，读盘与 SHA-256
  复算在同一快照事务内进行（原图比对 `documents.original_sha256`、成品比对
  `publications.output_sha256`）。
- **全有或全无**：先读全部记录，再读入并校验全部引用文件，全部通过后才一次性
  写 ZIP；任一文件缺失/读盘失败/摘要不符或 ZIP 写盘失败都整单拒绝（JSON 错误，
  非 ZIP）并清理临时文件，**不提供部分归档**。ZIP 完整生成并正常关闭后才作为
  下载响应，发送完成后清理临时文件。
- 新增错误码：`archive_original_missing`/`archive_original_read_failed`/
  `archive_original_corrupt`、`archive_artifact_missing`/
  `archive_artifact_read_failed`/`archive_artifact_corrupt`、
  `archive_unsupported_media_type`、`archive_write_failed`。只读快照助手复用
  `app/db.py::snapshot_tx`；**不新增任何数据表或列**，上传、标注、发布（含
  定时公开）、批次与幂等键、撤回、公开读取、离线交付包的路由、请求/响应字段
  及启动方式（Docker Compose / uvicorn）全部保持兼容。

## 历史变动（重发前遮盖范围比较）

- 新增 `POST /documents/{doc_id}/coverage-diff`：请求体
  `{"revision","baseline_version"}` 携带**当前标注修订号**与**基准发布
  版本号**；在**同一个只读快照事务**中返回新增/失去遮盖像素数、各自占
  旋正后图像（`width*height`）的比例，以及按水平边界段列出的失去遮盖
  区间（相邻段区间完全相同才合并）。像素按半开整数区间计算：**重叠矩形
  只计一次、边界相接不算重叠**。
- **基准取发布时冻结的标注**（发布行 `annotation_revision` 指向的历史
  修订矩形，追加永不改写），不受后续标注修改影响；比较**纯只读**——不生成
  版本、不写任何改动记录、不持写锁，可重复调用。
- 拒绝沿用统一错误体与既有错误码，且不泄露基准遮盖详情：未知文档
  `document_not_found`（404）、未知版本 `unknown_version`（404）、未到
  公开时间 `publication_not_yet_public`（404）、已撤回
  `publication_withdrawn`（410，撤回优先于公开时间）、当前修订号过期
  `stale_revision`（409，附 `current_revision`/`supplied_revision`）。
- 纯几何逻辑位于 `app/imaging.py::coverage_diff`（全整数运算，含区间
  并集/相减/水平段合并）；只读快照助手为 `app/db.py::snapshot_tx`。
  **不新增任何数据表或列**，上传、标注、发布（含定时公开）、批次与幂等键、
  撤回、公开读取、离线交付包的路由、请求/响应字段及启动方式
  （Docker Compose / uvicorn）全部保持兼容。

## 历史变动（遮盖方案迁移）

- 新增 `POST /annotation-migrations`：请求体
  `{"source_doc_id","source_revision","targets":[{"doc_id","expected_revision"},…]}`
  携带源文档 ID、源当前标注修订号与 **1～20 个不同目标**（源不能作为目标）；
  仅使用源**当前非空**标注（为空报 `source_annotations_empty`），按各文档
  **旋正后宽、高分别缩放**——左上边界向下取整、右下边界向上取整并限制在
  目标图内，**保留原因与原顺序**；成功 `200` 按请求顺序返回各目标新修订号
  （各 +1）与映射矩形。
- 源及所有目标都存在、源修订号与各目标预期修订号全部匹配时，才在**同一个
  `BEGIN IMMEDIATE` 事务**内整体替换各目标标注；任一校验失败则所有目标不变
  （附 `position`/`doc_id` 等定位），可修正后重试。事务与标注修改、发布
  （单份/批次）、撤回、打包串行；**源标注及既有发布版不变**，目标历史修订
  矩形照旧追加保留。
- 新增错误码：`invalid_migration_size`、`duplicate_target_in_migration`、
  `source_among_targets`、`source_annotations_empty`；既有
  `document_not_found`/`stale_revision` 在迁移场景附定位字段。
- 纯缩放逻辑位于 `app/imaging.py::scale_rect`（全整数运算）；**不新增任何
  数据表或列**，上传、标注、发布（含定时公开）、批次与幂等键、撤回、公开读取、
  离线交付包的路由、请求/响应字段及启动方式（Docker Compose / uvicorn）全部
  保持兼容。

## 历史变动（离线交付包 ZIP）

- 新增 `POST /publication-packages`：请求体 `{"items":[{"doc_id","version"},…]}`
  按顺序携带 **2～20 个互不重复的 `(doc_id, version)` 对**；成功 `200` 返回
  `application/zip`（下载名 `publication-package.zip`），内含各版发布 PNG 与
  `manifest.json`，清单按请求顺序记录文档 ID、版本号、**成品 SHA-256**（包内
  PNG 的实际摘要）、**遮盖数**与**包内文件名**（另含 `position`、顶层
  `created_at`/`count`）。
- 包内 PNG 文件名固定为 `{两位序号}_{doc_id}_v{version}.png`，只由请求位置、
  服务端文档 ID、版本号构成，**绝不取自上传文件名**，且为无目录的扁平结构。
- 任一资格项失败即**整包拒绝、不交付部分包**，错误附 `position`/`doc_id`/
  `version` 定位但不泄露该项成品摘要、遮盖数或撤回原因：
  `invalid_package_size`（400，附 `count`）、`duplicate_item_in_package`（400）、
  `document_not_found`/`unknown_version`（404）、
  `publication_not_yet_public`（404）、`publication_withdrawn`（410，撤回优先）；
  成品缺失/读盘失败/摘要不符为 `artifact_missing`/`artifact_read_failed`/
  `artifact_corrupt`（500），同样不交付部分包。
- **资格确认与撤回串行**：数量/重复检查后，资格确认、成品读盘与摘要复算、
  ZIP 生成全程在同一个 `BEGIN IMMEDIATE` 写事务内，与撤回（及发布、标注修改）
  互斥：撤回先生效则整包拒绝，打包先确认则并发撤回排队到事务提交后才生效，
  本次交付照常完成（成品文件本就保留），撤回只影响之后新发起的读取/打包。
- 交付 ZIP 使用服务端临时文件，响应发送后自动清理；**不新增任何数据表或列**，
  既有发布（含定时公开）、批次发布、幂等键、撤回与公开读取的路由、请求/响应
  字段及启动方式（Docker Compose / uvicorn）全部保持兼容。

## 历史变动（定时公开 publish_at）

- 单份发布 `POST /documents/{doc_id}/publish` 新增**可选 JSON 请求体**
  `{"publish_at": "RFC3339 时间"}`（省略请求体的既有调用方式不变）；
  批次发布 `POST /publication-batches` 顶层新增可选 `publish_at`，对批次内
  每份文档统一生效。时间必须带时区偏移（推荐 UTC 尾部 `Z`，其他偏移换算为
  UTC 并规范化）；首次提交时必须严格晚于当前时刻且不超过 30 天，否则
  `400 invalid_publish_time`；省略或 `null` 仍立即公开。
- 定时版本在发布当下即生成不可变成品、写盘并占用**正常版本号**
  （与立即公开共用 `MAX(version)+1` 序列与公开入口）；公开时间到达前，
  `/publications/{doc}/{version}` 与 `.../image` 一律返回
  `404 publication_not_yet_public`，不返回遮盖信息、摘要或成品文件；
  到点后无需再次操作即可读取（无后台任务，读取时按当前 UTC 时间判定）。
- 单份发布响应、批次发布/批次查询顶层及各发布条目（文档详情、发布列表、
  批次清单）均在原字段之后追加 `publish_at`（立即公开为 `null`），原字段
  及顺序保持不变。
- 永久撤回优先于公开时间：对未到点版本撤回后恒为 `410
  publication_withdrawn`，时间到达也不恢复；撤回记录、成品与版本号照旧保留。
- 批次幂等键的内容绑定纳入 `publish_at`：同键同内容（含相同公开时间）
  重放即使时间已过也原样返回首次结果（200），不再校验时间窗口；同键不同
  公开时间（含一边定时、一边立即）报 `409 idempotency_key_conflict`。
- 无效时间与批次任一文档失败一样整批回滚：不留版本、批次、成品文件，
  也不占用幂等键，可修正后用原参数/原键重试。
- 存储：`publications`、`publication_batches` 新增可空列 `publish_at`，
  旧数据卷启动时自动补列（`ALTER TABLE` 轻量迁移）；旧数据该列为 NULL，
  等同立即公开。
- 上传、标注、撤回、公开读取及未携带 `publish_at` 的既有路由、请求与响应
  字段均未改动，行为完全兼容。

## 历史变动（版本撤回）

- 新增 `POST /documents/{doc_id}/publications/{version}/withdraw`：凭非空原因
  永久撤回已发布版本（首次 201；重复撤回 200 返回首次记录，不改写原因与时间）；
  新增 `GET /documents/{doc_id}/publications/{version}/withdrawal` 查询该版本的
  撤回状态、时间与原因。未知文档/版本沿用 404 `document_not_found`/
  `unknown_version`，空白原因 400 `reason_required`。
- 新增表 `publication_withdrawals`（`(doc_id, version)` 主键、原因、撤回时间）；
  旧数据卷启动时自动建表（`CREATE TABLE IF NOT EXISTS` 轻量迁移）。
- 撤回生效后，新发起的公开元数据与图片读取返回 410 `publication_withdrawn`，
  不返回成品与遮盖详情；发布记录、成品文件与版本号序列保留，后续单份/批次
  发布继续原序列；生效前已开始的下载允许完成。
- 文档详情、发布列表、批次查询的版本条目末尾追加
  `withdrawn`/`withdrawn_at`/`withdrawal_reason`，原字段及顺序不变。
- 批次幂等键重放仍返回首次保存的响应，但不能恢复撤回版本的公开访问；
  并发撤回与发布在同一写事务内串行化，失败不留部分撤回记录。
- 上传、标注、单份/批次发布、公开读取的既有路由、请求与响应字段均未改动，
  行为完全兼容。

## 历史变动（批次发布幂等键）

- `POST /publication-batches` 新增可选字段 `idempotency_key`（1～128 字符，
  调用方保证全局唯一），与按请求顺序排列的 `(doc_id, expected_revision)` 绑定。
- 首次成功（201）持久化键与完整成功结果；相同键+相同内容重试返回 HTTP 200
  及首次的原批次标识、各文档版本与摘要，不再校验当前修订号、不重新渲染；
  相同键+不同内容返回 409 `idempotency_key_conflict`，原批次不受影响。
- 新增表 `batch_idempotency`（键、请求内容摘要、批次标识、持久化响应体）；
  旧数据卷启动时自动建表（`CREATE TABLE IF NOT EXISTS` 轻量迁移）。
- 任一校验、原图读取或成品落盘失败均不占用键、不产生部分版本，可用原键重试；
  并发同键重试在同一写事务内串行化，最多发布一次。
- 未带键的批次请求、单份发布与公开读取的路由、请求与响应字段均未改动，
  行为完全兼容；带键批次产物同样共用版本序列与公开入口。

## 历史变动（同批次发布）

- 新增 `POST /publication-batches`（2～20 份不同文档、各带预期修订号，整批原子）
  与 `GET /publication-batches/{batch_id}`（按批次标识查询，顺序与请求一致）。
- 新增表 `publication_batches`、`batch_publications`，并在 `publications` 增加
  可空列 `batch_id`；旧数据卷启动时自动补列（`ALTER TABLE` 轻量迁移）。
- 新增错误码：`invalid_batch_size`、`duplicate_document_in_batch`、
  `batch_not_found`、`original_unavailable`、`artifact_write_failed`；批次场景的
  `document_not_found`/`stale_revision` 附 `position`、`doc_id` 等定位字段。
- 既有单份发布、公开读取的路由、请求与响应字段均未改动，行为完全兼容；
  批次产物与单份产物共用版本序列与公开入口，已发布版本依旧不可变。
