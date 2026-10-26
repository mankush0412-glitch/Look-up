import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from .common import admin_only

IST = ZoneInfo("Asia/Kolkata")


def _as_utc(value):
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _fmt_dt(value):
    if not value:
        return "-"
    try:
        return _as_utc(value).astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")
    except Exception:
        return str(value)


def _api_stats_keyboard(page: int, total: int, page_size: int):
    rows = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin_api_stats:{page - 1}"))
    if (page + 1) * page_size < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin_api_stats:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"admin_api_stats:{page}")])
    rows.append([InlineKeyboardButton("⬅️ Settings", callback_data="stg_home")])
    return InlineKeyboardMarkup(rows)


def _api_stats_text(db_module, page: int = 0):
    summary = db_module.get_api_admin_summary()
    page_data = db_module.get_api_keys_admin_page(page=page, page_size=6)
    rows = page_data["rows"]
    total = page_data["total"]
    max_page = max(1, (total + page_data["page_size"] - 1) // page_data["page_size"])

    lines = [
        "📊 API ADMIN STATS",
        "━━━━━━━━━━━━━━━━━━━━",
        f"👥 Active API users: {summary['active_users']}",
        f"🔑 Keys: {summary['total_keys']} total • {summary['active_keys']} active • {summary['expired_keys']} expired • {summary['revoked_keys']} revoked",
        f"📈 Requests: {summary['requests_today']} today • {summary['requests_month']} this month • {summary['requests_total']} total",
        f"⏱ Rate-limited today: {summary['rate_limited_today']}",
        "",
        f"KEYS — Page {page + 1}/{max_page}",
    ]

    if not rows:
        lines.append("No API keys issued yet.")
    status_map = {"active": "✅ Active", "expired": "⌛ Expired", "revoked": "🚫 Revoked"}
    for row in rows:
        usage = row.get("usage") or {}
        prefix = row.get("key_prefix") or "-"
        lines.extend([
            "",
            f"{status_map.get(row.get('status'), row.get('status', '-'))} • {prefix}…",
            f"User: {row.get('user_id', '-')} • Created: {_fmt_dt(row.get('created_at'))}",
            f"Expires: {_fmt_dt(row.get('expires_at'))}",
            f"Usage: {int(usage.get('today', 0))} today • {int(usage.get('month', 0))} month • {int(row.get('total_requests', 0) or 0)} total",
            f"Last used: {_fmt_dt(row.get('last_used_at'))}",
            f"Last endpoint: {row.get('last_endpoint') or '-'}",
            f"Rate-limited: {int(row.get('rate_limited_requests', 0) or 0)} total",
        ])
    return "\n".join(lines), page_data


def register_admin_handlers(application, config, db_module):

    @admin_only(config)
    async def addcredit(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if len(context.args) != 2:
            await update.effective_message.reply_text(
                "Use: /addcredit <telegram_id> <amount>"
            )
            return

        try:
            user_id = int(context.args[0])
            amount = int(context.args[1])
        except ValueError:
            await update.effective_message.reply_text("Invalid user ID or amount.")
            return

        if not db_module.get_user(user_id):
            db_module.add_user(user_id, update.effective_user.id)

        balance = db_module.add_credits(user_id, amount)
        await update.effective_message.reply_text(
            f"✅ Credits updated.\nUser: {user_id}\nBalance: {balance}"
        )

    @admin_only(config)
    async def setcredit(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if len(context.args) != 2:
            await update.effective_message.reply_text(
                "Use: /setcredit <telegram_id> <amount>"
            )
            return

        try:
            user_id = int(context.args[0])
            amount = int(context.args[1])
        except ValueError:
            await update.effective_message.reply_text("Invalid user ID or amount.")
            return

        if not db_module.get_user(user_id):
            db_module.add_user(user_id, update.effective_user.id)

        balance = db_module.set_credits(user_id, amount)
        await update.effective_message.reply_text(
            f"✅ Credit balance set.\nUser: {user_id}\nBalance: {balance}"
        )

    @admin_only(config)
    async def credit(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if len(context.args) != 1:
            await update.effective_message.reply_text(
                "Use: /credit <telegram_id>"
            )
            return

        try:
            user_id = int(context.args[0])
        except ValueError:
            await update.effective_message.reply_text("Invalid user ID.")
            return

        await update.effective_message.reply_text(
            f"💳 User {user_id}\nCredits: {db_module.get_credits(user_id)}"
        )

    @admin_only(config)
    async def setcontactbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if len(context.args) != 1:
            current = db_module.get_contact_bot_username()
            await update.effective_message.reply_text(
                "Use: /setcontactbot @YourContactBot\n"
                f"Current: @{current}" if current else "Use: /setcontactbot @YourContactBot\nCurrent: Not configured"
            )
            return

        username = context.args[0].strip().lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            await update.effective_message.reply_text("❌ Invalid Telegram username.")
            return
        if not username.lower().endswith("bot"):
            await update.effective_message.reply_text("❌ Please provide a bot username ending in 'bot'.")
            return

        try:
            chat = await context.bot.get_chat(f"@{username}")
            resolved = (getattr(chat, "username", None) or username).lstrip("@")
        except Exception:
            await update.effective_message.reply_text(
                "❌ Could not resolve that bot username. Check the username and try again."
            )
            return

        db_module.set_contact_bot_username(resolved)
        await update.effective_message.reply_text(
            "✅ SRC contact bot updated.\n"
            f"Contact bot: @{resolved}\n\n"
            "The 🧾 Buy SRC button will now open this bot with a pre-written message."
        )

    @admin_only(config)
    async def contactbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
        current = db_module.get_contact_bot_username()
        if current:
            await update.effective_message.reply_text(f"🧾 SRC contact bot: @{current}")
        else:
            await update.effective_message.reply_text(
                "SRC contact bot is not configured.\nUse: /setcontactbot @YourContactBot"
            )

    @admin_only(config)
    async def api_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text, page_data = _api_stats_text(db_module, 0)
        await update.effective_message.reply_text(
            text,
            reply_markup=_api_stats_keyboard(0, page_data["total"], page_data["page_size"]),
        )

    async def api_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        user = update.effective_user
        if not q or not user or user.id not in getattr(config, "admin_ids", set()):
            if q:
                await q.answer("Admin only.", show_alert=True)
            return
        try:
            page = max(0, int(q.data.split(":", 1)[1]))
        except Exception:
            page = 0
        text, page_data = _api_stats_text(db_module, page)
        await q.answer()
        markup = _api_stats_keyboard(page, page_data["total"], page_data["page_size"])
        try:
            await q.message.edit_text(text, reply_markup=markup)
        except Exception as exc:
            if "not modified" not in str(exc).lower():
                await q.message.reply_text(text, reply_markup=markup)

    application.add_handler(CommandHandler("addcredit", addcredit))
    application.add_handler(CommandHandler("setcredit", setcredit))
    application.add_handler(CommandHandler("credit", credit))
    application.add_handler(CommandHandler("setcontactbot", setcontactbot))
    application.add_handler(CommandHandler("contactbot", contactbot))
    application.add_handler(CommandHandler("apistats", api_stats_cmd))
    application.add_handler(CallbackQueryHandler(api_stats_callback, pattern=r"^admin_api_stats:\d+$"))
