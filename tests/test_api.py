import hashlib
import io
import sqlite3
import threading

import pytest
from PIL import Image
from fastapi.testclient import TestClient

from app.config import Settings
from app.imaging import load_oriented, render_publication
from app.main import create_app


def png_bytes(size=(40, 30), color=(200, 10, 10)):
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def jpeg_bytes(size=(10, 20), orientation=None, color=(0, 200, 0)):
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    if orientation:
        exif = img.getexif()
        exif[274] = orientation  # EXIF Orientation 标签
        img.save(buf, "JPEG", exif=exif)
    else:
        img.save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture()
def client(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"))
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def upload(client, data, filename="img.png"):
    return client.post("/documents", files={"file": (filename, data)})


# ---------------- 上传与解码 ----------------

def test_upload_png(client):
    data = png_bytes()
    r = upload(client, data)
    assert r.status_code == 201
    body = r.json()
    assert body["width"] == 40 and body["height"] == 30
    assert body["annotation_revision"] == 0
    assert body["original_sha256"] == hashlib.sha256(data).hexdigest()


def test_exif_orientation_applied_to_coordinate_space(client):
    # 原始像素 10x20，EXIF 方向 6（旋转 90°）=> 标注坐标系应为 20x10
    r = upload(client, jpeg_bytes(size=(10, 20), orientation=6), "x.jpg")
    assert r.status_code == 201
    assert (r.json()["width"], r.json()["height"]) == (20, 10)


def test_decode_failure(client):
    r = upload(client, b"this is definitely not an image")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "image_decode_failed"


def test_truncated_image_decode_failure(client):
    r = upload(client, png_bytes()[:20])
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "image_decode_failed"


def test_unsupported_format(client):
    img = Image.new("RGB", (5, 5))
    buf = io.BytesIO()
    img.save(buf, "GIF")
    r = upload(client, buf.getvalue(), "x.gif")
    assert r.status_code == 415
    assert r.json()["detail"]["code"] == "unsupported_media_type"


def test_document_not_found(client):
    r = client.get("/documents/nope")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "document_not_found"


# ---------------- 标注与修订号 ----------------

def test_annotation_flow_and_stale_revision(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    rect = {"x": 1, "y": 2, "w": 5, "h": 5, "reason": "含身份证号"}

    r = client.put(f"/documents/{doc_id}/annotations",
                   json={"revision": 0, "rects": [rect]})
    assert r.status_code == 200
    assert r.json()["revision"] == 1

    # 携带过期修订号 -> 拒绝
    r = client.put(f"/documents/{doc_id}/annotations",
                   json={"revision": 0, "rects": [rect]})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "stale_revision"
    assert detail["current_revision"] == 1

    # 正确修订号可继续修改（含清空）
    r = client.put(f"/documents/{doc_id}/annotations",
                   json={"revision": 1, "rects": []})
    assert r.status_code == 200
    assert r.json()["revision"] == 2

    r = client.get(f"/documents/{doc_id}/annotations")
    assert r.json()["revision"] == 2
    assert r.json()["rects"] == []


def test_empty_rectangle_rejected(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    for bad in ({"x": 0, "y": 0, "w": 0, "h": 5, "reason": "r"},
                {"x": 0, "y": 0, "w": 5, "h": -1, "reason": "r"}):
        r = client.put(f"/documents/{doc_id}/annotations",
                       json={"revision": 0, "rects": [bad]})
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "empty_rectangle"


def test_out_of_bounds_rectangle_rejected(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]  # 40x30
    for bad in ({"x": -1, "y": 0, "w": 5, "h": 5, "reason": "r"},
                {"x": 35, "y": 0, "w": 6, "h": 5, "reason": "r"},
                {"x": 0, "y": 29, "w": 5, "h": 2, "reason": "r"}):
        r = client.put(f"/documents/{doc_id}/annotations",
                       json={"revision": 0, "rects": [bad]})
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "rectangle_out_of_bounds"


def test_reason_required(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    r = client.put(f"/documents/{doc_id}/annotations",
                   json={"revision": 0,
                         "rects": [{"x": 0, "y": 0, "w": 5, "h": 5, "reason": "  "}]})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "reason_required"


# ---------------- 发布 ----------------

def test_publish_and_immutability(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    client.put(f"/documents/{doc_id}/annotations",
               json={"revision": 0,
                     "rects": [{"x": 0, "y": 0, "w": 10, "h": 10, "reason": "人脸"}]})
    p1 = client.post(f"/documents/{doc_id}/publish")
    assert p1.status_code == 201
    v1 = p1.json()
    assert v1["version"] == 1 and v1["annotation_revision"] == 1

    # 修改标注后再发布
    client.put(f"/documents/{doc_id}/annotations",
               json={"revision": 1,
                     "rects": [{"x": 20, "y": 20, "w": 5, "h": 5, "reason": "车牌"}]})
    p2 = client.post(f"/documents/{doc_id}/publish").json()
    assert p2["version"] == 2 and p2["annotation_revision"] == 2
    assert p2["output_sha256"] != v1["output_sha256"]

    # v1 成品与记录不受后续标注影响
    img1 = client.get(f"/publications/{doc_id}/1/image")
    assert img1.status_code == 200
    assert img1.headers["content-type"] == "image/png"
    assert hashlib.sha256(img1.content).hexdigest() == v1["output_sha256"]
    meta1 = client.get(f"/publications/{doc_id}/1").json()
    assert meta1["redactions"] == [{"x": 0, "y": 0, "w": 10, "h": 10,
                                    "reason": "人脸"}]
    meta2 = client.get(f"/publications/{doc_id}/2").json()
    assert meta2["redactions"] == [{"x": 20, "y": 20, "w": 5, "h": 5,
                                    "reason": "车牌"}]


def test_redaction_pixels_black_and_metadata_stripped(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    client.put(f"/documents/{doc_id}/annotations",
               json={"revision": 0,
                     "rects": [{"x": 5, "y": 5, "w": 10, "h": 10, "reason": "敏感"}]})
    client.post(f"/documents/{doc_id}/publish")
    img_bytes = client.get(f"/publications/{doc_id}/1/image").content
    img = Image.open(io.BytesIO(img_bytes))
    img.load()
    assert img.format == "PNG"
    assert img.size == (40, 30)
    assert img.info.get("exif") is None  # 元数据已剥离
    px = img.convert("RGB")
    assert px.getpixel((7, 7)) == (0, 0, 0)      # 矩形内涂实
    assert px.getpixel((14, 14)) == (0, 0, 0)    # 矩形右下角在内
    assert px.getpixel((15, 15)) == (200, 10, 10)  # 矩形外不受影响
    assert px.getpixel((0, 0)) == (200, 10, 10)


def test_publish_without_annotations(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    p = client.post(f"/documents/{doc_id}/publish")
    assert p.status_code == 201
    assert p.json()["annotation_revision"] == 0
    meta = client.get(f"/publications/{doc_id}/1").json()
    assert meta["redactions"] == []
    img = Image.open(io.BytesIO(
        client.get(f"/publications/{doc_id}/1/image").content))
    assert img.size == (40, 30)
    assert img.convert("RGB").getpixel((20, 15)) == (200, 10, 10)


def test_unknown_version(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    client.post(f"/documents/{doc_id}/publish")
    for path in (f"/publications/{doc_id}/99", f"/publications/{doc_id}/99/image"):
        r = client.get(path)
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "unknown_version"


def test_exif_image_published_in_oriented_coordinates(client):
    # 旋正后 20x10，在旋正坐标系标注，发布图应为 20x10 且矩形被涂黑
    doc_id = upload(client, jpeg_bytes(size=(10, 20), orientation=6), "x.jpg").json()["doc_id"]
    client.put(f"/documents/{doc_id}/annotations",
               json={"revision": 0,
                     "rects": [{"x": 0, "y": 0, "w": 5, "h": 5, "reason": "印章"}]})
    client.post(f"/documents/{doc_id}/publish")
    img = Image.open(io.BytesIO(
        client.get(f"/publications/{doc_id}/1/image").content))
    assert img.size == (20, 10)
    assert img.convert("RGB").getpixel((2, 2)) == (0, 0, 0)


# ---------------- 并发：发布与标注修改 ----------------

def test_concurrent_publish_and_annotate_consistency(client):
    doc_id = upload(client, png_bytes(size=(60, 60))).json()["doc_id"]
    errors = []

    def annotator(n):
        for i in range(10):
            cur = client.get(f"/documents/{doc_id}/annotations").json()["revision"]
            r = client.put(
                f"/documents/{doc_id}/annotations",
                json={"revision": cur,
                      "rects": [{"x": (i * 5) % 50, "y": n * 10, "w": 5, "h": 5,
                                 "reason": f"区域{i}"}]})
            if r.status_code not in (200, 409):  # 409 是合法的并发冲突
                errors.append(f"annotate: {r.status_code} {r.text}")

    def publisher():
        for _ in range(10):
            r = client.post(f"/documents/{doc_id}/publish")
            if r.status_code != 201:
                errors.append(f"publish: {r.status_code} {r.text}")

    threads = ([threading.Thread(target=annotator, args=(k,)) for k in range(2)]
               + [threading.Thread(target=publisher) for _ in range(2)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    # 不变式：每个发布成品的摘要 == 用其记录的修订号对应的矩形重新渲染的结果
    settings = client.app.state.settings
    db = sqlite3.connect(settings.db_path)
    db.row_factory = sqlite3.Row
    doc = db.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    with open(doc["original_path"], "rb") as f:
        oriented, _ = load_oriented(f.read())
    pubs = db.execute(
        "SELECT * FROM publications WHERE doc_id = ?", (doc_id,)).fetchall()
    assert len(pubs) == 20
    for p in pubs:
        rects = db.execute(
            "SELECT x, y, w, h, reason FROM annotation_rects"
            " WHERE doc_id = ? AND revision = ? ORDER BY idx",
            (doc_id, p["annotation_revision"])).fetchall()
        expected = render_publication(oriented, rects)
        buf = io.BytesIO()
        expected.save(buf, "PNG")
        assert hashlib.sha256(buf.getvalue()).hexdigest() == p["output_sha256"], \
            f"publication v{p['version']} 与修订 {p['annotation_revision']} 不一致"
    db.close()
