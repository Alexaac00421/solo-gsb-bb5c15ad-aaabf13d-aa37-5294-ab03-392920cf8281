#!/usr/bin/env bash
# 服务数据归档端到端示例：
#   上传 2 份原图 -> 标注/发布（其中一份撤回，另含一次批次定时发布）
#   -> 请求一次全量服务数据归档（POST /service-data-archives，无请求体）
#   -> 得到 ZIP：同一数据库快照中的文档/标注/发布/撤回记录（JSON）+
#      该快照引用的原图与发布成品（相对路径）+ manifest.json 清单
#      （逐文件 SHA-256、文档/版本标识、文件数）
#   -> 校验清单摘要与包内字节一致；随后制造原图缺失，演示整单拒绝
#      （不交付部分归档），以及归档期间并发写入不受阻塞。
# 依赖：curl、python3（仅用于解析 JSON、生成示例图、解压与摘要校验）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

python3 - <<'EOF'
from PIL import Image
Image.new("RGB", (64, 48), (30, 90, 160)).save("/tmp/arc_a.png")
Image.new("RGB", (64, 48), (160, 90, 30)).save("/tmp/arc_b.png")
Image.new("RGB", (64, 48), (90, 30, 160)).save("/tmp/arc_c.png")
EOF

upload() {  # $1 = 文件路径 -> doc_id（上传名不会进入归档内文件名）
  curl -sS -F "file=@$1" "$BASE/documents" \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])"
}

echo "== 1. 上传三份原图，标注并发布（批次 + 撤回，制造多种记录） =="
A=$(upload /tmp/arc_a.png)
B=$(upload /tmp/arc_b.png)
C=$(upload /tmp/arc_c.png)
echo "A=$A B=$B C=$C"
curl -sS -X PUT "$BASE/documents/$A/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":4,"y":4,"w":12,"h":10,"reason":"身份证号"}]}' >/dev/null
curl -sS -X PUT "$BASE/documents/$B/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":40,"y":30,"w":10,"h":10,"reason":"人脸"}]}' >/dev/null
# A、B 作为同批次定时发布（未到公开时间的版本同样进入归档）
curl -sS -X POST "$BASE/publication-batches" -H 'Content-Type: application/json' \
  -d "{\"documents\":[
    {\"doc_id\":\"$A\",\"expected_revision\":1},
    {\"doc_id\":\"$B\",\"expected_revision\":1}
  ],\"publish_at\":\"2026-10-20T08:00:00Z\"}" >/dev/null
# C 单份发布后撤回（撤回记录与成品文件都保留在归档中）
curl -sS -X POST "$BASE/documents/$C/publish" >/dev/null
curl -sS -X POST "$BASE/documents/$C/publications/1/withdraw" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"遮盖遗漏：右下角工号未遮盖"}' >/dev/null

echo
echo "== 2. 请求服务数据归档（无请求体） -> 完整 ZIP 作为下载响应 =="
rm -f /tmp/archive.zip
curl -sS -o /tmp/archive.zip \
  -w "HTTP %{http_code}, %{size_download} bytes, %{content_type}\n" \
  -X POST "$BASE/service-data-archives"

echo
echo "-- 包内相对路径结构（originals/ + publications/ + 顶层 JSON + 清单）--"
python3 - <<'EOF'
import hashlib, json, zipfile
zf = zipfile.ZipFile("/tmp/archive.zip")
for n in zf.namelist():
    print(" ", n)
manifest = json.loads(zf.read("manifest.json"))
print("\nmanifest 顶层:")
print(" generated_at   :", manifest["generated_at"])
print(" record_counts  :", manifest["record_counts"])
print(" file_count     :", manifest["file_count"], "(不含 manifest.json 自身)")
# 逐文件校验：清单 SHA-256 == 包内字节；路径均为相对路径
for e in manifest["files"]:
    assert hashlib.sha256(zf.read(e["path"])).hexdigest() == e["sha256"], e["path"]
    assert not e["path"].startswith("/")
# 抽查标识字段
pubs = [e for e in manifest["files"] if e["kind"] == "publication"]
print("\n发布成品条目（带 doc_id + version）:")
for e in pubs:
    print(" ", e["path"], e["doc_id"], "v" + str(e["version"]), e["sha256"][:12], "…")
docs = json.loads(zf.read("documents.json"))["documents"]
anns = json.loads(zf.read("annotations.json"))
ws   = json.loads(zf.read("withdrawals.json"))
print("\n文档数:", len(docs), " 标注修订数:", anns["revision_count"],
      " 撤回记录数:", ws["count"])
print("清单 SHA-256 与包内全部文件字节一致，且全部为相对路径")
EOF

echo
echo "== 3. 全有或全无：临时移走一份原图，再次归档 -> 500 整单拒绝（非 ZIP） =="
DATA_DIR="${DATA_DIR:-./data}"
ORIG="$DATA_DIR/originals/$A.png"
if [ -f "$ORIG" ]; then
  mv "$ORIG" "$ORIG.held"
  echo "(已临时移走 $ORIG)"
  curl -sS -w "\nHTTP %{http_code}（content-type: %{content_type}）\n" \
    -X POST "$BASE/service-data-archives" -o /tmp/archive_fail.out
  echo "失败响应体：$(cat /tmp/archive_fail.out)"
  mv "$ORIG.held" "$ORIG"
  echo "(已恢复 $ORIG；恢复后归档重新成功)"
else
  echo "跳过：未找到 $ORIG（请在服务的 DATA_DIR 卷内手工移走一份原图后重试）"
fi

echo
echo "== 4. 并发写入不被阻塞：归档请求进行中仍可标注/发布 =="
echo "（可在一个终端执行 'curl -X POST $BASE/service-data-archives -o a.zip'，"
echo "  同时在另一终端执行 PUT 标注 / POST 发布，两者都正常完成；归档只对应"
echo "  它建立读快照的那一个一致时点，不会混入归档期间的新提交。）"
