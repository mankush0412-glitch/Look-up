import asyncio
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter, BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes

logger = logging.getLogger(__name__)

SEND_INTERVAL = 0.05  # ~20 msg/sec


async def _send_text_formatted(bot, chat_id: int, text: str, entities=None):
    """
    Try to send plain text with formatting in this order:
        1. Raw entities (perfect formatting copy)
        2. MarkdownV2
        3. HTML
        4. Plain text (final fallback — always works)
    Returns the mode that worked.
    """
    # 1. Entities mode (exact formatting, no escaping needed)
    if entities:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                entities=entities,
            )
            return "entities"
        except BadRequest:
            pass

    # 2. MarkdownV2
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return "markdown"
    except BadRequest:
        pass

    # 3. HTML
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        return "html"
    except BadRequest:
        pass

    # 4. Plain text
    await bot.send_message(chat_id=chat_id, text=text)
    return "plain"


def register_broadcast_handlers(application: Application, config, db_module) -> None:

    async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        user = update.effective_user

        if user is None or user.id not in getattr(config, "admin_ids", set()):
            await message.reply_text("⛔ You are not authorized to use this command.")
            return

        # ----- Decide the mode -----
        # Mode 1: Reply to any message → copy_message() (perfect copy: text,
        #         entities, photos, videos, documents, stickers, captions…)
        # Mode 2: /broadcast <text>    → text with MarkdownV2/HTML auto-detect

        reply_msg = message.reply_to_message
        use_copy = bool(reply_msg)
        source_text = None
        source_entities = None
        mode_note = ""

        if use_copy:
            mode_note = "reply → copy_message (full copy: media + formatting)"
        else:
            text = " ".join(context.args or []) if context.args else ""
            if not text:
                await message.reply_text(
                    "📢 BROADCAST — Usage\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    "Mode 1 — Reply to a message (BEST):\n"
                    "• Reply to any message with /broadcast\n"
                    "• Copies text, links, bold, italic, code, photos,\n"
                    "  videos, documents, stickers — everything.\n\n"
                    "Mode 2 — Plain text:\n"
                    "• /broadcast <your text>\n"
                    "• MarkdownV2 or HTML auto-detect hoga.\n\n"
                    "MarkdownV2 examples:\n"
                    "  *bold*   _italic_   `code`\n"
                    "  [link](https://example.com)\n\n"
                    "HTML examples:\n"
                    "  <b>bold</b>  <i>italic</i>  <code>code</code>\n"
                    "  <a href='https://example.com'>link</a>"
                )
                return
            source_text = text
            mode_note = "text → auto-detect (MarkdownV2 / HTML / plain)"

        user_ids = db_module.list_users()
        if not user_ids:
            await message.reply_text("No registered users found.")
            return

        status = await message.reply_text(
            f"📨 Broadcasting to {len(user_ids)} user(s)...\n"
            f"Mode: {mode_note}"
        )

        sent, failed, removed = 0, 0, 0
        detected_mode = None

        for uid in user_ids:
            try:
                if use_copy:
                    # Perfect copy — no "forwarded from" header
                    await context.bot.copy_message(
                        chat_id=uid,
                        from_chat_id=message.chat_id,
                        message_id=reply_msg.message_id,
                    )
                    detected_mode = "copy_message"
                else:
                    used = await _send_text_formatted(
                        context.bot, uid, source_text,
                        entities=source_entities,
                    )
                    if detected_mode is None:
                        detected_mode = used
                sent += 1
            except Forbidden:
                db_module.remove_user(uid)
                removed += 1
            except RetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
                try:
                    if use_copy:
                        await context.bot.copy_message(
                            chat_id=uid,
                            from_chat_id=message.chat_id,
                            message_id=reply_msg.message_id,
                        )
                    else:
                        await _send_text_formatted(
                            context.bot, uid, source_text,
                            entities=source_entities,
                        )
                    sent += 1
                except Exception:
                    failed += 1
            except Exception:
                logger.exception("Broadcast failed for user %s", uid)
                failed += 1

            await asyncio.sleep(SEND_INTERVAL)

        summary = (
            "✅ Broadcast finished\n"
            f"• Mode: {detected_mode or mode_note}\n"
            f"• Sent: {sent}\n"
            f"• Failed: {failed}\n"
            f"• Removed (blocked bot): {removed}"
        )
        try:
            await status.edit_text(summary)
        except Exception:
            await message.reply_text(summary)

    application.add_handler(CommandHandler("broadcast", broadcast))