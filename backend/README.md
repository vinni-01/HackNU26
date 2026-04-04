# AI Brainstorm Canvas — Backend

A production-ready backend for a real-time collaborative AI brainstorming canvas. An autonomous Gemini AI agent lives inside a shared [tldraw](https://tldraw.com) whiteboard, perceives the canvas spatially, hears participant voices through LiveKit, and contributes ideas, connections, and media — as a peer, not a tool.



## Setup

### Prerequisites

- Python 3.11+
- Redis (local or Cloud)
- A LiveKit Cloud project
- Gemini API key
- Cloudflare R2 bucket
- Cloudflare Vectorize index
- Higgsfield API key

### 1. Clone & Install

```powershell
git clone <repo-url>
cd HackNU26
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure Environment

Copy the template and fill in your credentials:

```powershell
copy .env.example .env   # or edit .env directly
```

Required values in `.env`:

| Variable | Description |
|----------|-------------|
| `SECRET_KEY` | JWT signing key — generate with `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DATABASE_URL` | SQLite (dev) or PostgreSQL (prod) — e.g. `sqlite:///./app.db` |
| `LIVEKIT_URL` | `wss://your-project.livekit.cloud` |
| `LIVEKIT_API_KEY` | LiveKit project API key |
| `LIVEKIT_API_SECRET` | LiveKit project API secret |
| `GEMINI_API_KEY` | Google AI Studio API key |
| `HIGGSFIELD_API_KEY` | Higgsfield AI API key |
| `R2_ENDPOINT` | Cloudflare R2 S3-compatible endpoint |
| `R2_ACCESS_KEY` | R2 access key ID |
| `R2_SECRET_KEY` | R2 secret access key |
| `R2_PUBLIC_BASE` | Public base URL for R2 assets |
| `CF_ACCOUNT_ID` | Cloudflare account ID |
| `CF_API_TOKEN` | Cloudflare API token (Vectorize permissions) |

### 3. Start Redis

```powershell
# If using Docker:
docker run -d -p 6379:6379 redis:7
```

### 4. Run the FastAPI Server

```powershell
uvicorn app.main:app --reload
```

API docs available at: http://localhost:8000/docs

### 5. Run the LiveKit Agent

```powershell
python -m agent.agent
```

### 6. Run the Higgsfield Worker

```powershell
python -m worker.higgsfield_worker
```

---

## API Reference

### Auth
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/auth/register` | Create a new user account |
| `POST` | `/auth/login` | Login and receive a JWT |
| `GET`  | `/auth/me` | Get current authenticated user |

### Boards
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/boards` | Create a new brainstorm board |
| `GET`  | `/boards` | List your boards |
| `GET`  | `/boards/{id}` | Get a specific board |
| `PATCH`| `/boards/{id}/mode` | Switch agent mode (`autonomous` / `permission`) |
| `GET`  | `/boards/{id}/state` | Fetch live canvas state from Redis |

### LiveKit
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/livekit/token` | Issue a participant token for a board's room |

### Canvas WebSocket
| Protocol | Path | Description |
|----------|------|-------------|
| `WS` | `/canvas/ws/{room_name}` | Bidirectional relay between Cloudflare DO and the agent |

### Memory (Spatial RAG)
| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/memory/snapshots/{board_id}` | List board snapshots in R2 |
| `POST` | `/memory/snapshots/{board_id}/take` | Trigger an immediate snapshot |
| `GET`  | `/memory/clusters/{board_id}?q=...` | Semantic search over canvas clusters |
| `POST` | `/memory/clusters/{board_id}` | Upsert a cluster embedding |

### Webhooks
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/webhooks/higgsfield/complete` | Higgsfield job completion callback |

---

## Agent Behavior

The Gemini agent (`Muse`) is a **spatial peer**, not an assistant:

- **Perceives** the canvas in real-time via a tiered perception model (Focused → Blurry → Peripheral)
- **Listens** to all participant voices with diarization-aware attribution
- **Decides** when to speak using silence detection, keyword triggers, and open-question recognition
- **Places** Ghost Shapes (opacity 0.4) with Accept/Reject affordances
- **Generates** cinematic video and images via the Higgsfield pipeline
- **Recalls** spatial memory from Vectorize ("that revenue cluster on the right")

### Agent Modes

| Mode | Behavior |
|------|----------|
| `permission` | Agent publishes a request card before any canvas edit; waits for human Accept |
| `autonomous` | Agent acts directly on the canvas without approval |

Switch mode at runtime: `PATCH /boards/{id}/mode`

---

## Project Structure

```
HackNU26/
├── app/                    # FastAPI application
│   ├── core/               # Config, database, security
│   ├── db/                 # Legacy shim (re-exports from core)
│   ├── models/             # SQLModel table definitions
│   ├── routers/            # API route handlers
│   ├── schemas/            # Pydantic request/response schemas
│   ├── deps.py             # FastAPI dependency injectors
│   └── main.py             # App factory + lifespan
├── agent/                  # LiveKit + Gemini agent
│   ├── agent.py            # Entrypoint, InnerThoughts, VoiceMutex, DecisionEngine
│   ├── config.py           # EnvConfig, persona prompt, tuning params
│   ├── perception_loop.py  # Background canvas perception cycle
│   ├── spatial_utils.py    # R-Tree, tiered perception, SpatialRAG, SnapshotManager
│   └── tools.py            # Gemini function-calling tool definitions & handlers
├── worker/
│   └── higgsfield_worker.py  # Async BullMQ-style media generation worker
├── .env                    # Secrets (never commit this)
├── requirements.txt        # Python dependencies
└── README.md
```

---

## Security

- All secrets are loaded exclusively from `.env` via `pydantic-settings` — nothing is hardcoded
- JWT tokens use `SECRET_KEY` from env; algorithm defaults to HS256
- Webhook endpoint validates HMAC-SHA256 signatures when `CF_WEBHOOK_SECRET` is set
- Sandboxed Python execution in the agent blocks dangerous imports
- Agent feedback loop is mitigated by filtering `source='agent'` events at the relay

---

## Development Tips

- Set `ENVIRONMENT=development` in `.env` to enable permissive CORS
- Leave `CF_WEBHOOK_SECRET` empty in dev to skip webhook signature validation  
- The R-Tree spatial index gracefully falls back to brute-force if `rtree` is not installed
- `SpatialRAG` falls back to random embeddings if `sentence-transformers` is unavailable
- SQLite is fine for local dev; switch to PostgreSQL for production
