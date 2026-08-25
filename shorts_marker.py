#!/usr/bin/env python3
"""롱폼 영상(파일 또는 유튜브 URL)에서 숏츠 후보 구간을 뽑는 MVP 도구.

사용법:
    python shorts_marker.py <영상파일 경로 | 유튜브 URL>

결과: output/<영상이름>/ 에 transcript.txt, transcript.json, shorts.md, shorts.json 저장.
각 단계는 독립 함수라서 나중에 웹UI(FastAPI 등)가 그대로 import해서 쓸 수 있다.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
CLAUDE_TIMEOUT_SEC = 600


def is_url(source: str) -> bool:
    """유튜브 URL만 허용 (내부망 주소 등으로 요청 유도되는 것 방지)."""
    if not source.startswith(("http://", "https://")):
        return False
    from urllib.parse import urlparse
    host = urlparse(source).hostname or ""
    return host == "youtu.be" or host.endswith("youtube.com")


def safe_name(name: str) -> str:
    """폴더 이름으로 쓸 수 있게 특수문자 제거."""
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    if not name.strip("."):  # "." ".." 같은 이름 반려
        return "video"
    return name[:80] or "video"


def fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def download_media(url: str, workdir: Path) -> Path:
    """유튜브 URL에서 영상(1080p 이하)+음성을 내려받아 파일 경로 반환.

    영상까지 받는 이유: 전사는 음성만으로 되지만 숏츠 자르기에 영상 트랙 필요.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            sys.executable, "-m", "yt_dlp",
            "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
            "--merge-output-format", "mp4",
            # 영상 id 포함 — 같은 제목의 다른 영상이 기존 파일로 오인되는 것 방지
            "-o", str(workdir / "%(title)s [%(id)s].%(ext)s"),
            "--print", "after_move:filepath",
            "--no-simulate",
            "--no-playlist",
            url,
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"유튜브 다운로드 실패:\n{result.stderr.strip()}")
    path = Path(result.stdout.strip().splitlines()[-1])
    if not path.exists():
        raise RuntimeError(f"다운로드 파일을 찾을 수 없음: {path}")
    return path


def transcribe(media_path: Path) -> dict:
    """Whisper로 전사. {language, segments:[{start, end, text}]} 반환."""
    import mlx_whisper  # 무거운 import라 함수 안에서

    result = mlx_whisper.transcribe(str(media_path), path_or_hf_repo=WHISPER_MODEL)
    segments = [
        {"start": round(s["start"], 2), "end": round(s["end"], 2), "text": s["text"].strip()}
        for s in result["segments"]
        if s["text"].strip()
    ]
    return {"language": result.get("language", ""), "segments": segments}


def transcript_to_text(transcript: dict) -> str:
    return "\n".join(
        f"[{fmt_time(s['start'])}] {s['text']}" for s in transcript["segments"]
    )


SELECT_PROMPT = """아래는 롱폼 영상의 타임스탬프 전사본이다. 유튜브 숏츠(세로 60초 이내)로 잘라 쓸 후보 구간을 골라라.

선정 기준:
- 길이 20~60초. 시작과 끝이 말 중간에서 끊기지 않게 전사본의 문장 경계에 맞출 것.
- 훅이 되는 발언: 놀라운 사실, 강한 주장, 질문, 감정, 실용 팁, 완결된 짧은 이야기.
- 3~8개 선정. 억지로 채우지 말고 좋은 구간만.

반드시 아래 형식의 JSON 배열만 출력하라. 설명 문장, 코드펜스 금지.
[{"start_sec": 0.0, "end_sec": 0.0, "title": "숏츠 제목 제안", "reason": "선정 이유 1~2문장", "hook": 8}]
hook은 1~10 훅 강도 점수.

전사본:
"""

def ask_claude_json(prompt: str, pattern: str, retries: int = 1) -> object:
    """Claude Code CLI 호출 후 응답에서 JSON만 추출. 내부서버 이전 시 이 함수만 API 호출로 교체."""
    last_err = None
    for _ in range(retries + 1):
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input=prompt,
            capture_output=True, text=True, timeout=CLAUDE_TIMEOUT_SEC,
        )
        if result.returncode != 0:
            last_err = RuntimeError(f"claude 호출 실패:\n{result.stderr.strip()}")
            continue
        raw = result.stdout.strip()
        match = re.search(pattern, raw, re.DOTALL)  # 앞뒤 잡담이 섞여도 JSON만 추출
        if not match:
            last_err = RuntimeError(f"claude 응답에서 JSON을 찾지 못함:\n{raw[:500]}")
            continue
        text = match.group(0)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 흔한 형식 오류(끝 쉼표)만 정리 후 재시도 — 정상 JSON은 위에서 이미 통과
            text = re.sub(r",\s*([}\]])", r"\1", text)
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                last_err = RuntimeError(f"claude 응답 JSON 형식 오류: {e}\n{text[:500]}")
    raise last_err


# ponytail: 전사본 전체를 한 번에 전달. 2~3시간급 초장편에서 잘리면 청크 분할 추가.
def select_segments(transcript_text: str) -> list[dict]:
    """전사본에서 숏츠 후보 구간 선정. 시각값 검증까지."""
    raw = ask_claude_json(SELECT_PROMPT + transcript_text, r"\[.*\]")
    clips = []
    for c in raw:
        try:
            c["start_sec"] = float(c["start_sec"])
            c["end_sec"] = float(c["end_sec"])
        except (KeyError, TypeError, ValueError):
            continue
        if c["end_sec"] > c["start_sec"]:
            clips.append(c)
    clips.sort(key=lambda c: c["start_sec"])
    return clips


META_PROMPT = """아래는 유튜브 숏츠 클립의 내용이다. 업로드용 메타데이터를 만들어라.

요구사항:
- titles: 제목 3안. 60자 이내, 훅이 되는 문구. 낚시 금지, 내용과 일치.
- description: 설명. 2~4문장으로 내용 요약 + 줄바꿈 후 해시태그 3~5개(#Shorts 포함).
- tags: 태그 12~18개. 한국어 위주, 검색어 형태.

반드시 아래 형식의 JSON 객체만 출력하라. 설명 문장, 코드펜스 금지.
{"titles": ["안1", "안2", "안3"], "description": "...", "tags": ["태그1", "태그2"]}

클립 제목 후보: {title}
선정 이유: {reason}

클립 대사 전문:
{script}
"""


def generate_metadata(clip: dict, script: str) -> dict:
    """숏츠 업로드용 제목·설명·태그 생성."""
    prompt = META_PROMPT.replace("{title}", clip.get("title", "")) \
        .replace("{reason}", clip.get("reason", "")).replace("{script}", script)
    return ask_claude_json(prompt, r"\{.*\}")


def clips_to_markdown(clips: list[dict], source: str) -> str:
    lines = [f"# 숏츠 추천 구간\n", f"원본: {source}\n"]
    for i, c in enumerate(clips, 1):
        dur = int(c["end_sec"] - c["start_sec"])
        lines.append(f"## {i}. {c['title']}")
        lines.append(f"- 구간: {fmt_time(c['start_sec'])} ~ {fmt_time(c['end_sec'])} ({dur}초)")
        lines.append(f"- 훅 점수: {c.get('hook', '?')}/10")
        lines.append(f"- 이유: {c['reason']}\n")
    return "\n".join(lines)


def process(source: str) -> Path:
    """전체 파이프라인. 결과 폴더 경로 반환. 웹UI가 부를 진입점."""
    if is_url(source):
        print("유튜브 다운로드 중...")
        media_path = download_media(source, OUTPUT_DIR / "_downloads")
    else:
        media_path = Path(source).expanduser().resolve()
        if not media_path.exists():
            raise FileNotFoundError(f"파일 없음: {media_path}")

    outdir = OUTPUT_DIR / safe_name(media_path.stem)
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"음성 인식 중... (첫 실행은 모델 다운로드로 오래 걸림)")
    transcript = transcribe(media_path)
    text = transcript_to_text(transcript)
    (outdir / "transcript.json").write_text(
        json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "transcript.txt").write_text(text, encoding="utf-8")
    print(f"전사 완료: {len(transcript['segments'])}개 문장, 언어={transcript['language']}")

    print("숏츠 구간 선정 중...")
    clips = select_segments(text)
    (outdir / "shorts.json").write_text(
        json.dumps({"source": source, "clips": clips}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    md = clips_to_markdown(clips, source)
    (outdir / "shorts.md").write_text(md, encoding="utf-8")

    print("\n" + md)
    print(f"결과 저장: {outdir}")
    return outdir


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    process(sys.argv[1])


if __name__ == "__main__":
    main()
