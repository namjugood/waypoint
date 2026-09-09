"""Waypoint 훅 스크립트들이 공유하는 공통 유틸리티.

이 파일은 표준 라이브러리만 사용한다 (외부 의존성 설치 불필요).
어떤 함수도 예외를 밖으로 던지지 않는 걸 원칙으로 한다 — 훅 스크립트가
죽으면 사용자의 실제 Claude Code 세션에 영향을 줄 수 있기 때문이다.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# .claude/scripts/lib_common.py -> 프로젝트 루트
PROJECT_ROOT = Path(__file__).resolve().parents[2]
WAYPOINTS_DIR = PROJECT_ROOT / "waypoints"
TMP_DIR = WAYPOINTS_DIR / ".tmp"  # 세션 진행 오프셋(state.json)만 로컬에 둔다 — git에 안 올라감
INPROGRESS_DIR = WAYPOINTS_DIR / "inprogress"  # 진행 중 세션의 원문/구조화 임시본. git-tracked.
TAGS_DIR = WAYPOINTS_DIR / "tags"  # 분류 완료된 요약본
RAW_DIR = WAYPOINTS_DIR / "raw"  # 분류 완료 후에도 안 지우고 보관하는 원문 전체 (요약의 손실 압축 대비)
INDEX_FILE = WAYPOINTS_DIR / "INDEX.md"
DEBUG_LOG = WAYPOINTS_DIR / ".debug.log"

STATUS_LABEL = {"in_progress": "진행중", "done": "완료", "paused": "보류"}

# 원문이 이만큼(문자 수) 쌓일 때마다 체크포인트를 돈다 — 세션 전체를 한 번에
# 분류하지 않고 조금씩 나눠서 처리하기 위한 임계값. 너무 작으면 체크포인트
# (AI 호출)가 잦아져서 느려지고, 너무 크면 한 체크포인트가 못 담는 내용이
# 늘어난다. 4000자 안팎이면 왕복 하나로 처리하기 적당한 크기다.
CHECKPOINT_THRESHOLD_CHARS = 4000

# 원격 웹 세션은 Archive를 눌러도 SessionEnd가 컨테이너 회수 전에 반드시
# 끝난다는 보장이 없다. 그래서 원문은 로컬에만 쌓아두지 않고 Stop 훅이
# 매 턴 이 디렉터리에 커밋+푸시한다 (git에 이미 올라간 내용은 컨테이너가
# 죽어도 사라지지 않는다). SessionEnd는 이걸 읽어 분류/정리만 담당한다.


def log_debug(msg: str) -> None:
    """실패해도 무시한다. 디버그 로그 남기기 자체가 훅을 죽이면 안 된다."""
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass


def in_headless_recursion_guard() -> bool:
    """call_claude_headless()가 띄운 하위 claude 프로세스 안에서 훅이 다시
    트리거된 상황인지 확인. true면 각 훅 스크립트는 즉시 종료해야 한다."""
    return os.environ.get("WAYPOINT_HEADLESS_CALL") == "1"


def read_hook_input() -> dict:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        return json.loads(raw)
    except Exception as e:
        log_debug(f"read_hook_input 실패: {e}")
        return {}


def slugify(text: str, maxlen: int = 40) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^\w\s가-힣-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text)
    text = text.strip("-")
    return text[:maxlen] or "untitled"


def detect_project_name(cwd: str) -> str:
    """git remote 이름을 우선 쓰고, 없으면 디렉토리 이름으로 대체."""
    cwd_path = Path(cwd) if cwd else PROJECT_ROOT
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd_path), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            url = result.stdout.strip()
            name = url.rstrip("/").split("/")[-1]
            name = re.sub(r"\.git$", "", name)
            if name:
                return slugify(name)
    except Exception as e:
        log_debug(f"detect_project_name git 조회 실패: {e}")
    return slugify(cwd_path.name)


def inprogress_md_path(project: str, session_id: str) -> Path:
    return INPROGRESS_DIR / project / f"{session_id}.md"


def structured_inprogress_path(project: str, session_id: str) -> Path:
    """체크포인트마다 해시태그 섹션으로 누적되는, 진행 중 세션의 '구조화된'
    임시본. 원문(inprogress_md_path)과 별개로 관리되고, 세션 종료 시 이
    내용이 최종 tags/ 문서의 본문이 된다."""
    return INPROGRESS_DIR / project / f"{session_id}.structured.md"


def tags_md_path(project: str, primary_tag: str, file_ts: str, file_slug: str) -> Path:
    return TAGS_DIR / project / primary_tag / f"{file_ts}-{file_slug}.md"


def raw_md_path(project: str, primary_tag: str, file_ts: str, file_slug: str) -> Path:
    """tags_md_path와 같은 <file_ts>-<file_slug>로 짝지어지는 원문 보관
    위치. 분류 후에도 지우지 않는다 — 요약이 손실 압축이라 놓치는 뉘앙스가
    있을 수 있어서, 필요할 때 원문을 다시 열어볼 수 있게 남겨둔다."""
    return RAW_DIR / project / primary_tag / f"{file_ts}-{file_slug}.md"


def state_path(session_id: str) -> Path:
    return TMP_DIR / f"{session_id}.state.json"


def extract_known_tags(index_text: str, limit: int = 40) -> list:
    """INDEX.md에 이미 쓰인 대표 태그(### 헤더)와 해시태그(- #태그)를 뽑아낸다.
    분류 프롬프트에 넘겨서 "새 태그를 짓기 전에 기존 걸 재사용"하도록
    유도하는 데 쓴다 — 안 그러면 세션마다 hooks/hook/Hooks처럼 같은 개념이
    다른 표기로 흩어진다."""
    tags = []
    for line in index_text.splitlines():
        s = line.strip()
        m = re.match(r"^### (.+?) \(", s)
        if m:
            tags.append(m.group(1).strip())
            continue
        m2 = re.match(r"^- #(\S+)", s)
        if m2:
            tags.append(m2.group(1).strip())
    seen = set()
    out = []
    for t in tags:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:limit]


def git_last_commit_epoch(rel_path) -> int:
    """rel_path(PROJECT_ROOT 기준 상대경로)의 마지막 커밋 시각(unix epoch).
    커밋 이력이 없으면 None. 로컬 mtime과 달리 컨테이너가 새로 clone돼도
    유효하다 — "이 파일이 최근에 실제로 활동 중인 세션에서 갱신됐는지"를
    파일시스템이 아니라 git 이력으로 판단하기 위함."""
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "log", "-1", "--format=%ct", "--", str(rel_path)],
            capture_output=True, text=True, timeout=10,
        )
        out = result.stdout.strip()
        if result.returncode == 0 and out:
            return int(out)
    except Exception as e:
        log_debug(f"git_last_commit_epoch 실패({rel_path}): {e}")
    return None


def git_commit_and_push(add_paths: list, message: str, use_add_all: bool = False) -> bool:
    """지정된 경로(들)만 스테이징해서 커밋+푸시. 다른 미관련 변경사항은
    건드리지 않는다. 실패해도 예외를 던지지 않고 False를 반환한다
    (훅이 죽으면 안 되므로). use_add_all=True면 삭제도 확실히 잡도록
    `git add -A <path>`를 쓴다 (경로 아래 파일이 삭제된 경우 등)."""
    try:
        add_cmd = ["git", "-C", str(PROJECT_ROOT), "add"]
        if use_add_all:
            add_cmd.append("-A")
        add_cmd += ["--"] + list(add_paths)
        subprocess.run(add_cmd, capture_output=True, text=True, timeout=15)

        status = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "diff", "--cached", "--quiet"],
            capture_output=True, timeout=10,
        )
        if status.returncode == 0:
            return True  # 커밋할 변경 없음 — 정상

        commit = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "-c", "user.email=noreply@anthropic.com",
             "-c", "user.name=Claude", "commit", "-m", message],
            capture_output=True, text=True, timeout=15,
        )
        if commit.returncode != 0:
            log_debug(f"git commit 실패: {commit.stderr[:300]}")
            return False

        # -u origin HEAD: 현재 브랜치가 아직 원격 추적 브랜치가 없어도
        # (예: 이번 세션에서 아직 한 번도 push 안 된 새 브랜치) 실패하지
        # 않는다 — 같은 이름으로 원격에 만들고 추적을 설정한다. 이미
        # upstream이 있으면 그냥 거기로 push하는 것과 동일하게 동작한다.
        push = subprocess.run(["git", "-C", str(PROJECT_ROOT), "push", "-u", "origin", "HEAD"],
                               capture_output=True, text=True, timeout=60)
        if push.returncode != 0:
            log_debug(f"git push 실패, pull --rebase 후 재시도: {push.stderr[:300]}")
            subprocess.run(["git", "-C", str(PROJECT_ROOT), "pull", "--rebase"],
                            capture_output=True, text=True, timeout=60)
            retry = subprocess.run(["git", "-C", str(PROJECT_ROOT), "push", "-u", "origin", "HEAD"],
                                    capture_output=True, text=True, timeout=60)
            if retry.returncode != 0:
                log_debug(f"git push 재시도도 실패(로컬 커밋은 유지됨): {retry.stderr[:300]}")
                return False
        return True
    except Exception as e:
        log_debug(f"git_commit_and_push 예외({add_paths}): {e}")
        return False


def detect_default_branch() -> str:
    """origin의 실제 기본 브랜치 이름(master/main 등)을 알아낸다.
    하드코딩하지 않는 이유: 이 도구는 다른 프로젝트에도 설치될 수 있고,
    그 프로젝트의 기본 브랜치명이 다를 수 있기 때문."""
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "ls-remote", "--symref", "origin", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("ref:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return parts[1].rsplit("/", 1)[-1]
    except Exception as e:
        log_debug(f"detect_default_branch 실패: {e}")
    return None


def sync_waypoints_to_master(prepare_fn, commit_message: str, max_retries: int = 5) -> bool:
    """세션 전용 브랜치가 아니라 저장소 **기본 브랜치**에 waypoints/ 변경만
    직접 커밋+push한다. 실제 코드 변경(현재 체크아웃된 브랜치의 다른
    커밋들)은 절대 건드리지 않는다 — 별도의 격리된 git worktree에서
    origin/<기본브랜치> 기준으로만 작업하기 때문이다.

    prepare_fn(root: Path)은 root 아래(주로 root/waypoints/...)의 파일들을
    원하는 최종 상태로 만드는 콜백이다. **중요**: push가 다른 세션과
    충돌해서(non-fast-forward) 재시도할 때마다, worktree를 최신
    origin/<기본브랜치>로 리셋한 뒤 prepare_fn을 다시 호출한다 — 로컬에
    미리 계산해둔 스냅샷을 그대로 덮어쓰면, INDEX.md처럼 여러 세션이 동시에
    건드리는 공유 파일에서 그 사이 다른 세션이 추가한 내용을 지워버리게
    된다. prepare_fn은 그래서 "그 시점의 최신 내용 위에 내 변경분을
    다시 적용"하는 방식으로 짜야 한다 (예: update_index_tree()는 항상 그
    호출 시점의 INDEX.md 내용을 읽어서 한 줄을 삽입하므로, 매번 새로
    fetch된 최신 위에서 실행되면 자연히 안전하게 병합된다).

    현재 브랜치가 이미 기본 브랜치라면 격리할 이유가 없어 그 자리에서
    바로 커밋+push한다. worktree 생성이나 기본 브랜치 감지 자체가
    실패하면 False를 반환한다 — 호출자는 (구식이지만 확실한) 현재
    브랜치 직접 커밋으로 폴백해야 한다. 데이터 유실 방지가 최우선이므로."""
    default_branch = detect_default_branch()
    if not default_branch:
        return False

    try:
        current = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "branch", "--show-current"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception:
        current = None

    if current == default_branch:
        prepare_fn(PROJECT_ROOT)
        return git_commit_and_push(["waypoints"], commit_message, use_add_all=True)

    tmp_dir = None
    try:
        subprocess.run(["git", "-C", str(PROJECT_ROOT), "fetch", "origin", default_branch],
                        capture_output=True, text=True, timeout=30)

        tmp_dir = tempfile.mkdtemp(prefix="waypoint-master-sync-")
        wt = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "worktree", "add", "-q", "--detach",
             tmp_dir, f"origin/{default_branch}"],
            capture_output=True, text=True, timeout=30,
        )
        if wt.returncode != 0:
            log_debug(f"worktree add 실패: {wt.stderr[:300]}")
            return False

        tmp_path = Path(tmp_dir)
        pushed = False
        for attempt in range(max_retries):
            prepare_fn(tmp_path)
            subprocess.run(["git", "-C", tmp_dir, "add", "-A", "--", "waypoints"],
                            capture_output=True, text=True, timeout=15)
            status = subprocess.run(["git", "-C", tmp_dir, "diff", "--cached", "--quiet"],
                                     capture_output=True, timeout=10)
            if status.returncode == 0:
                pushed = True  # 커밋할 변경 없음(이미 최신) — 정상
                break
            commit = subprocess.run(
                ["git", "-C", tmp_dir, "-c", "user.email=noreply@anthropic.com",
                 "-c", "user.name=Claude", "commit", "-m", commit_message],
                capture_output=True, text=True, timeout=15,
            )
            if commit.returncode != 0:
                log_debug(f"worktree commit 실패: {commit.stderr[:300]}")
                break
            push = subprocess.run(
                ["git", "-C", tmp_dir, "push", "origin", f"HEAD:refs/heads/{default_branch}"],
                capture_output=True, text=True, timeout=60,
            )
            if push.returncode == 0:
                pushed = True
                break
            log_debug(f"{default_branch} push 충돌(시도 {attempt + 1}/{max_retries}), "
                      f"최신으로 재동기화 후 재계산: {push.stderr[:200]}")
            subprocess.run(["git", "-C", tmp_dir, "fetch", "origin", default_branch],
                            capture_output=True, text=True, timeout=30)
            subprocess.run(["git", "-C", tmp_dir, "reset", "--hard", f"origin/{default_branch}"],
                            capture_output=True, text=True, timeout=15)

        if not pushed:
            return False

        # 현재 세션 브랜치의 워킹트리도 방금 push된 최종 상태로 맞춘다.
        # 이 내용은 이 브랜치로도 커밋+push한다 — 데이터는 이미 기본
        # 브랜치에 있으니 중복이긴 하지만, 세션 브랜치를 항상 push된
        # 상태로 유지해야(로컬 전용 커밋을 남겨두지 않아야) 다른 안전망
        # 훅(unpushed commit 감지 등)과 충돌하지 않는다. 내용이 같아서
        # 나중에 이 브랜치가 PR로 머지돼도 무해하게 합쳐진다.
        for name in ("tags", "inprogress", "raw"):
            local_d = WAYPOINTS_DIR / name
            src_d = tmp_path / "waypoints" / name
            shutil.rmtree(local_d, ignore_errors=True)
            if src_d.exists():
                shutil.copytree(src_d, local_d)
        src_index = tmp_path / "waypoints" / "INDEX.md"
        if src_index.exists():
            shutil.copy2(src_index, INDEX_FILE)

        git_commit_and_push(
            ["waypoints"],
            f"{commit_message} (mirror; 이미 {default_branch}에 push됨)",
            use_add_all=True,
        )
        return True
    except Exception as e:
        log_debug(f"sync_waypoints_to_master 예외: {e}")
        return False
    finally:
        if tmp_dir:
            subprocess.run(["git", "-C", str(PROJECT_ROOT), "worktree", "remove", "--force", tmp_dir],
                            capture_output=True, text=True, timeout=15)


def load_state(session_id: str) -> dict:
    p = state_path(session_id)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            log_debug(f"load_state 파싱 실패({session_id}): {e}")
    return {"last_line": 0, "project": None}


def save_state(session_id: str, state: dict) -> None:
    try:
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        state_path(session_id).write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        log_debug(f"save_state 실패({session_id}): {e}")


def read_transcript_entries(transcript_path: str) -> list:
    entries = []
    if not transcript_path:
        return entries
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
    except Exception as e:
        log_debug(f"read_transcript_entries 실패({transcript_path}): {e}")
    return entries


# 산출물로 취급할 확장자 — 일반 코드 편집까지 매번 기록하면 노이즈가 되므로
# "문서/보고서성 산출물"만 좁혀서 잡는다.
DELIVERABLE_EXTS = {".html", ".htm", ".pdf", ".docx", ".pptx", ".xlsx", ".md"}


def extract_text_and_files(message_content):
    """assistant/user 메시지 content(str 또는 block list)에서
    (표시용 텍스트, 산출물로 보이는 파일 경로 목록)을 뽑아낸다."""
    if isinstance(message_content, str):
        return message_content, []

    texts = []
    files = []
    if isinstance(message_content, list):
        for block in message_content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                t = block.get("text", "")
                if t:
                    texts.append(t)
            elif btype == "tool_use":
                name = block.get("name", "")
                inp = block.get("input", {}) or {}
                fp = inp.get("file_path") or inp.get("path")
                if name in ("Write", "Edit", "NotebookEdit") and fp:
                    if Path(fp).suffix.lower() in DELIVERABLE_EXTS:
                        files.append(fp)
    return "\n".join(texts).strip(), files


def call_claude_headless(prompt: str, timeout: int = 45) -> str:
    """헤드리스로 claude -p 를 호출해서 stdout 텍스트를 반환.
    실패하면 빈 문자열을 반환한다 (예외를 던지지 않음).

    중요: cwd를 프로젝트 디렉토리 밖(임시 폴더)으로 지정한다. 그렇지 않으면
    이 프로젝트의 .claude/settings.json 훅을 이 하위 프로세스도 그대로
    읽어버려서, SessionStart/Stop 훅이 재귀적으로 다시 이 함수를 호출하는
    무한루프에 빠진다 (실제로 겪은 버그). 추가 안전장치로 환경변수 마커도
    심어서, 혹시 다른 경로로 훅이 트리거되더라도 즉시 빠져나가게 한다."""
    import tempfile
    env = dict(os.environ)
    env["WAYPOINT_HEADLESS_CALL"] = "1"
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=timeout,
            cwd=tempfile.gettempdir(), env=env,
        )
        if result.returncode != 0:
            log_debug(f"call_claude_headless 실패 rc={result.returncode}: {result.stderr[:500]}")
            return ""
        return result.stdout.strip()
    except Exception as e:
        log_debug(f"call_claude_headless 예외: {e}")
        return ""


def extract_first_json_object(text: str):
    """응답 텍스트에서 첫 번째 {...} JSON 객체를 찾아 파싱. 실패 시 None."""
    if not text:
        return None
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start:i + 1]
                try:
                    return json.loads(candidate)
                except Exception:
                    return None
    return None


CHECKPOINT_INSTRUCTIONS = """\
아래는 Claude Code와 나눈 대화의 최근 일부다. 지금까지 이어지던 주제와
비교해서, 이 부분이 같은 주제의 연장인지 새로운 주제로 넘어갔는지
판단하고, 이 부분만 간결하게 요약해줘.

지금까지 이어지던 주제: {prev_hashtag_desc}
이미 이 프로젝트에서 쓰인 태그들 (가능하면 재사용, 정말 새 개념일 때만 새로 만들어): {known_tags}

반드시 아래 JSON 형식으로만, 다른 말 없이 응답해:
{{
  "same_topic": true 또는 false,
  "hashtag": "영문-kebab-case 태그 (same_topic이 true면 지금까지와 같은 태그를 그대로 반환)",
  "chunk_title": "이 부분 내용 8~15자 내외 제목",
  "chunk_summary_md": "이 부분만 간결하게 정리한 마크다운 요약 (불렛 가능, 핵심 결정/이유 위주)",
  "status": "in_progress 또는 done 또는 paused 중, 지금 시점 기준 이 작업의 상태",
  "has_unexplored_alt": true 또는 false,
  "unexplored_summary": "has_unexplored_alt가 true면 제안됐지만 선택 안 된 접근법을 1~2문장으로. 아니면 빈 문자열"
}}

status 판단 기준:
- done: 이 부분에서 다룬 작업이 명확히 완료됨
- paused: 진행 중이었는데 애매하게 끝남 (미해결 질문, 다음 단계가 남음)
- in_progress: 계속 이어지고 있는 성격의 작업

대화 내용:
---
{content}
---
"""


def classify_checkpoint(prev_hashtag: str, known_tags: list, pending_text: str,
                         timeout: int = 45) -> dict:
    """긴 세션 전체를 한 번에 분류하는 대신, 그때그때 쌓인 조각(pending_text)
    하나만 분류한다 — 그래서 세션이 아무리 길어져도 한 번의 호출이 봐야
    하는 입력 크기는 항상 이 조각 하나로 유지된다 (맵-리듀스의 map 단계).
    실패하면 예외를 던지지 않고, 새 주제("미분류")로 폴백해서 원문을
    그대로 보존한다 (데이터 유실 방지가 최우선)."""
    prev_desc = f"'{prev_hashtag}' 태그로 진행 중이던 작업" if prev_hashtag else "(아직 없음 — 이번이 첫 조각)"
    known = ", ".join(known_tags) if known_tags else "(아직 없음)"
    prompt = CHECKPOINT_INSTRUCTIONS.format(
        prev_hashtag_desc=prev_desc, known_tags=known, content=pending_text[:CHECKPOINT_THRESHOLD_CHARS * 2],
    )
    raw = call_claude_headless(prompt, timeout=timeout)
    result = extract_first_json_object(raw)
    if not result or not result.get("hashtag"):
        log_debug("classify_checkpoint 실패, 미분류 조각으로 폴백")
        first_line = next((l.strip() for l in pending_text.splitlines() if l.strip()), "미분류 내용")
        return {
            "same_topic": False,
            "hashtag": "미분류",
            "chunk_title": first_line[:20] or "미분류 내용",
            "chunk_summary_md": pending_text,
            "status": "in_progress",
            "has_unexplored_alt": False,
            "unexplored_summary": "",
        }
    result["hashtag"] = slugify(result.get("hashtag") or "미분류", maxlen=30) or "미분류"
    if result.get("status") not in STATUS_LABEL:
        result["status"] = "in_progress"
    return result


_STRUCTURED_META_RE = re.compile(r"^<!--\s*waypoint-meta:\s*(\{.*?\})\s*-->\n?", re.DOTALL)


def split_structured_meta(text: str):
    """구조화 임시본 맨 앞의 `<!-- waypoint-meta: {...} -->` 헤더를 분리해서
    (meta_dict, 본문) 튜플로 돌려준다. 헤더가 없으면 meta={}.

    이 메타(대표태그/파일명/현재 해시태그/상태/원문 소비 길이 등)를 로컬
    state.json이 아니라 여기(git-tracked 파일)에도 같이 적어두는 이유:
    세션이 죽은 뒤 **다른 세션**이 orphan 복구를 할 때는 죽은 세션의
    로컬 state.json에 접근할 방법이 없다(컨테이너가 다름). 반면 이 헤더는
    git에 커밋돼 있어서 어느 세션에서 읽어도 같은 내용을 본다."""
    m = _STRUCTURED_META_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = json.loads(m.group(1))
    except Exception:
        meta = {}
    return meta, text[m.end():]


def join_structured_meta(meta: dict, body: str) -> str:
    return f"<!-- waypoint-meta: {json.dumps(meta, ensure_ascii=False)} -->\n" + body


def append_structured_chunk(body_text: str, hashtag: str, chunk_title: str,
                             chunk_summary_md: str) -> str:
    """구조화된 임시본의 본문(메타 헤더 제외)에 이번 체크포인트 결과를
    반영한 새 텍스트를 돌려준다 (순수 함수, I/O 없음 — 재시도 시 안전하게
    반복 적용 가능). 같은 해시태그 섹션이 이미 있으면 그 아래 이어붙이고,
    없으면 새 `## #해시태그` 섹션을 연다."""
    section_header = f"## #{hashtag}"
    if section_header in body_text:
        return body_text.rstrip("\n") + f"\n\n{chunk_summary_md.strip()}\n"
    sep = "\n\n" if body_text.strip() else ""
    return body_text + f"{sep}{section_header} — {chunk_title}\n\n{chunk_summary_md.strip()}\n"


def apply_checkpoint_result(root: Path, project: str, session_id: str, result: dict,
                             primary_tag: str, file_ts: str, file_slug: str,
                             raw_consumed_chars: int) -> None:
    """체크포인트 분류 결과 하나를 구조화 임시본(헤더 메타 + 해시태그
    섹션)에 반영해서 파일에 쓴다. root 아래
    waypoints/inprogress/<project>/<session_id>.structured.md 를 갱신한다.
    호출하는 쪽(capture_turn.py, session_end.py)이 모든 인자를 미리
    확정해서 넘기므로, 이 함수 자체는 몇 번을 다시 호출해도(재시도) 항상
    같은 결과를 만든다."""
    p = root / "waypoints" / "inprogress" / project / f"{session_id}.structured.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    meta, body = split_structured_meta(text)

    body = append_structured_chunk(body, result["hashtag"], result["chunk_title"],
                                    result["chunk_summary_md"])

    meta["project"] = project
    meta["session_id"] = session_id
    meta["primary_tag"] = primary_tag
    meta["file_ts"] = file_ts
    meta["file_slug"] = file_slug
    meta["current_hashtag"] = result["hashtag"]
    meta["last_status"] = result["status"]
    meta["raw_consumed_chars"] = raw_consumed_chars
    if result.get("has_unexplored_alt"):
        meta["has_unexplored_alt"] = True
        meta["unexplored_summary"] = result.get("unexplored_summary", "")
    else:
        meta.setdefault("has_unexplored_alt", False)
        meta.setdefault("unexplored_summary", "")

    p.write_text(join_structured_meta(meta, body), encoding="utf-8")


def update_index_tree(project: str, primary_tag: str, status: str, hashtag: str,
                       entry_label: str, rel_path: str, has_unexplored_alt: bool = False,
                       index_file: Path = None) -> None:
    """INDEX.md를 프로젝트 → 대표태그(상태) → 해시태그 → 항목의 3단
    구조로 갱신한다.

    - 같은 (대표태그, 해시태그, 경로) 조합이 이미 있으면 항목을 또 추가하지
      않는다 (체크포인트가 같은 주제로 여러 번 불려도 중복 안 쌓임).
    - 대표태그 헤더의 상태 라벨은 호출할 때마다 최신 값으로 갱신된다
      (진행 중 → 완료 등).
    - index_file 인자로 다른 경로(예: sync_waypoints_to_master의 worktree
      안 INDEX.md)를 넘길 수 있다 — push 충돌로 재시도할 때마다 그 시점
      최신 파일 위에서 다시 실행되어야 안전하게 병합되기 때문이다."""
    index_file = index_file or INDEX_FILE
    index_file.parent.mkdir(parents=True, exist_ok=True)
    text = index_file.read_text(encoding="utf-8") if index_file.exists() else "# Waypoint Index\n"
    lines = text.splitlines()

    project_header = f"## {project}"
    status_label = STATUS_LABEL.get(status, status)
    tag_header_prefix = f"### {primary_tag} ("
    tag_header = f"### {primary_tag} ({status_label})"
    hashtag_line = f"- #{hashtag}"
    star = " ⭐(미탐색 대안 있음)" if has_unexplored_alt else ""
    ts = datetime.now().strftime("%H:%M")
    entry_line = f"  - [{ts}] {entry_label} — [파일]({rel_path}){star}"

    if project_header not in lines:
        lines += ["", project_header, "", tag_header, hashtag_line, entry_line]
        index_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return

    p_idx = lines.index(project_header)
    end_idx = len(lines)
    for i in range(p_idx + 1, len(lines)):
        if lines[i].startswith("## "):
            end_idx = i
            break

    tag_idx = None
    for i in range(p_idx, end_idx):
        if lines[i].startswith(tag_header_prefix):
            tag_idx = i
            lines[i] = tag_header  # 상태 라벨 최신화
            break

    if tag_idx is None:
        needs_blank = end_idx > p_idx + 1 and lines[end_idx - 1].strip() != ""
        insert = ([""] if needs_blank else []) + [tag_header, hashtag_line, entry_line, ""]
        lines[end_idx:end_idx] = insert
        index_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return

    tag_end = end_idx
    for i in range(tag_idx + 1, end_idx):
        if lines[i].startswith("### ") or lines[i].startswith("## "):
            tag_end = i
            break

    hash_idx = None
    for i in range(tag_idx + 1, tag_end):
        if lines[i].strip() == hashtag_line:
            hash_idx = i
            break

    if hash_idx is None:
        lines[tag_end:tag_end] = [hashtag_line, entry_line]
        index_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return

    entry_end = tag_end
    for i in range(hash_idx + 1, tag_end):
        if lines[i].startswith("- #") or lines[i].startswith("### ") or lines[i].startswith("## "):
            entry_end = i
            break

    already = any(rel_path in lines[i] for i in range(hash_idx + 1, entry_end))
    if not already:
        lines.insert(entry_end, entry_line)

    index_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
