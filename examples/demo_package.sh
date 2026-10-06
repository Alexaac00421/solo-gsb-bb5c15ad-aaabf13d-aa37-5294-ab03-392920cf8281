#!/usr/bin/env bash
# 离线交付包端到端示例：
#   上传 3 份原图 -> 各自标注/发布 -> 按请求顺序请求 2~20 个已公开版本
#   -> 得到 ZIP（各版 PNG + manifest.json 清单），清单按请求顺序记录
#      文档ID、版本号、成品 SHA-256、遮盖数、包内文件名（文件名不取上传名）
#   -> 演示各类整包拒绝：数量越界、重复项、未知版本、未到公开时间、已撤回
#      （均不交付部分包，也不泄露该项成品/遮盖详情）
# 依赖：curl、python3（仅用于解析 JSON、生成示例图、解压展示清单）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

python3 - <<'EOF'
from PIL import Image
Image.new("RGB", (64, 48), (30, 90, 160)).save("/tmp/pkg_a.png")
Image.new("RGB", (64, 48), (160, 90, 30)).save("/tmp/pkg_b.png")
Image.new("RGB", (64, 48), (90, 30, 160)).save("/tmp/pkg_c.png")
EOF

upload() {  # $1 = 文件路径 -> doc_id（上传名仅记录在案，不会进入交付包文件名）
  curl -sS -F "file=@$1" "$BASE/documents" \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])"
}

echo "== 1. 上传三份原图并分别标注、发布 =="
A=$(upload /tmp/pkg_a.png)
B=$(upload /tmp/pkg_b.png)
C=$(upload /tmp/pkg_c.png)
echo "A=$A B=$B C=$C"
curl -sS -X PUT "$BASE/documents/$A/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":4,"y":4,"w":12,"h":10,"reason":"身份证号"}]}' >/dev/null
curl -sS -X PUT "$BASE/documents/$B/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":40,"y":30,"w":10,"h":10,"reason":"人脸"},{"x":8,"y":8,"w":6,"h":6,"reason":"工号"}]}' >/dev/null
curl -sS -X POST "$BASE/documents/$A/publish" >/dev/null
curl -sS -X POST "$BASE/documents/$B/publish" >/dev/null
curl -sS -X POST "$BASE/documents/$C/publish" >/dev/null

echo
echo "== 2. 按请求顺序 C(v1), A(v1), B(v1) 请求离线交付包 =="
rm -f /tmp/pkg.zip
curl -sS -o /tmp/pkg.zip -w "HTTP %{http_code}, %{size_download} bytes, %{content_type}\n" \
  -X POST "$BASE/publication-packages" \
  -H 'Content-Type: application/json' \
  -d "{\"items\":[
    {\"doc_id\":\"$C\",\"version\":1},
    {\"doc_id\":\"$A\",\"version\":1},
    {\"doc_id\":\"$B\",\"version\":1}
  ]}"

echo "-- 包内文件清单（文件名只由位置+文档ID+版本号生成，与上传名无关）--"
python3 - <<'EOF'
import hashlib, json, zipfile
zf = zipfile.ZipFile("/tmp/pkg.zip")
print("\n".join(zf.namelist()))
manifest = json.loads(zf.read("manifest.json"))
print("\nmanifest.json:")
print(json.dumps(manifest, ensure_ascii=False, indent=2))
# 校验：清单记录的 SHA-256 与包内成品字节一致
for it in manifest["items"]:
    assert hashlib.sha256(zf.read(it["file"])).hexdigest() == it["output_sha256"]
print("清单中的 output_sha256 与包内 PNG 字节全部一致")
EOF

echo
echo "== 3. 数量越界（仅 1 项）-> 400 invalid_package_size =="
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/publication-packages" \
  -H 'Content-Type: application/json' \
  -d "{\"items\":[{\"doc_id\":\"$A\",\"version\":1}]}"

echo
echo "== 4. 重复项（同一 doc_id+version 出现两次）-> 400 duplicate_item_in_package =="
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/publication-packages" \
  -H 'Content-Type: application/json' \
  -d "{\"items\":[
    {\"doc_id\":\"$A\",\"version\":1},
    {\"doc_id\":\"$B\",\"version\":1},
    {\"doc_id\":\"$A\",\"version\":1}
  ]}"

echo
echo "== 5. 未知版本 -> 404 unknown_version（附 position，整包拒绝）=="
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/publication-packages" \
  -H 'Content-Type: application/json' \
  -d "{\"items\":[
    {\"doc_id\":\"$A\",\"version\":1},
    {\"doc_id\":\"$B\",\"version\":99}
  ]}"

echo
echo "== 6. 包内含未到公开时间的版本 -> 404 publication_not_yet_public =="
curl -sS -X POST "$BASE/documents/$C/publish" -H 'Content-Type: application/json' \
  -d '{"publish_at":"2026-10-20T08:00:00Z"}' >/dev/null
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/publication-packages" \
  -H 'Content-Type: application/json' \
  -d "{\"items\":[
    {\"doc_id\":\"$A\",\"version\":1},
    {\"doc_id\":\"$C\",\"version\":2}
  ]}"

echo
echo "== 7. 包内含已撤回版本 -> 410 publication_withdrawn（不泄露成品/遮盖详情）=="
curl -sS -X POST "$BASE/documents/$A/publications/1/withdraw" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"遮盖遗漏：右下角工号未遮盖"}' >/dev/null
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/publication-packages" \
  -H 'Content-Type: application/json' \
  -d "{\"items\":[
    {\"doc_id\":\"$B\",\"version\":1},
    {\"doc_id\":\"$A\",\"version\":1}
  ]}"
echo "# 注意：步骤 2 已确认的交付不受步骤 7 撤回影响（/tmp/pkg.zip 中 A/v1 成品保留）"
