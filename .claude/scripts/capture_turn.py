#!/usr/bin/env python3
"""Stop 훅 — 매 턴이 끝날 때마다 그 턴의 대화를 원문 로그에 append하고
바로 git commit+push한다.

왜 매 턴 커밋하는가: 원격 웹 세션에서는 "Archive"를 눌러도 SessionEnd
훅이 컨테이너 회수 전에 반드시 끝난다는 보장이 없다. 로컬에만 쌓아두면
SessionEnd가 못 돌았을 때 컨테이너와 함께 통째로 사라진다. 그래서 매 턴
바로 push해서, 세션이 어떻게 끝나든 원문만큼은 git에 이미 들어가 있게
한다 (분류/정리는 SessionEnd가 나중에 처리).

주의:
- stop_hook_active가 true면 즉시 종료한다 (무한 루프 방지 — Claude Code가
  Stop 훅 자체를 다시 트리거하는 상황을 막기 위한 표준 가드).
- 여기서는 AI를 호출하지 않는다 (판단이 필요 없는, 확실한 기계적 저장만).
- 절대 종료코드를 0이 아닌 값으로 내지 않는다 — 훅 실패가 사용자 세션을
  방해하면 안 되기 때문.
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_common import (  # noqa: E402
    PROJECT_ROOT, log_debug, read_hook_input, inprogress_md_path,
    detect_project_name, load_state, save_state, read_transcript_entries,
    extract_text_and_files, in_headless_recursion_guard, git_commit_and_push,
)


def format_entry(role: str, text: str, files: list) -> str:
    ts = datetime.now().strftime("%H:%M:%S")
    label = "사용자" if role == "user" else "Claude"
    out = f"\n### [{ts}] {label}\n{text}\n"
    for fp in files:
        out += f"\n> 산출물 생성됨: `{fp}`\n"
    return out


def main() -> None:
    if in_headless_recursion_guard():
        return
    data = read_hook_input()
    if data.get("stop_hook_active"):
        return

    session_id = data.get("session_id") or "unknown-session"
    transcript_path = data.get("transcript_path")
    cwd = data.get("cwd") or "."
    project = detect_project_name(cwd)

    entries = read_transcript_entries(transcript_path)
    if not entries:
        return

    state = load_state(session_id)
    last_line = state.get("last_line", 0)
    new_entries = entries[last_line:]
    if not new_entries:
        return

    tmp_file = inprogress_md_path(project, session_id)
    tmp_file.parent.mkdir(parents=True, exist_ok=True)
    if not tmp_file.exists():
        tmp_file.write_text(
            f"<!-- project: {project} | session: {session_id} -->\n"
            f"# Waypoint 진행 중 원문 로그 ({project})\n"
            f"\n> ⚠️ 자동 누적 로그입니다. AI 분류 전 원문이며, 세션 종료 시 "
            f"정리된 최종 기록으로 대체됩니다.\n",
            encoding="utf-8",
        )

    chunks = []
    for entry in new_entries:
        etype = entry.get("type")
        if etype not in ("user", "assistant"):
            continue
        message = entry.get("message") or {}
        content = message.get("content")
        text, files = extract_text_and_files(content)
        if not text and not files:
            continue
        chunks.append(format_entry(etype, text, files))

    if chunks:
        with open(tmp_file, "a", encoding="utf-8") as f:
            f.write("".join(chunks))

    save_state(session_id, {"last_line": len(entries), "project": project})

    if chunks:
        rel_path = tmp_file.relative_to(PROJECT_ROOT).as_posix()
        short_id = session_id[:8]
        message = (
            f"waypoint-log: {project} 진행 중 기록 갱신 (session {short_id})\n\n"
            f"자동 누적 로그 커밋입니다 (매 턴 저장, AI 분류 전 원문). "
            f"세션 종료 시 정리된 최종 기록(waypoint: ...)으로 대체되고 이 "
            f"파일은 정리됩니다."
        )
        git_commit_and_push([rel_path], message)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log_debug(f"capture_turn 최상위 예외: {e}")
    sys.exit(0)
