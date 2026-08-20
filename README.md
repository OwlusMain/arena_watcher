# Arena Battle Watcher Bot

A configurable Telegram bot that watches the Arena battle-mode API endpoint, detects when models join or leave the pool, and broadcasts notifications in every chat where the bot has been added.

The project does **not** hard-code a particular API route because the publicly-available Arena endpoints are protected by Cloudflare and/or require authentication. Instead, supply the exact endpoint plus any headers/cookies that work for your account or mirror. The bot will periodically poll that endpoint, track changes, and post updates.

## Features

- Polls a user-provided HTTP endpoint at a configurable interval.
- Detects newly-added and removed battle models by identifier.
- Optionally probes newly-added Arena models directly and posts the returned text or image.
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
| `ARENA_DIRECT_URL` | ❌ | Arena endpoint used to directly run a newly-added Arena model by id. When unset, direct probing is disabled. For Arena itself this should usually be `https://arena.ai/nextjs-api/stream/create-evaluation`. |
| `ARENA_DIRECT_REQUEST_TEMPLATE` | ❌ | JSON request body template for the direct Arena call. If omitted, the bot uses Arena's `create-evaluation` first-turn payload shape by default. Supported placeholders in string values: `$MODEL_ID`, `$PROMPT`, `$MODALITY`, `$REQUEST_ID`, `$EVALUATION_ID`, `$USER_MESSAGE_ID`, `$MODEL_MESSAGE_ID`, `$RECAPTCHA_V3_TOKEN`. |
| `ARENA_DIRECT_HEADERS` | ❌ | JSON object for headers on the direct Arena call. Defaults to `ARENA_REQUEST_HEADERS`. |
| `ARENA_DIRECT_COOKIES` | ❌ | JSON object for cookies on the direct Arena call. Defaults to `ARENA_REQUEST_COOKIES`. |
| `ARENA_DIRECT_BOOTSTRAP_URL` | ❌ | Optional page to open before the direct POST. If unset for `arena.ai`, the bot first loads `https://arena.ai/text/direct` to mirror the browser flow. |
| `ARENA_DIRECT_RECAPTCHA_V3_TOKEN` | ❌ | Static `recaptchaV3Token` to inject into the direct Arena payload. |
| `ARENA_DIRECT_RECAPTCHA_V3_TOKEN_COMMAND` | ❌ | Shell command that prints a fresh `recaptchaV3Token` to stdout before each direct Arena probe. Useful when the token must be refreshed dynamically. |
| `ARENA_DIRECT_TEXT_RESPONSE_PATH` | ❌ | Comma-separated JSON path to the returned text for text probes. |
| `ARENA_DIRECT_IMAGE_URL_RESPONSE_PATH` | ❌ | Comma-separated JSON path to an image URL for image probes. |
| `ARENA_DIRECT_IMAGE_BASE64_RESPONSE_PATH` | ❌ | Comma-separated JSON path to base64 image bytes for image probes. |
| `ARENA_DIRECT_IMAGE_MIME_TYPE_RESPONSE_PATH` | ❌ | Comma-separated JSON path to the image MIME type when using base64 image data. |
| `ARENA_DIRECT_TIMEOUT_SECONDS` | ❌ | Timeout for the direct Arena probe request (default `60`). |
| `POLL_INTERVAL_SECONDS` | ❌ | Polling cadence in seconds (default `300`). |
| `REMOVAL_WAITLIST_SECONDS` | ❌ | Delay before announcing removals when a poll returns removals but no additions (default `1800`). |
| `STATE_PATH` | ❌ | File path for storing chat subscriptions and known models (`data/state.json` by default). |
| `GOOGLE_API_KEY` / `GENAI_API_KEY` / `GEMINI_API_KEY` | ❌ | API key for Google Generative AI. When set, the bot also polls the Google catalog via the official SDK. |
| `GOOGLE_POLL_INTERVAL_SECONDS` | ❌ | Polling cadence for the Google lookup (defaults to `POLL_INTERVAL_SECONDS`). |
| `OPENAI_API_KEY` | ❌ | API key for OpenAI. When set, the bot polls the OpenAI models API for additions/removals. |
| `OPENAI_POLL_INTERVAL_SECONDS` | ❌ | Polling cadence for the OpenAI lookup (defaults to `POLL_INTERVAL_SECONDS`). |
| `ANTHROPIC_API_KEY` | ❌ | API key for Anthropic. When set, the bot polls the Anthropic models API for additions/removals. |
| `ANTHROPIC_API_VERSION` | ❌ | Anthropic API version header (defaults to `2023-06-01`). |
| `ANTHROPIC_POLL_INTERVAL_SECONDS` | ❌ | Polling cadence for the Anthropic lookup (defaults to `POLL_INTERVAL_SECONDS`). |
| `ADMIN_USER_IDS` | ❌ | Comma-separated Telegram user IDs allowed to manage model tags (e.g. `123,456`). |
| `DESIGNARENA_POLL_INTERVAL_SECONDS` | ❌ | Polling cadence for the DesignArena lookup (defaults to `POLL_INTERVAL_SECONDS`). |
| `DESIGNARENA_BASE_URL` | ❌ | Base URL for DesignArena scraping (defaults to `https://www.designarena.ai/`). |
| `DESIGNARENA_REQUEST_HEADERS` | ❌ | JSON object encoded as a string with extra HTTP headers for DesignArena requests. |
| `DESIGNARENA_REQUEST_COOKIES` | ❌ | JSON object encoded as a string with cookies for DesignArena requests (for example a clearance cookie if the site enables bot protection). |

### Example

If you can access Arena's Next.js data endpoint after solving the Cloudflare challenge, the response may live at something like:

```text
https://arena.ai/_next/data/<BUILD_ID>/en/arena.json
```

Assuming the JSON array is available at `pageProps.models` and each model has a `slug`, you could launch the bot with:

```bash
export TELEGRAM_BOT_TOKEN="<token>"
export ARENA_MODELS_URL="https://arena.ai/_next/data/<BUILD_ID>/en/arena.json"
export ARENA_MODELS_JSON_PATH="pageProps,models"
export ARENA_MODEL_ID_PATH="slug"
export ARENA_DIRECT_URL="https://arena.ai/nextjs-api/stream/create-evaluation"
export ARENA_DIRECT_HEADERS='{"accept":"*/*","content-type":"text/plain;charset=UTF-8","origin":"https://arena.ai","referer":"https://arena.ai/c/$EVALUATION_ID","x-request-id":"$REQUEST_ID"}'
export ARENA_DIRECT_RECAPTCHA_V3_TOKEN_COMMAND="/absolute/path/to/print_fresh_recaptcha_token.sh"
python main.py
```

When direct probing is enabled, the bot uses these prompts for newly-added Arena models:

- Image-capable models: `Draw a flipboard and write on a flipboard your name and the company who made you`
- Text models: `Tell me what model are you, who made you and what is your knowledge cutoff`

For `arena.ai`, the bot mirrors the browser's first-turn flow more closely now:

- it opens `/text/direct` first by default
- it generates a fresh evaluation id plus message ids
- it POSTs raw JSON text to `/nextjs-api/stream/create-evaluation`
- it decodes Arena's streamed line protocol to recover text and images

`/text/direct` is treated as a bootstrap page only. The evaluation/session id used by `create-evaluation` is generated client-side by the bot.

### OpenAI model tracking

If you provide an OpenAI API key, the bot will call `client.models.list()` and announce when OpenAI adds or removes models:

```bash
export OPENAI_API_KEY="<openai-key>"
# Optional overrides:
# export OPENAI_POLL_INTERVAL_SECONDS="300"
python main.py
```

### DesignArena model tracking

The bot polls DesignArena's public registry endpoint (`https://www.designarena.ai/api/registry`) to detect added/removed models. You can adjust cadence with `DESIGNARENA_POLL_INTERVAL_SECONDS`.

If DesignArena starts serving a Vercel security checkpoint to your runtime, provide the same browser headers and/or cookies that solve the challenge in `DESIGNARENA_REQUEST_HEADERS` or `DESIGNARENA_REQUEST_COOKIES`.

### Anthropic model tracking

If you provide an Anthropic API key, the bot will call the `/v1/models` endpoint and announce when Anthropic adds or removes models:

```bash
export ANTHROPIC_API_KEY="<anthropic-key>"
# Optional overrides:
# export ANTHROPIC_API_VERSION="2023-06-01"
# export ANTHROPIC_POLL_INTERVAL_SECONDS="300"
python main.py
```

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
