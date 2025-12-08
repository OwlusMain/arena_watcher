# Arena Battle Watcher Bot

A configurable Telegram bot that watches the LMArena battle-mode API endpoint, detects when models join or leave the pool, and broadcasts notifications in every chat where the bot has been added.

The project does **not** hard-code a particular API route because the publicly-available LMArena endpoints are protected by Cloudflare and/or require authentication. Instead, supply the exact endpoint plus any headers/cookies that work for your account or mirror. The bot will periodically poll that endpoint, track changes, and post updates.

## Features

- Polls a user-provided HTTP endpoint at a configurable interval.
- Detects newly-added and removed battle models by identifier.
- Sends updates to every chat where `/start` was issued.
- Offers `/stop` to unsubscribe a chat.
- Persists the latest known models and chat subscriptions on disk.

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Set the following environment variables before running `python main.py`:

| Variable | Required | Description |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | ✅ | Bot token from @BotFather. |
| `ARENA_MODELS_URL` | ✅ | Fully-qualified URL returning a JSON payload that lists the current battle models. |
| `ARENA_MODELS_JSON_PATH` | ❌ | Comma-separated path (e.g. `data,models`) pointing to the array of models inside the JSON response. Leave empty if the response is already an array. |
| `ARENA_MODEL_ID_PATH` | ❌ | Comma-separated path (e.g. `meta,id`) inside each model object to use as the unique identifier. Defaults to `id`, `slug`, `identifier`, `name`, `model`. |
| `ARENA_REQUEST_HEADERS` | ❌ | JSON object encoded as a string with any extra HTTP headers (e.g. cookies, auth tokens). |
| `ARENA_REQUEST_COOKIES` | ❌ | JSON object encoded as a string for cookies that should be attached to each request. |
| `POLL_INTERVAL_SECONDS` | ❌ | Polling cadence in seconds (default `300`). |
| `STATE_PATH` | ❌ | File path for storing chat subscriptions and known models (`data/state.json` by default). |
| `GOOGLE_API_KEY` / `GENAI_API_KEY` / `GEMINI_API_KEY` | ❌ | API key for Google Generative AI. When set, the bot also polls the Google catalog via the official SDK. |
| `GOOGLE_POLL_INTERVAL_SECONDS` | ❌ | Polling cadence for the Google lookup (defaults to `POLL_INTERVAL_SECONDS`). |
| `OPENAI_API_KEY` | ❌ | API key for OpenAI. When set, the bot polls the OpenAI models API for additions/removals. |
| `OPENAI_POLL_INTERVAL_SECONDS` | ❌ | Polling cadence for the OpenAI lookup (defaults to `POLL_INTERVAL_SECONDS`). |
| `ADMIN_USER_IDS` | ❌ | Comma-separated Telegram user IDs allowed to manage model tags (e.g. `123,456`). |
| `DESIGNARENA_POLL_INTERVAL_SECONDS` | ❌ | Polling cadence for the DesignArena lookup (defaults to `POLL_INTERVAL_SECONDS`). |

### Example

If you can access LMArena's Next.js data endpoint after solving the Cloudflare challenge, the response may live at something like:

```text
https://lmarena.ai/_next/data/<BUILD_ID>/en/arena.json
```

Assuming the JSON array is available at `pageProps.models` and each model has a `slug`, you could launch the bot with:

```bash
export TELEGRAM_BOT_TOKEN="<token>"
export ARENA_MODELS_URL="https://lmarena.ai/_next/data/<BUILD_ID>/en/arena.json"
export ARENA_MODELS_JSON_PATH="pageProps,models"
export ARENA_MODEL_ID_PATH="slug"
python main.py
```

### OpenAI model tracking

If you provide an OpenAI API key, the bot will call `client.models.list()` and announce when OpenAI adds or removes models:

```bash
export OPENAI_API_KEY="<openai-key>"
# Optional overrides:
# export OPENAI_POLL_INTERVAL_SECONDS="300"
python main.py
```

### DesignArena model tracking

The bot automatically polls the DesignArena model bundle (`designarena.ai`) to detect added/removed models. No credentials are required. You can adjust cadence with `DESIGNARENA_POLL_INTERVAL_SECONDS`.

### Google/Vertex model tracking

If you provide your Gemini/Google Generative AI API key, the bot will additionally use the official `google-genai` client (`client.models.list()`) to fetch model names (no capabilities are shown) and post when new models appear or existing ones disappear:

```bash
export GOOGLE_API_KEY="<api-key>"
# or GENAI_API_KEY / GEMINI_API_KEY
# Optional overrides:
# export GOOGLE_POLL_INTERVAL_SECONDS="300"
python main.py
```

> ℹ️ You may need to supply `ARENA_REQUEST_HEADERS` and/or `ARENA_REQUEST_COOKIES` (for example a `cf_clearance` cookie) for the fetches to succeed.

## Usage

1. Start the bot locally with `python main.py`.
2. Invite the bot to any group or DM it directly.
3. Run `/start` to subscribe the chat.
4. The bot polls the configured endpoint at the specified interval and posts updates when models are added or removed.
5. Use `/stop` to unsubscribe the chat.

### Model tagging (admin only)

Configure `ADMIN_USER_IDS` to allow specific Telegram users to label models. Tags appear in italics inside parentheses after the model name in notifications.

```text
/tag <identifier|name> <tag text>  # add/update a tag
/tag <identifier|name>             # clear the tag
```

Example: `/tag gemini-2.5-flash Gemini 3 Flash` produces `gemini-2.5-flash <i>(Gemini 3 Flash)</i>` in updates.

## Development Notes

- The project uses `cloudscraper` to cope with typical Cloudflare anti-bot pages; still, you must provide working cookies/headers if deeper protection is enabled.
- State is persisted as JSON. Delete the `data/state.json` file to reset.
- Extend `arena_watcher/arena_client.py` if you need to normalise the API payload further (e.g. mapping field names).
