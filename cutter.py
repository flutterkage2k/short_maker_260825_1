"""숏츠 mp4 생성: 구간 자르기 + 세로 1080x1920(위아래 검정) + 텍스트 굽기.

홈브루 ffmpeg에 libass/drawtext가 없어서, 자막·타이틀은 Pillow로 PNG를 그려
ffmpeg overlay 필터로 시간 맞춰 얹는 방식을 쓴다.
웹 편집기(WYSIWYG)가 좌표·크기·색을 style로 넘기면 그대로 렌더링한다.
"""

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
FONT_PATH = "/System/Library/Fonts/AppleSDGothicNeo.ttc"
MAX_TEXT_W = W - 120

# 편집기와 서버가 공유하는 기본 스타일. y는 1080x1920 캔버스 기준 텍스트 상단 좌표.
# cta는 마지막 last_sec초 동안만 표시되는 텍스트(루프 안 깨는 CTA용).
DEFAULT_STYLE = {
    "title": {"enabled": False, "text": "", "y": 170, "size": 76,
              "color": "#FFD400", "outline": "#000000"},
    "subs": {"enabled": True, "y": 1330, "size": 58,
             "color": "#FFFFFF", "outline": "#000000"},
    "cta": {"enabled": False, "text": "풀영상은 채널에서", "y": 1600, "size": 48,
            "color": "#7AD97B", "outline": "#000000", "last_sec": 3},
}

VF_VERTICAL = (
    "scale=1080:1920:force_original_aspect_ratio=decrease,"
    "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
)


def merge_style(style: dict | None) -> dict:
    merged = {k: dict(v) for k, v in DEFAULT_STYLE.items()}
    for key in merged:
        if style and key in style:
            merged[key].update(style[key])
    return merged


def _hex_rgba(hex_color: str) -> tuple:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) + (255,)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font) -> list[str]:
    """폭 초과하면 어절 단위 줄바꿈."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        cand = f"{cur} {w}".strip()
        if draw.textlength(cand, font=font) <= MAX_TEXT_W or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _render_text_png(text: str, size: int, color: str, outline: str, out_path: Path):
    """외곽선 있는 텍스트 PNG(투명 배경, 가로 1080, 가운데 정렬) 생성."""
    font = ImageFont.truetype(FONT_PATH, size)
    stroke = max(3, size // 15)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lines = _wrap(probe, text, font)
    line_h = size + 14
    img = Image.new("RGBA", (W, line_h * len(lines) + stroke * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        tw = draw.textlength(line, font=font)
        draw.text(((W - tw) / 2, stroke + i * line_h), line, font=font,
                  fill=_hex_rgba(color), stroke_width=stroke,
                  stroke_fill=_hex_rgba(outline))
    img.save(out_path)


def make_short(
    source: Path,
    start: float,
    end: float,
    out_path: Path,
    segments: list[dict] | None = None,
    style: dict | None = None,
) -> Path:
    """구간을 잘라 세로 숏츠 mp4 생성.

    segments: 절대 시각 기준 전사 세그먼트(자막용). None이면 자막 없음.
    style: DEFAULT_STYLE 형태. 편집기에서 넘어온 좌표·크기·색 그대로 사용.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    st = merge_style(style)
    duration = end - start

    workdir = out_path.parent / f".{out_path.stem}_work"
    workdir.mkdir(exist_ok=True)
    overlays = []  # (png_path, y좌표, 표시 시작, 표시 끝) — 클립 기준 시각

    if st["title"]["enabled"] and st["title"]["text"].strip():
        png = workdir / "title.png"
        t = st["title"]
        _render_text_png(t["text"], int(t["size"]), t["color"], t["outline"], png)
        overlays.append((png, int(t["y"]), 0, duration))

    if st["cta"]["enabled"] and st["cta"]["text"].strip():
        png = workdir / "cta.png"
        c = st["cta"]
        _render_text_png(c["text"], int(c["size"]), c["color"], c["outline"], png)
        overlays.append((png, int(c["y"]),
                         max(0, duration - float(c.get("last_sec", 3))), duration))

    if segments and st["subs"]["enabled"]:
        s = st["subs"]
        for i, seg in enumerate(x for x in segments
                                if x["end"] > start and x["start"] < end):
            png = workdir / f"s{i}.png"
            _render_text_png(seg["text"], int(s["size"]), s["color"], s["outline"], png)
            overlays.append((png, int(s["y"]),
                             max(seg["start"], start) - start,
                             min(seg["end"], end) - start))

    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-ss", str(start), "-t", str(duration), "-i", str(source.resolve())]
    for png, _, _, _ in overlays:
        cmd += ["-loop", "1", "-i", str(png)]

    graph = [f"[0:v]{VF_VERTICAL}[v0]"]
    for i, (_, y, s, e) in enumerate(overlays):
        graph.append(
            f"[v{i}][{i + 1}:v]overlay=(W-w)/2:{y}"
            f":enable='between(t,{s:.2f},{e:.2f})'[v{i + 1}]")
    cmd += [
        "-filter_complex", ";".join(graph),
        "-map", f"[v{len(overlays)}]", "-map", "0:a?",
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out_path.resolve()),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    for png, _, _, _ in overlays:
        png.unlink(missing_ok=True)
    workdir.rmdir()
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 실패:\n{result.stderr.strip()[-800:]}")
    return out_path
