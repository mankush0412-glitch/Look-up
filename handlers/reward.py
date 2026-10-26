import logging
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

ASK_CREDITS = 1
ASK_DAYS = 2


def _cancel_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="reward_cancel")]
    ])


async def _notify_reward_claim(context, config, user, token,
                                credits, new_balance, link_doc=None):
    """Send a claim notification to log chat + all admins."""
    now = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")

    username = f"@{user.username}" if getattr(user, "username", None) else "No username"
    full_name = getattr(user, "full_name", None) or "Unknown"

    creator_line = ""
    if link_doc and link_doc.get("created_by"):
        creator_line = f"👤 Link created by: {link_doc['created_by']}\n"

    uses_line = ""
    if link_doc is not None:
        uses_line = f"📊 Total claims on link: {int(link_doc.get('uses', 0)) + 1}\n"

    text = (
        "🎁 REWARD CLAIMED\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 User: {full_name}\n"
        f"🔗 Username: {username}\n"
        f"🆔 Telegram ID: {user.id}\n"
        f"💰 Credits received: {credits}\n"
        f"💳 New balance: {new_balance}\n"
        f"🔑 Token: {token}\n"
        f"{creator_line}"
        f"{uses_line}"
        f"🕒 Time: {now}"
    )

    # 1. Log channel (if configured)
    log_chat_id = getattr(config, "log_chat_id", None)
    if log_chat_id:
        try:
            await context.bot.send_message(
                chat_id=log_chat_id,
                text=text,
                disable_web_page_preview=True,
            )
        except Exception:
            logger.exception("Failed to send reward claim to log chat")

    # 2. All admins
    for admin_id in getattr(config, "admin_ids", set()):
        if log_chat_id and str(admin_id) == str(log_chat_id):
            continue
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=text,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.warning(
                "Failed to notify admin %s: %s",
                admin_id, type(exc).__name__,
            )


async def process_reward_claim(update: Update, context: ContextTypes.DEFAULT_TYPE,
                               token: str, db_module, config):
    """Called from /start handler when arg starts with 'reward_'."""
    user = update.effective_user
    if not user:
        return

    # Fetch link doc BEFORE claiming (to get created_by + current uses)
    link_doc = None
    try:
        link_doc = db_module.get_reward_link(token)
    except Exception:
        logger.exception("Failed to fetch reward link doc")

    result = db_module.claim_reward(token, user.id)
    status = result.get("status")

    if status == "ok":
        credits = int(result.get("credits", 0))
        db_module.add_credits(user.id, credits)
        new_balance = db_module.get_credits(user.id)

        await update.effective_message.reply_text(
            "🎁 REWARD CLAIMED\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Credits received: {credits}\n"
            f"💳 New balance: {new_balance}"
        )

        try:
            await _notify_reward_claim(
                context, config, user, token,
                credits, new_balance, link_doc=link_doc,
            )
        except Exception:
            logger.exception("Reward claim notification failed")

    elif status == "already_claimed":
        await update.effective_message.reply_text(
            "⚠️ ALREADY CLAIMED\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "You have already claimed this reward link."
        )
    elif status == "expired":
        await update.effective_message.reply_text(
            "⌛ LINK EXPIRED\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "This reward link has expired."
        )
    else:
        await update.effective_message.reply_text(
            "❌ INVALID LINK\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "This reward link is invalid or unknown."
        )


def register_reward_handlers(application, config, db_module):

    async def reward_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id not in getattr(config, "admin_ids", set()):
            await update.effective_message.reply_text("⛔ Admin only.")
            return ConversationHandler.END

        context.user_data.pop("reward_credits", None)

        await update.effective_message.reply_text(
            "🎁 CREATE REWARD LINK\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Step 1 of 2\n\n"
            "How many credits should each user receive?\n"
            "Send a number (e.g. 5)",
            reply_markup=_cancel_kb(),
        )
        return ASK_CREDITS

    async def ask_credits(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id not in getattr(config, "admin_ids", set()):
            return ConversationHandler.END

        text = (update.message.text or "").strip()
        try:
            credits = int(text)
            if credits <= 0 or credits > 10000:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid number. Send a positive integer (1–10000).",
                reply_markup=_cancel_kb(),
            )
            return ASK_CREDITS

        context.user_data["reward_credits"] = credits

        await update.message.reply_text(
            "🎁 CREATE REWARD LINK\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Step 2 of 2\n\n"
            f"Credits per user: {credits}\n\n"
            "Now send expiry in days (e.g. 7)\n"
            "Allowed: 1 – 365",
            reply_markup=_cancel_kb(),
        )
        return ASK_DAYS

    async def ask_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id not in getattr(config, "admin_ids", set()):
            return ConversationHandler.END

        text = (update.message.text or "").strip()
        try:
            days = int(text)
            if days <= 0 or days > 365:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid number of days. Send an integer (1–365).",
                reply_markup=_cancel_kb(),
            )
            return ASK_DAYS

        credits = context.user_data.pop("reward_credits", 0)
        if credits <= 0:
            await update.message.reply_text(
                "❌ Something went wrong. Please try /reward again."
            )
            return ConversationHandler.END

        token = secrets.token_urlsafe(8)
        expires_at = datetime.now(timezone.utc) + timedelta(days=days)

        db_module.create_reward_link(
            token=token,
            credits=credits,
            expires_at=expires_at,
            created_by=user.id,
        )

        bot_username = (await context.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start=reward_{token}"

        exp_str = expires_at.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")

        await update.message.reply_text(
            "✅ REWARD LINK CREATED\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Credits per user: {credits}\n"
            f"⏳ Expires: {exp_str}\n"
            f"🔗 Link:\n{link}\n\n"
            "Each user can claim once.\n"
            "Share this link with anyone.\n\n"
            "🔔 You will be notified for every claim."
        )
        return ConversationHandler.END

    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.pop("reward_credits", None)

        if update.callback_query:
            try:
                await update.callback_query.answer()
            except Exception:
                pass

            try:
                await update.callback_query.message.edit_text(
                    "❎ Reward creation cancelled."
                )
            except Exception as exc:
                err_text = str(exc).lower()
                if "not modified" not in err_text:
                    logger.warning("Cancel edit failed: %s", exc)
                    try:
                        await update.callback_query.message.reply_text(
                            "❎ Reward creation cancelled."
                        )
                    except Exception:
                        logger.exception("Cancel fallback failed")
        else:
            try:
                await update.effective_message.reply_text(
                    "❎ Reward creation cancelled."
                )
            except Exception:
                logger.exception("Cancel reply failed")

        return ConversationHandler.END

    conv = ConversationHandler(
        entry_points=[CommandHandler("reward", reward_entry)],
        states={
            ASK_CREDITS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_credits),
                CallbackQueryHandler(cancel, pattern=r"^reward_cancel$"),
            ],
            ASK_DAYS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_days),
                CallbackQueryHandler(cancel, pattern=r"^reward_cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    application.add_handler(conv)