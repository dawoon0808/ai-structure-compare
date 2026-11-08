#!/usr/bin/env python3
"""AI 사용 구조 비교 도구 (단일 파일, Python 3.8+ 표준 라이브러리만 사용).

같은 코드·문서에서 AI 사용 구조(에이전트·스킬·CLAUDE.md·규칙)만 섹션(폴더/worktree)마다 다르게 두고,
같은 작업을 시켜 토큰·작업 시간·품질(사람 판정)을 비교한다.

명령:
  init       기록 폴더 준비, --install-hooks 로 이 섹션의 .claude/settings.local.json 에 훅 등록
  structure  이 섹션의 AI 구조(에이전트·스킬·규칙 파일·지문)와 코드 기준(커밋) 출력
  plan       실행 예정 등록
  start      실행 시작 (구조 스냅샷·커밋 자동 기록)
  event      스킬/파일/에이전트/토큰/메모 수동 기록 (훅을 못 쓸 때)
  end        AI 작업 종료 → '판정 대기' (합격 여부는 넣지 않음)
  judge      사람 판정
  hook       Claude Code 훅 진입점 (stdin JSON)
  serve      실시간 대시보드 (http://localhost:8765)
  reset      AI 설정은 남기고 코드·문서 변경만 되돌림 (작업 사이 초기화, 기본은 미리보기)
  build      단일 HTML 스냅샷
  status     텍스트 요약
기록 폴더: --dir > 환경변수 MES_DIR > 섹션의 .mes-dir 파일 > (git) 원본 저장소/.mes > (SVN·기타) 섹션 폴더 옆 mes-compare-data
  → 섹션 폴더들을 한 상위 폴더 아래에 나란히 두면 기록이 자동으로 한곳에 모인다. git·SVN 모두 지원.
"""
import argparse
import datetime as dt
import fnmatch
import glob as globlib
import hashlib
import json
import os
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "2.0"
STALE_MIN = 5
MAIN_AGENT = "메인 세션"
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
REVIEW_HINTS = ("review", "리뷰", "검증", "reviewer", "verif")
FILE_TOOLS = {"Read": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "Write": "file_path",
              "NotebookEdit": "notebook_path"}
AI_PATHS = (".claude", "CLAUDE.md", "CLAUDE.local.md", ".mcp.json", ".mes")
# 명령 문자열 안의 파일 경로 (따옴표 안 공백 경로 포함). 확장자가 있는 것만
CMD_PATH_RE = re.compile(r"\"[^\"\n]+?\.[A-Za-z0-9]{1,6}\"|'[^'\n]+?\.[A-Za-z0-9]{1,6}'|(?:[A-Za-z]:)?[\w.\-/\\가-힣~]+\.[A-Za-z0-9]{1,6}\b")
DATA_POINTER = ".mes-dir"
PROFILE_MARK = ".mes-profile.json"      # 섹션에 적용한 구조 기록
PROFILE_BACKUP = ".mes-profile-backup"  # 구조 적용 전 백업
PROFILE_MANIFEST = "profile.json"       # 구조 묶음의 목록 파일
INTERNAL_DIR = ".claude/imported"       # 섹션 밖 @import 파일을 묶음 안으로 넣는 위치
SKIP_DIRS = (".git/", "node_modules/", ".svn/", ".mes")
IMPORT_DEPTH = 5  # Claude Code 의 @import 최대 깊이
IMPORT_RE = re.compile(r"(?<![\w`/@])@((?:~|[A-Za-z]:)?[^\s`'\"<>()\[\]{}]+)")
# setup.json 에서 읽는 분류 (configure 가 채움)
AI_GLOBS = []    # ai_files: 섹션 안에서 구조(AI 설정)로 볼 파일 패턴. 예: "**/AGENTS.md"
FIXED_REFS = []  # fixed_refs: 모든 구조에 같아야 하는 참조 문서 패턴 (섹션 밖 절대 경로 가능)
PROFILE_FILES = set()  # 이 섹션에 apply 로 적용한 구조 파일 (load_profile_mark 가 채움)
SETUP_EXAMPLE = {
    "baseline": "",
    "ai_files": [],
    "fixed_refs": [],
    "criteria": {"rate_drop_max": 0, "token_cut_min": 10, "time_cut_min": 0, "min_tasks": 5},
    "structures": {
        "예: B": {"display": "공용 규칙·검색·검증", "agent_skills": {"메인 세션": "통합 검색, 격리 빌드 검증"}}
    },
}


# ---------------------------------------------------------------- 공통
def now_iso():
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_ts(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def run_cmd(cmd, cwd, timeout, encoding=None):
    """명령 실행 → (성공 여부, stdout, stderr). git 은 UTF-8, svn 은 시스템 인코딩으로 읽는다."""
    try:
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                           encoding=encoding, errors="replace")
        return r.returncode == 0, r.stdout, r.stderr.strip()
    except Exception as e:
        return False, "", str(e)


def git_run(args, cwd, timeout=10):
    # core.quotePath=false: 한글 경로를 "\355\225\234" 처럼 이스케이프하지 않고 그대로 받는다
    return run_cmd(["git", "-c", "core.quotePath=false"] + args, cwd, timeout, "utf-8")


def git(args, cwd):
    ok, out, _ = git_run(args, cwd)
    return out.rstrip() if ok else None


def svn_run(args, cwd, timeout=20):
    return run_cmd(["svn"] + args, cwd, timeout)


def svn(args, cwd):
    ok, out, _ = svn_run(args, cwd)
    return out.rstrip() if ok else None


def vcs(cwd):
    if git(["rev-parse", "--is-inside-work-tree"], cwd) == "true":
        return "git"
    if svn(["info", "--show-item", "wc-root"], cwd):
        return "svn"
    return None


def find_pointer(cwd):
    p = Path(cwd).resolve()
    for q in [p, *list(p.parents)[:3]]:
        f = q / DATA_POINTER
        if f.is_file():
            t = f.read_text(encoding="utf-8").strip()
            if t:
                return Path(t)
    return None


def repo_root(cwd="."):
    """원본 저장소 루트 (worktree 여도 공통)."""
    common = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd)
    if common:
        p = Path(common)
        return p.parent if p.name == ".git" else p
    top = git(["rev-parse", "--show-toplevel"], cwd)
    return Path(top) if top else None


def get_dir(arg=None, cwd="."):
    if arg:
        d = Path(arg)
    elif os.environ.get("MES_DIR"):
        d = Path(os.environ["MES_DIR"])
    elif find_pointer(cwd):
        d = find_pointer(cwd)
    else:
        root = repo_root(cwd)
        # git: 원본 저장소/.mes (worktree 공유) · SVN/기타: 섹션 폴더들 옆 공용 폴더
        d = (root / ".mes") if root else Path(cwd).resolve().parent / "mes-compare-data"
    d.mkdir(parents=True, exist_ok=True)
    return d.resolve()


def load_json(p, default):
    try:
        # utf-8-sig: PowerShell 5.1 등이 붙이는 BOM 이 있어도 읽는다
        return json.loads(Path(p).read_text(encoding="utf-8-sig"))
    except Exception:
        return default


class events_lock:
    """events.jsonl 쓰기 잠금. 여러 섹션의 훅이 동시에 쓰면 긴 줄이 서로 섞여 깨지므로 잠금 파일로 순서를 맞춘다."""

    def __init__(self, d):
        self.p = Path(d) / "events.lock"

    def __enter__(self):
        import time
        self.f = open(self.p, "a+b")
        for _ in range(400):  # 최대 약 20초
            try:
                if os.name == "nt":
                    import msvcrt
                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                time.sleep(0.05)
        return self  # 잠금을 못 얻어도 기록은 남긴다

    def __exit__(self, *a):
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self.f.close()


def append(d, ev):
    ev.setdefault("ts", now_iso())
    line = (json.dumps(ev, ensure_ascii=False) + chr(10)).encode("utf-8")
    with events_lock(d):
        with open(Path(d) / "events.jsonl", "ab") as f:
            f.write(line)  # 한 번에 쓴다


def cur_key(sect):
    """섹션별 진행 중 실행 파일 이름에 쓰는 짧은 키 (섹션마다 실행을 따로 둬 여러 섹션을 동시에 돌릴 수 있게)."""
    import hashlib
    return hashlib.sha1(str(Path(sect).resolve()).lower().encode("utf-8")).hexdigest()[:10]


def cur_path(d, sect):
    return Path(d) / f"current-{cur_key(sect)}.json"


def current_all(d):
    """진행 중인 실행 전부 (섹션별 파일 + 예전 단일 파일)."""
    out = []
    for p in sorted(Path(d).glob("current-*.json")) + [Path(d) / "current.json"]:
        c = load_json(p, None)
        if c:
            c["_path"] = str(p)
            out.append(c)
    return out


def current(d, sect=None):
    """sect 를 주면 그 섹션의 진행 중 실행, 없으면 첫 번째 것."""
    for c in current_all(d):
        if sect is None:
            return c
        try:
            Path(sect).resolve().relative_to(Path(c.get("section") or "").resolve())  # 섹션 안 하위 폴더도 같은 섹션
            return c
        except ValueError:
            continue
    return None


def save_current(cur):
    p = Path(cur.pop("_path"))
    p.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
    cur["_path"] = str(p)


def read_events(d):
    out = []
    p = Path(d) / "events.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def front_name(p):
    try:
        txt = p.read_text(encoding="utf-8", errors="ignore")
        m = re.match(r"^---\s*\n(.*?)\n---", txt, re.S)
        if m:
            n = re.search(r"^name:\s*['\"]?([^'\"\n]+)", m.group(1), re.M)
            if n:
                return n.group(1).strip(), txt
        return None, txt
    except Exception:
        return None, ""


# ---------------------------------------------------------------- 구조 스냅샷
def snapshot(section):
    """섹션의 AI 사용 구조만 읽는다 (코드·문서는 제외)."""
    sec = Path(section).resolve()
    h = hashlib.sha1()
    agents, skills, files, plugins = [], [], [], []

    def add_file(p, listed=True):
        rel = p.relative_to(sec).as_posix()
        if listed:
            files.append(rel)
        try:
            h.update(rel.encode())
            h.update(p.read_bytes())
        except Exception:
            pass

    for p in sorted((sec / ".claude" / "agents").glob("**/*.md")):
        n, _ = front_name(p)
        agents.append(n or p.stem)
        add_file(p, False)
    for p in sorted((sec / ".claude" / "skills").glob("*/SKILL.md")):
        n, _ = front_name(p)
        skills.append(n or p.parent.name)
        add_file(p, False)
        for extra in sorted(p.parent.rglob("*")):
            if extra.is_file() and extra != p:
                try:
                    h.update(extra.relative_to(sec).as_posix().encode())
                    h.update(extra.read_bytes())
                except Exception:
                    pass
    for p in sorted((sec / ".claude" / "commands").glob("**/*.md")):
        add_file(p)
    for p in sorted((sec / ".claude" / "rules").glob("**/*")):
        if p.is_file():
            add_file(p)
    for name in ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md", ".mcp.json"):
        p = sec / name
        if p.is_file():
            add_file(p)
    for name in (".claude/settings.json",):
        cfg = load_json(sec / name, {})
        for k, v in (cfg.get("enabledPlugins") or {}).items():
            if v:
                plugins.append(k)
        h.update(json.dumps({k: cfg.get(k) for k in ("enabledPlugins", "permissions", "model")}, sort_keys=True).encode())
    for pat in ("CLAUDE.md", "CLAUDE.local.md"):
        for p in sorted(sec.rglob(pat)):
            rel = p.relative_to(sec).as_posix()
            if rel not in files and not rel.startswith(SKIP_DIRS):
                add_file(p)
    # setup.json ai_files: 프로젝트가 정한 구조 파일 (예: 하위 폴더의 AGENTS.md)
    for g in AI_GLOBS:
        if Path(g).is_absolute():
            continue
        for p in sorted(sec.glob(g)):
            rel = p.relative_to(sec).as_posix()
            if p.is_file() and rel not in files and not rel.startswith(SKIP_DIRS):
                add_file(p)
    # CLAUDE.md 등이 @경로 로 불러오는 파일도 구조에 포함 (불러온 파일 안의 @import 도 따라감)
    imports, seen = [], {(sec / f).resolve() for f in files}
    for f in [f for f in files if f.rsplit("/", 1)[-1] in ("CLAUDE.md", "CLAUDE.local.md")]:
        for q in md_imports(sec / f, seen):
            try:
                rel, outside = q.relative_to(sec).as_posix(), False
            except ValueError:
                rel, outside = q.as_posix(), True
            try:
                data = q.read_bytes()
            except Exception:
                continue
            h.update(rel.encode())
            h.update(data)
            imports.append({"path": rel, "from": f, "outside": outside, "ai": outside or is_ai_path(rel),
                            "h": hashlib.sha1(data).hexdigest()[:8]})
            if rel not in files:
                files.append(rel)
    # apply 로 적용한 구조 묶음의 나머지 파일(도구 등)도 구조 지문에 포함
    mark = load_json(sec / PROFILE_MARK, {}) or {}
    for r in mark.get("files", []):
        p = sec / r
        if safe_rel(r) and r not in files and p.is_file() and not r.startswith(".claude/"):
            add_file(p)
    profile = mark.get("name", "")  # apply 로 적용한 구조 이름
    return {"section": str(sec), "section_name": sec.name, "agents": agents, "skills": skills,
            "files": files, "plugins": plugins, "imports": imports, "profile": profile, "fp": h.hexdigest()[:8]}


def md_imports(path, seen, depth=1):
    """md 파일의 @경로 import 를 따라가며 실제 있는 파일을 돌려준다.
    상대 경로는 그 파일 위치 기준, ~/ 는 홈 폴더. 코드 블록·인라인 코드 안의 @ 는 무시한다."""
    if depth > IMPORT_DEPTH:
        return
    for _, q in import_tokens(path):
        if q in seen:
            continue
        seen.add(q)
        yield q
        yield from md_imports(q, seen, depth + 1)


def import_tokens(path):
    """md 파일이 직접 불러오는 @경로 → [(원문 토큰, 실제 파일 경로)]. 실제 있는 파일만."""
    try:
        txt = Path(path).read_text(encoding="utf-8-sig", errors="ignore")
    except Exception:
        return []
    txt = re.sub(r"```.*?```", "", txt, flags=re.S)
    txt = re.sub(r"`[^`\n]*`", "", txt)
    out = []
    for m in IMPORT_RE.finditer(txt):
        raw = m.group(1).rstrip(".,;:!?")
        q = Path(os.path.expanduser(raw)) if raw.startswith("~") else Path(raw)
        if not q.is_absolute():
            q = Path(path).parent / q
        try:
            q = q.resolve()
            if q.is_file():
                out.append((raw, q))
        except Exception:
            continue
    return out


def configure(d):
    """기록 폴더의 setup.json 에서 파일 분류(ai_files, fixed_refs)를 읽는다."""
    s = load_json(Path(d) / "setup.json", {})
    if not isinstance(s, dict):
        return
    AI_GLOBS[:] = [str(g).replace("\\", "/") for g in (s.get("ai_files") or [])]
    FIXED_REFS[:] = [str(g).replace("\\", "/") for g in (s.get("fixed_refs") or [])]


def glob_match(p, g):
    # fnmatch 의 * 는 / 도 넘으므로 "**/X" 는 하위 폴더의 X, 앞의 "**/" 를 뗀 "X" 는 최상위 X
    return fnmatch.fnmatch(p, g) or (g.startswith("**/") and fnmatch.fnmatch(p, g[3:]))


def is_ai_path(p):
    p = p.strip().strip('"').replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    return (p.startswith(AI_PATHS) or "/.claude/" in "/" + p or name in ("CLAUDE.md", "CLAUDE.local.md")
            or any(glob_match(p, g) for g in AI_GLOBS) or p in PROFILE_FILES)


def load_profile_mark(section):
    """apply 로 적용한 구조 파일 목록을 AI 설정으로 취급 (reset·코드 지문에서 제외)."""
    PROFILE_FILES.clear()
    PROFILE_FILES.update(r for r in (load_json(Path(section) / PROFILE_MARK, {}) or {}).get("files", []) if safe_rel(r))


def refs_fingerprint(section):
    """setup.json fixed_refs 에 맞는 참조 문서들의 내용 지문. 설정이 없으면 None."""
    if not FIXED_REFS:
        return None
    found = set()
    for g in FIXED_REFS:
        pat = g if Path(g).is_absolute() else str(Path(section).resolve() / g)
        for f in globlib.glob(pat, recursive=True):
            if os.path.isfile(f):
                found.add(Path(f).resolve())
    h = hashlib.sha1()
    for f in sorted(found):
        try:
            h.update(f.as_posix().encode())
            h.update(f.read_bytes())
        except Exception:
            pass
    return {"fp": h.hexdigest()[:10] if found else "", "n": len(found)}


def svn_changes(section):
    """svn status → [(상태문자, 경로)]"""
    out = svn(["status"], section) or ""
    res = []
    for l in out.splitlines():
        if len(l) > 8 and l[0] in "MADR?!~C":
            res.append((l[0], l[8:].strip()))
    return res


def code_base(section):
    """참조 코드·문서 기준 지문 + AI 설정 외 수정 여부.
    git: AI 설정 파일을 뺀 커밋 내용 · SVN: 저장소 경로 + 리비전.
    섹션마다 AI 설정만 바꿔도 코드·문서가 같으면 같은 지문이 나온다."""
    if vcs(section) == "svn":
        url = svn(["info", "--show-item", "relative-url"], section) or ""
        try:
            ver = subprocess.run(["svnversion"], cwd=str(section), capture_output=True, text=True, timeout=30).stdout.strip()
        except Exception:
            ver = ""
        rev = re.sub(r"[MSP]", "", ver or "")
        dirty = [p for c, p in svn_changes(section) if not is_ai_path(p)]
        return {"vcs": "svn", "commit": f"r{rev}" if rev else "", "code_fp": hashlib.sha1(f"{url}@{rev}".encode()).hexdigest()[:10] if rev else "",
                "dirty": dirty[:20], "dirty_n": len(dirty), "mixed": ":" in rev}
    head = git(["rev-parse", "HEAD"], section)
    tree = git(["ls-tree", "-r", "HEAD"], section) or ""
    lines = [l for l in tree.splitlines() if "\t" in l and not is_ai_path(l.split("\t", 1)[1])]
    code_fp = hashlib.sha1("\n".join(lines).encode()).hexdigest()[:10] if lines else ""
    dirty = sorted({p for _, p, _ in git_changes(section)[1] if not is_ai_path(p)})
    return {"vcs": "git" if head else "", "commit": (head or "")[:12], "code_fp": code_fp, "dirty": dirty[:20], "dirty_n": len(dirty)}


def git_changes(section):
    """git status → (저장소 최상위, [(상태 XY, 경로, 새 파일 여부)]). 경로는 최상위 기준.
    -z 로 읽어 공백·한글·이름 바꾸기(R old→new)를 정확히 나눈다."""
    top = git(["rev-parse", "--show-toplevel"], section)
    ok, out, _ = git_run(["status", "--porcelain", "-z", "--untracked-files=all"], section)
    res = []
    if not top or not ok:
        return top, res
    items = out.split("\0")
    i = 0
    while i < len(items):
        it = items[i]
        i += 1
        if len(it) < 4:
            continue
        xy, p = it[:2], it[3:]
        if xy[0] in "RC" and i < len(items):
            orig = items[i]
            i += 1
            if xy[0] == "R" and orig:
                res.append((xy, orig, False))  # 이름 바꾸기 전 파일은 원래대로 복구
            res.append((xy, p, True))
        else:
            res.append((xy, p, xy == "??" or "A" in xy))
    return top, res


def delete_new(base, rel, errors):
    """새로 생긴 파일·폴더 삭제. 폴더면 안의 AI 설정 파일은 남기고 나머지만 지운 뒤 빈 폴더를 정리한다."""
    f = Path(base) / rel.replace("\\", "/").rstrip("/")
    try:
        if f.resolve() == Path(base).resolve():  # 섹션 폴더 자체는 절대 지우지 않는다
            return
        if f.is_dir():
            for q in sorted(f.rglob("*"), key=lambda x: len(x.parts), reverse=True):
                relq = q.relative_to(base).as_posix()
                if q.is_file() and not is_ai_path(relq):
                    q.unlink()
                elif q.is_dir() and not any(q.iterdir()):
                    q.rmdir()
            if not any(f.iterdir()):
                f.rmdir()
        elif f.exists():
            f.unlink()
        # 지운 뒤 비어 버린 상위 폴더 정리 (base 는 남김)
        par = f.parent
        while par != Path(base) and par.is_dir() and not any(par.iterdir()):
            par.rmdir()
            par = par.parent
    except Exception as e:
        errors.append(f"삭제 실패 {rel}: {e}")


def chunks(lst, n=100):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def reset_code(section, yes):
    """AI 설정은 남기고 코드·문서 변경만 되돌린다. 실패가 있으면 ValueError."""
    kind = vcs(section)
    base = Path(section)
    revert, delete, staged_new = [], [], []
    if kind == "git":
        top, ch = git_changes(section)
        if not top:
            raise ValueError("git 상태를 읽지 못함")
        base = Path(top)
        for xy, p, new in ch:
            if not p or is_ai_path(p):
                continue
            if new:
                delete.append(p)
                if xy != "??":
                    staged_new.append(p)
            else:
                revert.append(p)
    elif kind == "svn":
        for c, p in svn_changes(section):
            if is_ai_path(p):
                continue
            if c == "?":
                delete.append(p)
            elif c == "A":  # svn add 된 새 파일: 추가 취소 후 디스크에서도 삭제
                staged_new.append(p)
                delete.append(p)
            else:
                revert.append(p)
    else:
        raise ValueError("git/SVN 작업 폴더가 아니어서 되돌릴 수 없음")
    revert, delete, staged_new = (sorted(set(x)) for x in (revert, delete, staged_new))
    print(f"되돌릴 파일 {len(revert)}개 · 지울 새 파일 {len(delete)}개 (AI 설정 파일은 유지)")
    for p in revert[:30]:
        print("  되돌림", p)
    for p in delete[:30]:
        print("  삭제  ", p)
    if not yes:
        print("미리보기입니다. 실제로 하려면 --yes 를 붙이세요.")
        return
    errors = []
    if kind == "git":
        for part in chunks(staged_new):
            ok, _, err = git_run(["rm", "-r", "--cached", "-q", "--ignore-unmatch", "--"] + part, base, 60)
            if not ok:
                errors.append(f"git rm --cached 실패: {err}")
        for part in chunks(revert):
            # 인덱스가 아니라 HEAD 에서 복구 (AI 가 스테이징한 변경도 되돌림)
            ok, _, err = git_run(["checkout", "HEAD", "--"] + part, base, 60)
            if not ok:
                errors.append(f"git checkout 실패: {err}")
    else:
        for part in chunks(staged_new):
            ok, _, err = svn_run(["revert", "--depth", "infinity", "--"] + part, base, 60)
            if not ok:
                errors.append(f"svn revert 실패: {err}")
        for part in chunks(revert):
            ok, _, err = svn_run(["revert", "--"] + part, base, 60)
            if not ok:
                errors.append(f"svn revert 실패: {err}")
    for p in delete:
        delete_new(base, p, errors)
    if errors:
        raise ValueError("일부를 되돌리지 못함:\n  " + "\n  ".join(errors))
    print("완료")


# ---------------------------------------------------------------- 구조 묶음 (capture / apply)
def internal_path(q):
    """섹션 밖 파일을 묶음 안에 둘 경로 (공통 상위 폴더를 못 찾을 때). C:/dev/a.md → .claude/imported/C/dev/a.md"""
    s = Path(q).as_posix()
    m = re.match(r"^([A-Za-z]):/(.*)$", s)
    return f"{INTERNAL_DIR}/{m.group(1).upper()}/{m.group(2)}" if m else f"{INTERNAL_DIR}/{s.lstrip('/')}"


def internal_map(paths):
    """섹션 밖 파일들 → 묶음 안 경로. 공통 상위 폴더 기준으로 짧게 두어(Windows 경로 길이 제한)
    파일끼리의 상대 @import 는 그대로 유지한다. 예: C:/dev/svn_mini/Document/AGENTS.md → .claude/imported/Document/AGENTS.md"""
    paths = sorted(set(paths))
    if not paths:
        return {}
    try:
        root = Path(os.path.commonpath([str(q.parent) for q in paths]))
    except ValueError:  # 드라이브가 다르면 공통 상위가 없다
        return {q: internal_path(q) for q in paths}
    name = root.name or (root.drive.rstrip(":") or "ext")
    return {q: f"{INTERNAL_DIR}/{name}/{q.relative_to(root).as_posix()}" for q in paths}


def compare_hook_entries(cfg):
    """settings 의 hooks 중 이 도구가 등록한 항목만 {이벤트: [항목]}"""
    out = {}
    for ev, lst in ((cfg or {}).get("hooks") or {}).items():
        mine = [x for x in (lst or []) if "mes_compare" in json.dumps(x, ensure_ascii=False)]
        if mine:
            out[ev] = mine
    return out


def strip_compare_hooks(cfg):
    hooks = cfg.get("hooks")
    if isinstance(hooks, dict):
        for ev in list(hooks):
            lst = [x for x in (hooks[ev] or []) if "mes_compare" not in json.dumps(x, ensure_ascii=False)]
            if lst:
                hooks[ev] = lst
            else:
                del hooks[ev]
        if not hooks:
            del cfg["hooks"]
    return cfg


def safe_rel(p):
    """묶음·기록 안의 경로가 섹션 밖을 가리키지 않는지 확인."""
    pp = Path(p)
    return bool(p) and not pp.is_absolute() and ".." not in pp.parts and not re.match(r"^[A-Za-z]:", p)


def tree_files(base, rel_dir=""):
    root = Path(base) / rel_dir
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(base).as_posix()
            if "/__pycache__/" not in "/" + rel and not rel.startswith(SKIP_DIRS):
                yield rel


def unversioned(section):
    """git/SVN 에 등록되지 않은 경로 (구조 파일 후보)."""
    kind = vcs(section)
    if kind == "svn":
        return [p.replace("\\", "/") for c, p in svn_changes(section) if c == "?"]
    if kind == "git":
        top, ch = git_changes(section)
        try:
            pre = Path(section).resolve().relative_to(Path(top).resolve()).as_posix() if top else "."
        except ValueError:
            pre = "."
        out = []
        for xy, p, _ in ch:
            if xy == "??":
                if pre not in (".", ""):
                    if not p.startswith(pre + "/"):
                        continue
                    p = p[len(pre) + 1:]
                out.append(p)
        return out
    return []


def rewrite_imports(src_path, new_rel, data, outside, out):
    """복사한 md 안의 '섹션 밖 @경로' 를 묶음 안 복사본을 가리키는 상대 경로로 바꾼다."""
    if not new_rel.lower().endswith(".md"):
        return data
    bom = data.startswith(b"\xef\xbb\xbf")
    try:
        txt = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data
    changed = False
    for raw, q in import_tokens(src_path):
        tgt = outside.get(q)
        if tgt is None:
            continue
        relp = os.path.relpath(out / tgt, (out / new_rel).parent).replace("\\", "/")
        txt = txt.replace("@" + raw, "@" + relp)
        changed = True
    if not changed:
        return data
    return (b"\xef\xbb\xbf" if bom else b"") + txt.encode("utf-8")


def capture_profile(section, out, includes, yes):
    """섹션의 구조 파일을 경로 그대로 묶음 폴더에 복사한다. 섹션 밖 @import 파일은 묶음 안으로 넣는다."""
    sec, out = Path(section).resolve(), Path(out).resolve()
    if out == sec or sec in out.parents:
        raise ValueError("묶음 폴더는 섹션 밖에 두세요")
    if out.exists() and any(x.name != ".git" for x in out.iterdir()):
        if not (out / PROFILE_MANIFEST).is_file():
            raise ValueError(f"{out} 는 비어 있지 않고 구조 묶음도 아님 (다른 폴더를 지정하세요)")
        if not yes:
            raise ValueError(f"{out} 에 이미 구조 묶음이 있음. 덮어쓰려면 --yes")
    sn = snapshot(sec)
    inside, missing = set(), []
    for f in sn["files"]:
        if not Path(f).is_absolute():
            inside.add(f)
    if (sec / ".claude").is_dir():
        inside.update(r for r in tree_files(sec, ".claude") if not r.endswith(".bak"))
    for inc in includes:
        rel = inc.replace("\\", "/").strip("/")
        p = sec / rel
        if not safe_rel(rel):
            raise ValueError(f"--include 는 섹션 안 상대 경로여야 함: {inc}")
        if p.is_file():
            inside.add(rel)
        elif p.is_dir():
            inside.update(tree_files(sec, rel))
        else:
            missing.append(inc)
    inside = {r for r in inside if not r.startswith(SKIP_DIRS) and r not in (DATA_POINTER, PROFILE_MARK)}
    outside = internal_map([Path(i["path"]).resolve() for i in sn["imports"] if i.get("outside")])

    if out.exists():  # 이전 묶음 내용 정리 (.git 은 유지)
        import shutil
        for x in out.iterdir():
            if x.name == ".git":
                continue
            shutil.rmtree(x) if x.is_dir() else x.unlink()
    entries = []

    def write(rel, data):
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        entries.append({"path": rel, "sha1": hashlib.sha1(data).hexdigest()[:12]})

    for rel in sorted(inside):
        src = sec / rel
        data = src.read_bytes()
        if rel == ".claude/settings.local.json":  # 비교 훅은 섹션마다 다시 설치하므로 묶음에서 뺀다
            try:
                cfg = strip_compare_hooks(json.loads(data.decode("utf-8-sig") or "{}"))
                data = json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8")
            except Exception:
                pass
        write(rel, rewrite_imports(src, rel, data, outside, out))
    for q, rel in sorted(outside.items()):
        write(rel, rewrite_imports(q, rel, q.read_bytes(), outside, out))
    manifest = {"name": out.name, "captured_from": str(sec), "captured_at": now_iso(), "tool_version": VERSION,
                "agents": sn["agents"], "skills": sn["skills"],
                "internalized": {q.as_posix(): r for q, r in outside.items()}, "files": entries}
    (out / PROFILE_MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"구조 묶음: {out}  ·  파일 {len(entries)}개 (섹션 안 {len(inside)} · 섹션 밖 @import {len(outside)})")
    for q, r in sorted(outside.items()):
        print(f"  섹션 밖 → 묶음 안: {q.as_posix()} → {r}")
    for m in missing:
        print("경고: --include 경로 없음:", m)
    cands, noise = [], 0
    for p in unversioned(sec):
        p = p.rstrip("/")
        if p in inside or any(r.startswith(p + "/") for r in inside) or p.startswith(SKIP_DIRS) \
                or p in (DATA_POINTER, PROFILE_MARK):
            continue
        if is_build_output(p):  # 빌드 산출물·캐시는 구조 후보가 아니다
            noise += 1
            continue
        cands.append(p)
    if cands:
        # 이름이 . 으로 시작하거나 md·toml·json 인 것(규칙·설정일 가능성이 큰 것)을 앞에 보여준다
        cands.sort(key=lambda p: (not (p.rsplit("/", 1)[-1].startswith(".") or p.lower().endswith((".md", ".toml", ".json"))), p))
        print(f"참고: 저장소에 등록되지 않은 파일·폴더 {len(cands)}개 — 구조에 필요하면 --include 로 넣으세요"
              + (f" (빌드 산출물·캐시 {noise}개는 생략)" if noise else ""))
        for p in cands[:30]:
            print("   ", p)


BUILD_DIRS = {"bin", "obj", ".vs", "packages", "node_modules", "runtimes", "testresults", "__pycache__", ".idea",
              "debug", "release", "x64", "x86"}


def is_build_output(p):
    parts = [x.lower() for x in p.replace("\\", "/").split("/")]
    return any(x in BUILD_DIRS for x in parts) or parts[-1].endswith(
        (".dll", ".pdb", ".exe", ".cache", ".user", ".suo", ".log", ".tmp", ".bak", ".zip"))


def apply_profile(section, src):
    """구조 묶음을 섹션에 적용할 계획 → (이름, 쓸 파일, 지울 파일)."""
    sec, src = Path(section).resolve(), Path(src).resolve()
    man = load_json(src / PROFILE_MANIFEST, None)
    if not isinstance(man, dict):
        raise ValueError(f"{src} 에 {PROFILE_MANIFEST} 가 없음 (capture 로 만든 구조 묶음을 지정하세요)")
    # 묶음을 손으로 고쳐 새 구조를 만들 수 있으므로 목록이 아니라 실제 폴더 내용을 적용한다
    new = [r for r in tree_files(src) if r != PROFILE_MANIFEST]
    bad = [r for r in new if not safe_rel(r)]
    if bad:
        raise ValueError(f"묶음에 섹션 밖을 가리키는 경로가 있음: {bad[:3]}")
    prev = [r for r in (load_json(sec / PROFILE_MARK, {}) or {}).get("files", []) if safe_rel(r)]
    drop = [r for r in prev if r not in set(new)]
    return man.get("name") or src.name, new, drop


def do_apply(section, src, yes):
    sec, src = Path(section).resolve(), Path(src).resolve()
    name, new, drop = apply_profile(sec, src)
    over = [r for r in new if (sec / r).exists()]
    print(f"구조 '{name}' 적용: 쓸 파일 {len(new)}개 (덮어씀 {len(over)}) · 이전 구조에서 지울 파일 {len(drop)}개")
    for r in new[:30]:
        print("  쓰기  ", r, "(덮어씀)" if r in over else "")
    for r in drop[:30]:
        print("  지우기", r)
    if not yes:
        print("미리보기입니다. 실제로 하려면 --yes 를 붙이세요. (바뀌는 파일은 먼저 백업)")
        return
    import shutil
    bdir = sec / PROFILE_BACKUP / dt.datetime.now().strftime("%Y%m%d-%H%M%S")

    def backup(r):
        f = sec / r
        if f.is_file():
            (bdir / r).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, bdir / r)

    keep = {}
    st_p = sec / ".claude" / "settings.local.json"
    if st_p.is_file():
        try:
            keep = compare_hook_entries(json.loads(st_p.read_text(encoding="utf-8-sig") or "{}"))
        except Exception:
            keep = {}
    for r in drop:
        backup(r)
        f = sec / r
        if f.is_file():
            f.unlink()
            par = f.parent
            while par != sec and par.is_dir() and not any(par.iterdir()):
                par.rmdir()
                par = par.parent
    for r in new:
        backup(r)
        data = (src / r).read_bytes()
        if r == ".claude/settings.local.json" and keep:  # 설치돼 있던 비교 훅은 유지
            try:
                cfg = json.loads(data.decode("utf-8-sig") or "{}")
                hooks = cfg.setdefault("hooks", {})
                for ev, lst in keep.items():
                    cur = hooks.setdefault(ev, [])
                    cur.extend(x for x in lst if x not in cur)
                data = json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8")
            except Exception:
                pass
        dst = sec / r
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
    if keep and ".claude/settings.local.json" not in new:  # 묶음에 설정이 없으면 비교 훅만 남긴 설정을 둔다
        cfg = {"hooks": keep}
        st_p.parent.mkdir(parents=True, exist_ok=True)
        st_p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    (sec / PROFILE_MARK).write_text(json.dumps({"name": name, "source": str(src), "applied_at": now_iso(),
                                                "files": new}, ensure_ascii=False, indent=1), encoding="utf-8")
    load_profile_mark(sec)  # 방금 적용한 파일을 구조 파일로 보고 경고를 계산
    sn = snapshot(sec)
    print(f"적용 완료: 구조 {name} · 에이전트 {len(sn['agents'])} · 스킬 {len(sn['skills'])} · 규칙 파일 {len(sn['files'])}"
          f" · 지문 {sn['fp']}" + (f" · 백업 {bdir}" if bdir.exists() else ""))
    warn_imports(sn)


# ---------------------------------------------------------------- 상태 계산
def front_desc(p):
    """SKILL.md·에이전트 md 머리말의 description (여러 줄 > | 형식 포함)."""
    try:
        txt = p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    m = re.match(r"^---\s*\n(.*?)\n---", txt, re.S)
    if not m:
        return ""
    lines = m.group(1).splitlines()
    for i, ln in enumerate(lines):
        mm = re.match(r"^description:\s*(.*)$", ln)
        if not mm:
            continue
        v = mm.group(1).strip()
        if v in (">", "|", ">-", "|-", ""):
            v = " ".join(x.strip() for x in lines[i + 1:] if x.startswith((" ", "\t")))
        return v.strip("'\"")
    return ""


def struct_desc(section):
    """섹션의 에이전트·스킬 설명 {'ag:이름'|'sk:이름': 설명}."""
    sec, out = Path(section or ""), {}
    if not section or not sec.is_dir():
        return out
    for p in sorted((sec / ".claude" / "agents").glob("**/*.md")):
        n, _ = front_name(p)
        out["ag:" + (n or p.stem)] = front_desc(p)
    for p in sorted((sec / ".claude" / "skills").glob("*/SKILL.md")):
        n, _ = front_name(p)
        out["sk:" + (n or p.parent.name)] = front_desc(p)
    return out


SG_CACHE = {}
SG_EXT = (".md", ".json", ".py", ".yml", ".yaml", ".ps1", ".sh", ".txt")
SG_INDEX = ("목차.md", "index.md", "readme.md", "지도.md", "claude.md", "agents.md")


def sg_kind(rel):
    low, name = rel.lower(), rel.rsplit("/", 1)[-1].lower()
    if name in ("claude.md", "claude.local.md", "agents.md"):
        return "지침"
    if re.match(r"^\.claude/skills/[^/]+/skill\.md$", low):
        return "스킬"
    if low.startswith(".claude/agents/"):
        return "에이전트"
    if name in ("settings.json", ".mcp.json"):
        return "훅·설정"
    if low.endswith((".py", ".ps1", ".sh")):
        return "도구"
    if low.endswith((".json", ".yml", ".yaml")) or "/rules/" in low or "/검사/" in low:
        return "검사 규칙"
    return "지식·문서"


def struct_graph(section, listed):
    """구조 파일들(지침·스킬·에이전트·지식·규칙·도구·훅)과 서로 참조하는 관계. 코드·문서 원본은 넣지 않는다."""
    sec = Path(section or "")
    if not section or not sec.is_dir():
        return None
    rels = {r for r in (listed or []) if not re.match(r"^([A-Za-z]:|/)", r)}
    cl = sec / ".claude"
    if cl.is_dir():
        for p in cl.rglob("*"):
            if p.is_file() and p.suffix.lower() in SG_EXT and not {"imported", "__pycache__"} & set(p.parts) \
                    and p.name not in ("settings.local.json", "검증결과.md") and not p.name.endswith(".bak"):
                rels.add(p.relative_to(sec).as_posix())
    rels = sorted(rels)[:300]
    sig = tuple((r, (sec / r).stat().st_mtime if (sec / r).exists() else 0) for r in rels)
    if SG_CACHE.get(str(sec), (None,))[0] == sig:
        return SG_CACHE[str(sec)][1]
    desc = struct_desc(section)
    nodes, texts = [], {}
    for r in rels:
        k = sg_kind(r)
        m = re.match(r"^\.claude/skills/([^/]+)/", r)
        label = m.group(1) if k == "스킬" else Path(r).stem if k == "에이전트" else r.rsplit("/", 1)[-1]
        if k in ("스킬", "에이전트"):
            n, _ = front_name(sec / r)
            label = n or label
        d = desc.get(("sk:" if k == "스킬" else "ag:") + label, "") if k in ("스킬", "에이전트") else ""
        nodes.append({"id": r, "kind": k, "label": label, "desc": d})
        try:
            texts[r] = (sec / r).read_text(encoding="utf-8", errors="ignore")[:200000]
        except Exception:
            texts[r] = ""
    edges = set()
    for a in nodes:
        t = texts.get(a["id"], "")
        a_index = a["id"].rsplit("/", 1)[-1].lower() in SG_INDEX
        for b in nodes:
            if b is a or not t:
                continue
            bid, bname = b["id"], b["id"].rsplit("/", 1)[-1]
            toks = [bid]
            if bname.lower() not in ("skill.md", "claude.md", "agents.md", "settings.json"):
                toks.append(bname)
            if b["kind"] in ("스킬", "에이전트"):
                toks.append(b["label"])
            if a_index and b["kind"] == "지식·문서" and len(Path(bname).stem) >= 2:
                toks.append(Path(bname).stem)  # 목차·지도는 확장자 없이 주제 이름으로 가리킨다
            if any(x and x in t for x in toks):
                edges.add((a["id"], bid))
        m = re.match(r"^(\.claude/skills/[^/]+)/(?!SKILL\.md$)", a["id"])
        if m and f"{m.group(1)}/SKILL.md" in texts:  # 스킬 부속 파일은 그 스킬에 붙인다
            edges.add((f"{m.group(1)}/SKILL.md", a["id"]))
    res = {"nodes": nodes, "edges": sorted(edges)}
    SG_CACHE[str(sec)] = (sig, res)
    return res


def short_path(name):
    """최근 활동에 보일 짧은 이름 (폴더/파일)."""
    p = str(name or "").replace("\\", "/").rstrip("/")
    parts = p.split("/")
    return "/".join(parts[-2:]) if len(parts) > 2 else p


def is_temp_path(name):
    p = str(name or "").replace("\\", "/").lower()
    return bool(re.match(r"^([a-z]:)?/", p)) and any(k in p for k in ("/temp/", "/tmp/", "scratchpad", "side-session-notes", "/appdata/"))


def build_state(d, detail=False):
    d = Path(d)
    setup = load_json(d / "setup.json", {})
    if isinstance(setup, list):  # 1.x 형식 호환
        setup = {"structures": {r.get("method"): r for r in setup if r.get("method")}}
    events = read_events(d)
    rows, extra, plans = {}, {}, {}
    fps, snaps, first_seen = {}, {}, {}

    for ev in events:
        t = ev.get("type")
        rid = ev.get("run_id")
        if t == "plan":
            plans[(ev.get("project", ""), ev["task_id"], ev["structure"])] = ev
            continue
        if t == "run_start":
            sn = ev.get("snapshot") or {}
            name = ev["structure"]
            fp = sn.get("fp", "-")
            fps.setdefault(name, [])
            if fp not in fps[name]:
                fps[name].append(fp)
            snaps[(name, fp)] = sn
            first_seen.setdefault(name, ev["ts"])
            base = MAIN_AGENT
            rows[rid] = {
                "run_id": rid, "rep": ev.get("rep", 1), "project": ev.get("project", ""),
                "project_name": ev.get("project_name") or ev.get("project", ""), "task_id": ev["task_id"],
                "task": ev.get("task", ev["task_id"]), "area": ev.get("area", ""), "risk": ev.get("risk", "일반"),
                "structure_name": name, "fp": fp, "section": sn.get("section_name", ""),
                "commit": (ev.get("code") or {}).get("commit", ""), "code_fp": (ev.get("code") or {}).get("code_fp", ""),
                "dirty_n": (ev.get("code") or {}).get("dirty_n", 0),
                "refs_fp": (ev.get("refs") or {}).get("fp", ""),
                "status": "진행 중", "final": "진행 중", "fail_reason": "", "tokens": None, "minutes": None,
                "rework": 0, "agent": base, "skills": "", "files": "", "agent_skills": [], "agent_files": [],
                "skill_called": "아니오" if sn.get("skills") else "해당 없음", "review": "해당 없음",
                "checks_passed": None, "checks_total": ev.get("checks_total"), "judged_by": "", "judged_at": "",
                "ai_note": "", "sessions": 0, "wrong_section": False, "started": ev["ts"], "ended": ""}
            extra[rid] = {"last_ts": ev["ts"], "skills": [], "files": [], "agents": [base], "tokens": None,
                          "skill": None, "file": None, "agent": base, "base": base, "sessions": set(), "recent": []}
            continue
        if rid not in rows:
            continue
        r, x = rows[rid], extra[rid]
        if t == "judge":
            if r["status"] in ("판정 대기", "완료"):
                r.update(status="완료", final=ev["final"], fail_reason=ev.get("fail_reason", ""),
                         judged_by=ev.get("by", ""), judged_at=ev["ts"])
            continue
        x["last_ts"] = ev.get("ts", x["last_ts"])
        if ev.get("session"):
            x["sessions"].add(ev["session"])
            sc = x.setdefault("scount", {})
            sc[ev["session"]] = sc.get(ev["session"], 0) + 1
        who = ev.get("agent") or x["agent"] or x["base"]
        if t in ("skill", "file", "agent"):
            key = {"skill": "sk:", "file": "fi:", "agent": "ag:"}[t] + str(ev.get("name", ""))
            x["recent"] = [k for k in x["recent"] if k != key][-11:] + [key]  # 최근에 쓴 것 12개
        if t == "skill":
            x["skill"] = ev["name"]
            if ev["name"] not in x["skills"]:
                x["skills"].append(ev["name"])
            if [who, ev["name"]] not in r["agent_skills"]:
                r["agent_skills"].append([who, ev["name"]])
            r["skill_called"] = "예"
        elif t == "file":
            x["file"] = ev["name"]
            if ev["name"] not in x["files"]:
                x["files"].append(ev["name"])
            if [who, ev["name"]] not in r["agent_files"]:
                r["agent_files"].append([who, ev["name"]])
        elif t == "agent":
            x["agent"] = ev["name"]
            if ev["name"] not in x["agents"]:
                x["agents"].append(ev["name"])
            if ev.get("review"):
                r["review"] = "예"
        elif t == "agent_end":
            x["agent"] = x["base"]
        elif t == "tokens":
            x["tokens"] = ev.get("total")
        elif t == "note":
            r["ai_note"] = ev.get("name", "")
        elif t == "wrong_section":
            r["wrong_section"] = True
        elif t == "run_end":
            r.update(status="판정 대기", final="판정 대기", ended=ev["ts"])
            for k in ("checks_passed", "checks_total", "rework"):
                if ev.get(k) is not None:
                    r[k] = ev[k]
            if ev.get("note"):
                r["ai_note"] = ev["note"]
            r["tokens"] = ev.get("tokens", x["tokens"])
            s, e = parse_ts(r["started"]), parse_ts(ev["ts"])
            r["minutes"] = ev.get("minutes", round((e - s).total_seconds() / 60, 1) if s and e else None)
        r["skills"] = ", ".join(x["skills"])
        r["files"] = ", ".join(x["files"])
        r["agent"] = ", ".join(x["agents"])
        r["sessions"] = len(x["sessions"])
        if r["status"] == "진행 중":
            r["tokens"] = x["tokens"]

    # 구조 버전 키
    def skey(name, fp):
        lst = fps.get(name, [fp])
        return name if len(lst) <= 1 else f"{name} v{lst.index(fp) + 1}"

    for r in rows.values():
        r["structure"] = skey(r["structure_name"], r["fp"])
    sconf = setup.get("structures") or {}
    structures = []
    for name in sorted(fps, key=lambda n: first_seen.get(n, "")):
        for fp in fps[name]:
            sn = snaps.get((name, fp), {})
            c = sconf.get(name, {})
            structures.append({"key": skey(name, fp), "name": name, "fp": fp, "display": c.get("display") or (f"구조 {sn['profile']}" if sn.get("profile") else ""),
                               "agents": sn.get("agents", []), "skills": sn.get("skills", []),
                               "desc": struct_desc(sn.get("section")) if detail else {},
                               "graph": struct_graph(sn.get("section"), sn.get("files")) if detail else None,
                               "files": sn.get("files", []), "plugins": sn.get("plugins", []),
                               "section": sn.get("section_name", ""), "agent_skills": c.get("agent_skills") or {},
                               "versions": len(fps[name]), "imports": sn.get("imports", [])})
    for (proj, task, sname), ev in plans.items():
        if any(r["project"] == proj and r["task_id"] == task and r["structure_name"] == sname for r in rows.values()):
            continue
        rid = f"{proj}/{task}:{sname}#plan" if proj else f"{task}:{sname}#plan"
        rows[rid] = {"run_id": rid, "rep": 0, "project": ev.get("project", ""), "project_name": ev.get("project_name", ""),
                     "task_id": task, "task": ev.get("task", task), "area": ev.get("area", ""), "risk": ev.get("risk", "일반"),
                     "structure": sname, "structure_name": sname, "fp": "", "section": "", "commit": "", "dirty_n": 0,
                     "status": "대기", "final": "대기", "fail_reason": "", "tokens": None, "minutes": None, "rework": 0,
                     "agent": "", "skills": "", "files": "", "agent_skills": [], "agent_files": [], "skill_called": "대기",
                     "review": "해당 없음", "checks_passed": None, "checks_total": ev.get("checks_total"),
                     "judged_by": "", "judged_at": "", "ai_note": "", "sessions": 0, "wrong_section": False,
                     "started": "", "ended": ""}

    now = dt.datetime.now(dt.timezone.utc)
    live = []
    for rid, r in rows.items():
        x = extra.get(rid)
        if r["status"] != "진행 중" or not x:
            continue
        lt = parse_ts(x["last_ts"])
        live.append({"run_id": rid, "project": r["project"], "task_id": r["task_id"], "task": r["task"],
                     "structure": r["structure"], "section": r["section"], "agent": x["agent"], "skill": x["skill"],
                     "file": x["file"], "started": r["started"], "last_ts": x["last_ts"], "tokens": x["tokens"],
                     "skills_n": len(x["skills"]), "files_n": len(x["files"]), "sessions": r["sessions"],
                     "wrong_section": r["wrong_section"], "recent": x["recent"],
                     "stale": bool(lt and (now - lt).total_seconds() > STALE_MIN * 60)})

    sname_of = {rid: r["structure"] for rid, r in rows.items()}
    labels = {"run_start": "시작", "run_end": "AI 종료", "judge": "판정", "skill": "스킬", "file": "파일",
              "agent": "에이전트", "agent_end": "에이전트 종료", "tokens": "토큰", "note": "메모",
              "wrong_section": "⚠ 섹션 불일치", "session": "세션"}
    # 최근 활동: 같은 실행의 연속된 파일·토큰 기록은 한 줄로 묶고, 중요한 기록(시작·종료·판정·경고·스킬·에이전트)을 구분한다
    feed, judged_seen = [], set()
    for ev in reversed(events):
        t = ev.get("type")
        if t in ("plan", "tokens"):  # 토큰 누적은 진행 카드·종료 기록에 나오므로 활동 목록에서는 뺀다
            continue
        rid = ev.get("run_id", "")
        last = feed[-1] if feed else None
        if t in ("file", "tokens") and last and last["run_id"] == rid and last["kind"] == t:
            if t == "file":
                last["n"] += 1
                last["ts0"] = ev.get("ts")
                if len(last["items"]) < 40:
                    last["items"].append(ev.get("name", ""))
                last["temp"] += is_temp_path(ev.get("name"))
            continue
        if len(feed) >= 40:
            break
        text = {"tokens": f"누적 {ev.get('total') or 0:,}", "run_start": f"{ev.get('task', '')}",
                "run_end": f"{(ev.get('minutes') or 0)}분 · 토큰 {ev.get('tokens') or 0:,}" + (f" · 자동 검증 {ev['checks_passed']}/{ev.get('checks_total', '—')}" if ev.get("checks_passed") is not None else ""),
                "judge": f"{ev.get('final', '')} · {ev.get('by', '')}" + (f" · {ev['fail_reason']}" if ev.get("fail_reason") else ""),
                "wrong_section": f"작업 위치 {ev.get('name', '')}"}.get(t, ev.get("name", ""))
        if ev.get("agent") and t == "skill" and ev["agent"] != MAIN_AGENT:
            text = f"{text} · {ev['agent']}"
        item = {"ts": ev.get("ts"), "ts0": ev.get("ts"), "run_id": rid, "kind": t, "type": labels.get(t, t), "text": text,
                "structure": sname_of.get(rid, ""), "key": t in ("run_start", "run_end", "judge", "wrong_section", "skill", "agent"),
                "n": 1, "items": [ev.get("name", "")] if t == "file" else [], "temp": int(is_temp_path(ev.get("name"))) if t == "file" else 0,
                "final": ev.get("final", ""), "old": False}
        if t == "judge":
            item["old"] = rid in judged_seen  # 같은 실행의 더 새 판정이 있으면 정정된 판정
            judged_seen.add(rid)
        feed.append(item)
    for f in feed:
        if f["kind"] == "file":
            names = [short_path(x) for x in f["items"] if not is_temp_path(x)]
            uniq = list(dict.fromkeys(names))
            f["text"] = (", ".join(uniq[:3]) + (f" 외 {len(uniq) - 3}개" if len(uniq) > 3 else "")) if uniq else "임시 파일"
            if f["temp"]:
                f["text"] += f" (임시 파일 {f['temp']}건 포함)" if uniq else f" {f['temp']}건"
            f["type"] = f"파일 {f['n']}건" if f["n"] > 1 else "파일"

    if detail:
        try:
            lives = {c.get("run_id"): c for c in current_all(d)}
        except Exception:
            lives = {}
        for rid, r in rows.items():
            x = extra.get(rid)
            if x and r["status"] != "대기":
                r["tok"] = run_detail(r, x.get("scount") or {}, lives.get(rid))

    base = setup.get("baseline") or ""
    keys = [s["key"] for s in structures]
    if base not in keys:
        base = next((s["key"] for s in structures if s["name"] == base), keys[0] if keys else "")
    crit = dict(SETUP_EXAMPLE["criteria"], **(setup.get("criteria") or {}))
    return {"meta": {"label": "", "generated_at": now.astimezone().strftime("%Y-%m-%d %H:%M:%S"), "version": VERSION},
            "runs": list(rows.values()), "structures": structures, "baseline": base, "criteria": crit,
            "live": live, "feed": feed, "stale_min": STALE_MIN}


def task_label(r):
    """작업 표시 이름: 프로젝트가 있으면 '프로젝트/작업ID'."""
    return f"{r['project']}/{r['task_id']}" if r.get("project") else r["task_id"]


def validate(state):
    errs, warns = [], []
    by_task, by_refs = {}, {}
    for r in state["runs"]:
        k = r["run_id"]
        if r["status"] in ("대기",):
            continue
        # 같은 작업 ID 라도 프로젝트가 다르면 다른 작업
        by_task.setdefault(task_label(r), set()).add(r.get("code_fp") or r.get("commit") or "?")
        if r["status"] == "완료" and r["final"] == "합격":
            for m in ("tokens", "minutes"):
                if r.get(m) is None:
                    warns.append(f"{k}: 합격인데 {m} 미측정 → 해당 지표 집계 제외")
        if r.get("skill_called") == "아니오" and r["status"] != "진행 중":
            warns.append(f"{k}: 구조에 스킬이 있는데 한 번도 호출되지 않음")
        if r.get("wrong_section"):
            warns.append(f"{k}: 다른 섹션(폴더)에서 작업한 기록이 있음")
        if (r.get("sessions") or 0) > 1:
            warns.append(f"{k}: 세션 {r['sessions']}개가 섞임")
        if r.get("dirty_n"):
            warns.append(f"{k}: 시작 시 코드·문서에 커밋 안 된 변경 {r['dirty_n']}건")
        if r["status"] == "판정 대기":
            warns.append(f"{k}: 사람 판정 대기")
        by_refs.setdefault(task_label(r), set()).add(r.get("refs_fp") or "")
    for t, cs in by_task.items():
        if len(cs) > 1:
            warns.append(f"{t}: 구조마다 코드·문서가 다름(AI 설정 제외 지문 {sorted(cs)}) → 참조 코드·문서를 같은 커밋으로 맞추세요")
    for t, cs in by_refs.items():
        cs.discard("")
        if len(cs) > 1:
            warns.append(f"{t}: 고정 참조 문서(fixed_refs)가 실행 사이에 바뀜(지문 {sorted(cs)}) → 비교 도중 참조 문서를 고치지 마세요")
    changed_names = set()
    for s in state["structures"]:
        if s["versions"] > 1 and s["name"] not in changed_names:  # 버전마다 같은 경고를 반복하지 않음
            changed_names.add(s["name"])
            warns.append(f"구조 {s['name']}: 비교 도중 구조가 바뀜 → {s['versions']}개 버전으로 나눠 집계")
        for w in import_warnings(s.get("imports") or []):
            warns.append(f"구조 {s['key']}: {w}")
    # 섹션 밖에서 불러오는 규칙이 모든 구조에 똑같으면 구조 차이를 만들지 않는다
    outs = [{(i["path"], i["h"]) for i in (s.get("imports") or []) if i.get("outside")} for s in state["structures"]]
    if len(outs) > 1:
        common = set.intersection(*outs)
        if common:
            names = ", ".join(sorted(p for p, _ in common)[:3]) + (" 외" if len(common) > 3 else "")
            warns.append(f"섹션 밖 공용 규칙 {len(common)}개({names})를 모든 구조가 똑같이 불러옴 → 이 파일들은 구조 차이에 영향 없음")
    return errs, warns


def import_warnings(imports):
    """섹션 안에서 @import 로 불러오지만 AI 설정으로 분류되지 않은 파일 → 코드·문서로 취급되는 문제."""
    bad = [i["path"] for i in imports if not i.get("outside") and not is_ai_path(i["path"])]
    if not bad:
        return []
    names = ", ".join(bad[:3]) + (" 외" if len(bad) > 3 else "")
    return [f"@import 로 불러오는 {names} 가 코드·문서로 취급됨 → setup.json ai_files 에 추가하세요"
            " (구조마다 다르면 코드 지문 경고가 나고 reset 이 되돌림)"]


def warn_imports(sn):
    for w in import_warnings(sn.get("imports") or []):
        print("경고:", w)
    outs = [i["path"] for i in (sn.get("imports") or []) if i.get("outside")]
    if outs:
        print(f"참고: 섹션 밖 규칙 {len(outs)}개를 불러옴 ({', '.join(outs[:3])}{' 외' if len(outs) > 3 else ''})"
              " → 모든 구조가 같은 파일을 불러오면 구조 차이에 영향 없음")


def render_html(state, live):
    st = dict(state)
    st["meta"] = dict(st["meta"], live=live)
    blob = json.dumps(st, ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.replace("__MES_DATA__", blob, 1)


# ---------------------------------------------------------------- 훅
def transcript_tokens(path, since):
    """대화 기록의 usage 합계.
    Claude Code 는 응답 하나를 내용 블록마다 따로 기록하면서 같은 usage 를 반복해 붙이므로
    message.id(+requestId) 가 같은 기록은 한 번만 센다 (마지막 값 사용)."""
    by_msg, total = {}, 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = parse_ts(rec.get("timestamp"))
                if since and ts and ts < since:
                    continue
                msg = rec.get("message") or {}
                u = msg.get("usage") or rec.get("usage") or {}
                n = sum(int(u.get(k) or 0) for k in TOKEN_FIELDS)
                if not n:
                    continue
                mid = msg.get("id")
                if mid:
                    by_msg[(mid, rec.get("requestId"))] = n
                else:
                    total += n
    except Exception:
        return None
    return total + sum(by_msg.values())


def run_tokens(cur):
    """진행 중 실행의 토큰 = 실행을 시작한 세션의 대화 기록 + 하위 에이전트 대화 기록 (시작 이후)."""
    since = parse_ts(cur.get("started"))
    tp = cur.get("transcript_path")
    main_t = transcript_tokens(tp, since) if tp else None
    subs = cur.get("subagent_tokens") or {}
    if not isinstance(subs, dict):  # 이전 형식(누적 정수) 호환
        subs = {"_": int(subs)}
    if main_t is None and not subs:
        return None
    return (main_t or 0) + sum(subs.values())


# ---------------------------------------------------------------- 토큰 상세
# 모델은 호출마다 그때까지의 대화 전체를 다시 읽는다. 호출 k 의 문맥 크기(입력+캐시 쓰기+캐시 읽기)에서
# 직전 호출의 문맥 크기를 빼면 그 사이에 붙은 내용(도구 결과·AI 답변·훅 알림)의 실제 토큰 수가 된다.
# 이 차이를 그 사이 항목들에 글자 수 비율로 나누고, 각 항목이 이후 몇 번의 호출에서 다시 읽혔는지 곱해
# '어디에 토큰이 쓰였나'를 나눈다. 나눈 값의 합 = 실제 토큰 합계.
TX_CACHE, TXD_CACHE, SID_PATH = {}, {}, {}
TOOL_CATS = (("파일 읽기", ("Read",)), ("검색", ("Grep", "Glob")), ("명령 실행", ("Bash", "PowerShell")),
             ("파일 수정", ("Edit", "MultiEdit", "Write", "NotebookEdit")), ("스킬", ("Skill",)),
             ("하위 에이전트", ("Agent", "Task")))


def tool_cat(name):
    return next((c for c, ns in TOOL_CATS if name in ns), "기타 도구")


def tool_label(name, ti, cwd):
    v = (ti.get("file_path") or ti.get("notebook_path") or ti.get("skill") or ti.get("pattern")
         or ti.get("command") or ti.get("subagent_type") or ti.get("description") or ti.get("url") or "")
    v = " ".join(str(v).split())
    if ti.get("file_path") or ti.get("notebook_path"):
        try:
            v = Path(v).resolve().relative_to(Path(cwd).resolve()).as_posix() if cwd else v
        except Exception:
            pass
        v = v if len(v) <= 90 else "…" + v[-89:]  # 파일은 끝(파일 이름)을 남긴다
    else:
        v = v[:90]
    return f"{name} {v}".strip()


def blen(c):
    """내용 블록의 글자 수 (그림은 대략 4,000자로 셈)."""
    if c is None:
        return 0
    if isinstance(c, str):
        return len(c)
    if isinstance(c, list):
        return sum(blen(x) for x in c)
    if isinstance(c, dict):
        if c.get("type") == "image":
            return 4000
        for k in ("text", "thinking", "content"):
            if c.get(k) is not None:
                return blen(c[k])
        if c.get("input") is not None:
            return len(json.dumps(c["input"], ensure_ascii=False))
        return 0
    return len(str(c))


def tx_epoch(ts):
    t = parse_ts(ts)
    return t.timestamp() if t else None


def tx_find(sid):
    if sid in SID_PATH:
        return SID_PATH[sid]
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")) / "projects"
    p = next(iter(root.glob(f"*/{sid}.jsonl")), None) if root.is_dir() else None
    if p:
        SID_PATH[sid] = p
    return p


def parse_tx(path):
    """대화 기록을 호출(call)과 항목(item) 순서로 읽는다. 파일이 바뀌지 않았으면 이전 결과를 쓴다."""
    path = Path(path)
    try:
        stt = path.stat()
    except OSError:
        return None
    key = (stt.st_mtime, stt.st_size)
    c = TX_CACHE.get(str(path))
    if c and c[0] == key:
        return c[1]
    calls, items, use, tools, cwd = [], [], {}, {}, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("isSidechain"):
                continue
            t, ts = rec.get("type"), tx_epoch(rec.get("timestamp"))
            cwd = cwd or rec.get("cwd")
            msg = rec.get("message") or {}
            if t == "assistant":
                mid = msg.get("id") or rec.get("uuid")
                u = msg.get("usage") or {}
                if mid not in use:
                    ctx = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                    calls.append((ts, mid, ctx, len(items)))  # items[:n] 까지가 이 호출의 문맥
                use[mid] = u
                for b in msg.get("content") or []:
                    if isinstance(b, dict):
                        if b.get("type") == "tool_use":
                            tools[b.get("id")] = (b.get("name") or "", b.get("input") or {})
                        items.append(("AI 생각·답변", None, blen(b)))
            elif t == "user":
                cnt = msg.get("content")
                for b in (cnt if isinstance(cnt, list) else [cnt]):
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        name, ti = tools.get(b.get("tool_use_id"), ("", {}))
                        items.append((tool_cat(name), tool_label(name, ti, cwd), blen(b.get("content"))))
                    elif b:
                        items.append(("요청·훅 알림", None, blen(b)))
            elif t in ("attachment", "system"):
                items.append(("요청·훅 알림", None, min(20000, len(json.dumps(rec.get("attachment") or rec.get("content") or "", ensure_ascii=False)))))
    res = {"calls": calls, "items": items, "use": use}
    TX_CACHE[str(path)] = (key, res)
    return res


def tx_detail(path, since, until):
    """대화 기록 한 개에서 [since, until] 사이 호출들의 토큰을 종류·용도별로 나눈다."""
    P = parse_tx(path)
    if not P or not P["calls"]:
        return None
    calls, items, use = P["calls"], P["items"], P["use"]
    s0 = since.timestamp() if since else None
    s1 = until.timestamp() if until else None
    sel = [c for c in calls if c[0] is not None and (s0 is None or c[0] >= s0) and (s1 is None or c[0] <= s1 + 5)]
    if not sel:
        return None
    K, base = len(sel), calls[0][2]
    kinds = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    for c in sel:
        u = use.get(c[1]) or {}
        kinds["input"] += int(u.get("input_tokens") or 0)
        kinds["cache_write"] += int(u.get("cache_creation_input_tokens") or 0)
        kinds["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
        kinds["output"] += int(u.get("output_tokens") or 0)
    start = sel[0][2]
    b0 = min(base, start)
    live = [["기본 문맥", None, b0, 0, None], ["이전 작업 이월", None, start - b0, 0, None]]
    done = []
    for j in range(1, K):
        prev, cur = sel[j - 1], sel[j]
        delta = cur[2] - prev[2]
        if delta < 0:  # 대화 압축: 이전 내용이 빠지고 요약이 들어감
            for it in live[1:]:
                it[4] = j
            done += live[1:]
            live = [live[0], ["대화 압축", "압축 요약", max(0, cur[2] - b0), j, None]]
            continue
        seg = items[prev[3]:cur[3]]
        tot = sum(x[2] for x in seg)
        if not tot:
            if delta:
                live.append(["기타 도구", None, delta, j, None])
            continue
        for cat, label, n in seg:
            if n:
                live.append([cat, label, delta * n / tot, j, None])
    parts, top, acts = {}, [], {}
    for it in done + live:
        cat, label, tok, k, end = it
        reads = (end if end is not None else K) - k
        cost = tok * reads
        parts[cat] = parts.get(cat, 0) + cost
        if label and cost:
            top.append((cost, label, cat, tok, reads))
        if label and cat != "대화 압축":  # 작업 흐름 그림용: 도구 호출 하나하나 (같은 대상은 횟수로)
            a = acts.setdefault(label, [cat, 0, 0])
            a[1] += 1
            a[2] += cost
    parts["출력 생성"] = kinds["output"]
    top.sort(reverse=True)
    total = sum(kinds.values())
    acts = sorted(([c, l, n, round(x)] for l, (c, n, x) in acts.items()), key=lambda v: -v[3])[:160]
    return {"calls": K, "base": base, "start": start, "max": max(c[2] for c in sel), "total": total, **kinds, "acts": acts,
            "parts": {k: round(v) for k, v in parts.items() if round(v)},
            "top": [{"label": l, "cat": c, "tok": round(t), "reads": r, "cost": round(x)} for x, l, c, t, r in top[:12]]}


def sub_totals(path, since, until):
    """하위 에이전트 대화 기록(<세션>/subagents/*.jsonl)의 토큰 합계."""
    out = {}
    d = Path(path).with_suffix("") / "subagents"
    if not d.is_dir():
        return out
    for p in d.glob("*.jsonl"):
        x = tx_detail(p, since, until)
        if x and x["total"]:
            out[p.stem] = x["total"]
    return out


def run_detail(r, sessions, cur=None):
    """실행 하나의 토큰 상세. 실행 중 기록이 가장 많은 세션의 대화 기록을 쓴다."""
    tp = (cur or {}).get("transcript_path") if cur and cur.get("run_id") == r["run_id"] else None
    if not tp and sessions:
        sid = max(sessions, key=sessions.get)
        tp = tx_find(sid)
    if not tp or not Path(tp).exists():
        return None
    since, until = parse_ts(r.get("started")), parse_ts(r.get("ended")) if r.get("ended") else None
    try:
        stt = Path(tp).stat()
        key = (str(tp), stt.st_mtime, stt.st_size, r.get("started"), r.get("ended"))
        if key in TXD_CACHE:
            return TXD_CACHE[key]
        x = tx_detail(tp, since, until)
        if x:
            subs = sub_totals(tp, since, until)
            if subs:
                x["parts"]["하위 에이전트"] = x["parts"].get("하위 에이전트", 0) + sum(subs.values())
                x["total"] += sum(subs.values())
                x["subs"] = subs
        TXD_CACHE[key] = x
        return x
    except Exception:
        return None


def run_hook(dir_arg):
    try:
        # Windows 의 기본 stdin 인코딩(cp949)으로 읽으면 한글이 든 입력이 깨지거나 통째로 버려진다
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    except Exception:
        return
    cwd = Path(payload.get("cwd") or ".")
    d = get_dir(dir_arg, cwd)
    sid0 = payload.get("session_id")
    lives = current_all(d)
    # 1) 이 세션이 시작한 실행  2) 세션이 정해지지 않은(수동 start) 실행 중 cwd 가 그 섹션 안인 것
    cur = next((c for c in lives if sid0 and c.get("session") == sid0), None)
    if cur is None:
        for c in lives:
            if c.get("session"):
                continue  # 다른 세션이 맡은 실행에는 기록하지 않는다
            try:
                cwd.resolve().relative_to(Path(c.get("section") or ""))
                cur = c
                break
            except Exception:
                continue
    if not cur:
        return
    ev_name, tool, ti = payload.get("hook_event_name", ""), payload.get("tool_name", ""), payload.get("tool_input") or {}
    evs = []
    stack = cur.setdefault("agent_stack", [])
    sec = Path(cur.get("section", "")) if cur.get("section") else None
    sid = payload.get("session_id")
    changed = False
    # 누가 한 일인가: 하위 에이전트 안의 훅 입력에는 agent_id/agent_type 이 들어온다.
    # 이런 입력을 한 번이라도 받았으면 입력값만 믿는다(없으면 메인 세션). 받은 적이 없는 Claude Code 버전에서만
    # Agent 호출 스택으로 추정한다 — 호출이 거부·실패해 SubagentStop 이 안 오면 스택이 남으므로 Stop 때 비운다.
    if payload.get("agent_id") or payload.get("agent_type"):
        if not cur.get("payload_agents"):
            cur["payload_agents"] = True
            changed = True
    if cur.get("payload_agents"):
        # 메인 세션도 이름을 명시해야 집계 때 '마지막으로 시작한 에이전트'로 잘못 붙지 않는다
        who = payload.get("agent_type") or payload.get("agent_id") or MAIN_AGENT
    else:
        who = stack[-1] if stack else None
    # 실행을 처음 기록한 세션의 대화 기록 경로를 남겨 end 가 마지막 턴까지 토큰을 직접 계산하게 한다
    # (start~end 를 한 턴에 하면 그 사이에 Stop 훅이 오지 않는다)
    if payload.get("transcript_path") and not cur.get("transcript_path"):
        cur["transcript_path"] = payload["transcript_path"]
        changed = True
    if sec and not cur.get("wrong_flag"):
        try:
            cwd.resolve().relative_to(sec)
        except ValueError:
            evs.append({"type": "wrong_section", "name": str(cwd)})
            cur["wrong_flag"] = True
            changed = True
    if ev_name == "PreToolUse":
        if tool == "Skill":
            n = ti.get("skill") or ti.get("name") or ti.get("command")
            if n:
                evs.append({"type": "skill", "name": str(n).split(":")[-1], "agent": who})
        elif tool in FILE_TOOLS and ti.get(FILE_TOOLS[tool]):
            p = Path(ti[FILE_TOOLS[tool]])
            try:
                p = p.resolve().relative_to(sec) if sec else p
            except Exception:
                pass
            evs.append({"type": "file", "name": p.as_posix() if isinstance(p, Path) else str(p), "agent": who})
        elif tool in ("Bash", "PowerShell") and ti.get("command"):
            # 명령(cat·sed·grep·Get-Content 등)으로 읽은 파일도 기록: 명령 안의 경로 중 실제로 있는 파일만
            seen_p = set()
            for m in CMD_PATH_RE.finditer(str(ti["command"])):
                raw = m.group(0).strip("'\"")
                try:
                    q = Path(raw.replace("\\", "/"))
                    q = (q if q.is_absolute() else cwd / q).resolve()
                    if not q.is_file():
                        continue
                    name = q.relative_to(sec).as_posix() if sec else q.as_posix()
                except Exception:
                    try:
                        name = q.as_posix()
                    except Exception:
                        continue
                if name not in seen_p and len(seen_p) < 20:
                    seen_p.add(name)
                    evs.append({"type": "file", "name": name, "agent": who})
        elif tool in ("Task", "Agent"):
            n = str(ti.get("subagent_type") or ti.get("description") or "하위 에이전트")
            ev = {"type": "agent", "name": n}
            if any(h in n.lower() for h in REVIEW_HINTS):
                ev["review"] = True
            evs.append(ev)
            stack.append(n)
            changed = True
    elif ev_name in ("Stop", "SubagentStop"):
        since = parse_ts(cur.get("started"))
        atp = payload.get("agent_transcript_path")
        if atp:
            t2 = transcript_tokens(atp, since)
            if t2:
                subs = cur.get("subagent_tokens") or {}
                if not isinstance(subs, dict):
                    subs = {"_": int(subs)}
                subs[atp] = t2  # 같은 하위 에이전트가 다시 멈춰도(이어서 실행) 중복 가산하지 않도록 경로별 최신값
                cur["subagent_tokens"] = subs
                changed = True
        if ev_name == "SubagentStop" and stack:
            # 끝난 에이전트 이름이 오면 그 항목을 뺀다 (병렬 실행 시 마지막 항목이 아닐 수 있음)
            name = payload.get("agent_type")
            if name in stack:
                stack.reverse()
                stack.remove(name)
                stack.reverse()
            else:
                name = stack.pop()
            evs.append({"type": "agent_end", "name": name})
            changed = True
        elif ev_name == "Stop" and stack:
            # 메인 세션 턴 종료: 끝 신호를 못 받은(거부·실패한) 호출이 남아 있으면 정리
            evs.append({"type": "agent_end", "name": stack[-1]})
            stack.clear()
            changed = True
        total = run_tokens(cur)
        if total is not None:
            evs.append({"type": "tokens", "total": total})
    if changed:
        save_current(cur)
    for ev in evs:
        ev["run_id"] = cur["run_id"]
        if sid:
            ev["session"] = sid
        if not ev.get("agent"):
            ev.pop("agent", None)
        append(d, ev)


def install_hooks(section, data_dir, script):
    cfg_p = Path(section) / ".claude" / "settings.local.json"
    cfg = {}
    if cfg_p.exists():
        # 읽지 못한 설정을 {} 로 보고 덮어쓰면 기존 권한·설정이 사라지므로, 읽기 실패 시 중단한다
        try:
            txt = cfg_p.read_text(encoding="utf-8-sig")
            cfg = json.loads(txt) if txt.strip() else {}
        except Exception as e:
            raise ValueError(f"{cfg_p} 를 읽지 못해 훅을 등록하지 않음 (파일은 그대로 둠): {e}")
        if not isinstance(cfg, dict) or not isinstance(cfg.get("hooks", {}), dict):
            raise ValueError(f"{cfg_p} 형식이 예상과 달라 훅을 등록하지 않음 (파일은 그대로 둠)")
    cmd = f'"{sys.executable}" "{script}" --dir "{data_dir}" hook'
    hooks = cfg.setdefault("hooks", {})
    added = []
    for event, matcher in (("PreToolUse", "*"), ("Stop", None), ("SubagentStop", None)):
        lst = hooks.setdefault(event, [])
        lst[:] = [x for x in lst if "mes_compare" not in json.dumps(x, ensure_ascii=False)]
        entry = {"hooks": [{"type": "command", "command": cmd}]}
        if matcher:
            entry["matcher"] = matcher
        lst.append(entry)
        added.append(event)
    cfg_p.parent.mkdir(parents=True, exist_ok=True)
    if cfg_p.exists():
        cfg_p.with_suffix(".json.bak").write_bytes(cfg_p.read_bytes())  # 원본 그대로 백업
    cfg_p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg_p, added


# ---------------------------------------------------------------- 판정
def do_judge(d, run_id, final, reason, by):
    if final not in ("합격", "불합격"):
        raise ValueError("판정은 합격 또는 불합격")
    if not str(by or "").strip():
        raise ValueError("판정자를 입력하세요")
    if final == "불합격" and not str(reason or "").strip():
        raise ValueError("불합격 사유를 입력하세요")
    st = build_state(d)
    row = next((r for r in st["runs"] if r["run_id"] == run_id), None)
    if not row:
        raise ValueError(f"{run_id} 실행을 찾지 못함")
    if row["status"] not in ("판정 대기", "완료"):
        raise ValueError(f"{run_id} 는 아직 AI 작업이 끝나지 않음 ({row['status']})")
    append(d, {"type": "judge", "run_id": run_id, "final": final,
               "fail_reason": reason if final == "불합격" else "", "by": by.strip()})
    return f"{run_id} → {final}"


def find_run(st, run_id=None, task_id=None, structure=None, project=None):
    if run_id:
        return run_id
    cands = [r for r in st["runs"] if r["task_id"] == task_id and structure in (r["structure"], r["structure_name"])
             and (not project or r["project"] == project) and r["status"] in ("판정 대기", "완료")]
    projs = sorted({r["project"] for r in cands})
    if len(projs) > 1:
        raise ValueError(f"{task_id}:{structure} 실행이 여러 프로젝트에 있음 ({', '.join(projs)}) → --project 로 지정하세요")
    cands.sort(key=lambda r: (r["status"] != "판정 대기", -r["rep"]))
    if not cands:
        raise ValueError(f"{task_id}:{structure} 판정할 실행이 없음")
    return cands[0]["run_id"]


# ---------------------------------------------------------------- 서버
def serve(d, host, port):
    d = Path(d)

    class H(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self._send(200, render_html(build_state(d, True), True), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._send(200, json.dumps(build_state(d, True), ensure_ascii=False), "application/json; charset=utf-8")
            elif path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            else:
                self._send(404, "not found", "text/plain; charset=utf-8")

        def do_POST(self):
            if self.path.split("?")[0] != "/api/judge":
                return self._send(404, "{}", "application/json")
            try:
                b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8"))
                msg = do_judge(d, b.get("run_id"), b.get("final"), b.get("fail_reason", ""), b.get("by", ""))
                self._send(200, json.dumps({"ok": True, "message": msg}, ensure_ascii=False), "application/json; charset=utf-8")
            except ValueError as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False), "application/json; charset=utf-8")

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((host, port), H)
    print(f"실시간 대시보드: http://localhost:{port}   (데이터: {d})   Ctrl+C 로 종료")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------- CLI
def utf8_output():
    """출력을 파이프로 받을 때(Claude 가 실행할 때 등) Windows 기본 인코딩(cp949)은 '—' 같은 문자에서
    UnicodeEncodeError 로 명령이 중간에 멈추므로 UTF-8 로 내보낸다. Windows 콘솔에 직접 출력할 때는 그대로 둔다.
    (Windows 에서 NUL 로 보낸 출력은 isatty() 가 True 라서 isatty 대신 콘솔 객체인지로 판단한다)"""
    for s in (sys.stdout, sys.stderr):
        try:
            raw = getattr(getattr(s, "buffer", None), "raw", None)
            if type(raw).__name__ == "_WindowsConsoleIO":
                s.reconfigure(errors="replace")
            else:
                s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    utf8_output()
    ap = argparse.ArgumentParser(description="AI 사용 구조 비교 도구")
    ap.add_argument("--dir", help="기록 폴더 (기본: MES_DIR 또는 원본 저장소/.mes)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init")
    i.add_argument("--install-hooks", action="store_true", help="이 섹션의 .claude/settings.local.json 에 훅 등록")
    i.add_argument("--section", default=".")
    sub.add_parser("structure").add_argument("--section", default=".")

    for name in ("plan", "start"):
        s = sub.add_parser(name)
        s.add_argument("--task-id", required=True)
        s.add_argument("--task", required=True)
        s.add_argument("--structure", "--method", dest="structure", help="구조 이름 (기본: 섹션 폴더 이름)")
        s.add_argument("--project", help="기본: 저장소 이름")
        s.add_argument("--project-name")
        s.add_argument("--area", default="")
        s.add_argument("--risk", default="일반", choices=["고위험", "일반"])
        s.add_argument("--checks-total", type=int)
        s.add_argument("--section", default=".")

    e = sub.add_parser("event")
    e.add_argument("--type", required=True, choices=["skill", "file", "agent", "agent_end", "tokens", "note"])
    e.add_argument("--name")
    e.add_argument("--total", type=int)
    e.add_argument("--review", action="store_true")
    e.add_argument("--agent")

    n = sub.add_parser("end")
    n.add_argument("--checks-passed", type=int, help="자동 검증 통과 수 (참고)")
    n.add_argument("--checks-total", type=int)
    n.add_argument("--rework", type=int)
    n.add_argument("--tokens", type=int)
    n.add_argument("--minutes", type=float)
    n.add_argument("--note", help="AI 작업 메모 (판정 아님)")

    j = sub.add_parser("judge")
    j.add_argument("--run", help="실행 ID (예: P1-T01:B#1)")
    j.add_argument("--task-id")
    j.add_argument("--structure")
    j.add_argument("--project", help="같은 작업 ID 가 여러 프로젝트에 있을 때 지정")
    j.add_argument("--final", required=True, choices=["합격", "불합격"])
    j.add_argument("--fail-reason", default="")
    j.add_argument("--by", required=True)

    rs = sub.add_parser("reset", help="AI 설정은 남기고 코드·문서 변경만 되돌림 (기본 미리보기)")
    rs.add_argument("--yes", action="store_true")
    rs.add_argument("--section", default=".")
    cp = sub.add_parser("capture", help="섹션의 구조 파일을 경로 그대로 구조 묶음 폴더에 담기")
    cp.add_argument("--out", required=True, help="구조 묶음 폴더 (섹션 밖, 폴더 이름이 구조 이름)")
    cp.add_argument("--include", action="append", help="자동으로 못 찾는 구조 파일·폴더 (섹션 기준, 여러 번 가능)")
    cp.add_argument("--section", default=".")
    cp.add_argument("--yes", action="store_true", help="이미 있는 묶음 덮어쓰기")
    ap_ = sub.add_parser("apply", help="구조 묶음을 이 섹션에 적용 (기본 미리보기)")
    ap_.add_argument("profile", help="구조 묶음 폴더")
    ap_.add_argument("--section", default=".")
    ap_.add_argument("--yes", action="store_true")
    sub.add_parser("hook")
    v = sub.add_parser("serve")
    v.add_argument("--port", type=int, default=8765)
    v.add_argument("--host", default="127.0.0.1")
    b = sub.add_parser("build")
    b.add_argument("--out", required=True)
    b.add_argument("--label", default="")
    sub.add_parser("status")

    a = ap.parse_args()
    if a.cmd == "hook":
        try:
            run_hook(a.dir)
        except Exception:
            pass
        sys.exit(0)

    sect = Path(getattr(a, "section", ".") or ".").resolve()
    if a.cmd == "capture":  # 담기는 기록 폴더가 필요 없으므로 원본 폴더 옆에 기록 폴더를 만들지 않는다
        if a.dir:
            configure(a.dir)
        try:
            capture_profile(sect, a.out, a.include or [], a.yes)
        except (ValueError, OSError) as e:
            sys.exit(str(e))
        return
    d = get_dir(a.dir, sect)
    configure(d)
    load_profile_mark(sect)
    if a.cmd == "init":
        if not (d / "setup.json").exists():
            (d / "setup.json").write_text(json.dumps(SETUP_EXAMPLE, ensure_ascii=False, indent=1), encoding="utf-8")
        (d / "events.jsonl").touch()
        (sect / DATA_POINTER).write_text(str(d), encoding="utf-8")
        print(f"기록 폴더: {d}  (이 섹션의 {DATA_POINTER} 에 기록 — 섹션들이 같은 폴더를 쓰는지 확인)")
        if a.install_hooks:
            try:
                p, added = install_hooks(sect, d, Path(__file__).resolve())
            except ValueError as e:
                sys.exit(str(e))
            print(f"훅 등록: {p} ({', '.join(added)}) — 이 섹션에서 새로 여는 세션부터 적용")
        sn = snapshot(sect)
        print(f"이 섹션 구조: {sn['section_name']} · 에이전트 {len(sn['agents'])} · 스킬 {len(sn['skills'])} · 규칙 파일 {len(sn['files'])} · 지문 {sn['fp']}")
        warn_imports(sn)
    elif a.cmd == "reset":
        cur = current(d, sect)
        if cur:
            sys.exit(f"진행 중인 실행이 있습니다: {cur['run_id']} (먼저 end)")
        try:
            reset_code(sect, a.yes)
        except ValueError as e:
            sys.exit(str(e))
    elif a.cmd == "apply":
        cur = current(d, sect)
        if cur:
            sys.exit(f"진행 중인 실행이 있습니다: {cur['run_id']} (먼저 end)")
        try:
            do_apply(sect, a.profile, a.yes)
        except (ValueError, OSError) as e:
            sys.exit(str(e))
    elif a.cmd == "structure":
        print(json.dumps({"snapshot": snapshot(sect), "code": code_base(sect), "refs": refs_fingerprint(sect), "data_dir": str(d)}, ensure_ascii=False, indent=1))
    elif a.cmd in ("plan", "start"):
        name = a.structure or sect.name
        root = repo_root(sect)
        if not root and vcs(sect) == "svn":
            rr = svn(["info", "--show-item", "repos-root-url"], sect) or ""
            proj = a.project or (rr.rstrip("/").rsplit("/", 1)[-1] or sect.name)
        else:
            proj = a.project or (root.name if root else sect.name)
        base = {"task_id": a.task_id, "task": a.task, "structure": name, "project": proj,
                "project_name": a.project_name or proj, "area": a.area, "risk": a.risk, "checks_total": a.checks_total}
        if a.cmd == "plan":
            append(d, dict(base, type="plan"))
            print("계획:", a.task_id, name)
            return
        cur = current(d, sect)
        if cur:
            sys.exit(f"이 섹션에서 이미 진행 중: {cur['run_id']} (먼저 end)")
        reps = [x for x in read_events(d) if x.get("type") == "run_start" and x.get("task_id") == a.task_id and x.get("structure") == name]
        rep = len(reps) + 1
        rid = f"{a.task_id}:{name}#{rep}"
        sn, cb = snapshot(sect), code_base(sect)
        refs = refs_fingerprint(sect)
        ev = dict(base, type="run_start", run_id=rid, rep=rep, snapshot=sn, code=cb, refs=refs)
        append(d, ev)
        cur_path(d, sn["section"]).write_text(json.dumps({"run_id": rid, "started": ev["ts"], "section": sn["section"]}, ensure_ascii=False), encoding="utf-8")
        print(f"시작: {rid} · 구조 {name} (지문 {sn['fp']}) · 코드·문서 지문 {cb['code_fp'] or '없음'} (커밋 {cb['commit'] or '없음'})"
              + (f" · 고정 참조 {refs['n']}개 (지문 {refs['fp'] or '없음'})" if refs else ""))
        if refs is not None and not refs["n"]:
            print("경고: setup.json fixed_refs 에 맞는 파일이 없음 → 경로·패턴을 확인하세요")
        warn_imports(sn)
        if cb.get("mixed"):
            print("경고: 작업 폴더에 리비전이 섞여 있음 → svn update 로 한 리비전에 맞추세요")
        if cb["dirty_n"]:
            print(f"경고: 코드·문서에 커밋 안 된 변경 {cb['dirty_n']}건 → 구조끼리 참조 코드가 달라질 수 있음")
    elif a.cmd == "event":
        cur = current(d, sect) or sys.exit("이 섹션에 진행 중인 실행이 없습니다")
        ev = {"type": a.type, "run_id": cur["run_id"]}
        for k in ("name", "total", "agent"):
            if getattr(a, k) is not None:
                ev[k] = getattr(a, k)
        if a.review:
            ev["review"] = True
        append(d, ev)
    elif a.cmd == "end":
        cur = current(d, sect) or sys.exit("이 섹션에 진행 중인 실행이 없습니다")
        ev = {"type": "run_end", "run_id": cur["run_id"]}
        for k in ("checks_passed", "checks_total", "rework", "tokens", "minutes", "note"):
            if getattr(a, k) is not None:
                ev[k] = getattr(a, k)
        if a.tokens is None:
            t = run_tokens(cur)  # 마지막 턴(Stop 훅 전)까지 포함해 직접 계산
            if t is not None:
                ev["tokens"] = t
        append(d, ev)
        Path(cur["_path"]).unlink()
        print("AI 작업 종료:", cur["run_id"], "→ 사람 판정 대기")
    elif a.cmd == "judge":
        try:
            rid = find_run(build_state(d), a.run, a.task_id, a.structure, a.project)
            print("판정:", do_judge(d, rid, a.final, a.fail_reason, a.by))
        except ValueError as e:
            sys.exit(str(e))
    elif a.cmd == "serve":
        serve(d, a.host, a.port)
    elif a.cmd == "build":
        st = build_state(d, True)
        errs, warns = validate(st)
        for w in warns:
            print("경고:", w)
        if errs:
            for x in errs:
                print("오류:", x)
            sys.exit(1)
        st["meta"]["label"] = a.label
        Path(a.out).write_text(render_html(st, False), encoding="utf-8")
        print(f"생성: {a.out}")
    elif a.cmd == "status":
        st = build_state(d)
        cnt = {}
        for r in st["runs"]:
            cnt[r["status"]] = cnt.get(r["status"], 0) + 1
        print(f"기록 폴더: {d}")
        print("구조:", ", ".join(f"{s['key']}({s['section']}, {s['fp']})" for s in st["structures"]) or "없음", "· 기준:", st["baseline"] or "—")
        print("상태별:", ", ".join(f"{k} {v}" for k, v in cnt.items()) or "기록 없음")
        for x in st["live"]:
            print(f"  진행 중 {x['run_id']} {x['task']} · 스킬 {x['skill'] or '—'} · 파일 {x['file'] or '—'}" + (" · 멈춤 의심" if x["stale"] else ""))
        for r in st["runs"]:
            if r["status"] == "판정 대기":
                print(f"  판정 대기 {r['run_id']} {r['task']}")


TEMPLATE = r'''<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI 사용 구조 비교</title>
<style>
:root{color-scheme:dark;--bg:#0b1220;--panel:#111a2e;--panel2:#0f182b;--fg:#e6ecf7;--mut:#8a97b0;--line:#22304d;--line2:#2a3a5c;--ok:#3ecf8e;--warn:#f5b748;--bad:#ff6b6b;--info:#5aa9ff;--grid:#1d2944;--pop:#16213a;--A:#7d89a3;--B:#4f8cff;--C:#b36bff;--r:8px;font-family:"Pretendard","Apple SD Gothic Neo","Malgun Gothic",system-ui,sans-serif}
*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--fg);font-size:14px}
.wrap{max-width:1280px;margin:0 auto;padding:28px clamp(12px,3vw,32px) 48px;display:flex;flex-direction:column;gap:20px}
h1{font-size:26px;margin:0;font-weight:700;letter-spacing:-.01em}h2{font-size:16px;margin:0;font-weight:600}h3{font-size:13px;margin:0 0 8px;font-weight:600}
.sub{color:var(--mut);margin:4px 0 0}.note{color:var(--mut);font-size:12px;margin:4px 0 12px}
.badge{display:inline-block;margin-left:8px;padding:1px 8px;border:1px solid var(--line);border-radius:var(--r);font-size:12px;color:var(--warn);vertical-align:middle;font-weight:500}
.badge.live{color:var(--ok)}.badge.live::before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--ok);margin-right:6px;animation:pulse 1.6s infinite}
@keyframes pulse{50%{opacity:.25}}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:var(--r);overflow:hidden}
.seg button{font:inherit;background:transparent;color:var(--mut);border:0;padding:6px 14px;cursor:pointer}
.seg button+button{border-left:1px solid var(--line)}.seg button[aria-pressed=true]{background:var(--panel);color:var(--fg);font-weight:600}
.seg.sm button{font-size:12px;padding:3px 10px}
select,.btn,input[type=text]{font:inherit;background:var(--panel);color:var(--fg);border:1px solid var(--line);border-radius:var(--r);padding:6px 10px}
.btn{cursor:pointer}.btn:hover{border-color:var(--line2)}.btn.ok{color:var(--ok)}.btn.bad{color:var(--bad)}
.card{background:linear-gradient(180deg,#121c33,var(--panel2));border:1px solid var(--line);border-radius:var(--r);padding:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,20rem),1fr));gap:14px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(7.5rem,1fr));gap:8px;margin:12px 0}
.tile{border:1px solid var(--line);border-radius:var(--r);padding:8px 14px}.tile .v{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}.tile .k{font-size:12px;color:var(--mut)}
.tile.hot .v{color:var(--warn)}.tile.bad .v{color:var(--bad)}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:var(--mut)}.legend i{display:inline-block;width:10px;height:8px;border-radius:2px;margin-right:5px}
.chart{position:relative}.gscroll{overflow-x:auto}
.kpi{display:grid;grid-template-columns:minmax(8rem,1.1fr) 1fr 1fr minmax(5rem,.8fr);font-variant-numeric:tabular-nums}
.kpi>div{padding:9px 4px;border-bottom:1px solid var(--line)}.kpi .h{font-size:12px;color:var(--mut)}.kpi small{display:block;color:var(--mut);font-size:12px}
.big{font-size:26px;font-weight:700}.mut{color:var(--mut);font-size:12px}.good{color:var(--ok)}.badc{color:var(--bad)}
.take{margin:0 0 6px}.take b{font-variant-numeric:tabular-nums}
.pill{display:inline-block;padding:0 7px;border-radius:var(--r);font-size:12px;font-weight:600}
.pill.ok{color:var(--ok);background:rgba(62,207,142,.14)}.pill.bad{color:var(--bad);background:rgba(255,107,107,.14)}
.pill.wait{color:var(--warn);background:rgba(245,183,72,.14)}.pill.jw{color:var(--info);background:rgba(90,169,255,.14)}
.reason{display:block;font-size:12px;color:var(--bad);margin-top:2px}.flag{display:block;font-size:12px;color:var(--warn);margin-top:2px}
dl.kv{margin:0;display:grid;grid-template-columns:6rem 1fr;gap:4px 8px;font-size:12px;font-variant-numeric:tabular-nums}dl.kv dt{color:var(--mut)}dl.kv dd{margin:0}
.chips{display:flex;flex-wrap:wrap;gap:4px}.chip{border:1px solid var(--line);border-radius:var(--r);padding:0 6px;font-size:12px}
.chip.f{font-family:ui-monospace,Consolas,monospace}.chip.none{color:var(--mut);border-style:dashed}.chip.warn{color:var(--bad);border-color:var(--bad)}
.judge{grid-column:1/-1;border:1px dashed var(--line2);border-radius:var(--r);padding:10px 14px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:12px}
.judge input[type=text]{flex:1 1 12rem;padding:4px 8px}.judge .who{flex:0 1 8rem}
.sk{display:grid;grid-template-columns:minmax(6rem,9rem) 1fr;gap:4px 8px;font-size:12px;align-items:center}
.sk .bars{display:flex;flex-direction:column;gap:2px}.sk .b{display:flex;align-items:center;gap:6px}.sk .b i{display:block;height:8px;border-radius:2px;min-width:2px}.sk .b span{color:var(--mut)}
.lives{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,19rem),1fr));gap:10px;margin:12px 0}
.lv{border:1px solid var(--line);border-left:3px solid var(--B);border-radius:var(--r);padding:10px 14px}
.lv.stale{border-color:var(--warn)}.lv .t{font-weight:600}.lv .row{display:flex;gap:6px;font-size:12px;margin-top:3px}.lv .row .k{color:var(--mut);flex:0 0 4.5rem}
.lv .now{color:var(--info)}.feed{list-style:none;margin:0;padding:0;font-size:12px;max-height:16rem;overflow:auto}
.feed li{display:grid;grid-template-columns:4.2rem 1rem minmax(6rem,9rem) 5.5rem 1fr;gap:6px;padding:3px 0;border-bottom:1px solid rgba(34,48,77,.6)}
.feed .d{width:8px;height:8px;border-radius:50%;margin-top:4px}
.pend{display:flex;flex-direction:column;gap:8px;margin:6px 0 12px}.pi{border:1px solid var(--line);border-radius:var(--r);padding:8px 12px;display:flex;flex-direction:column;gap:6px}.pi .judge{border:0;padding:0}
.tip{position:fixed;pointer-events:none;background:var(--pop);border:1px solid var(--line2);border-radius:var(--r);box-shadow:0 8px 24px rgba(0,0,0,.45);padding:6px 10px;font-size:12px;max-width:18rem;z-index:9}
.tip .t{color:#9fb0cc;margin-bottom:2px}.tip .r{display:flex;gap:6px;align-items:center}.tip .r i{width:8px;height:8px;border-radius:50%;flex:none}.tip .n{color:var(--mut);margin-top:2px}
details.foot summary{cursor:pointer}details.foot dl{grid-template-columns:8rem 1fr}
svg text{font-family:inherit}
@media(max-width:40rem){.feed li{grid-template-columns:4rem 1rem 1fr}.feed li>:nth-child(3),.feed li>:nth-child(4){display:none}}

.crit{font-size:12px;color:var(--mut);margin:0 0 10px;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.crit input{width:4.2rem;font:inherit;font-size:12px;background:var(--panel);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:2px 6px}
.lbwrap{overflow-x:auto}
table.lb{width:100%;border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums}
table.lb th{text-align:left;color:var(--mut);font-weight:500;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
table.lb td{padding:8px;border-bottom:1px solid var(--line);vertical-align:top}
table.lb tr.base td{background:rgba(255,255,255,.02)}
table.lb tbody tr:not(.base):hover td{background:rgba(255,255,255,.03)}
table.lb tr.sel td{background:rgba(90,169,255,.08)}
table.lb .big2{font-size:15px;font-weight:700}
table.lb .d{font-size:12px}
.sw{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.bar label.mut{margin-left:4px}
.feed li.k .ty{color:var(--fg);font-weight:600}.feed li.old{opacity:.45}.feed li.old .tx{text-decoration:line-through}
.feed .ty{color:var(--mut)}.feed .tx{min-width:0;overflow-wrap:anywhere}.feed details summary{cursor:pointer;list-style:none}.feed details summary::-webkit-details-marker{display:none}
.feed details ul{margin:4px 0 2px;padding-left:14px;color:var(--mut);font-family:ui-monospace,Consolas,monospace;font-size:11px}
.feed .ok{color:var(--ok)}.feed .bad{color:var(--bad)}
.tb{display:grid;grid-template-columns:minmax(7rem,11rem) 1fr;gap:6px 10px;align-items:center;font-size:12px}
.tb .lab{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.tb .stack{display:flex;height:18px;border-radius:4px;overflow:hidden;background:rgba(255,255,255,.03)}
.tb .stack i{display:block;height:100%;min-width:1px}.tb .stack.thin{height:10px}.tb .val{color:var(--mut);font-size:11px;margin-left:6px;white-space:nowrap}
.tb .row{display:flex;align-items:center}.tb .grp{grid-column:1/-1;color:var(--info);font-size:11px;margin-top:8px}
table.tt{border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums;width:100%}
table.tt th{text-align:left;color:var(--mut);font-weight:500;padding:4px 8px;border-bottom:1px solid var(--line)}table.tt td{padding:5px 8px;border-bottom:1px solid rgba(34,48,77,.6)}
table.tt td.n,table.tt th.n{text-align:right}table.tt td:nth-child(2),table.tt td.n{white-space:nowrap}table.tt .f{font-family:ui-monospace,Consolas,monospace;font-size:11px;overflow-wrap:anywhere}
.stars{display:block;width:100%;background:radial-gradient(ellipse at center,#120d0b 0%,#07080c 70%);border-radius:var(--r);cursor:default}
.zoom3{display:flex;gap:6px;margin-top:-44px;margin-left:10px;position:relative;width:max-content}.leg3{margin-top:18px}.leg3 .legend i{display:inline-block;margin-right:5px;vertical-align:-1px}
.g3wrap{position:relative}.g3live{position:absolute;right:10px;top:34px;max-width:min(30rem,55%);background:rgba(10,12,18,.8);border:1px solid rgba(120,220,150,.35);border-radius:8px;padding:7px 10px;font-size:12px;line-height:1.5}
.g3live.stale{border-color:rgba(240,180,90,.45)}.g3lt{font-weight:600;color:var(--ok)}.g3live.stale .g3lt{color:#f0b45a}.g3lt i{display:inline-block;width:8px;height:8px;border-radius:50%;background:currentColor;margin-right:6px;animation:pulse 1.6s infinite}.g3panel{position:absolute;left:10px;top:34px;background:rgba(10,12,18,.72);border:1px solid rgba(255,255,255,.08);border-radius:8px;padding:8px 10px;font-size:12px;min-width:12.5rem}
.g3ph{color:var(--mut);font-size:11px;margin-bottom:4px}.g3row{display:grid;grid-template-columns:16px 1fr auto auto;gap:6px;align-items:center;padding:3px 4px;border-radius:5px;cursor:pointer}.g3row:hover,.g3row.on{background:rgba(255,255,255,.07)}
.g3row b{font-weight:600;font-variant-numeric:tabular-nums}.g3row small{color:var(--mut);font-size:10.5px}.g3pf{color:var(--mut);font-size:10.5px;margin-top:6px;max-width:13rem}
.g3head{position:absolute;left:12px;top:8px;right:12px;color:#c9bfb6;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.seg button[disabled]{opacity:.45;cursor:default}
.gpick{margin-top:10px;border:1px solid var(--line);border-radius:var(--r);padding:10px 14px;font-size:12px}
.gpick h3{margin:0 0 4px}.gpick ul{margin:6px 0 0;padding-left:16px;columns:2 18rem;font-family:ui-monospace,Consolas,monospace;font-size:11px;color:var(--mut)}
details.fold>summary{cursor:pointer;list-style:none;display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}details.fold>summary::-webkit-details-marker{display:none}details.fold>summary h2::before{content:'▸ ';color:var(--mut)}details.fold[open]>summary h2::before{content:'▾ '}details.fold[open]>summary{margin-bottom:8px}
.al details summary{cursor:pointer;list-style:none}.al details summary::-webkit-details-marker{display:none}
.al{display:flex;flex-direction:column;gap:6px}.al .it{border-left:2px solid var(--line2);padding:2px 0 2px 8px}.al .it.un{opacity:.6;border-left-style:dashed}
.al .nm{font-weight:600}.al .us{font-size:11px;margin-left:6px}.al .ds{color:var(--mut);font-size:12px;margin-top:2px}
</style>
</head>
<body>
<script type="application/json" id="mes-data">__MES_DATA__</script>
<div class="wrap">
  <header>
    <h1>AI 사용 구조 비교 <span class="badge" id="label">—</span></h1>
    <p class="sub">같은 코드·문서에서 AI 사용 구조(에이전트·스킬·CLAUDE.md·규칙)만 바꿔 같은 작업을 시키고, 토큰·작업 시간·품질을 비교합니다. 합격·불합격은 사람이 판정합니다.</p>
  </header>
  <div class="bar">
    <label class="mut" for="base">기준 구조</label><select id="base"></select>
    <label class="mut" for="comp">비교 구조</label><select id="comp"></select>
    <select id="project" aria-label="프로젝트"></select>
    <select id="risk" aria-label="작업 범위"><option value="ALL">모든 작업</option><option value="고위험">고위험 작업만</option><option value="일반">일반 작업만</option></select>
    <label class="btn" id="loadwrap">결과 파일 불러오기<input type="file" id="file" accept=".json" hidden></label>
  </div>

  <section class="card" id="live">
    <h2>지금 진행 상황</h2>
    <p class="note" id="syncnote">—</p>
    <div class="lives" id="lives"></div>
    <h3 id="pendh">사람 판정 필요</h3>
    <div class="pend" id="pend"></div>
    <div class="bar" style="justify-content:space-between;margin-bottom:6px"><h3 style="margin:0">최근 활동</h3>
      <div class="seg sm" role="group" aria-label="활동 범위"><button type="button" data-feed="key" aria-pressed="false">중요한 것만</button><button type="button" data-feed="all" aria-pressed="true">전체</button></div></div>
    <ol class="feed" id="feed"></ol>
  </section>

  <section class="card">
    <h2>구조별 비교 <span class="mut" id="lbbase"></span></h2>
    <p class="note">각 구조를 기준 구조와 <b>같은 작업끼리만</b> 맞대어 비교합니다. 합격률은 사람 판정 기준이고, 토큰·시간은 양쪽 모두 합격한 작업만 평균합니다. 행을 누르면 아래에서 기준 구조와 자세히 비교합니다.</p>
    <div class="crit">채택 기준:
      합격률 하락 <input type="number" id="c_rate" step="1" min="0">%p 이내 ·
      토큰 절감 <input type="number" id="c_tok" step="1">% 이상 ·
      시간 절감 <input type="number" id="c_time" step="1">% 이상 ·
      비교 작업 <input type="number" id="c_min" step="1" min="1">개 이상
    </div>
    <div class="lbwrap"><table class="lb" id="lb"></table></div>
  </section>

  <section class="card">
    <p class="take" id="take">—</p>
    <div class="kpi" id="kpi"></div>
    <p class="mut" id="kpinote" style="margin-top:10px">—</p>
  </section>

  <details class="card fold" id="tokcard">
    <summary><h2>토큰은 어디에 쓰였나</h2><span class="mut" id="tsum"></span></summary>
    <p class="note">AI는 호출할 때마다 그때까지의 대화 전체를 다시 읽습니다. 그래서 한 번 읽은 파일·명령 결과도 그 뒤 호출 수만큼 다시 계산됩니다. 대화 기록의 호출별 문맥 크기로 나눈 값이라 합계는 실제 토큰과 같고, 항목별 값은 추정입니다. <b>기본 문맥</b>은 시스템 지시·CLAUDE.md·스킬 목록·도구 목록처럼 매 호출에 붙는 몫이고, <b>이전 작업 이월</b>은 앞 작업의 대화를 이어 써서 같이 읽힌 몫입니다. 캐시 읽기는 요금이 입력의 1/10입니다.</p>
    <div class="bar" style="margin-bottom:10px"><label class="mut" for="tview">보기</label><select id="tview"></select></div>
    <div class="legend" id="tleg" style="margin-bottom:10px"></div>
    <div id="tbars"></div>
    <div id="tdetail" style="margin-top:14px"></div>
  </details>

  <section class="card">
    <h2 id="dumbh">프로젝트별 비교</h2>
    <p class="note">프로젝트마다 기준 구조와 비교 구조의 막대를 나란히 놓았습니다. 토큰·시간·재작업은 짧을수록, 합격률은 길수록 좋습니다. 괄호는 집계에 들어간 작업 수입니다.</p>
    <div class="bar" style="margin-bottom:8px">
      <div class="seg sm" role="group" aria-label="지표">
        <button type="button" data-metric="rate" aria-pressed="false">합격률</button>
        <button type="button" data-metric="fp" aria-pressed="false">첫 시도 합격률</button>
        <button type="button" data-metric="tokens" aria-pressed="true">평균 토큰</button>
        <button type="button" data-metric="minutes" aria-pressed="false">평균 시간</button>
        <button type="button" data-metric="rework" aria-pressed="false">평균 재작업</button>
      </div>
      <div class="legend" id="dumbleg"></div>
    </div>
    <div class="chart" id="dumb"></div>
  </section>

  <section class="card">
    <h2>작업 흐름</h2>
    <p class="note" id="gnote">—</p>
    <div class="tiles" id="tiles"></div>
    <div class="bar" style="margin:10px 0 6px"><label class="mut" for="gview">보기</label><select id="gview"></select>
      <div class="seg sm" role="group" aria-label="표현"><button type="button" data-gmode="stars" aria-pressed="true">3D 연결</button><button type="button" data-gmode="lines" aria-pressed="false">연결선</button></div>
      <div class="legend" id="gleg"></div></div>
    <div class="chart gscroll" id="graph"></div>
    <div id="gpick" class="gpick" hidden></div>
  </section>

  <div class="grid">
    <section class="card"><h2>구조 구성</h2><p class="note">실행할 때 섹션에서 자동으로 읽은 AI 사용 구조입니다. 에이전트·스킬은 각 파일의 설명(description)과, 지금 보는 실행들에서 쓴 횟수를 함께 보여 줍니다.</p><div class="grid" id="setup"></div></section>
    <section class="card"><h2>실제 사용한 스킬</h2><p class="note">같은 작업 중 각 스킬을 호출한 실행 수입니다.</p><div class="legend" id="skleg" style="margin-bottom:8px"></div><div class="sk" id="sk"></div></section>
  </div>

  <details class="card foot">
    <summary>집계 기준</summary>
    <dl class="kv" style="margin-top:10px">
      <dt>비교 단위</dt><dd>같은 작업 ID를 두 구조가 모두 실행한 경우만 비교합니다. 구조(섹션)마다 작업 종류·개수가 달라도 왜곡되지 않습니다.</dd>
      <dt>구조</dt><dd>실행 시작 때 섹션의 .claude/agents, .claude/skills, CLAUDE.md, 규칙·명령 파일, 플러그인 설정을 읽어 지문을 만듭니다. 같은 이름인데 지문이 바뀌면 v1·v2로 나눠 집계합니다.</dd>
      <dt>판정</dt><dd>AI 작업이 끝나면 ‘판정 대기’, 사람이 합격·불합격을 넣으면 ‘완료’. 자동 검증 통과 수는 참고 값입니다.</dd>
      <dt>합격률</dt><dd>작업마다 판정된 실행의 합격 비율을 구하고, 작업들의 평균을 냅니다(반복 실행 포함).</dd>
      <dt>첫 시도 합격률</dt><dd>재작업 없이(재작업 0회) 합격한 비율입니다.</dd>
      <dt>토큰·시간</dt><dd>양쪽 모두 합격한 실행이 있고 그 지표가 측정된 작업만, 합격한 실행의 평균으로 비교합니다. 토큰과 시간은 따로 판정합니다. 시간은 AI 작업 시작~종료입니다.</dd>
      <dt>판정 엇갈림</dt><dd>같은 작업·같은 구조를 여러 번 돌렸을 때 합격과 불합격이 섞인 경우입니다. 많으면 결과가 불안정하다는 뜻입니다.</dd>
      <dt>경고</dt><dd>구조마다 코드·문서 지문이 다름(AI 설정 파일을 뺀 커밋 내용 기준, 참조 코드·문서 불일치), 커밋 안 된 코드 변경, 다른 섹션에서 작업, 한 실행에 세션 여러 개, 구조에 스킬이 있는데 미호출.</dd>
    </dl>
  </details>
</div>
<div class="tip" id="tip" hidden></div>
<script>
(() => {
const D0 = JSON.parse(document.getElementById('mes-data').textContent);
const PAL = ['#7d89a3', '#4f8cff', '#b36bff', '#3ecf8e', '#f5b748', '#ff7aa8', '#38c6d9', '#c3e88d', '#ff9f5a', '#9aa5ff'];
const S = { d: D0, project: 'ALL', risk: 'ALL', base: D0.baseline || '', comp: '', metric: 'tokens', view: 'auto', crit: { ...(D0.criteria || {}) },
  feed: 'all', gmode: 'stars', tview: 'pair', pick: null, openDesc: new Set(),
  live: location.protocol.startsWith('http') && !!(D0.meta && D0.meta.live) };
try { const c = JSON.parse(localStorage.getItem('mes_crit') || 'null'); if (c) S.crit = { ...S.crit, ...c }; } catch (e) {}
const $ = (s) => document.querySelector(s), $$ = (s) => [...document.querySelectorAll(s)];
const split = (v) => (Array.isArray(v) ? v : String(v || '').split(',')).map((s) => String(s).trim()).filter(Boolean);
const nf = (v) => (v == null ? '—' : Number(v).toLocaleString('ko-KR'));
const f1 = (v) => (v == null ? '—' : Number(v).toFixed(1));
const sg = (v) => (v == null ? '—' : (v > 0 ? '+' : v < 0 ? '−' : '') + Math.abs(v).toFixed(1));
const r1 = (x) => Math.round(x * 10) / 10;
const avg = (a) => (a.length ? a.reduce((s, v) => s + v, 0) / a.length : null);
function el(tag, a = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(a)) {
    if (v == null || v === false) continue;
    if (k === 'class') e.className = v; else if (k === 'text') e.textContent = v;
    else if (k === 'style') e.style.cssText = v; else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  kids.flat().forEach((c) => c != null && e.append(c instanceof Node ? c : document.createTextNode(String(c))));
  return e;
}
function sv(tag, a = {}, text) {
  const e = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(a)) if (v != null) e.setAttribute(k, v);
  if (text != null) e.textContent = text;
  return e;
}
const ctx = document.createElement('canvas').getContext('2d');
const tw = (s, px = 12) => { ctx.font = `${px}px Pretendard, "Apple SD Gothic Neo", "Malgun Gothic", system-ui, sans-serif`; return ctx.measureText(s).width * 1.06; };
const tip = $('#tip');
const tipShow = (ev, lines) => { tip.replaceChildren(...lines); tip.hidden = false; tipMove(ev); };
const tipMove = (ev) => { const w = tip.offsetWidth; tip.style.left = Math.min(ev.clientX + 12, innerWidth - w - 8) + 'px'; tip.style.top = (ev.clientY + 14) + 'px'; };
const tipHide = () => { tip.hidden = true; };
const tl = (cls, text, c) => el('div', { class: cls }, c ? el('i', { style: `background:${c}` }) : null, text);
const ago = (ts) => { const s = Math.max(0, (Date.now() - Date.parse(ts)) / 1000); return s < 60 ? `${Math.floor(s)}초 전` : s < 3600 ? `${Math.floor(s / 60)}분 전` : `${Math.floor(s / 3600)}시간 전`; };
const dur = (ts) => { const s = Math.max(0, Math.floor((Date.now() - Date.parse(ts)) / 1000)); return `${Math.floor(s / 60)}분 ${String(s % 60).padStart(2, '0')}초`; };
const hm = (ts) => { const d = new Date(ts); return isNaN(d) ? '' : d.toTimeString().slice(0, 8); };

const structs = () => S.d.structures || [];
const sOf = (k) => structs().find((s) => s.key === k) || { key: k, name: k, agents: [], skills: [], files: [], plugins: [], agent_skills: {} };
const col = (k) => { const i = structs().findIndex((s) => s.key === k); return PAL[(i < 0 ? 0 : i) % PAL.length]; };
const sLabel = (k) => { const s = sOf(k); return s.display ? `${k} · ${s.display}` : k; };
const allRuns = () => (S.d.runs || []).filter((r) => (S.project === 'ALL' || r.project === S.project));
const runs = () => allRuns().filter((r) => S.risk === 'ALL' || r.risk === S.risk);
const judged = (r) => r.status === '완료';

const tkey = (r) => `${r.project || ''}::${r.task_id}`; // 같은 작업 ID 라도 프로젝트가 다르면 다른 작업
function agg(rs) { // task -> structure -> stats
  const T = {};
  rs.filter((r) => r.status !== '대기').forEach((r) => { const t = tkey(r); ((T[t] = T[t] || {})[r.structure] = T[t][r.structure] || []).push(r); });
  const st = (list) => {
    const j = list.filter(judged), p = j.filter((r) => r.final === '합격');
    const pm = (f) => { const v = p.map((r) => r[f]).filter((x) => x != null); return v.length ? avg(v) : null; };
    return { list, n: list.length, j: j.length, pass: j.length ? p.length / j.length : null, fp: j.length ? j.filter((r) => r.final === '합격' && !r.rework).length / j.length : null,
      tok: pm('tokens'), min: pm('minutes'), rw: j.length ? avg(j.map((r) => r.rework || 0)) : null,
      mixed: j.length > 1 && p.length > 0 && p.length < j.length, pend: list.filter((r) => r.status === '판정 대기').length,
      miss: list.filter((r) => r.skill_called === '아니오' && r.status !== '진행 중').length };
  };
  const out = {}; Object.entries(T).forEach(([t, m]) => { out[t] = {}; Object.entries(m).forEach(([k, l]) => { out[t][k] = st(l); }); });
  return out;
}
function compare(A, B, project) {
  const P = project || S.project;
  const rs = (S.d.runs || []).filter((r) => (P === 'ALL' || r.project === P) && (S.risk === 'ALL' || r.risk === S.risk));
  const G = agg(rs), ids = Object.keys(G).filter((t) => G[t][A] && G[t][B]);
  const q = ids.filter((t) => G[t][A].j && G[t][B].j);
  const side = (k) => ({ rate: q.length ? r1(avg(q.map((t) => G[t][k].pass)) * 100) : null, fp: q.length ? r1(avg(q.map((t) => G[t][k].fp)) * 100) : null,
    rw: q.length ? r1(avg(q.map((t) => G[t][k].rw))) : null, pend: ids.reduce((s, t) => s + G[t][k].pend, 0), miss: ids.reduce((s, t) => s + G[t][k].miss, 0),
    mixed: ids.filter((t) => G[t][k].mixed).length, runs: ids.reduce((s, t) => s + G[t][k].n, 0) });
  const eff = (f) => { const ok = q.filter((t) => G[t][A][f] != null && G[t][B][f] != null); return [ok.length, ok.length ? avg(ok.map((t) => G[t][A][f])) : null, ok.length ? avg(ok.map((t) => G[t][B][f])) : null]; };
  const [tn, ta, tb] = eff('tok'), [mn, ma, mb] = eff('min');
  const a = { ...side(A), tok: ta == null ? null : Math.round(ta), min: ma == null ? null : r1(ma) }, b = { ...side(B), tok: tb == null ? null : Math.round(tb), min: mb == null ? null : r1(mb) };
  const pct = (x, y) => (x == null || y == null || !x ? null : r1((y - x) / x * 100));
  return { A, B, ids, q: q.length, tn, mn, a, b, dRate: a.rate == null || b.rate == null ? null : r1(b.rate - a.rate), dFp: a.fp == null || b.fp == null ? null : r1(b.fp - a.fp),
    dTok: pct(a.tok, b.tok), dMin: pct(a.min, b.min), dRw: a.rw == null || b.rw == null ? null : r1(b.rw - a.rw) };
}
function own(k) { // 한 구조의 모든 작업 기준 값 (기준 구조 행)
  const G = agg(runs()), ids = Object.keys(G).filter((t) => G[t][k]), q = ids.filter((t) => G[t][k].j);
  const mean = (f) => { const v = q.map((t) => G[t][k][f]).filter((x) => x != null); return [v.length, v.length ? avg(v) : null]; };
  const [tn, tok] = mean('tok'), [mn, min] = mean('min');
  return { n: ids.length, q: q.length, rate: q.length ? r1(avg(q.map((t) => G[t][k].pass)) * 100) : null, fp: q.length ? r1(avg(q.map((t) => G[t][k].fp)) * 100) : null,
    tok: tok == null ? null : Math.round(tok), tn, min: min == null ? null : r1(min), mn,
    pend: ids.reduce((s, t) => s + G[t][k].pend, 0), mixed: ids.filter((t) => G[t][k].mixed).length, miss: ids.reduce((s, t) => s + G[t][k].miss, 0) };
}
function verdict(c) {
  const k = S.crit;
  if (c.q < (k.min_tasks || 1)) return ['표본 부족', 'wait', `판정된 비교 작업 ${c.q}개 (기준 ${k.min_tasks}개 이상)`];
  if (c.dRate != null && c.dRate < -(k.rate_drop_max || 0)) return ['품질 하락', 'bad', `합격률 ${sg(c.dRate)}%p`];
  const tokOk = c.dTok != null && c.dTok <= -(k.token_cut_min || 0), timeOk = c.dMin == null ? (k.time_cut_min || 0) <= 0 : c.dMin <= -(k.time_cut_min || 0);
  if (tokOk && timeOk) return ['채택 후보', 'ok', `토큰 ${sg(c.dTok)}% · 시간 ${sg(c.dMin)}%`];
  return ['효과 부족', 'jw', `토큰 ${sg(c.dTok)}% · 시간 ${sg(c.dMin)}%`];
}

function ensureSel() {
  const keys = structs().map((s) => s.key);
  if (!keys.includes(S.base)) S.base = keys[0] || '';
  if (!keys.includes(S.comp) || S.comp === S.base) S.comp = keys.find((k) => k !== S.base) || S.base;
}
function renderBar() {
  ensureSel();
  const opts = () => structs().map((s) => el('option', { value: s.key, text: sLabel(s.key) }));
  $('#base').replaceChildren(...opts()); $('#base').value = S.base;
  $('#comp').replaceChildren(...opts()); $('#comp').value = S.comp;
  const seen = new Map(); (S.d.runs || []).forEach((r) => seen.set(r.project, r.project_name));
  const sel = $('#project'); sel.replaceChildren(el('option', { value: 'ALL', text: '전체 프로젝트' }), ...[...seen].map(([id, n]) => el('option', { value: id, text: n && n !== id ? `${id} · ${n}` : id })));
  if (![...seen.keys(), 'ALL'].includes(S.project)) S.project = 'ALL';
  sel.value = S.project; $('#risk').value = S.risk;
  const lb = $('#label'); lb.textContent = S.live ? `실시간 · ${S.d.meta.generated_at || ''}` : [S.d.meta.label, S.d.meta.generated_at].filter(Boolean).join(' · ') || '스냅샷';
  lb.classList.toggle('live', S.live); $('#loadwrap').hidden = S.live;
  ['rate', 'tok', 'time', 'min'].forEach((k) => { const m = { rate: 'rate_drop_max', tok: 'token_cut_min', time: 'time_cut_min', min: 'min_tasks' }[k]; const i = $('#c_' + k); if (document.activeElement !== i) i.value = S.crit[m] ?? ''; });
}

function judgeForm(r) {
  if (!S.live || r.status !== '판정 대기') return null;
  let who = ''; try { who = localStorage.getItem('mes_judge') || ''; } catch (e) {}
  const reason = el('input', { type: 'text', placeholder: '불합격 사유 (불합격일 때 필수)' });
  const by = el('input', { type: 'text', class: 'who', placeholder: '판정자', value: who });
  const msg = el('span', { class: 'mut' });
  const send = async (final) => {
    if (!by.value.trim()) { msg.textContent = '판정자를 입력하세요.'; return; }
    if (final === '불합격' && !reason.value.trim()) { msg.textContent = '불합격 사유를 입력하세요.'; return; }
    try { localStorage.setItem('mes_judge', by.value.trim()); } catch (e) {}
    msg.textContent = '저장 중…';
    const res = await fetch('api/judge', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ run_id: r.run_id, final, fail_reason: final === '불합격' ? reason.value.trim() : '', by: by.value.trim() }) });
    const j = await res.json().catch(() => ({})); msg.textContent = res.ok ? '저장됨' : (j.error || '저장 실패'); if (res.ok) { if (document.activeElement) document.activeElement.blur(); poll(true); }
  };
  return el('div', { class: 'judge' },
    el('button', { class: 'btn ok', type: 'button', onclick: (e) => { e.preventDefault(); send('합격'); } }, '합격'),
    el('button', { class: 'btn bad', type: 'button', onclick: (e) => { e.preventDefault(); send('불합격'); } }, '불합격'), reason, by, msg);
}
function renderLive() {
  const live = (S.d.live || []).filter((x) => S.project === 'ALL' || x.project === S.project);
  $('#syncnote').textContent = S.live ? `5초마다 자동 갱신 · 마지막 갱신 ${hm(Date.now())} · 기록 없음이 ${S.d.stale_min || 5}분 넘으면 멈춤 의심으로 표시` : '스냅샷 파일입니다. 실시간으로 보려면 mes_compare.py serve 로 여세요.';
  $('#lives').replaceChildren(...(live.length ? live.map((x) => el('div', { class: 'lv' + (x.stale || x.wrong_section ? ' stale' : ''), style: `border-left-color:${col(x.structure)}` },
    el('div', { class: 't' }, `${x.task} `, el('span', { class: 'mut' }, `${x.run_id}`),
      x.stale ? el('span', { class: 'pill wait', style: 'margin-left:6px' }, '멈춤 의심') : null, x.wrong_section ? el('span', { class: 'pill bad', style: 'margin-left:6px' }, '섹션 불일치') : null,
      x.sessions > 1 ? el('span', { class: 'pill wait', style: 'margin-left:6px' }, `세션 ${x.sessions}개`) : null),
    el('div', { class: 'row' }, el('span', { class: 'k' }, '구조'), el('span', {}, `${x.structure} · 섹션 ${x.section || '—'}`)),
    el('div', { class: 'row' }, el('span', { class: 'k' }, '에이전트'), el('span', {}, x.agent || '—')),
    el('div', { class: 'row' }, el('span', { class: 'k' }, '지금 스킬'), el('span', { class: 'now' }, x.skill || '—')),
    el('div', { class: 'row' }, el('span', { class: 'k' }, '최근 파일'), el('span', { class: 'chip f' }, x.file || '—')),
    el('div', { class: 'row' }, el('span', { class: 'k' }, '경과'), el('span', { 'data-dur': x.started }, dur(x.started)), el('span', { class: 'mut' }, ` · 토큰 ${nf(x.tokens)} · 스킬 ${x.skills_n} · 파일 ${x.files_n}`)),
    el('div', { class: 'row' }, el('span', { class: 'k' }, '최근 기록'), el('span', { 'data-ago': x.last_ts }, ago(x.last_ts)))
  )) : [el('div', { class: 'mut' }, '진행 중인 실행이 없습니다.')]));
  const pend = allRuns().filter((r) => r.status === '판정 대기');
  $('#pendh').textContent = `사람 판정 필요 ${pend.length}건`;
  const keep = document.activeElement && document.activeElement.closest('.judge');
  if (!keep) $('#pend').replaceChildren(...(pend.length ? pend.map((r) => el('div', { class: 'pi', style: `border-left:3px solid ${col(r.structure)}` },
    el('div', {}, el('span', { class: 'pill jw' }, '판정 대기'), ` ${r.task} `, el('span', { class: 'mut' }, `${r.run_id} · 토큰 ${nf(r.tokens)} · ${f1(r.minutes)}분 · 재작업 ${r.rework ?? '—'} · 자동 검증(참고) ${r.checks_passed ?? '—'}/${r.checks_total ?? '—'}`)),
    r.ai_note ? el('div', { class: 'mut' }, `AI 메모: ${r.ai_note}`) : null,
    judgeForm(r) || el('div', { class: 'mut' }, '판정은 실시간 대시보드(serve)나 judge 명령으로 입력합니다.'))) : [el('div', { class: 'mut' }, '판정할 실행이 없습니다.')]));
  const ids = new Set(allRuns().map((r) => r.run_id));
  const feed = (S.d.feed || []).filter((f) => (S.project === 'ALL' || ids.has(f.run_id)) && (S.feed === 'all' || f.key));
  $$('[data-feed]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.feed === S.feed)));
  const fbox = $('#feed'), open = new Set([...fbox.querySelectorAll('details[open]')].map((x) => x.dataset.k));
  fbox.replaceChildren(...(feed.length ? feed.map((f) => {
    const k = `${f.run_id}|${f.ts}`;
    const tx = f.items && f.items.length > 1
      ? el('details', { 'data-k': k, open: open.has(k) ? '' : null }, el('summary', {}, f.text, el('span', { class: 'mut' }, ' ▾')), el('ul', {}, ...[...new Set(f.items)].map((x) => el('li', {}, x))))
      : el('span', { title: f.items && f.items[0] ? f.items[0] : null }, f.text);
    return el('li', { class: (f.key ? 'k' : '') + (f.old ? ' old' : '') },
      el('span', { class: 'mut', title: f.ts0 !== f.ts ? `${hm(f.ts0)} ~ ${hm(f.ts)}` : null }, hm(f.ts)), el('span', { class: 'd', style: `background:${col(f.structure)}` }),
      el('span', { class: 'mut' }, runName(f.run_id)),
      el('span', { class: 'ty' + (f.kind === 'judge' ? (f.final === '합격' ? ' ok' : ' bad') : '') }, f.old ? '판정(정정 전)' : f.type), el('span', { class: 'tx' }, tx));
  }) : [el('li', { class: 'mut' }, '기록이 없습니다.')]));
}
const runName = (rid) => { const m = /^(?:(.*)\/)?([^:]+):(.+)#(\d+)$/.exec(rid || ''); return m ? `${m[2]} · ${m[3]}${m[4] !== '1' ? ' #' + m[4] : ''}` : rid; };

function renderBoard() {
  const B = S.base; $('#lbbase').textContent = B ? `· 기준: ${sLabel(B)}` : '';
  const cls = (v, up) => (v == null || v === 0 ? '' : (up ? v > 0 : v < 0) ? 'good' : 'badc');
  const head = el('tr', {}, ...['구조', '구성', '비교 작업', '합격률', '첫 시도 합격률', '평균 토큰', '평균 시간(분)', '판정 대기 · 엇갈림', '채택 판단'].map((t) => el('th', {}, t)));
  const rows = structs().map((s) => {
    const isB = s.key === B, c = isB ? null : compare(B, s.key);
    const comp = `에이전트 ${s.agents.length} · 스킬 ${s.skills.length} · 규칙 ${s.files.length}`;
    const cellv = (v, d, unit, up, fmt = f1) => el('td', {}, el('span', { class: 'big2' }, v == null ? '—' : fmt(v) + unit), d === undefined ? null : el('span', { class: 'd ' + cls(d, up) }, ` ${sg(d)}${unit === '%' && up ? 'p' : unit === '%' ? '%' : '%'}`));
    if (isB) {
      const o = own(s.key), others = structs().filter((x) => x.key !== B).length;
      return el('tr', { class: 'base' }, el('td', {}, el('i', { class: 'sw', style: `background:${col(s.key)}` }), el('b', {}, sLabel(s.key)), el('div', { class: 'mut' }, `기준 · 섹션 ${s.section || '—'} · 지문 ${s.fp}`)),
        el('td', { class: 'mut' }, comp),
        el('td', {}, `${o.q} / ${o.n}`, el('div', { class: 'mut' }, '판정됨 / 전체 작업')),
        el('td', {}, el('span', { class: 'big2' }, o.rate == null ? '—' : f1(o.rate) + '%')),
        el('td', {}, el('span', { class: 'big2' }, o.fp == null ? '—' : f1(o.fp) + '%')),
        el('td', {}, el('span', { class: 'big2' }, nf(o.tok)), el('div', { class: 'mut' }, `합격 ${o.tn}개 작업 평균`)),
        el('td', {}, el('span', { class: 'big2' }, f1(o.min)), el('div', { class: 'mut' }, `합격 ${o.mn}개 작업 평균`)),
        el('td', {}, `${o.pend} · ${o.mixed}`, o.miss ? el('div', { class: 'flag' }, `스킬 미호출 ${o.miss}`) : null),
        el('td', {}, el('span', { class: 'pill jw' }, '기준'), el('div', { class: 'mut' }, others ? '아래 구조들의 "기준" 값은 공통 작업만 다시 평균한 값' : '비교할 다른 구조가 아직 없음')));
    }
    const [vt, vc, vn] = verdict(c);
    const tr = el('tr', { class: s.key === S.comp ? 'sel' : '', style: 'cursor:pointer', onclick: () => { S.comp = s.key; renderAll(); } },
      el('td', {}, el('i', { class: 'sw', style: `background:${col(s.key)}` }), el('b', {}, sLabel(s.key)), el('div', { class: 'mut' }, `섹션 ${s.section || '—'} · 지문 ${s.fp}${s.versions > 1 ? ' · 구조 변경됨' : ''}`)),
      el('td', { class: 'mut' }, comp),
      el('td', {}, `${c.q} / ${c.ids.length}`, el('div', { class: 'mut' }, `판정됨 / 공통 작업`)),
      el('td', {}, el('span', { class: 'big2' }, c.b.rate == null ? '—' : f1(c.b.rate) + '%'), el('span', { class: 'd ' + cls(c.dRate, true) }, ` ${sg(c.dRate)}%p`)),
      el('td', {}, el('span', { class: 'big2' }, c.b.fp == null ? '—' : f1(c.b.fp) + '%'), el('span', { class: 'd ' + cls(c.dFp, true) }, ` ${sg(c.dFp)}%p`)),
      el('td', {}, el('span', { class: 'big2' }, nf(c.b.tok)), el('span', { class: 'd ' + cls(c.dTok) }, ` ${sg(c.dTok)}%`), el('div', { class: 'mut' }, `기준 ${nf(c.a.tok)} · ${c.tn}개 작업`)),
      el('td', {}, el('span', { class: 'big2' }, f1(c.b.min)), el('span', { class: 'd ' + cls(c.dMin) }, ` ${sg(c.dMin)}%`), el('div', { class: 'mut' }, `기준 ${f1(c.a.min)} · ${c.mn}개 작업`)),
      el('td', {}, `${c.b.pend} · ${c.b.mixed}`, c.b.miss ? el('div', { class: 'flag' }, `스킬 미호출 ${c.b.miss}`) : null),
      el('td', {}, el('span', { class: 'pill ' + vc }, vt), el('div', { class: 'mut' }, vn)));
    return tr;
  });
  $('#lb').replaceChildren(el('thead', {}, head), el('tbody', {}, ...(rows.length ? rows : [el('tr', {}, el('td', { colspan: 9, class: 'mut' }, '아직 실행 기록이 없습니다.'))])));
}

function renderKpi() {
  const c = compare(S.base, S.comp), { a, b } = c, L = S.base, R = S.comp;
  const cls = (v, up) => (v == null || v === 0 ? '' : (up ? v > 0 : v < 0) ? 'good' : 'badc');
  if (L === R) { $('#take').textContent = '비교할 구조를 하나 더 실행하면 기준 구조와의 비교가 나옵니다.'; $('#kpi').replaceChildren(); $('#kpinote').textContent = ''; return; }
  $('#take').replaceChildren(`${R}는 ${L} 대비 토큰 `, el('b', { class: cls(c.dTok) }, sg(c.dTok) + '%'), ', 시간 ', el('b', { class: cls(c.dMin) }, sg(c.dMin) + '%'), ', 합격률 ', el('b', { class: cls(c.dRate, true) }, sg(c.dRate) + '%p'), ` 차이입니다${S.risk !== 'ALL' ? ` (${S.risk} 작업)` : ''}.`);
  const cell = (big, rest) => el('div', {}, el('span', { class: 'big' }, big), rest ? el('span', { class: 'mut' }, ' ' + rest) : null);
  const sw = (k) => el('i', { class: 'sw', style: `background:${col(k)}` });
  $('#kpi').replaceChildren(el('div', { class: 'h' }, '지표'), el('div', { class: 'h' }, sw(L), `기준 · ${sLabel(L)}`), el('div', { class: 'h' }, sw(R), `비교 · ${sLabel(R)}`), el('div', { class: 'h' }, '차이'),
    el('div', {}, '합격률', el('small', {}, `양쪽 모두 판정된 ${c.q}개 작업`)), cell(a.rate == null ? '—' : f1(a.rate) + '%'), cell(b.rate == null ? '—' : f1(b.rate) + '%'), el('div', { class: cls(c.dRate, true) }, sg(c.dRate) + '%p'),
    el('div', {}, '첫 시도 합격률', el('small', {}, '재작업 없이 합격')), cell(a.fp == null ? '—' : f1(a.fp) + '%'), cell(b.fp == null ? '—' : f1(b.fp) + '%'), el('div', { class: cls(c.dFp, true) }, sg(c.dFp) + '%p'),
    el('div', {}, '작업당 평균 토큰', el('small', {}, `양쪽 합격·측정 ${c.tn}개 작업`)), cell(nf(a.tok)), cell(nf(b.tok)), el('div', { class: cls(c.dTok) }, sg(c.dTok) + '%'),
    el('div', {}, '작업당 평균 시간(분)', el('small', {}, `양쪽 합격·측정 ${c.mn}개 작업`)), cell(f1(a.min)), cell(f1(b.min)), el('div', { class: cls(c.dMin) }, sg(c.dMin) + '%'),
    el('div', {}, '평균 재작업', el('small', {}, '판정된 실행 기준')), cell(f1(a.rw)), cell(f1(b.rw)), el('div', { class: cls(c.dRw) }, sg(c.dRw)));
  $('#kpinote').textContent = `공통 작업 ${c.ids.length}개 (판정 완료 ${c.q}개) · 실행 ${a.runs}건 / ${b.runs}건 · 판정 대기 ${a.pend} / ${b.pend} · 판정 엇갈림 ${a.mixed} / ${b.mixed}작업 · ${R} 스킬 미호출 ${b.miss}건`;
}

function renderDumb() {
  const box = $('#dumb'); box.replaceChildren();
  const L = S.base, R = S.comp, metric = S.metric;
  $('#dumbh').textContent = `프로젝트별 비교 · ${L} vs ${R}`;
  $('#dumbleg').replaceChildren(el('span', {}, el('i', { style: `background:${col(L)}` }), `${L} (기준)`), el('span', {}, el('i', { style: `background:${col(R)}` }), R));
  $$('[data-metric]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.metric === metric)));
  if (L === R) { box.append(el('div', { class: 'mut' }, '비교 구조를 고르세요.')); return; }
  const names = new Map(); (S.d.runs || []).forEach((r) => names.set(r.project, r.project_name));
  const cats = [['ALL', '전체'], ...[...names].map(([id, n]) => [id, n && n !== id ? `${id} · ${n}` : id])];
  const M = { rate: ['rate', 'q', (v) => `${f1(v)}%`, true], fp: ['fp', 'q', (v) => `${f1(v)}%`, true], tokens: ['tok', 'tn', nf, false], minutes: ['min', 'mn', (v) => `${f1(v)}분`, false], rework: ['rw', 'q', (v) => `${f1(v)}회`, false] }[metric];
  const [key, nkey, fmt, up] = M;
  const rows = cats.map(([id, label]) => { const c = compare(L, R, id); return { id, label, a: c.a[key], b: c.b[key], n: c[nkey], tasks: c.ids.length }; }).filter((r) => r.id === 'ALL' || r.tasks);
  const W = Math.max(280, box.clientWidth || 900), barH = 14, gap = 3, rowH = barH * 2 + gap + 18, mt = 4, H = mt + rows.length * rowH, maxL = W * .34;
  const cut = (s) => { if (tw(s) <= maxL) return s; let t = s; while (t.length > 1 && tw(t + '…') > maxL) t = t.slice(0, -1); return t + '…'; };
  const labels = rows.map((r) => cut(r.label)), ml = Math.max(...labels.map((s) => tw(s, 13))) + 16;
  const vals = rows.flatMap((r) => [r.a, r.b]).filter((v) => v != null);
  const pctM = metric === 'rate' || metric === 'fp';
  const rawMax = pctM ? 100 : Math.max(1, ...vals), step = Math.pow(10, Math.floor(Math.log10(rawMax))), nice = pctM ? 100 : Math.ceil(rawMax / step) * step;
  const vw = tw(fmt(nice) + ' (00개)') + 10, x = (v) => ml + (W - vw - ml) * v / nice;
  const svg = sv('svg', { width: '100%', viewBox: `0 0 ${W} ${H + 24}`, style: 'display:block;font-size:12px' }); box.append(svg);
  const nT = W < 480 ? 2 : 4;
  for (let i = 0; i <= nT; i++) { const v = nice * i / nT; svg.append(sv('line', { x1: x(v), x2: x(v), y1: mt, y2: H, stroke: 'var(--grid)' }), sv('text', { x: x(v), y: H + 16, 'text-anchor': 'middle', fill: 'var(--mut)' }, metric === 'tokens' && v >= 1000 ? `${r1(v / 1000)}k` : pctM ? `${v}%` : nf(r1(v)))); }
  rows.forEach((r, i) => {
    const y0 = mt + i * rowH + 8, sel = r.id === S.project, g = sv('g'); svg.append(g);
    if (r.id === 'ALL' && rows.length > 1) g.append(sv('line', { x1: 0, x2: W, y1: y0 + rowH - 10, y2: y0 + rowH - 10, stroke: 'var(--line)' }));
    g.append(sv('text', { x: ml - 10, y: y0 + barH + gap / 2, dy: '.32em', 'text-anchor': 'end', fill: sel ? 'var(--info)' : 'var(--fg)', 'font-size': 13, 'font-weight': r.id === 'ALL' || sel ? 700 : 400 }, labels[i]));
    [[r.a, L, y0], [r.b, R, y0 + barH + gap]].forEach(([v, m, yy]) => {
      if (v == null) { g.append(sv('text', { x: ml + 4, y: yy + barH / 2, dy: '.32em', fill: 'var(--mut)', 'font-size': 11 }, `${m} 집계할 작업 없음`)); return; }
      const wbar = Math.max(2, x(v) - ml);
      g.append(sv('rect', { x: ml, y: yy, width: wbar, height: barH, rx: 3, fill: col(m) }));
      g.append(sv('text', { x: ml + wbar + 6, y: yy + barH / 2, dy: '.32em', fill: 'var(--fg)', 'font-size': 11 }, `${fmt(v)} `), sv('text', { x: ml + wbar + 6 + tw(fmt(v) + ' ', 11), y: yy + barH / 2, dy: '.32em', fill: 'var(--mut)', 'font-size': 11 }, `(${r.n}개)`));
    });
    const hit = sv('rect', { x: 0, y: y0 - 6, width: W, height: rowH - 4, fill: 'transparent', style: r.id === 'ALL' ? null : 'cursor:pointer' }); g.append(hit);
    hit.addEventListener('mouseenter', (ev) => { const d = r.a != null && r.b != null ? (pctM ? r.b - r.a : r.a ? (r.b - r.a) / r.a * 100 : null) : null;
      tipShow(ev, [tl('t', r.label), tl('r', `${L}  ${r.a == null ? '—' : fmt(r.a)}`, col(L)), tl('r', `${R}  ${r.b == null ? '—' : fmt(r.b)}`, col(R)),
        d == null ? null : tl('r', `차이 ${sg(r1(d))}${pctM ? '%p' : '%'} ${d === 0 ? '' : (up ? d > 0 : d < 0) ? '(개선)' : '(악화)'}`), tl('n', `공통 작업 ${r.tasks}개 · 집계 ${r.n}개${r.id === 'ALL' ? '' : ' · 클릭하면 이 프로젝트만 보기'}`)].filter(Boolean)); });
    hit.addEventListener('mousemove', tipMove); hit.addEventListener('mouseleave', tipHide);
    if (r.id !== 'ALL') hit.addEventListener('click', () => { tipHide(); S.project = S.project === r.id ? 'ALL' : r.id; renderAll(); });
  });
}

const TCATS = [['기본 문맥', '#7d89a3'], ['이전 작업 이월', '#ff6b6b'], ['요청·훅 알림', '#9aa5ff'], ['AI 생각·답변', '#b36bff'], ['파일 읽기', '#4f8cff'], ['검색', '#38c6d9'],
  ['명령 실행', '#f5b748'], ['파일 수정', '#3ecf8e'], ['스킬', '#ff7aa8'], ['하위 에이전트', '#c3e88d'], ['기타 도구', '#5f6b85'], ['대화 압축', '#ff9f5a'], ['출력 생성', '#e6ecf7']];
const TDESC = { '기본 문맥': '시스템 지시·CLAUDE.md·스킬 목록·도구 목록 등 매 호출에 붙는 몫', '이전 작업 이월': '같은 세션의 앞 작업 대화를 이어 써서 함께 읽힌 몫',
  '요청·훅 알림': '사용자 요청, 훅이 붙인 알림·검증 결과, 스킬 본문 등', 'AI 생각·답변': 'AI가 쓴 생각·답변·도구 호출 내용이 다음 호출에서 다시 읽힌 몫',
  '출력 생성': 'AI가 새로 만든 출력 토큰', '대화 압축': '대화가 길어져 압축된 요약' };
const tcol = (c) => (TCATS.find((x) => x[0] === c) || [0, '#5f6b85'])[1];
const KINDS = [['calls', '호출 수'], ['base', '기본 문맥 (호출 1회)'], ['start', '시작 문맥 (이월 포함)'], ['max', '최대 문맥'], ['input', '입력 (캐시 아님)'], ['cache_write', '캐시 쓰기'], ['cache_read', '캐시 읽기'], ['output', '출력'], ['total', '합계']];
function tokAvg(list) {
  const L = list.filter((r) => r.tok), n = L.length, o = { n, parts: {} };
  if (!n) return o;
  KINDS.forEach(([k]) => { o[k] = L.reduce((s, r) => s + (r.tok[k] || 0), 0) / n; });
  L.forEach((r) => Object.entries(r.tok.parts || {}).forEach(([c, v]) => { o.parts[c] = (o.parts[c] || 0) + v / n; }));
  return o;
}
function stackBar(parts, total, max, thin) {
  const bar = el('div', { class: 'stack' + (thin ? ' thin' : ''), style: `width:${Math.max(1, total / max * 100)}%` });
  TCATS.forEach(([c, colr]) => { const v = parts[c]; if (!v) return;
    const seg = el('i', { style: `width:${v / total * 100}%;background:${colr}` }); bar.append(seg);
    seg.addEventListener('mouseenter', (ev) => tipShow(ev, [tl('r', c, colr), tl('t', `${nf(Math.round(v))} 토큰 · ${f1(v / total * 100)}%`), TDESC[c] ? tl('n', TDESC[c]) : null].filter(Boolean)));
    seg.addEventListener('mousemove', tipMove); seg.addEventListener('mouseleave', tipHide); });
  return bar;
}
function renderTokens() {
  const all = runs().filter((r) => r.tok && r.status !== '대기'), sel = $('#tview');
  const og = el('optgroup', { label: '실행 하나 (상위 항목까지)' });
  [...all].sort((a, b) => a.run_id.localeCompare(b.run_id)).forEach((r) => og.append(el('option', { value: r.run_id, text: `${runName(r.run_id)} · 토큰 ${nf(r.tok.total)}` })));
  sel.replaceChildren(el('option', { value: 'pair', text: `기준·비교 구조 평균 (공통 작업)` }), el('option', { value: 'tasks', text: `작업별 (${S.base} · ${S.comp})` }), el('option', { value: 'all', text: '모든 구조 평균' }), og);
  if (![...sel.options].some((o) => o.value === S.tview)) S.tview = 'pair';
  sel.value = S.tview;
  const box = $('#tbars'), det = $('#tdetail'); det.replaceChildren();
  if (!all.length) { box.replaceChildren(el('div', { class: 'mut' }, '토큰 상세를 계산할 대화 기록이 없습니다. (실시간 대시보드에서만 계산)')); $('#tleg').replaceChildren(); return; }
  const done = all.filter((r) => r.status !== '진행 중');
  $('#tsum').textContent = [...new Set([S.base, S.comp])].map((k) => { const a = tokAvg(done.filter((r) => r.structure === k)); if (!a.n) return null;
    const [c, v] = Object.entries(a.parts).sort((p, q) => q[1] - p[1])[0] || ['—', 0]; return `${k} 실행당 ${nf(Math.round(a.total))} (가장 큰 몫: ${c} ${f1(v / a.total * 100)}%)`; }).filter(Boolean).join(' · ');
  let rows = [], cols = [];
  const run = all.find((r) => r.run_id === S.tview);
  if (run) {
    rows = [{ label: runName(run.run_id), c: col(run.structure), a: tokAvg([run]), sub: run.task }]; cols = rows;
  } else if (S.tview === 'tasks' || S.tview === 'pair') {
    const ks = [...new Set([S.base, S.comp])], G = {};
    done.forEach((r) => { if (ks.includes(r.structure)) ((G[tkey(r)] = G[tkey(r)] || {})[r.structure] = G[tkey(r)][r.structure] || []).push(r); });
    const common = Object.keys(G).filter((t) => ks.every((k) => G[t][k])).sort();
    if (S.tview === 'pair') {
      rows = ks.map((k) => ({ label: k, c: col(k), a: tokAvg(common.flatMap((t) => G[t][k])), sub: `공통 작업 ${common.length}개 평균` })); cols = rows;
    } else {
      common.forEach((t) => { const r0 = G[t][ks[0]][0]; rows.push({ grp: `${r0.task_id} · ${String(r0.task).slice(0, 60)}` }); ks.forEach((k) => rows.push({ label: k, c: col(k), a: tokAvg(G[t][k]), thin: true })); });
      cols = ks.map((k) => ({ label: k, c: col(k), a: tokAvg(common.flatMap((t) => G[t][k])) }));
    }
  } else {
    rows = structs().map((s) => ({ label: s.key, c: col(s.key), a: tokAvg(done.filter((r) => r.structure === s.key)), sub: '' })).filter((x) => x.a.n);
    rows.forEach((x) => { x.sub = `실행 ${x.a.n}건 평균`; }); cols = rows;
  }
  const used = new Set(rows.flatMap((x) => (x.a ? Object.keys(x.a.parts) : [])));
  $('#tleg').replaceChildren(...TCATS.filter(([c]) => used.has(c)).map(([c, colr]) => el('span', { title: TDESC[c] || '' }, el('i', { style: `background:${colr}` }), c)));
  const max = Math.max(1, ...rows.filter((x) => x.a).map((x) => x.a.total || 0));
  box.replaceChildren(el('div', { class: 'tb' }, ...rows.flatMap((x) => x.grp ? [el('div', { class: 'grp' }, x.grp)] : [
    el('div', { class: 'lab', title: x.sub || x.label }, el('i', { class: 'sw', style: `background:${x.c}` }), x.label, x.sub && !x.thin ? el('div', { class: 'mut' }, x.sub) : null),
    el('div', { class: 'row' }, x.a.n ? stackBar(x.a.parts, x.a.total, max, x.thin) : el('span', { class: 'mut' }, '기록 없음'), el('span', { class: 'val' }, x.a.n ? nf(Math.round(x.a.total)) : ''))])));
  // 종류별 표 + 용도별 표
  const C = cols.filter((x) => x.a && x.a.n);
  if (!C.length) return;
  const th = (t, n) => el('th', { class: n ? 'n' : '' }, t);
  const kindT = el('table', { class: 'tt' }, el('tr', {}, th('토큰 종류 (실행 평균)'), ...C.map((x) => th(x.label, 1))),
    ...KINDS.map(([k, t]) => el('tr', {}, el('td', {}, t), ...C.map((x) => el('td', { class: 'n' }, k === 'calls' ? f1(x.a[k]) : nf(Math.round(x.a[k] || 0)))))));
  const cats = TCATS.filter(([c]) => C.some((x) => x.a.parts[c]));
  const partT = el('table', { class: 'tt' }, el('tr', {}, th('용도'), ...C.map((x) => th(x.label, 1))),
    ...cats.map(([c, colr]) => el('tr', { title: TDESC[c] || '' }, el('td', {}, el('i', { class: 'sw', style: `background:${colr}` }), c),
      ...C.map((x) => el('td', { class: 'n' }, nf(Math.round(x.a.parts[c] || 0)), el('span', { class: 'mut' }, ` ${f1((x.a.parts[c] || 0) / (x.a.total || 1) * 100)}%`))))));
  det.append(el('div', { class: 'grid' }, el('div', {}, partT), el('div', {}, kindT)));
  if (run && run.tok.top && run.tok.top.length) {
    det.append(el('h3', { style: 'margin:16px 0 6px' }, '토큰을 많이 쓴 항목 (크기 × 그 뒤 다시 읽힌 호출 수)'),
      el('table', { class: 'tt' }, el('tr', {}, th('항목'), th('용도'), th('크기', 1), th('다시 읽힘', 1), th('누적 토큰', 1), th('비율', 1)),
        ...run.tok.top.map((t) => el('tr', {}, el('td', { class: 'f' }, t.label), el('td', {}, el('i', { class: 'sw', style: `background:${tcol(t.cat)}` }), t.cat),
          el('td', { class: 'n' }, nf(t.tok)), el('td', { class: 'n' }, `${t.reads}회`), el('td', { class: 'n' }, nf(t.cost)), el('td', { class: 'n' }, `${f1(t.cost / run.tok.total * 100)}%`)))));
    if (run.tok.subs) det.append(el('p', { class: 'mut' }, '하위 에이전트: ' + Object.entries(run.tok.subs).map(([k, v]) => `${k} ${nf(v)}`).join(' · ')));
  }
}

function renderTiles() {
  const rs = runs();
  const T = [['작업', new Set(rs.map(tkey)).size], ['실행', rs.filter((r) => r.status !== '대기').length], ['구조', structs().length], ['진행 중', rs.filter((r) => r.status === '진행 중').length, 'hot'],
    ['판정 대기', rs.filter((r) => r.status === '판정 대기').length, 'hot'], ['스킬 미호출', rs.filter((r) => r.skill_called === '아니오' && r.status !== '진행 중').length, 'bad'],
    ['경고', rs.filter((r) => r.wrong_section || r.sessions > 1 || r.dirty_n).length, 'bad'], ['총 토큰', rs.reduce((s, r) => s + (r.tokens || 0), 0)]];
  $('#tiles').replaceChildren(...T.map(([k, v, c]) => el('div', { class: 'tile' + (c && v > 0 ? ' ' + c : '') }, el('div', { class: 'v' }, nf(v)), el('div', { class: 'k' }, k))));
}

const FILE_GROUPS = ['규칙·지침', '설계 문서', '소스 코드', '기타 파일'];
function fileGroup(f, conf) {
  const p = f.toLowerCase().replace(/\\/g, '/');
  if (conf.has(f) || /(^|\/)(\.claude|rules?|checks?|agents?|skills?|guides?|standards?)\//.test(p) || /(^|\/)claude(\.local)?\.md$/.test(p)) return '규칙·지침';
  if (/(^|\/)(docs?|design|designs|spec|specs|설계|문서)\//.test(p) || /\.(md|docx?|xlsx?|pdf|puml|drawio|pptx?|hwp)$/.test(p)) return '설계 문서';
  if (/\.(java|kt|kts|js|mjs|ts|tsx|jsx|vue|py|cs|vb|xml|sql|go|cpp|cc|c|h|hpp|rb|php|json|ya?ml|properties|gradle|jsp|html|css|scss)$/.test(p)) return '소스 코드';
  return '기타 파일';
}
function renderGview() {
  const sel = $('#gview'), rs = runs(), live = (S.d.live || []).filter((x) => S.project === 'ALL' || x.project === S.project);
  const opts = [el('option', { value: 'auto', text: live.length ? '자동 (진행 중 실행 우선)' : '자동 (기준·비교 구조)' }), el('option', { value: 'pair', text: `기준·비교 구조 (${S.base} · ${S.comp})` }), el('option', { value: 'all', text: '모든 구조' })];
  live.forEach((x) => opts.push(el('option', { value: x.run_id, text: `● 진행 중 · ${x.task} (${x.run_id})` })));
  rs.filter((r) => r.status !== '진행 중' && r.status !== '대기').sort((a, b) => a.run_id.localeCompare(b.run_id)).forEach((r) => opts.push(el('option', { value: r.run_id, text: `${r.task} (${r.run_id}) · ${r.final}` })));
  sel.replaceChildren(...opts);
  if (![...sel.options].some((o) => o.value === S.view)) S.view = 'auto';
  sel.value = S.view;
}
const STAR_NOTE = '구조 3D 보기입니다. 구조를 이루는 파일(CLAUDE.md·AGENTS.md, 스킬, 에이전트, 지식·지도 문서, 검사 규칙, 도구, 훅)이 점이고, 한 파일이 다른 파일·스킬을 가리키면 선으로 이어져 저절로 묶음이 생깁니다. 밝은 점은 보는 실행들에서 실제로 읽거나 부른 것, 흐린 점은 구조에만 있고 안 쓴 것입니다. 보기에서 실행 하나를 고르면 그 실행에서 쓴 것만 밝아집니다.';
const LINE_NOTE = '① 프로젝트 → ② 구조(또는 실행) → ③ 에이전트 → ④ 스킬 → ⑤ 참조 파일(규칙·지침 / 설계 문서 / 소스 코드) 순서입니다. 선은 실제로 그 에이전트가 스킬을 부르거나 파일을 읽은 기록이고, 굵을수록 여러 실행에서 쓴 것입니다. 점선은 구조에는 있지만 쓰지 않은 것, 빛나는 노드는 지금 사용 중인 것입니다. 실행 하나를 고르면 사용 순서 번호가 붙습니다.';
const FG_DESC = { '규칙·지침': 'CLAUDE.md·지식·스킬 등 AI 설정 파일', '설계 문서': '설계서·문서 (md·xlsx·pptx 등)', '소스 코드': '코드·프로젝트 파일', '기타 파일': '그 밖의 파일' };
const SCOL = { agent: '#ff7a3d', skill: '#6dffb0', '규칙·지침': '#ff5c7a', '설계 문서': '#ffd84d', '소스 코드': '#ff9a52', '기타 파일': '#b08d7e',
  '검색': '#3fd0e0', '명령 실행': '#a98bff', '파일 수정': '#4fdc8f', '파일 읽기': '#ff9a52', '기타 도구': '#7d8aa8' };
const RUNCOL = { 합격: '#fff0e0', 불합격: '#ff4d4d', 대기: '#f5b748', 진행: '#5aa9ff' };
const pairsAll = (r, f) => (Array.isArray(r[f]) ? r[f] : []).filter((x) => Array.isArray(x) && x[1]).map(([a, v]) => [a || MAIN, v]);
let starAnim = null;
const stopStars = () => { if (starAnim) cancelAnimationFrame(starAnim); starAnim = null; };
const SPR = {};
function sprite(hex, r) {
  const k = hex + '|' + r; if (SPR[k]) return SPR[k];
  const s = Math.ceil(r * 2), c = document.createElement('canvas'); c.width = c.height = s * 2;
  const g = c.getContext('2d'), gr = g.createRadialGradient(s, s, 0, s, s, s);
  gr.addColorStop(0, '#fff6ee'); gr.addColorStop(.22, hex); gr.addColorStop(.5, hex); gr.addColorStop(.62, hex + '55'); gr.addColorStop(1, hex + '00');
  g.fillStyle = gr; g.fillRect(0, 0, s * 2, s * 2); return (SPR[k] = c);
}
// ---- 구조 3D: 가운데 = 구조의 시작점(CLAUDE.md), 둘레 = 종류별 공(공 가운데 = 그 종류에서 가장 많이 연결된 파일)
const KIND_ORDER = ['지침', '스킬', '에이전트', '지식·문서', '검사 규칙', '도구', '훅·설정'];
const KCOL = { '지침': '#ffb36b', '스킬': '#ff9f43', '에이전트': '#a78bfa', '지식·문서': '#5ec8ff', '검사 규칙': '#ff6b7f', '도구': '#3fe0b0', '훅·설정': '#c7cbe0' };
const G3SHAPE = { '지침': 'ring', '스킬': 'diamond', '에이전트': 'square', '지식·문서': 'circle', '검사 규칙': 'triangle', '도구': 'cross', '훅·설정': 'hex' };
const KNAME = { '지침': '지침 (CLAUDE.md·AGENTS.md)', '지식·문서': '지식·지도 문서', '도구': '도구 스크립트' };
function shape3(g, sh, x, y, r) {
  g.beginPath();
  if (sh === 'square') g.rect(x - r * .9, y - r * .9, r * 1.8, r * 1.8);
  else if (sh === 'diamond') { g.moveTo(x, y - r * 1.35); g.lineTo(x + r * 1.15, y); g.lineTo(x, y + r * 1.35); g.lineTo(x - r * 1.15, y); g.closePath(); }
  else if (sh === 'triangle') { g.moveTo(x, y - r * 1.3); g.lineTo(x + r * 1.2, y + r * .8); g.lineTo(x - r * 1.2, y + r * .8); g.closePath(); }
  else if (sh === 'hex') { for (let i = 0; i < 6; i++) { const a = i * Math.PI / 3 + Math.PI / 6, px = x + r * 1.15 * Math.cos(a), py = y + r * 1.15 * Math.sin(a); i ? g.lineTo(px, py) : g.moveTo(px, py); } g.closePath(); }
  else if (sh === 'cross') { const w = r * .42; g.rect(x - r * 1.15, y - w, r * 2.3, w * 2); g.rect(x - w, y - r * 1.15, w * 2, r * 2.3); }
  else if (sh === 'ring') { g.arc(x, y, r * .75, 0, 7); g.fill(); g.beginPath(); g.lineWidth = Math.max(1.2, r * .32); g.arc(x, y, r * 1.35, 0, 7); g.stroke(); return; }
  else g.arc(x, y, r, 0, 7);
  g.fill();
}
function buildStruct3(k, mr, live) {
  const s = sOf(k), gr = s.graph || { nodes: [], edges: [] };
  const reads = new Map(), sk = new Map(), ag = new Map(), add = (m, key, rid) => { if (!m.has(key)) m.set(key, new Set()); m.get(key).add(rid); };
  mr.forEach((r) => { pairsAll(r, 'agent_files').forEach(([, f]) => add(reads, String(f).replace(/\\/g, '/'), r.run_id));
    pairsAll(r, 'agent_skills').forEach(([, x]) => add(sk, x, r.run_id)); split(r.agent).forEach((a) => add(ag, a, r.run_id)); });
  const hot = live.filter((x) => x.structure === k);
  const N = gr.nodes.map((gn) => {
    const auto = /^(\.claude\/)?CLAUDE(\.local)?\.md$/.test(gn.id) || (s.imports || []).some((im) => im.path === gn.id);
    const runs = auto ? new Set(mr.map((r) => r.run_id)) : (gn.kind === '스킬' ? sk.get(gn.label) : gn.kind === '에이전트' ? ag.get(gn.label) : null) || reads.get(gn.id) || new Set();
    return { id: gn.id, kind: gn.kind, label: gn.label, path: gn.id, desc: gn.desc || '', colr: KCOL[gn.kind] || '#c7cbe0', runs, used: runs.size > 0, auto, nb: new Set(), deg: 0,
      hot: hot.some((x) => { const rc = x.recent || [x.skill && 'sk:' + x.skill, x.agent && 'ag:' + x.agent, x.file && 'fi:' + x.file];
        return rc.includes(gn.kind === '스킬' ? 'sk:' + gn.label : gn.kind === '에이전트' ? 'ag:' + gn.label : 'fi:' + gn.id); }) };
  });
  const by = new Map(N.map((n) => [n.id, n])), E = [];
  gr.edges.forEach(([a, b]) => { const A = by.get(a), B = by.get(b); if (A && B && A !== B) { E.push({ a: A, b: B, ref: true }); A.nb.add(B); B.nb.add(A); A.deg++; B.deg++; } });
  const root = by.get('CLAUDE.md') || by.get('.claude/CLAUDE.md') || { id: '#root', kind: '지침', label: sLabel(k), path: '', desc: '', colr: KCOL['지침'], runs: new Set(), used: true, nb: new Set(), deg: 0 };
  root.root = true; root.hot = hot.length > 0; if (!by.has(root.id)) N.push(root);
  // 종류별 공
  const groups = KIND_ORDER.map((kd) => ({ kd, list: N.filter((n) => n.kind === kd && n !== root).sort((a, b) => b.deg - a.deg || a.label.localeCompare(b.label)) })).filter((gp) => gp.list.length);
  const rb = (n) => 26 + 11 * Math.sqrt(n);
  const ringR = Math.max(150, groups.reduce((t, gp) => t + 2 * rb(gp.list.length) + 40, 0) / (2 * Math.PI));
  const tot = groups.reduce((t, gp) => t + 2 * rb(gp.list.length) + 40, 0);
  let ang = -Math.PI / 2;
  root.bx = 0; root.by = 0; root.bz = 0; root.ox = 0; root.oy = 0; root.oz = 0; root.gi = -1;
  groups.forEach((gp, gi) => {
    const r = rb(gp.list.length), span = (2 * r + 40) / tot * Math.PI * 2; ang += span / 2;
    gp.x = Math.cos(ang) * ringR; gp.y = Math.sin(ang) * ringR; gp.r = r; gp.spin = (gi % 2 ? -1 : 1) * (.12 + .05 * (gi % 3)); ang += span / 2;
    gp.hub = gp.list[0];
    gp.list.forEach((n, i) => {
      n.gi = gi; n.bx = gp.x; n.by = gp.y; n.bz = 0;
      if (n === gp.hub) { n.ox = n.oy = n.oz = 0; n.hubOf = gp.kd; return; }
      const m = gp.list.length - 1, j = i - 1, yy = 1 - 2 * (j + .5) / m, rr = Math.sqrt(1 - yy * yy), th = j * 2.39996, d = r * (.45 + .55 * Math.cbrt(((j * 7919) % 97) / 97 + .01));
      n.ox = Math.cos(th) * rr * d; n.oy = yy * d; n.oz = Math.sin(th) * rr * d;
      if (!n.nb.has(gp.hub)) E.push({ a: gp.hub, b: n, spoke: true }); gp.hub.nb.add(n); n.nb.add(gp.hub);
    });
    E.push({ a: root, b: gp.hub, trunk: true }); root.nb.add(gp.hub); gp.hub.nb.add(root);
  });
  return { k, N, E, root, groups, ringR, by, hot };
}
let g3open = null;
function renderForce3D(box, keys, run, rs, live) {
  stopStars();
  const all = structs().map((s) => s.key);
  if (!all.includes(S.g3k)) S.g3k = S.base;
  const k = run ? run.structure : S.g3k;
  const mr = run ? [run] : rs.filter((r) => r.structure === k && r.status !== '대기');
  const G = buildStruct3(k, mr, live), { N, E, root, groups } = G;
  // 구조 고르기 버튼
  const pick = el('div', { class: 'seg sm', style: 'margin-bottom:8px' }, ...all.map((x) => el('button', { type: 'button', 'aria-pressed': String(x === k), disabled: run ? '' : null,
    onclick: () => { S.g3k = x; S.pick = null; S.g3grp = null; renderGraph(); } }, x)));
  box.append(el('div', { class: 'bar', style: 'margin-bottom:6px' }, el('span', { class: 'mut' }, run ? `실행 ${runName(run.run_id)} 의 구조` : '구조'), pick));
  const W = Math.max(320, box.clientWidth || 900), H = Math.min(820, Math.max(560, W * .78));
  const wrap = el('div', { class: 'g3wrap', style: `height:${H}px` });
  const cv = el('canvas', { class: 'stars', style: `height:${H}px;touch-action:none` }), dpr = devicePixelRatio || 1;
  cv.width = W * dpr; cv.height = H * dpr; wrap.append(cv); box.append(wrap);
  const g = cv.getContext('2d'); g.scale(dpr, dpr);
  // 왼쪽 범례 패널 + 위쪽 요약
  const ico = (sh, c) => { const ic = el('canvas', { width: 28, height: 28, style: 'width:14px;height:14px' }); const q = ic.getContext('2d'); q.scale(2, 2); q.fillStyle = c; q.strokeStyle = c; shape3(q, sh, 7, 7, sh === 'ring' ? 3.4 : 4.4); return ic; };
  const panel = el('div', { class: 'g3panel' }, el('div', { class: 'g3ph' }, '종류'),
    ...groups.map((gp) => { const u = gp.list.filter((n) => n.used).length;
      return el('div', { class: 'g3row' + (S.g3grp === gp.kd ? ' on' : ''), title: '누르면 이 종류만 강조', onclick: () => { S.g3grp = S.g3grp === gp.kd ? null : gp.kd; S.pick = null; renderGraph(); } },
        ico(G3SHAPE[gp.kd], KCOL[gp.kd]), el('span', {}, KNAME[gp.kd] || gp.kd), el('b', {}, String(gp.list.length)), el('small', {}, `사용 ${u}`)); }),
    el('div', { class: 'g3pf' }, `밝음 = ${run ? '이 실행에서' : `실행 ${mr.length}건에서`} 읽거나 부름 · 흐림 = 안 씀`));
  const head = el('div', { class: 'g3head' }, `${sLabel(k)} · ${root.id === '#root' ? '시작 파일 없음' : root.label + ' 기준'} · ` + groups.map((gp) => `${gp.kd} ${gp.list.length}`).join(' · '));
  wrap.append(panel, head);
  // 진행 중 표시: 구조 파일을 안 쓰는 동안에도 지금 무엇을 하는지 보이게
  G.hot.forEach((x) => { const ago = x.last_ts ? Math.max(0, Math.round((Date.now() - new Date(x.last_ts)) / 1000)) : null;
    const rc = (x.recent || []).slice().reverse(), used = rc.filter((key) => G.by.has(key.slice(3)) || N.some((n) => (n.kind === '스킬' && 'sk:' + n.label === key) || (n.kind === '에이전트' && 'ag:' + n.label === key)));
    const nm = (key) => key.startsWith('fi:') ? short(key.slice(3)) : key.slice(3);
    wrap.append(el('div', { class: 'g3live' + (x.stale ? ' stale' : '') },
      el('div', { class: 'g3lt' }, el('i', {}), `${x.stale ? '멈춤 의심' : '작업 중'} · ${runName(x.run_id)}${ago != null ? ` · ${ago < 90 ? ago + '초' : Math.round(ago / 60) + '분'} 전` : ''}`),
      el('div', {}, `지금: ${x.agent && x.agent !== '메인 세션' ? x.agent + ' · ' : ''}${rc[0] ? nm(rc[0]) : '시작하는 중'}`),
      el('div', { class: 'mut' }, used.length ? `최근 쓴 구조: ${used.slice(0, 4).map(nm).join(', ')} (깜빡임)` : groups.length ? '최근에는 구조 파일 없이 코드만 읽는 중' : '이 구조에는 구조 파일이 없음 · 코드만 읽는 중'))); });
  let ay = S.g3 ? S.g3.ay : 0, ax = S.g3 ? S.g3.ax : 0, uz = S.g3 ? S.g3.uz : 1, pan = S.g3 ? S.g3.pan : [0, 0], drag = null, hover = null, t0 = performance.now();
  let sel = (S.pick && G.by.get(S.pick)) || null;
  const maxR = Math.max(...groups.map((gp) => gp.r), 30), fitZ = Math.min(W - 200, H) * .47 / (G.ringR + maxR + 20);
  const pos = (n, t) => { const gp = n.gi >= 0 ? groups[n.gi] : null, a = gp ? t * gp.spin : 0, c = Math.cos(a), s2 = Math.sin(a);
    return [n.bx + n.ox * c - n.oz * s2, n.by + n.oy, n.bz + n.ox * s2 + n.oz * c]; };
  const proj = ([x, y, z]) => { const cy = Math.cos(ay), sy = Math.sin(ay), cx = Math.cos(ax), sx = Math.sin(ax);
    const x1 = x * cy - z * sy, z1 = x * sy + z * cy, y2 = y * cx - z1 * sx, z2 = y * sx + z1 * cx, D = 2200, s = D / (D + z2);
    return [W / 2 + 90 + pan[0] + x1 * s * fitZ * uz, H / 2 + pan[1] + y2 * s * fitZ * uz, z2, s]; };
  const draw = (now) => {
    const t = (now - t0) / 1000;
    N.forEach((n) => { const q = proj(pos(n, t)); n.sx = q[0]; n.sy = q[1]; n.sz = q[2]; n.ss = q[3]; });
    g.clearRect(0, 0, W, H);
    const foc = hover || sel, near = foc ? foc.nb : null, grp = S.g3grp;
    const lit = (n) => (!foc && !grp) || n === foc || (near && near.has(n)) || (grp && n.kind === grp) || n.root;
    // 선: 공 안의 바큇살, 구조→공 줄기, 참조
    E.forEach((e) => { const { a, b } = e, on = foc && (a === foc || b === foc), l = lit(a) && lit(b);
      const col0 = e.trunk ? 'rgba(255,220,190,' : e.spoke ? hexA(b.colr) : 'rgba(255,255,255,';
      const al = on ? .9 : e.trunk ? (l ? .35 : .08) : e.spoke ? (l ? (b.used ? .35 : .14) : .04) : (l ? (a.gi === b.gi ? .14 : .035) : .012);
      g.strokeStyle = col0 + al + ')'; g.lineWidth = on ? 1.4 : e.trunk ? 1.4 : .7; g.beginPath(); g.moveTo(a.sx, a.sy); g.lineTo(b.sx, b.sy); g.stroke(); });
    // 공 뒤의 은은한 빛
    g.globalCompositeOperation = 'lighter';
    groups.forEach((gp) => { const hb = gp.hub, rr = gp.r * hb.ss * fitZ * uz * 1.15, used = gp.list.filter((n) => n.used).length / gp.list.length;
      const gr = g.createRadialGradient(hb.sx, hb.sy, 0, hb.sx, hb.sy, rr); gr.addColorStop(0, hexA(KCOL[gp.kd]) + (.1 + .22 * used) * ((!grp || grp === gp.kd) && (!foc || foc.gi === gp.gi || near && [...near].some((x) => x.gi === gp.gi)) ? 1 : .3) + ')'); gr.addColorStop(1, hexA(KCOL[gp.kd]) + '0)');
      g.fillStyle = gr; g.beginPath(); g.arc(hb.sx, hb.sy, rr, 0, 7); g.fill(); });
    const rg = g.createRadialGradient(root.sx, root.sy, 0, root.sx, root.sy, 40); rg.addColorStop(0, 'rgba(255,190,130,.45)'); rg.addColorStop(1, 'rgba(255,190,130,0)'); g.fillStyle = rg; g.beginPath(); g.arc(root.sx, root.sy, 40, 0, 7); g.fill();
    g.globalCompositeOperation = 'source-over';
    const zs = N.map((n) => n.sz), zmin = Math.min(...zs), zmax = Math.max(...zs), depth = (z) => 1 - (z - zmin) / ((zmax - zmin) || 1);
    [...N].sort((a, b) => b.sz - a.sz).forEach((n) => {
      const big = n.root ? 9 : n.hubOf ? 7 : n.kind === '스킬' || n.kind === '에이전트' || n.kind === '지침' ? 4.6 : 3.4;
      const pulse = n.hot ? .5 + .5 * Math.sin(t * 5) : 0, rad = Math.max(2, big * n.ss * Math.pow(fitZ * uz, .3)) * (1 + pulse * .45);
      const a = (n.used ? .62 + .38 * depth(n.sz) : .18 + .12 * depth(n.sz)) * (lit(n) ? 1 : .18);
      g.globalAlpha = a; g.fillStyle = n.colr; g.strokeStyle = n.colr;
      if (n.used && lit(n)) { g.globalCompositeOperation = 'lighter'; g.globalAlpha = a * .35; g.beginPath(); g.arc(n.sx, n.sy, rad * 2.2, 0, 7); g.fill(); g.globalCompositeOperation = 'source-over'; g.globalAlpha = a; }
      shape3(g, n.root ? 'ring' : G3SHAPE[n.kind] || 'circle', n.sx, n.sy, rad);
      if (n.root || n.hubOf) { g.globalAlpha = Math.min(1, a + .2); g.strokeStyle = '#ffffff'; g.lineWidth = 1.2; g.beginPath(); g.arc(n.sx, n.sy, rad * (n.root ? 2 : 1.6), 0, 7); g.stroke(); }
      if (n === sel) { g.globalAlpha = 1; g.strokeStyle = '#ffffff'; g.lineWidth = 2; g.beginPath(); g.arc(n.sx, n.sy, rad + 6, 0, 7); g.stroke(); }
    });
    g.globalAlpha = 1; g.textAlign = 'left';
    N.forEach((n) => { const show = n.root || n.hubOf || n === foc || (near && near.has(n)) || (grp && n.kind === grp && n.used); if (!show) return;
      g.font = n.root ? '600 13px Pretendard, "Malgun Gothic", sans-serif' : n.hubOf ? '600 12px Pretendard, "Malgun Gothic", sans-serif' : '11px Pretendard, "Malgun Gothic", sans-serif';
      g.fillStyle = n === foc ? '#ffffff' : n.root || n.hubOf ? '#f4ece6' : 'rgba(235,225,215,.85)';
      const tx = n.root ? n.label : n.label.length > 34 ? n.label.slice(0, 33) + '…' : n.label, off = n.root ? 18 : n.hubOf ? 13 : 8;
      if (n.root) { g.textAlign = 'center'; g.fillText(tx, n.sx, n.sy + 30); if (n.id !== '#root') { g.fillStyle = '#a8968c'; g.font = '11px Pretendard, "Malgun Gothic", sans-serif'; g.fillText(sLabel(k), n.sx, n.sy + 44); } g.textAlign = 'left'; }
      else g.fillText(tx, n.sx + off, n.sy + 4); });
    S.g3 = { ay, ax, uz, pan };
    starAnim = requestAnimationFrame(draw);
  };
  const at = (ev) => { const b = cv.getBoundingClientRect(), x = ev.clientX - b.left, y = ev.clientY - b.top; let best = null, bd = 10;
    N.forEach((n) => { const d = Math.hypot(n.sx - x, n.sy - y); if (d < bd) { bd = d; best = n; } }); return best; };
  const zoomAt = (f, mx, my) => { const nu = Math.max(.4, Math.min(10, uz * f)); f = nu / uz; const cx0 = W / 2 + 90, cy0 = H / 2; pan = [(mx - cx0) - ((mx - cx0) - pan[0]) * f, (my - cy0) - ((my - cy0) - pan[1]) * f]; uz = nu; };
  cv.addEventListener('wheel', (ev) => { ev.preventDefault(); const b = cv.getBoundingClientRect(); zoomAt(Math.exp(-ev.deltaY * .0015), ev.clientX - b.left, ev.clientY - b.top); }, { passive: false });
  cv.addEventListener('pointerdown', (ev) => { drag = { x: ev.clientX, y: ev.clientY, ay, ax, pan: [...pan], shift: ev.shiftKey, moved: false }; cv.setPointerCapture(ev.pointerId); });
  cv.addEventListener('pointerup', (ev) => { const d = drag; drag = null; if (d && !d.moved) { const n = at(ev); sel = n; S.pick = n ? n.id : null; showPick3(n, run, mr); } });
  cv.addEventListener('pointermove', (ev) => {
    if (drag) { const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y; if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
      if (drag.shift || ev.shiftKey) pan = [drag.pan[0] + dx, drag.pan[1] + dy]; else { ay = drag.ay + dx * .006; ax = Math.max(-1.3, Math.min(1.3, drag.ax + dy * .006)); } tipHide(); return; }
    const n = at(ev); hover = n; cv.style.cursor = n ? 'pointer' : 'grab'; if (!n) { tipHide(); return; }
    tipShow(ev, [tl('t', `${n.root ? '시작 파일' : KNAME[n.kind] || n.kind} · ${n.label}${n.hot ? ' (지금 사용 중)' : ''}`, n.colr), n.path && n.path !== n.label ? tl('n', n.path) : null, n.desc ? tl('n', n.desc) : null,
      n.root && n.id === '#root' ? null : tl('r', n.auto ? '세션마다 자동으로 읽힘' : n.used ? `${run ? '이 실행에서' : `실행 ${n.runs.size}건에서`} ${n.kind === '스킬' ? '부름' : n.kind === '에이전트' ? '씀' : '읽음'}` : '안 씀'),
      tl('n', `참조 연결 ${n.nb.size}개 · 누르면 강조 고정`)].filter(Boolean)); });
  cv.addEventListener('pointerleave', () => { hover = null; tipHide(); });
  cv.addEventListener('dblclick', () => { ay = 0; ax = 0; uz = 1; pan = [0, 0]; });
  const zb = (t, f) => el('button', { type: 'button', class: 'btn', style: 'padding:2px 10px', onclick: f }, t);
  wrap.append(el('div', { class: 'zoom3' }, zb('+', () => zoomAt(1.4, W / 2 + 90, H / 2)), zb('−', () => zoomAt(1 / 1.4, W / 2 + 90, H / 2)), zb('처음 보기', () => { ay = 0; ax = 0; uz = 1; pan = [0, 0]; })));
  box.append(el('div', { class: 'mut', style: 'margin-top:8px' }, '가운데 = 구조의 시작 파일(CLAUDE.md) · 둘레의 공 = 종류별 묶음(공 가운데 = 그 종류에서 가장 많이 연결된 파일) · 선 = 파일이 다른 파일·스킬을 가리킴 · 점을 누르면 연결 강조 고정(빈 곳을 누르면 해제) · 왼쪽 종류를 누르면 그 종류만 강조 · 휠·+/− 확대 · 끌면 회전 · Shift+끌기 이동 · 두 번 누르면 처음 보기'));
  showPick3(sel, run, mr);
  starAnim = requestAnimationFrame(draw);
}
const hexA = (hex) => { const n = parseInt(hex.slice(1), 16); return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},`; };
const short = (p) => { const s = String(p).replace(/\\/g, '/'); return s.length > 60 ? '…' + s.slice(-58) : s; };
function showPick3(n, run, mr) {
  const box = $('#gpick');
  if (!n) { box.hidden = true; return; }
  const groups = {}; [...n.nb].forEach((x) => { (groups[x.kind] = groups[x.kind] || []).push(x); });
  box.hidden = false;
  box.replaceChildren(el('h3', {}, el('i', { class: 'sw', style: `background:${n.colr}` }), `${n.root ? '시작 파일' : KNAME[n.kind] || n.kind} · ${n.label}`,
      el('span', { class: 'mut' }, `  ${n.auto ? '세션마다 자동으로 읽힘' : n.used ? `${run ? '이 실행에서' : `실행 ${n.runs.size}/${mr.length}건에서`} 사용` : '안 씀'}`)),
    n.path && n.path !== n.label ? el('div', { class: 'mut' }, n.path) : null, n.desc ? el('div', { class: 'mut' }, n.desc) : null,
    ...KIND_ORDER.filter((kd) => groups[kd]).map((kd) => el('div', { style: 'margin-top:6px' }, el('b', {}, `연결된 ${KNAME[kd] || kd} ${groups[kd].length}개`),
      el('ul', {}, ...groups[kd].sort((a, b) => b.runs.size - a.runs.size || a.label.localeCompare(b.label)).map((x) => el('li', { style: x.used ? '' : 'opacity:.55' }, `${x.path || x.label}${x.used ? `  (실행 ${x.runs.size}건)` : ''}`))))));
}
function renderGraph() {
  const box = $('#graph'); box.replaceChildren(); renderGview();
  $$('[data-gmode]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.gmode === S.gmode)));
  $('#gnote').textContent = S.gmode === 'stars' ? STAR_NOTE : LINE_NOTE;
  if (S.gmode === 'stars') {
    const live0 = (S.d.live || []).filter((x) => S.project === 'ALL' || x.project === S.project);
    const view0 = S.view === 'auto' ? (live0[0] ? live0[0].run_id : 'pair') : S.view;
    const run0 = view0 === 'all' || view0 === 'pair' ? null : (S.d.runs || []).find((r) => r.run_id === view0);
    const keys0 = run0 ? [run0.structure] : view0 === 'pair' ? [...new Set([S.base, S.comp])] : structs().map((s) => s.key);
    $('#gleg').replaceChildren(...keys0.map((k) => el('span', {}, el('i', { style: `background:${col(k)}` }), k)));
    renderForce3D(box, keys0, run0, runs(), live0, new Set(structs().flatMap((s) => s.files)));
    return;
  }
  stopStars(); $('#gpick').hidden = true;
  const rs = runs(), all = S.d.runs || [];
  const live = (S.d.live || []).filter((x) => S.project === 'ALL' || x.project === S.project);
  const view = S.view === 'auto' ? (live[0] ? live[0].run_id : 'pair') : S.view;
  const run = view === 'all' || view === 'pair' ? null : all.find((r) => r.run_id === view);
  const keys = run ? [run.structure] : view === 'pair' ? [...new Set([S.base, S.comp])] : structs().map((s) => s.key);
  $('#gleg').replaceChildren(...keys.map((k) => el('span', {}, el('i', { style: `background:${col(k)}` }), k)));
  const conf = new Set(structs().flatMap((s) => s.files));
  const cfgSkills = (k) => { const s = sOf(k); const m = s.agent_skills || {}; const mapped = Object.entries(m).flatMap(([a, v]) => split(v).map((x) => [a, x])); const ms = new Set(mapped.map((p) => p[1])); return [...mapped, ...s.skills.filter((x) => !ms.has(x)).map((x) => [MAIN, x])]; };
  const cfgAgents = (k) => [MAIN, ...sOf(k).agents];
  const cfgFiles = (k) => sOf(k).files.map((f) => [MAIN, f]);
  const pairsOf = (r, f) => (Array.isArray(r[f]) ? r[f] : []).filter((x) => Array.isArray(x) && x[1]).map(([a, v]) => [a || MAIN, v]);
  const liveOf = run ? live.find((x) => x.run_id === run.run_id) : null;
  const hot = new Set((run ? (liveOf ? [liveOf] : []) : live).flatMap((x) => [x.agent && 'ag:' + x.agent, x.skill && 'sk:' + x.skill, x.file && 'fi:' + x.file]).filter(Boolean));
  const hotS = new Set(live.map((x) => x.structure));
  const N = new Map(), L = new Map();
  const node = (c, key, label, extra = {}) => { if (!N.has(key)) N.set(key, { col: c, key, label, n: {}, ...extra }); return N.get(key); };
  const link = (m, a, b, used, n = 1) => { const k = `${m}|${a}|${b}`; const l = L.get(k) || { m, a, b, c: 0, used: false }; if (used && n > 0) { l.c += n; l.used = true; } L.set(k, l); };
  node(0, 'p', run ? (run.project_name || run.project) : S.project === 'ALL' ? '전체 프로젝트' : ((rs[0] && rs[0].project_name) || S.project));
  const firstUse = {}; let o1 = 0, o2 = 0;
  keys.forEach((k) => {
    const mk = run ? 'run' : 'st:' + k, mr = run ? [run] : rs.filter((r) => r.structure === k && r.status !== '대기');
    node(1, mk, run ? run.task : sLabel(k), { m: k, sub: run ? `${run.run_id} · ${run.final}` : `섹션 ${sOf(k).section || '—'}` });
    link(k, 'p', mk, true, Math.max(1, mr.length));
    cfgAgents(k).forEach((a) => { node(2, 'ag:' + a, a); link(k, mk, 'ag:' + a, false); });
    mr.forEach((r) => split(r.agent).forEach((a) => { node(2, 'ag:' + a, a); link(k, mk, 'ag:' + a, true); }));
    mr.forEach((r) => pairsOf(r, 'agent_skills').forEach(([a, x]) => { node(2, 'ag:' + a, a); node(3, 'sk:' + x, x); link(k, 'ag:' + a, 'sk:' + x, true); if (run && !(('sk:' + x) in firstUse)) firstUse['sk:' + x] = ++o1; }));
    cfgSkills(k).forEach(([a, x]) => { node(2, 'ag:' + a, a); node(3, 'sk:' + x, x); link(k, 'ag:' + a, 'sk:' + x, false); });
    mr.forEach((r) => pairsOf(r, 'agent_files').forEach(([a, f]) => { node(2, 'ag:' + a, a); node(4, 'fi:' + f, f, { g: fileGroup(f, conf) }); link(k, 'ag:' + a, 'fi:' + f, true); if (run && !(('fi:' + f) in firstUse)) firstUse['fi:' + f] = ++o2; }));
    cfgFiles(k).forEach(([a, f]) => { node(2, 'ag:' + a, a); node(4, 'fi:' + f, f, { g: fileGroup(f, conf) }); link(k, 'ag:' + a, 'fi:' + f, false); });
  });
  L.forEach((l) => { const t = N.get(l.b); if (t && l.used) t.n[l.m] = (t.n[l.m] || 0) + l.c; });
  if (!run) FILE_GROUPS.forEach((g) => {
    const tot0 = (x) => Object.values(x.n).reduce((s, v) => s + v, 0);
    const fs = [...N.values()].filter((x) => x.col === 4 && x.g === g).sort((a, b) => tot0(b) - tot0(a) || a.label.localeCompare(b.label));
    const lim = g === '규칙·지침' ? 10 : 6;
    if (fs.length <= lim + 1) return;
    const rest = fs.slice(lim), key = 'fi:*' + g, aggN = node(4, key, `${g} 외 ${rest.length}개`, { g, agg: rest.map((x) => x.label) });
    rest.forEach((x) => { N.delete(x.key); [...L.entries()].forEach(([kk, l]) => { if (l.b === x.key) { L.delete(kk); link(l.m, l.a, key, false); const nl = L.get(`${l.m}|${l.a}|${key}`); if (l.used) { nl.used = true; nl.c += l.c; aggN.n[l.m] = (aggN.n[l.m] || 0) + l.c; } } }); });
  });
  const cols = [0, 1, 2, 3, 4].map((c) => [...N.values()].filter((x) => x.col === c));
  const tot = (x) => Object.values(x.n).reduce((s, v) => s + v, 0);
  cols[2].sort((a, b) => (a.label === MAIN ? -1 : b.label === MAIN ? 1 : 0) || tot(b) - tot(a) || a.label.localeCompare(b.label));
  const agIdx = new Map(cols[2].map((x, i) => [x.key, i]));
  const firstAg = (x) => Math.min(...[...L.values()].filter((l) => l.b === x.key).map((l) => agIdx.get(l.a) ?? 99));
  cols[3].sort((a, b) => (run ? (firstUse[a.key] || 99) - (firstUse[b.key] || 99) : 0) || firstAg(a) - firstAg(b) || tot(b) - tot(a) || a.label.localeCompare(b.label));
  cols[4].sort((a, b) => FILE_GROUPS.indexOf(a.g) - FILE_GROUPS.indexOf(b.g) || (a.agg ? 1 : 0) - (b.agg ? 1 : 0) || (run ? (firstUse[a.key] || 99) - (firstUse[b.key] || 99) : 0) || tot(b) - tot(a) || a.label.localeCompare(b.label));
  const badge = (x) => { if (run || x.col < 2) return ''; const ms = keys.filter((m) => x.n[m]); return ms.length && keys.length > 1 ? '  ' + ms.map((m) => `${m.length > 4 ? m.slice(0, 4) : m}${x.n[m]}`).join(' ') : ms.length ? `  ${tot(x)}` : ''; };
  const text = (x) => (run && firstUse[x.key] ? `${firstUse[x.key]}. ` : '') + x.label;
  const NH = 22, GAP = 6, GH = 20, PAD = 10;
  const wOf = (x) => Math.min(280, tw(text(x)) + tw(badge(x), 11) + PAD * 2 + 4);
  const cw = cols.map((c) => Math.max(70, ...c.map((x) => Math.max(wOf(x), x.sub ? tw(x.sub, 11) + PAD * 2 : 0))));
  const avail = box.clientWidth || 1100, gap = Math.max(46, (avail - cw.reduce((s, v) => s + v, 0)) / 4);
  const W = Math.max(avail, cw.reduce((s, v) => s + v, 0) + gap * 4);
  const X = []; cw.reduce((x, w, i) => { X[i] = x; return x + w + gap; }, 0);
  const colH = cols.map((c, i) => { let h = 0, g = null; c.forEach((x) => { if (i === 4 && x.g !== g) { g = x.g; h += GH; } h += (x.sub ? NH + 12 : NH) + GAP; }); return h; });
  const top = 30, H = top + Math.max(...colH, 120) + 6;
  cols.forEach((c, i) => { let y = top + (H - top - colH[i]) / 2, g = null; c.forEach((x) => { if (i === 4 && x.g !== g) { g = x.g; x.gh = y + 13; y += GH; } x.h = x.sub ? NH + 12 : NH; x.x = X[i]; x.w = cw[i]; x.y = y + x.h / 2; y += x.h + GAP; }); });
  const svg = sv('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}`, style: 'display:block;font-size:12px;min-width:100%' }); box.append(svg);
  const heads = ['① 프로젝트', run ? '② 실행' : '② 구조', '③ 에이전트', '④ 스킬', '⑤ 참조 파일'];
  heads.forEach((t, i) => svg.append(sv('text', { x: X[i], y: 14, fill: 'var(--mut)', 'font-size': 11, 'font-weight': 600 }, t)));
  const gl = sv('g'), gn = sv('g'); svg.append(gl, gn);
  const curve = (x1, y1, x2, y2) => { const mx = (x1 + x2) / 2; return `M${x1},${y1}C${mx},${y1} ${mx},${y2} ${x2},${y2}`; };
  const maxC = Math.max(1, ...[...L.values()].map((l) => l.c));
  const LL = [...L.values()].filter((l) => N.has(l.a) && N.has(l.b)).filter((l) => l.used || ![...L.values()].some((o) => o !== l && o.m === l.m && o.a === l.a && o.b === l.b && o.used)).map((l) => {
    const A = N.get(l.a), B = N.get(l.b);
    const p = sv('path', { d: curve(A.x + A.w, A.y, B.x, B.y), fill: 'none', stroke: col(l.m), 'stroke-width': l.used ? 1.5 + (run ? 1 : 4 * l.c / maxC) : 1.2, 'stroke-dasharray': l.used ? null : '4 4', opacity: l.used ? .6 : .35 });
    gl.append(p); return [l, p];
  });
  const touch = (key) => { let grow = true; const up = new Set([key]), dn = new Set([key]);
    while (grow) { grow = false; LL.forEach(([l]) => { if (dn.has(l.a) && !dn.has(l.b)) { dn.add(l.b); grow = true; } if (up.has(l.b) && !up.has(l.a)) { up.add(l.a); grow = true; } }); }
    return (l) => (dn.has(l.a) && dn.has(l.b)) || (up.has(l.a) && up.has(l.b)); };
  const focus = (f) => LL.forEach(([l, p]) => p.setAttribute('opacity', f(l) ? .95 : .05));
  const unfocus = () => { LL.forEach(([l, p]) => p.setAttribute('opacity', l.used ? .6 : .35)); tipHide(); };
  cols.forEach((c, i) => c.forEach((x) => {
    if (x.gh) gn.append(sv('text', { x: x.x, y: x.gh, fill: 'var(--info)', 'font-size': 11 }, x.g));
    const used = i < 2 || tot(x) > 0, on = hot.has(x.key) || (i === 1 && !run && hotS.has(x.m)) || (i === 1 && run && liveOf);
    const g = sv('g', { style: 'cursor:default' });
    const stroke = i === 1 ? col(x.m) : on ? 'var(--info)' : 'var(--line2)';
    if (on) { const halo = sv('rect', { x: x.x - 3, y: x.y - x.h / 2 - 3, width: x.w + 6, height: x.h + 6, rx: 9, fill: 'none', stroke: 'var(--info)', 'stroke-width': 2 }); halo.append(sv('animate', { attributeName: 'opacity', values: '1;.15;1', dur: '1.6s', repeatCount: 'indefinite' })); g.append(halo); }
    g.append(sv('rect', { x: x.x, y: x.y - x.h / 2, width: x.w, height: x.h, rx: 7, fill: i === 0 ? 'var(--fg)' : 'var(--panel)', stroke, 'stroke-width': i === 1 || on ? 1.6 : 1, 'stroke-dasharray': used ? null : '3 3' }));
    const lbl = sv('text', { x: x.x + PAD, y: x.sub ? x.y - 6 : x.y, dy: '.32em', fill: i === 0 ? 'var(--bg)' : on ? 'var(--info)' : used ? 'var(--fg)' : 'var(--mut)', 'font-weight': i < 2 ? 600 : 400 });
    let t = text(x); while (t.length > 2 && tw(t) + tw(badge(x), 11) + PAD * 2 > x.w) t = t.slice(0, -2) + '…';
    lbl.textContent = t; if (badge(x)) lbl.append(sv('tspan', { fill: 'var(--mut)', 'font-size': 11 }, badge(x)));
    g.append(lbl);
    if (x.sub) g.append(sv('text', { x: x.x + PAD, y: x.y + 9, dy: '.32em', fill: 'var(--mut)', 'font-size': 11 }, x.sub));
    gn.append(g);
    g.addEventListener('mouseenter', (ev) => {
      focus(i === 0 ? () => true : touch(x.key));
      const lines = [tl('t', `${heads[i].slice(2)} · ${x.label}${on ? ' (지금 사용 중)' : ''}`)];
      const dsc = keys.map((m) => (sOf(m).desc || {})[x.key]).find(Boolean); if (dsc) lines.push(tl('n', dsc));
      if (x.agg) lines.push(tl('n', x.agg.slice(0, 12).join(', ') + (x.agg.length > 12 ? ' …' : '')));
      if (i >= 2) keys.forEach((m) => { const ls = LL.filter(([l]) => l.m === m && l.b === x.key).map(([l]) => l); if (!ls.length) return; const u = ls.filter((l) => l.used);
        lines.push(tl('r', `${m}  ${u.length ? (run ? `사용${firstUse[x.key] ? ` · ${firstUse[x.key]}번째` : ''}` : `${u.reduce((s, l) => s + l.c, 0)}회 실행에서 사용`) : '구조에만 있음 · 사용 안 함'}${i > 2 && u.length ? ` · ${u.map((l) => N.get(l.a).label).join(', ')}` : ''}`, col(m))); });
      if (i === 1 && !run) { const s = sOf(x.m), mr = rs.filter((r) => r.structure === x.m); lines.push(tl('r', `실행 ${mr.length}건 · 합격 ${mr.filter((r) => r.final === '합격').length} · 판정 대기 ${mr.filter((r) => r.status === '판정 대기').length}`), tl('n', `에이전트 ${s.agents.length} · 스킬 ${s.skills.length} · 규칙 ${s.files.length} · 지문 ${s.fp || '—'}`)); }
      if (i === 1 && run) lines.push(tl('r', `${run.status} · 토큰 ${nf(run.tokens)} · ${f1(run.minutes)}분 · 재작업 ${run.rework ?? '—'}`), tl('n', `섹션 ${run.section || '—'} · 코드·문서 지문 ${run.code_fp || '—'} · 세션 ${run.sessions || 0}개`));
      tipShow(ev, lines);
    });
    g.addEventListener('mousemove', tipMove); g.addEventListener('mouseleave', unfocus);
  }));
}
const MAIN = '메인 세션';

function renderSetup() {
  const keys = [...new Set([S.base, S.comp])].filter(Boolean);
  const chips = (v, c) => { const it = split(v); return el('div', { class: 'chips' }, it.length ? it.map((s) => el('span', { class: 'chip' + (c ? ' ' + c : '') }, s)) : el('span', { class: 'chip none' }, '없음')); };
  const alist = (k, names, pre, field) => {
    const mr = runs().filter((r) => r.structure === k && r.status !== '대기'), ds = sOf(k).desc || {};
    if (!names.length) return el('span', { class: 'chip none' }, '없음');
    return el('div', { class: 'al' }, ...names.map((nm) => { const u = mr.filter((r) => split(r[field]).includes(nm)).length;
      const head = [el('span', { class: 'nm' }, nm), el('span', { class: 'us ' + (u ? 'good' : 'mut') }, u ? `실행 ${u}/${mr.length}건에서 사용` : `안 씀 (실행 ${mr.length}건)`)];
      return el('div', { class: 'it' + (u ? '' : ' un') }, ds[pre + nm] ? el('details', { 'data-k': k + pre + nm, open: S.openDesc.has(k + pre + nm) ? '' : null, ontoggle: (e) => { e.target.open ? S.openDesc.add(k + pre + nm) : S.openDesc.delete(k + pre + nm); } },
        el('summary', {}, ...head, el('span', { class: 'mut' }, ' ▾')), el('div', { class: 'ds' }, ds[pre + nm])) : el('div', {}, ...head)); }));
  };
  $('#setup').replaceChildren(...keys.map((k) => { const s = sOf(k); return el('div', {}, el('h3', {}, el('i', { class: 'sw', style: `background:${col(k)}` }), `${sLabel(k)}${k === S.base ? ' (기준)' : ''}`), el('dl', { class: 'kv' },
    el('dt', {}, '섹션 · 지문'), el('dd', {}, `${s.section || '—'} · ${s.fp || '—'}`), el('dt', {}, '에이전트'), el('dd', {}, alist(k, s.agents, 'ag:', 'agent')), el('dt', {}, '스킬'), el('dd', {}, alist(k, s.skills, 'sk:', 'skills')),
    el('dt', {}, '규칙·지침'), el('dd', {}, chips(s.files, 'f')), el('dt', {}, '플러그인'), el('dd', {}, chips(s.plugins)))); }));
  const L = S.base, R = S.comp, G = agg(runs()), ids = Object.keys(G).filter((t) => G[t][L] && G[t][R]);
  const c = {}; ids.forEach((t) => [L, R].forEach((k, i) => G[t][k].list.forEach((r) => split(r.skills).forEach((s) => { c[s] = c[s] || [0, 0]; c[s][i]++; }))));
  const rows = Object.entries(c).sort((a, b) => b[1][1] - a[1][1] || b[1][0] - a[1][0]), max = Math.max(1, ...rows.flatMap(([, v]) => v));
  $('#skleg').replaceChildren(el('span', {}, el('i', { style: `background:${col(L)}` }), L), el('span', {}, el('i', { style: `background:${col(R)}` }), R));
  $('#sk').replaceChildren(...(rows.length ? rows.flatMap(([s, v]) => [el('div', {}, s), el('div', { class: 'bars' }, [L, R].map((m, i) => el('div', { class: 'b' }, el('i', { style: `width:calc(${v[i] / max} * (100% - 2rem));background:${col(m)}` }), el('span', {}, v[i]))))]) : [el('div', { class: 'mut' }, '호출 기록이 없습니다.')]));
}

function renderAll() {
  renderBar(); renderLive(); renderBoard(); renderKpi(); renderTokens(); renderDumb(); renderTiles(); renderGraph(); renderSetup();
}
$$('[data-feed]').forEach((b) => b.addEventListener('click', () => { S.feed = b.dataset.feed; renderLive(); }));
$$('[data-gmode]').forEach((b) => b.addEventListener('click', () => { S.gmode = b.dataset.gmode; renderGraph(); }));
$('#tview').addEventListener('change', (e) => { S.tview = e.target.value; renderTokens(); });
$('#base').addEventListener('change', (e) => { S.base = e.target.value; if (S.comp === S.base) S.comp = ''; renderAll(); });
$('#comp').addEventListener('change', (e) => { S.comp = e.target.value; renderAll(); });
$('#project').addEventListener('change', (e) => { S.project = e.target.value; renderAll(); });
$('#risk').addEventListener('change', (e) => { S.risk = e.target.value; renderAll(); });
$('#gview').addEventListener('change', (e) => { S.view = e.target.value; renderGraph(); });
$$('[data-metric]').forEach((b) => b.addEventListener('click', () => { S.metric = b.dataset.metric; renderDumb(); }));
[['rate', 'rate_drop_max'], ['tok', 'token_cut_min'], ['time', 'time_cut_min'], ['min', 'min_tasks']].forEach(([id, k]) => $('#c_' + id).addEventListener('input', (e) => {
  const v = parseFloat(e.target.value); if (isNaN(v)) return; S.crit[k] = v; try { localStorage.setItem('mes_crit', JSON.stringify(S.crit)); } catch (er) {} renderBoard(); }));
$('#file').addEventListener('change', async (e) => {
  const f = e.target.files[0]; if (!f) return;
  try { const j = JSON.parse(await f.text()); S.d = { live: [], feed: [], structures: [], ...j, meta: { ...(j.meta || {}), label: f.name, live: false } }; S.base = S.d.baseline || ''; S.comp = ''; renderAll(); }
  catch (err) { alert('JSON 파일을 읽지 못했습니다: ' + err.message); }
  e.target.value = '';
});
let lastSig = '', rt;
async function poll(force) {
  try {
    const r = await fetch('api/state', { cache: 'no-store' }); if (!r.ok) throw new Error(r.status);
    const d = await r.json(); d.meta.live = true;
    const sig = JSON.stringify([d.runs, d.structures, d.live.map((x) => [x.run_id, x.skill, x.file, x.agent, x.tokens, x.stale, x.wrong_section, x.sessions]), d.feed[0]]);
    S.d = d;
    if (force || sig !== lastSig) { lastSig = sig; renderAll(); } else renderLive();
  } catch (e) { $('#syncnote').textContent = `서버 연결 끊김 (${e.message}) · 다시 시도 중`; }
}
setInterval(() => { $$('[data-dur]').forEach((e) => (e.textContent = dur(e.dataset.dur))); $$('[data-ago]').forEach((e) => (e.textContent = ago(e.dataset.ago))); }, 1000);
addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(() => { renderGraph(); renderDumb(); }, 150); });
renderAll();
if (S.live) setInterval(poll, 5000);
})();
</script>

</body>
</html>
'''

if __name__ == "__main__":
    main()
