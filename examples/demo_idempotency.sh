#!/usr/bin/env bash
# 批次发布幂等键端到端示例：
#   上传 2 份原图 -> 标注 -> 携带幂等键同批次发布
#   -> 网络中断后原样重发：返回首次结果（HTTP 200，不产生第二批版本）
#   -> 相同键 + 不同内容：HTTP 409 冲突，原批次不受影响
#   -> 失败请求不占键：修正后可用原键重试成功
# 依赖：curl、python3（仅用于解析 JSON、生成示例图）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
KEY="demo-$(date +%s)-$$"   # 调用方负责生成全局唯一键

python3 - <<'EOF'
from PIL import Image
Image.new("RGB", (64, 48), (30, 90, 160)).save("/tmp/idem_doc_a.png")
Image.new("RGB", (64, 48), (160, 90, 30)).save("/tmp/idem_doc_b.png")
EOF

upload() {  # $1 = 文件路径 -> doc_id
  curl -sS -F "file=@$1" "$BASE/documents" \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])"
}

echo "== 1. 上传两份原图并标注（A -> rev1，B 保持 rev0）=="
A=$(upload /tmp/idem_doc_a.png)
B=$(upload /tmp/idem_doc_b.png)
curl -sS -X PUT "$BASE/documents/$A/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":4,"y":4,"w":12,"h":10,"reason":"身份证号"}]}' >/dev/null
echo "A=$A  B=$B  KEY=$KEY"

echo "== 2. 携带幂等键同批次发布（首次：HTTP 201）=="
REQ="{\"idempotency_key\":\"$KEY\",\"documents\":[
  {\"doc_id\":\"$A\",\"expected_revision\":1},
  {\"doc_id\":\"$B\",\"expected_revision\":0}
]}"
RESP1=$(curl -sS -w "\n%{http_code}" -X POST "$BASE/publication-batches" \
  -H 'Content-Type: application/json' -d "$REQ")
echo "$RESP1" | head -n -1 | python3 -m json.tool
echo "HTTP $(echo "$RESP1" | tail -n 1)"
BATCH_ID=$(echo "$RESP1" | head -n -1 | python3 -c "import sys,json;print(json.load(sys.stdin)['batch_id'])")

echo "== 3. 模拟网络中断后原样重发（相同键+相同内容：HTTP 200，原样返回首次结果）=="
# 期间标注继续变化也不影响重放：不再校验当前修订号、不重新渲染
curl -sS -X PUT "$BASE/documents/$A/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":1,"rects":[{"x":30,"y":30,"w":8,"h":8,"reason":"人脸"}]}' >/dev/null
RESP2=$(curl -sS -w "\n%{http_code}" -X POST "$BASE/publication-batches" \
  -H 'Content-Type: application/json' -d "$REQ")
echo "HTTP $(echo "$RESP2" | tail -n 1)"
echo "$RESP2" | head -n -1 | python3 -c "
import sys, json
again = json.load(sys.stdin)
first = json.loads('''$(echo "$RESP1" | head -n -1)''')
assert again == first, '重放结果与首次不一致'
print('重放结果与首次完全一致，batch_id =', again['batch_id'])
"
echo "A 的公开版本仍只有 v1（未产生第二批版本）："
curl -sS "$BASE/documents/$A" | python3 -c "import sys,json;print([p['version'] for p in json.load(sys.stdin)['publications']])"

echo "== 4. 相同键 + 不同内容：HTTP 409 冲突，原批次不受影响 =="
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/publication-batches" \
  -H 'Content-Type: application/json' \
  -d "{\"idempotency_key\":\"$KEY\",\"documents\":[
    {\"doc_id\":\"$A\",\"expected_revision\":2},
    {\"doc_id\":\"$B\",\"expected_revision\":0}
  ]}"
curl -sS "$BASE/publication-batches/$BATCH_ID" | python3 -c "import sys,json;print('原批次仍在:',json.load(sys.stdin)['batch_id'])"

echo "== 5. 失败不占键：过期修订号 409 后，修正内容可用原键重试成功 =="
KEY2="$KEY-retry"
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/publication-batches" \
  -H 'Content-Type: application/json' \
  -d "{\"idempotency_key\":\"$KEY2\",\"documents\":[
    {\"doc_id\":\"$A\",\"expected_revision\":1},
    {\"doc_id\":\"$B\",\"expected_revision\":0}
  ]}"   # A 当前已是 rev2，预期给 1 -> 整批 409，键不被占用
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/publication-batches" \
  -H 'Content-Type: application/json' \
  -d "{\"idempotency_key\":\"$KEY2\",\"documents\":[
    {\"doc_id\":\"$A\",\"expected_revision\":2},
    {\"doc_id\":\"$B\",\"expected_revision\":0}
  ]}"   # 修正后原键重试 -> 201 首次成功
