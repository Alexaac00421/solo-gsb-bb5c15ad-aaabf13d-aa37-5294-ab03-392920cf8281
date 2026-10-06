#!/usr/bin/env bash
# 批次发布端到端示例：
#   上传 3 份原图 -> 各自标注 -> 同批次按预期修订号发布（2~20 份）
#   -> 按批次标识查询（顺序与请求一致）-> 公开读取每份成品
#   -> 演示过期修订号导致整批失败、且不占用任何版本号
# 依赖：curl、python3（仅用于解析 JSON、生成示例图）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

python3 - <<'EOF'
from PIL import Image
Image.new("RGB", (64, 48), (30, 90, 160)).save("/tmp/batch_doc_a.png")
Image.new("RGB", (64, 48), (160, 90, 30)).save("/tmp/batch_doc_b.png")
Image.new("RGB", (64, 48), (90, 30, 160)).save("/tmp/batch_doc_c.png")
EOF

upload() {  # $1 = 文件路径 -> doc_id
  curl -sS -F "file=@$1" "$BASE/documents" \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])"
}

echo "== 1. 上传三份原图 =="
A=$(upload /tmp/batch_doc_a.png)
B=$(upload /tmp/batch_doc_b.png)
C=$(upload /tmp/batch_doc_c.png)
echo "A=$A"; echo "B=$B"; echo "C=$C"

echo "== 2. 分别标注（A -> rev1，B -> rev1，C 保持 rev0）=="
curl -sS -X PUT "$BASE/documents/$A/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":4,"y":4,"w":12,"h":10,"reason":"身份证号"}]}' >/dev/null
curl -sS -X PUT "$BASE/documents/$B/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":40,"y":30,"w":10,"h":10,"reason":"人脸"}]}' >/dev/null

echo "== 3. 同批次发布（携带各自当前修订号，顺序 A,B,C）=="
RESP=$(curl -sS -X POST "$BASE/publication-batches" \
  -H 'Content-Type: application/json' \
  -d "{\"documents\":[
    {\"doc_id\":\"$A\",\"expected_revision\":1},
    {\"doc_id\":\"$B\",\"expected_revision\":1},
    {\"doc_id\":\"$C\",\"expected_revision\":0}
  ]}")
echo "$RESP" | python3 -m json.tool
BATCH_ID=$(echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin)['batch_id'])")

echo "== 4. 按批次标识查询：清单顺序与请求一致（A,B,C）=="
curl -sS "$BASE/publication-batches/$BATCH_ID" | python3 -m json.tool

echo "== 5. 公开读取批次内各文档成品（沿用单份公开读取入口，行为兼容）=="
for pair in "$A 1" "$B 1" "$C 1"; do
  set -- $pair
  curl -sS -o "/tmp/batch_pub_$1.png" -w "doc ${1:0:8} v$2 image: HTTP %{http_code}, %{size_download} bytes\n" \
    "$BASE/publications/$1/$2/image"
done

echo "== 6. 过期修订号：整批 409 失败，不留下部分版本、不占版本号 =="
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/publication-batches" \
  -H 'Content-Type: application/json' \
  -d "{\"documents\":[
    {\"doc_id\":\"$A\",\"expected_revision\":1},
    {\"doc_id\":\"$B\",\"expected_revision\":0}
  ]}"
# B 的预期修订号给了过期的 0（当前为 1），整批失败：
# 两份文档都不会出现 v2；重新以正确修订号请求时版本仍为 2/2
curl -sS "$BASE/documents/$B" | python3 -c "import sys,json;print('B 现有版本:',[p['version'] for p in json.load(sys.stdin)['publications']])"
