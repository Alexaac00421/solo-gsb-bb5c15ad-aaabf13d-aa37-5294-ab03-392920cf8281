#!/usr/bin/env bash
# 服务数据归档恢复端到端示例（运维把归档 ZIP 恢复到新实例）：
#   1) 在源实例准备数据（上传/标注/单份+批次定时发布/撤回），取一份归档 ZIP；
#   2) 在**空的目标实例**上先核验（POST /service-data-archives/verify），
#      再整体恢复（POST /service-data-archives/restore，multipart 字段 file）；
#   3) 打印各类恢复计数，并验证既有查询与公开接口按恢复记录工作
#      （立即公开 200 / 定时未到点 404 / 已撤回 410）；
#   4) 演示两类明确拒绝：非空实例恢复（409 instance_not_empty）、
#      篡改归档（400 invalid_archive，附可定位 issues）。
#
# 需要两个服务实例（不同 DATA_DIR）：
#   源实例  : DATA_DIR=./data         uvicorn app.main:app --port 8000
#   目标实例: DATA_DIR=/tmp/restore-target uvicorn app.main:app --port 8001
# 依赖：curl、python3（仅用于生成示例图、解析 JSON 与改写 ZIP）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
BASE_RESTORE="${BASE_RESTORE:-http://localhost:8001}"

python3 - <<'EOF'
from PIL import Image
Image.new("RGB", (64, 48), (30, 90, 160)).save("/tmp/rst_a.png")
Image.new("RGB", (64, 48), (160, 90, 30)).save("/tmp/rst_b.png")
EOF

upload() {  # $1 = 文件路径 -> doc_id（在源实例上）
  curl -sS -F "file=@$1" "$BASE/documents" \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])"
}

echo "== 1. 源实例准备数据：上传、标注、单份发布、批次定时发布、撤回 =="
A=$(upload /tmp/rst_a.png)
B=$(upload /tmp/rst_b.png)
echo "A=$A B=$B"
curl -sS -X PUT "$BASE/documents/$A/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":4,"y":4,"w":12,"h":10,"reason":"身份证号"}]}' >/dev/null
curl -sS -X POST "$BASE/documents/$A/publish" >/dev/null   # A v1 立即公开
curl -sS -X PUT "$BASE/documents/$B/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":40,"y":30,"w":10,"h":10,"reason":"人脸"}]}' >/dev/null
PUBLISH_AT=$(python3 -c 'from datetime import datetime,timedelta,timezone
print((datetime.now(timezone.utc)+timedelta(days=7)).isoformat(timespec="seconds").replace("+00:00","Z"))')
curl -sS -X POST "$BASE/publication-batches" -H 'Content-Type: application/json' \
  -d "{\"documents\":[
    {\"doc_id\":\"$A\",\"expected_revision\":1},
    {\"doc_id\":\"$B\",\"expected_revision\":1}
  ],\"idempotency_key\":\"handover-demo-001\",\"publish_at\":\"$PUBLISH_AT\"}" >/dev/null
curl -sS -X POST "$BASE/documents/$A/publications/1/withdraw" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"遮盖遗漏：右下角工号未遮盖"}' >/dev/null   # A v1 撤回

echo
echo "== 2. 源实例导出服务数据归档 ZIP =="
rm -f /tmp/restore-handover.zip
curl -sS -o /tmp/restore-handover.zip -X POST "$BASE/service-data-archives"
ls -l /tmp/restore-handover.zip

echo
echo "== 3. 目标实例（空）先核验归档（纯只读，应 valid=true） =="
curl -sS -X POST "$BASE_RESTORE/service-data-archives/verify" \
  -F "file=@/tmp/restore-handover.zip;type=application/zip" \
  | python3 -c 'import sys,json;b=json.load(sys.stdin)
print("valid =", b["valid"]); print("record_counts =", b["record_counts"])'

echo
echo "== 4. 在空目标实例上整体恢复（返回各类恢复计数） =="
curl -sS -X POST "$BASE_RESTORE/service-data-archives/restore" \
  -F "file=@/tmp/restore-handover.zip;type=application/zip" \
  | python3 -m json.tool

echo
echo "== 5. 恢复后既有查询与公开接口按恢复记录工作 =="
echo "-- 文档详情（ID/版本/摘要/时间与源实例一致）:"
curl -sS "$BASE_RESTORE/documents/$A" | python3 -m json.tool
echo "-- A v1 已撤回 -> 410:"
curl -sS -o /dev/null -w "HTTP %{http_code}\n" "$BASE_RESTORE/publications/$A/1"
echo "-- A v2 批次定时公开（$PUBLISH_AT 前）-> 404:"
curl -sS -o /dev/null -w "HTTP %{http_code}\n" "$BASE_RESTORE/publications/$A/2"
echo "-- B v1 批次定时公开（同上）-> 404:"
curl -sS -o /dev/null -w "HTTP %{http_code}\n" "$BASE_RESTORE/publications/$B/1"

echo
echo "== 6. 明确拒绝之一：目标实例已非空 -> 409 instance_not_empty =="
curl -sS -X POST "$BASE_RESTORE/service-data-archives/restore" \
  -F "file=@/tmp/restore-handover.zip;type=application/zip" \
  | python3 -m json.tool

echo
echo "== 7. 明确拒绝之二：篡改归档 -> 400 invalid_archive（附可定位 issues） =="
python3 - <<'EOF'
import json, zipfile
src = zipfile.ZipFile("/tmp/restore-handover.zip")
pubs = json.loads(src.read("publications.json"))["publications"]
target = pubs[0]["output_file"]
with zipfile.ZipFile("/tmp/restore-tampered.zip", "w", zipfile.ZIP_DEFLATED) as out:
    for n in src.namelist():
        data = src.read(n)
        if n == target:
            data = b"tampered-bytes"   # 模拟交付途中被改写
        out.writestr(n, data)
print("已篡改成员:", target)
EOF
curl -sS -X POST "$BASE_RESTORE/service-data-archives/restore" \
  -F "file=@/tmp/restore-tampered.zip;type=application/zip" \
  | python3 -c 'import sys,json
d=json.load(sys.stdin)["detail"]
print("code =", d["code"])
for i in d["issues"][:3]:
    print("  -", i["code"], i.get("path", ""), i["message"][:80])'

echo
echo "结论：仅空实例可恢复；无效归档/非空实例/目标冲突/写入失败都明确拒绝，"
echo "不留下可查询的部分记录或本次文件。恢复后定时公开与撤回规则照常生效。"
