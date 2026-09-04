"""숏츠 mp4 생성: 구간 자르기 + 세로 1080x1920(위아래 검정) + 텍스트·배너 굽기.

홈브루 ffmpeg에 libass/drawtext가 없어서, 자막·타이틀은 Pillow로 PNG를 그려
ffmpeg overlay 필터로 시간 맞춰 얹는 방식을 쓴다.
웹 편집기(WYSIWYG)가 좌표·크기·색·표시 구간을 style로 넘기면 그대로 렌더링한다.
"""

import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
FONT_PATH = "/System/Library/Fonts/AppleSDGothicNeo.ttc"
MAX_TEXT_W = W - 120
BASE_DIR = Path(__file__).resolve().parent
BGM_DIR = BASE_DIR / "bgm"
BANNER_DIR = BASE_DIR / "banner"
BGM_FADE_SEC = 1.5

# Whisper가 음악·박수 구간에서 뱉는 라벨 — 자막으로 구우면 화면에 "音楽"만 뜬다
NOISE_LABELS = {
    "音楽", "[音楽]", "(音楽)", "♪", "♪♪", "拍手", "[拍手]", "(拍手)", "笑",
    "[音楽]♪", "Music", "[Music]", "(Music)", "Applause", "[Applause]",
    "음악", "[음악]", "박수", "[박수]",
}

# 편집기와 서버가 공유하는 기본 스타일. y는 1080x1920 캔버스 기준 요소 상단 좌표.
# start/dur은 클립 기준 초. dur 0이면 클립 끝까지.
DEFAULT_STYLE = {
    "title": {"enabled": False, "text": "", "y": 170, "size": 76,
              "color": "#FFD400", "outline": "#000000", "start": 0, "dur": 0},
    "title2": {"enabled": False, "text": "", "y": 300, "size": 64,
               "color": "#FFFFFF", "outline": "#000000", "start": 0, "dur": 0},
    "banner": {"enabled": False, "file": "", "y": 420, "width": 700,
               "start": 0, "dur": 0},
    "subs": {"enabled": True, "y": 1330, "size": 58,
             "color": "#FFFFFF", "outline": "#000000"},
    "cta": {"enabled": False, "text": "풀영상은 채널에서", "y": 1600, "size": 48,
            "color": "#7AD97B", "outline": "#000000", "last_sec": 3},
    "bgm": {"file": "", "volume": 0.15},  # bgm/ 폴더의 파일명, 원음 대비 볼륨
    # 사용자가 직접 넣은 자막(번역 등). 절대 시각 기준 [{start, end, text}]
    "extra_subs": [],
}

VF_VERTICAL = (
    "scale=1080:1920:force_original_aspect_ratio=decrease,"
    "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
)


def merge_style(style: dict | None) -> dict:
    """저장된 스타일에 새로 생긴 항목의 기본값을 채워 넣는다."""
    merged = {}
    for key, default in DEFAULT_STYLE.items():
        if isinstance(default, dict):
            merged[key] = dict(default)
            if style and isinstance(style.get(key), dict):
                merged[key].update(style[key])
        else:
            merged[key] = list(style[key]) if style and isinstance(
                style.get(key), list) else list(default)
    return merged


def media_duration(path: Path) -> float:
    """미디어 길이(초). 못 읽으면 0."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(Path(path).resolve())],
        capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


MIN_SUB_SEC = 0.35     # 이보다 짧으면 읽을 수 없음 — Whisper 환청 토막
REPEAT_LIMIT = 3       # 같은 말이 연달아 이만큼 나오면 환청으로 보고 버림


def clean_segments(segments: list[dict]) -> list[dict]:
    """자막으로 쓸 수 없는 세그먼트 제거.

    음악·박수 라벨, 빈 텍스트, 뒤집힌 시각, 너무 짧은 토막,
    그리고 같은 말이 연달아 반복되는 구간(Whisper가 음악·무음에서 뱉는 환청).
    """
    kept = []
    for s in segments:
        text = (s.get("text") or "").strip()
        if not text or text in NOISE_LABELS:
            continue
        start, end = float(s.get("start", 0)), float(s.get("end", 0))
        if end - start < MIN_SUB_SEC:
            continue
        kept.append(s)

    out, i = [], 0
    while i < len(kept):
        j = i
        while j + 1 < len(kept) and kept[j + 1]["text"].strip() == kept[i]["text"].strip():
            j += 1
        run = j - i + 1
        if run < REPEAT_LIMIT:
            out.extend(kept[i:j + 1])
        i = j + 1
    return out


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


def _render_banner_png(src: Path, width: int, out_path: Path):
    """배너 이미지를 지정 폭으로 줄여 PNG로 저장(비율 유지, 투명도 보존)."""
    img = Image.open(src).convert("RGBA")
    width = max(80, min(W, int(width)))
    height = max(1, round(img.height * width / img.width))
    img.resize((width, height), Image.LANCZOS).save(out_path)


def _window(conf: dict, duration: float) -> tuple[float, float]:
    """표시 구간(클립 기준). dur이 0이거나 없으면 시작~클립 끝."""
    start = max(0.0, min(float(conf.get("start", 0) or 0), duration))
    dur = float(conf.get("dur", 0) or 0)
    end = duration if dur <= 0 else min(duration, start + dur)
    return start, max(start, end)


def _safe_pick(directory: Path, name: str) -> Path | None:
    """지정 폴더 안의 파일만 허용 (스타일은 클라이언트 입력이라 경로 검증)."""
    if not name:
        return None
    candidate = directory / Path(name).name
    return candidate if candidate.is_file() else None


def make_short(
    source: Path,
    start: float,
    end: float,
    out_path: Path,
    segments: list[dict] | None = None,
    style: dict | None = None,
) -> Path:
    """구간을 잘라 세로 숏츠 mp4 생성.

    segments: 절대 시각 기준 전사 세그먼트(자막용). None이면 원본 자막 없음.
    style: DEFAULT_STYLE 형태. 편집기에서 넘어온 좌표·크기·색·표시 구간 그대로 사용.
    """
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
         "stream=index", "-of", "csv=p=0", str(source.resolve())],
        capture_output=True, text=True)
    if not probe.stdout.strip():
        raise RuntimeError(
            "원본에 영상 트랙이 없습니다(음성 전용 파일). "
            "예전 버전에서 URL로 분석한 작업이면 영상을 다시 분석해 주세요.")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    st = merge_style(style)
    duration = end - start

    workdir = out_path.parent / f".{out_path.stem}_work"
    workdir.mkdir(exist_ok=True)
    try:
        return _make_short_inner(source, start, duration, out_path, segments, st, workdir)
    finally:
        # 중간에 어떤 예외로 죽어도 PNG 잔재가 남아 다음 시도를 막지 않게
        shutil.rmtree(workdir, ignore_errors=True)


def _make_short_inner(source, start, duration, out_path, segments, st, workdir) -> Path:
    end = start + duration
    overlays = []  # (png_path, y좌표, 표시 시작, 표시 끝) — 클립 기준 시각

    for key in ("title", "title2"):
        t = st[key]
        if t["enabled"] and t["text"].strip():
            png = workdir / f"{key}.png"
            _render_text_png(t["text"], int(t["size"]), t["color"], t["outline"], png)
            s, e = _window(t, duration)
            overlays.append((png, int(t["y"]), s, e))

    b = st["banner"]
    banner_src = _safe_pick(BANNER_DIR, b.get("file", "")) if b.get("enabled") else None
    if banner_src:
        png = workdir / "banner.png"
        _render_banner_png(banner_src, b.get("width", 700), png)
        s, e = _window(b, duration)
        overlays.append((png, int(b["y"]), s, e))

    if st["cta"]["enabled"] and st["cta"]["text"].strip():
        png = workdir / "cta.png"
        c = st["cta"]
        _render_text_png(c["text"], int(c["size"]), c["color"], c["outline"], png)
        overlays.append((png, int(c["y"]),
                         max(0, duration - float(c.get("last_sec", 3))), duration))

    # 사용자가 직접 넣은 자막(번역 등) — 원본 자막 스타일을 그대로 쓴다
    sub_style = st["subs"]
    extra = [x for x in st["extra_subs"]
             if (x.get("text") or "").strip()
             and float(x["end"]) > start and float(x["start"]) < end
             and float(x["end"]) > float(x["start"])]
    for i, x in enumerate(extra):
        png = workdir / f"x{i}.png"
        _render_text_png(x["text"].strip(), int(sub_style["size"]),
                         sub_style["color"], sub_style["outline"], png)
        overlays.append((png, int(sub_style["y"]),
                         max(float(x["start"]), start) - start,
                         min(float(x["end"]), end) - start))

    if segments and sub_style["enabled"]:
        for i, seg in enumerate(
                x for x in clean_segments(segments)
                if x["end"] > start and x["start"] < end
                # 추가 자막이 덮는 시간대는 원본 자막 대신 추가 자막이 나온다
                and not any(float(e["start"]) < (x["start"] + x["end"]) / 2 < float(e["end"])
                            for e in extra)):
            png = workdir / f"s{i}.png"
            _render_text_png(seg["text"], int(sub_style["size"]),
                             sub_style["color"], sub_style["outline"], png)
            overlays.append((png, int(sub_style["y"]),
                             max(seg["start"], start) - start,
                             min(seg["end"], end) - start))

    bgm = st.get("bgm") or {}
    bgm_path = _safe_pick(BGM_DIR, bgm.get("file", ""))

    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-ss", str(start), "-t", str(duration), "-i", str(source.resolve())]
    for png, _, _, _ in overlays:
        cmd += ["-loop", "1", "-i", str(png)]
    if bgm_path:
        cmd += ["-stream_loop", "-1", "-i", str(bgm_path)]  # 짧으면 반복

    graph = [f"[0:v]{VF_VERTICAL}[v0]"]
    for i, (_, y, s, e) in enumerate(overlays):
        graph.append(
            f"[v{i}][{i + 1}:v]overlay=(W-w)/2:{y}"
            f":enable='between(t,{s:.2f},{e:.2f})'[v{i + 1}]")
    audio_map = "0:a?"
    if bgm_path:
        vol = max(0.0, min(1.0, float(bgm.get("volume", 0.15))))
        fade_start = max(0.0, duration - BGM_FADE_SEC)
        ai = len(overlays) + 1
        graph.append(
            f"[{ai}:a]volume={vol},afade=t=out:st={fade_start:.2f}:d={BGM_FADE_SEC}[bgm]")
        # duration=first: 목소리(원본) 길이 기준으로 끝냄, normalize=0: 원음 볼륨 유지
        graph.append("[0:a][bgm]amix=inputs=2:duration=first:normalize=0[aout]")
        audio_map = "[aout]"
    cmd += [
        "-filter_complex", ";".join(graph),
        "-map", f"[v{len(overlays)}]", "-map", audio_map,
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out_path.resolve()),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        out_path.unlink(missing_ok=True)  # 깨진 조각 파일이 목록에 남지 않게
        raise RuntimeError(f"ffmpeg 실패:\n{result.stderr.strip()[-800:]}")
    return out_path
