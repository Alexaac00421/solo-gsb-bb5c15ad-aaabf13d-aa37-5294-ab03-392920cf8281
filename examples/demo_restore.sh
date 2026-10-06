#!/usr/bin/env bash
# 服务数据归档恢复端到端示例（运维把本服务导出的归档 ZIP 恢复到新实例）：
#   1) 源实例准备数据（上传/标注/发布/定时公开/批次+幂等键/撤回）并取归档；
#   2) 目标实例（空实例）先按现有规则核验归档，再调用恢复 API
#      （POST /service-data-archives/restore，multipart 字段 file）；
#   3) 打印各类恢复计数，并用既有查询/公开接口验证恢复结果
#      （ID/版本/摘要/时间保留，撤回 410、定时公开 404、批次关系完整）；
#   4) 演示拒绝：非空实例 409 instance_not_empty、篡改归档 400 invalid_archive。
#
# 用法（两个实例，目标必须为空实例）：
#   终端1：DATA_DIR=./data-src uvicorn app.main:app --port 8000
#   终端2：DATA_DIR=./data-dst uvicorn app.main:app --port 8001
#   终端3：bash examples/demo_restore.sh
# 依赖：curl、python3（仅用于生成示例图、解析 JSON 与篡改 ZIP 演示）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"            # 源实例（有数据）
TARGET="${TARGET_BASE:-http://localhost:8001}"   # 目标实例（空实例）

python3 - <<'EOF'
from PIL import Image
Image.new("RGB", (64, 48), (30, 90, 160)).save("/tmp/rst_a.png")
Image.new("RGB", (64, 48), (160, 90, 30)).save("/tmp/rst_b.png")
EOF

upload() {  # $1 = 文件路径 -> doc_id
  curl -sS -F "file=@$1" "$BASE/documents" \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])"
}

echo "== 1. 源实例准备数据：上传、标注、发布（含定时公开）、批次、撤回 =="
A=$(upload /tmp/rst_a.png)
B=$(upload /tmp/rst_b.png)
echo "A=$A B=$B"
curl -sS -X PUT "$BASE/documents/$A/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":4,"y":4,"w":12,"h":10,"reason":"身份证号"}]}' >/dev/null
curl -sS -X POST "$BASE/documents/$A/publish" >/dev/null          # A v1 立即公开
PUBLISH_AT=$(python3 - <<'EOF'
from datetime import datetime, timedelta, timezone
print((datetime.now(timezone.utc) + timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ"))
EOF
)
curl -sS -X POST "$BASE/documents/$A/publish" -H 'Content-Type: application/json' \
  -d "{\"publish_at\":\"$PUBLISH_AT\"}" >/dev/null                 # A v2 定时公开
curl -sS -X POST "$BASE/documents/$A/publications/1/withdraw" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"遮盖遗漏：右下角工号未遮盖"}' >/dev/null           # A v1 撤回
curl -sS -X PUT "$BASE/documents/$B/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":40,"y":30,"w":10,"h":10,"reason":"人脸"}]}' >/dev/null
BATCH=$(curl -sS -X POST "$BASE/publication-batches" -H 'Content-Type: application/json' \
  -d "{\"documents\":[{\"doc_id\":\"$A\",\"expected_revision\":1},
                      {\"doc_id\":\"$B\",\"expected_revision\":1}],
      \"idempotency_key\":\"ops-demo-key-1\"}")
BATCH_ID=$(echo "$BATCH" | python3 -c "import sys,json;print(json.load(sys.stdin)['batch_id'])")
echo "批次 batch_id=$BATCH_ID（A v3、B v1；幂等键 ops-demo-key-1）"

echo
echo "== 2. 源实例取服务数据归档 ZIP =="
rm -f /tmp/restore-demo.zip
curl -sS -o /tmp/restore-demo.zip -X POST "$BASE/service-data-archives"
ls -l /tmp/restore-demo.zip

echo
echo "== 3. 目标实例：先按现有归档核验规则核验（应 valid=true） =="
curl -sS -X POST "$TARGET/service-data-archives/verify" \
  -F "file=@/tmp/restore-demo.zip;type=application/zip" \
  | python3 -c 'import sys,json;b=json.load(sys.stdin)
print("valid =", b["valid"], " record_counts =", b["record_counts"])'

echo
echo "== 4. 目标实例：恢复归档（空实例整体恢复，返回各类恢复计数） =="
curl -sS -X POST "$TARGET/service-data-archives/restore" \
  -F "file=@/tmp/restore-demo.zip;type=application/zip" \
  | python3 -m json.tool

echo
echo "== 5. 既有查询/公开接口按恢复记录工作（ID/版本/摘要/时间保留） =="
echo "-- 文档 A 元数据与发布列表（v1 已撤回、v2 定时 $PUBLISH_AT、v3 批次） --"
curl -sS "$TARGET/documents/$A" | python3 -m json.tool
echo "-- 公开读取 A v1（已撤回 -> 410） --"
curl -sS -o /dev/null -w "HTTP %{http_code}\n" "$TARGET/publications/$A/1"
echo "-- 公开读取 A v2（未到公开时间 -> 404） --"
curl -sS "$TARGET/publications/$A/2" | python3 -c 'import sys,json
print(json.load(sys.stdin)["detail"]["code"])'
echo "-- 公开读取 A v3（批次立即公开 -> 200，摘要与源一致） --"
curl -sS "$TARGET/publications/$A/3" \
  | python3 -c 'import sys,json;b=json.load(sys.stdin)
print("version =", b["version"], " output_sha256 =", b["output_sha256"][:16], "…")'
echo "-- 批次清单（成员顺序与公开时间保留） --"
curl -sS "$TARGET/publication-batches/$BATCH_ID" | python3 -m json.tool

echo
echo "== 6. 拒绝演示：目标已非空 -> 409 instance_not_empty（不留部分恢复） =="
curl -sS -X POST "$TARGET/service-data-archives/restore" \
  -F "file=@/tmp/restore-demo.zip;type=application/zip" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin)["detail"]
print("HTTP 409", d["code"], "-", d["message"])'

echo
echo "== 7. 拒绝演示：篡改一个成品成员 -> 400 invalid_archive（附可定位 issues） =="
python3 - <<'EOF'
import json, zipfile
src = zipfile.ZipFile("/tmp/restore-demo.zip")
pubs = json.loads(src.read("publications.json"))["publications"]
target = pubs[0]["output_file"]
with zipfile.ZipFile("/tmp/restore-demo-tampered.zip", "w", zipfile.ZIP_DEFLATED) as out:
    for n in src.namelist():
        data = src.read(n)
        if n == target:
            data = b"tampered-bytes"
        out.writestr(n, data)
print("已篡改成员:", target)
EOF
curl -sS -X POST "$TARGET/service-data-archives/restore" \
  -F "file=@/tmp/restore-demo-tampered.zip;type=application/zip" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin)["detail"]
print("HTTP 400", d["code"])
for i in d["issues"][:3]:
    print("  -", i["code"], i.get("path", ""))'

echo
echo "结论：恢复只在空实例上进行；无效归档、非空实例、目标冲突或写入失败都"
echo "明确拒绝且不留部分记录/文件。批次幂等键不随归档恢复，旧键重放按新键处理。"
