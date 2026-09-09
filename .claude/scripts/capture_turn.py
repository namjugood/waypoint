#!/usr/bin/env python3
"""Stop 훅 — 매 턴이 끝날 때마다 그 턴의 대화를 원문 로그에 append하고
바로 (세션 전용 브랜치가 아니라) 저장소 기본 브랜치에 commit+push한다.
누적된 원문이 일정량을 넘으면 체크포인트를 돌려 해시태그 단위로 미리
정리해둔다 (맵-리듀스의 "map" 단계).

왜 매 턴 커밋하는가: 원격 웹 세션에서는 "Archive"를 눌러도 SessionEnd
훅이 컨테이너 회수 전에 반드시 끝난다는 보장이 없다. 로컬에만 쌓아두면
SessionEnd가 못 돌았을 때 컨테이너와 함께 통째로 사라진다. 그래서 매 턴
바로 push해서, 세션이 어떻게 끝나든 원문만큼은 git에 이미 들어가 있게
한다.

왜 기본 브랜치로 직접 보내는가: 원격 웹 세션은 세션마다 전용 브랜치가
자동 배정된다. 세션 브랜치에만 커밋하면 그 브랜치가 머지되기 전까지는
다음 세션이 이 기록을 못 본다 — /waypoint의 연속성이 깨진다. 그래서
waypoints/ 변경만 격리해서(실제 코드 변경은 절대 안 건드리고) 기본
브랜치에 바로 push한다 (`lib_common.sync_waypoints_to_master`).

왜 체크포인트를 매 턴이 아니라 누적량 기준으로 도는가: "이번 조각이
이전 주제와 같은지 다른지" 판단하려면 AI 호출이 필요한데, 매 턴 그걸
하면 턴마다 몇 초씩 늘어난다. 원문 누적(git commit)은 지금처럼 매 턴
그대로 하고(AI 호출 없음, 빠름), AI 분류만 CHECKPOINT_THRESHOLD_CHARS
만큼 쌓였을 때만 돈다. 체크포인트 처리 중 세션이 죽어도 원문은 이미
안전하게 커밋돼 있으므로 데이터 유실은 없다 — 다음 체크포인트나
SessionEnd/orphan 복구가 이어서 처리한다.

주의:
- stop_hook_active가 true면 즉시 종료한다 (무한 루프 방지).
- 절대 종료코드를 0이 아닌 값으로 내지 않는다.
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_common import (  # noqa: E402
    PROJECT_ROOT, INDEX_FILE, CHECKPOINT_THRESHOLD_CHARS, inprogress_md_path,
    structured_inprogress_path, split_structured_meta,
    log_debug, read_hook_input, detect_project_name, load_state, save_state,
    read_transcript_entries, extract_text_and_files, in_headless_recursion_guard,
    git_commit_and_push, sync_waypoints_to_master, extract_known_tags,
    classify_checkpoint, apply_checkpoint_result, update_index_tree, slugify,
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

    if not chunks:
        save_state(session_id, {**state, "last_line": len(entries), "project": project})
        return

    chunk_text = "".join(chunks)
    header_text = (
        f"<!-- project: {project} | session: {session_id} -->\n"
        f"# Waypoint 진행 중 원문 로그 ({project})\n"
        f"\n> ⚠️ 자동 누적 로그입니다. AI 분류 전 원문이며, 세션 종료 시 "
        f"정리된 최종 기록으로 대체됩니다.\n"
    )

    # 원문 파일이 이번 턴까지 반영되면 가질 총 길이 — 체크포인트가 돌면
    # "여기까지는 이미 구조화했다"는 표시로 구조화 임시본 헤더에 남긴다
    # (raw_consumed_chars). 로컬에 이미 미러링된 원문 길이 + 이번 턴 조각.
    local_raw = inprogress_md_path(project, session_id)
    existing_raw_text = local_raw.read_text(encoding="utf-8") if local_raw.exists() else header_text
    raw_total_chars = len(existing_raw_text) + len(chunk_text)

    # 체크포인트(map 단계): 임계값을 넘었을 때만 AI 호출 — 계산은 여기서
    # 딱 한 번만 하고, prepare()는 그 결과를 그대로 적용만 한다 (재시도돼도
    # 매번 같은 값을 적용해야 안전하므로 datetime.now() 등을 prepare 안에서
    # 새로 계산하지 않는다).
    #
    # pending_text/primary_tag/file_ts/file_slug/current_hashtag는 로컬
    # state.json이 아니라 구조화 임시본의 git 추적 메타 헤더(+원문 파일)를
    # 우선한다 — state.json은 `waypoints/.tmp/`(git 미추적)에 있어서 턴
    # 사이에 사라질 수 있는데, 그러면 이미 진행 중이던 체크포인트를 "이번이
    # 첫 조각"으로 오인해 매번 새 file_ts/파일을 만들어버리고, 이전
    # 체크포인트가 INDEX.md에 남긴 링크는 영원히 고아가 된다(실제로 겪은
    # 버그). 구조화 임시본은 매 체크포인트마다 커밋되는 git 추적 파일이라
    # 더 안정적인 진실 공급원이다. pending_text는 raw_consumed_chars 이후의
    # 원문 조각으로 역산한다 — session_end.py의 finalize_session()과 같은
    # 방식.
    structured_path = structured_inprogress_path(project, session_id)
    structured_meta = {}
    if structured_path.exists():
        structured_meta, _ = split_structured_meta(structured_path.read_text(encoding="utf-8"))

    consumed = structured_meta.get("raw_consumed_chars")
    if consumed is not None:
        pending_text = existing_raw_text[consumed:] + chunk_text
    else:
        pending_text = (state.get("pending_text") or "") + chunk_text

    checkpoint_result = None
    primary_tag = structured_meta.get("primary_tag") or state.get("primary_tag")
    file_ts = structured_meta.get("file_ts") or state.get("file_ts")
    file_slug = structured_meta.get("file_slug") or state.get("file_slug")
    current_hashtag = structured_meta.get("current_hashtag") or state.get("current_hashtag")
    rel_path = None
    if len(pending_text) >= CHECKPOINT_THRESHOLD_CHARS:
        known_tags = extract_known_tags(INDEX_FILE.read_text(encoding="utf-8")) if INDEX_FILE.exists() else []
        checkpoint_result = classify_checkpoint(current_hashtag, known_tags, pending_text)
        primary_tag = primary_tag or checkpoint_result["hashtag"]
        file_ts = file_ts or datetime.now().strftime("%Y%m%d-%H%M%S")
        file_slug = file_slug or slugify(checkpoint_result["chunk_title"])
        rel_path = f"tags/{project}/{primary_tag}/{file_ts}-{file_slug}.md"

    def prepare(root):
        wt_raw = root / "waypoints" / "inprogress" / project / f"{session_id}.md"
        wt_raw.parent.mkdir(parents=True, exist_ok=True)
        if not wt_raw.exists():
            wt_raw.write_text(header_text, encoding="utf-8")
        with open(wt_raw, "a", encoding="utf-8") as f:
            f.write(chunk_text)

        if checkpoint_result:
            apply_checkpoint_result(root, project, session_id, checkpoint_result,
                                     primary_tag, file_ts, file_slug, raw_total_chars)
            update_index_tree(
                project, primary_tag, checkpoint_result["status"], checkpoint_result["hashtag"],
                checkpoint_result["chunk_title"], rel_path,
                has_unexplored_alt=bool(checkpoint_result.get("has_unexplored_alt")),
                index_file=root / "waypoints" / "INDEX.md",
            )

    short_id = session_id[:8]
    if checkpoint_result:
        message = (
            f"waypoint-checkpoint: {project} #{checkpoint_result['hashtag']} 정리 "
            f"(session {short_id})\n\n"
            f"자동 체크포인트 커밋입니다 — 누적된 원문 조각을 해시태그로 "
            f"분류해 정리했습니다 (세션 전체가 아니라 이 조각만 분류 — "
            f"긴 세션에서도 한 번에 처리하는 양을 작게 유지하기 위함)."
        )
    else:
        message = (
            f"waypoint-log: {project} 진행 중 기록 갱신 (session {short_id})\n\n"
            f"자동 누적 로그 커밋입니다 (매 턴 저장, AI 분류 전 원문)."
        )

    if not sync_waypoints_to_master(prepare, message):
        log_debug(f"master 동기화 실패({session_id}), 현재 브랜치로 폴백")
        prepare(PROJECT_ROOT)
        git_commit_and_push(["waypoints"], message, use_add_all=True)

    new_state = {"last_line": len(entries), "project": project}
    if checkpoint_result:
        new_state["pending_text"] = ""
        new_state["current_hashtag"] = checkpoint_result["hashtag"]
        new_state["primary_tag"] = primary_tag
        new_state["file_ts"] = file_ts
        new_state["file_slug"] = file_slug
        new_state["last_status"] = checkpoint_result["status"]
        new_state["has_unexplored_alt"] = bool(state.get("has_unexplored_alt")) or bool(
            checkpoint_result.get("has_unexplored_alt"))
        new_state["unexplored_summary"] = (
            checkpoint_result.get("unexplored_summary") if checkpoint_result.get("has_unexplored_alt")
            else state.get("unexplored_summary", "")
        )
    else:
        new_state["pending_text"] = pending_text
        for k in ("current_hashtag", "primary_tag", "file_ts", "file_slug", "last_status",
                  "has_unexplored_alt", "unexplored_summary"):
            if k in state:
                new_state[k] = state[k]
    save_state(session_id, new_state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log_debug(f"capture_turn 최상위 예외: {e}")
    sys.exit(0)
