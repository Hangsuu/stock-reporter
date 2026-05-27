# stock-reporter

Claude Code CLI(`claude -p` 헤드리스 모드)로 미국/한국 증시 리포트를 생성해 텔레그램으로 보내는 자동화 봇.
시세·재무 데이터를 파이썬으로 수집하고, 분석/코멘트 생성은 로컬에 로그인된 Claude Code 세션에 위임한다.

## 동작 개요

```
launchd (스케줄)
  └─ scripts/run.sh <job>
       └─ ./venv/bin/python -m src.reporter <job>
            ├─ src/data_*.py      시세·재무·게시판 수집 (yfinance, FDR, 네이버, OpenDART)
            ├─ src/analyst.py     `claude -p --model <CLAUDE_MODEL>` 호출로 분석문 생성
            └─ src/notifier.py    텔레그램 전송
```

분석 품질은 **이 저장소 안의 프롬프트(`src/analyst.py`)** + **로컬 Claude Code 세션/모델**의 조합으로 결정된다.
프롬프트는 git으로 따라오지만, Claude Code 로그인 세션·모델 버전은 머신마다 다르므로 아래 셋업을 동일하게 맞춰야 같은 수준이 나온다.

## 사전 요구사항

- **Python 3.13** (venv가 3.13 기준으로 빌드됨)
- **Node.js + Claude Code CLI** — `claude` 명령이 PATH에 있어야 하고 `claude login` 완료 상태여야 함
  - `ANTHROPIC_API_KEY`는 불필요(로그인 세션 사용)
- **macOS** — 스케줄링에 launchd 사용 (다른 OS면 cron 등으로 대체 필요, 아래 "이식성" 참고)

## 셋업

```bash
# 1) 클론
git clone <repo-url> stock-reporter && cd stock-reporter

# 2) Claude Code 설치 후 로그인 (미설치 시)
#    설치는 https://docs.claude.com/claude-code 참고
claude login
claude --version          # 동작 확인

# 3) 환경변수
cp .env.example .env
#    .env 편집: 최소 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 입력
#    CLAUDE_MODEL은 별칭(opus) 대신 풀네임(claude-opus-4-7) 권장 — 아래 "재현성" 참고

# 4) 가상환경 (반드시 Python 3.13)
python3.13 -m venv venv
./venv/bin/pip install -r requirements.txt

# 5) 단발 테스트 (텔레그램 전송 없이 콘솔 출력만)
./venv/bin/python -m src.reporter us --dry-run --force
```

## 잡(job) 종류

`python -m src.reporter <job> [--dry-run] [--force] [--ticker 종목코드] [--note "추가 관점"]`

| job | 설명 |
|-----|------|
| `us`, `us_top20` | 미국 증시 리포트 / 시총 상위 |
| `kr`, `kr_top20` | 한국 증시 리포트 / 시총 상위 |
| `kr_deepdive` | 한 종목 심층 분석 (`--ticker`로 강제 지정 가능) |
| `kr_quarterly`, `kr_board` | 분기 실적 / 게시판 요약 |
| `insight` | 인사이트 |
| `chart_lesson` | 차트 강의 |
| `macro`, `macro_daily` | 매크로 (주간/일간) |

`--dry-run` 콘솔만 출력, `--force` 주말에도 실행.

## 스케줄링 (launchd, macOS)

```bash
./scripts/install_launchd.sh     # plist 템플릿을 현재 머신 경로로 렌더링 후 등록
./scripts/uninstall_launchd.sh   # 등록 해제
```

기본 등록 잡: `us us_top20 kr kr_top20 kr_deepdive insight chart_lesson bot`.
추가/제거는 `scripts/install_launchd.sh` 상단의 `for label in ...` 목록을 편집.
(`macro*`, `monitor_*` 템플릿도 존재하지만 기본 스케줄에는 빠져 있음.)

## ⚠️ 절대경로 처리 (중요)

macOS launchd는 plist 내부의 `$HOME`/환경변수를 **확장하지 않는다.** 따라서 plist에는 절대경로가 필요한데,
이를 git에 박아두면 다른 사용자/머신에서 깨진다. 그래서:

- `launchd/*.plist`는 **placeholder 템플릿**이다. 다음 두 토큰을 포함한다:
  - `__PROJECT_DIR__`  → 이 저장소의 절대경로
  - `__NODE_BIN_DIR__` → `claude` CLI가 들어있는 디렉터리(PATH에 추가됨)
- **`install_launchd.sh`가 설치 시점에 실제 경로를 주입**한다. 절대경로는 git이 아니라 *설치하는 머신의 환경*에서 결정된다:
  - `__PROJECT_DIR__` — 저장소 위치에서 자동 계산 (`cd $(dirname $0)/..`)
  - `__NODE_BIN_DIR__` — `.env`의 `NODE_BIN_DIR`로 지정, 없으면 `which claude`로 자동 탐지
- 따라서 **plist 원본(`launchd/*.plist`)을 직접 `cp`로 설치하면 안 된다.** 반드시 `install_launchd.sh`를 거칠 것.
  (placeholder가 그대로 남아 launchd가 실행에 실패한다.)

`claude` 자동 탐지가 실패하거나 특정 node 설치를 강제하려면 `.env`에 지정:

```bash
NODE_BIN_DIR=/Users/you/.nvm/versions/node/v20.19.6/bin
```

## 재현성 / "어느 환경에서나 같은 수준" 체크리스트

- **Claude Code 로그인 필수** — 세션은 머신별 상태라 git으로 오지 않는다.
- **모델은 풀네임 고정 권장** — `.env`의 `CLAUDE_MODEL=opus`(별칭)는 머신의 Claude Code 버전이 resolve하는 모델에 따라 달라진다. 동일 출력이 중요하면 `CLAUDE_MODEL=claude-opus-4-7`처럼 풀네임을 쓴다.
- **Python 3.13 + `requirements.txt`** — venv 폴더는 git에 없고(플랫폼 의존 바이너리), 재설치로 복원한다. 단 핀이 `>=`라 동일 버전 보장은 아니다. 완전 고정이 필요하면 `pip freeze > requirements.lock` 사용.

## git에 올라가지 않는 것 (`.gitignore`)

`.env`(시크릿), `venv/`, `.claude/settings.local.json`(머신 고유 경로), `docs_cache/`, `logs/`, `__pycache__/`.
새 환경에서는 위 셋업 단계로 `.env`와 `venv`를 다시 만든다.
