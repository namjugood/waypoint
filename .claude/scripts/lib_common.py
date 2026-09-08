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
INPROGRESS_DIR = WAYPOINTS_DIR / "inprogress"  # 진행 중 세션의 원문 로그. git-tracked.
TAGS_DIR = WAYPOINTS_DIR / "tags"
INDEX_FILE = WAYPOINTS_DIR / "INDEX.md"
DEBUG_LOG = WAYPOINTS_DIR / ".debug.log"

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


def state_path(session_id: str) -> Path:
    return TMP_DIR / f"{session_id}.state.json"


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

        push = subprocess.run(["git", "-C", str(PROJECT_ROOT), "push"],
                               capture_output=True, text=True, timeout=60)
        if push.returncode != 0:
            log_debug(f"git push 실패, pull --rebase 후 재시도: {push.stderr[:300]}")
            subprocess.run(["git", "-C", str(PROJECT_ROOT), "pull", "--rebase"],
                            capture_output=True, text=True, timeout=60)
            retry = subprocess.run(["git", "-C", str(PROJECT_ROOT), "push"],
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
    다시 적용"하는 방식으로 짜야 한다 (예: update_index()는 항상 그
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

        # 현재 세션 브랜치의 로컬 워킹트리도 방금 push된 최종 상태로 맞춰서
        # git status가 지저분해지지 않게 한다. 이 커밋은 이 브랜치로는
        # push하지 않는다 — 이미 기본 브랜치에 올라갔으므로 중복 적재할
        # 필요가 없다 (나중에 이 브랜치로 PR을 올려도, 내용이 이미 같아서
        # 충돌 없이 무해하게 합쳐진다).
        for name in ("tags", "inprogress"):
            local_d = WAYPOINTS_DIR / name
            src_d = tmp_path / "waypoints" / name
            shutil.rmtree(local_d, ignore_errors=True)
            if src_d.exists():
                shutil.copytree(src_d, local_d)
        src_index = tmp_path / "waypoints" / "INDEX.md"
        if src_index.exists():
            shutil.copy2(src_index, INDEX_FILE)

        local_status = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "status", "--porcelain", "--", "waypoints"],
            capture_output=True, text=True, timeout=10,
        )
        if local_status.stdout.strip():
            subprocess.run(["git", "-C", str(PROJECT_ROOT), "add", "-A", "--", "waypoints"],
                            capture_output=True, text=True, timeout=15)
            subprocess.run(
                ["git", "-C", str(PROJECT_ROOT), "-c", "user.email=noreply@anthropic.com",
                 "-c", "user.name=Claude", "commit", "-m",
                 f"{commit_message} (local mirror; 이미 {default_branch}에 push됨, "
                 f"이 브랜치로는 push 안 함)"],
                capture_output=True, text=True, timeout=15,
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
