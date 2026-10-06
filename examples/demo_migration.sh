#!/usr/bin/env bash
# 遮盖方案迁移端到端示例：
#   上传 1 份源图（100x50）与 2 份同版式不同尺寸目标图（200x100、50x25）
#   -> 源图标注（修订号 1）-> 一次性把方案按宽高分别缩放迁移到两份目标
#   -> 核对目标当前标注（修订号各 +1）与源标注不变
#   -> 演示修订号过期导致整批失败、所有目标不变
#   -> 演示源不能作为目标、源当前标注为空两种拒绝
# 依赖：curl、python3（仅用于解析 JSON、生成示例图）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

python3 - <<'EOF'
from PIL import Image
Image.new("RGB", (100, 50), (30, 90, 160)).save("/tmp/mig_src.png")
Image.new("RGB", (200, 100), (160, 90, 30)).save("/tmp/mig_big.png")
Image.new("RGB", (50, 25), (90, 30, 160)).save("/tmp/mig_small.png")
EOF

upload() {  # $1 = 文件路径 -> doc_id
  curl -sS -F "file=@$1" "$BASE/documents" \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])"
}

echo "== 1. 上传源图（100x50）与两份目标图（200x100、50x25）=="
SRC=$(upload /tmp/mig_src.png)
BIG=$(upload /tmp/mig_big.png)
SMALL=$(upload /tmp/mig_small.png)
echo "SRC=$SRC  BIG=$BIG  SMALL=$SMALL"

echo "== 2. 在源图上标注（当前修订号 0 -> 1）=="
curl -sS -X PUT "$BASE/documents/$SRC/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[
        {"x":0,"y":0,"w":100,"h":50,"reason":"整页边框"},
        {"x":33,"y":7,"w":3,"h":3,"reason":"印章"}
      ]}' | python3 -m json.tool

echo "== 3. 迁移：源 rev1 -> BIG(预期 rev0)、SMALL(预期 rev0)，顺序 BIG,SMALL =="
RESP=$(curl -sS -X POST "$BASE/annotation-migrations" \
  -H 'Content-Type: application/json' \
  -d "{
    \"source_doc_id\": \"$SRC\",
    \"source_revision\": 1,
    \"targets\": [
      {\"doc_id\": \"$BIG\", \"expected_revision\": 0},
      {\"doc_id\": \"$SMALL\", \"expected_revision\": 0}
    ]
  }")
echo "$RESP" | python3 -m json.tool
# BIG(200x100)：整页 -> 0,0,200,100；印章 33/3 -> 66，7/3 -> 14，3*2 -> 6
# SMALL(50x25)：整页 -> 0,0,50,25；印章 x 33*0.5=16.5 -> 左 16、右 ceil(18)=18；
#               y 7*0.5=3.5 -> 上 3、下 5

echo "== 4. 核对各目标当前标注（修订号均为 1），源标注仍是 rev1 不变 =="
curl -sS "$BASE/documents/$BIG/annotations" | python3 -m json.tool
curl -sS "$BASE/documents/$SMALL/annotations" | python3 -m json.tool
curl -sS "$BASE/documents/$SRC/annotations" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('源修订号:',d['revision'],'矩形数:',len(d['rects']))"

echo "== 5. 过期修订号：整批 409，所有目标不变（BIG 仍停在 rev1）=="
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/annotation-migrations" \
  -H 'Content-Type: application/json' \
  -d "{
    \"source_doc_id\": \"$SRC\",
    \"source_revision\": 1,
    \"targets\": [
      {\"doc_id\": \"$BIG\", \"expected_revision\": 0},
      {\"doc_id\": \"$SMALL\", \"expected_revision\": 1}
    ]
  }"
# BIG 已在第 3 步迁移为 rev1，请求却带 0 -> stale_revision（position=0），
# SMALL 也不会被改动；用正确修订号重试时两份都拿到 rev2。

echo "== 6. 源不能作为目标 / 源当前标注为空：400 =="
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/annotation-migrations" \
  -H 'Content-Type: application/json' \
  -d "{
    \"source_doc_id\": \"$SRC\", \"source_revision\": 1,
    \"targets\": [{\"doc_id\": \"$SRC\", \"expected_revision\": 1}]
  }"

EMPTY=$(upload /tmp/mig_small.png)
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/annotation-migrations" \
  -H 'Content-Type: application/json' \
  -d "{
    \"source_doc_id\": \"$EMPTY\", \"source_revision\": 0,
    \"targets\": [{\"doc_id\": \"$BIG\", \"expected_revision\": 1}]
  }"
# 新上传文档当前标注为空 -> source_annotations_empty；BIG 不受影响
