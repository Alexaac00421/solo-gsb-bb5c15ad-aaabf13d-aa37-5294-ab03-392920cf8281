#!/usr/bin/env bash
# 定时公开（统一 UTC 公开时间）端到端示例：
#   上传 + 标注 -> 单份定时发布（约 12 秒后公开）
#   -> 公开时间前详情/图片 404 publication_not_yet_public（不泄露遮盖与文件）
#   -> 时间到达后无需再次操作即可读取
#   -> 无效时间（过去 / 超过 30 天）-> 400 invalid_publish_time，不占版本号
#   -> 批次定时发布 + 幂等键：时间过后原样重放仍返回首次结果（200，不重新校验）
#   -> 同键不同公开时间 -> 409 idempotency_key_conflict
#   -> 公开时间前永久撤回：到点前后都返回 410，时间到达也不恢复
# 依赖：curl、python3（仅用于解析 JSON、生成示例图与 RFC 3339 时间戳）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

iso_in() {  # $1 = 距当前的秒数 -> RFC 3339（UTC，尾部 Z）
  python3 - "$1" <<'EOF'
import sys
from datetime import datetime, timedelta, timezone
print((datetime.now(timezone.utc) + timedelta(seconds=int(sys.argv[1])))
      .replace(microsecond=0).isoformat().replace("+00:00", "Z"))
EOF
}

code() {  # 仅输出 HTTP 状态码
  curl -sS -o /dev/null -w "%{http_code}" "$@"
}

python3 - <<'EOF'
from PIL import Image
Image.new("RGB", (64, 48), (30, 90, 160)).save("/tmp/sched_a.png")
Image.new("RGB", (64, 48), (160, 90, 30)).save("/tmp/sched_b.png")
Image.new("RGB", (64, 48), (90, 30, 160)).save("/tmp/sched_c.png")
EOF

upload() {
  curl -sS -F "file=@$1" "$BASE/documents" \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])"
}

echo "== 1. 上传三份原图并标注 =="
A=$(upload /tmp/sched_a.png)
B=$(upload /tmp/sched_b.png)
C=$(upload /tmp/sched_c.png)
for d in "$A" "$B" "$C"; do
  curl -sS -X PUT "$BASE/documents/$d/annotations" -H 'Content-Type: application/json' \
    -d '{"revision":0,"rects":[{"x":4,"y":4,"w":12,"h":10,"reason":"身份证号"}]}' >/dev/null
done
echo "A=$A B=$B C=$C"

echo "== 2. 单份定时发布 A v1（12 秒后公开）；发布当下即固化成品并占用 v1 =="
T_A=$(iso_in 12)
curl -sS -X POST "$BASE/documents/$A/publish" -H 'Content-Type: application/json' \
  -d "{\"publish_at\":\"$T_A\"}" | python3 -m json.tool
# 立即再发一份（省略 publish_at -> 立即公开），版本号续为 v2
curl -sS -X POST "$BASE/documents/$A/publish"
echo

echo "== 3. 公开时间到达前：A v1 详情与图片均 404 publication_not_yet_public =="
echo "  GET  .../1        -> HTTP $(code "$BASE/publications/$A/1")"
echo "  GET  .../1/image  -> HTTP $(code "$BASE/publications/$A/1/image")"
echo "  A v2 立即公开可读 -> HTTP $(code "$BASE/publications/$A/2")"

echo "== 4. 无效时间：过去 / 超过 30 天 -> 400 invalid_publish_time，且不占版本号 =="
curl -sS -w "\n  HTTP %{http_code}\n" -X POST "$BASE/documents/$B/publish" \
  -H 'Content-Type: application/json' -d '{"publish_at":"2020-01-01T00:00:00Z"}'
curl -sS -w "\n  HTTP %{http_code}\n" -X POST "$BASE/documents/$B/publish" \
  -H 'Content-Type: application/json' \
  -d "{\"publish_at\":\"$(python3 -c 'from datetime import datetime,timedelta,timezone;print((datetime.now(timezone.utc)+timedelta(days=31)).date())')T00:00:00Z\"}"
echo "  两次失败后 B 再发布仍为 v1："
curl -sS -X POST "$BASE/documents/$B/publish" \
  | python3 -c "import sys,json;b=json.load(sys.stdin);print('  version =',b['version'],'publish_at =',b['publish_at'])"

echo "== 5. 批次定时发布 B、C（携带幂等键，8 秒后公开） =="
T_BATCH=$(iso_in 8)
KEY="sched-$(date +%s)-$$"
REQ="{\"idempotency_key\":\"$KEY\",\"publish_at\":\"$T_BATCH\",\"documents\":[
  {\"doc_id\":\"$B\",\"expected_revision\":1},
  {\"doc_id\":\"$C\",\"expected_revision\":1}
]}"
RESP=$(curl -sS -w "\n%{http_code}" -X POST "$BASE/publication-batches" \
  -H 'Content-Type: application/json' -d "$REQ")
echo "  HTTP $(echo "$RESP" | tail -n 1)"
echo "$RESP" | head -n -1 | python3 -c "import sys,json;b=json.load(sys.stdin);print('  batch_id =',b['batch_id'],'publish_at =',b['publish_at'])"
BATCH_ID=$(echo "$RESP" | head -n -1 | python3 -c "import sys,json;print(json.load(sys.stdin)['batch_id'])")
echo "  批次查询同样返回公开时间："
curl -sS "$BASE/publication-batches/$BATCH_ID" \
  | python3 -c "import sys,json;b=json.load(sys.stdin);print('  publish_at =',b['publish_at'],'; 各条目 ->',[(d['doc_id'][:8],d['version'],d['publish_at']) for d in b['documents']])"
echo "  到点前公开读取 B v2：HTTP $(code "$BASE/publications/$B/2")（与立即公开的 v1 同入口）"

echo "== 6. 等待公开时间到达（无需再次操作）……"
sleep 13
echo "  A v1：详情 HTTP $(code "$BASE/publications/$A/1")，图片 HTTP $(code "$BASE/publications/$A/1/image")"
echo "  批次 B v2 / C v1 图片：HTTP $(code "$BASE/publications/$B/2/image") / $(code "$BASE/publications/$C/1/image")"

echo "== 7. 时间过后用原键原样重放批次：HTTP 200，返回首次结果，不重新校验时间窗口 =="
curl -sS -w "\n  HTTP %{http_code}\n" -X POST "$BASE/publication-batches" \
  -H 'Content-Type: application/json' -d "$REQ" | tail -n 2

echo "== 8. 同键不同公开时间 -> 409 idempotency_key_conflict，原批次不受影响 =="
curl -sS -w "\n  HTTP %{http_code}\n" -X POST "$BASE/publication-batches" \
  -H 'Content-Type: application/json' \
  -d "{\"idempotency_key\":\"$KEY\",\"publish_at\":\"$(iso_in 3600)\",\"documents\":[
    {\"doc_id\":\"$B\",\"expected_revision\":1},
    {\"doc_id\":\"$C\",\"expected_revision\":1}
  ]}"

echo "== 9. 永久撤回优先于公开时间：对 C v1 撤回，再等到期也始终 410 =="
curl -sS -o /dev/null -X POST "$BASE/documents/$C/publications/1/withdraw" \
  -H 'Content-Type: application/json' -d '{"reason":"定时版本发现遮盖遗漏"}'
echo "  撤回后 C v1：详情 HTTP $(code "$BASE/publications/$C/1")，图片 HTTP $(code "$BASE/publications/$C/1/image")"
echo "  （公开时间已过；已撤回版本不会因时间到达而恢复）"
