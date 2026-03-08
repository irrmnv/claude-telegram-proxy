# Claude Telegram Proxy

A personal Telegram bot that proxies messages to Claude via the Claude Code CLI. Uses a Claude Pro subscription instead of a paid API key.

## Quick Start

1. **Clone and configure**

```bash
cp .env.example .env
# Edit .env with your values:
#   TELEGRAM_BOT_TOKEN=your-bot-token
#   AUTHORIZED_USERS=your-telegram-user-id
```

2. **Build and run**

```bash
docker build -t claude-telegram-proxy .
docker run -d --name claude-bot --env-file .env -v claude-home:/root claude-telegram-proxy
```

3. **Authenticate Claude CLI** (first run only)

```bash
docker exec -it claude-bot claude login
```

4. **Find your Telegram user ID** (if needed)

Send `/whoami` to the bot — your user ID will appear in `docker logs claude-bot`.

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Clear conversation, start fresh |
| `/stop` | Cancel the current request |
| `/model <name>` | Switch model (e.g. `opus`, `sonnet`, `haiku`) |
| `/model` | Show current model |
| `/whoami` | Log your user ID (visible in bot logs) |

## Supported Media

- **Text** — plain text messages
- **Images** — photos and image documents
- **PDFs** — PDF documents

Audio and voice messages are not supported.
