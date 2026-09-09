#!/usr/bin/env python3
"""SessionEnd 훅 (best-effort) + 고아 원문로그 복구용 finalize_session().

session_start.py도 이 파일의 finalize_session()을 import해서 재사용한다
(정상 종료 시엔 SessionEnd가, 비정상 종료 시엔 다음 SessionStart가 호출).
후자의 경우 finalize_session을 호출하는 세션은 원래 대화를 나눈 세션이
아닐 수 있으므로, 로컬 state.json에는 의존하지 않고 구조화 임시본의
헤더(git-tracked)에서 필요한 정보를 읽는다.

원문 자체는 이미 Stop 훅이 매 턴 커밋+푸시해뒀고(waypoints/inprogress/),
누적량이 CHECKPOINT_THRESHOLD_CHARS를 넘을 때마다 체크포인트로 미리
해시태그 단위 정리도 돼 있다(구조화 임시본). 여기서는:
1. 아직 체크포인트 안 된 마지막 조각이 있으면 한 번 더 분류해서 마저 반영
2. 완성된 구조화 임시본을 최종 문서로 waypoints/tags/에 저장
3. 원문은 지우지 않고 waypoints/raw/로 옮겨서 보관 (요약은 손실 압축이라
   나중에 원문 확인이 필요할 수 있음)
4. 산출물로 언급된 파일들을 attachments/ 로 복사
5. INDEX.md(태그트리) 최종 상태 반영
6. inprogress 임시본 정리
7. 전부 저장소 기본 브랜치로 git commit + push
"""
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_common import (  # noqa: E402
    PROJECT_ROOT, inprogress_md_path, structured_inprogress_path, state_path,
    log_debug, read_hook_input, detect_project_name, slugify,
    in_headless_recursion_guard, git_commit_and_push, sync_waypoints_to_master,
    extract_known_tags, classify_checkpoint, apply_checkpoint_result,
    update_index_tree, split_structured_meta,
)


def extract_deliverable_paths(content: str) -> list:
    return re.findall(r"산출물 생성됨: `(.*?)`", content)


def _cleanup_inprogress(root: Path, project: str, session_id: str) -> None:
    (root / "waypoints" / "inprogress" / project / f"{session_id}.md").unlink(missing_ok=True)
    (root / "waypoints" / "inprogress" / project / f"{session_id}.structured.md").unlink(missing_ok=True)


def finalize_session(project: str, session_id: str) -> None:
    """분류/정리 결과를 어디에 반영할지는 sync_waypoints_to_master()가
    맡는다: 실제 코드 변경이 섞여 있는 현재 세션 브랜치가 아니라, 격리된
    worktree를 통해 저장소 기본 브랜치에 직접 push한다. 여기서는 그
    prepare_fn을 만드는 데 필요한 내용(체크포인트 분류 결과 등)을 미리
    계산해서 넘긴다 — push 충돌로 재시도될 때마다 prepare_fn이 다시
    불릴 수 있으므로, 계산된 값들은 재시도 사이에 그대로 재사용해도
    안전한(멱등한) 것들이어야 한다. INDEX.md는 예외라서 update_index_tree()
    를 prepare_fn 안에서 매번 그 시점 최신 파일에 대고 다시 실행한다."""
    raw_file = inprogress_md_path(project, session_id)
    if not raw_file.exists():
        return
    content = raw_file.read_text(encoding="utf-8")
    if not content.strip() or len(content.splitlines()) <= 3:
        # 헤더(+경고 문구)만 있고 실제 대화가 없으면 그냥 정리만 하고 끝
        message = f"waypoint: {project} 빈 세션 기록 정리 (session {session_id[:8]})"

        def prepare_empty(root):
            _cleanup_inprogress(root, project, session_id)

        if not sync_waypoints_to_master(prepare_empty, message):
            log_debug(f"master 동기화 실패({session_id}), 현재 브랜치로 폴백")
            prepare_empty(PROJECT_ROOT)
            git_commit_and_push(["waypoints"], message, use_add_all=True)
        state_path(session_id).unlink(missing_ok=True)
        return

    structured_file = structured_inprogress_path(project, session_id)
    structured_text = structured_file.read_text(encoding="utf-8") if structured_file.exists() else ""
    meta, body = split_structured_meta(structured_text)

    primary_tag = meta.get("primary_tag")
    file_ts = meta.get("file_ts")
    file_slug = meta.get("file_slug")
    current_hashtag = meta.get("current_hashtag")
    consumed = meta.get("raw_consumed_chars") or 0
    pending_text = content[consumed:]

    final_result = None
    if pending_text.strip():
        # 아직 체크포인트 안 된 마지막 조각(또는 체크포인트가 한 번도 안
        # 돈 짧은 세션 전체)을 여기서 한 번 더 분류해서 마저 반영한다.
        # 맵-리듀스의 마지막 map 호출과 같다 — 이 호출도 pending_text
        # 하나만 보므로, 세션이 길었어도 이 시점 입력 크기는 여전히 작다.
        index_file_for_tags = PROJECT_ROOT / "waypoints" / "INDEX.md"
        known_tags = extract_known_tags(index_file_for_tags.read_text(encoding="utf-8")) \
            if index_file_for_tags.exists() else []
        final_result = classify_checkpoint(current_hashtag, known_tags, pending_text)
        primary_tag = primary_tag or final_result["hashtag"]
        file_ts = file_ts or datetime.now().strftime("%Y%m%d-%H%M%S")
        file_slug = file_slug or slugify(final_result["chunk_title"])

    if not primary_tag:
        # pending_text도 비어있고 이전 체크포인트도 없었던 경우 (사실상
        # 빈 세션인데 위의 3줄 이하 검사만으로는 못 걸러진 경우) — 안전하게
        # 정리만 하고 끝낸다.
        message = f"waypoint: {project} 빈 세션 기록 정리 (session {session_id[:8]})"

        def prepare_empty2(root):
            _cleanup_inprogress(root, project, session_id)

        if not sync_waypoints_to_master(prepare_empty2, message):
            prepare_empty2(PROJECT_ROOT)
            git_commit_and_push(["waypoints"], message, use_add_all=True)
        state_path(session_id).unlink(missing_ok=True)
        return

    rel_path = f"tags/{project}/{primary_tag}/{file_ts}-{file_slug}.md"
    deliverables = extract_deliverable_paths(content)
    attach_dirname = f"{file_ts}-{file_slug}-attachments"

    def prepare(root: Path):
        # 마지막 조각이 있으면 구조화 임시본에 마저 반영
        if final_result:
            apply_checkpoint_result(root, project, session_id, final_result,
                                     primary_tag, file_ts, file_slug, len(content))

        wt_structured = root / "waypoints" / "inprogress" / project / f"{session_id}.structured.md"
        final_meta, final_body = split_structured_meta(
            wt_structured.read_text(encoding="utf-8") if wt_structured.exists() else structured_text
        )
        status = final_meta.get("last_status", "in_progress")
        has_alt = bool(final_meta.get("has_unexplored_alt"))
        unexplored_summary = final_meta.get("unexplored_summary", "")
        all_hashtags = re.findall(r"^## #(\S+)", final_body, flags=re.MULTILINE)
        title_match = re.search(rf"^## #{re.escape(primary_tag)} — (.+)$", final_body, flags=re.MULTILINE)
        title = title_match.group(1).strip() if title_match else primary_tag

        frontmatter = (
            f"---\n"
            f"title: {title}\n"
            f"project: {project}\n"
            f"tags: {all_hashtags or [primary_tag]}\n"
            f"status: {status}\n"
            f"has_unexplored_alt: {str(has_alt).lower()}\n"
            f"created: {datetime.now().isoformat(timespec='seconds')}\n"
            f"raw: raw/{project}/{primary_tag}/{file_ts}-{file_slug}.md\n"
            f"---\n\n"
        )
        doc_body = frontmatter + final_body
        if has_alt and unexplored_summary:
            doc_body += f"\n\n## 미탐색 대안\n{unexplored_summary}\n"
        if deliverables:
            doc_body += f"\n\n## 산출물\n- [{attach_dirname}]({attach_dirname}/)\n"

        wt_target = root / "waypoints" / "tags" / project / primary_tag / f"{file_ts}-{file_slug}.md"
        wt_target.parent.mkdir(parents=True, exist_ok=True)
        wt_target.write_text(doc_body, encoding="utf-8")

        # 원문은 지우지 않고 raw/로 그대로 옮겨서 보관 (요약 손실 대비)
        wt_raw_archive = root / "waypoints" / "raw" / project / primary_tag / f"{file_ts}-{file_slug}.md"
        wt_raw_archive.parent.mkdir(parents=True, exist_ok=True)
        wt_raw_archive.write_text(content, encoding="utf-8")

        if deliverables:
            attach_dir = wt_target.parent / attach_dirname
            for fp in deliverables:
                try:
                    src = Path(fp)
                    if not src.is_absolute():
                        src = PROJECT_ROOT / fp
                    if src.exists() and src.is_file():
                        attach_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, attach_dir / src.name)
                except Exception as e:
                    log_debug(f"산출물 복사 실패({fp}): {e}")

        _cleanup_inprogress(root, project, session_id)

        # 상태 라벨을 최종값으로 갱신 (이미 있는 해시태그면 update_index_tree가
        # 중복 없이 상태만 최신화한다). final_result가 있으면(방금 마지막
        # 조각을 새로 분류한 경우) 그 조각의 정확한 제목/해시태그를 쓰고,
        # 없으면(전부 이전 체크포인트에서 이미 반영된 경우) 세션 대표
        # 제목으로 상태만 갱신한다.
        last_hashtag = final_result["hashtag"] if final_result else (all_hashtags[-1] if all_hashtags else primary_tag)
        last_label = final_result["chunk_title"] if final_result else title
        update_index_tree(
            project, primary_tag, status, last_hashtag,
            last_label, rel_path, has_unexplored_alt=has_alt,
            index_file=root / "waypoints" / "INDEX.md",
        )

    message = f"waypoint: {project} / {primary_tag} 세션 정리 완료 (session {session_id[:8]})"
    if not sync_waypoints_to_master(prepare, message):
        log_debug(f"master 동기화 실패({session_id}), 현재 브랜치로 폴백")
        prepare(PROJECT_ROOT)
        git_commit_and_push(["waypoints"], message, use_add_all=True)

    state_path(session_id).unlink(missing_ok=True)


def main() -> None:
    if in_headless_recursion_guard():
        return
    data = read_hook_input()
    session_id = data.get("session_id") or "unknown-session"
    cwd = data.get("cwd") or "."
    project = detect_project_name(cwd)
    finalize_session(project, session_id)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log_debug(f"session_end 최상위 예외: {e}")
    sys.exit(0)
