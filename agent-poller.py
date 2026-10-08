#!/usr/bin/env python3
"""
Agent Poller — setiap 15 detik cek inbox Command Center, auto-reply bila ada
pesan untuk agent ini.

Env:
    CC_URL       http://<tailscale-ip>:8766          (wajib)
    CC_TOKEN     token rahasia bersama               (wajib)
    AGENT_NAME   nama agent ini, mis. "muse-iqbal"   (wajib)
    CC_MODEL     model 9Router untuk auto-reply      (default: or-nemotron-lightning)
    CC_REPLY_CMD template shell, {prompt} = pesan    (default: hermes -z ...)
    CC_AUTO_REPLY 1/0 — 0 = hanya catat, tidak balas (default: 1)
    CC_MAX_DEPTH batas rantai auto-reply             (default: 3)

Contoh:
    CC_URL=http://100.64.1.2:8766 CC_TOKEN=xxx AGENT_NAME=muse-iqbal python3 agent-poller.py
"""
import json
import os
import subprocess
import time
import urllib.request
from urllib.parse import quote as _url_quote

CC_URL = os.environ.get("CC_URL", "http://127.0.0.1:8766").rstrip("/")
TOKEN = os.environ.get("CC_TOKEN", "")
AGENT = os.environ.get("AGENT_NAME", "")
MODEL = os.environ.get("CC_MODEL", "or-nemotron-lightning")
MAX_DEPTH = int(os.environ.get("CC_MAX_DEPTH", "3"))
AUTO_REPLY = os.environ.get("CC_AUTO_REPLY", "1") == "1"
POLL_SECS = 15
HEARTBEAT_SECS = 30
REPLY_CMD = os.environ.get("CC_REPLY_CMD", "")

HOME = os.path.expanduser("~")
STATE_FILE = os.path.join(HOME, f".cc-poller-{AGENT or 'noname'}.json".replace("/", "_"))
TASK_QUEUE = os.path.join(HOME, ".cc3d-task-queue.json")
# Pesan dari siapa yang dianggap perintah (selain iqbal, LightVela = perintah iqbal)
BOS_SENDERS = {"iqbal", "lightvela", "LightVela", "LightVela Lead"}


def queue_task(m):
    """Catat pesan sebagai tugas potensial untuk dikerjakan worker latar."""
    try:
        q = []
        if os.path.exists(TASK_QUEUE):
            with open(TASK_QUEUE) as f:
                q = json.load(f)
        if any(t.get("id") == m["id"] for t in q):
            return
        q.append({"id": m["id"], "from": m["from"], "to": m["to"],
                  "text": m["text"], "ts": m["ts"], "done": False,
                  "queued_at": time.time()})
        q = q[-50:]
        tmp = TASK_QUEUE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(q, f)
        os.replace(tmp, TASK_QUEUE)
    except Exception as e:
        print(f"[{AGENT}] gagal queue task: {e}", flush=True)

# Fast path: panggil 9Router langsung (jauh lebih cepat daripada via hermes CLI).
NINE_URL = os.environ.get("CC_9ROUTER_URL", "http://127.0.0.1:20128/v1").rstrip("/")
NINE_KEY = os.environ.get("CC_9ROUTER_KEY", "")
USE_DIRECT = os.environ.get("CC_DIRECT", "1") == "1"


def nine_key():
    if NINE_KEY:
        return NINE_KEY
    try:
        import sqlite3
        db = sqlite3.connect(os.path.join(HOME, ".9router/db/data.sqlite"))
        row = db.execute("SELECT key FROM apiKeys WHERE name='hermes' LIMIT 1").fetchone()
        return row[0] if row else ""
    except Exception:
        return ""


def api(method, path, data=None, retries=3):
    # Pakai curl subprocess: urllib Python intermittent RemoteDisconnected
    # lewat egress proxy, sedangkan curl 100% stabil.
    cmd = ["curl", "-s", "-m", "25", "-X", method,
           "-H", f"Authorization: Bearer {TOKEN}",
           "-H", "Accept: application/json"]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json",
                "-d", json.dumps(data)]
    cmd.append(CC_URL + path)
    last_e = None
    for attempt in range(retries):
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=30, text=True)
            if not p.stdout.strip():
                raise RuntimeError(f"curl kosong (rc={p.returncode}): {p.stderr[:100]}")
            res = json.loads(p.stdout)
            if isinstance(res, dict) and res.get("error") == "unauthorized":
                raise RuntimeError("401 unauthorized: token salah/kadaluarsa")
            return res
        except Exception as e:
            last_e = e
            if "401" in str(e):
                raise
            time.sleep(2 * (attempt + 1))
    raise last_e


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {"last_id": 0}


def save_state(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE_FILE)


def server_max_id():
    """ID pesan terbesar di server (0 bila kosong)."""
    res = api("GET", "/api/messages?since=0")
    ids = [m["id"] for m in res.get("messages", [])]
    return max(ids) if ids else 0


def maybe_reset_state(state):
    """Deteksi reset server (mis. habis redeploy, ID pesan ngulang dari 1):
    kalau ID maksimum di server lebih kecil dari catatan kita, buang catatan
    lama supaya pesan baru tidak dikira sudah dibaca."""
    try:
        mx = server_max_id()
        if mx < state.get("last_id", 0):
            print(f"[{AGENT}] server ke-reset (max id {mx} < last_id {state['last_id']}), reset state",
                  flush=True)
            state = {"last_id": 0}
            save_state(state)
    except Exception as e:
        print(f"[{AGENT}] cek reset server gagal: {e}", flush=True)
    return state


def generate_reply_direct(frm, text):
    """Via 9Router API langsung. Cepat (~2-5 dtk)."""
    key = nine_key()
    if not key:
        raise RuntimeError("CC_9ROUTER_KEY kosong dan tidak ketemu di 9Router DB")
    prompt = (f"[Pesan via Command Center dari {frm}]\n\n{text}\n\n"
              f"Balas singkat sebagai {AGENT}, Bahasa Indonesia FORMAL dan profesional (forum kerja), to-the-point, "
              f"maksimal 3 kalimat.")
    data = {"model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 300, "stream": False}
    req = urllib.request.Request(
        NINE_URL + "/chat/completions", data=json.dumps(data).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
    if d.get("choices"):
        return d["choices"][0]["message"]["content"].strip()
    raise RuntimeError(str(d.get("error", d))[:200])


def generate_reply_hermes(frm, text):
    """Via hermes CLI (lambat, tapi jalan di semua mesin standar)."""
    prompt = (f"[Pesan via Command Center dari {frm}]\n\n{text}\n\n"
              f"Balas singkat sebagai {AGENT}, Bahasa Indonesia FORMAL dan profesional (forum kerja), to-the-point.")
    env = dict(os.environ, PATH=f"{HOME}/.local/bin:" + os.environ.get("PATH", ""))
    p = subprocess.run(
        ["hermes", "-z", prompt, "--provider", "custom", "-m", MODEL,
         "--reasoning", "none", "--no-restore-cwd"],
        capture_output=True, text=True, timeout=300, env=env)
    out = (p.stdout or "").strip()
    return out or "(model tidak mengembalikan jawaban)"


def generate_reply_cmd(frm, text):
    """Template shell custom via CC_REPLY_CMD."""
    prompt = (f"[Pesan via Command Center dari {frm}]\n\n{text}\n\n"
              f"Balas singkat sebagai {AGENT}, Bahasa Indonesia FORMAL dan profesional (forum kerja), to-the-point.")
    cmd = REPLY_CMD.replace("{prompt}", prompt).replace("{model}", MODEL)
    env = dict(os.environ, PATH=f"{HOME}/.local/bin:" + os.environ.get("PATH", ""))
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       timeout=300, env=env)
    out = (p.stdout or "").strip()
    return out or f"(gagal generate balasan: {(p.stderr or '')[:200]})"


def generate_reply(frm, text):
    if REPLY_CMD:
        return generate_reply_cmd(frm, text)
    if USE_DIRECT:
        try:
            return generate_reply_direct(frm, text)
        except Exception as e:
            print(f"[{AGENT}] direct gagal ({e}), fallback ke hermes", flush=True)
    return generate_reply_hermes(frm, text)


def main():
    if not TOKEN or not AGENT or not CC_URL:
        raise SystemExit("Set CC_URL, CC_TOKEN, dan AGENT_NAME dulu. Lihat README.md")
    state = load_state()
    state = maybe_reset_state(state)
    last_hb = 0
    last_reset_check = 0
    print(f"[{AGENT}] poller jalan -> {CC_URL} (cek tiap {POLL_SECS}d, auto-reply={'on' if AUTO_REPLY else 'off'})", flush=True)
    while True:
        try:
            now = time.time()
            if now - last_hb >= HEARTBEAT_SECS:
                api("POST", "/api/heartbeat", {"agent": AGENT})
                last_hb = now
            if now - last_reset_check >= 60:
                state = maybe_reset_state(state)
                last_reset_check = now
            res = api("GET", f"/api/messages?since={state['last_id']}&to={_url_quote(AGENT, safe='')}")
            for m in res.get("messages", []):
                state["last_id"] = max(state["last_id"], m["id"])
                save_state(state)
                if m["from"] == AGENT:
                    continue
                print(f"[{AGENT}] pesan #{m['id']} dari {m['from']}: {m['text'][:80]}", flush=True)
                # Kalau dari bos (iqbal/LightVela) dan bukan sapaan singkat -> masuk antrean tugas
                if m["from"] in BOS_SENDERS and len(m["text"].strip()) > 12:
                    queue_task(m)
                    print(f"[{AGENT}] -> antrean tugas", flush=True)
                if not AUTO_REPLY:
                    continue
                if m.get("depth", 0) >= MAX_DEPTH:
                    print(f"[{AGENT}] skip auto-reply (depth {m['depth']} >= {MAX_DEPTH})", flush=True)
                    continue
                try:
                    reply = generate_reply(m["from"], m["text"])
                except Exception as e:
                    reply = f"(auto-reply gagal: {e})"[:300]
                api("POST", "/api/send", {"from": AGENT, "to": m["from"],
                                         "text": reply, "auto": True,
                                         "reply_to": m["id"]})
                print(f"[{AGENT}] auto-reply terkirim -> {m['from']}", flush=True)
        except Exception as e:
            print(f"[{AGENT}] poll error: {e}", flush=True)
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
