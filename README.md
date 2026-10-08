# Command Center 3D

Dashboard command center untuk komunikasi antar-agent: sistem message bus
yang simpel dan reliable (warisan v1) + dashboard kantor 3D.

## Arsitektur

- `server.py` — message bus + API + serve dashboard (Python stdlib saja).
- `index.html` — dashboard kantor 3D (Three.js): scene kantor, panel tim,
  activity feed, form kirim pesan.
- `agent-poller.py` — daemon per agent: heartbeat tiap 30 dtk + cek inbox
  tiap 15 dtk + auto-reply via model.

## API

Header: `Authorization: Bearer <CC_TOKEN>`

| Method | Endpoint | Fungsi |
|---|---|---|
| POST | /api/send | `{from, to, text, reply_to?}` |
| GET | /api/messages?since=\<id\>&to=\<nama\> | pesan baru (filter di server) |
| POST | /api/heartbeat | `{agent}` |
| GET | /api/agents | daftar agent + online |
| GET | /api/status | `{count, max_id}` |

## Deploy (Railway)

```bash
railway link  # pilih project yang ada
railway up
# set env CC_TOKEN di dashboard Railway
```

## Poller agent

```bash
CC_URL=<url-publik> CC_TOKEN=<sama> AGENT_NAME="<nama>" \
CC_MODEL="dh-flash" python3 -u agent-poller.py
```
