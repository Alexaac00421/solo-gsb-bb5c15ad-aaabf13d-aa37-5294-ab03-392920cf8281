#!/usr/bin/env bash
# 端到端调用示例：发布基准版本 -> 修改当前标注 -> 比较遮盖范围（只读）->
# 各类拒绝（修订过期 / 版本不存在 / 未到公开时间 / 已撤回）
# 依赖：curl、python3（仅用于解析 JSON 与生成示例图）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

python3 - <<'EOF'
from PIL import Image
Image.new("RGB", (64, 48), (30, 90, 160)).save("/tmp/archive_diff_sample.png")
EOF

echo "== 1. 上传原图（旋正后 64x48，共 3072 像素） =="
DOC=$(curl -sS -F "file=@/tmp/archive_diff_sample.png" "$BASE/documents")
echo "$DOC"
DOC_ID=$(echo "$DOC" | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])")

echo "== 2. 标注并发布基准版本 v1（矩形 x[8,24) y[8,20)，16x12=192 像素） =="
curl -sS -X PUT "$BASE/documents/$DOC_ID/annotations" \
  -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":8,"y":8,"w":16,"h":12,"reason":"含证件号码"}]}'
echo
curl -sS -X POST "$BASE/documents/$DOC_ID/publish"
echo

echo "== 3. 修改当前标注（修订 1 -> 2）：右移 8 像素并加高 =="
curl -sS -X PUT "$BASE/documents/$DOC_ID/annotations" \
  -H 'Content-Type: application/json' \
  -d '{"revision":1,"rects":[{"x":16,"y":8,"w":16,"h":16,"reason":"含证件号码"}]}'
echo

echo "== 4. 比较当前标注(修订 2)与基准 v1（同一读取快照，纯只读） =="
curl -sS -X POST "$BASE/documents/$DOC_ID/coverage-diff" \
  -H 'Content-Type: application/json' \
  -d '{"revision":2,"baseline_version":1}' | python3 -m json.tool
# 基准覆盖 x[8,24) y[8,20)，当前覆盖 x[16,32) y[8,24)，重叠 x[16,24) y[8,20)：
#   新增 = 256-96 = 160 像素（右侧 8×16 + 底部 8×4）
#   失去 = 192-96 = 96 像素（x[8,16)×y[8,20)），区间按水平段列出（相邻相同才合并）

echo "== 5. 修订号过期 -> 409 stale_revision（附 current_revision） =="
curl -sS -X POST "$BASE/documents/$DOC_ID/coverage-diff" \
  -H 'Content-Type: application/json' \
  -d '{"revision":1,"baseline_version":1}'
echo

echo "== 6. 基准版本不存在 -> 404 unknown_version（不泄露遮盖详情） =="
curl -sS -X POST "$BASE/documents/$DOC_ID/coverage-diff" \
  -H 'Content-Type: application/json' \
  -d '{"revision":2,"baseline_version":99}'
echo

echo "== 7. 未到公开时间的版本作基准 -> 404 publication_not_yet_public =="
curl -sS -X POST "$BASE/documents/$DOC_ID/publish" \
  -H 'Content-Type: application/json' \
  -d '{"publish_at":"2026-10-20T08:00:00Z"}' >/dev/null
curl -sS -X POST "$BASE/documents/$DOC_ID/coverage-diff" \
  -H 'Content-Type: application/json' \
  -d '{"revision":2,"baseline_version":2}'
echo

echo "== 8. 已撤回的版本作基准 -> 410 publication_withdrawn（优先于公开时间） =="
curl -sS -X POST "$BASE/documents/$DOC_ID/publications/1/withdraw" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"遮盖遗漏：底部工号未遮盖"}'
echo
curl -sS -X POST "$BASE/documents/$DOC_ID/coverage-diff" \
  -H 'Content-Type: application/json' \
  -d '{"revision":2,"baseline_version":1}'
echo

echo "== 9. 比较是只读的：发布列表仍只有 v1、v2，当前修订号仍为 2 =="
curl -sS "$BASE/documents/$DOC_ID/publications"
echo
