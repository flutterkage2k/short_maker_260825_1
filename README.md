# 숏츠 마커 (shorts-marker)

롱폼 영상(파일 또는 유튜브 URL)을 넣으면:

1. 음성을 인식해 타임스탬프 전사본을 만들고
2. AI가 숏츠로 쓸 구간을 골라 이유와 함께 보여주고
3. 고른 구간을 세로(1080x1920) 숏츠 mp4로 잘라주는 도구.

자막·상단 후킹 타이틀·마지막 CTA는 웹 편집기(WYSIWYG)에서 드래그로 위치를 잡고
색·크기를 조절해 그대로 영상에 굽는다. 업로드용 제목·설명·태그도 AI가 생성한다.

## 요구사항

- macOS + Apple Silicon (음성 인식이 mlx-whisper 사용 — 애플 실리콘 전용)
- ffmpeg (`brew install ffmpeg`)
- Claude Code CLI 로그인 상태 (`claude` 명령) — 구간 선정·메타데이터 생성에 사용.
  API 키 불필요, 구독으로 처리됨.
- Python 3.13+

## 설치

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

첫 실행 때 Whisper 모델(약 1.6GB)을 자동 다운로드한다(한 번만).

## 웹UI 실행 (권장)

```bash
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8123
```

브라우저에서 `http://localhost:8123` 접속.

### 사용 흐름

1. **영상 파일 선택** 또는 **유튜브 URL** 입력 → **분석 시작**
2. 작업 목록에서 진행 상태 확인 (다운로드 → 음성 인식 → 구간 선정 → 완료)
3. 완료된 작업 클릭 → 추천 구간 목록 (제목·시각·훅 점수·선정 이유)
4. 구간마다:
   - **숏츠 생성** — 기본 스타일로 바로 mp4 생성 (자막 넣기 체크 여부 선택)
   - **자막 편집** — WYSIWYG 편집기:
     - 레이아웃 템플릿 4종 (타이틀+자막 / 자막만 / 영상 위 자막 / 타이틀만)
     - 상단 타이틀(후킹 멘트)·하단 자막·마지막 CTA: 드래그로 위치, 크기·색·테두리 조절
     - CTA는 마지막 N초에만 표시 (루프 안 깨는 방식)
     - 자막 오인식 교정 — 수정하면 전사본 원본에 반영
   - **업로드 정보 생성** — 제목 3안·설명(해시태그 포함)·태그를 AI가 생성, 복사 버튼 제공
5. 생성된 mp4는 카드에서 바로 재생·다운로드

## CLI 실행 (분석만)

```bash
.venv/bin/python shorts_marker.py <영상파일 경로 | 유튜브 URL>
```

## 결과 파일 (`output/<영상이름>/`)

| 파일 | 내용 |
| --- | --- |
| `transcript.txt` | `[03:24] 발언` 형식 전사본 |
| `transcript.json` | 초 단위 세그먼트 원본 데이터 |
| `shorts.md` / `shorts.json` | 추천 구간 목록 (시각·제목·이유·훅 점수) |
| `source.json` | 원본 미디어 경로 기록 |
| `clips/clip_N*.mp4` | 생성된 숏츠 영상 |
| `clips/style_N.json` | 편집기에서 저장한 스타일 |
| `clips/meta_N.json` | 업로드 정보 (제목·설명·태그) |

## 구조

- `shorts_marker.py` — 파이프라인 (다운로드·전사·구간 선정·메타데이터). CLI 겸용
- `cutter.py` — ffmpeg 자르기 + 세로 변환 + 텍스트 오버레이(Pillow로 렌더링)
- `app.py` — FastAPI 서버 (작업 대기열·자르기·편집기·메타데이터 API)
- `static/index.html` — 대시보드 + WYSIWYG 편집기

참고: 자막을 Pillow PNG + ffmpeg overlay로 굽는 이유는 홈브루 ffmpeg가
libass/drawtext 없이 빌드되기 때문. 별도 ffmpeg 재빌드 불필요.

## 내부 서버(NAS 등) 이전 시

- `transcribe()`(mlx-whisper)를 faster-whisper로 교체 필요 — 애플 실리콘 전용이라
- `ask_claude_json()`이 claude CLI를 호출하므로, 서버에도 claude CLI 설치+로그인
  하거나 이 함수만 Anthropic API 호출로 교체
- 폰트 경로(`cutter.FONT_PATH`)가 macOS 시스템 폰트라 리눅스에선 나눔고딕 등으로 변경
