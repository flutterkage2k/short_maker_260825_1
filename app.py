"""숏츠 마커 웹UI 서버.

실행:
    .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000

브라우저에서 http://localhost:8000 접속.
무거운 작업(다운로드·음성인식·구간선정)은 워커 스레드 1개가 순서대로 처리.
자르기(ffmpeg)는 빨라서 요청 안에서 바로 처리.
"""

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import cutter
from shorts_marker import (
    OUTPUT_DIR, clips_to_markdown, download_media, generate_metadata, is_url,
    safe_name, select_segments, transcribe, transcript_to_text,
)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = OUTPUT_DIR / "_uploads"
STATE_LABELS = {
    "queued": "대기 중",
    "downloading": "유튜브 다운로드 중",
    "transcribing": "음성 인식 중",
    "selecting": "숏츠 구간 선정 중",
    "done": "완료",
    "error": "오류",
}

app = FastAPI(title="숏츠 마커")

job_queue: "queue.Queue[dict]" = queue.Queue()
active_jobs: dict[str, dict] = {}  # id -> {label, state, error, outdir}
jobs_lock = threading.Lock()


def write_status(outdir: Path, state: str, error: str | None = None,
                 elapsed: int | None = None):
    (outdir / "status.json").write_text(
        json.dumps({"state": state, "error": error, "elapsed": elapsed},
                   ensure_ascii=False), encoding="utf-8")


def run_job(job: dict):
    jid = job["id"]
    t0 = time.time()
    with jobs_lock:
        active_jobs[jid]["t0"] = t0

    def set_state(state, outdir=None, error=None):
        with jobs_lock:
            active_jobs[jid]["state"] = state
            if error:
                active_jobs[jid]["error"] = error
            if outdir:
                active_jobs[jid]["outdir"] = str(outdir)
        if outdir:
            elapsed = int(time.time() - t0) if state in ("done", "error") else None
            write_status(outdir, state, error, elapsed)

    outdir = None
    try:
        source = job["source"]
        if job["kind"] == "url":
            set_state("downloading")
            media_path = download_media(source, OUTPUT_DIR / "_downloads")
        else:
            media_path = Path(source)

        outdir = OUTPUT_DIR / safe_name(media_path.stem)
        if outdir.exists():
            # 재분석: 새 구간 목록과 어긋나는 옛 클립·스타일·메타 제거
            shutil.rmtree(outdir / "clips", ignore_errors=True)
        outdir.mkdir(parents=True, exist_ok=True)
        with jobs_lock:
            active_jobs[jid]["label"] = media_path.stem
        # 원본 위치 기록 — 자르기 단계에서 사용
        (outdir / "source.json").write_text(
            json.dumps({"media": str(media_path.resolve()), "input": source,
                        "duration": round(cutter.media_duration(media_path), 2)},
                       ensure_ascii=False), encoding="utf-8")

        set_state("transcribing", outdir)
        transcript = transcribe(media_path)
        (outdir / "transcript.json").write_text(
            json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
        (outdir / "transcript.txt").write_text(transcript_to_text(transcript), encoding="utf-8")

        set_state("selecting", outdir)
        clips = select_segments(transcript_to_text(transcript),
                                max_sec=cutter.media_duration(media_path))
        (outdir / "shorts.json").write_text(
            json.dumps({"source": source, "clips": clips}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        (outdir / "shorts.md").write_text(clips_to_markdown(clips, source), encoding="utf-8")

        set_state("done", outdir)
        with jobs_lock:
            del active_jobs[jid]  # 완료되면 디스크 목록으로 넘어감
    except Exception as e:  # noqa: BLE001 — 워커는 죽으면 안 됨, 오류는 상태로 보고
        try:
            set_state("error", outdir, error=str(e))
            if outdir:  # 디스크 status가 이어받으므로 목록 이중 표시 방지
                with jobs_lock:
                    active_jobs.pop(jid, None)
        except Exception:  # noqa: BLE001 — 오류 처리 중 IO 실패로 워커가 죽지 않게
            pass


def worker():
    while True:
        run_job(job_queue.get())


def cleanup_interrupted():
    """서버 재시작으로 끊긴 작업이 '진행 중'으로 영영 남지 않게 오류로 표시."""
    if not OUTPUT_DIR.exists():
        return
    for d in OUTPUT_DIR.iterdir():
        status_path = d / "status.json"
        if not d.is_dir() or d.name.startswith("_") or not status_path.exists():
            continue
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            status = {"state": "unknown"}
        if status.get("state") not in ("done", "error"):
            write_status(d, "error", "서버 재시작으로 작업이 중단됨 — 같은 파일/URL로 다시 분석하세요")


# Claude 연결 점검 — 이 서버의 claude 호출은 서버가 도는 컴퓨터의 로그인 계정(구독)을 쓴다
CLAUDE_STATUS = {
    "installed": None,
    "api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),  # 설정 시 크레딧 과금 위험
    "login": "checking",  # checking | ok | fail
    "detail": "",
}


def check_claude():
    if not shutil.which("claude"):
        CLAUDE_STATUS.update(installed=False, login="fail",
                             detail="claude 명령을 찾을 수 없음")
        return
    CLAUDE_STATUS["installed"] = True
    try:  # 로그인 유효성은 실제 호출로만 확인 가능 — 아주 짧은 호출 1회
        r = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input="OK라고 한 단어만 답해.",
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            CLAUDE_STATUS["login"] = "ok"
        else:
            CLAUDE_STATUS.update(login="fail", detail=r.stderr.strip()[-300:])
    except Exception as e:  # noqa: BLE001
        CLAUDE_STATUS.update(login="fail", detail=str(e))


@app.get("/api/health")
def health():
    return CLAUDE_STATUS


BGM_EXTS = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg"}
BANNER_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


@app.get("/api/bgm")
def list_bgm():
    """bgm/ 폴더의 음악 파일 목록."""
    cutter.BGM_DIR.mkdir(exist_ok=True)
    return {"files": sorted(f.name for f in cutter.BGM_DIR.iterdir()
                            if f.suffix.lower() in BGM_EXTS)}


@app.get("/api/banner")
def list_banner():
    """banner/ 폴더의 이미지 목록."""
    cutter.BANNER_DIR.mkdir(exist_ok=True)
    return {"files": sorted(f.name for f in cutter.BANNER_DIR.iterdir()
                            if f.suffix.lower() in BANNER_EXTS)}


cleanup_interrupted()
threading.Thread(target=worker, daemon=True).start()
threading.Thread(target=check_claude, daemon=True).start()


def job_dir(name: str) -> Path:
    d = (OUTPUT_DIR / name).resolve()
    if name.startswith("_") or not d.is_dir() or d.parent != OUTPUT_DIR.resolve():
        raise HTTPException(404, "작업 없음")  # _downloads/_uploads 내부 폴더 접근 차단
    return d


@app.get("/api/jobs")
def list_jobs():
    now = time.time()
    with jobs_lock:
        active = [
            {"id": j["id"], "label": j["label"], "state": j["state"],
             "state_label": STATE_LABELS.get(j["state"], j["state"]), "error": j.get("error"),
             "elapsed": int(now - j["t0"]) if "t0" in j else 0}
            for j in active_jobs.values()
        ]
    jobs = []
    if OUTPUT_DIR.exists():
        for d in sorted(OUTPUT_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir() or d.name.startswith("_"):
                continue
            status = {"state": "done", "error": None}
            if (d / "status.json").exists():
                status = json.loads((d / "status.json").read_text(encoding="utf-8"))
            if status["state"] not in ("done", "error"):
                continue  # 진행 중인 건 active 목록이 담당
            entry = {"name": d.name, "state": status["state"],
                     "state_label": STATE_LABELS.get(status["state"]), "error": status.get("error"),
                     "elapsed": status.get("elapsed"),
                     "duration": source_duration(d),  # 영상 길이 밖 구간 표시용
                     "clips": [], "cuts": {}, "metas": {}}
            if (d / "shorts.json").exists():
                entry["clips"] = json.loads((d / "shorts.json").read_text(encoding="utf-8"))["clips"]
                clips_dir = d / "clips"
                if clips_dir.exists():
                    for f in clips_dir.glob("clip_*.mp4"):
                        idx = f.stem.split("_")[1]
                        entry["cuts"].setdefault(idx, []).append(f"/output/{d.name}/clips/{f.name}")
                    for f in clips_dir.glob("meta_*.json"):
                        idx = f.stem.split("_")[1]
                        entry["metas"][idx] = json.loads(f.read_text(encoding="utf-8"))
            jobs.append(entry)
    return {"active": active, "jobs": jobs}


@app.post("/api/jobs")
async def create_job(file: UploadFile | None = None, url: str = Form("")):
    url = url.strip()
    if file and file.filename:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        jid = uuid.uuid4().hex[:8]
        orig = Path(file.filename)
        # id 접미사 — 같은 이름 파일을 연달아 올려도 진행 중인 원본을 덮어쓰지 않게
        dest = UPLOAD_DIR / (safe_name(orig.stem) + "_" + jid + orig.suffix)
        total = 0
        with dest.open("wb") as f:
            while chunk := await file.read(1 << 20):
                total += len(chunk)
                if total > 5 * 1024 ** 3:
                    f.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(413, "파일이 너무 큽니다 (5GB 제한)")
                f.write(chunk)
        job = {"id": jid, "kind": "file", "source": str(dest),
               "label": orig.stem, "state": "queued"}
    elif is_url(url):
        job = {"id": uuid.uuid4().hex[:8], "kind": "url", "source": url,
               "label": url, "state": "queued"}
    else:
        raise HTTPException(400, "파일 또는 유튜브 URL을 입력하세요")
    with jobs_lock:
        active_jobs[job["id"]] = job
    job_queue.put(job)
    return {"id": job["id"]}


def get_source_media(d: Path) -> Path:
    info = json.loads((d / "source.json").read_text(encoding="utf-8")) \
        if (d / "source.json").exists() else None
    if not info or not Path(info["media"]).exists():
        raise HTTPException(409, "원본 파일을 찾을 수 없음 (CLI로 처리한 옛 작업은 원본 경로 기록이 없습니다)")
    return Path(info["media"])


def source_duration(d: Path) -> float:
    """원본 영상 길이(초). source.json에 캐시 — 매 폴링마다 ffprobe 하지 않게."""
    path = d / "source.json"
    if not path.exists():
        return 0.0
    info = json.loads(path.read_text(encoding="utf-8"))
    if not info.get("duration"):
        media = Path(info.get("media", ""))
        if not media.exists():
            return 0.0
        info["duration"] = round(cutter.media_duration(media), 2)
        path.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return float(info.get("duration") or 0)


def load_clip(d: Path, index: int) -> dict:
    shorts = json.loads((d / "shorts.json").read_text(encoding="utf-8"))
    if not 0 <= index < len(shorts["clips"]):
        raise HTTPException(400, "구간 번호가 잘못됨")
    return shorts["clips"][index]


class TranscriptEdit(BaseModel):
    i: int      # transcript.json segments 전체 기준 인덱스
    text: str


class CutRequest(BaseModel):
    index: int                       # clips 배열 기준 0부터
    subtitles: bool = True
    style: dict | None = None        # 편집기 스타일. 있으면 저장 후 사용
    edits: list[TranscriptEdit] = [] # 자막 오타 수정 (전사본에 반영)


@app.post("/api/jobs/{name}/cut")
def cut_clip(name: str, req: CutRequest):
    d = job_dir(name)
    clip = load_clip(d, req.index)
    if not float(clip["end_sec"]) > float(clip["start_sec"]):
        raise HTTPException(400, "구간 시각이 잘못됨 (끝이 시작보다 앞)")
    dur = source_duration(d)
    if dur and float(clip["start_sec"]) >= dur:
        raise HTTPException(
            400, f"이 구간은 영상 길이({int(dur // 60):02d}:{int(dur % 60):02d}) 밖입니다 "
                 "— AI가 없는 시각을 만들어낸 구간이라 생성할 수 없습니다")
    media = get_source_media(d)

    transcript = json.loads((d / "transcript.json").read_text(encoding="utf-8"))
    if req.edits:  # 오타 수정은 전사본 원본에 반영 — 다른 구간에도 적용됨
        from shorts_marker import transcript_to_text
        for e in req.edits:
            if 0 <= e.i < len(transcript["segments"]):
                transcript["segments"][e.i]["text"] = e.text.strip()
        (d / "transcript.json").write_text(
            json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
        (d / "transcript.txt").write_text(transcript_to_text(transcript), encoding="utf-8")

    if req.style is not None:  # 편집기 경로: 스타일 저장 + 적용
        style = cutter.merge_style(req.style)
        (d / "clips").mkdir(exist_ok=True)
        (d / "clips" / f"style_{req.index + 1}.json").write_text(
            json.dumps(style, ensure_ascii=False, indent=2), encoding="utf-8")
        segments = transcript["segments"] if style["subs"]["enabled"] else None
        out_path = d / "clips" / f"clip_{req.index + 1}_edit.mp4"
        cutter.make_short(media, clip["start_sec"], clip["end_sec"], out_path,
                          segments, style)
    else:  # 목록의 빠른 생성 버튼 경로
        segments = transcript["segments"] if req.subtitles else None
        suffix = "_sub" if req.subtitles else ""
        out_path = d / "clips" / f"clip_{req.index + 1}{suffix}.mp4"
        cutter.make_short(media, clip["start_sec"], clip["end_sec"], out_path, segments)
    return {"file": f"/output/{name}/clips/{out_path.name}"}


@app.delete("/api/jobs/{name}")
def delete_job(name: str):
    """작업 폴더 전체 삭제. 다운로드/업로드된 원본도 output 안에 있으면 함께 삭제."""
    d = job_dir(name)
    if (d / "source.json").exists():
        info = json.loads((d / "source.json").read_text(encoding="utf-8"))
        media = Path(info.get("media", ""))
        # output/_downloads, output/_uploads 안의 파일만 삭제 — 사용자 원본(temp/ 등)은 안 건드림
        if media.exists() and media.resolve().parent.parent == OUTPUT_DIR.resolve():
            media.unlink(missing_ok=True)
    shutil.rmtree(d)
    return {"ok": True}


class MetaRequest(BaseModel):
    index: int


@app.post("/api/jobs/{name}/meta")
def make_meta(name: str, req: MetaRequest):
    """구간별 업로드 정보(제목 3안·설명·태그) 생성. claude 호출이라 수십 초 걸림."""
    d = job_dir(name)
    clip = load_clip(d, req.index)
    segments = json.loads((d / "transcript.json").read_text(encoding="utf-8"))["segments"]
    script = "\n".join(s["text"] for s in segments
                       if s["end"] > clip["start_sec"] and s["start"] < clip["end_sec"])
    meta = generate_metadata(clip, script)
    (d / "clips").mkdir(exist_ok=True)
    (d / "clips" / f"meta_{req.index + 1}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


@app.get("/api/jobs/{name}/editor/{index}")
def editor_data(name: str, index: int):
    """자막 편집기 초기 데이터: 미리보기 미디어 URL, 구간 자막, 저장된 스타일."""
    d = job_dir(name)
    clip = load_clip(d, index)
    media = get_source_media(d)

    # 미리보기용: 원본을 작업 폴더에 심볼릭 링크 → /output 정적 서빙(구간 탐색 지원)
    link = d / f"source{media.suffix}"
    if not link.exists():
        try:
            os.symlink(media.resolve(), link)
        except OSError:
            shutil.copy2(media, link)

    segments = json.loads((d / "transcript.json").read_text(encoding="utf-8"))["segments"]
    # 실제로 구워지는 자막만 목록에 — 음악·박수 라벨은 렌더러에서 빠지므로 여기서도 제외
    usable = {id(x) for x in cutter.clean_segments(segments)}
    segs = [{"i": i, "start": s["start"], "end": s["end"], "text": s["text"]}
            for i, s in enumerate(segments)
            if id(s) in usable and s["end"] > clip["start_sec"] and s["start"] < clip["end_sec"]]

    style_path = d / "clips" / f"style_{index + 1}.json"
    if style_path.exists():
        # merge_style: 예전에 저장된 스타일에 새로 생긴 요소(cta 등) 기본값 채움
        style = cutter.merge_style(json.loads(style_path.read_text(encoding="utf-8")))
    else:
        style = cutter.merge_style(None)
        style["title"]["enabled"] = True
        style["title"]["text"] = clip.get("title", "")
    return {"clip": clip, "media_url": f"/output/{name}/{link.name}",
            "segments": segs, "style": style, "duration": source_duration(d)}


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


OUTPUT_DIR.mkdir(exist_ok=True)
cutter.BANNER_DIR.mkdir(exist_ok=True)
app.mount("/banner", StaticFiles(directory=cutter.BANNER_DIR), name="banner")
app.mount("/output", StaticFiles(directory=OUTPUT_DIR, follow_symlink=True), name="output")
