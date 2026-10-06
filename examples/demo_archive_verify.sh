#!/usr/bin/env bash
# 服务数据归档核验端到端示例（运维交接前，纯只读）：
#   1) 先从服务取一份服务数据归档 ZIP（POST /service-data-archives）；
#   2) 调用核验 API（POST /service-data-archives/verify，multipart 字段 file）
#      —— 核验只在 ZIP 内部读取，不解压到服务 DATA_DIR、不打开数据库、
#         不改写任何现有记录；上传的 ZIP 放在系统临时目录、核验后即删；
#   3) 打印 valid、五类记录计数与 file_count；
#   4) 演示篡改一个成员后核验如何整体判失败并给出可定位的原因（issues）。
# 依赖：curl、python3（仅用于生成示例图、解析 JSON 与改写 ZIP）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

python3 - <<'EOF'
from PIL import Image
Image.new("RGB", (64, 48), (30, 90, 160)).save("/tmp/ver_a.png")
Image.new("RGB", (64, 48), (160, 90, 30)).save("/tmp/ver_b.png")
EOF

upload() {
  curl -sS -F "file=@$1" "$BASE/documents" \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])"
}

echo "== 1. 准备数据：上传、标注、发布、撤回 =="
A=$(upload /tmp/ver_a.png)
B=$(upload /tmp/ver_b.png)
echo "A=$A B=$B"
curl -sS -X PUT "$BASE/documents/$A/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":4,"y":4,"w":12,"h":10,"reason":"身份证号"}]}' >/dev/null
curl -sS -X POST "$BASE/documents/$A/publish" >/dev/null
curl -sS -X POST "$BASE/documents/$A/publications/1/withdraw" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"遮盖遗漏：右下角工号未遮盖"}' >/dev/null
curl -sS -X POST "$BASE/documents/$B/publish" >/dev/null

echo
echo "== 2. 取得服务数据归档 ZIP =="
rm -f /tmp/handover.zip
curl -sS -o /tmp/handover.zip -X POST "$BASE/service-data-archives"
ls -l /tmp/handover.zip

echo
echo "== 3. 交接前核验（原始归档，应 valid=true 并返回各类记录计数） =="
curl -sS -X POST "$BASE/service-data-archives/verify" \
  -F "file=@/tmp/handover.zip;type=application/zip" \
  | python3 -m json.tool

echo
echo "== 4. 篡改一个发布成品后再次核验（应 valid=false，给出可定位原因） =="
python3 - <<'EOF'
import json, zipfile
src = zipfile.ZipFile("/tmp/handover.zip")
pubs = json.loads(src.read("publications.json"))["publications"]
target = pubs[0]["output_file"]
names = src.namelist()
with zipfile.ZipFile("/tmp/handover_tampered.zip", "w", zipfile.ZIP_DEFLATED) as out:
    for n in names:
        data = src.read(n)
        if n == target:
            data = b"tampered-bytes"   # 模拟交付途中被改写
        out.writestr(n, data)
print("已篡改成员:", target, "-> /tmp/handover_tampered.zip")
EOF

curl -sS -X POST "$BASE/service-data-archives/verify" \
  -F "file=@/tmp/handover_tampered.zip;type=application/zip" \
  | python3 -c '
import sys, json
body = json.load(sys.stdin)
print("valid =", body["valid"])
for i in body["issues"]:
    code, message = i["code"], i["message"]
    where = " ".join(f"{k}={v}" for k, v in i.items()
                     if k not in ("code", "message"))
    print(f"  - [{code}] {message} {where}")
'
echo
echo "结论：valid=true 才可交接；valid=false 时按 issues 中的 path/doc_id/"
echo "version/field 定位问题，整体拒收，服务端不会解压或改写任何数据。"
