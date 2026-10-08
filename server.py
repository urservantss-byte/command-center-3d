#!/usr/bin/env python3
"""
Command Center — message bus + dashboard untuk komunikasi antar agent via Tailscale.

Cara jalan:
    CC_TOKEN="rahasia-bersama" CC_PORT=8766 python3 server.py
    -> listen di 0.0.0.0:8766, buka http://<tailscale-ip>:8766 dari browser.

API (butuh header Authorization: Bearer <CC_TOKEN>):
    POST /api/send        {from, to, text, auto?, reply_to?} -> {ok, id, depth}
    GET  /api/messages?since=<id>&to=<nama>                  -> {messages: [...]}
    POST /api/heartbeat   {agent}                            -> {ok}
    GET  /api/status                                         -> {ok, count, max_id}
    GET  /api/agents                                         -> {agents: [...]}

Pesan: {id, from, to, text, ts, auto, reply_to, depth}
- depth dihitung server dari reply_to (anti reply-loop tak berujung).
- Token JANGAN di-commit ke repo. Buat random:  python3 -c "import secrets; print(secrets.token_hex(24))"
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("PORT", os.environ.get("CC_PORT", "8766")))
TOKEN = os.environ.get("CC_TOKEN", "")
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "messages.json")
MAX_MSGS = 1000
ONLINE_AFTER = 90  # detik sejak heartbeat terakhir = online

lock = threading.Lock()
messages = []
agents = {}  # nama -> {"last_seen": ts, "ip": str}
next_id = 1


def load():
    global messages, next_id
    try:
        with open(DATA_FILE) as f:
            data = json.load(f)
            messages = data.get("messages", [])[-MAX_MSGS:]
            next_id = max([m["id"] for m in messages], default=0) + 1
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        pass


def save():
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"messages": messages[-MAX_MSGS:]}, f)
    os.replace(tmp, DATA_FILE)


def check_auth(handler):
    if not TOKEN:
        return True  # tanpa token = mode terbuka (tidak disarankan)
    return handler.headers.get("Authorization") == f"Bearer {TOKEN}"


DASHBOARD = """<!DOCTYPE html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Command Center</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#12100d;color:#e8e0d4;font-family:system-ui,sans-serif;min-height:100vh}
header{background:#1c1813;border-bottom:2px solid #e0573f;padding:14px 20px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}
h1{font-size:20px}h1 span{color:#e0573f}
#tok{background:#0d0b09;border:1px solid #444;color:#e8e0d4;padding:8px 10px;border-radius:8px;width:220px}
main{display:grid;grid-template-columns:240px 1fr;gap:14px;padding:14px;max-width:1200px;margin:0 auto}
@media(max-width:700px){main{grid-template-columns:1fr}}
.card{background:#1c1813;border:1px solid #2e2820;border-radius:12px;padding:14px}
.card h3{font-size:14px;color:#e0573f;margin-bottom:10px;text-transform:uppercase;letter-spacing:1px}
.agent{padding:8px 10px;border-radius:8px;margin-bottom:6px;background:#241f18;font-size:14px;display:flex;justify-content:space-between;align-items:center}
#convs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.chip{background:#241f18;border:1px solid #444;color:#e8e0d4;padding:6px 12px;border-radius:20px;font-size:13px;cursor:pointer}
.chip.sel{background:#e0573f;border-color:#e0573f;color:#fff;font-weight:bold}
.del{background:none;border:0;color:#9a8f7d;cursor:pointer;font-size:15px;margin-left:4px}
.del:hover{color:#e0573f}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:7px}
.on{background:#4caf50}.off{background:#555}
#feed{height:56vh;overflow-y:auto;display:flex;flex-direction:column;gap:8px;margin-bottom:12px}
.msg{background:#241f18;border-radius:10px;padding:10px 12px;font-size:14px;border-left:3px solid #e0573f}
.msg .meta{font-size:11px;color:#9a8f7d;margin-bottom:4px}
.msg.auto{border-left-color:#4caf50;opacity:.92}
.msg.admin{border-left-color:#f5c542}
.abadge{background:#f5c542;color:#12100d;font-size:10px;font-weight:bold;border-radius:4px;padding:1px 6px;margin-left:6px;vertical-align:1px}
.msg .rt{font-size:11px;color:#9a8f7d;font-style:italic}
form#send{display:grid;grid-template-columns:1fr 1fr;gap:8px}
form#send input,form#send textarea{background:#0d0b09;border:1px solid #444;color:#e8e0d4;padding:9px 10px;border-radius:8px;font-size:14px;font-family:inherit}
form#send textarea{grid-column:1/-1;min-height:64px;resize:vertical}
form#send button{grid-column:1/-1;background:#e0573f;border:0;color:#fff;padding:10px;border-radius:8px;font-size:15px;cursor:pointer;font-weight:bold}
form#send button:hover{background:#c74a34}
#status{font-size:12px;color:#9a8f7d}
</style></head><body>
<header>
  <h1><span>⛩</span> Command Center</h1>
  <div><input id="tok" type="password" placeholder="CC token…"><span id="status"></span></div>
</header>
<main>
  <div class="card"><h3>Agents</h3><div id="agents"></div></div>
  <div class="card"><h3>Message Feed</h3><div id="convs"></div><div id="feed"></div>
    <form id="send">
      <input id="f" list="senders" placeholder="dari (nama)">
      <datalist id="senders"></datalist>
      <input id="t" list="receivers" placeholder="untuk (nama agent / all)">
      <datalist id="receivers"></datalist>
      <textarea id="x" placeholder="tulis pesan…"></textarea>
      <button>Kirim ➤</button>
    </form>
  </div>
</main>
<script>
const tok=()=>document.getElementById('tok').value||localStorage.cc_tok||'';
document.getElementById('tok').value=localStorage.cc_tok||'';
document.getElementById('tok').onchange=e=>localStorage.cc_tok=e.target.value;
document.getElementById('f').value=localStorage.cc_from||'iqbal';
const isAdmin=n=>n.toLowerCase()==='iqbal';
let lastId=0,allMsgs=[],currentConv='all';
const me=()=>document.getElementById('f').value.trim()||'iqbal';
function matchConv(x){
  if(currentConv==='all')return true;
  return (x.from===me()&&x.to===currentConv)||(x.from===currentConv&&x.to===me());
}
function msgHTML(x){
  return `<div class="meta">${esc(x.from)} → ${esc(x.to)} · ${new Date(x.ts*1000).toLocaleTimeString()}${x.auto?' · auto-reply':''}${isAdmin(x.from)?'<span class="abadge">👑 ADMIN</span>':''}</div>${x.reply_to?`<div class="rt">↳ balasan #${x.reply_to}</div>`:''}<div>${esc(x.text)}</div>`;
}
function renderFeed(){
  const feed=document.getElementById('feed');feed.innerHTML='';
  allMsgs.filter(matchConv).forEach(x=>{
    const d=document.createElement('div');d.className='msg'+(x.auto?' auto':'')+(isAdmin(x.from)?' admin':'');
    d.innerHTML=msgHTML(x);feed.appendChild(d);});
  feed.scrollTop=feed.scrollHeight;
}
function renderChips(agents){
  const c=document.getElementById('convs');
  const opts=[['all','🌐 Semua'],...agents.map(g=>[g.name,'💬 '+g.name])];
  c.innerHTML=opts.map(([v,l])=>`<button class="chip${currentConv===v?' sel':''}" data-v="${esc(v)}">${esc(l)}</button>`).join('');
  c.querySelectorAll('.chip').forEach(b=>b.onclick=()=>{currentConv=b.dataset.v;
    document.getElementById('t').value=currentConv==='all'?'all':currentConv;
    renderChips(agents);renderFeed();});
}
async function api(p,o={}){const r=await fetch(p,{...o,headers:{...(o.headers||{}),'Authorization':'Bearer '+tok()}});if(!r.ok)throw 0;return r.json()}
function esc(s){return s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function tick(){
  try{
    const a=await api('/api/agents');document.getElementById('status').textContent='● terhubung';
    document.getElementById('agents').innerHTML=a.agents.map(g=>
      `<div class="agent"><span><span class="dot ${g.online?'on':'off'}"></span>${esc(g.name)}</span><span><small>${g.online?'online':'offline'}</small> <button class="del" data-n="${esc(g.name)}" title="hapus">×</button></span></div>`).join('')||'<small>belum ada agent</small>';
    document.querySelectorAll('.del').forEach(b=>b.onclick=async()=>{if(confirm('Hapus agent '+b.dataset.n+'?')){await api('/api/agents/'+encodeURIComponent(b.dataset.n),{method:'DELETE'});tick();}});
    const names=['iqbal',...a.agents.map(g=>g.name)];
    document.getElementById('senders').innerHTML=names.map(n=>`<option value="${esc(n)}">`).join('');
    document.getElementById('receivers').innerHTML=['all',...a.agents.map(g=>g.name)].map(n=>`<option value="${esc(n)}">`).join('');
    const m=await api('/api/messages?since='+lastId);
    m.messages.forEach(x=>{lastId=Math.max(lastId,x.id);allMsgs.push(x);});
    allMsgs=allMsgs.slice(-300);
    renderChips(a.agents);renderFeed();
  }catch(e){document.getElementById('status').textContent='○ butuh token / server mati'}
}
document.getElementById('send').onsubmit=async e=>{e.preventDefault();
  const f=document.getElementById('f').value.trim(),t=document.getElementById('t').value.trim(),x=document.getElementById('x').value.trim();
  if(!f||!t||!x)return alert('lengkapi dari, untuk, dan pesan');
  localStorage.cc_from=f;
  await api('/api/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({from:f,to:t,text:x})});
  document.getElementById('x').value='';tick();};
tick();setInterval(tick,5000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "CommandCenter/1.0"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        if n <= 0 or n > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except (json.JSONDecodeError, ValueError):
            return {}

    def do_DELETE(self):
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        if not check_auth(self):
            return self._json({"error": "unauthorized"}, 401)
        if u.path.startswith("/api/agents/"):
            from urllib.parse import unquote
            name = unquote(u.path[len("/api/agents/"):]).strip()[:64]
            with lock:
                gone = agents.pop(name, None) is not None
            return self._json({"ok": gone})
        return self._json({"error": "not found"}, 404)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            body = DASHBOARD.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not u.path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        if not check_auth(self):
            return self._json({"error": "unauthorized"}, 401)
        if u.path == "/api/agents":
            now = time.time()
            with lock:
                out = [{"name": n, "online": now - v["last_seen"] < ONLINE_AFTER,
                        "last_seen": v["last_seen"]} for n, v in agents.items()]
            return self._json({"agents": sorted(out, key=lambda x: x["name"])})
        if u.path == "/api/status":
            with lock:
                ms = list(messages)
            return self._json({"ok": True, "count": len(ms),
                               "max_id": max([m["id"] for m in ms], default=0)})
        if u.path == "/api/messages":
            q = parse_qs(u.query)
            try:
                since = int(q.get("since", ["0"])[0])
            except ValueError:
                since = 0
            to = q.get("to", [None])[0]
            with lock:
                msgs = [m for m in messages
                        if m["id"] > since and (to is None or m["to"] in (to, "all", "*"))]
            return self._json({"messages": msgs})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        if not check_auth(self):
            return self._json({"error": "unauthorized"}, 401)
        data = self._body()
        if u.path == "/api/heartbeat":
            name = str(data.get("agent", "")).strip()[:64]
            if not name:
                return self._json({"error": "agent required"}, 400)
            with lock:
                agents[name] = {"last_seen": time.time(),
                                "ip": self.client_address[0]}
            return self._json({"ok": True})
        if u.path == "/api/send":
            frm = str(data.get("from", "")).strip()[:64]
            to = str(data.get("to", "")).strip()[:64]
            text = str(data.get("text", ""))[:4000]
            auto = bool(data.get("auto", False))
            reply_to = data.get("reply_to")
            if not frm or not to or not text.strip():
                return self._json({"error": "from, to, text required"}, 400)
            with lock:
                global next_id
                depth = 0
                if isinstance(reply_to, int):
                    parent = next((m for m in messages if m["id"] == reply_to), None)
                    if parent:
                        depth = parent.get("depth", 0) + 1
                msg = {"id": next_id, "from": frm, "to": to, "text": text,
                       "ts": time.time(), "auto": auto,
                       "reply_to": reply_to if isinstance(reply_to, int) else None,
                       "depth": depth}
                messages.append(msg)
                next_id += 1
                save()
            return self._json({"ok": True, "id": msg["id"], "depth": depth})
        return self._json({"error": "not found"}, 404)


def main():
    load()
    if not TOKEN:
        print("PERINGATAN: CC_TOKEN kosong — API terbuka tanpa auth. Jangan dipakai di jaringan publik.")
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Command Center jalan di 0.0.0.0:{PORT}  (buka http://<tailscale-ip>:{PORT})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
