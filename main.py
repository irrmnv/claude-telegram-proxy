"""Claude Telegram Proxy — a small bot that forwards messages to Claude via CLI."""

import functools
import io
import json
import logging
import os
import asyncio
import subprocess
import tempfile

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
log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
AUTHORIZED_USERS: set[int] = {
    int(uid.strip())
    for uid in os.environ.get("AUTHORIZED_USERS", "").split(",")
    if uid.strip()
}
DEFAULT_MODEL = os.environ.get("CLAUDE_MODEL", "haiku")

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

async def download_bytes(file_obj) -> bytes:
    buf = io.BytesIO()
    await file_obj.download_to_memory(buf)
    return buf.getvalue()


def _run_claude(user_id: int, prompt: str) -> str:
    """Run Claude CLI in a subprocess. Called from a thread via asyncio.to_thread."""
    model = user_models.get(user_id, DEFAULT_MODEL)
    session_id = user_sessions.get(user_id)
    cmd = ["claude", "-p", "--output-format", "json", "--model", model,
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

    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=300)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return "Request timed out."
    finally:
        active_processes.pop(user_id, None)

    if proc.returncode in (-9, -15):
        return "Request cancelled."

    if proc.returncode != 0:
        err = stderr.strip() or stdout.strip()
        log.error("Claude CLI error (rc=%d): %s", proc.returncode, err)
        return f"Error from Claude CLI:\n{err[:500]}"

    try:
        output = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout.strip() or "No response from Claude."

    if "session_id" in output:
        user_sessions[user_id] = output["session_id"]

    return output.get("result", stdout.strip())


def _get_lock(user_id: int) -> asyncio.Lock:
    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()
    return user_locks[user_id]


async def send_to_claude(user_id: int, text: str, media_files: list[str] | None = None) -> str:
    prompt_parts = []
    if media_files:
        for path in media_files:
            prompt_parts.append(f"[Attached file: {path}]")
    if text:
        prompt_parts.append(text)

    prompt = "\n".join(prompt_parts) if prompt_parts else "(empty message)"
    async with _get_lock(user_id):
        return await asyncio.to_thread(_run_claude, user_id, prompt)


# ---------------------------------------------------------------------------
# Telegram reply
# ---------------------------------------------------------------------------

async def send_reply(update: Update, text: str) -> None:
    max_len = 4096
    while text:
        chunk, text = text[:max_len], text[max_len:]
        try:
            await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await update.message.reply_text(chunk)


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
    temp_files: list[str] = []

    try:
        if message.photo:
            photo = message.photo[-1]
            file = await context.bot.get_file(photo.file_id)
            img_bytes = await download_bytes(file)
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            tmp.write(img_bytes)
            tmp.close()
            media_files.append(tmp.name)
            temp_files.append(tmp.name)

        if message.document and message.document.mime_type:
            mime = message.document.mime_type
            if mime.startswith("image/") or mime == "application/pdf":
                ext = ".pdf" if mime == "application/pdf" else f".{mime.split('/')[-1]}"
                file = await context.bot.get_file(message.document.file_id)
                doc_bytes = await download_bytes(file)
                tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
                tmp.write(doc_bytes)
                tmp.close()
                media_files.append(tmp.name)
                temp_files.append(tmp.name)

        if not text and not media_files:
            return

        await context.bot.send_chat_action(chat_id=message.chat_id, action="typing")
        reply = await send_to_claude(user_id, text or "", media_files)
        await send_reply(update, reply)
    finally:
        for f in temp_files:
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
