#!/usr/bin/env python3
"""SessionStart 훅.

하는 일:
1. 이전에 비정상 종료로 처리 안 된 원문 로그(다른 세션의 것)가 있으면
   session_end 로직을 재사용해서 먼저 정리한다 (안전망).
2. 이번 세션용 원문 로그 파일을 만든다 (resume이면 이미 있을 수 있으니 유지).
3. INDEX.md에서 이 프로젝트와 관련된 "진행중/보류" 항목을 뽑아서
   additionalContext로 Claude에게 흘려준다 -> Claude가 자연스럽게
   "이어서 할 만한 작업이 있어요" 라고 사용자에게 제안할 수 있게.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_common import (  # noqa: E402
    INPROGRESS_DIR, INDEX_FILE, PROJECT_ROOT, log_debug, read_hook_input,
    inprogress_md_path, detect_project_name, in_headless_recursion_guard,
    git_last_commit_epoch,
)
from session_end import finalize_session  # noqa: E402


ORPHAN_IDLE_SECONDS = 10 * 60  # 이보다 오래 안 건드려진 원문로그만 "죽은 세션"으로 간주


def recover_orphans(current_session_id: str) -> None:
    """다른 세션 ID의 원문 로그 중, 한동안 갱신이 없었던 것만 정리한다.
    같은 프로젝트에서 다른 세션(다른 탭/창, 다른 컨테이너)이 지금 동시에
    진행 중일 수 있으므로, 단순히 "session_id가 다르다"는 이유만으로
    finalize하면 아직 살아있는 세션의 기록을 중간에 끊어버리게 된다
    (실제로 겪은 버그).

    원격 웹 세션은 매번 새 컨테이너(=새로운 로컬 디스크)로 시작하므로
    로컬 mtime으로는 "최근에 활동 중인지"를 판단할 수 없다 (fresh clone
    직후엔 모든 파일의 mtime이 "지금"이 되어버림). 대신 git 커밋 이력의
    타임스탬프로 판단한다 — Stop 훅이 매 턴 커밋하므로, 다른 세션이 아직
    활성 상태라면 최근 커밋이 있을 것이다."""
    if not INPROGRESS_DIR.exists():
        return
    now = time.time()
    # *.structured.md(체크포인트로 쌓이는 구조화 임시본)는 원문(raw) 파일과
    # 짝을 이루는 파생물이라 여기서 별도 세션으로 잡으면 안 된다 — sid를
    # "<진짜세션ID>.structured"로 잘못 파싱하게 된다. 원문 파일만 훑는다.
    for md_file in INPROGRESS_DIR.glob("*/*.md"):
        if md_file.name.endswith(".structured.md"):
            continue
        sid = md_file.stem
        if sid == current_session_id:
            continue
        project = md_file.parent.name
        try:
            rel_path = md_file.relative_to(PROJECT_ROOT).as_posix()
            last_commit = git_last_commit_epoch(rel_path)
            reference_time = last_commit if last_commit is not None else md_file.stat().st_mtime
            if now - reference_time < ORPHAN_IDLE_SECONDS:
                continue  # 최근에 갱신됨 -> 다른 세션이 아직 활성 중일 가능성, 건드리지 않음
            if md_file.stat().st_size == 0:
                md_file.unlink(missing_ok=True)
                continue
            log_debug(f"고아 원문로그 발견({ORPHAN_IDLE_SECONDS}s 이상 idle), 정리 시도: {project}/{sid}")
            finalize_session(project, sid)
        except Exception as e:
            log_debug(f"고아 원문로그 정리 실패({project}/{sid}): {e}")


def build_resume_context(project: str) -> str:
    if not INDEX_FILE.exists():
        return ""
    try:
        content = INDEX_FILE.read_text(encoding="utf-8")
    except Exception:
        return ""

    # INDEX.md는 "## <project>" 섹션 단위로 구성됨. 해당 프로젝트 섹션만 추출.
    lines = content.splitlines()
    section_lines = []
    in_section = False
    for line in lines:
        if line.startswith("## "):
            in_section = (line.strip() == f"## {project}")
            if in_section:
                continue
        if in_section:
            section_lines.append(line)

    if not section_lines:
        return ""

    # 완료 표시가 없는(=완료가 아닌) 항목만 대략적으로 필터링
    relevant = [l for l in section_lines if l.strip() and "(완료)" not in l]
    if not relevant:
        return ""

    body = "\n".join(relevant[:15])
    return (
        "[Waypoint] 이 프로젝트에서 이전에 기록된, 아직 끝나지 않은 작업들이에요. "
        "사용자가 관련된 걸 물어보면 이어갈 수 있다고 자연스럽게 언급해주세요 "
        "(강요하지 말고, 짧게 목록으로 제시하고 사용자가 고르게 하세요):\n\n" + body
    )


def main() -> None:
    if in_headless_recursion_guard():
        return
    data = read_hook_input()
    session_id = data.get("session_id") or "unknown-session"
    cwd = data.get("cwd") or "."

    recover_orphans(session_id)

    project = detect_project_name(cwd)
    tmp_file = inprogress_md_path(project, session_id)
    if not tmp_file.exists():
        tmp_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file.write_text(
            f"<!-- project: {project} | session: {session_id} -->\n"
            f"# Waypoint 진행 중 원문 로그 ({project})\n"
            f"\n> ⚠️ 자동 누적 로그입니다. AI 분류 전 원문이며, 세션 종료 시 "
            f"정리된 최종 기록으로 대체됩니다.\n",
            encoding="utf-8",
        )
        # 아직 내용이 없으므로 여기선 커밋하지 않는다 (Stop 훅이 실제 대화가
        # 쌓이는 첫 턴에 커밋). 헤더만 있는 빈 커밋을 방지하기 위함.

    context = build_resume_context(project)
    if context:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        }))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log_debug(f"session_start 최상위 예외: {e}")
    sys.exit(0)
