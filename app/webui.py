"""Local setup page: pick the CLI, model, effort and mode, and sign Cursor and Claude Code in.

Served by the bot on 127.0.0.1 only. Reach it from another machine through an SSH tunnel.
Every request needs the key from ~/.config/cursor-telegram/ui-key (see scripts/ui.sh).
"""

from __future__ import annotations

from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import hmac
import json
import os
import re
import secrets
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlparse

from app.access import CLAUDE_MODELS, EFFORT_LEVELS, find_claude, normalize_model
from app.constants import log

DEFAULT_PORT = 8787
LOGIN_SECONDS = 600
OUTPUT_LIMIT = 4000
STATUS_TTL = 5.0
TOOLS = ("cursor", "claude")
MODES = ("agent", "ask", "plan")
URL = re.compile(r"https?://[^\s\"'<>]+")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def load_key(path: Path) -> str:
    if path.is_file():
        key = path.read_text().strip()
        if key:
            return key
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    key = secrets.token_urlsafe(24)
    path.write_text(key + "\n")
    os.chmod(path, 0o600)
    return key


class Login:
    """One vendor sign-in process. The vendor CLI prints a link; the page shows it."""

    def __init__(self, tool: str, argv: list[str], env: dict[str, str]) -> None:
        self.tool = tool
        self.started = time.time()
        self.output = ""
        self.exit_code: int | None = None
        self.lock = threading.Lock()
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._expire, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        while True:
            chunk = os.read(self.proc.stdout.fileno(), 4096)
            if not chunk:
                break
            text = ANSI.sub("", chunk.decode("utf-8", "replace")).replace("\r", "\n")
            with self.lock:
                self.output = (self.output + text)[-OUTPUT_LIMIT:]
        self.exit_code = self.proc.wait()

    def _expire(self) -> None:
        deadline = self.started + LOGIN_SECONDS
        while self.proc.poll() is None and time.time() < deadline:
            time.sleep(1)
        self.cancel()

    def send(self, line: str) -> None:
        if self.proc.poll() is None and self.proc.stdin is not None:
            self.proc.stdin.write((line.strip() + "\n").encode())
            self.proc.stdin.flush()

    def cancel(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()

    def snapshot(self) -> dict:
        with self.lock:
            output = self.output
        match = URL.search(output)
        running = self.proc.poll() is None
        return {
            "running": running,
            "ok": None if running else self.exit_code == 0,
            "url": match.group(0) if match else "",
            "output": output.strip(),
        }


class SetupPage:
    def __init__(self, bridge, port: int | None = None, key_path: Path | None = None) -> None:
        self.bridge = bridge
        self.port = port or int(os.environ.get("CURSOR_TELEGRAM_UI_PORT", DEFAULT_PORT))
        self.key_path = key_path or Path.home() / ".config" / "cursor-telegram" / "ui-key"
        self.key = load_key(self.key_path)
        extra = {h.strip() for h in os.environ.get("CURSOR_TELEGRAM_UI_HOSTS", "").split(",") if h.strip()}
        self.hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"} | extra
        self.logins: dict[str, Login] = {}
        self.login_lock = threading.Lock()
        self._status: dict[str, tuple[float, dict]] = {}
        self.server: ThreadingHTTPServer | None = None

    # ---- state shown on the page ----

    def tool_status(self, tool: str, fresh: bool = False) -> dict:
        cached = self._status.get(tool)
        if cached and not fresh and time.time() - cached[0] < STATUS_TTL:
            return cached[1]
        status = self._probe(tool)
        self._status[tool] = (time.time(), status)
        return status

    def _probe(self, tool: str) -> dict:
        try:
            if tool == "claude":
                argv = [self._claude_binary(), "auth", "status", "--json"]
            else:
                argv = [self.bridge.agent, "status", "--format", "json"]
            done = subprocess.run(argv, capture_output=True, text=True, timeout=20, env=self.bridge.agent_env())
        except Exception as exc:  # missing binary, timeout
            return {"installed": False, "signed_in": False, "detail": f"{exc.__class__.__name__}"}
        try:
            data = json.loads(done.stdout)
        except json.JSONDecodeError:
            data = {}
        if tool == "claude":
            signed = bool(data.get("loggedIn"))
            who = data.get("email") or ""
            extra = data.get("subscriptionType") or data.get("authMethod") or ""
        else:
            signed = bool(data.get("isAuthenticated")) and done.returncode == 0
            who = (data.get("userInfo") or {}).get("email") or ""
            extra = ""
        return {"installed": True, "signed_in": signed, "account": who, "detail": extra}

    def _claude_binary(self) -> str:
        return self.bridge.claude or find_claude()

    def chat_settings(self) -> dict:
        rows = [r for r in self.bridge.state.data.get("chats", {}).values() if isinstance(r, dict)]
        row = rows[0] if rows else {}
        return {
            "model": row.get("model") or "auto",
            "claude_model": row.get("claude_model") or "default",
            "effort": row.get("effort") or "default",
            "mode": row.get("mode") or "agent",
            "chats": len(rows),
        }

    def snapshot(self) -> dict:
        return {
            "cli": self.bridge.current_cli(),
            "busy": self.bridge.runner.busy(),
            "settings": self.chat_settings(),
            "tools": {tool: self.tool_status(tool) for tool in TOOLS},
            "logins": {tool: login.snapshot() for tool, login in self.logins.items()},
            "claude_models": CLAUDE_MODELS,
            "efforts": EFFORT_LEVELS,
            "modes": list(MODES),
        }

    # ---- changes ----

    def apply_config(self, body: dict) -> str | None:
        """Validate first, then write. Return an error message or None."""
        changes: dict[str, str] = {}
        cli = body.get("cli")
        if cli is not None and cli not in TOOLS:
            return "cli must be cursor or claude"
        for field in ("model", "claude_model"):
            if body.get(field) is not None:
                try:
                    changes[field] = normalize_model(str(body[field]))
                except ValueError as exc:
                    return str(exc)
        if body.get("effort") is not None:
            if body["effort"] not in EFFORT_LEVELS:
                return "unknown effort level"
            changes["effort"] = "" if body["effort"] == "default" else body["effort"]
        if body.get("mode") is not None:
            if body["mode"] not in MODES:
                return "unknown mode"
            changes["mode"] = body["mode"]
        state = self.bridge.state
        with state.lock:
            if cli is not None:
                state.data["cli"] = cli
            for row in state.data.get("chats", {}).values():
                if isinstance(row, dict):
                    row.update(changes)
            state.save()
        return None

    def start_login(self, tool: str) -> str | None:
        if tool not in TOOLS:
            return "unknown tool"
        try:
            argv = [self._claude_binary(), "auth", "login"] if tool == "claude" else [self.bridge.agent, "login"]
        except Exception as exc:
            return str(exc)
        env = self.bridge.agent_env()
        env["NO_OPEN_BROWSER"] = "1"
        env["BROWSER"] = "true"
        with self.login_lock:
            current = self.logins.get(tool)
            if current and current.proc.poll() is None:
                current.cancel()
            try:
                self.logins[tool] = Login(tool, argv, env)
            except OSError as exc:
                return f"could not start sign-in: {exc.strerror}"
        self._status.pop(tool, None)
        return None

    # ---- server ----

    def start(self) -> None:
        page = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "setup"

            def log_message(self, *_args) -> None:
                pass

            def _allowed_host(self) -> bool:
                return (self.headers.get("Host") or "") in page.hosts

            def _authed(self) -> bool:
                jar = cookies.SimpleCookie(self.headers.get("Cookie") or "")
                have = jar["ui_key"].value if "ui_key" in jar else ""
                return hmac.compare_digest(have, page.key)

            def _send(self, code: int, body: bytes, kind: str = "application/json", extra: dict | None = None) -> None:
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
                for name, value in (extra or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, payload: dict) -> None:
                self._send(code, json.dumps(payload).encode())

            def do_GET(self) -> None:
                if not self._allowed_host():
                    return self._json(403, {"error": "bad host"})
                url = urlparse(self.path)
                supplied = parse_qs(url.query).get("key", [""])[0]
                if supplied:
                    if not hmac.compare_digest(supplied, page.key):
                        return self._json(403, {"error": "bad key"})
                    cookie = f"ui_key={page.key}; HttpOnly; SameSite=Strict; Path=/"
                    return self._send(302, b"", extra={"Location": "/", "Set-Cookie": cookie})
                if not self._authed():
                    return self._send(401, b"Open the address from scripts/ui.sh (it carries the key).", "text/plain")
                if url.path == "/api/state":
                    return self._json(200, page.snapshot())
                if url.path == "/api/models":
                    return self._json(200, {"models": [{"id": i, "label": label} for i, label in page.bridge.cursor_models()]})
                if url.path == "/":
                    return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
                self._json(404, {"error": "not found"})

            def do_POST(self) -> None:
                if not self._allowed_host() or not self._authed() or self.headers.get("X-Requested-With") != "setup":
                    return self._json(403, {"error": "forbidden"})
                try:
                    size = min(int(self.headers.get("Content-Length") or 0), 8192)
                    body = json.loads(self.rfile.read(size) or b"{}")
                except (ValueError, json.JSONDecodeError):
                    return self._json(400, {"error": "bad json"})
                if not isinstance(body, dict):
                    return self._json(400, {"error": "bad json"})
                path = urlparse(self.path).path
                error: str | None = None
                if path == "/api/config":
                    error = page.apply_config(body)
                elif path == "/api/login":
                    error = page.start_login(str(body.get("tool")))
                elif path in {"/api/login/code", "/api/login/cancel"}:
                    login = page.logins.get(str(body.get("tool")))
                    if login is None:
                        error = "no sign-in running"
                    elif path.endswith("code"):
                        login.send(str(body.get("code") or "")[:2000])
                    else:
                        login.cancel()
                elif path == "/api/refresh":
                    for tool in TOOLS:
                        page.tool_status(tool, fresh=True)
                else:
                    return self._json(404, {"error": "not found"})
                if error:
                    return self._json(400, {"error": error})
                self._json(200, {"ok": True})

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True, name="setup-page").start()
        log.info("setup page on 127.0.0.1:%d (key in %s)", self.port, self.key_path)

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
        for login in self.logins.values():
            login.cancel()


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent setup</title>
<style>
:root{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--ink:#14171c;--mute:#667085;--line:#dfe3e8;--accent:#2563eb;--ok:#15803d;--bad:#b42318}
@media(prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--ink:#e8eaed;--mute:#98a2b3;--line:#272c36;--accent:#6d9bff;--ok:#4ade80;--bad:#f87171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:28px 18px 60px}h1{font-size:22px;margin:0 0 4px}.sub{color:var(--mute);margin:0 0 22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:16px}
h2{font-size:15px;margin:0 0 12px}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:8px 0}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:9px;overflow:hidden}
.seg button{border:0;background:none;color:var(--ink);padding:8px 16px;cursor:pointer;font:inherit}
.seg button.on{background:var(--accent);color:#fff}
label{color:var(--mute);min-width:90px;font-size:13px}select,input{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:7px 10px}
select{min-width:240px}input.code{flex:1;min-width:200px}
button.act{font:inherit;border:1px solid var(--line);background:var(--bg);color:var(--ink);border-radius:8px;padding:7px 14px;cursor:pointer}
button.pri{background:var(--accent);border-color:var(--accent);color:#fff}
.pill{font-size:12px;padding:2px 9px;border-radius:99px;border:1px solid var(--line)}.ok{color:var(--ok)}.bad{color:var(--bad)}
.tool{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}.who{color:var(--mute);font-size:13px}
pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;white-space:pre-wrap;word-break:break-all;max-height:160px;overflow:auto;font-size:12px;margin:8px 0}
.warn{color:var(--mute);font-size:13px;margin:6px 0 0}#toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:8px 16px;border-radius:9px;opacity:0;transition:.2s;pointer-events:none}
#toast.show{opacity:1}a{color:var(--accent);word-break:break-all}
</style></head><body><main>
<h1>Agent setup</h1><p class="sub">Choose the tool the Telegram bot uses and sign each one in. Changes apply to the next message.</p>
<div class="card"><h2>Tool for the next message</h2>
<div class="seg" id="cli"><button data-v="cursor">Cursor</button><button data-v="claude">Claude Code</button></div>
<p class="warn" id="busy"></p></div>
<div class="card"><h2>Sign in</h2><div id="tools"></div></div>
<div class="card"><h2>Cursor</h2>
<div class="row"><label for="model">Model</label><select id="model"></select></div></div>
<div class="card"><h2>Claude Code</h2>
<div class="row"><label for="claude_model">Model</label><select id="claude_model"></select></div>
<div class="row"><label for="effort">Effort</label><select id="effort"></select></div></div>
<div class="card"><h2>Mode</h2>
<div class="row"><label for="mode">Mode</label><select id="mode"></select></div>
<p class="warn">agent can edit files and run any command as this user. ask is read only. plan only plans.</p></div>
<div id="toast"></div></main>
<script>
const $=id=>document.getElementById(id);
const H={"Content-Type":"application/json","X-Requested-With":"setup"};
let state=null,busy=false,editing=false;
function toast(t){const e=$("toast");e.textContent=t;e.classList.add("show");setTimeout(()=>e.classList.remove("show"),2200)}
async function post(p,b){const r=await fetch(p,{method:"POST",headers:H,body:JSON.stringify(b||{})});const j=await r.json();if(!r.ok)throw new Error(j.error||r.status);return j}
function fill(sel,items,val){const s=$(sel);if(editing&&document.activeElement===s)return;
 const want=items.map(i=>i.id+"|"+i.label).join(",");if(s.dataset.k!==want){s.innerHTML="";for(const i of items){const o=document.createElement("option");o.value=i.id;o.textContent=i.label;s.appendChild(o)}s.dataset.k=want}
 if(![...s.options].some(o=>o.value===val)){const o=document.createElement("option");o.value=val;o.textContent=val;s.appendChild(o)}
 s.value=val}
function simple(a){return a.map(x=>({id:x,label:x}))}
function render(){const s=state;
 for(const b of $("cli").children)b.classList.toggle("on",b.dataset.v===s.cli);
 $("busy").textContent=s.busy?"A run is in progress. It finishes on the tool it started with.":"";
 fill("claude_model",simple(s.claude_models),s.settings.claude_model);
 fill("effort",simple(s.efforts),s.settings.effort);
 fill("mode",simple(s.modes),s.settings.mode);
 if(!$("model").dataset.k)fill("model",[{id:"auto",label:"auto"}],s.settings.model);else if($("model").value!==s.settings.model&&document.activeElement!==$("model"))$("model").value=s.settings.model;
 const box=$("tools");if(box.contains(document.activeElement)&&document.activeElement.tagName==="INPUT")return;box.innerHTML="";
 for(const t of ["cursor","claude"]){const st=s.tools[t],lg=s.logins[t];
  const d=document.createElement("div");d.style.marginBottom="14px";
  const top=document.createElement("div");top.className="tool";
  const name=document.createElement("div");name.innerHTML="<b></b> <span class='pill'></span><div class='who'></div>";
  name.querySelector("b").textContent=t==="cursor"?"Cursor":"Claude Code";
  const pill=name.querySelector(".pill");pill.textContent=!st.installed?"not installed":st.signed_in?"signed in":"signed out";pill.className="pill "+(st.signed_in?"ok":"bad");
  name.querySelector(".who").textContent=[st.account,st.detail].filter(Boolean).join(" · ");
  const btn=document.createElement("button");btn.className="act"+(st.signed_in?"":" pri");btn.textContent=st.signed_in?"Sign in again":"Sign in";
  btn.disabled=!st.installed||(lg&&lg.running);btn.onclick=async()=>{try{await post("/api/login",{tool:t});poll()}catch(e){toast(e.message)}};
  top.append(name,btn);d.appendChild(top);
  if(lg&&(lg.running||lg.output)){
   if(lg.url){const p=document.createElement("p");p.append("Open this link, approve, then ");const a=document.createElement("a");a.href=lg.url;a.target="_blank";a.rel="noopener noreferrer";a.textContent=lg.url;p.append(lg.running?"come back here: ":"(done): ",a);d.appendChild(p)}
   const pre=document.createElement("pre");pre.textContent=lg.output||"Starting…";d.appendChild(pre);
   if(lg.running){const r=document.createElement("div");r.className="row";const i=document.createElement("input");i.className="code";i.placeholder="If the page gives you a code, paste it here";
    const send=document.createElement("button");send.className="act";send.textContent="Send code";send.onclick=async()=>{try{await post("/api/login/code",{tool:t,code:i.value});i.value="";toast("Code sent")}catch(e){toast(e.message)}};
    const c=document.createElement("button");c.className="act";c.textContent="Cancel";c.onclick=()=>post("/api/login/cancel",{tool:t});
    r.append(i,send,c);d.appendChild(r)}
   else{const m=document.createElement("p");m.className="warn "+(lg.ok?"ok":"bad");m.textContent=lg.ok?"Signed in.":"Sign-in did not finish. Try again.";d.appendChild(m)}}
  box.appendChild(d)}}
async function load(){const r=await fetch("/api/state");if(r.status===401){document.body.textContent="Open the address from scripts/ui.sh (it carries the key).";return}state=await r.json();render()}
async function poll(){await load()}
async function models(){const r=await fetch("/api/models");const j=await r.json();if(j.models.length){fill("model",j.models,state?state.settings.model:"auto")}}
for(const b of $("cli").children)b.onclick=async()=>{try{await post("/api/config",{cli:b.dataset.v});toast("Next message uses "+b.textContent);load()}catch(e){toast(e.message)}};
for(const f of ["model","claude_model","effort","mode"]){$(f).onfocus=()=>editing=true;$(f).onblur=()=>editing=false;
 $(f).onchange=async()=>{try{await post("/api/config",{[f]:$(f).value});toast("Saved");load()}catch(e){toast(e.message)}}}
load().then(models);setInterval(load,2500);
</script></body></html>
"""
