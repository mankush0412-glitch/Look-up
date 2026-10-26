import logging
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from functools import wraps

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

_rate_history: dict[int, deque[float]] = defaultdict(deque)


def is_authorized(user_id: int, config, db_module) -> bool:
    if getattr(config, "public_access", False):
        return True
    return user_id in getattr(config, "admin_ids", set()) or db_module.is_dynamic_user(user_id)


async def _is_member(context, chat_id, user_id) -> bool:
    """Return True if the user is currently a member/admin/creator of the chat."""
    try:
        member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        status = str(getattr(member, "status", "")).lower()

        if status in {"member", "administrator", "creator", "owner"}:
            return True
        if status == "restricted":
            return bool(getattr(member, "is_member", False))
        return False
    except Exception as exc:
        # Could be: bot not admin, user never interacted, chat not found.
        logger.debug(
            "get_chat_member failed chat=%s user=%s: %s",
            chat_id, user_id, type(exc).__name__,
        )
        return False


async def ensure_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE, config, db_module) -> bool:
    user = update.effective_user
    if not user:
        return False

    if user.id in getattr(config, "admin_ids", set()):
        return True

    channels = db_module.list_force_channels()
    if not channels:
        return True

    missing = []
    link_map = {}

    for channel in channels:
        chat_id = channel.get("chat_id")
        username = channel.get("username")
        is_public = bool(username)

        # 1) Always try real membership check first (works for both
        #    public AND private as long as the bot is admin there).
        if await _is_member(context, chat_id, user.id):
            continue

        # 2) Not a member (or bot cannot verify). Handle per channel type.
        if is_public:
            # Public channel: no fallback — user must join.
            missing.append(channel)
            continue

        # Private channel: rely on the 24h join-request record from MongoDB.
        if db_module.has_active_join_request(user.id, chat_id):
            continue

        # No membership and no active 24h record → generate a one-time link.
        link = db_module.get_pending_join_link(user.id, chat_id)
        if not link:
            try:
                invite = await context.bot.create_chat_invite_link(
                    chat_id=chat_id,
                    name=f"jr_{user.id}_{chat_id}",
                    creates_join_request=True,
                    expire_date=datetime.now(timezone.utc) + timedelta(hours=2),
                )
                link = invite.invite_link
                db_module.store_pending_join_link(user.id, chat_id, link)
            except Exception as exc:
                logger.warning(
                    "Join-request link gen failed for chat %s: %s",
                    chat_id, type(exc).__name__,
                )
                link = channel.get("invite_link")

        link_map[chat_id] = link
        missing.append(channel)

    if not missing:
        return True

    keyboard = []
    for channel in missing:
        chat_id = channel["chat_id"]
        title = channel.get("title") or "Join Channel"
        link = link_map.get(chat_id) or channel.get("invite_link")
        if link:
            keyboard.append([InlineKeyboardButton(f"➕ Join {title}", url=link)])

    keyboard.append([
        InlineKeyboardButton("✅ I've Joined", callback_data="check_force_join")
    ])

    text = (
        "🔒 JOIN REQUIRED\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "You must join the channel/group(s) below before using this bot.\n\n"
        "After joining, tap “✅ I've Joined”."
    )

    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass
        try:
            await update.callback_query.message.edit_text(
                text, reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception:
            try:
                await update.callback_query.message.reply_text(
                    text, reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except Exception:
                logger.exception("Failed to show join prompt")
    elif update.effective_message:
        try:
            await update.effective_message.reply_text(
                text, reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception:
            logger.exception("Failed to show join prompt")

    return False


def authorized_only(config, db_module):
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user = update.effective_user
            if not user:
                return
            if not await ensure_force_join(update, context, config, db_module):
                return
            if not is_authorized(user.id, config, db_module):
                if update.effective_message:
                    await update.effective_message.reply_text(
                        "⛔ You are not authorized to use this bot."
                    )
                return
            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator


def admin_only(config):
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user = update.effective_user
            if not user or user.id not in getattr(config, "admin_ids", set()):
                if update.effective_message:
                    await update.effective_message.reply_text("⛔ Admin only.")
                return
            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator


def credit_user_only(config, db_module):
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user = update.effective_user
            if not user:
                return
            if not await ensure_force_join(update, context, config, db_module):
                return
            if user.id in getattr(config, "admin_ids", set()):
                return await func(update, context, *args, **kwargs)

            if not db_module.get_user(user.id):
                db_module.add_user(user.id)

            if db_module.get_credits(user.id) <= 0 and not db_module.has_active_bot_pass(user.id):
                if update.callback_query:
                    try:
                        await update.callback_query.answer("Credits finished — view plans to continue.")
                    except Exception:
                        pass
                if update.effective_message:
                    await update.effective_message.reply_text(
                        "💳 LIMIT OVER\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        "Your credits are finished. Buy credits or activate an unlimited plan to continue.",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🛒 Buy Now", callback_data="view_plans")],
                            [InlineKeyboardButton("📋 View Plans", callback_data="view_plans")],
                        ]),
                    )
                return
            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator


def normalize_mobile(value: str) -> str | None:
    value = value.strip()
    value = (
        value.replace(" ", "")
        .replace("-", "")
        .replace("(", "")
        .replace(")", "")
    )
    if value.startswith("+91"):
        value = value[3:]
    elif value.startswith("91") and len(value) == 12:
        value = value[2:]
    if value.startswith("0") and len(value) == 11:
        value = value[1:]
    if not re.fullmatch(r"\d{10}", value):
        return None
    return value


def check_rate_limit(user_id: int, limit: int) -> bool:
    now = time.monotonic()
    q = _rate_history[user_id]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


def mask_mobile(mobile: str) -> str:
    return "*" * max(0, len(mobile) - 4) + mobile[-4:]