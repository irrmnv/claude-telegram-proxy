# Claude Telegram Proxy

A Python application that proxies Claude for personal use via Telegram.

## Design Principles

- **Minimal dependencies.** Prefer up to 20 lines of custom code over pulling in a large library. Only add a dependency when it genuinely earns its weight.
- **Simplicity first.** The codebase should follow the spirit of Karpathy's nanoGPT — small, readable, and self-contained.

## Requirements

### Message Rendering

Claude responses are Markdown-formatted. Telegram's Markdown support is limited — it handles bold, italic, inline code, code blocks, and links, but does not support tables, headings, or embedded media. The bot sends replies with `ParseMode.MARKDOWN` and falls back to plain text if parsing fails.

### Media Support

The proxy handles inbound images (photos and image documents) and PDF files. These are saved to temporary files and referenced by path when invoking the CLI.

Audio and voice messages are not supported — the Claude CLI cannot process audio files. These message types are silently ignored.

### Authorization

The bot must only respond to messages from a hardcoded list of authorized Telegram user IDs. Messages from any other user must be silently ignored — no response, no error, no acknowledgment.

A `/whoami` command is available to all users — it logs the sender's user ID, username, and name to stdout (visible via `docker logs`) without replying in chat.

### Claude Backend

Instead of using the paid Anthropic API directly, the proxy uses **Claude Code CLI** (`claude`) as its backend. This leverages a Claude Pro subscription, which includes CLI access, avoiding the need for a separate API key.

Messages are sent to Claude by invoking the CLI in print mode with streaming output (`claude -p --output-format stream-json`). The CLI emits newline-delimited JSON events which are parsed in real time to extract text deltas and the session ID. The CLI is given access to `WebSearch`, `WebFetch`, and `Read` tools via `--allowedTools`. An `--append-system-prompt` flag overrides the CLI's default coding-focused persona, instructing it to act as a general-purpose assistant.

The CLI runs in a background thread that pushes stream events into an `asyncio.Queue`. An async consumer reads the queue and progressively edits a Telegram message with the accumulated response text, throttled to ~1.5 seconds between edits to respect Telegram's rate limits. A cursor character (▍) is shown during streaming and removed on completion. This gives users immediate visual feedback as the response is generated.

#### Model Selection

Users can switch the Claude model at any time via a `/model` command (e.g. `/model opus`). The selected model persists per-user for subsequent messages until changed again. The default model is `haiku`. The model is passed to the CLI via the `--model` flag.

### Conversation Management

Conversations are persistent by default — each new message continues the existing dialogue via CLI session IDs (`--resume <session-id>`). The `/start` command explicitly clears the session and resets to a fresh state. There is no automatic context clearing between messages.

The `/stop` command kills the currently running Claude CLI process for the user, cancelling an in-flight request.

### Concurrency

Requests are serialized per user via an `asyncio.Lock`. This prevents race conditions where concurrent messages or a `/start` command could interleave with an in-flight request and corrupt the session state. Different users are fully concurrent.

### Deployment

The project is containerized with Docker. The Dockerfile installs Python, Node.js (for the `claude` CLI via npm), `poppler-utils` (for PDF text extraction), and the bot's Python dependencies. A `docker-compose.yml` is provided with a persistent volume for the container's home directory (preserving Claude CLI auth state).

The Claude CLI must be authenticated inside the container on first run via `docker exec -it claude-bot claude login`.

Environment variables (`TELEGRAM_BOT_TOKEN`, `AUTHORIZED_USERS`) are provided via a `.env` file.
