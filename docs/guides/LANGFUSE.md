# Langfuse model monitoring

See the [project README](../../README.md), [runtime guides](README.md), and
[project brain](../../BRAIN.md).

## Configure

Create a project in [Langfuse Cloud](https://cloud.langfuse.com), or use your
self-hosted instance. Copy that project's public and secret API keys from its
settings into `backend/.env`:

```dotenv
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-your-project-key
LANGFUSE_SECRET_KEY=sk-lf-your-project-key
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_ENVIRONMENT=development
```

Use the base URL of the region/instance where you created the project. For US
Cloud use `https://us.cloud.langfuse.com`; a self-hosted URL must be reachable
from the backend (inside its container when using Docker). Keep keys in the
backend environment, outside Git and frontend bundles. When enabled, traces
include prompts, retrieved context, history, and responses. Use HTTPS in cloud
mode; HTTP is permitted only in local mode for self-hosting. URLs containing
credentials, query strings, or fragments are rejected.

Install the dependency for an existing native environment:

```powershell
uv pip install --python backend/.venv/Scripts/python.exe -r backend/requirements-prod.txt
```

Restart the native backend. For the WSL app deployment, rebuild only the backend
to install the SDK and pick up `backend/.env`:

```powershell
./scripts/wsl-compose.ps1 --profile app up -d --build backend
./scripts/wsl-compose.ps1 logs --tail 60 backend
```

Compose already passes `backend/.env` through its `env_file`. No new model or
Langfuse server container is required when using Cloud. Existing LangSmith
tracing remains independently configurable.

## Monitor a chat

### Built-in admin dashboard

Open `/admin/monitoring` in the app, or click **Admin** in the signed-in sidebar.
Pin existing account IDs in `ADMIN_USER_IDS` (comma-separated) for cloud access.
Email/password registration does not verify email ownership, so cloud mode
ignores email allowlists. `ADMIN_EMAILS` and `ADMIN_LOCAL_EMAILS` allow additional
emails only when `DEPLOYMENT_MODE=local`. Empty allowlists deny access to everyone;
deleted accounts lose access even if their ID remains configured.
Restart the backend after changes. The dashboard uses the same login token as
chat; every monitoring data endpoint requires administrator authorization.

The dashboard reads Langfuse through the backend using Basic Auth. Project keys
are never returned to the browser. Reads work even when new tracing is disabled.
Missing keys show a setup state, and upstream failures show actionable errors.
Use Langfuse Cloud/current v4 self-hosting: this dashboard uses the current
[Metrics API v2](https://langfuse.com/docs/metrics/features/metrics-api) and
[Observations API v2](https://langfuse.com/docs/api-and-data-platform/features/observations-api).
Older self-hosted v3 instances must upgrade before using these routes.

Features: 24-hour/7-day/30-day windows, model filtering, generation counts,
reported USD cost, input/output tokens, average latency/first-token latency,
failed attempts, daily UTC activity, model comparison, paginated generation
lists, and on-demand prompt/response/usage/metadata inspection. Totals use the
Metrics API for the entire window, independently of the 50-row observation page.
The list's search and error toggle apply only to loaded calls. Optional refresh
runs once per minute while the tab is visible; it pauses during active requests
and detail inspection. The dashboard scopes all reads to this app's
`interactive_chatbot_turn` trace name and configured Langfuse environment.
Generation attempts include fallbacks; a failed attempt can belong to a chat
that eventually succeeded. Unpriced models can have incomplete cost data.

To update the existing local Docker app:

```powershell
./scripts/wsl-compose.ps1 --profile app up -d --no-deps --build backend frontend
```

Send a normal chat message, then open **Tracing** in your Langfuse project.
Look for `interactive_chatbot_turn`. Expand the graph's `generate` node to see
LLM generations and fallback attempts, including their actual model names.
Inspect latency, time to first token, failures, prompts/responses, and reported
token usage. Tokens are captured from provider metadata; costs depend on
Langfuse model pricing. Configure custom model pricing in Langfuse for local
aliases such as `persona-qwen`. Existing local feedback estimates are separate
and are not presented to Langfuse as measured token counts.

Sessions use `user_id:character_id:session_id`, matching conversation history
isolation. Metadata includes the turn ID, requested model/provider, agent, and
character. A fresh callback handler is created per turn so concurrent chats
do not share handler run state.

Coverage is the chat LangGraph and its LangChain children: local llama.cpp/vLLM,
Ollama, Gemini, DeepSeek, retrieval nodes and LangChain embedding calls where
callbacks propagate. Raw health/warmup calls and TTS/STT are outside this trace.
The SDK exports in background batches; shutdown drains pending exports off the
event loop. There is no synchronous flush on each token or turn.

## Troubleshooting

- No traces: check `LANGFUSE_ENABLED=true`, both project keys, instance/region
  URL, outbound connectivity, and the backend startup log. Initialization only
  confirms local configuration; it does not authenticate keys against Langfuse.
- Missing keys or SDK initialization/callback failures disable tracing without
  preventing chat. Export failures are handled by the SDK; chat can continue
  even when telemetry cannot reach the dashboard.
- Missing usage: some providers omit usage in their stream. OpenAI-compatible
  chat clients request streaming usage explicitly; other providers use their
  native metadata. Langfuse may estimate usage when it recognizes the model.
- Callback import errors: keep `langchain` and `langchain-core` on compatible
  major versions. Upgrade/install from the project's dependency manifests in
  a fresh environment rather than mixing LangChain 0.x and core 1.x.
- Disable exports by setting `LANGFUSE_ENABLED=false` and restarting.

Implementation follows the official
[LangChain/LangGraph integration](https://langfuse.com/integrations/frameworks/langchain).
