"""mes_compare.py 짧은 명령.

가장 쉬운 방법 (플러그인 mes-compare 를 설치했을 때):
  aicompare root [비교폴더] [--yes]     비교 폴더 표시 (기본: 지금 프로젝트의 상위 폴더). 그 아래 폴더 = 섹션, 자동 감지·기록·대시보드
                                 Claude Code 에서는 "비교 시작해줘"

예전 방식 (섹션마다 훅 설치):
  python mc.py install           aicompare 명령 등록 (이후 어디서든 aicompare 로 실행)
  aicompare setup 섹션1=새구조 섹션2=현재 섹션3=구조없음
                                 섹션들을 한 기록 폴더에 연결: 기록 훅 + 자동 시작·종료 + 위험 명령 차단을 한 번에 등록
                                 (=이름 을 빼면 기존 이름·폴더 이름. --dir 로 기록 폴더 지정, 기본은 섹션들의 공통 상위/mes-compare-data)
  aicompare check                       연결한 섹션마다 훅·기록 폴더·구조 이름 점검
  aicompare dash                        대시보드를 열고 브라우저까지 띄움 (어디서든, 이미 켜져 있으면 브라우저만)

섹션(develop) 폴더나 그 하위 폴더 어디서든:
  aicompare start "LOT 분할 처리"       새 작업: 작업 ID(T01, T02 …)를 자동으로 붙여 시작
  aicompare new "LOT 분할 처리"         위와 같음
  aicompare start                       이 구조가 아직 안 한 작업 중 가장 최근 것으로 시작
  aicompare start T01                   작업 ID를 직접 지정 (설명은 기록에서 찾아 씀)
  aicompare start T01 "LOT 분할 처리"   작업 ID와 설명을 직접 지정
  aicompare end                         진행 중인 실행 종료 (판정 대기)
  aicompare tasks                       작업 목록과 구조별 실행 여부
  aicompare status                      텍스트 요약
  aicompare dash                        대시보드 (http://localhost:8765)
  aicompare <그 밖의 명령> ...           mes_compare.py 에 그대로 넘김 (judge, reset, structure 등)

구조 이름: 섹션의 .mes-structure 파일 > apply 한 구조 이름(.mes-profile.json) > 폴더 이름
기록 폴더: 섹션의 .mes-dir
"""
import json
import re
import subprocess
import sys
from pathlib import Path

TOOL = str(Path(__file__).resolve().with_name("mes_compare.py"))
CONF = Path.home() / ".mes-compare.json"   # aicompare setup 이 기억하는 기록 폴더·섹션 목록
PORT = 8765
ID_RE = re.compile(r"^[A-Za-z]+-?\d+$")   # T01, P1-T01 같은 모양이면 작업 ID 로 본다
AUTO_RE = re.compile(r"^T(\d+)$")


def find_section(quiet=False):
    p = Path.cwd().resolve()
    for q in [p, *p.parents]:
        if (q / ".mes-dir").is_file():
            return q
    if quiet:
        return None
    sys.exit("섹션 폴더(.mes-dir 가 있는 develop 폴더) 안에서 실행하세요.")


def structure_name(sec):
    f = sec / ".mes-structure"
    if f.is_file() and f.read_text(encoding="utf-8-sig").strip():
        return f.read_text(encoding="utf-8-sig").strip()
    try:
        name = json.loads((sec / ".mes-profile.json").read_text(encoding="utf-8-sig")).get("name")
    except Exception:
        name = None
    return name or sec.name


def read_tasks(data_dir):
    """기록에서 작업 목록 → (처음 나온 순서의 [작업ID], {작업ID: 설명}, {작업ID: {실행한 구조}})"""
    order, desc, ran = [], {}, {}
    try:
        with open(Path(data_dir) / "events.jsonl", encoding="utf-8") as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                tid = ev.get("task_id")
                if not tid:
                    continue
                if tid not in desc:
                    order.append(tid)
                    desc[tid] = ""
                if ev.get("task"):
                    desc[tid] = ev["task"]
                if ev.get("type") == "run_start" and ev.get("structure"):
                    ran.setdefault(tid, set()).add(ev["structure"])
    except FileNotFoundError:
        pass
    return order, desc, ran


def next_id(order):
    nums = [int(m.group(1)) for m in (AUTO_RE.match(t) for t in order) if m]
    return f"T{(max(nums) + 1) if nums else 1:02d}"


# ---------------------------------------------------------------- 자동 기록 (Claude Code 훅)
# UserPromptSubmit: 섹션 세션에 작업을 입력하면 그 입력을 작업 설명으로 실행을 자동 시작.
#   같은 세션의 이어지는 입력은 같은 실행에 포함. 다른 세션에서 새 작업을 입력하면 이전 실행을 자동 종료.
# SessionEnd: 실행을 시작한 세션을 닫으면 자동 종료.
AUTO_STATE = "auto-state.json"
MIN_PROMPT = 10  # 이보다 짧은 입력(test 등)은 작업으로 보지 않음
# 비교 도구를 다루는 입력("비교 시작해줘", 확인 답장 등)은 작업이 아니므로 실행을 시작·종료하지 않는다
CONTROL_RE = re.compile(r"aicompare|mes_compare|mc\.py|비교\s*(시작|폴더|설정|연결|확정|중지|끝)|판정\s*입력")


def _load(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def _last_ts(transcript):
    """대화 기록의 마지막 시각 (작업 시간 계산용)."""
    last = None
    try:
        with open(transcript, encoding="utf-8") as fh:
            for line in fh:
                try:
                    ts = json.loads(line).get("timestamp")
                except Exception:
                    continue
                if ts:
                    last = ts
    except Exception:
        pass
    return last


def _minutes(started, last):
    import datetime as dt
    try:
        s = dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
        e = dt.datetime.fromisoformat(last.replace("Z", "+00:00"))
        return max(round((e - s).total_seconds() / 60, 1), 0.0)
    except Exception:
        return None


def _auto_end(sec, data_dir, cur, log):
    cmd = [sys.executable, TOOL, "--dir", data_dir, "end", "--note", "자동 종료"]
    m = _minutes(cur.get("started", ""), _last_ts(cur.get("transcript_path", "")) or "")
    if m is not None:
        cmd += ["--minutes", str(m)]
    r = subprocess.run(cmd, cwd=str(sec), capture_output=True, text=True, encoding="utf-8", errors="replace")
    log(f"자동 종료 {cur.get('run_id')}: {r.stdout.strip() or r.stderr.strip()}")


def clean_prompt(p):
    """앱이 붙이는 안내 문구를 떼고 사용자가 보낸 질문 본문만 남긴다 (같은 질문이 같은 작업 ID 로 묶이도록)."""
    m = re.search(r"<cross-session-message[^>]*>(.*?)</cross-session-message>", p, re.S)
    if m:
        p = m.group(1)
    p = re.sub(r"<system-reminder>.*?</system-reminder>", "", p, flags=re.S)
    if "Background (from the session" in p:
        m = re.search(r"\bTask:\s*(.*?)\s*(?:Shared notes folder|$)", p, re.S)
        if m:
            p = m.group(1)
    return " ".join(p.split())


def _cur_key(sec):
    """mes_compare.cur_key 와 같은 규칙: 섹션마다 진행 중 실행·자동 상태 파일을 따로 둔다."""
    import hashlib
    return hashlib.sha1(str(Path(sec).resolve()).lower().encode("utf-8")).hexdigest()[:10]


def auto_hook(payload=None):
    if payload is None:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace") or "{}")
    cwd = Path(payload.get("cwd") or ".").resolve()
    sec = next((q for q in [cwd, *cwd.parents] if (q / ".mes-dir").is_file()), None)
    if not sec:
        return
    data_dir = (sec / ".mes-dir").read_text(encoding="utf-8").strip()
    d = Path(data_dir)
    log_p = d / "auto-hook.log"

    def log(msg):
        import datetime as dt
        with open(log_p, "a", encoding="utf-8") as fh:
            fh.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}\n")

    event, sid = payload.get("hook_event_name", ""), payload.get("session_id", "")
    cur_p, st_p = d / f"current-{_cur_key(sec)}.json", d / f"auto-state-{_cur_key(sec)}.json"
    cur = _load(cur_p)
    st = _load(st_p, {}) or {}
    mine = bool(cur and st.get("run_id") == cur.get("run_id"))  # 자동으로 시작한 실행인가 (aicompare start 로 시작한 것은 건드리지 않음)

    if event == "SessionEnd":
        if mine and st.get("session") == sid:
            _auto_end(Path(cur.get("section") or sec), data_dir, cur, log)
        return
    if event != "UserPromptSubmit":
        return
    prompt = clean_prompt(payload.get("prompt") or "")
    if len(prompt) < MIN_PROMPT or prompt.startswith("/"):
        return
    if CONTROL_RE.search(prompt):
        log(f"건너뜀: 비교 도구 조작 입력 ({prompt[:60]})")
        return
    if cur:
        if not mine:
            log(f"건너뜀: 수동으로 시작한 실행 {cur.get('run_id')} 진행 중")
            return
        if st.get("session") == sid:
            # 같은 세션의 이어지는 입력. 다른 세션이 보낸 새 과제(cross-session-message)만 새 실행으로 나눈다.
            order, desc, _ = read_tasks(data_dir)
            cur_task = desc.get(str(cur.get("run_id", "")).split(":")[0])
            if "<cross-session-message" not in (payload.get("prompt") or "") or prompt[:300] == cur_task:
                return
        _auto_end(Path(cur.get("section") or sec), data_dir, cur, log)
    task = prompt[:300]
    order, desc, _ = read_tasks(data_dir)
    same = [t for t in order if desc.get(t) == task]
    task_id = same[-1] if same else next_id(order)
    r = subprocess.run([sys.executable, TOOL, "--dir", data_dir, "start", "--task-id", task_id, "--task", task,
                        "--structure", structure_name(sec)], cwd=str(sec), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    log(f"자동 시작 ({sec}): {r.stdout.strip() or r.stderr.strip()}")
    cur = _load(cur_p)
    if r.returncode == 0 and cur:
        if payload.get("transcript_path"):
            cur["transcript_path"] = payload["transcript_path"]
        cur["session"] = sid  # 기록 훅이 이 세션의 입력만 이 실행에 붙인다 (다른 세션 기록 섞임 방지)
        cur_p.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
        st_p.write_text(json.dumps({"run_id": cur["run_id"], "session": sid}, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- 안전 차단 (PreToolUse 훅)
# 섹션 세션이 SVN 명령이나 DB 접속을 실행하지 못하게 막는다. 종료 코드 2 = Claude Code 가 도구 실행을 거부.
GUARD_RULES = [
    (r"(^|[\s;&|(\"'/\\])svn(\.exe)?(\s|$|[\"'])", "SVN 명령"),
    (r"tortoiseproc", "SVN 명령"),
    (r"(^|[\s;&|(\"'/\\])(sqlcmd|sqlplus|osql|isql|bcp|mysql|psql)(\.exe)?(\s|$|[\"'])", "DB 클라이언트 실행"),
    (r"invoke-sqlcmd", "DB 접속"),
    (r"new-object\s+[\w.]*(sqlconnection|oracleconnection|odbcconnection|oledbconnection|mysqlconnection)", "DB 접속"),
    (r"\[\s*system\.data\.(sqlclient|odbc|oledb|oracleclient)", "DB 접속"),
    (r"add-type[^\n]*system\.data", "DB 접속"),
    (r"import\s+(pyodbc|cx_oracle|oracledb|pymssql|pymysql|psycopg2)", "DB 접속"),
    # 빌드 도구(MSBuild·vswhere·csc)는 허용하고, 그 밖의 bin 폴더 exe 실행만 막는다
    (r"[\\/]bin[\\/](?!(msbuild|vswhere|csc|vbc)\.exe)[^\s\"']*\.exe", "빌드된 MES 프로그램 실행 (DB 접속 가능)"),
    (r"start-process", "외부 프로그램 실행"),
]
# DB 쓰기·실행으로 보는 말 (하나라도 있으면 조회로 보지 않음)
DB_WRITE = (r"\b(insert|update|delete|merge|upsert|drop|alter|create|truncate|rename|grant|revoke|deny|exec|execute|call|begin\s+tran"
            r"|commit|rollback|into|bulk|executenonquery|executescalar\s*\(\s*\)\s*;?\s*$|sp_\w+|xp_\w+|dbms_\w+|lock\s+table|for\s+update)\b")


def guard_hook(payload=None):
    if payload is None:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace") or "{}")
    if payload.get("tool_name") not in ("Bash", "PowerShell"):
        return 0
    cmd = str((payload.get("tool_input") or {}).get("command") or "")
    low = cmd.lower()
    for pat, why in GUARD_RULES:
        if re.search(pat, low):
            # DB 는 조회(SELECT)만 허용: 명령에 조회문이 있고 쓰기·실행 문장이 하나도 없으면 통과
            if why.startswith("DB") and re.search(r"\bselect\b", low) and not re.search(DB_WRITE, low):
                continue
            sys.stderr.write(f"비교 테스트 규칙으로 차단됨: {why}. 이 테스트에서는 SVN 명령과 DB 변경을 할 수 없습니다 "
                             "(DB 는 SELECT 조회만 가능, INSERT·UPDATE·DELETE·EXEC·프로시저 호출 등은 불가).\n")
            try:
                cwd = Path(payload.get("cwd") or ".").resolve()
                sec = next((q for q in [cwd, *cwd.parents] if (q / ".mes-dir").is_file()), None)
                if sec:
                    import datetime as dt
                    log_p = Path((sec / ".mes-dir").read_text(encoding="utf-8").strip()) / "guard.log"
                    with open(log_p, "a", encoding="utf-8") as fh:
                        fh.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {sec.parent.name} 차단({why}): {cmd[:300]!r}\n")
            except Exception:
                pass
            return 2
    return 0


ROOT_MARK = ".mes-compare"   # 비교 폴더 표시. 내용 {"data_dir": "<기록 폴더>"}
DATA_NAME = "mes-compare-data"


def find_root(start):
    p = Path(start).resolve()
    return next((q for q in [p, *p.parents] if (q / ROOT_MARK).is_file()), None)


def root_data(root):
    dd = (_load(root / ROOT_MARK, {}) or {}).get("data_dir") or str(root / DATA_NAME)
    return Path(dd) if Path(dd).is_absolute() else (root / dd)


def root_sections(root):
    dd = root_data(root).resolve()
    return [c for c in sorted(root.iterdir()) if c.is_dir() and not c.name.startswith((".", "_")) and c.resolve() != dd]


def section_of(root, cwd):
    """비교 폴더 바로 아래 폴더 중 cwd 를 품은 것 (비교 폴더 자체·기록 폴더는 섹션 아님)."""
    try:
        parts = Path(cwd).resolve().relative_to(root).parts
    except ValueError:
        return None
    if not parts or parts[0].startswith((".", "_")):
        return None
    sec = root / parts[0]
    return sec if sec.is_dir() and sec.resolve() != root_data(root).resolve() else None


def has_section_hooks(sec, cwd):
    """예전 방식(aicompare setup 으로 섹션에 훅을 넣음)이면 그 훅이 기록하므로 전역 훅은 건너뛴다 (이중 기록 방지)."""
    for q in [Path(cwd).resolve(), *Path(cwd).resolve().parents]:
        f = q / ".claude" / "settings.local.json"
        try:
            if f.is_file() and "mes_compare" in f.read_text(encoding="utf-8-sig"):
                return True
        except OSError:
            pass
        if q == sec:
            break
    return False


def ensure_section(sec, data_dir):
    """처음 보는 섹션: 기록 폴더 연결(.mes-dir)과 구조 스냅샷 (훅은 섹션에 넣지 않음)."""
    md = sec / ".mes-dir"
    if md.is_file() and Path(md.read_text(encoding="utf-8").strip()).resolve() == Path(data_dir).resolve():
        return False
    subprocess.run([sys.executable, TOOL, "--dir", str(data_dir), "init", "--section", str(sec)],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
    return True


SERVE_REG = Path.home() / ".mes-compare-serve.json"   # 기록 폴더 → 대시보드 포트 (비교 폴더가 여럿이어도 섞이지 않게)


def dash_port(data_dir):
    """이 기록 폴더의 대시보드 포트 (켜져 있을 때만), 없으면 None."""
    port = (_load(SERVE_REG, {}) or {}).get(str(Path(data_dir).resolve()))
    return port if port and port_open(port) else None


def ensure_dash(data_dir):
    """이 기록 폴더의 대시보드가 꺼져 있으면 빈 포트(8765부터)로 뒤에서 켠다 (창 없이, 출력은 기록 폴더 serve.log)."""
    if dash_port(data_dir):
        return False
    port = next(p for p in range(PORT, PORT + 50) if not port_open(p))
    import os
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    log = open(Path(data_dir) / "serve.log", "ab")
    subprocess.Popen([sys.executable, TOOL, "--dir", str(data_dir), "serve", "--port", str(port)], stdout=log, stderr=log,
                     stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    reg = _load(SERVE_REG, {}) or {}
    reg[str(Path(data_dir).resolve())] = port
    SERVE_REG.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    return True


def global_hook():
    """플러그인 전역 훅 하나로 모든 이벤트를 받는다. 비교 폴더 밖이면 아무것도 하지 않고 끝난다."""
    raw = sys.stdin.buffer.read()
    payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
    cwd = Path(payload.get("cwd") or ".").resolve()
    root = find_root(cwd)
    if not root:
        return 0
    sec = section_of(root, cwd)
    if not sec or has_section_hooks(sec, cwd):
        return 0
    data_dir = root_data(root)
    data_dir.mkdir(parents=True, exist_ok=True)
    ensure_section(sec, data_dir)
    ev, rc = payload.get("hook_event_name", ""), 0
    if ev == "PreToolUse" and payload.get("tool_name") in ("Bash", "PowerShell"):
        rc = guard_hook(payload)
    if ev in ("UserPromptSubmit", "SessionEnd"):
        auto_hook(payload)
        if ev == "UserPromptSubmit":
            ensure_dash(data_dir)
    if ev in ("PreToolUse", "Stop", "SubagentStop"):
        subprocess.run([sys.executable, TOOL, "--dir", str(data_dir), "hook"], input=raw, capture_output=True)
    return rc


def default_root(cwd):
    """'비교 시작' 기본 위치: 지금 프로젝트(.git·.svn 가 있는 맨 위 폴더, 없으면 지금 폴더)의 상위 폴더."""
    p = Path(cwd).resolve()
    tops = [q for q in [p, *p.parents] if (q / ".git").exists() or (q / ".svn").is_dir()]
    return (tops[-1] if tops else p).parent


def do_root(rest):
    yes = "--yes" in rest
    rest = [a for a in rest if a != "--yes"]
    root = Path(rest[0]).resolve() if rest else default_root(Path.cwd())
    if not root.is_dir():
        sys.exit(f"폴더가 없습니다: {root}")
    mark = root / ROOT_MARK
    secs = root_sections(root)
    print(f"비교 폴더: {root}" + ("  (이미 표시됨)" if mark.is_file() else ""))
    print(f"기록 폴더: {root_data(root) if mark.is_file() else root / DATA_NAME}")
    print("섹션 (이 아래 폴더, 폴더 이름 = 구조 이름. 나중에 폴더를 더 만들면 자동으로 추가):")
    for c in secs:
        old = " · 예전 방식 훅이 있어 그 훅이 기록" if has_section_hooks(c, c) else ""
        print(f"  - {structure_name(c)}  ({c}){old}")
    if not secs:
        print("  (아직 없음 — 이 폴더 아래에 같은 코드를 폴더별로 받아 두세요)")
    if not yes:
        print("이 폴더를 비교 폴더로 표시하려면 --yes 를 붙여 다시 실행")
        return 0
    dd = root / DATA_NAME
    dd.mkdir(parents=True, exist_ok=True)
    if not mark.is_file():
        mark.write_text(json.dumps({"data_dir": DATA_NAME}, ensure_ascii=False), encoding="utf-8")
    for c in secs:
        if not has_section_hooks(c, c):
            ensure_section(c, dd)
    c = _conf()
    c["data_dir"] = str(dd)
    CONF.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
    ensure_dash(dd)
    print("\n준비 완료. 섹션 폴더에서 새 세션을 열고 작업을 시키면 자동으로 기록됩니다.")
    print(f"대시보드: http://localhost:{(_load(SERVE_REG, {}) or {}).get(str(dd.resolve()), PORT)}  (꺼지면 다음 작업 때 자동으로 다시 켜짐 · 열기: aicompare dash)")
    return 0


def _conf():
    return _load(CONF, {}) or {}


def _hook_cmd(kind):
    return f'"{sys.executable}" "{Path(__file__).resolve()}" {kind} --tag mes_compare'


def _settings(sec):
    p = sec / ".claude" / "settings.local.json"
    if not p.exists():
        return p, {}
    try:
        txt = p.read_text(encoding="utf-8-sig")
        return p, (json.loads(txt) if txt.strip() else {})
    except Exception as e:
        sys.exit(f"{p} 를 읽지 못해 중단 (파일은 그대로 둠): {e}")


def add_mc_hooks(sec, guard=True):
    """자동 시작·종료(UserPromptSubmit·SessionEnd)와 위험 명령 차단(PreToolUse Bash|PowerShell) 훅. 예전 mc.py 항목은 바꿔 끼운다."""
    p, cfg = _settings(sec)
    hooks = cfg.setdefault("hooks", {})

    def put(event, matcher, kind, on=True):
        lst = hooks.setdefault(event, [])
        lst[:] = [x for x in lst if "mc.py" not in json.dumps(x, ensure_ascii=False)]
        if on:
            e = {"hooks": [{"type": "command", "command": _hook_cmd(kind)}]}
            if matcher:
                e["matcher"] = matcher
            lst.insert(0, e)
        if not lst:
            hooks.pop(event)

    put("PreToolUse", "Bash|PowerShell", "guard-hook", guard)
    put("UserPromptSubmit", None, "auto-hook")
    put("SessionEnd", None, "auto-hook")
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.with_suffix(".json.bak").write_bytes(p.read_bytes())
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def check_section(sec, data_dir=None):
    """섹션 연결 상태 → (문제 목록, 구조 이름)."""
    probs = []
    md = sec / ".mes-dir"
    if not md.is_file():
        probs.append("기록 폴더 연결(.mes-dir) 없음")
    elif data_dir and Path(md.read_text(encoding="utf-8").strip()).resolve() != Path(data_dir).resolve():
        probs.append(f"다른 기록 폴더에 연결됨: {md.read_text(encoding='utf-8').strip()}")
    _, cfg = _settings(sec)
    hooks = cfg.get("hooks") or {}
    for ev, need, what in (("PreToolUse", "mes_compare.py", "파일·스킬 기록"), ("Stop", "mes_compare.py", "토큰 기록"),
                           ("UserPromptSubmit", "auto-hook", "자동 시작"), ("SessionEnd", "auto-hook", "자동 종료")):
        if need not in json.dumps(hooks.get(ev) or [], ensure_ascii=False):
            probs.append(f"{what} 훅 없음 ({ev})")
    me = json.dumps(str(Path(__file__).resolve()), ensure_ascii=False)[1:-1]
    if "mc.py" in json.dumps(hooks, ensure_ascii=False) and me not in json.dumps(hooks, ensure_ascii=False):
        probs.append("예전 위치의 mc.py 를 가리킴 → aicompare setup 다시 실행")
    return probs, structure_name(sec)


def port_open(port=PORT):
    import socket
    with socket.socket() as s:
        s.settimeout(.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def open_dash(data_dir, rest):
    import threading
    import webbrowser
    port = dash_port(data_dir)
    if port:
        url = f"http://localhost:{port}"
        print(f"대시보드가 이미 켜져 있습니다 → 브라우저로 엽니다: {url}")
        webbrowser.open(url)
        return 0
    url = f"http://localhost:{PORT}"
    if port_open() and not (_load(SERVE_REG, {}) or {}):
        print(f"대시보드가 이미 켜져 있습니다 → 브라우저로 엽니다: {url}")
        webbrowser.open(url)
        return 0
    port = next(p for p in range(PORT, PORT + 50) if not port_open(p))
    url = f"http://localhost:{port}"
    reg = _load(SERVE_REG, {}) or {}
    reg[str(Path(data_dir).resolve())] = port
    SERVE_REG.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"대시보드: {url}  (기록 폴더 {data_dir} · 끄려면 Ctrl+C)", flush=True)
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    return subprocess.call([sys.executable, TOOL, "--dir", str(data_dir), "serve", "--port", str(port)] + rest)


def do_setup(rest):
    import os
    secs, data_dir, guard, i = [], None, True, 0
    while i < len(rest):
        a = rest[i]
        if a == "--dir" and i + 1 < len(rest):
            data_dir, i = rest[i + 1], i + 2
            continue
        if a == "--no-guard":
            guard = False
        else:
            path, _, name = a.partition("=")
            secs.append((Path(path).resolve(), name.strip()))
        i += 1
    if not secs:
        sys.exit("사용법: aicompare setup 섹션폴더[=구조이름] ... [--dir 기록폴더] [--no-guard]")
    for sec, _ in secs:
        if not sec.is_dir():
            sys.exit(f"폴더가 없습니다: {sec}")
    if not data_dir:  # 예: 프로젝트_1/develop, 프로젝트_2/develop → aiTest/mes-compare-data
        common = Path(os.path.commonpath([str(s.parent) for s, _ in secs])) if len(secs) > 1 else secs[0][0].parent
        data_dir = common / "mes-compare-data"
    data_dir = Path(data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    print(f"기록 폴더: {data_dir}\n")
    names = []
    for sec, name in secs:
        if name:
            (sec / ".mes-structure").write_text(name, encoding="utf-8")
        r = subprocess.run([sys.executable, TOOL, "--dir", str(data_dir), "init", "--install-hooks", "--section", str(sec)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode:
            sys.exit(f"{sec}: 연결 실패\n{r.stdout}{r.stderr}")
        add_mc_hooks(sec, guard)  # init 이 PreToolUse 의 mes_compare 표시 항목을 지우므로 그 뒤에 넣는다
        probs, nm = check_section(sec, data_dir)
        names.append(nm)
        summary = next((ln.split("이 섹션 구조:", 1)[1].strip() for ln in r.stdout.splitlines() if "이 섹션 구조:" in ln), "")
        print(f"  {'✓' if not probs else '✗'} {nm:<8} {sec}\n      {summary}")
        for w in [ln for ln in r.stdout.splitlines() if ln.startswith(("경고", "참고"))] + probs:
            print(f"      {w}")
    if len(set(names)) < len(names):
        print("\n경고: 구조 이름이 겹칩니다 → 섹션폴더=이름 으로 서로 다르게 지정하세요")
    new = [str(s) for s, _ in secs]
    known = [s for s in _conf().get("sections", []) if Path(s).is_dir() and s not in new]
    CONF.write_text(json.dumps({"data_dir": str(data_dir), "sections": known + new}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n준비 완료. 각 섹션에서 새로 여는 Claude 세션부터 자동 기록됩니다 (작업을 그냥 시키면 됨).")
    print("대시보드: aicompare dash   ·   점검: aicompare check")
    return 0


def do_check():
    c = _conf()
    if not c.get("sections"):
        sys.exit("연결한 섹션이 없습니다. 먼저: aicompare setup 섹션폴더=이름 ...")
    print(f"기록 폴더: {c.get('data_dir')}")
    bad = 0
    for s in c["sections"]:
        sec = Path(s)
        if not sec.is_dir():
            print(f"  ✗ {s}  (폴더 없음)")
            bad += 1
            continue
        probs, nm = check_section(sec, c.get("data_dir"))
        bad += bool(probs)
        print(f"  {'✓' if not probs else '✗'} {nm:<8} {s}" + "".join(f"\n      {p}" for p in probs))
    lives = [(_load(p) or {}).get("run_id") for p in sorted(Path(c.get("data_dir") or ".").glob("current*.json"))]
    lives = [x for x in lives if x]
    print(f"진행 중인 실행: {', '.join(lives)}" if lives else "진행 중인 실행: 없음")
    print(f"대시보드: {'켜져 있음' if port_open() else '꺼져 있음 (aicompare dash)'}")
    print("문제 없음" if not bad else "→ 문제가 있는 섹션은 aicompare setup 을 다시 실행하세요")
    return 1 if bad else 0


def do_install():
    import os
    scripts = Path(sys.executable).parent / "Scripts"
    scripts.mkdir(exist_ok=True)
    cmd = scripts / "aicompare.cmd"
    cmd.write_text(f'@"{sys.executable}" "{Path(__file__).resolve()}" %*\r\n', encoding="utf-8")
    print(f"aicompare 명령 등록: {cmd}")
    on_path = str(scripts).lower().rstrip("\\") in [x.lower().rstrip("\\") for x in os.environ.get("PATH", "").split(os.pathsep)]
    print("이제 어디서든 aicompare 를 쓸 수 있습니다." if on_path else f"{scripts} 가 PATH 에 없습니다 → PATH 에 추가하세요.")
    return 0


def main():
    args = sys.argv[1:]
    for st in (sys.stdout, sys.stderr):   # Windows 기본(cp949)으로 쓰면 Claude Code 에 차단 사유가 깨져 전달된다
        try:
            st.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if args and args[0] == "install":
        return do_install()
    if args and args[0] == "setup":
        return do_setup(args[1:])
    if args and args[0] == "check":
        return do_check()
    if args and args[0] == "dash":
        sec = find_section(quiet=True)
        rt = find_root(Path.cwd())
        dd = ((sec / ".mes-dir").read_text(encoding="utf-8").strip() if sec else
              str(root_data(rt)) if rt else _conf().get("data_dir"))
        if not dd:
            sys.exit("기록 폴더를 모릅니다. 섹션 폴더 안에서 실행하거나 먼저 aicompare setup 을 하세요.")
        return open_dash(dd, args[1:])
    if args and args[0] == "root":
        return do_root(args[1:])
    if args and args[0] == "global-hook":
        try:
            return global_hook()
        except Exception:
            return 0   # 훅 오류가 작업을 막지 않게
    if args and args[0] == "guard-hook":
        try:
            return guard_hook()
        except Exception:
            return 0
    if args and args[0] == "auto-hook":
        try:
            auto_hook()
        except Exception:
            pass
        return 0  # 훅 오류가 작업을 막지 않게 항상 성공
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    sec = find_section()
    data_dir = (sec / ".mes-dir").read_text(encoding="utf-8").strip()
    cmd, rest = args[0], args[1:]
    base = [sys.executable, TOOL, "--dir", data_dir]
    name = structure_name(sec)

    if cmd in ("start", "new"):
        order, desc, ran = read_tasks(data_dir)
        i = 0  # 앞쪽의 작업 ID·설명(최대 2개) 뒤는 mes_compare.py 옵션 (--risk 등)
        while i < len(rest) and i < 2 and not rest[i].startswith("-"):
            i += 1
        pos, opts = rest[:i], rest[i:]
        task_id = task = None
        if cmd == "new":
            if not pos:
                sys.exit('사용법: aicompare new "작업 설명"')
            task_id, task = next_id(order), pos[0]
        elif not pos:
            todo = [t for t in reversed(order) if name not in ran.get(t, set())]
            if not todo:
                sys.exit(f'구조 {name} 가 안 한 작업이 없습니다. 새 작업은: aicompare start "작업 설명"')
            task_id, task = todo[0], desc[todo[0]]
            if len(todo) > 1:
                print(f"이 구조가 아직 안 한 작업: {', '.join(reversed(todo))} → 가장 최근 {task_id} 로 시작", flush=True)
        elif pos[0] in desc or (ID_RE.match(pos[0]) and len(pos) == 2):
            task_id = pos[0]
            task = pos[1] if len(pos) > 1 else desc.get(task_id)
            if not task:
                sys.exit(f'{task_id} 는 처음 쓰는 작업 ID 입니다. 설명을 함께 적으세요: aicompare start {task_id} "작업 설명"')
            if len(pos) > 1 and desc.get(task_id) and desc[task_id] != task:
                print(f"경고: {task_id} 의 기존 설명은 '{desc[task_id]}' 입니다. 구조끼리 같은 설명을 써야 비교가 정확합니다.", flush=True)
        else:
            # 작업 ID 가 아니면 설명으로 보고 새 작업을 만든다 (같은 설명의 작업이 있으면 그것을 쓴다)
            same = [t for t in order if desc.get(t) == pos[0]]
            task_id, task = (same[-1], pos[0]) if same else (next_id(order), pos[0])
        if name in ran.get(task_id, set()):
            print(f"참고: 구조 {name} 는 {task_id} 를 이미 실행했습니다 → 반복 실행으로 기록됩니다.", flush=True)
        extra = [] if "--structure" in opts else ["--structure", name]
        return subprocess.call(base + ["start", "--task-id", task_id, "--task", task] + extra + opts, cwd=str(sec))
    if cmd == "tasks":
        order, desc, ran = read_tasks(data_dir)
        if not order:
            print('작업이 없습니다. 새 작업: aicompare start "작업 설명"')
            return 0
        for t in order:
            done = ", ".join(sorted(ran.get(t, set()))) or "—"
            mark = "✓" if name in ran.get(t, set()) else " "
            print(f"{mark} {t}  {desc[t]}  (실행한 구조: {done})")
        print(f"✓ = 이 섹션 구조({name})가 실행함")
        return 0
    return subprocess.call(base + [cmd] + rest, cwd=str(sec))


if __name__ == "__main__":
    sys.exit(main())
