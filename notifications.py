import asyncio
import logging
from datetime import timezone
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)
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
    return _as_utc(value).astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")


def _renew_keyboard(kind: str):
    rows = [[InlineKeyboardButton("💎 View Plans", callback_data="view_plans")]]
    if kind.startswith("api_"):
        rows.insert(0, [InlineKeyboardButton("🔑 My API", callback_data="my_api")])
    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def _message_for(event: dict) -> str:
    kind = event.get("kind")
    expires = _fmt_dt(event.get("expires_at"))
    if kind == "api_3d":
        return (
            "⏳ API PLAN EXPIRY REMINDER\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Your API plan will expire within 3 days.\n"
            f"Valid until: {expires}\n\n"
            "Renew before expiry to avoid interruption."
        )
    if kind == "api_1d":
        return (
            "⚠️ API PLAN EXPIRES SOON\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Your API plan will expire within 24 hours.\n"
            f"Valid until: {expires}\n\n"
            "Renew now if you want uninterrupted API access."
        )
    if kind == "api_expired":
        return (
            "❌ API PLAN EXPIRED\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Expired at: {expires}\n\n"
            "Your API key is no longer accepted for lookups. Renew the API plan to continue."
        )
    if kind == "bot_expired":
        return (
            "⌛ 24-HOUR UNLIMITED PASS EXPIRED\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Expired at: {expires}\n\n"
            "Normal credit deduction is active again."
        )
    return "Your plan status has changed."


async def _run_once(application, db_module):
    events = db_module.get_due_plan_notifications()
    for event in events:
        try:
            await application.bot.send_message(
                chat_id=event["user_id"],
                text=_message_for(event),
                reply_markup=_renew_keyboard(event.get("kind", "")),
            )
        except Exception as exc:
            logger.warning(
                "Plan notification failed for user %s (%s): %s",
                event.get("user_id"), event.get("kind"), exc,
            )
            continue
        try:
            db_module.mark_plan_notification(
                event["user_id"], event["kind"], event["expires_at"]
            )
        except Exception:
            logger.exception("Could not mark plan notification as sent")


async def plan_notification_worker(application, db_module, interval_seconds: int = 900, owner_id: str = "bot"):
    """Check paid-plan notifications with a MongoDB lease to avoid duplicate sends."""
    await asyncio.sleep(15)
    while True:
        try:
            lease_ttl = max(120, min(int(interval_seconds), 900))
            if db_module.acquire_worker_lease("plan-notification-worker", owner_id, ttl_seconds=lease_ttl):
                await _run_once(application, db_module)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Plan notification worker iteration failed")
        await asyncio.sleep(max(60, int(interval_seconds)))
