"""Claude Telegram Proxy — a small bot that forwards messages to Claude via CLI."""

import functools
import json
import logging
import os
import asyncio
import itertools
import subprocess
import tempfile
import time

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.constants import ParseMode

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
AUTHORIZED_USERS: set[int] = {
    int(uid.strip())
    for uid in os.environ.get("AUTHORIZED_USERS", "").split(",")
    if uid.strip()
}
DEFAULT_MODEL = os.environ.get("CLAUDE_MODEL", "haiku")
STREAM_EDIT_INTERVAL = 1.5  # seconds between Telegram message edits
PONDERING_PHRASES = [
    "Brewing...", "Pondering...", "Mulling it over...", "Cooking up a response...",
    "Chewing on it...", "Percolating...", "Noodling on it...", "Simmering...",
]

# Per-user state
user_models: dict[int, str] = {}
user_sessions: dict[int, str] = {}  # user_id -> session_id
active_processes: dict[int, subprocess.Popen] = {}  # user_id -> running process
user_locks: dict[int, asyncio.Lock] = {}  # serialize requests per user


# ---------------------------------------------------------------------------
# Auth decorator
# ---------------------------------------------------------------------------

def authorized(fn):
    @functools.wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user is None or update.effective_user.id not in AUTHORIZED_USERS:
            return
        return await fn(update, context)
    return wrapper


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _save_telegram_file(bot, file_id: str, suffix: str) -> str:
    """Download a Telegram file to a temp file, return its path."""
    file = await bot.get_file(file_id)
    data = await file.download_as_bytearray()
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(data)
    tmp.close()
    return tmp.name


def _stream_claude(user_id: int, prompt: str, queue: "asyncio.Queue[dict | None]",
                   loop: asyncio.AbstractEventLoop) -> None:
    """Run Claude CLI with stream-json output, pushing events to the queue."""
    model = user_models.get(user_id, DEFAULT_MODEL)
    session_id = user_sessions.get(user_id)
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose",
           "--include-partial-messages", "--model", model,
           "--allowedTools", "WebSearch,WebFetch,Read",
           "--append-system-prompt",
           "You are a general-purpose assistant in a Telegram chat. "
           "Answer any question the user asks, not just coding questions."]

    if session_id:
        cmd.extend(["--resume", session_id])

    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    active_processes[user_id] = proc

    # Send prompt and close stdin so the CLI starts processing
    proc.stdin.write(prompt)
    proc.stdin.close()

    put = lambda ev: asyncio.run_coroutine_threadsafe(queue.put(ev), loop)

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            put(event)

        proc.wait(timeout=300)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        put({"type": "error", "error": "Request timed out."})
    finally:
        active_processes.pop(user_id, None)

    if proc.returncode in (-9, -15):
        put({"type": "error", "error": "Request cancelled."})
    elif proc.returncode != 0:
        stderr = proc.stderr.read().strip()
        err = stderr or "Unknown error"
        log.error("Claude CLI error (rc=%d): %s", proc.returncode, err)
        put({"type": "error", "error": f"Error from Claude CLI:\n{err[:500]}"})

    put(None)  # sentinel — always last


def _get_lock(user_id: int) -> asyncio.Lock:
    return user_locks.setdefault(user_id, asyncio.Lock())


# ---------------------------------------------------------------------------
# Telegram reply — streaming edition
# ---------------------------------------------------------------------------

async def _try_edit(msg, text: str) -> None:
    """Edit a message, trying Markdown first, falling back to plain text."""
    try:
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        try:
            await msg.edit_text(text)
        except Exception:
            log.debug("edit_text failed", exc_info=True)


async def _send_final(msg, chat_id: int, text: str, bot) -> None:
    """Send the final text. If it exceeds 4096 chars, split into multiple messages."""
    max_len = 4096
    # Edit the existing message with the first chunk
    first, rest = text[:max_len], text[max_len:]
    await _try_edit(msg, first)
    # Send remaining chunks as new messages
    while rest:
        chunk, rest = rest[:max_len], rest[max_len:]
        try:
            await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await bot.send_message(chat_id=chat_id, text=chunk)


async def _iter_events(queue: asyncio.Queue):
    """Yield events from the queue until sentinel (None) or timeout."""
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=300)
        except asyncio.TimeoutError:
            yield {"type": "error", "error": "Request timed out."}
            return
        if event is None:
            return
        yield event


async def stream_to_telegram(user_id: int, prompt: str, chat_id: int, bot,
                              reply_to_message_id: int) -> None:
    """Stream Claude's response, progressively editing a Telegram message."""
    queue: asyncio.Queue[dict | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    thread_task = loop.run_in_executor(None, _stream_claude, user_id, prompt, queue, loop)

    msg = await bot.send_message(
        chat_id=chat_id, text="...", reply_to_message_id=reply_to_message_id
    )

    streaming = ""
    final = ""
    error_text = None
    thinking = False
    pondering = itertools.cycle(PONDERING_PHRASES)
    last_edit = 0.0

    async for event in _iter_events(queue):
        etype = event.get("type", "")

        if etype == "error":
            error_text = event["error"]
            break

        if etype == "stream_event":
            inner = event.get("event", {})
            inner_type = inner.get("type", "")

            if inner_type == "content_block_start":
                block = inner.get("content_block", {})
                if block.get("type") == "thinking":
                    thinking = True
                elif block.get("type") == "text":
                    thinking = False

            elif inner_type == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "text_delta":
                    streaming += delta.get("text", "")

        elif etype == "result":
            sid = event.get("session_id")
            if sid:
                user_sessions[user_id] = sid
            final = event.get("result", "")

        now = time.monotonic()
        if now - last_edit >= STREAM_EDIT_INTERVAL:
            if streaming:
                await _try_edit(msg, streaming[:4090] + " ▍")
                last_edit = now
            elif thinking:
                await _try_edit(msg, next(pondering))
                last_edit = now

    await thread_task

    text = final or streaming
    if error_text:
        await _try_edit(msg, error_text)
    elif text:
        await _send_final(msg, chat_id, text, bot)
    else:
        await _try_edit(msg, "No response from Claude.")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def handle_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the user's ID to stdout (for the operator to see in docker logs)."""
    user = update.effective_user
    if user is None:
        return
    log.info("whoami: user_id=%d username=%s name=%s", user.id, user.username, user.full_name)


@authorized
async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    async with _get_lock(user_id):
        user_sessions.pop(user_id, None)
    model = user_models.get(user_id, DEFAULT_MODEL)
    await update.message.reply_text(
        f"Conversation cleared. Using model: `{model}`",
        parse_mode=ParseMode.MARKDOWN,
    )


@authorized
async def handle_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    proc = active_processes.get(user_id)
    if proc and proc.poll() is None:
        proc.kill()
    else:
        await update.message.reply_text("Nothing to stop.")


@authorized
async def handle_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    args = context.args

    if not args:
        current = user_models.get(user_id, DEFAULT_MODEL)
        await update.message.reply_text(
            f"Current model: `{current}`\n\nUsage: `/model <model>` (e.g. `opus`, `sonnet`, `haiku`)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    new_model = args[0]
    user_models[user_id] = new_model
    await update.message.reply_text(
        f"Model set to: `{new_model}`",
        parse_mode=ParseMode.MARKDOWN,
    )


@authorized
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    message = update.message

    text = message.text or message.caption or None
    media_files: list[str] = []

    try:
        if message.photo:
            media_files.append(
                await _save_telegram_file(context.bot, message.photo[-1].file_id, ".jpg"))

        if message.document and message.document.mime_type:
            mime = message.document.mime_type
            if mime.startswith("image/") or mime == "application/pdf":
                ext = ".pdf" if mime == "application/pdf" else f".{mime.split('/')[-1]}"
                media_files.append(
                    await _save_telegram_file(context.bot, message.document.file_id, ext))

        if not text and not media_files:
            return

        prompt_parts = [f"[Attached file: {p}]" for p in media_files]
        if text:
            prompt_parts.append(text)

        await context.bot.send_chat_action(chat_id=message.chat_id, action="typing")
        async with _get_lock(user_id):
            await stream_to_telegram(
                user_id, "\n".join(prompt_parts), message.chat_id, context.bot, message.message_id
            )
    finally:
        for f in media_files:
            try:
                os.unlink(f)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("whoami", handle_whoami))
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("stop", handle_stop))
    app.add_handler(CommandHandler("model", handle_model))
    app.add_handler(MessageHandler(
        filters.TEXT | filters.PHOTO | filters.Document.IMAGE | filters.Document.PDF,
        handle_message,
    ))

    log.info("Bot started. Authorized users: %s", AUTHORIZED_USERS)
    app.run_polling()


if __name__ == "__main__":
    main()
