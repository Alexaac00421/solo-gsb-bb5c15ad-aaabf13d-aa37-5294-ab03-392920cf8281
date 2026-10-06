#!/usr/bin/env bash
# 版本撤回端到端示例：
#   上传 -> 标注 -> 发布 v1 -> 发现遮盖遗漏，凭文档 ID+版本号+非空原因撤回 v1
#   -> 公开读取 v1 返回 410 publication_withdrawn（不返回成品与遮盖详情）
#   -> 重复撤回返回首次记录（HTTP 200，原因不被改写）
#   -> 文档详情/发布列表/撤回查询均标识撤回状态（时间+原因）
#   -> 修正标注后重新发布得到 v2（版本号不复用），v2 公开可读
# 依赖：curl、python3（仅用于解析 JSON、生成示例图）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

python3 - <<'EOF'
from PIL import Image
Image.new("RGB", (64, 48), (30, 90, 160)).save("/tmp/withdraw_sample.png")
EOF

echo "== 1. 上传、标注并发布 v1 =="
DOC=$(curl -sS -F "file=@/tmp/withdraw_sample.png" "$BASE/documents")
DOC_ID=$(echo "$DOC" | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])")
curl -sS -X PUT "$BASE/documents/$DOC_ID/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":8,"y":8,"w":16,"h":12,"reason":"含证件号码"}]}' >/dev/null
curl -sS -X POST "$BASE/documents/$DOC_ID/publish"
echo

echo "== 2. 发现遮盖遗漏，凭文档 ID+版本号+非空原因撤回 v1（HTTP 201）=="
curl -sS -w "\nHTTP %{http_code}\n" -X POST \
  "$BASE/documents/$DOC_ID/publications/1/withdraw" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"遮盖遗漏：右下角工号未遮盖"}'

echo "== 3. 撤回生效后，新发起的公开元数据与图片读取均 410 publication_withdrawn =="
curl -sS -w "\nHTTP %{http_code}\n" "$BASE/publications/$DOC_ID/1"
curl -sS -w "\nHTTP %{http_code}\n" "$BASE/publications/$DOC_ID/1/image"

echo "== 4. 重复撤回：HTTP 200 返回首次记录，原因不被改写 =="
curl -sS -w "\nHTTP %{http_code}\n" -X POST \
  "$BASE/documents/$DOC_ID/publications/1/withdraw" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"另一条原因不会被记录"}'

echo "== 5. 查询撤回状态/时间/原因 =="
curl -sS "$BASE/documents/$DOC_ID/publications/1/withdrawal"
echo
echo "-- 发布列表同样标识撤回状态（原字段及顺序保留）--"
curl -sS "$BASE/documents/$DOC_ID/publications"
echo

echo "== 6. 空原因 400 reason_required；未知版本 404 unknown_version =="
curl -sS -w "\nHTTP %{http_code}\n" -X POST \
  "$BASE/documents/$DOC_ID/publications/1/withdraw" \
  -H 'Content-Type: application/json' -d '{"reason":"  "}'
curl -sS -w "\nHTTP %{http_code}\n" -X POST \
  "$BASE/documents/$DOC_ID/publications/99/withdraw" \
  -H 'Content-Type: application/json' -d '{"reason":"x"}'

echo "== 7. 修正标注后重新发布 -> v2（版本号不复用），v2 公开可读、v1 仍 410 =="
curl -sS -X PUT "$BASE/documents/$DOC_ID/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":1,"rects":[{"x":8,"y":8,"w":16,"h":12,"reason":"含证件号码"},{"x":40,"y":30,"w":20,"h":14,"reason":"工号"}]}' >/dev/null
curl -sS -X POST "$BASE/documents/$DOC_ID/publish"
echo
curl -sS -o /tmp/withdraw_v2.png -w "v2 image: HTTP %{http_code}, %{size_download} bytes\n" \
  "$BASE/publications/$DOC_ID/2/image"
curl -sS -w "\nHTTP %{http_code}\n" "$BASE/publications/$DOC_ID/1"
