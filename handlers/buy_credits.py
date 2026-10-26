import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from io import BytesIO
from urllib.parse import urlencode

import qrcode

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

from .common import authorized_only
from .start import back_menu
from razorpay_payments import (create_payment_qr, create_payment_link, valid_payment_link_url,
                               payment_qr_keyboard, payment_link_keyboard,
                               check_and_process_order, send_api_key_delivery)
from payment_logs import send_payment_log

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
WAITING_CREDIT_AMOUNT, WAITING_PROOF = range(1, 3)


def plans_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Buy Credits — ₹1 = 1", callback_data="plan_credits")],
        [InlineKeyboardButton("⚡ 24h Unlimited — ₹50", callback_data="plan_day")],
        [InlineKeyboardButton("🔑 API 30 Days — ₹300", callback_data="plan_api_terms")],
        [InlineKeyboardButton("📚 API Docs", callback_data="api_docs")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
    ])


def purchase_prompt_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Buy Now", callback_data="view_plans")],
        [InlineKeyboardButton("📋 View Plans", callback_data="view_plans")],
    ])


def _proof_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="buy_cancel")]])


def _admin_keyboard(order_id: str):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"credit_approve:{order_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"credit_reject:{order_id}"),
    ]])


def _as_utc(value):
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _fmt_dt(value):
    """Format a stored datetime for users in India Standard Time."""
    if not value:
        return "-"
    try:
        return _as_utc(value).astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")
    except Exception:
        return str(value)


_ORDER_LABELS = {
    "credits": "Credits",
    "bot_day": "24h Unlimited Bot",
    "api_month": "API — 30 Days",
}

_STATUS_LABELS = {
    "pending": "🟡 Pending",
    "processing": "🟠 Processing",
    "approved": "✅ Approved",
    "rejected": "❌ Rejected",
    "payment_failed": "⚠️ Payment Failed/Expired",
}


def _order_history_text(orders: list[dict]) -> str:
    if not orders:
        return (
            "📦 ORDER HISTORY\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "No purchase orders yet."
        )

    lines = ["📦 ORDER HISTORY", "━━━━━━━━━━━━━━━━━━━━"]
    for order in orders:
        order_type = order.get("order_type", "credits")
        plan = _ORDER_LABELS.get(order_type, order_type.replace("_", " ").title())
        if order_type == "credits":
            credits_amount = int(order.get("credits_amount", 0) or 0)
            if credits_amount:
                plan = f"{credits_amount} Credits"
        status = _STATUS_LABELS.get(order.get("status"), str(order.get("status", "Unknown")).title())
        payment_method = str(order.get("payment_method", "manual") or "manual").lower()
        if payment_method == "razorpay":
            method_label = "Razorpay Link" if order.get("razorpay_payment_link_id") else "Razorpay QR"
        else:
            method_label = "Manual"
        lines.extend([
            "",
            f"#{order.get('order_id', '-')}",
            f"{plan} • ₹{int(order.get('price_amount', 0) or 0)}",
            f"{status} • {method_label}",
            f"{_fmt_dt(order.get('created_at'))}",
        ])
    return "\n".join(lines)


def _history_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="order_history")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
    ])


def _my_api_keyboard(active: bool = False):
    rows = []
    if active:
        rows.append([InlineKeyboardButton("♻️ Regenerate API Key", callback_data="api_key_rotate_confirm")])
    rows.append([InlineKeyboardButton("📚 API Docs", callback_data="api_docs")])
    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def _my_api_text(config, db_module, user_id: int) -> str:
    info = db_module.get_api_plan_info(user_id)
    expires = _as_utc(info.get("expires_at"))
    prefix = info.get("key_prefix") or "-"
    now = datetime.now(timezone.utc)
    active = bool(expires and expires > now)
    base = _public_api_base(config, db_module)
    return (
        "🔑 MY API\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"Status: {'✅ Active' if active else '❌ Inactive'}\n"
        f"Valid until: {_fmt_dt(expires) if expires else '-'}\n"
        f"Key prefix: {(prefix + '…') if prefix != '-' else '-'}\n"
        f"Base URL: {base}\n\n"
        "For security, the full API key is shown only when it is generated/activated."
    )


def _payment_text(config, package: dict) -> str:
    account_name = getattr(config, "payment_account_name", "").strip()
    lines = [
        "💸 MANUAL PAYMENT",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Plan: {package['label']}",
        f"Amount: ₹{package['price']}",
    ]
    if account_name:
        lines.append(f"Payee: {account_name}")
    lines.extend([
        "",
        "Scan the QR and pay the exact amount shown above.",
        "Then send payment screenshot/document or UTR/transaction ID.",
        "Activation happens after admin approval.",
    ])
    return "\n".join(lines)


def _build_upi_qr(config, package: dict) -> BytesIO:
    upi_id = getattr(config, "payment_upi_id", "").strip()
    if not upi_id:
        raise ValueError("PAYMENT_UPI_ID is not configured")

    payee_name = getattr(config, "payment_account_name", "").strip() or "Payment"
    params = {
        "pa": upi_id,
        "pn": payee_name,
        "am": f"{int(package['price'])}.00",
        "cu": "INR",
        "tn": package.get("label", "Bot purchase")[:70],
    }
    upi_uri = "upi://pay?" + urlencode(params)

    qr = qrcode.QRCode(version=None, box_size=10, border=4)
    qr.add_data(upi_uri)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    buffer.name = "payment_qr.png"
    return buffer


async def _send_payment_qr(message, config, package: dict):
    qr_image = _build_upi_qr(config, package)
    await message.reply_photo(
        photo=qr_image,
        caption=_payment_text(config, package),
        reply_markup=_proof_keyboard(),
    )


def _plans_text():
    return (
        "💎 PLANS\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💳 Credits: ₹1 = 1 credit (minimum ₹10)\n"
        "⚡ Bot Unlimited: ₹50 / 24 hours\n"
        "🔑 API Unlimited: ₹300 / 30 days\n\n"
        "API monthly quota is unlimited; technical rate limits and fair-use rules apply."
    )


def _api_terms_text():
    return (
        "📜 API TERMS\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "• Plan valid for 30 days.\n"
        "• Monthly requests unlimited; rate/fair-use limits apply.\n"
        "• Spam, flooding, abuse or excessive parallel requests are not allowed.\n"
        "• API key sharing/reselling is not allowed.\n"
        "• Abuse may cause suspension; terms violations are non-refundable.\n\n"
        "Continue only if you agree."
    )


def _public_api_base(config, db_module):
    configured = (getattr(config, "public_api_url", "") or "").strip().rstrip("/")
    auto_tunnel = bool(getattr(config, "auto_cloudflare_tunnel", False))

    # Production/custom-domain mode: always prefer the configured stable URL.
    if configured and not auto_tunnel:
        return configured

    runtime = db_module.get_runtime_public_api_url()
    if runtime:
        return runtime.rstrip("/")
    if auto_tunnel:
        return "https://API-TUNNEL-STARTING"
    if configured:
        return configured
    return "https://API-URL-NOT-READY"


def _api_docs_parts(config, db_module):
    base = _public_api_base(config, db_module)
    mobile_endpoint = f"{base}/api/lookup"
    id_endpoint = f"{base}/api/id"
    swagger = f"{base}/docs"

    part1 = (
        "📚 API DOCUMENTATION — 1/3\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🌐 BASE URL\n"
        f"{base}\n\n"
        "🔐 AUTHENTICATION\n"
        "Every lookup request must include:\n"
        "X-API-Key: YOUR_API_KEY\n\n"
        "📱 MOBILE LOOKUP\n"
        f"GET {mobile_endpoint}?number=9876543210\n"
        "Parameter: number (10-digit Indian mobile number)\n\n"
        "🆔 ID LOOKUP\n"
        f"GET {id_endpoint}?id=123456789012\n"
        "Parameter: id (exact 12-digit ID)\n\n"
        "Keep your API key private. The full key is shown only when activated or regenerated."
    )

    part2 = (
        "📚 API DOCUMENTATION — 2/3\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "💻 MOBILE cURL\n"
        f"curl -G '{mobile_endpoint}' \\n"
        "  --data-urlencode 'number=9876543210' \\n"
        "  -H 'X-API-Key: YOUR_API_KEY'\n\n"
        "💻 ID cURL\n"
        f"curl -G '{id_endpoint}' \\n"
        "  --data-urlencode 'id=123456789012' \\n"
        "  -H 'X-API-Key: YOUR_API_KEY'\n\n"
        "✅ SUCCESS RESPONSE\n"
        "{\n"
        "  \"success\": true,\n"
        "  \"type\": \"mobile\",\n"
        "  \"query\": \"9876543210\",\n"
        "  \"count\": 1,\n"
        "  \"data\": [...]\n"
        "}"
    )

    part3 = (
        "📚 API DOCUMENTATION — 3/3\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚠️ ERRORS\n"
        "400 — Invalid mobile number / invalid 12-digit ID\n"
        "401 — Missing, invalid, or expired API key\n"
        "404 — No records found\n"
        "429 — Rate limit exceeded\n"
        "502 — Upstream/API processing error\n"
        "503 — Lookup service temporarily unavailable\n\n"
        "♾ PLAN & RATE LIMITS\n"
        "• API plan validity: 30 days from activation/renewal.\n"
        "• Monthly request quota: unlimited.\n"
        "• Technical per-minute limits and fair-use protections apply.\n\n"
        "🔑 KEY MANAGEMENT\n"
        "Use My API to check status/expiry or regenerate a lost key. Regeneration revokes the old key without changing plan expiry.\n\n"
        "🧪 SWAGGER DOCS\n"
        f"{swagger}"
    )

    return [part1, part2, part3]


def _api_docs_keyboard(config, db_module):
    base = _public_api_base(config, db_module)
    rows = []
    if base.startswith("https://") or base.startswith("http://"):
        rows.append([InlineKeyboardButton("🌐 Open Swagger Docs", url=f"{base}/docs")])
    rows.append([InlineKeyboardButton("🔑 My API", callback_data="my_api")])
    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def register_buy_credit_handlers(application, config, db_module):
    async def _start_razorpay_payment(message, context, package: dict, user, method: str):
        """Create exactly one payment instrument for a particular order."""
        order_id = secrets.token_hex(5).upper()
        processing_message = None
        is_link = method == "link"
        kind = "Payment Link" if is_link else "UPI QR"
        try:
            processing_message = await message.reply_text(
                "⏳ PROCESSING PAYMENT\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"Plan: {package['label']}\n"
                f"Amount: ₹{package['price']}\n\n"
                f"🔄 Razorpay {kind} generate ho raha hai..."
            )
        except Exception:
            logger.exception("Could not send Razorpay processing message")

        try:
            db_module.create_plan_order(
                order_id, user.id, package["type"], package["price"], {},
                package.get("credits", 0), payment_method="razorpay",
            )
            if is_link:
                link = await create_payment_link(db_module, order_id, package, user)
                link_id = str(link.get("id") or "")
                url = str(link.get("short_url") or "")
                if not link_id or not valid_payment_link_url(url):
                    raise RuntimeError("Razorpay returned an invalid Payment Link URL or ID")
                db_module.update_razorpay_order(
                    order_id, payment_link_id=link_id, payment_url=url,
                    payment_status=str(link.get("status") or "created"),
                    expire_by=link.get("expire_by"), enforce_reference=True,
                )
                order = db_module.get_credit_order(order_id) or {}
                await send_payment_log(
                    context.bot, db_module, "LINK CREATED", order, user=user,
                    status=str(link.get("status") or "created"),
                )
            else:
                qr = await create_payment_qr(db_module, order_id, package, user)
                qr_id = str(qr.get("id") or "")
                image_url = str(qr.get("image_url") or "")
                if not qr_id or not image_url:
                    raise RuntimeError("Razorpay did not return a QR image")
                db_module.update_razorpay_qr_order(
                    order_id, qr_id=qr_id, image_url=image_url,
                    payment_status=str(qr.get("status") or "active"),
                    close_by=qr.get("close_by"),
                )
                order = db_module.get_credit_order(order_id) or {}
                await send_payment_log(
                    context.bot, db_module, "QR CREATED", order, user=user,
                    qr_id=qr_id, status=str(qr.get("status") or "active"),
                )
        except Exception as exc:
            logger.exception("Failed to create Razorpay %s", kind)
            try:
                db_module.mark_order_payment_failed(order_id, str(exc))
            except Exception:
                pass
            error_text = (
                f"❌ {kind.upper()} FAILED\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"Razorpay {kind} could not be created.\n"
                "Please try again or contact admin."
            )
            if processing_message:
                try:
                    await processing_message.edit_text(error_text, reply_markup=back_menu())
                except Exception:
                    await message.reply_text(error_text, reply_markup=back_menu())
            else:
                await message.reply_text(error_text, reply_markup=back_menu())
            return ConversationHandler.END

        common = (
            f"Order: #{order_id}\n"
            f"Plan: {package['label']}\n"
            f"Amount: ₹{package['price']}\n\n"
            "Payment automatic verify hoga. Screenshot ya admin approval nahi chahiye.\n"
            "Payment karne ke baad ✅ Check Payment dabayein."
        )
        try:
            if is_link:
                await message.reply_text(
                    "💳 RAZORPAY PAYMENT LINK\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    + common
                    + "\n\n👇 Pay Now button se Razorpay checkout kholein.",
                    reply_markup=payment_link_keyboard(order_id, url),
                )
            else:
                caption = (
                    "💳 RAZORPAY UPI QR\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    + common
                    + "\n\nScan QR. QR single-use hai aur expire hota hai."
                )
                try:
                    await message.reply_photo(
                        photo=image_url, caption=caption,
                        reply_markup=payment_qr_keyboard(order_id),
                    )
                except Exception:
                    logger.exception("Telegram could not fetch Razorpay QR image URL")
                    await message.reply_text(
                        caption + f"\n\nQR image: {image_url}",
                        reply_markup=payment_qr_keyboard(order_id),
                        disable_web_page_preview=False,
                    )
        finally:
            if processing_message:
                try:
                    await processing_message.delete()
                except Exception:
                    logger.debug("Could not delete Razorpay processing message", exc_info=True)
        return ConversationHandler.END

    async def _begin_purchase(message, context, package: dict, user):
        if db_module.get_payment_mode() == "razorpay":
            if not db_module.is_razorpay_configured():
                await message.reply_text(
                    "⚠️ Razorpay is selected but not fully configured. Please contact admin.",
                    reply_markup=back_menu(),
                )
                context.user_data.pop("buy_package", None)
                return ConversationHandler.END
            # Keep the choice bound to this user and this plan; old buttons
            # cannot trigger unexpected orders after a different plan is chosen.
            token = secrets.token_hex(4)
            context.user_data["razorpay_checkout"] = {
                "token": token, "user_id": int(user.id), "package": dict(package),
            }
            context.user_data.pop("buy_package", None)
            await message.reply_text(
                "💳 CHOOSE PAYMENT OPTION\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"Plan: {package['label']}\n"
                f"Amount: ₹{package['price']}\n\n"
                "Dono options automatic Razorpay API se verify honge.\n"
                "Koi webhook ya callback URL required nahi.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📱 Pay with UPI QR", callback_data=f"rzp_method:qr:{token}")],
                    [InlineKeyboardButton("🔗 Pay with Payment Link", callback_data=f"rzp_method:link:{token}")],
                    [InlineKeyboardButton("⬅️ Back to Plans", callback_data="view_plans")],
                ]),
            )
            return ConversationHandler.END

        context.user_data.pop("razorpay_checkout", None)
        context.user_data["buy_package"] = package
        try:
            await _send_payment_qr(message, config, package)
        except Exception:
            logger.exception("Failed to generate payment QR")
            await message.reply_text("⚠️ Payment QR could not be generated. Please contact admin.")
            context.user_data.pop("buy_package", None)
            return ConversationHandler.END
        return WAITING_PROOF

    @authorized_only(config, db_module)
    async def razorpay_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q, user = update.callback_query, update.effective_user
        if not q or not user:
            return
        bits = q.data.split(":")
        method, token = bits[1], bits[2]
        checkout = context.user_data.get("razorpay_checkout") or {}
        if (method not in {"qr", "link"} or token != checkout.get("token")
                or int(checkout.get("user_id") or 0) != user.id):
            await q.answer("Payment choice expired. Please select a plan again.", show_alert=True)
            return
        if db_module.get_payment_mode() != "razorpay" or not db_module.is_razorpay_configured():
            context.user_data.pop("razorpay_checkout", None)
            await q.answer("Razorpay is currently unavailable.", show_alert=True)
            return
        package = checkout.get("package") or {}
        order_type = package.get("type")
        amount = package.get("price")
        valid = (
            isinstance(amount, int) and not isinstance(amount, bool)
            and ((order_type == "credits" and 10 <= amount <= 100000
                  and package.get("credits") == amount)
                 or (order_type == "bot_day" and amount == 50)
                 or (order_type == "api_month" and amount == 300))
        )
        if not valid:
            context.user_data.pop("razorpay_checkout", None)
            await q.answer("Invalid plan. Please try again.", show_alert=True)
            return
        # Consume the token before network requests: avoid duplicate link/QR
        # creation when a button is double-clicked.
        context.user_data.pop("razorpay_checkout", None)
        await q.answer("Creating your payment...")
        return await _start_razorpay_payment(q.message, context, package, user, method)

    async def _show_plans(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.callback_query:
            await update.callback_query.answer()
            try:
                await update.callback_query.message.edit_text(_plans_text(), reply_markup=plans_keyboard())
            except Exception:
                await update.callback_query.message.reply_text(_plans_text(), reply_markup=plans_keyboard())
        else:
            await update.effective_message.reply_text(_plans_text(), reply_markup=plans_keyboard())
        return ConversationHandler.END

    @authorized_only(config, db_module)
    async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        return await _show_plans(update, context)

    @authorized_only(config, db_module)
    async def show_plans(update: Update, context: ContextTypes.DEFAULT_TYPE):
        return await _show_plans(update, context)

    @authorized_only(config, db_module)
    async def credit_amount_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        await q.message.edit_text(
            "💳 BUY CREDITS\n━━━━━━━━━━━━━━━━━━━━\n₹1 = 1 credit\nMinimum purchase: ₹10\n\nSend the number of credits you want (example: 25).",
            reply_markup=_proof_keyboard(),
        )
        return WAITING_CREDIT_AMOUNT

    @authorized_only(config, db_module)
    async def receive_credit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (update.effective_message.text or "").strip()
        try:
            amount = int(text)
        except ValueError:
            amount = 0
        if amount < 10 or amount > 100000:
            await update.effective_message.reply_text("Enter a valid credit amount (minimum 10).", reply_markup=_proof_keyboard())
            return WAITING_CREDIT_AMOUNT
        package = {"type": "credits", "credits": amount, "price": amount, "label": f"{amount} Credits"}
        return await _begin_purchase(update.effective_message, context, package, update.effective_user)

    @authorized_only(config, db_module)
    async def select_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        package = {"type": "bot_day", "credits": 0, "price": 50, "label": "24 Hours Unlimited Bot Search"}
        return await _begin_purchase(q.message, context, package, update.effective_user)

    @authorized_only(config, db_module)
    async def api_terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ I Agree & Continue", callback_data="plan_api_accept")],
            [InlineKeyboardButton("❌ Cancel", callback_data="view_plans")],
        ])
        await q.message.edit_text(_api_terms_text(), reply_markup=kb)
        return ConversationHandler.END

    @authorized_only(config, db_module)
    async def select_api(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        package = {"type": "api_month", "credits": 0, "price": 300, "label": "API Unlimited — 30 Days"}
        return await _begin_purchase(q.message, context, package, update.effective_user)

    @authorized_only(config, db_module)
    async def api_docs(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.callback_query:
            await update.callback_query.answer()
            message = update.callback_query.message
        else:
            message = update.effective_message

        base = _public_api_base(config, db_module)
        docs_path = Path(__file__).resolve().parent.parent / "API_DOCUMENTATION.md"
        try:
            docs_text = docs_path.read_text(encoding="utf-8")
        except Exception:
            logger.exception("Failed to read API_DOCUMENTATION.md")
            docs_text = "\n\n".join(_api_docs_parts(config, db_module))

        # Keep the attached markdown aligned with the current public API URL.
        docs_text = docs_text.replace("https://api.teamsecret.in", base)
        payload = BytesIO(docs_text.encode("utf-8"))
        payload.name = "API_DOCUMENTATION.md"

        await message.reply_document(
            document=payload,
            filename="API_DOCUMENTATION.md",
            caption=(
                "📚 Full API documentation attached as Markdown.\n"
                f"🌐 Base URL: {base}\n"
                "🕒 All bot-displayed dates/times use India Standard Time (IST)."
            ),
            reply_markup=_api_docs_keyboard(config, db_module),
        )

    @authorized_only(config, db_module)
    async def order_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user:
            return
        orders = db_module.get_user_orders(user.id, limit=10)
        text = _order_history_text(orders)
        if update.callback_query:
            await update.callback_query.answer()
            try:
                await update.callback_query.message.edit_text(text, reply_markup=_history_keyboard())
            except Exception:
                await update.callback_query.message.reply_text(text, reply_markup=_history_keyboard())
        else:
            await update.effective_message.reply_text(text, reply_markup=_history_keyboard())

    @authorized_only(config, db_module)
    async def my_api(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user:
            return
        info = db_module.get_api_plan_info(user.id)
        expires = _as_utc(info.get("expires_at"))
        active = bool(expires and expires > datetime.now(timezone.utc))
        text = _my_api_text(config, db_module, user.id)
        keyboard = _my_api_keyboard(active)
        if update.callback_query:
            await update.callback_query.answer()
            try:
                await update.callback_query.message.edit_text(text, reply_markup=keyboard)
            except Exception:
                await update.callback_query.message.reply_text(text, reply_markup=keyboard)
        else:
            await update.effective_message.reply_text(text, reply_markup=keyboard)

    @authorized_only(config, db_module)
    async def api_key_rotate_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        user = update.effective_user
        if not q or not user:
            return
        await q.answer()
        info = db_module.get_api_plan_info(user.id)
        expires = _as_utc(info.get("expires_at"))
        if not expires or expires <= datetime.now(timezone.utc):
            await q.message.edit_text(
                "❌ No active API plan found.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]),
            )
            return
        await q.message.edit_text(
            "♻️ REGENERATE API KEY\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Your current API key will stop working immediately.\n"
            "Plan expiry will stay the same.\n\n"
            "Generate a new key?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Generate New Key", callback_data="api_key_rotate_do")],
                [InlineKeyboardButton("❌ Cancel", callback_data="my_api")],
            ]),
        )

    @authorized_only(config, db_module)
    async def api_key_rotate_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        user = update.effective_user
        if not q or not user:
            return
        await q.answer("Generating new key...")
        rotated = db_module.rotate_api_key(user.id)
        if not rotated:
            await q.message.edit_text(
                "❌ Your API plan is not active.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]),
            )
            return
        raw_key, expires = rotated
        base = _public_api_base(config, db_module)

        # Secret key is sent in its own permanent message with no callback buttons.
        await context.bot.send_message(
            user.id,
            "🔑 NEW API KEY\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"{raw_key}\n\n"
            f"Valid until: {_fmt_dt(expires)}\n"
            f"Base URL: {base}\n\n"
            f"Mobile Lookup: {base}/api/lookup?number=9876543210\n"
            f"ID Lookup: {base}/api/id?id=123456789012\n"
            "Header: X-API-Key: YOUR_API_KEY\n\n"
            "⚠️ Your previous API key has been revoked. Save this key securely.\n"
            "Use /apidocs for complete integration documentation.",
        )
        await q.message.edit_text(
            "✅ New API key generated.\nThe key was sent in a separate message above so navigation cannot replace it.",
            reply_markup=_my_api_keyboard(True),
        )

    @authorized_only(config, db_module)
    async def receive_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user, message = update.effective_user, update.effective_message
        package = context.user_data.get("buy_package")
        if not user or not message or not package:
            return ConversationHandler.END
        if db_module.get_payment_mode() != "manual":
            context.user_data.pop("buy_package", None)
            await message.reply_text(
                "⚠️ Manual payment is currently disabled. Open View Plans and use Razorpay.",
                reply_markup=back_menu(),
            )
            return ConversationHandler.END
        if not getattr(config, "payment_upi_id", "").strip():
            await message.reply_text("⚠️ Payment is not configured yet. Please contact admin.")
            return ConversationHandler.END

        if message.photo:
            proof = {"type": "photo", "file_id": message.photo[-1].file_id}
        elif message.document:
            proof = {"type": "document", "file_id": message.document.file_id, "file_name": message.document.file_name}
        elif message.text and len(message.text.strip()) >= 4:
            proof = {"type": "text", "value": message.text.strip()[:300]}
        else:
            await message.reply_text("Send payment screenshot/document or valid UTR/transaction ID.", reply_markup=_proof_keyboard())
            return WAITING_PROOF

        if proof.get("type") == "text" and db_module.is_manual_transaction_used(proof.get("value", "")):
            await message.reply_text(
                "❌ This UTR/transaction ID has already been submitted for another order.\n"
                "Please send the correct unique transaction ID.",
                reply_markup=_proof_keyboard(),
            )
            return WAITING_PROOF

        order_id = secrets.token_hex(5).upper()
        try:
            db_module.create_plan_order(order_id, user.id, package["type"], package["price"], proof, package.get("credits", 0), payment_method="manual")
        except Exception as exc:
            if proof.get("type") == "text" and "duplicate" in str(exc).lower():
                await message.reply_text(
                    "❌ This UTR/transaction ID was already used. Send a different valid UTR.",
                    reply_markup=_proof_keyboard(),
                )
                return WAITING_PROOF
            logger.exception("Failed to create purchase order")
            await message.reply_text("❌ Could not create payment request. Please try again.")
            return ConversationHandler.END

        admin_text = (
            "💰 PURCHASE REQUEST\n━━━━━━━━━━━━━━━━━━━━\n"
            f"Order: #{order_id}\nUser: {user.full_name or user.id}\n"
            f"Username: @{user.username}" if user.username else f"💰 PURCHASE REQUEST\n━━━━━━━━━━━━━━━━━━━━\nOrder: #{order_id}\nUser: {user.full_name or user.id}\nUsername: No username"
        )
        admin_text += f"\nUser ID: {user.id}\nPlan: {package['label']}\nAmount: ₹{package['price']}\nProof: {proof['type']}"
        if proof["type"] == "text":
            admin_text += f"\nUTR/Txn: {proof['value']}"

        delivered = 0
        for admin_id in getattr(config, "admin_ids", set()):
            try:
                if proof["type"] in {"photo", "document"}:
                    await context.bot.copy_message(chat_id=admin_id, from_chat_id=message.chat_id, message_id=message.message_id)
                await context.bot.send_message(chat_id=admin_id, text=admin_text, reply_markup=_admin_keyboard(order_id))
                delivered += 1
            except Exception:
                logger.exception("Admin notification failed")
        try:
            await send_payment_log(
                context.bot, db_module, "MANUAL PROOF SUBMITTED",
                db_module.get_credit_order(order_id) or {}, user=user, status="pending",
                note=(proof.get("value") if proof.get("type") == "text" else proof.get("type")),
            )
        except Exception:
            logger.exception("Payment log failed for manual order %s", order_id)

        context.user_data.pop("buy_package", None)
        await message.reply_text(
            f"✅ Payment proof submitted.\nOrder ID: #{order_id}\nStatus: Pending admin approval." if delivered else f"⚠️ Order #{order_id} saved, but admin notification failed.",
            reply_markup=back_menu(),
        )
        return ConversationHandler.END

    @authorized_only(config, db_module)
    async def razorpay_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        user = update.effective_user
        if not q or not user:
            return
        order_id = q.data.split(":", 1)[1]
        order = db_module.get_credit_order(order_id)
        if not order or int(order.get("user_id", 0) or 0) != user.id:
            await q.answer("Order not found.", show_alert=True)
            return
        if str(order.get("payment_method", "")) != "razorpay":
            await q.answer("Not a Razorpay order.", show_alert=True)
            return
        if order.get("status") == "approved":
            await q.answer("Payment already verified and activated.", show_alert=True)
            return
        if order.get("status") not in {"pending", "processing"}:
            await q.answer("This payment is no longer pending.", show_alert=True)
            return

        await q.answer("Checking payment...")
        status, activated = await check_and_process_order(context.application, config, db_module, order)
        if status == "paid":
            if activated:
                await q.message.reply_text("✅ Payment verified. Your purchase has been activated.", reply_markup=back_menu())
            else:
                await q.message.reply_text("✅ Payment is paid. Activation is already processing or completed.", reply_markup=back_menu())
        elif status in {"cancelled", "expired"}:
            await q.message.reply_text(f"❌ Razorpay payment is {status}. Please create a new order. If money was debited, contact support with the order ID.", reply_markup=back_menu())
        elif status == "settlement_pending":
            await q.message.reply_text("⌛ Payment settlement is pending or the checkout expired recently. Bot is still verifying directly with Razorpay.")
        elif status in {"mismatch", "duplicate"}:
            await q.message.reply_text("⚠️ Payment details could not be safely matched to this order. Contact support with the order ID; do not pay again before confirming.")
        elif status == "error":
            await q.message.reply_text("⚠️ Could not verify payment right now. Please try again shortly.")
        else:
            await q.message.reply_text(
                f"⏳ Payment not confirmed yet. Razorpay status: {status or 'pending'}.\n"
                "If you just paid, wait a few seconds and check again."
            )

    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.pop("buy_package", None)
        context.user_data.pop("razorpay_checkout", None)
        if update.callback_query:
            await update.callback_query.answer("Cancelled")
            await update.callback_query.message.reply_text("❌ Purchase cancelled.", reply_markup=back_menu())
        else:
            await update.effective_message.reply_text("❌ Purchase cancelled.", reply_markup=back_menu())
        return ConversationHandler.END

    async def admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q, admin = update.callback_query, update.effective_user
        if not q or not admin or admin.id not in getattr(config, "admin_ids", set()):
            if q: await q.answer("Admin only.", show_alert=True)
            return
        action, order_id = q.data.split(":", 1)
        order = db_module.get_credit_order(order_id)
        if not order:
            await q.answer("Order not found.", show_alert=True)
            return
        status = str(order.get("status") or "")
        if action == "credit_approve" and status not in {"pending", "processing"}:
            await q.answer("Order already processed.", show_alert=True)
            return
        if action == "credit_reject" and status != "pending":
            await q.answer("Order is already processing or processed.", show_alert=True)
            return
        if str(order.get("payment_method", "manual")) == "razorpay":
            await q.answer("Razorpay orders are verified automatically and cannot be manually approved.", show_alert=True)
            return

        if action == "credit_reject":
            rejected = db_module.finalize_credit_order(order_id, "rejected", admin.id)
            if not rejected:
                await q.answer("Already processed.", show_alert=True); return
            await q.answer("Rejected")
            await q.message.edit_text(f"❌ ORDER REJECTED\nOrder: #{order_id}\nUser ID: {rejected['user_id']}")
            try: await context.bot.send_message(rejected["user_id"], f"❌ Payment rejected.\nOrder: #{order_id}", reply_markup=back_menu())
            except Exception: pass
            try:
                await send_payment_log(context.bot, db_module, "MANUAL REJECTED", rejected, status="rejected", admin_id=admin.id)
            except Exception:
                logger.exception("Payment reject log failed")
            return

        claimed = db_module.claim_credit_order(order_id, admin.id)
        if not claimed:
            await q.answer("Already being processed.", show_alert=True); return
        api_order = False
        try:
            order_type = claimed.get("order_type", "credits")
            if order_type == "credits":
                result, _ = db_module.apply_credit_order(
                    claimed["user_id"], claimed.get("credits_amount", 0), order_id
                )
                user_text = f"✅ PAYMENT APPROVED\nOrder: #{order_id}\nCredits added: +{claimed.get('credits_amount', 0)}\nNew balance: {result}"
                admin_detail = f"Credits: +{claimed.get('credits_amount', 0)} | Balance: {result}"
            elif order_type == "bot_day":
                expires, _ = db_module.activate_bot_pass_for_order(claimed["user_id"], order_id, 24)
                user_text = f"✅ 24-HOUR UNLIMITED ACTIVATED\nValid until: {_fmt_dt(expires)}\nCredits will not be deducted while pass is active."
                admin_detail = f"Bot pass until: {_fmt_dt(expires)}"
            elif order_type == "api_month":
                _, expires, _ = db_module.activate_api_plan_for_order(claimed["user_id"], order_id, 30)
                db_module.mark_order_key_delivery(order_id, "pending")
                api_order = True
                user_text = "✅ API plan activated. Your API key will be sent in a separate message."
                admin_detail = f"API plan until: {_fmt_dt(expires)}"
            else:
                raise ValueError("Unknown order type")
            finalized = db_module.finalize_credit_order(order_id, "approved", admin.id)
            if not finalized:
                existing = db_module.get_credit_order(order_id)
                if not existing or existing.get("status") != "approved":
                    raise RuntimeError("Could not finalize order")
        except Exception:
            db_module.reset_credit_order_pending(order_id, admin.id)
            logger.exception("Approval failed")
            await q.answer("Approval failed; order restored.", show_alert=True)
            return

        await q.answer("Approved")
        await q.message.edit_text(f"✅ ORDER APPROVED\nOrder: #{order_id}\nUser ID: {claimed['user_id']}\n{admin_detail}")
        try:
            if api_order:
                delivered = await send_api_key_delivery(context.application, config, db_module, order_id)
                if not delivered:
                    await context.bot.send_message(
                        claimed["user_id"],
                        "✅ API plan is active, but the key message could not be delivered right now. "
                        "The bot will retry automatically.",
                    )
            else:
                await context.bot.send_message(claimed["user_id"], user_text, reply_markup=back_menu())
        except Exception:
            logger.warning("Could not notify approved user")
        try:
            await send_payment_log(
                context.bot, db_module, "MANUAL APPROVED",
                db_module.get_credit_order(order_id) or claimed, status="approved", admin_id=admin.id,
            )
        except Exception:
            logger.exception("Payment approval log failed")

    application.add_handler(CommandHandler("buycredits", buy_command))
    application.add_handler(CommandHandler("plans", show_plans))
    application.add_handler(CommandHandler("orders", order_history))
    application.add_handler(CommandHandler("myapi", my_api))
    application.add_handler(CommandHandler("apidocs", api_docs))
    application.add_handler(CallbackQueryHandler(order_history, pattern=r"^order_history$"))
    application.add_handler(CallbackQueryHandler(my_api, pattern=r"^my_api$"))
    application.add_handler(CallbackQueryHandler(api_key_rotate_confirm, pattern=r"^api_key_rotate_confirm$"))
    application.add_handler(CallbackQueryHandler(api_key_rotate_do, pattern=r"^api_key_rotate_do$"))
    application.add_handler(CallbackQueryHandler(show_plans, pattern=r"^(buy_credits|view_plans)$"))
    application.add_handler(CallbackQueryHandler(api_terms, pattern=r"^plan_api_terms$"))
    application.add_handler(CallbackQueryHandler(api_docs, pattern=r"^api_docs$"))
    application.add_handler(CallbackQueryHandler(razorpay_check, pattern=r"^rzp_check:[A-F0-9]{10}$"))
    application.add_handler(CallbackQueryHandler(razorpay_method, pattern=r"^rzp_method:(qr|link):[a-f0-9]{8}$"))

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(credit_amount_entry, pattern=r"^plan_credits$"),
            CallbackQueryHandler(select_day, pattern=r"^plan_day$"),
            CallbackQueryHandler(select_api, pattern=r"^plan_api_accept$"),
        ],
        states={
            WAITING_CREDIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_credit_amount), CallbackQueryHandler(cancel, pattern=r"^buy_cancel$")],
            WAITING_PROOF: [MessageHandler((filters.PHOTO | filters.Document.ALL | filters.TEXT) & ~filters.COMMAND, receive_proof), CallbackQueryHandler(cancel, pattern=r"^buy_cancel$")],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern=r"^buy_cancel$")],
        allow_reentry=True,
    )
    application.add_handler(conv)
    application.add_handler(CallbackQueryHandler(admin_action, pattern=r"^credit_(approve|reject):[A-F0-9]{10}$"))
