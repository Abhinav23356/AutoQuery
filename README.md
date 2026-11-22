# AutoQuery
[![(https://github.com/user-attachments/assets/d42652b4-809f-4b79-b64a-8ee3b5b214e7)]]

AutoQuery is a full-stack analytics chat app. React/Vite provides a searchable `⌘K` palette, tabbed chart/data/SQL results, and expandable timed tool traces. FastAPI stores conversations and uploads, AutoGen orchestrates Gemini, and an MCP chart server builds Recharts configurations.

Every investigation must have its own uploaded CSV before analysis. The agent receives a query tool for that investigation's CSV and the chart tool; it has no PostgreSQL analytics tool and cannot use another investigation's data. Chats without an upload receive an instruction to upload a CSV instead of falling back to sample data or general knowledge.

PostgreSQL is used for account and session records plus persistence: conversation history, execution traces, and uploaded CSV bytes. On request, the API reloads that chat's saved CSV into a separate read-only SQLite query cache. It is keyed by conversation ID and is never shared across chats. CSV uploads are limited to 5 MB, 20,000 rows, and 100 columns.

## Start the app

Requirements: Docker Compose and a Google AI Studio Gemini API key. Copy `.env.example` to `.env` and set `GEMINI_API_KEY`.

```sh
docker compose up --build
```

Open <http://localhost:5173> and create an account. Passwords are hashed with Argon2; the browser receives a 14-day HttpOnly session cookie, while PostgreSQL stores only its SHA-256 token hash. Conversations and uploaded datasets are isolated by account. The first account created in a pre-authentication database adopts existing unowned conversations. The API is at <http://localhost:8000>; `/api/health` stays public. PostgreSQL initializes account, session, chat, and upload storage without loading a sample analytics dataset. To remove all locally persisted chats and uploads, run `docker compose down -v` (this deletes the PostgreSQL volume).

For deployments, set `POSTGRES_PASSWORD` and `APP_PASSWORD` to strong secrets and set `SESSION_COOKIE_SECURE=true` when serving the app over HTTPS. Keep HTTPS enabled so session cookies are sent securely. The app database role receives access only to the `users`, `sessions`, `conversations`, `executions`, and `uploaded_datasets` tables. The chart server returns validated Recharts-ready chart configurations; no generated code is executed.

## Architecture

```text
React dashboard → FastAPI → AutoGen AssistantAgent → Gemini
                    ├─ PostgreSQL app storage (chat history and uploaded CSV bytes)
                    ├─ per-conversation read-only SQLite cache (CSV queries)
                    └─ chart-mcp-server → chart configuration
```

`backend/agent/mcp_client.py` starts the chart MCP server only. `backend/agent/orchestrator.py` adapts its schema and the current chat's CSV query function into AutoGen tools. The PostgreSQL app connection is used by FastAPI for persistence, never by the analytics agent. Gemini is configured through Google's OpenAI-compatible Chat Completions endpoint; set `GEMINI_MODEL` to a model available to the AI Studio key.

For a file-by-file backend explanation, request lifecycle diagram, data isolation details, safety boundaries, limitations, and a presentation-ready walkthrough, see [Backend Deep Dive](docs/backend-deep-dive.md).

## API

Register or sign in through the web UI. `POST /api/auth/register` and `POST /api/auth/login` accept an email and a password of at least 12 characters; `POST /api/auth/logout` ends the current session. The `GET /api/auth/me` endpoint returns the signed-in account. All conversation, dataset, execution, and chat endpoints require a valid session cookie.

- `POST /api/chat` — `{ "question": "...", "conversation_id": "optional-id" }`
- `POST /api/conversations/{id}/dataset` — upload a CSV for that chat
- `GET /api/conversations`
- `GET /api/conversations/{id}`
- `DELETE /api/conversations/{id}` — permanently deletes the conversation and its uploaded CSV
- `GET /api/conversations/{id}/dataset`
- `DELETE /api/conversations/{id}/dataset`
- `GET /api/executions/{id}`
- `GET /api/health`

Chat is request/response rather than streamed. Tool events are returned with the answer and shown in the expandable execution trace.

## Local development

Start PostgreSQL and initialize app storage:

```sh
docker compose up postgres init-app-db
```

Then install `backend/requirements.txt`, set `APP_DATABASE_URL` to the app database role, set `GEMINI_API_KEY`, run `uvicorn main:app --reload` from `backend/`, and serve the frontend with `cd frontend && npm install && npm run dev`.
