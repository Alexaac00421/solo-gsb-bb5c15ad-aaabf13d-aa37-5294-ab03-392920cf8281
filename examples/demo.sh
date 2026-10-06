#!/usr/bin/env bash
# 端到端调用示例：上传 -> 标注 -> 过期修订冲突 -> 重新标注 -> 发布 -> 公开读取
# 依赖：curl、python3（仅用于解析 JSON 与生成示例图）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

python3 - <<'EOF'
from PIL import Image
Image.new("RGB", (64, 48), (30, 90, 160)).save("/tmp/archive_sample.png")
EOF

echo "== 1. 上传原图 =="
DOC=$(curl -sS -F "file=@/tmp/archive_sample.png" "$BASE/documents")
echo "$DOC"
DOC_ID=$(echo "$DOC" | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])")

echo "== 2. 标注（revision=0 -> 1）=="
curl -sS -X PUT "$BASE/documents/$DOC_ID/annotations" \
  -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":8,"y":8,"w":16,"h":12,"reason":"含证件号码"}]}'
echo

echo "== 3. 携带过期修订号 -> 409 stale_revision =="
curl -sS -X PUT "$BASE/documents/$DOC_ID/annotations" \
  -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[]}'
echo

echo "== 4. 发布 v1 =="
curl -sS -X POST "$BASE/documents/$DOC_ID/publish"
echo

echo "== 5. 修改标注后发布 v2（v1 不受影响）=="
curl -sS -X PUT "$BASE/documents/$DOC_ID/annotations" \
  -H 'Content-Type: application/json' \
  -d '{"revision":1,"rects":[{"x":40,"y":30,"w":10,"h":10,"reason":"人脸"}]}'
echo
curl -sS -X POST "$BASE/documents/$DOC_ID/publish"
echo

echo "== 6. 公开读取 v1 元数据（遮盖坐标+原因）与成品图 =="
curl -sS "$BASE/publications/$DOC_ID/1"
echo
curl -sS -o /tmp/archive_v1.png -w "v1 image: HTTP %{http_code}, %{size_download} bytes\n" \
  "$BASE/publications/$DOC_ID/1/image"

echo "== 7. 未知版本 -> 404 unknown_version =="
curl -sS "$BASE/publications/$DOC_ID/99"
echo
