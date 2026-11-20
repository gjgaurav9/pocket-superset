#!/usr/bin/env python3
"""Pocket Superset bridge.

A small HTTP server on the laptop that lets a phone follow the Claude Code session running
in a Superset worktree, and send it the next prompt. It only ever calls three Superset CLI
commands (snapshot, terminals read, terminals send) with IDs it has checked against the
current snapshot, and reads Claude's own transcript files. It never runs a shell command
sent by the phone.

Run:    python3 server.py                 # http://127.0.0.1:8780
Pair:   open http://127.0.0.1:8780/local/pair on the laptop, type the code on the phone
Check:  python3 server.py --once          # print the cards as JSON and exit
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOME = os.path.expanduser("~")
PROJECTS_DIR = os.path.join(HOME, ".claude", "projects")
HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(HERE, "app")
DATA_DIR = os.environ.get("POCKET_DATA_DIR") or os.path.join(HOME, ".pocket-superset")
DEVICES_FILE = os.path.join(DATA_DIR, "devices.json")
AUDIT_LOG = os.path.join(DATA_DIR, "audit.log")
KEYCHAIN_SERVICE = "pocket-superset-paradev"
COOKIE = "ps_device"

TAIL_BYTES = 768 * 1024
MAX_TURNS = 80
MAX_TURN_CHARS = 6000
MAX_PROMPT_CHARS = 8000
SCREEN_LINES = 45
CARD_REFRESH_SECONDS = 3.0
WORKSPACE_REFRESH_SECONDS = 60.0
INPUT_TOOLS = {"AskUserQuestion": "asked you a question",
               "ExitPlanMode": "wants you to approve the plan"}
STALE_SECONDS = 3600
BG_AGENT_STALE_SECONDS = 3600

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# Keys the phone may press. Digits answer numbered prompts, esc stops Claude or dismisses a prompt.
ALLOWED_KEYS = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "esc", "enter", "up", "down", "shift+tab"}
STATE_ORDER = {"needs_input": 0, "finished": 1, "working": 2, "idle": 3, "stale": 4, "unknown": 5}

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
}


def log(msg):
    sys.stderr.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
    sys.stderr.flush()


def parse_time(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# --------------------------------------------------------------------------- Superset CLI

def find_superset():
    for c in (os.environ.get("SUPERSET_BIN"), shutil.which("superset"),
              os.path.join(HOME, ".paradev", "bin", "superset"),
              os.path.join(HOME, ".superset", "bin", "superset")):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def load_api_key():
    """A Paradev API key lets the CLI work under launchd, where the desktop login is not
    visible. Taken from SUPERSET_API_KEY, else from the login Keychain."""
    key = os.environ.get("SUPERSET_API_KEY")
    if key:
        return key
    try:
        p = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                           capture_output=True, text=True, timeout=5)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    except Exception:
        pass
    return None


class CliError(Exception):
    pass


class Cli:
    def __init__(self, binary, api_key=None):
        self.binary = binary
        self.env = dict(os.environ)
        if api_key:
            self.env["SUPERSET_API_KEY"] = api_key

    def run(self, args, timeout=25):
        if not self.binary:
            raise CliError("Superset CLI not found. Set SUPERSET_BIN or put superset on PATH.")
        try:
            p = subprocess.run([self.binary] + args + ["--json"], capture_output=True, text=True,
                               timeout=timeout, env=self.env)
        except subprocess.TimeoutExpired:
            raise CliError(f"superset {args[0]} {args[1] if len(args) > 1 else ''} timed out")
        if p.returncode != 0:
            msg = (p.stderr or p.stdout).strip().splitlines()
            raise CliError(msg[-1] if msg else f"superset exited {p.returncode}")
        out = p.stdout.strip()
        if not out:
            return None
        try:
            return json.loads(out)
        except ValueError:
            return out

    def snapshot(self):
        return self.run(["snapshot"]) or {"workspaces": []}

    def workspaces(self):
        return self.run(["workspaces", "list"]) or []

    def projects(self):
        return self.run(["projects", "list"]) or []

    def read_screen(self, ws, term, lines=SCREEN_LINES):
        data = self.run(["terminals", "read", "--local", "--workspace", ws, "--terminal", term,
                         "--max-lines", str(lines)], timeout=15)
        return data.get("text", "") if isinstance(data, dict) else (data or "")

    def send_text(self, ws, term, text):
        return self.run(["terminals", "send", "--local", "--workspace", ws, "--terminal", term,
                         "--text", text], timeout=20)

    def send_keys(self, ws, term, keys):
        return self.run(["terminals", "send", "--local", "--workspace", ws, "--terminal", term,
                         "--keys", keys], timeout=20)


# --------------------------------------------------------------------------- transcripts

def encode_cwd(cwd):
    """Claude Code names a project folder by replacing '/' and '.' in the cwd with '-'."""
    return re.sub(r"[/.]", "-", cwd)


def transcripts_for(cwd, limit=6):
    d = os.path.join(PROJECTS_DIR, encode_cwd(os.path.normpath(cwd)))
    out = []
    try:
        for name in os.listdir(d):
            if name.endswith(".jsonl"):
                fp = os.path.join(d, name)
                try:
                    out.append((os.path.getmtime(fp), fp))
                except OSError:
                    pass
    except OSError:
        return []
    out.sort(reverse=True)
    return [fp for _, fp in out[:limit]]


def read_tail_lines(path, nbytes=TAIL_BYTES):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > nbytes:
            f.seek(size - nbytes)
            data = f.read()
            data = data[data.find(b"\n") + 1:]
        else:
            data = f.read()
    return data.decode("utf-8", "replace").splitlines(), size


def load_records(lines):
    recs = []
    for line in lines:
        if not line.startswith("{"):
            continue
        try:
            recs.append(json.loads(line))
        except ValueError:
            continue
    return recs


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _is_tool_result(content):
    return isinstance(content, list) and bool(content) and all(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def _clip(s, n=MAX_TURN_CHARS):
    s = s.strip()
    return s if len(s) <= n else s[:n].rstrip() + "\n…"


def tool_summary(block):
    name = block.get("name") or "tool"
    inp = block.get("input") or {}
    detail = ""
    for k in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt"):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            detail = v.strip().splitlines()[0]
            break
    if name == "AskUserQuestion":
        qs = inp.get("questions") or []
        if qs and isinstance(qs[0], dict):
            detail = qs[0].get("question") or detail
    return f"{name}: {detail[:160]}" if detail else name


def records_to_turns(recs):
    """Turn transcript records into what the phone shows: your prompts, Claude's text, and a
    one-line note per tool call. Tool results, thinking and sidechains are left out."""
    turns = []
    for r in recs:
        t = r.get("type")
        if t not in ("user", "assistant") or r.get("isSidechain") or r.get("isMeta"):
            continue
        msg = r.get("message") or {}
        content = msg.get("content")
        ts = parse_time(r.get("timestamp"))
        rid = r.get("uuid") or ""
        if t == "user":
            if _is_tool_result(content):
                continue
            text = _text_of(content).strip()
            if not text:
                continue
            if "[Request interrupted by user" in text:
                turns.append({"id": rid, "role": "event", "text": "You stopped Claude", "ts": ts})
                continue
            m = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
            if m:
                args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
                cmd = m.group(1).strip()
                if args and args.group(1).strip():
                    cmd += " " + args.group(1).strip()
                turns.append({"id": rid, "role": "you", "text": cmd, "ts": ts})
                continue
            if text.startswith(("<local-command", "<command-message", "<system-reminder", "<bash-")):
                continue
            turns.append({"id": rid, "role": "you", "text": _clip(text), "ts": ts})
        else:
            blocks = content if isinstance(content, list) else []
            for i, b in enumerate(blocks):
                if not isinstance(b, dict):
                    continue
                bid = f"{rid}:{i}"
                if b.get("type") == "text" and (b.get("text") or "").strip():
                    turns.append({"id": bid, "role": "claude", "text": _clip(b["text"]), "ts": ts})
                elif b.get("type") == "tool_use":
                    turns.append({"id": bid, "role": "tool", "text": tool_summary(b), "ts": ts})
    return turns


def classify(recs, mtime, now):
    """State of a session from its last user/assistant record (same rules as worktree-watch)."""
    title = None
    for r in reversed(recs):
        if r.get("type") == "ai-title":
            title = r.get("aiTitle")
            break
    state, label, last_reply, last_prompt = "unknown", "", None, None
    found = False
    for r in reversed(recs):
        t = r.get("type")
        if t not in ("user", "assistant") or r.get("isSidechain") or r.get("isMeta"):
            continue
        msg = r.get("message") or {}
        content = msg.get("content")
        if t == "assistant" and last_reply is None:
            txt = _text_of(content).strip()
            if txt:
                last_reply = txt
        if t == "user" and last_prompt is None and not _is_tool_result(content):
            txt = _text_of(content).strip()
            if txt and not txt.startswith("<"):
                last_prompt = txt
        if found:
            if last_reply is not None and last_prompt is not None:
                break
            continue
        found = True
        if t == "user":
            text = _text_of(content).lstrip()
            if _is_tool_result(content):
                state, label = "working", "running tools"
            elif "[Request interrupted by user" in text:
                state, label = "idle", "you stopped it"
            elif text.startswith("<"):
                state, label = "idle", "waiting for you"
            else:
                state, label = "working", "thinking"
        else:
            blocks = content if isinstance(content, list) else []
            tools = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
            stop = msg.get("stop_reason")
            if tools:
                name = tools[-1].get("name") or "a tool"
                if name in INPUT_TOOLS:
                    state, label = "needs_input", INPUT_TOOLS[name]
                else:
                    state, label = "working", f"running {name}"
            elif stop in (None, "tool_use"):
                state, label = "working", "writing"
            else:
                state, label = "finished", "finished its turn"
    if state == "working" and now - mtime > STALE_SECONDS:
        state, label = "stale", "no activity for over an hour"
    return {"title": title, "state": state, "label": label,
            "last_reply": _clip(last_reply or "", 400) or None,
            "last_prompt": _clip(last_prompt or "", 200) or None}


def background_agents(transcript_path, now):
    session_id = os.path.basename(transcript_path)[:-len(".jsonl")]
    d = os.path.join(os.path.dirname(transcript_path), session_id, "subagents")
    n = 0
    try:
        names = os.listdir(d)
    except OSError:
        return 0
    for name in names:
        if not name.endswith(".meta.json"):
            continue
        mp = os.path.join(d, name)
        try:
            with open(mp) as f:
                if json.load(f).get("finished"):
                    continue
            if now - os.path.getmtime(mp) <= BG_AGENT_STALE_SECONDS:
                n += 1
        except Exception:
            continue
    return n


# --------------------------------------------------------------------------- screen

RULE_RE = re.compile(r"^\s*[─━]{12,}\s*$")
CHOICE_RE = re.compile(r"^\s*[│|]?\s*(❯)?\s*(\d)[.)]\s+(.+?)\s*[│|]?\s*$")


def parse_screen(text):
    """Read Claude Code's screen: the input box (between the last two horizontal rules), the
    mode footer under it, and an open numbered prompt (permission or question) if one shows."""
    lines = text.split("\n")
    rules = [i for i, l in enumerate(lines) if RULE_RE.match(l)]
    input_text, footer = None, ""
    if len(rules) >= 2 and rules[-1] - rules[-2] <= 12:
        box = [l.strip() for l in lines[rules[-2] + 1:rules[-1]]]
        if box:
            box[0] = re.sub(r"^[❯>]\s?", "", box[0])
        input_text = "\n".join(box).strip()
        footer = " ".join(l.strip() for l in lines[rules[-1] + 1:] if l.strip())
    choices, question = [], None
    window_start = max(0, len(lines) - 30)
    run = [(i, m) for i in range(window_start, len(lines)) for m in [CHOICE_RE.match(lines[i])] if m]
    # keep the last consecutive 1..n sequence that has a ❯ cursor on one of its options
    seqs, cur = [], []
    for i, m in run:
        n = int(m.group(2))
        if n == 1:
            if cur:
                seqs.append(cur)
            cur = [(i, m)]
        elif cur and n == int(cur[-1][1].group(2)) + 1:
            cur.append((i, m))
    if cur:
        seqs.append(cur)
    first_line = None
    for seq in reversed(seqs):
        if len(seq) >= 2 and any(m.group(1) for _, m in seq):
            choices = [{"key": m.group(2), "label": m.group(3).strip(), "selected": bool(m.group(1))}
                       for _, m in seq]
            first_line = seq[0][0]
            break
    menu = False
    if not choices:
        choices, first_line = _unnumbered_menu(lines, rules if input_text is not None else [])
        menu = bool(choices)
    if choices:
        for j in range(first_line - 1, max(-1, first_line - 12), -1):
            s = lines[j].strip().strip("│|").strip()
            if s and "?" in s:
                question = s
                break
    return {"text": text.rstrip(), "input": input_text, "footer": footer,
            "choices": choices, "menu": menu, "question": question}


MENU_HINT_RE = re.compile(r"(Enter to (confirm|select)|Esc to (cancel|exit|go back))", re.I)


def _unnumbered_menu(lines, rules):
    """A menu without numbers (folder trust, model picker): one option carries the ❯ cursor,
    its neighbours are the other options, and a hint such as "Enter to confirm" sits below."""
    box = range(rules[-2], rules[-1] + 1) if len(rules) >= 2 else range(0)
    start = max(0, len(lines) - 30)
    for i in range(len(lines) - 1, start - 1, -1):
        raw = lines[i].strip().strip("│|").rstrip()
        if not raw.lstrip().startswith("❯") or i in box:
            continue
        if not any(MENU_HINT_RE.search(l) for l in lines[i + 1:i + 14]):
            continue
        lo = i
        while lo - 1 >= start and lines[lo - 1].strip().strip("│|").strip() and (lo - 1) not in box \
                and not RULE_RE.match(lines[lo - 1]) and len(lines[lo - 1].strip()) < 100:
            lo -= 1
        hi = i
        while hi + 1 < len(lines) and lines[hi + 1].strip().strip("│|").strip() and (hi + 1) not in box \
                and not MENU_HINT_RE.search(lines[hi + 1]) and len(lines[hi + 1].strip()) < 100:
            hi += 1
        # options share the indentation of the cursor line; a question line above usually does not
        opts = []
        for j in range(lo, hi + 1):
            s = lines[j].strip().strip("│|").strip()
            sel = s.startswith("❯")
            label = s.lstrip("❯").strip()
            if label:
                opts.append({"index": len(opts), "label": label, "selected": sel, "_line": j})
        indent = len(lines[i]) - len(lines[i].lstrip())
        opts = [o for o in opts if o["selected"] or abs((len(lines[o["_line"]]) - len(lines[o["_line"]].lstrip())) - indent) <= 2]
        if 2 <= len(opts) <= 9:
            first = opts[0]["_line"]
            for n, o in enumerate(opts):
                o["index"] = n
                del o["_line"]
            return opts, first
        return [], None
    return [], None


# --------------------------------------------------------------------------- cards

class Board:
    """The cards the phone lists: one per live Claude terminal in a Superset workspace."""

    def __init__(self, cli):
        self.cli = cli
        self.lock = threading.Lock()
        self.cards = {}            # (ws, term) -> card (with private _transcript)
        self.error = None
        self.updated_at = 0
        self.paths = {}            # ws id -> {path, name, project, branch}
        self.paths_at = 0
        self.parse_cache = {}      # transcript -> (mtime, size, classify result, title)

    def _refresh_paths(self):
        if time.time() - self.paths_at < WORKSPACE_REFRESH_SECONDS and self.paths:
            return
        projects = {}
        try:
            for p in self.cli.projects():
                projects[p.get("id")] = p
        except CliError as e:
            log(f"projects list: {e}")
        paths = {}
        for w in self.cli.workspaces():
            if w.get("archivedAt"):
                continue
            path = w.get("worktreePath")
            if not path and w.get("type") == "local":
                proj = projects.get(w.get("projectId")) or {}
                path = proj.get("repoPath") or proj.get("path") or proj.get("mainRepoPath")
            paths[w["id"]] = {"path": path, "name": w.get("name"), "project": w.get("projectName"),
                              "branch": w.get("branch")}
        self.paths = paths
        self.paths_at = time.time()

    def _session(self, fp, now):
        try:
            st = os.stat(fp)
        except OSError:
            return None
        cached = self.parse_cache.get(fp)
        if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
            info = cached[2]
        else:
            lines, _ = read_tail_lines(fp)
            info = classify(load_records(lines), st.st_mtime, now)
            self.parse_cache[fp] = (st.st_mtime, st.st_size, info)
        info = dict(info)
        info["mtime"] = st.st_mtime
        if info["state"] == "finished":
            bg = background_agents(fp, now)
            if bg:
                info["state"] = "working"
                info["label"] = f"{bg} background agent{'s' if bg > 1 else ''} running"
        return info

    def refresh(self):
        now = time.time()
        try:
            self._refresh_paths()
            snap = self.cli.snapshot()
        except CliError as e:
            with self.lock:
                self.error = str(e)
            return
        cards = {}
        for w in snap.get("workspaces", []):
            terms = [t for t in (w.get("terminals") or [])
                     if ((t.get("agent") or {}).get("id") or "").startswith("claude")]
            if not terms:
                continue
            meta = self.paths.get(w["id"]) or {}
            cwd = meta.get("path")
            candidates = transcripts_for(cwd) if cwd else []
            sessions = [(fp, self._session(fp, now)) for fp in candidates]
            sessions = [(fp, s) for fp, s in sessions if s]
            # Claude sets the terminal title to the session title, so match on titles across
            # every terminal first; only then hand the newest leftover transcripts to terminals
            # without a title. Otherwise a dead terminal could take a live session's transcript.
            used, picks = set(), {}
            titles = {t["id"]: re.sub(r"^[^\w]+", "", t.get("title") or "").strip() for t in terms}
            for t in terms:
                for fp, s in sessions:
                    if titles[t["id"]] and fp not in used and (s.get("title") or "").strip() == titles[t["id"]]:
                        picks[t["id"]] = (fp, s)
                        used.add(fp)
                        break
            for t in terms:
                if t["id"] in picks:
                    continue
                for fp, s in sessions:
                    if fp not in used:
                        picks[t["id"]] = (fp, s)
                        used.add(fp)
                        break
            for t in terms:
                ttitle = titles[t["id"]]
                fp, s = picks.get(t["id"], (None, None))
                card = {
                    "ws": w["id"], "term": t["id"],
                    "workspace": meta.get("name") or w.get("name"),
                    "project": meta.get("project") or w.get("project") or "other",
                    "branch": meta.get("branch") or w.get("branch"),
                    "title": (s or {}).get("title") or ttitle or meta.get("name") or "Claude",
                    "state": (s or {}).get("state", "unknown"),
                    "label": (s or {}).get("label", "no transcript found yet"),
                    "last_reply": (s or {}).get("last_reply"),
                    "last_prompt": (s or {}).get("last_prompt"),
                    "updated_at": (s or {}).get("mtime"),
                    "_transcript": fp,
                }
                cards[(w["id"], t["id"])] = card
        with self.lock:
            self.cards = cards
            self.error = None
            self.updated_at = now

    def run_forever(self):
        while True:
            t0 = time.time()
            try:
                self.refresh()
            except Exception as e:
                log(f"refresh error: {e}")
            time.sleep(max(1.0, CARD_REFRESH_SECONDS - (time.time() - t0)))

    def public(self):
        with self.lock:
            cards = [{k: v for k, v in c.items() if not k.startswith("_")} for c in self.cards.values()]
            err, at = self.error, self.updated_at
        cards.sort(key=lambda c: (STATE_ORDER.get(c["state"], 9), -(c["updated_at"] or 0)))
        return {"cards": cards, "error": err, "updated_at": at}

    def get(self, ws, term):
        if not (isinstance(ws, str) and isinstance(term, str) and UUID_RE.match(ws) and UUID_RE.match(term)):
            return None
        with self.lock:
            c = self.cards.get((ws, term))
            return dict(c) if c else None


# --------------------------------------------------------------------------- devices

def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Devices:
    """Paired phones. Only a hash of each device secret is stored, so the file is not a key."""

    def __init__(self, path=DEVICES_FILE):
        self.path = path
        self.lock = threading.Lock()
        self.pair = None          # {"code", "expires", "tries"}
        try:
            with open(path) as f:
                self.data = json.load(f)
        except Exception:
            self.data = {}

    def _flush(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def check(self, token):
        if not token:
            return None
        h = _hash(token)
        with self.lock:
            for known, info in self.data.items():
                if hmac.compare_digest(known, h):
                    return info
        return None

    def new_code(self):
        with self.lock:
            self.pair = {"code": f"{secrets.randbelow(10**6):06d}", "expires": time.time() + 300, "tries": 0}
            return self.pair["code"]

    def redeem(self, code, name):
        with self.lock:
            p = self.pair
            if not p or time.time() > p["expires"]:
                return None, "No pairing code is active. Open /local/pair on the laptop for a new one."
            p["tries"] += 1
            if p["tries"] > 5:
                self.pair = None
                return None, "Too many wrong codes. Get a new code on the laptop."
            if not hmac.compare_digest(p["code"], (code or "").strip()):
                return None, "That code is wrong."
            self.pair = None
            token = secrets.token_urlsafe(32)
            self.data[_hash(token)] = {"name": (name or "phone")[:60], "paired_at": time.time()}
            self._flush()
            return token, None

    def list(self):
        with self.lock:
            return [dict(v) for v in self.data.values()]

    def revoke_all(self):
        with self.lock:
            self.data = {}
            self._flush()


def audit(entry):
    entry = dict(entry, at=datetime.now().isoformat(timespec="seconds"))
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        log(f"audit write failed: {e}")


# --------------------------------------------------------------------------- HTTP

PAIR_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pair · Pocket Superset</title><link rel="stylesheet" href="/app.css">
<main class="pair"><h1>Pair this phone</h1>
<p>On the laptop, open <b>http://127.0.0.1:{port}/local/pair</b> and type the 6-digit code here.</p>
<form method="post" action="/pair"><label for="code">Code</label>
<input id="code" name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" pattern="[0-9]{{6}}" required autofocus>
<label for="name">Name this device</label><input id="name" name="name" value="My phone" maxlength="60">
<button type="submit">Pair</button></form><p class="err">{error}</p></main>"""

LOCAL_PAIR_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pairing code · Pocket Superset</title><link rel="stylesheet" href="/app.css">
<main class="pair"><h1>Pairing code</h1><p class="code">{code}</p>
<p>Open Pocket Superset on the phone and type this code. It works once and expires in 5 minutes.</p>
<p>Paired devices: {devices}</p></main>"""


class Handler(BaseHTTPRequestHandler):
    board = None
    devices = None
    cli = None
    port = 8780
    allow_login = None
    phone_prompts = {}      # (ws, term) -> [recent prompt texts sent from the phone]
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---- helpers
    def _headers(self, code, ctype, length=None, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                         "connect-src 'self'; frame-ancestors 'none'; form-action 'self'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if not isinstance(body, (bytes, str)):
            body = json.dumps(body)
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self._headers(code, ctype, len(data), extra)
        self.wfile.write(data)

    def _ts_login(self):
        return self.headers.get("Tailscale-User-Login")

    def _is_laptop(self):
        """A request made on the laptop itself: loopback and not forwarded by Tailscale Serve."""
        return (self.client_address[0] in ("127.0.0.1", "::1") and not self._ts_login()
                and not self.headers.get("X-Forwarded-For"))

    def _tailnet_ok(self):
        login = self._ts_login()
        if self.allow_login and login and login != self.allow_login:
            return False
        if self.allow_login and not login and not self._is_laptop():
            return False
        return True

    def _cookie(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return None

    def _device(self):
        return self.devices.check(self._cookie())

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 * 1024:
            return None
        return self.rfile.read(n) if n else b""

    def _same_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host") or ""
        return urlparse(origin).netloc == host

    # ---- GET
    def do_GET(self):
        if not self._tailnet_ok():
            return self._send(403, {"error": "This Tailscale user is not allowed."})
        u = urlparse(self.path)
        path = u.path
        if path == "/health":
            return self._send(200, {"ok": True})
        if path in STATIC:
            name, ctype = STATIC[path]
            try:
                with open(os.path.join(APP_DIR, name), "rb") as f:
                    return self._send(200, f.read(), ctype)
            except FileNotFoundError:
                return self._send(404, "missing", "text/plain")
        if path == "/pair":
            return self._send(200, PAIR_PAGE.format(port=self.port, error=""), "text/html; charset=utf-8")
        if path == "/local/pair":
            if not self._is_laptop():
                return self._send(403, "Open this page on the laptop itself.", "text/plain")
            code = self.devices.new_code()
            n = len(self.devices.list())
            return self._send(200, LOCAL_PAIR_PAGE.format(code=code, devices=n), "text/html; charset=utf-8")
        if not path.startswith("/api/"):
            return self._send(404, {"error": "not found"})
        if not self._device():
            return self._send(401, {"error": "This device is not paired.", "pair": "/pair"})
        if path == "/api/cards":
            return self._send(200, self.board.public())
        if path == "/api/stream":
            q = parse_qs(u.query)
            return self._stream((q.get("ws") or [""])[0], (q.get("term") or [""])[0])
        return self._send(404, {"error": "not found"})

    # ---- POST
    def do_POST(self):
        if not self._tailnet_ok():
            return self._send(403, {"error": "This Tailscale user is not allowed."})
        path = urlparse(self.path).path
        raw = self._body()
        if raw is None:
            return self._send(413, {"error": "Request too large."})
        if not self._same_origin():
            return self._send(403, {"error": "Cross-site request refused."})
        if path == "/pair":
            form = parse_qs(raw.decode("utf-8", "replace"))
            token, err = self.devices.redeem((form.get("code") or [""])[0], (form.get("name") or [""])[0])
            if err:
                return self._send(400, PAIR_PAGE.format(port=self.port, error=err), "text/html; charset=utf-8")
            secure = "; Secure" if (self._ts_login() or self.headers.get("X-Forwarded-Proto") == "https") else ""
            cookie = f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000{secure}"
            audit({"kind": "pair", "device": (form.get("name") or [""])[0], "tailscale": self._ts_login()})
            return self._send(303, b"", "text/plain", {"Set-Cookie": cookie, "Location": "/"})
        device = self._device()
        if not device:
            return self._send(401, {"error": "This device is not paired.", "pair": "/pair"})
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self._send(415, {"error": "Send JSON."})
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._send(400, {"error": "Bad JSON."})
        if path == "/api/prompt":
            return self._prompt(body, device)
        if path == "/api/key":
            return self._key(body, device)
        return self._send(404, {"error": "not found"})

    # ---- actions
    def _card_or_404(self, body):
        card = self.board.get(body.get("ws"), body.get("term"))
        if not card:
            self._send(404, {"error": "That Claude session is not running any more."})
        return card

    def _prompt(self, body, device):
        card = self._card_or_404(body)
        if not card:
            return
        text = body.get("text")
        mode = body.get("mode") or "safe"
        if not isinstance(text, str) or not text.strip():
            return self._send(400, {"error": "Type a prompt first."})
        if len(text) > MAX_PROMPT_CHARS:
            return self._send(400, {"error": f"Keep a prompt under {MAX_PROMPT_CHARS} characters."})
        text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text).replace("\r\n", "\n").strip()
        if mode not in ("safe", "anyway", "clear"):
            return self._send(400, {"error": "Unknown send mode."})
        ws, term = card["ws"], card["term"]
        try:
            screen = parse_screen(self.cli.read_screen(ws, term))
        except CliError as e:
            return self._send(502, {"error": f"Could not read the terminal: {e}"})
        if screen["choices"]:
            return self._send(409, {"reason": "choice_open", "screen": screen,
                                    "error": "Claude is showing a prompt with options. Answer it first."})
        if screen["input"] is None:
            # Without Claude's input box on screen the text could land in a menu or, if Claude
            # has exited, in the shell, where it would run as a command. Never send then.
            return self._send(409, {"reason": "no_input_box", "screen": screen,
                                    "error": "Claude's input box is not on the laptop screen. "
                                             "Claude may have exited or be showing a menu."})
        if mode == "safe":
            if screen["input"]:
                return self._send(409, {"reason": "input_not_empty", "input": screen["input"],
                                        "error": "The laptop's input box is not empty."})
            if card["state"] == "working":
                return self._send(409, {"reason": "busy",
                                        "error": "Claude is still working. Your prompt would be queued."})
        try:
            if mode == "clear" and screen["input"]:
                self.cli.send_keys(ws, term, "ctrl+u")
            self.cli.send_text(ws, term, text)
        except CliError as e:
            audit({"kind": "prompt", "ws": ws, "term": term, "device": device.get("name"),
                   "text": text, "ok": False, "error": str(e)})
            return self._send(502, {"error": f"Sending failed: {e}"})
        audit({"kind": "prompt", "ws": ws, "term": term, "device": device.get("name"),
               "mode": mode, "text": text, "ok": True})
        recent = self.phone_prompts.setdefault((ws, term), [])
        recent.append(text)
        del recent[:-20]
        return self._send(200, {"ok": True})

    def _key(self, body, device):
        card = self._card_or_404(body)
        if not card:
            return
        if "choose" in body:
            return self._choose(card, body.get("choose"), body.get("label"), device)
        key = body.get("key")
        if key not in ALLOWED_KEYS:
            return self._send(400, {"error": "That key is not allowed."})
        try:
            self.cli.send_keys(card["ws"], card["term"], key)
        except CliError as e:
            return self._send(502, {"error": f"Sending failed: {e}"})
        audit({"kind": "key", "ws": card["ws"], "term": card["term"], "device": device.get("name"), "key": key})
        return self._send(200, {"ok": True})

    def _choose(self, card, index, label, device):
        """Pick an option in an unnumbered menu: move the cursor with arrow keys, then Enter.
        The screen is read again first, so a stale tap cannot land on a different menu."""
        if not isinstance(index, int) or isinstance(index, bool):
            return self._send(400, {"error": "Pick an option by its position."})
        try:
            screen = parse_screen(self.cli.read_screen(card["ws"], card["term"]))
        except CliError as e:
            return self._send(502, {"error": f"Could not read the terminal: {e}"})
        opts = screen["choices"] if screen.get("menu") else []
        if not (0 <= index < len(opts)) or (label is not None and opts[index]["label"] != label):
            return self._send(409, {"reason": "menu_changed", "screen": screen,
                                    "error": "The menu on the laptop changed. Check it again."})
        cur = next((o["index"] for o in opts if o["selected"]), 0)
        step = "down" if index > cur else "up"
        keys = [step] * abs(index - cur) + ["enter"]
        try:
            self.cli.send_keys(card["ws"], card["term"], ",".join(keys))
        except CliError as e:
            return self._send(502, {"error": f"Sending failed: {e}"})
        audit({"kind": "choose", "ws": card["ws"], "term": card["term"], "device": device.get("name"),
               "option": opts[index]["label"]})
        return self._send(200, {"ok": True})

    # ---- live stream
    def _event(self, name, data):
        payload = f"event: {name}\ndata: {json.dumps(data)}\n\n".encode("utf-8")
        self.wfile.write(payload)
        self.wfile.flush()

    def _stream(self, ws, term):
        card = self.board.get(ws, term)
        if not card:
            return self._send(404, {"error": "That Claude session is not running any more."})
        self.close_connection = True
        self._headers(200, "text/event-stream; charset=utf-8", extra={"X-Accel-Buffering": "no",
                                                                       "Connection": "close"})
        fp, offset, partial = None, 0, b""
        last_screen, last_screen_at, last_ping = None, 0.0, time.time()
        last_state = None
        try:
            while True:
                now = time.time()
                card = self.board.get(ws, term)
                if not card:
                    self._event("gone", {"error": "The Claude session closed."})
                    return
                pub = {k: v for k, v in card.items() if not k.startswith("_")}
                if (pub["state"], pub["label"]) != last_state:
                    last_state = (pub["state"], pub["label"])
                    self._event("card", pub)
                # transcript: full history on (re)attach, then only what was appended
                tfp = card.get("_transcript")
                if tfp != fp:
                    fp, partial = tfp, b""
                    if fp and os.path.exists(fp):
                        lines, offset = read_tail_lines(fp)
                        turns = records_to_turns(load_records(lines))[-MAX_TURNS:]
                        self._mark_phone(ws, term, turns)
                        self._event("reset", {"turns": turns})
                    else:
                        offset = 0
                        self._event("reset", {"turns": []})
                elif fp:
                    try:
                        size = os.path.getsize(fp)
                    except OSError:
                        size = offset
                    if size < offset:
                        fp = None      # rewritten; reload on the next tick
                        continue
                    if size > offset:
                        with open(fp, "rb") as f:
                            f.seek(offset)
                            chunk = partial + f.read(size - offset)
                        offset = size
                        cut = chunk.rfind(b"\n")
                        if cut < 0:
                            partial = chunk
                        else:
                            partial = chunk[cut + 1:]
                            text = chunk[:cut].decode("utf-8", "replace")
                            turns = records_to_turns(load_records(text.splitlines()))
                            if turns:
                                self._mark_phone(ws, term, turns)
                                self._event("turns", {"turns": turns})
                # live screen: often while Claude works or waits, slowly when idle
                gap = 1.5 if pub["state"] in ("working", "needs_input") else 5.0
                if now - last_screen_at >= gap:
                    last_screen_at = now
                    try:
                        screen = parse_screen(self.cli.read_screen(ws, term))
                    except CliError as e:
                        screen = {"error": str(e)}
                    if screen != last_screen:
                        last_screen = screen
                        self._event("screen", screen)
                if now - last_ping > 15:
                    last_ping = now
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                time.sleep(0.8)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _mark_phone(self, ws, term, turns):
        recent = self.phone_prompts.get((ws, term)) or []
        for t in turns:
            if t["role"] == "you" and t["text"].strip() in recent:
                t["from_phone"] = True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=int(os.environ.get("POCKET_PORT", "8780")))
    ap.add_argument("--allow-login", default=os.environ.get("POCKET_ALLOW_LOGIN"),
                    help="only this Tailscale login may connect over the tailnet")
    ap.add_argument("--once", action="store_true", help="print the cards as JSON and exit")
    ap.add_argument("--devices", action="store_true", help="list paired devices and exit")
    ap.add_argument("--revoke-all", action="store_true", help="unpair every device and exit")
    args = ap.parse_args()

    devices = Devices()
    if args.devices:
        print(json.dumps(devices.list(), indent=1))
        return
    if args.revoke_all:
        devices.revoke_all()
        print("Every device is unpaired. Pair again from /local/pair.")
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    cli = Cli(find_superset(), load_api_key())
    board = Board(cli)
    board.refresh()
    if args.once:
        print(json.dumps(board.public(), indent=1))
        return
    threading.Thread(target=board.run_forever, daemon=True).start()
    Handler.board, Handler.devices, Handler.cli = board, devices, cli
    Handler.port, Handler.allow_login = args.port, args.allow_login
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    log(f"Pocket Superset on http://127.0.0.1:{args.port} · superset: {cli.binary or 'NOT FOUND'} · "
        f"{len(board.public()['cards'])} Claude sessions · {board.error or 'ok'}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
