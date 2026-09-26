"""生成 Windows 多尺寸 icon.ico，以及 assets/icon-1024.png（macOS .icns 同源）。

内置「CA」字标：地图帮橙底 + 白字，对应产品名 cursorAdmin。
若 assets/icon-1024.png 已存在且未加 --force，则直接用该 PNG 出 ico。

ICO 每一帧写成 BMP/DIB（BGRA + AND 掩码），不用 PNG 帧——Windows UpdateResource
对 PNG 帧会失败；Pillow 新版默认 PNG 帧，故自己拼 ICO 容器。
"""

from __future__ import annotations

import argparse
import os
import struct

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")
LOCAL_PNG = os.path.join(ASSETS, "icon-1024.png")
ICON_SIZES = [16, 24, 32, 48, 64, 128, 256]

SIZE = 1024
# 与 web/style.css --brand / --brand-dark 对齐
BRAND = (250, 140, 22, 255)
BRAND_DARK = (212, 107, 8, 255)
INK = (255, 255, 255, 255)

_FONT_CANDIDATES = (
    "/System/Library/Fonts/SFNSRounded.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
)


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if not os.path.isfile(path):
            continue
        try:
            return ImageFont.truetype(path, size=size, index=0)
        except OSError:
            continue
    return ImageFont.load_default()


def _vertical_gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    column = Image.new("RGBA", (1, size))
    for y in range(size):
        t = y / (size - 1)
        column.putpixel(
            (0, y),
            (
                round(top[0] + (bottom[0] - top[0]) * t),
                round(top[1] + (bottom[1] - top[1]) * t),
                round(top[2] + (bottom[2] - top[2]) * t),
                255,
            ),
        )
    return column.resize((size, size))


def _render_icon() -> Image.Image:
    """圆角橙底 + 居中白字 CA。"""
    gradient = _vertical_gradient(SIZE, BRAND, BRAND_DARK)
    mask = Image.new("L", (SIZE, SIZE), 0)
    # 设计口径「圆角 10」：按 100 单位画板换算到 1024 → 约 10% 边长
    corner = SIZE * 10 // 100
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=corner, fill=255)
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    img.paste(gradient, (0, 0), mask)

    draw = ImageDraw.Draw(img)
    label = "CA"
    # 略偏大，小尺寸缩略后仍能辨认
    font = _load_font(520)
    bbox = draw.textbbox((0, 0), label, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    # SF/Arial 的 bbox 顶边常略偏上，视觉居中再下移一点
    x = (SIZE - tw) / 2 - bbox[0]
    y = (SIZE - th) / 2 - bbox[1] + 18
    draw.text((x, y), label, font=font, fill=INK)
    return img


def load_source(*, force: bool = False) -> Image.Image:
    if os.path.exists(LOCAL_PNG) and not force:
        print("使用自定义图标：", LOCAL_PNG)
        return Image.open(LOCAL_PNG).convert("RGBA")
    print("生成 cursorAdmin「CA」字标图标")
    img = _render_icon()
    os.makedirs(ASSETS, exist_ok=True)
    img.save(LOCAL_PNG)
    print("已写入源图：", LOCAL_PNG)
    return img


def _dib_frame(im: Image.Image) -> bytes:
    """单帧 BMP/DIB：BITMAPINFOHEADER(高度翻倍) + BGRA 像素(自下而上) + 全 0 AND 掩码。"""
    w, h = im.size
    flipped = im.transpose(Image.FLIP_TOP_BOTTOM)
    color = flipped.tobytes("raw", "BGRA")
    mask_row = ((w + 31) // 32) * 4
    mask = b"\x00" * (mask_row * h)
    header = struct.pack(
        "<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, 0, 0, 0, 0, 0
    )
    return header + color + mask


def write_ico(src: Image.Image, out_path: str, sizes) -> None:
    frames = []
    for s in sizes:
        frame = src.resize((s, s), Image.LANCZOS)
        frames.append((s, _dib_frame(frame)))

    entries = bytearray()
    blobs = bytearray()
    offset = 6 + 16 * len(frames)
    for s, data in frames:
        dim = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)

    with open(out_path, "wb") as handle:
        handle.write(struct.pack("<HHH", 0, 1, len(frames)))
        handle.write(bytes(entries))
        handle.write(bytes(blobs))


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 cursorAdmin 图标")
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略已有 assets/icon-1024.png，重新生成 CA 字标",
    )
    args = parser.parse_args()
    src = load_source(force=args.force)
    out = os.path.join(ROOT, "icon.ico")
    write_ico(src, out, ICON_SIZES)
    print("icon.ico created (BMP frames) ->", out)


if __name__ == "__main__":
    main()
