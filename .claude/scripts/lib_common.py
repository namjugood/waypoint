"""Waypoint 훅 스크립트들이 공유하는 공통 유틸리티.

이 파일은 표준 라이브러리만 사용한다 (외부 의존성 설치 불필요).
어떤 함수도 예외를 밖으로 던지지 않는 걸 원칙으로 한다 — 훅 스크립트가
죽으면 사용자의 실제 Claude Code 세션에 영향을 줄 수 있기 때문이다.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# .claude/scripts/lib_common.py -> 프로젝트 루트
PROJECT_ROOT = Path(__file__).resolve().parents[2]
WAYPOINTS_DIR = PROJECT_ROOT / "waypoints"
TMP_DIR = WAYPOINTS_DIR / ".tmp"
TAGS_DIR = WAYPOINTS_DIR / "tags"
INDEX_FILE = WAYPOINTS_DIR / "INDEX.md"
DEBUG_LOG = WAYPOINTS_DIR / ".debug.log"


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


def temp_md_path(session_id: str) -> Path:
    return TMP_DIR / f"{session_id}.md"


def state_path(session_id: str) -> Path:
    return TMP_DIR / f"{session_id}.state.json"


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
