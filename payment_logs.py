import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def _fmt_now() -> str:
    return datetime.now(timezone.utc).astimezone(IST).strftime("%d %b %Y, %I:%M:%S %p IST")


def _plan_label(order: dict) -> str:
    order_type = str(order.get("order_type", "credits"))
    if order_type == "credits":
        return f"{int(order.get('credits_amount', 0) or 0)} Credits"
    if order_type == "bot_day":
        return "24 Hours Unlimited Bot"
    if order_type == "api_month":
        return "API Unlimited — 30 Days"
    return order_type


async def send_payment_log(bot, db_module, event: str, order: dict, user=None, **extra) -> bool:
    chat_id = db_module.get_payment_log_chat_id()
    if not chat_id:
        return False

    user_id = int(order.get("user_id", 0) or 0)
    full_name = ""
    username = ""
    if user is not None:
        full_name = getattr(user, "full_name", None) or getattr(user, "first_name", None) or ""
        username = getattr(user, "username", None) or ""
    if not full_name and user_id:
        try:
            chat = await bot.get_chat(user_id)
            full_name = getattr(chat, "full_name", None) or getattr(chat, "first_name", None) or ""
            username = getattr(chat, "username", None) or ""
        except Exception:
            pass

    lines = [
        f"💳 PAYMENT LOG — {event}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Order: #{order.get('order_id', '-')}",
        f"User: {full_name or user_id or '-'}",
        f"Username: @{username}" if username else "Username: No username",
        f"User ID: {user_id or '-'}",
        f"Plan: {_plan_label(order)}",
        f"Amount: ₹{int(order.get('price_amount', 0) or 0)}",
        f"Method: {str(order.get('payment_method', 'manual')).title()}",
        f"Time: {_fmt_now()}",
    ]

    payment_id = extra.get("payment_id") or order.get("razorpay_payment_id")
    rrn = extra.get("rrn") or order.get("razorpay_rrn")
    status = extra.get("status")
    admin_id = extra.get("admin_id")
    note = extra.get("note")
    qr_id = extra.get("qr_id") or order.get("razorpay_qr_id")

    if qr_id:
        lines.append(f"QR ID: {qr_id}")
    link_id = order.get("razorpay_payment_link_id")
    if link_id:
        lines.append(f"Payment Link ID: {link_id}")
    if payment_id:
        lines.append(f"Payment ID: {payment_id}")
    if rrn:
        lines.append(f"RRN/UTR: {rrn}")
    if status:
        lines.append(f"Status: {status}")
    if admin_id:
        lines.append(f"Admin ID: {admin_id}")
    if note:
        lines.append(f"Note: {str(note)[:500]}")

    try:
        await bot.send_message(chat_id=chat_id, text="\n".join(lines))
        return True
    except Exception:
        logger.exception("Failed to send payment log to chat %s", chat_id)
        return False
