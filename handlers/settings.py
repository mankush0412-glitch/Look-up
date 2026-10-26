import logging
import re
from datetime import timezone
from zoneinfo import ZoneInfo

import httpx

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

ADD_CHANNEL_INPUT = 1
SET_CONTACT_INPUT = 2
SET_WELCOME_IMAGE = 3
SET_RZP_KEY_ID = 4
SET_RZP_KEY_SECRET = 5
SET_RZP_BUSINESS_NAME = 6
SET_PAYMENT_LOG_CHAT = 7
ADMIN_ONLY = "⛔ Admin only."
IST = ZoneInfo("Asia/Kolkata")


def _is_admin(user, config) -> bool:
    return bool(user) and user.id in getattr(config, "admin_ids", set())


def _parse_chat_identifier(value: str):
    value = value.strip()
    if value.lstrip("-").isdigit():
        return int(value)
    return value


def _settings_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Force Join Channels", callback_data="stg_fj")],
        [
            InlineKeyboardButton("🤖 Contact Bot", callback_data="stg_contact"),
            InlineKeyboardButton("🖼 Welcome Image", callback_data="stg_welcome"),
        ],
        [InlineKeyboardButton("💳 Payment Settings", callback_data="stg_payment")],
        [InlineKeyboardButton("📊 API Stats", callback_data="admin_api_stats:0")],
        [InlineKeyboardButton("📋 Current Settings", callback_data="stg_overview")],
        [InlineKeyboardButton("❌ Close", callback_data="stg_close")],
    ])


def _fj_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Channel", callback_data="stg_fj_add")],
        [
            InlineKeyboardButton("📋 List / Remove", callback_data="stg_fj_list"),
            InlineKeyboardButton("🔍 Check Status", callback_data="stg_fj_check"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="stg_home")],
    ])


def _contact_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Set Contact Bot", callback_data="stg_contact_set")],
        [InlineKeyboardButton("⬅️ Back", callback_data="stg_home")],
    ])


def _welcome_kb(has_image: bool):
    rows = [[InlineKeyboardButton("📤 Upload Image", callback_data="stg_welcome_set")]]
    if has_image:
        rows.append([InlineKeyboardButton("🗑 Remove Image", callback_data="stg_welcome_remove")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="stg_home")])
    return InlineKeyboardMarkup(rows)


def _mask_key_id(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return "Not configured"
    if len(value) <= 10:
        return value[:4] + "…"
    return value[:8] + "…" + value[-4:]


def _payment_kb(mode: str, configured: bool):
    manual_label = "✅ Manual QR" if mode == "manual" else "Manual QR"
    razorpay_label = "✅ Razorpay" if mode == "razorpay" else "Razorpay"
    rows = [
        [
            InlineKeyboardButton(manual_label, callback_data="stg_pay_mode:manual"),
            InlineKeyboardButton(razorpay_label, callback_data="stg_pay_mode:razorpay"),
        ],
        [InlineKeyboardButton("🔑 Set Key ID", callback_data="stg_rzp_keyid_set")],
        [InlineKeyboardButton("🔒 Set Key Secret", callback_data="stg_rzp_secret_set")],
        [InlineKeyboardButton("🏷 Business Name", callback_data="stg_rzp_name_set")],
        [InlineKeyboardButton("📢 Payment Log Group", callback_data="stg_payment_log")],
        [InlineKeyboardButton("📊 Payment Stats", callback_data="stg_payment_stats")],
    ]
    if configured:
        rows.append([InlineKeyboardButton("🧪 Test Razorpay", callback_data="stg_rzp_test")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="stg_home")])
    return InlineKeyboardMarkup(rows)


def _payment_log_kb(has_chat: bool):
    rows = [[InlineKeyboardButton("✏️ Set Payment Log Group", callback_data="stg_payment_log_set")]]
    if has_chat:
        rows.append([InlineKeyboardButton("🧪 Test Log Group", callback_data="stg_payment_log_test")])
        rows.append([InlineKeyboardButton("🗑 Remove Log Group", callback_data="stg_payment_log_clear")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="stg_payment")])
    return InlineKeyboardMarkup(rows)


def _as_utc(value):
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _fmt_ist(value) -> str:
    if not value:
        return "-"
    try:
        return _as_utc(value).astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")
    except Exception:
        return str(value)


def _order_label(order: dict) -> str:
    order_type = str(order.get("order_type") or "credits")
    if order_type == "credits":
        credits = int(order.get("credits_amount", 0) or 0)
        return f"{credits} Credits" if credits else "Credits"
    if order_type == "bot_day":
        return "24h Unlimited Bot"
    if order_type == "api_month":
        return "API 30 Days"
    return order_type.replace("_", " ").title()


def _status_label(status: str) -> str:
    status = str(status or "pending").lower()
    return {
        "approved": "✅ Paid",
        "pending": "⏳ Pending",
        "processing": "⏳ Processing",
        "rejected": "❌ Rejected",
        "payment_failed": "⚠️ Failed",
        "failed": "⚠️ Failed",
    }.get(status, status.title())


def _payment_stats_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 Payment History", callback_data="stg_payment_history:0")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="stg_payment_stats")],
        [InlineKeyboardButton("⬅️ Payment Settings", callback_data="stg_payment")],
    ])


def _payment_history_kb(page: int, has_prev: bool, has_next: bool):
    nav = []
    if has_prev:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"stg_payment_history:{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"stg_payment_history:{page+1}"))
    rows = []
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("📊 Payment Stats", callback_data="stg_payment_stats")])
    rows.append([InlineKeyboardButton("⬅️ Payment Settings", callback_data="stg_payment")])
    return InlineKeyboardMarkup(rows)


SETTINGS_TEXT = (
    "⚙️ SETTINGS PANEL\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "Configure your bot from here.\n\n"
    "Choose a category below:"
)

FJ_TEXT = (
    "📢 FORCE JOIN CHANNELS\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "Manage channels/groups users must join before using the bot.\n\n"
    "🌐 Public → membership auto-verify\n"
    "🔒 Private → per-user 24h join request pass"
)


def register_settings_handlers(application, config, db_module):

    def _current_settings_text() -> str:
        contact_bot = db_module.get_contact_bot_username()
        welcome_set = bool(db_module.get_welcome_image_file_id())
        force_channels = len(db_module.list_force_channels())
        payment_mode = db_module.get_payment_mode()
        payment_log = db_module.get_payment_log_chat_title() or db_module.get_payment_log_chat_id() or "Not configured"
        return (
            "⚙️ CURRENT SETTINGS\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 SRC Contact Bot: @{contact_bot}\n" if contact_bot else "⚙️ CURRENT SETTINGS\n━━━━━━━━━━━━━━━━━━━━\n🤖 SRC Contact Bot: Not configured\n"
        ) + (
            f"🖼 Welcome Image: {'Configured' if welcome_set else 'Not configured'}\n"
            f"💳 Payment Mode: {'Razorpay' if payment_mode == 'razorpay' else 'Manual QR'}\n"
            f"📢 Payment Log: {payment_log}\n"
            f"📢 Force Join Channels: {force_channels}"
        )

    async def _show_settings_panel(message):
        await message.reply_text(SETTINGS_TEXT, reply_markup=_settings_kb())

    async def _edit_or_reply(query, text, reply_markup=None, **kwargs):
        try:
            await query.message.edit_text(text, reply_markup=reply_markup, **kwargs)
        except Exception:
            await query.message.reply_text(text, reply_markup=reply_markup, **kwargs)

    # ---------- core add channel helper ----------

    async def _add_channel(update, context, identifier):
        msg = update.effective_message
        try:
            chat = await context.bot.get_chat(identifier)
        except Exception as exc:
            await msg.reply_text(
                "❌ Could not access this chat.\n"
                "Make sure the bot is added as admin.\n\n"
                f"Error: {type(exc).__name__}"
            )
            return

        username = getattr(chat, "username", None)
        is_public = bool(username)

        if is_public:
            invite_link = f"https://t.me/{username}"
        else:
            try:
                invite = await context.bot.create_chat_invite_link(
                    chat_id=chat.id,
                    name="LookupBot_ForceJoin",
                    creates_join_request=True,
                )
                invite_link = invite.invite_link
            except Exception as exc:
                await msg.reply_text(
                    "❌ Could not create join-request invite link.\n"
                    "Bot needs admin + 'Invite Users' permission.\n\n"
                    f"Error: {type(exc).__name__}"
                )
                return

        title = (
            getattr(chat, "title", None)
            or getattr(chat, "full_name", None)
            or str(chat.id)
        )

        db_module.add_force_channel(
            chat_id=chat.id,
            title=title,
            invite_link=invite_link,
            username=username,
        )

        mode = "🌐 Public (auto-verify)" if is_public else "🔒 Private (24h pass)"
        await msg.reply_text(
            "✅ FORCE JOIN ADDED\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Chat: {title}\n"
            f"ID: {chat.id}\n"
            f"Mode: {mode}\n"
            f"Link: {invite_link}"
        )

    # ---------- /settings /setting ----------

    async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not _is_admin(user, config):
            await update.effective_message.reply_text(ADMIN_ONLY)
            return
        await update.effective_message.reply_text(SETTINGS_TEXT, reply_markup=_settings_kb())

    async def stg_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        await _edit_or_reply(q, SETTINGS_TEXT, reply_markup=_settings_kb())

    async def stg_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        try:
            await q.message.delete()
        except Exception:
            pass

    async def stg_overview(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        await _edit_or_reply(q, _current_settings_text(), reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data="stg_home")],
        ]))

    async def stg_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not _is_admin(update.effective_user, config):
            await q.answer("Admin only.", show_alert=True)
            return
        await q.answer()
        mode = db_module.get_payment_mode()
        cfg = db_module.get_razorpay_config()
        configured = bool(cfg.get("key_id") and cfg.get("key_secret"))
        text = (
            "💳 PAYMENT SETTINGS\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Active mode: {'⚡ Razorpay' if mode == 'razorpay' else '🧾 Manual QR'}\n\n"
            f"Razorpay Key ID: {_mask_key_id(cfg.get('key_id'))}\n"
            f"Key Secret: {'Configured ✅' if cfg.get('key_secret') else 'Not configured'}\n"
            f"Business Name: {cfg.get('business_name') or 'Not configured'}\n"
            f"Payment Log Group: {db_module.get_payment_log_chat_title() or db_module.get_payment_log_chat_id() or 'Not configured'}\n\n"
            "Razorpay options: UPI QR + Payment Link.\n"
            "Verification: direct Key ID + Secret API polling (NO webhook URL).\n"
            "When Razorpay mode is ON, manual QR/proof submission is disabled for new purchases. "
            "Existing manual pending orders can still be approved."
        )
        await _edit_or_reply(q, text, reply_markup=_payment_kb(mode, configured))

    async def stg_payment_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not _is_admin(update.effective_user, config):
            await q.answer("Admin only.", show_alert=True)
            return
        await q.answer()
        stats = db_module.get_payment_stats()
        text = (
            "📊 PAYMENT STATS\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Total Revenue: ₹{stats['revenue_total']}\n"
            f"📅 Today: ₹{stats['revenue_today']} ({stats['paid_today']} paid)\n"
            f"🗓 This Month: ₹{stats['revenue_month']} ({stats['paid_month']} paid)\n\n"
            f"📦 Total Orders: {stats['total_orders']}\n"
            f"✅ Paid/Approved: {stats['approved']}\n"
            f"⏳ Pending: {stats['pending']}\n"
            f"❌ Rejected: {stats['rejected']}\n"
            f"⚠️ Failed/Expired: {stats['failed']}\n\n"
            "PAYMENT METHODS\n"
            f"🧾 Manual: {stats['manual']}\n"
            f"⚡ Razorpay: {stats['razorpay']}\n\n"
            "Revenue counts approved orders only. Today/month use IST."
        )
        await _edit_or_reply(q, text, reply_markup=_payment_stats_kb())

    async def stg_payment_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not _is_admin(update.effective_user, config):
            await q.answer("Admin only.", show_alert=True)
            return
        await q.answer()
        try:
            page = max(0, int(q.data.split(":", 1)[1]))
        except Exception:
            page = 0
        history = db_module.get_payment_history(page=page, page_size=6)
        items = history.get("items") or []
        total = int(history.get("total", 0) or 0)
        if not items:
            text = "📜 PAYMENT HISTORY\n━━━━━━━━━━━━━━━━━━━━\nNo payment orders found."
        else:
            start_no = page * int(history.get("page_size", 6) or 6) + 1
            lines = [
                "📜 PAYMENT HISTORY",
                "━━━━━━━━━━━━━━━━━━━━",
                f"Total orders: {total} • Page {page + 1}",
                "",
            ]
            for idx, order in enumerate(items, start_no):
                method = "Razorpay" if str(order.get("payment_method", "manual")) == "razorpay" else "Manual"
                lines.extend([
                    f"{idx}. #{order.get('order_id', '-')}",
                    f"   {_status_label(order.get('status'))} • {method}",
                    f"   {_order_label(order)} • ₹{int(order.get('price_amount', 0) or 0)}",
                    f"   User: {order.get('user_id', '-')}",
                    f"   Created: {_fmt_ist(order.get('created_at'))}",
                ])
                if order.get("status") == "approved":
                    lines.append(f"   Paid: {_fmt_ist(order.get('processed_at') or order.get('updated_at'))}")
                if order.get("razorpay_payment_id"):
                    lines.append(f"   Payment ID: {order.get('razorpay_payment_id')}")
                if order.get("razorpay_rrn"):
                    lines.append(f"   RRN/UTR: {order.get('razorpay_rrn')}")
                lines.append("")
            text = "\n".join(lines).strip()
        await _edit_or_reply(
            q,
            text,
            reply_markup=_payment_history_kb(
                page,
                bool(history.get("has_prev")),
                bool(history.get("has_next")),
            ),
        )

    async def stg_pay_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        user = update.effective_user
        if not _is_admin(user, config):
            await q.answer("Admin only.", show_alert=True)
            return
        mode = q.data.split(":", 1)[1]
        if mode == "razorpay" and not db_module.is_razorpay_configured():
            await q.answer("Set Razorpay Key ID and Key Secret first.", show_alert=True)
            return
        db_module.set_payment_mode(mode)
        await q.answer("Payment mode updated.")
        cfg = db_module.get_razorpay_config()
        configured = bool(cfg.get("key_id") and cfg.get("key_secret"))
        text = (
            "💳 PAYMENT SETTINGS\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Active mode: {'⚡ Razorpay' if mode == 'razorpay' else '🧾 Manual QR'}\n\n"
            f"Razorpay Key ID: {_mask_key_id(cfg.get('key_id'))}\n"
            f"Key Secret: {'Configured ✅' if cfg.get('key_secret') else 'Not configured'}\n"
            f"Business Name: {cfg.get('business_name') or 'Not configured'}\n"
            f"Payment Log Group: {db_module.get_payment_log_chat_title() or db_module.get_payment_log_chat_id() or 'Not configured'}\n\n"
            "Razorpay mode provides UPI QR and Payment Link options, both verified without webhooks.\n"
            "Manual QR/proof submission is disabled for new purchases."
        )
        try:
            await q.message.edit_text(text, reply_markup=_payment_kb(mode, configured))
        except Exception:
            await q.message.reply_text(text, reply_markup=_payment_kb(mode, configured))

    async def rzp_keyid_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not _is_admin(update.effective_user, config):
            await q.answer("Admin only.", show_alert=True)
            return ConversationHandler.END
        await q.answer()
        await q.message.reply_text(
            "🔑 SEND RAZORPAY KEY ID\n"
            "Example: rzp_live_xxxxxxxxxx or rzp_test_xxxxxxxxxx\n\n"
            "Send /cancel to cancel."
        )
        return SET_RZP_KEY_ID

    async def rzp_keyid_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update.effective_user, config):
            return ConversationHandler.END
        value = (update.effective_message.text or "").strip()
        if len(value) < 8 or any(ch.isspace() for ch in value):
            await update.effective_message.reply_text("❌ Invalid Key ID. Send the exact Razorpay Key ID.")
            return SET_RZP_KEY_ID
        db_module.set_razorpay_key_id(value)
        await update.effective_message.reply_text("✅ Razorpay Key ID saved.", reply_markup=_settings_kb())
        return ConversationHandler.END

    async def rzp_secret_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not _is_admin(update.effective_user, config):
            await q.answer("Admin only.", show_alert=True)
            return ConversationHandler.END
        if not update.effective_chat or update.effective_chat.type != "private":
            await q.answer("Set Razorpay Key Secret only in the bot's private DM.", show_alert=True)
            return ConversationHandler.END
        await q.answer()
        await q.message.reply_text(
            "🔒 SEND RAZORPAY KEY SECRET\n"
            "The secret will be encrypted before storage and will never be displayed back.\n\n"
            "Send /cancel to cancel."
        )
        return SET_RZP_KEY_SECRET

    async def rzp_secret_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update.effective_user, config):
            return ConversationHandler.END
        if not update.effective_chat or update.effective_chat.type != "private":
            await update.effective_message.reply_text("❌ Send the Razorpay Key Secret only in the bot's private DM.")
            return ConversationHandler.END
        value = (update.effective_message.text or "").strip()
        if len(value) < 8 or any(ch.isspace() for ch in value):
            await update.effective_message.reply_text("❌ Invalid Key Secret. Send the exact Razorpay Key Secret.")
            return SET_RZP_KEY_SECRET
        try:
            db_module.set_razorpay_key_secret(value)
        except RuntimeError as exc:
            logger.error("Could not securely save Razorpay secret: %s", exc)
            await update.effective_message.reply_text(
                "❌ Secure settings encryption is not initialized. Run the bot using ./start.sh once, then try again."
            )
            return SET_RZP_KEY_SECRET
        try:
            await update.effective_message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="✅ Razorpay Key Secret encrypted and saved. It will stay hidden from the settings screen.",
            reply_markup=_settings_kb(),
        )
        return ConversationHandler.END

    async def rzp_name_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not _is_admin(update.effective_user, config):
            await q.answer("Admin only.", show_alert=True)
            return ConversationHandler.END
        await q.answer()
        await q.message.reply_text(
            "🏷 SEND PAYMENT BUSINESS/DISPLAY NAME\n"
            "Example: Team Secret\n\n"
            "This appears in Razorpay QR and Payment Link descriptions."
        )
        return SET_RZP_BUSINESS_NAME

    async def rzp_name_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update.effective_user, config):
            return ConversationHandler.END
        value = (update.effective_message.text or "").strip()
        if len(value) < 2 or len(value) > 80:
            await update.effective_message.reply_text("❌ Business name must be 2-80 characters.")
            return SET_RZP_BUSINESS_NAME
        db_module.set_razorpay_business_name(value)
        await update.effective_message.reply_text("✅ Razorpay business name saved.", reply_markup=_settings_kb())
        return ConversationHandler.END

    async def rzp_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not _is_admin(update.effective_user, config):
            await q.answer("Admin only.", show_alert=True)
            return
        await q.answer("Testing Razorpay...")
        cfg = db_module.get_razorpay_config()
        if not cfg.get("key_id") or not cfg.get("key_secret"):
            await q.message.reply_text("❌ Razorpay credentials are incomplete.")
            return
        try:
            async with httpx.AsyncClient(timeout=15.0, auth=(cfg["key_id"], cfg["key_secret"])) as client:
                qr_resp = await client.get("https://api.razorpay.com/v1/payments/qr_codes", params={"count": 1})
                link_resp = await client.get("https://api.razorpay.com/v1/payment_links", params={"count": 1})
            if qr_resp.status_code == 401 or link_resp.status_code == 401:
                await q.message.reply_text("❌ Razorpay authentication failed. Recheck Key ID and Key Secret.")
                return
            qr_state = "✅ Available" if qr_resp.status_code < 400 else f"⚠️ HTTP {qr_resp.status_code}"
            link_state = "✅ Available" if link_resp.status_code < 400 else f"⚠️ HTTP {link_resp.status_code}"
            await q.message.reply_text(
                "💳 RAZORPAY CONNECTION TEST\n"
                f"UPI QR API: {qr_state}\n"
                f"Payment Links API: {link_state}\n\n"
                "If either API fails, enable that product in your Razorpay account. "
                "These are read-only access checks, not a successful test payment."
            )
        except Exception as exc:
            logger.exception("Razorpay test failed")
            await q.message.reply_text(f"❌ Razorpay test failed: {type(exc).__name__}")

    async def stg_payment_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not _is_admin(update.effective_user, config):
            await q.answer("Admin only.", show_alert=True)
            return
        await q.answer()
        chat_id = db_module.get_payment_log_chat_id()
        title = db_module.get_payment_log_chat_title()
        text = (
            "📢 PAYMENT LOG GROUP\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Current: {title or 'Not configured'}\n"
            f"Chat ID: {chat_id or '-'}\n\n"
            "Razorpay QR creation, successful payments, and manual payment approval/rejection logs are sent here."
        )
        await _edit_or_reply(q, text, reply_markup=_payment_log_kb(bool(chat_id)))

    async def payment_log_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not _is_admin(update.effective_user, config):
            await q.answer("Admin only.", show_alert=True)
            return ConversationHandler.END
        await q.answer()
        await q.message.reply_text(
            "📢 SET PAYMENT LOG GROUP\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Send the group/supergroup chat ID (example -1001234567890), @username, "
            "or forward a message from the target group.\n\n"
            "The bot must be a member and must be able to send messages there.\n"
            "Send /cancel to cancel."
        )
        return SET_PAYMENT_LOG_CHAT

    async def payment_log_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update.effective_user, config):
            return ConversationHandler.END
        msg = update.effective_message
        identifier = None
        fwd_chat = getattr(msg, "forward_from_chat", None)
        if fwd_chat:
            identifier = fwd_chat.id
        else:
            origin = getattr(msg, "forward_origin", None)
            if origin is not None:
                chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
                if chat:
                    identifier = chat.id
        if identifier is None and msg.text:
            identifier = _parse_chat_identifier(msg.text)
        if identifier is None:
            await msg.reply_text("❌ Send a valid group chat ID, @username, or forwarded group message.")
            return SET_PAYMENT_LOG_CHAT
        try:
            chat = await context.bot.get_chat(identifier)
            chat_type = str(getattr(chat, "type", ""))
            if chat_type not in {"group", "supergroup", "channel"}:
                await msg.reply_text("❌ Please select a group, supergroup, or channel.")
                return SET_PAYMENT_LOG_CHAT
            title = getattr(chat, "title", None) or str(chat.id)
            await context.bot.send_message(
                chat_id=chat.id,
                text="✅ Payment log group connected successfully. Future payment events will be logged here.",
            )
        except Exception as exc:
            await msg.reply_text(
                "❌ Could not send to that group. Add the bot to the group and allow it to send messages, then try again.\n"
                f"Error: {type(exc).__name__}"
            )
            return SET_PAYMENT_LOG_CHAT
        db_module.set_payment_log_chat(chat.id, title)
        await msg.reply_text(
            f"✅ Payment log group saved.\nGroup: {title}\nChat ID: {chat.id}",
            reply_markup=_settings_kb(),
        )
        return ConversationHandler.END

    async def payment_log_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not _is_admin(update.effective_user, config):
            await q.answer("Admin only.", show_alert=True)
            return
        chat_id = db_module.get_payment_log_chat_id()
        if not chat_id:
            await q.answer("Payment log group is not configured.", show_alert=True)
            return
        try:
            await context.bot.send_message(chat_id=chat_id, text="🧪 Payment log test successful.")
            await q.answer("Test log sent ✅", show_alert=True)
        except Exception:
            await q.answer("Could not send to payment log group.", show_alert=True)

    async def payment_log_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not _is_admin(update.effective_user, config):
            await q.answer("Admin only.", show_alert=True)
            return
        db_module.clear_payment_log_chat()
        await q.answer("Payment log group removed.")
        await _edit_or_reply(
            q,
            "✅ Payment log group removed.",
            reply_markup=_payment_log_kb(False),
        )

    async def stg_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        current = db_module.get_contact_bot_username()
        text = (
            "🤖 SRC CONTACT BOT\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Current bot: @{current}\n\n"
            "Tap below to update the contact bot username."
            if current else
            "🤖 SRC CONTACT BOT\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Current bot: Not configured\n\n"
            "Tap below to set the contact bot username."
        )
        await _edit_or_reply(q, text, reply_markup=_contact_kb())

    async def contact_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not _is_admin(user, config):
            if update.callback_query:
                await update.callback_query.answer("Admin only.", show_alert=True)
            return ConversationHandler.END
        q = update.callback_query
        await q.answer()
        await q.message.reply_text(
            "🤖 SET SRC CONTACT BOT\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Send the bot username.\n\n"
            "Example: @YourContactBot",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="stg_contact_cancel")]
            ]),
        )
        return SET_CONTACT_INPUT

    async def contact_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not _is_admin(user, config):
            return ConversationHandler.END

        username = (update.effective_message.text or "").strip().lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            await update.effective_message.reply_text("❌ Invalid Telegram username. Send a valid @bot username.")
            return SET_CONTACT_INPUT
        if not username.lower().endswith("bot"):
            await update.effective_message.reply_text("❌ Please provide a bot username ending in 'bot'.")
            return SET_CONTACT_INPUT

        try:
            chat = await context.bot.get_chat(f"@{username}")
            resolved = (getattr(chat, "username", None) or username).lstrip("@")
        except Exception:
            await update.effective_message.reply_text(
                "❌ Could not resolve that bot username. Check it and try again."
            )
            return SET_CONTACT_INPUT

        db_module.set_contact_bot_username(resolved)
        await update.effective_message.reply_text(
            "✅ SRC contact bot updated.\n"
            f"Contact bot: @{resolved}\n\n"
            "The 🧾 Buy SRC button will now open this bot with a pre-written message.",
            reply_markup=_settings_kb(),
        )
        return ConversationHandler.END

    async def contact_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.callback_query:
            await update.callback_query.answer("Cancelled")
            await update.callback_query.message.reply_text("❌ Contact bot update cancelled.", reply_markup=_settings_kb())
        else:
            await update.effective_message.reply_text("❌ Contact bot update cancelled.", reply_markup=_settings_kb())
        return ConversationHandler.END

    async def stg_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        current = db_module.get_welcome_image_file_id()
        text = (
            "🖼 WELCOME IMAGE\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Status: {'Configured' if current else 'Not configured'}\n\n"
            "Upload a photo here and the bot will use it in the welcome message."
        )
        await _edit_or_reply(q, text, reply_markup=_welcome_kb(bool(current)))

    async def welcome_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not _is_admin(user, config):
            if update.callback_query:
                await update.callback_query.answer("Admin only.", show_alert=True)
            return ConversationHandler.END
        q = update.callback_query
        await q.answer()
        await q.message.reply_text(
            "🖼 SET WELCOME IMAGE\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Send the photo you want to use as the welcome image.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="stg_welcome_cancel")]
            ]),
        )
        return SET_WELCOME_IMAGE

    async def welcome_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not _is_admin(user, config):
            return ConversationHandler.END
        message = update.effective_message
        if not message.photo:
            await message.reply_text("❌ Please send a photo image.")
            return SET_WELCOME_IMAGE

        file_id = message.photo[-1].file_id
        db_module.set_welcome_image_file_id(file_id)
        await message.reply_text(
            "✅ Welcome image updated successfully.\n"
            "New users will now see this image in the welcome message.",
            reply_markup=_settings_kb(),
        )
        return ConversationHandler.END

    async def welcome_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        db_module.clear_welcome_image_file_id()
        await _edit_or_reply(
            q,
            "✅ Welcome image removed. The bot will now send text-only welcome messages.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back", callback_data="stg_welcome")],
            ]),
        )

    async def welcome_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.callback_query:
            await update.callback_query.answer("Cancelled")
            await update.callback_query.message.reply_text("❌ Welcome image update cancelled.", reply_markup=_settings_kb())
        else:
            await update.effective_message.reply_text("❌ Welcome image update cancelled.", reply_markup=_settings_kb())
        return ConversationHandler.END

    # ---------- Add Channel conversation ----------

    async def stg_fj(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        await _edit_or_reply(q, FJ_TEXT, reply_markup=_fj_kb())

    async def add_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not _is_admin(user, config):
            if update.callback_query:
                await update.callback_query.answer("Admin only.", show_alert=True)
            else:
                await update.effective_message.reply_text(ADMIN_ONLY)
            return ConversationHandler.END

        text = (
            "➕ ADD FORCE JOIN CHANNEL\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Send one of:\n\n"
            "• Forward a message from that channel/group\n"
            "• @username (public)\n"
            "• Numeric chat ID (e.g. -1001234567890)\n\n"
            "⚠️ Bot must be admin with 'Invite Users' permission."
        )

        if update.callback_query:
            q = update.callback_query
            await q.answer()
            await q.message.reply_text(
                text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="stg_fj_add_cancel")]
                ]),
            )
        else:
            await update.effective_message.reply_text(text)
        return ADD_CHANNEL_INPUT

    async def add_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not _is_admin(user, config):
            return ConversationHandler.END

        msg = update.effective_message
        identifier = None

        fwd_chat = getattr(msg, "forward_from_chat", None)
        if fwd_chat:
            identifier = fwd_chat.id
        else:
            origin = getattr(msg, "forward_origin", None)
            if origin is not None:
                chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
                if chat:
                    identifier = chat.id

        if identifier is None and msg.text:
            identifier = _parse_chat_identifier(msg.text)

        if identifier is None:
            await msg.reply_text(
                "❌ Invalid input. Forward a message or send @username / chat ID."
            )
            return ADD_CHANNEL_INPUT

        await _add_channel(update, context, identifier)
        return ConversationHandler.END

    async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        try:
            await q.message.edit_text("❎ Add channel cancelled.")
        except Exception:
            pass
        return ConversationHandler.END

    async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_text("❎ Cancelled.")
        return ConversationHandler.END

    # ---------- List / Remove ----------

    async def fj_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()

        channels = db_module.list_force_channels()
        if not channels:
            await _edit_or_reply(
                q,
                "📭 No force-join channels configured yet.\n\nTap ➕ Add Channel to add one.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Add Channel", callback_data="stg_fj_add")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="stg_fj")],
                ]),
            )
            return

        lines = [f"📋 FORCE JOIN CHANNELS ({len(channels)})", "━━━━━━━━━━━━━━━━━━━━"]
        keyboard = []

        for i, ch in enumerate(channels, 1):
            title = ch.get("title") or str(ch["chat_id"])
            mode = "🌐 Public" if ch.get("username") else "🔒 Private"
            lines.append(f"{i}. {title}\n   ID: {ch['chat_id']}\n   {mode}")
            keyboard.append([
                InlineKeyboardButton(
                    f"❌ Remove {title[:22]}",
                    callback_data=f"stg_fj_rm:{ch['chat_id']}",
                )
            ])

        keyboard.append([InlineKeyboardButton("➕ Add Channel", callback_data="stg_fj_add")])
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="stg_fj")])

        text = "\n\n".join(lines)
        try:
            await q.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                disable_web_page_preview=True,
            )
        except Exception:
            await q.message.reply_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                disable_web_page_preview=True,
            )

    async def fj_rm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        try:
            chat_id = int(q.data.split(":", 1)[1])
        except Exception:
            return
        if db_module.remove_force_channel(chat_id):
            await q.message.reply_text(f"✅ Removed channel: {chat_id}")
        else:
            await q.message.reply_text("❌ Channel not found.")

    # ---------- Check Status ----------

    async def fj_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer("Checking...")

        channels = db_module.list_force_channels()
        if not channels:
            await _edit_or_reply(
                q,
                "📭 No channels to check.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Back", callback_data="stg_fj")]
                ]),
            )
            return

        bot_id = context.bot.id
        lines = ["🔍 CHANNEL STATUS CHECK", "━━━━━━━━━━━━━━━━━━━━"]

        for i, ch in enumerate(channels, 1):
            chat_id = ch["chat_id"]
            title = ch.get("title") or str(chat_id)
            username = ch.get("username")
            mode = "🌐 Public" if username else "🔒 Private"

            block = [f"{i}. {title}\n   ID: {chat_id}\n   Mode: {mode}"]

            try:
                await context.bot.get_chat(chat_id)
                block.append("   ✅ Bot has access")
            except Exception as exc:
                block.append(f"   ❌ Access failed: {type(exc).__name__}")
                lines.append("\n".join(block))
                continue

            try:
                member = await context.bot.get_chat_member(chat_id, bot_id)
                st = str(getattr(member, "status", "")).lower()
                if st in {"administrator", "creator", "owner"}:
                    block.append("   ✅ Bot is admin")
                else:
                    block.append(f"   ⚠️ Bot status: {st}")
            except Exception as exc:
                block.append(f"   ❌ Admin check failed: {type(exc).__name__}")

            try:
                invite = await context.bot.create_chat_invite_link(
                    chat_id=chat_id,
                    name="LookupBot_Check",
                    creates_join_request=not bool(username),
                )
                block.append("   ✅ Can create invite link")
                try:
                    await context.bot.revoke_chat_invite_link(
                        chat_id=chat_id, invite_link=invite.invite_link
                    )
                except Exception:
                    pass
            except Exception as exc:
                block.append(f"   ❌ Invite link failed: {type(exc).__name__}")

            lines.append("\n".join(block))

        chunks = []
        cur = ""
        for block in lines:
            if cur and len(cur) + len(block) + 2 > 3800:
                chunks.append(cur)
                cur = block
            else:
                cur = f"{cur}\n\n{block}".strip() if cur else block
        if cur:
            chunks.append(cur)

        for idx, chunk in enumerate(chunks):
            is_last = idx == len(chunks) - 1
            markup = (
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Back", callback_data="stg_fj")]
                ])
                if is_last else None
            )
            if idx == 0:
                try:
                    await q.message.edit_text(chunk, reply_markup=markup, disable_web_page_preview=True)
                    continue
                except Exception:
                    pass
            await q.message.reply_text(chunk, reply_markup=markup, disable_web_page_preview=True)

    # ---------- Register ----------

    application.add_handler(CommandHandler("settings", settings_cmd))
    application.add_handler(CommandHandler("setting", settings_cmd))
    application.add_handler(CallbackQueryHandler(stg_home, pattern=r"^stg_home$"))
    application.add_handler(CallbackQueryHandler(stg_close, pattern=r"^stg_close$"))
    application.add_handler(CallbackQueryHandler(stg_fj, pattern=r"^stg_fj$"))
    application.add_handler(CallbackQueryHandler(stg_payment, pattern=r"^stg_payment$"))
    application.add_handler(CallbackQueryHandler(stg_payment_stats, pattern=r"^stg_payment_stats$"))
    application.add_handler(CallbackQueryHandler(stg_payment_history, pattern=r"^stg_payment_history:\d+$"))
    application.add_handler(CallbackQueryHandler(stg_pay_mode, pattern=r"^stg_pay_mode:(manual|razorpay)$"))
    application.add_handler(CallbackQueryHandler(rzp_test, pattern=r"^stg_rzp_test$"))
    application.add_handler(CallbackQueryHandler(stg_payment_log, pattern=r"^stg_payment_log$"))
    application.add_handler(CallbackQueryHandler(payment_log_test, pattern=r"^stg_payment_log_test$"))
    application.add_handler(CallbackQueryHandler(payment_log_clear, pattern=r"^stg_payment_log_clear$"))
    application.add_handler(CallbackQueryHandler(stg_contact, pattern=r"^stg_contact$"))
    application.add_handler(CallbackQueryHandler(stg_welcome, pattern=r"^stg_welcome$"))
    application.add_handler(CallbackQueryHandler(stg_overview, pattern=r"^stg_overview$"))
    application.add_handler(CallbackQueryHandler(welcome_remove, pattern=r"^stg_welcome_remove$"))
    application.add_handler(CallbackQueryHandler(fj_list, pattern=r"^stg_fj_list$"))
    application.add_handler(CallbackQueryHandler(fj_check, pattern=r"^stg_fj_check$"))
    application.add_handler(CallbackQueryHandler(fj_rm_cb, pattern=r"^stg_fj_rm:-?\d+$"))

    add_channel_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_open, pattern=r"^stg_fj_add$")],
        states={
            ADD_CHANNEL_INPUT: [
                MessageHandler(filters.ALL & ~filters.COMMAND, add_receive),
                CallbackQueryHandler(add_cancel, pattern=r"^stg_fj_add_cancel$"),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        allow_reentry=True,
    )
    application.add_handler(add_channel_conv)

    contact_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(contact_open, pattern=r"^stg_contact_set$")],
        states={
            SET_CONTACT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, contact_receive),
                CallbackQueryHandler(contact_cancel, pattern=r"^stg_contact_cancel$"),
            ]
        },
        fallbacks=[CommandHandler("cancel", contact_cancel)],
        allow_reentry=True,
    )
    application.add_handler(contact_conv)

    rzp_keyid_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(rzp_keyid_open, pattern=r"^stg_rzp_keyid_set$")],
        states={SET_RZP_KEY_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, rzp_keyid_receive)]},
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        allow_reentry=True,
    )
    application.add_handler(rzp_keyid_conv)

    rzp_secret_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(rzp_secret_open, pattern=r"^stg_rzp_secret_set$")],
        states={SET_RZP_KEY_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, rzp_secret_receive)]},
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        allow_reentry=True,
    )
    application.add_handler(rzp_secret_conv)

    rzp_name_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(rzp_name_open, pattern=r"^stg_rzp_name_set$")],
        states={SET_RZP_BUSINESS_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, rzp_name_receive)]},
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        allow_reentry=True,
    )
    application.add_handler(rzp_name_conv)

    payment_log_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(payment_log_open, pattern=r"^stg_payment_log_set$")],
        states={SET_PAYMENT_LOG_CHAT: [MessageHandler(filters.ALL & ~filters.COMMAND, payment_log_receive)]},
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        allow_reentry=True,
    )
    application.add_handler(payment_log_conv)

    welcome_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(welcome_open, pattern=r"^stg_welcome_set$")],
        states={
            SET_WELCOME_IMAGE: [
                MessageHandler(filters.PHOTO, welcome_receive),
                MessageHandler((filters.ALL & ~filters.COMMAND), welcome_receive),
                CallbackQueryHandler(welcome_cancel, pattern=r"^stg_welcome_cancel$"),
            ]
        },
        fallbacks=[CommandHandler("cancel", welcome_cancel)],
        allow_reentry=True,
    )
    application.add_handler(welcome_conv)
