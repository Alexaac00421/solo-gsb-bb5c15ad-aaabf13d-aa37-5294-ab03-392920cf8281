"""图像处理：解码、EXIF 旋正、不可逆涂实、去元数据输出。"""
from __future__ import annotations

from io import BytesIO
from typing import Any, Iterable, Mapping

from PIL import Image, ImageDraw, ImageOps



SUPPORTED_FORMATS = {"PNG": ".png", "JPEG": ".jpg"}


def scale_rect(
    rect: Mapping[str, Any],
    src_width: int,
    src_height: int,
    dst_width: int,
    dst_height: int,
) -> dict:
    """把源图（旋正后）坐标系中的一个矩形缩放到目标图坐标系。

    宽、高方向分别缩放（适配同版式、不同尺寸），全部使用整数运算：
    左上边界向下取整（``floor``），右下边界向上取整（``ceil``），
    使遮盖区域向外扩一点也不缩——宁可多遮，不可漏遮。结果再夹到
    目标图 ``[0, dst_width/dst_height]`` 内。``reason`` 原样保留。

    源矩形为标注接口接受的合法矩形（w,h > 0 且在源图内），因此
    floor 左/上边界严格小于 ceil 右/下边界，输出 w/h 必然 >= 1。
    """
    src_right = rect["x"] + rect["w"]
    src_bottom = rect["y"] + rect["h"]
    # floor(a*num/den)：非负整数下整除即向下取整
    nx = rect["x"] * dst_width // src_width
    ny = rect["y"] * dst_height // src_height
    # ceil(a*num/den) = (a*num + den - 1) // den
    nr = (src_right * dst_width + src_width - 1) // src_width
    nb = (src_bottom * dst_height + src_height - 1) // src_height
    # 限制在目标图内
    nx = max(0, min(nx, dst_width))
    ny = max(0, min(ny, dst_height))
    nr = max(0, min(nr, dst_width))
    nb = max(0, min(nb, dst_height))
    return {"x": nx, "y": ny, "w": nr - nx, "h": nb - ny, "reason": rect["reason"]}


# ---------------- 遮盖范围比较（旋正后像素坐标，全整数运算） ----------------

def _clipped_rect(
    rect: Mapping[str, Any], width: int, height: int
) -> tuple[int, int, int, int] | None:
    """把矩形规范成旋正后图像内的半开像素区间 (x0, y0, x1, y1)。

    库内矩形在写入时已校验为 w/h>0 且不越界，这里的夹取仅为防御；
    夹取后为空（无像素）时返回 None。
    """
    x0 = max(0, min(width, int(rect["x"])))
    y0 = max(0, min(height, int(rect["y"])))
    x1 = max(0, min(width, int(rect["x"]) + int(rect["w"])))
    y1 = max(0, min(height, int(rect["y"]) + int(rect["h"])))
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def _merge_intervals(
    spans: Iterable[tuple[int, int]],
) -> list[tuple[int, int]]:
    """合并半开整数区间（左闭右开）为有序、互不相交的并集。

    以像素网格计：相接区间（如 [0,10) 与 [10,20)）不共享任何像素
    （边界相接不算重叠），将其连续化不改变并集像素数；真正重叠的区间
    （如 [0,11) 与 [10,20) 共享像素列 10）只计一次。
    """
    merged: list[tuple[int, int]] = []
    for a, b in sorted(spans):
        if b <= a:
            continue
        if merged and a <= merged[-1][1]:
            if b > merged[-1][1]:
                merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def _subtract_intervals(
    minuend: list[tuple[int, int]], subtrahend: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """两个有序、互不相交半开区间并集之差 A \\ B（结果同样有序不相交）。"""
    result: list[tuple[int, int]] = []
    j = 0
    nb = len(subtrahend)
    for s, e in minuend:
        while j < nb and subtrahend[j][1] <= s:
            j += 1  # 完全落在本段左侧的区间，对后续各段也无需再看
        cur = s
        k = j
        while k < nb and subtrahend[k][0] < e:
            bs, be = subtrahend[k]
            if bs > cur:
                result.append((cur, bs if bs < e else e))
            if be > cur:
                cur = be
            if cur >= e:
                break
            k += 1
        if cur < e:
            result.append((cur, e))
    return result


def coverage_diff(
    current: Iterable[Mapping[str, Any]],
    baseline: Iterable[Mapping[str, Any]],
    width: int,
    height: int,
) -> dict:
    """比较当前标注与基准（发布时冻结）标注的遮盖像素集合。

    全部基于旋正后图像的整数像素坐标，矩形按半开区间 [x, x+w) × [y, y+h)
    处理：重叠矩形的像素只计一次，边界相接（共享零个像素）不算重叠。
    以所有矩形的水平边切出水平条带，条带内分别求两套矩形的 x 区间并集，
    再做集合差：

    - 新增遮盖 = 当前并集 − 基准并集；失去遮盖 = 基准并集 − 当前并集；
    - ``lost_segments`` 仅列存在失去遮盖的水平段 [y0, y1)，段内按 x 升序
      列出失去遮盖的像素列区间 [x0, x1)；相邻段的区间列表完全相同时合并
      为一段（同样半开、右端不含）。

    返回 {"added_pixels", "lost_pixels", "lost_segments"}；占比由路由层
    除以旋正后图像总像素 width*height 得到。
    """
    cur_rects: list[tuple[int, int, int, int]] = []
    base_rects: list[tuple[int, int, int, int]] = []
    for r in current:
        clipped = _clipped_rect(r, width, height)
        if clipped is not None:
            cur_rects.append(clipped)
    for r in baseline:
        clipped = _clipped_rect(r, width, height)
        if clipped is not None:
            base_rects.append(clipped)

    # 条带边界来自两套矩形的全部水平边；条带内任一矩形要么整段覆盖、要么不覆盖
    y_edges = sorted(
        {edge for (_, y0, _, y1) in cur_rects + base_rects
         for edge in (y0, y1)}
    )

    added_pixels = 0
    lost_pixels = 0
    merged_segments: list[tuple[int, int, tuple[tuple[int, int], ...]]] = []
    for y0, y1 in zip(y_edges, y_edges[1:]):
        cur_iv = _merge_intervals(
            (x0, x1) for (x0, ry0, x1, ry1) in cur_rects
            if ry0 <= y0 and ry1 >= y1
        )
        base_iv = _merge_intervals(
            (x0, x1) for (x0, ry0, x1, ry1) in base_rects
            if ry0 <= y0 and ry1 >= y1
        )
        added_iv = _subtract_intervals(cur_iv, base_iv)
        lost_iv = _subtract_intervals(base_iv, cur_iv)
        band_h = y1 - y0
        added_pixels += sum(x1 - x0 for x0, x1 in added_iv) * band_h
        lost_pixels += sum(x1 - x0 for x0, x1 in lost_iv) * band_h
        if lost_iv:
            iv_key = tuple(lost_iv)
            if (
                merged_segments
                and merged_segments[-1][1] == y0
                and merged_segments[-1][2] == iv_key
            ):
                # 相邻水平段的失去遮盖区间完全相同 -> 合并
                merged_segments[-1] = (
                    merged_segments[-1][0], y1, iv_key
                )
            else:
                merged_segments.append((y0, y1, iv_key))

    return {
        "added_pixels": added_pixels,
        "lost_pixels": lost_pixels,
        "lost_segments": [
            {
                "y0": seg_y0,
                "y1": seg_y1,
                "intervals": [
                    {"x0": x0, "x1": x1} for x0, x1 in intervals
                ],
            }
            for seg_y0, seg_y1, intervals in merged_segments
        ],
    }


class ImageDecodeError(Exception):
    """上传字节无法解码为图像。"""


class UnsupportedFormatError(Exception):
    """能解码但不是 PNG/JPEG。"""


def load_oriented(data: bytes) -> tuple[Image.Image, str]:
    """解码图像并按 EXIF Orientation 旋正。

    返回 (旋正后的图像, 原始格式)。标注坐标系即该图像的像素坐标，
    与查看器看到的方向一致。
    """
    try:
        img = Image.open(BytesIO(data))
        fmt = img.format
        img.load()  # 强制解码，尽早暴露截断/损坏数据
    except Exception as exc:
        raise ImageDecodeError(str(exc) or exc.__class__.__name__) from exc
    if fmt not in SUPPORTED_FORMATS:
        raise UnsupportedFormatError(fmt or "unknown")
    try:
        oriented = ImageOps.exif_transpose(img)
        oriented.load()
    except Exception as exc:
        raise ImageDecodeError(str(exc) or exc.__class__.__name__) from exc
    return oriented, fmt


# 各模式的"纯黑不透明"填充值
_FILLS = {
    "RGB": (0, 0, 0),
    "RGBA": (0, 0, 0, 255),
    "L": 0,
    "LA": (0, 255),
}


def render_publication(
    oriented: Image.Image, rects: Iterable[Mapping[str, Any]]
) -> Image.Image:
    """把敏感矩形涂实为纯黑，并返回不携带任何元数据的干净图像。

    涂实是单向的：输出中矩形区域只含黑色像素，无法还原。
    """
    fill = _FILLS.get(oriented.mode)
    if fill is None:  # P / CMYK / I;16 等统一转 RGB
        oriented = oriented.convert("RGB")
        fill = (0, 0, 0)
    out = oriented.copy()
    draw = ImageDraw.Draw(out)
    for r in rects:
        x, y, w, h = r["x"], r["y"], r["w"], r["h"]
        draw.rectangle([x, y, x + w - 1, y + h - 1], fill=fill)
    # 重建像素容器，彻底丢弃 EXIF/ICC/文本等元数据
    clean = Image.new(out.mode, out.size)
    clean.paste(out)
    return clean
