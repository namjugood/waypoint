#!/usr/bin/env python3
"""Stop 훅 — 매 턴이 끝날 때마다 그 턴의 대화를 임시 md에 append.

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
    TMP_DIR, log_debug, read_hook_input, temp_md_path,
    load_state, save_state, read_transcript_entries, extract_text_and_files,
    in_headless_recursion_guard,
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

    entries = read_transcript_entries(transcript_path)
    if not entries:
        return

    state = load_state(session_id)
    last_line = state.get("last_line", 0)
    new_entries = entries[last_line:]
    if not new_entries:
        return

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    tmp_file = temp_md_path(session_id)
    if not tmp_file.exists():
        tmp_file.write_text(f"# Waypoint 임시 기록\n(session: {session_id})\n", encoding="utf-8")

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

    save_state(session_id, {"last_line": len(entries), "project": state.get("project")})


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log_debug(f"capture_turn 최상위 예외: {e}")
    sys.exit(0)
