#!/usr/bin/env python3
"""SessionEnd 훅 (best-effort) + 고아 원문로그 복구용 finalize_session().

session_start.py도 이 파일의 finalize_session()을 import해서 재사용한다
(정상 종료 시엔 SessionEnd가, 비정상 종료 시엔 다음 SessionStart가 호출).

원문 자체는 이미 Stop 훅이 매 턴 커밋+푸시해뒀으므로(waypoints/inprogress/),
여기서는 데이터 유실 걱정 없이 "정리"만 담당한다:
1. inprogress의 원문 md를 읽어서 헤드리스 claude -p 호출로 태그/상태/제목/요약을 분류
2. 실패하면 "미분류" 태그로 원문 그대로 저장 (데이터 유실 방지가 최우선)
3. waypoints/tags/<project>/<tag>/<timestamp>-<slug>.md 로 저장
4. 산출물로 언급된 파일들을 attachments/ 로 복사
5. INDEX.md(태그트리) 갱신
6. inprogress 원문 파일 삭제
7. 전부 git commit + push
"""
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_common import (  # noqa: E402
    PROJECT_ROOT, TAGS_DIR, INDEX_FILE,
    log_debug, read_hook_input, inprogress_md_path, state_path,
    detect_project_name, slugify, call_claude_headless,
    extract_first_json_object, in_headless_recursion_guard,
    git_commit_and_push,
)

STATUS_LABEL = {"in_progress": "진행중", "done": "완료", "paused": "보류"}

CLASSIFY_INSTRUCTIONS = """\
아래는 Claude Code와 나눈 대화 기록의 일부다. 이 내용을 정리해줘.

반드시 아래 JSON 형식으로만, 다른 말 없이 응답해:
{{
  "title": "8~15자 내외 짧은 제목",
  "tags": ["주제태그1", "주제태그2"],
  "status": "in_progress 또는 done 또는 paused 중 하나",
  "has_unexplored_alt": true 또는 false,
  "unexplored_summary": "만약 has_unexplored_alt가 true면, 제안됐지만 선택 안 된 접근법을 1~2문장으로. 아니면 빈 문자열",
  "summary_md": "대화 내용을 마크다운으로 간결하게 정리한 요약. 핵심 결정/이유/결과 위주로. 불렛 포인트 활용 가능"
}}

status 판단 기준:
- done: 요청한 작업이 명확히 완료됨
- paused: 진행 중이었는데 애매하게 끝남 (미해결 질문, 다음 단계가 남음)
- in_progress: 계속 이어지고 있는 성격의 작업

대화 기록:
---
{content}
---
"""


def extract_deliverable_paths(content: str) -> list:
    return re.findall(r"산출물 생성됨: `(.*?)`", content)


def build_fallback_result(content: str) -> dict:
    first_line = next((l.strip() for l in content.splitlines() if l.strip() and not l.startswith("<!--") and not l.startswith("#")), "미분류 기록")
    return {
        "title": first_line[:20] or "미분류 기록",
        "tags": ["미분류"],
        "status": "in_progress",
        "has_unexplored_alt": False,
        "unexplored_summary": "",
        "summary_md": content,
    }


def update_index(project: str, tag: str, status: str, title: str,
                  rel_path: str, has_unexplored_alt: bool) -> None:
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    if INDEX_FILE.exists():
        text = INDEX_FILE.read_text(encoding="utf-8")
    else:
        text = "# Waypoint Index\n"

    lines = text.splitlines()
    project_header = f"## {project}"
    tag_header = f"### {tag} ({STATUS_LABEL.get(status, status)})"
    star = " ⭐(미탐색 대안 있음)" if has_unexplored_alt else ""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry_line = f"- [{ts}] {title} — [파일]({rel_path}){star}"

    if project_header not in lines:
        lines.append("")
        lines.append(project_header)
        lines.append("")
        lines.append(tag_header)
        lines.append(entry_line)
    else:
        p_idx = lines.index(project_header)
        # 이 프로젝트 섹션의 끝(다음 "## " 헤더 직전 또는 파일 끝) 찾기
        end_idx = len(lines)
        for i in range(p_idx + 1, len(lines)):
            if lines[i].startswith("## "):
                end_idx = i
                break
        if tag_header in lines[p_idx:end_idx]:
            t_idx = lines.index(tag_header, p_idx, end_idx)
            insert_at = t_idx + 1
            while insert_at < end_idx and lines[insert_at].strip().startswith("-"):
                insert_at += 1
            lines.insert(insert_at, entry_line)
        else:
            lines.insert(end_idx, tag_header)
            lines.insert(end_idx + 1, entry_line)
            lines.insert(end_idx + 2, "")

    INDEX_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def finalize_session(project: str, session_id: str) -> None:
    tmp_file = inprogress_md_path(project, session_id)
    if not tmp_file.exists():
        return
    content = tmp_file.read_text(encoding="utf-8")
    if not content.strip() or len(content.splitlines()) <= 3:
        # 헤더(+경고 문구)만 있고 실제 대화가 없으면 그냥 정리만 하고 끝
        tmp_file.unlink(missing_ok=True)
        state_path(session_id).unlink(missing_ok=True)
        git_commit_and_push(
            [tmp_file.relative_to(PROJECT_ROOT).as_posix()],
            f"waypoint: {project} 빈 세션 기록 정리 (session {session_id[:8]})",
            use_add_all=True,
        )
        return

    prompt = CLASSIFY_INSTRUCTIONS.format(content=content[:20000])
    raw_response = call_claude_headless(prompt, timeout=45)
    result = extract_first_json_object(raw_response)
    if not result:
        log_debug(f"태그 분류 실패({session_id}), 미분류로 폴백")
        result = build_fallback_result(content)

    tags = result.get("tags") or ["미분류"]
    primary_tag = slugify(tags[0]) or "미분류"
    status = result.get("status") if result.get("status") in STATUS_LABEL else "in_progress"
    title = result.get("title") or "제목 없음"
    has_alt = bool(result.get("has_unexplored_alt"))
    summary_md = result.get("summary_md") or content

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = slugify(title)
    target_dir = TAGS_DIR / project / primary_tag
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / f"{ts}-{slug}.md"

    frontmatter = (
        f"---\n"
        f"title: {title}\n"
        f"project: {project}\n"
        f"tags: {tags}\n"
        f"status: {status}\n"
        f"has_unexplored_alt: {str(has_alt).lower()}\n"
        f"created: {datetime.now().isoformat(timespec='seconds')}\n"
        f"---\n\n"
    )
    body = frontmatter + summary_md
    if has_alt and result.get("unexplored_summary"):
        body += f"\n\n## 미탐색 대안\n{result['unexplored_summary']}\n"

    # 산출물 복사
    deliverables = extract_deliverable_paths(content)
    if deliverables:
        attach_dir = target_dir / f"{ts}-{slug}-attachments"
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
        if attach_dir.exists():
            body += f"\n\n## 산출물\n- [{attach_dir.name}]({attach_dir.name}/)\n"

    target_file.write_text(body, encoding="utf-8")

    rel_path = target_file.relative_to(TAGS_DIR.parent).as_posix()
    update_index(project, primary_tag, status, title, rel_path, has_alt)

    # 정리 끝났으니 진행 중 원문 로그(inprogress)는 삭제 — 최종본이 tags/에 있음
    tmp_file.unlink(missing_ok=True)
    state_path(session_id).unlink(missing_ok=True)

    git_commit_and_push(
        ["waypoints/tags", "waypoints/INDEX.md",
         tmp_file.relative_to(PROJECT_ROOT).as_posix()],
        f"waypoint: {project} / {title}",
        use_add_all=True,
    )


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
