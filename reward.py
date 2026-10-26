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


async def process_reward_claim(update: Update, context: ContextTypes.DEFAULT_TYPE,
                               token: str, db_module):
    """Called from /start handler when arg starts with 'reward_'."""
    user = update.effective_user
    if not user:
        return

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
            "Share this link with anyone."
        )
        return ConversationHandler.END

    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.pop("reward_credits", None)

        if update.callback_query:
            await update.callback_query.answer()
            try:
                await update.callback_query.message.edit_text(
                    "❎ Reward creation cancelled."
                )
            except Exception:
                await update.callback_query.message.reply_text(
                    "❎ Reward creation cancelled."
                )
        else:
            await update.effective_message.reply_text("❎ Reward creation cancelled.")
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