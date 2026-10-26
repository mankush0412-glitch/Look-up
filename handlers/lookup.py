import logging
import re
from collections import deque
from datetime import datetime
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

from .common import credit_user_only, check_rate_limit, mask_mobile, normalize_mobile
from .start import HELP_TEXT, WELCOME_TEXT, main_menu

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

WAITING_MOBILE = 1

# Safety caps
MAX_LOOKUP_ITERATIONS = 20      # max BFS steps (mobile + id lookups combined)
MAX_RECORDS_RETURNED = 50       # max records returned to user
MAX_ALT_TOKENS_PER_RECORD = 5   # sanity cap per record


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _safe(value):
    if value is None or value == "":
        return "Not available"
    return str(value).strip()




def _limit_purchase_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Buy Now", callback_data="view_plans")],
        [InlineKeyboardButton("📋 View Plans", callback_data="view_plans")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
    ])

def _result_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Search Another Number", callback_data="search_mobile")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
    ])


def _split_multi(value) -> list[str]:
    """Split a value like '999,888 777/666' into tokens."""
    if value is None:
        return []
    parts = re.split(r"[,\s/|;]+", str(value))
    out = []
    for p in parts:
        p = p.strip()
        if p:
            out.append(p)
    return out


def _extract_alts_and_ids(record: dict):
    """Extract alternate numbers and IDs from a record."""
    alts = set()
    ids = set()

    # alt field — may contain one or many numbers
    for token in _split_multi(record.get("alt"))[:MAX_ALT_TOKENS_PER_RECORD]:
        alts.add(token)

    # id field — usually a 12-digit numeric string
    id_val = record.get("id")
    if id_val is not None:
        id_str = str(id_val).strip()
        if id_str.isdigit():
            ids.add(id_str)

    return alts, ids


def _format_record(record, index, total):
    return (
        f"📄 RECORD {index} OF {total}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Name: {_safe(record.get('name'))}\n"
        f"📱 Mobile: {_safe(record.get('mobile'))}\n"
        f"👨 Father Name: {_safe(record.get('fname'))}\n"
        f"📍 Address: {_safe(record.get('address'))}\n"
        f"☎️ Alternate: {_safe(record.get('alt'))}\n"
        f"🌐 Circle: {_safe(record.get('circle'))}\n"
        f"🆔 ID: {_safe(record.get('id'))}"
    )


# ------------------------------------------------------------------
# BFS-style expanded lookup (used by both /lookup and /id)
# ------------------------------------------------------------------

async def _expanded_lookup_from_seed(api_client, seed: tuple[str, str]):
    """
    Breadth-first search:
        seed -> records -> (alt numbers + 12-digit IDs) -> their records -> ...

    seed is ("mobile", "9876543210") or ("id", "123456789012")

    Returns:
        all_records : list[dict]
        searched    : {"mobiles": [...], "ids": [...]}
        final_status: "ok" | "not_found" | "timeout" | "auth_failed" | "unavailable" | "error"
    """
    seen_mobiles: set[str] = set()
    seen_ids: set[str] = set()
    seen_record_keys: set[tuple[str, str]] = set()

    all_records: list[dict] = []
    searched = {"mobiles": [], "ids": []}

    queue: deque[tuple[str, str]] = deque([seed])

    first_error_status: str | None = None
    iterations = 0

    while queue and iterations < MAX_LOOKUP_ITERATIONS:
        iterations += 1
        kind, value = queue.popleft()

        if kind == "mobile":
            if value in seen_mobiles:
                continue
            seen_mobiles.add(value)
            searched["mobiles"].append(value)
            result = await api_client.lookup_mobile(value)
        else:  # "id"
            if value in seen_ids:
                continue
            seen_ids.add(value)
            searched["ids"].append(value)
            result = await api_client.lookup_id(value)

        status = result.get("status")

        if status == "ok":
            pass
        elif status == "not_found":
            continue
        else:
            # Record first real error (timeout / auth / unavailable / error)
            if first_error_status is None:
                first_error_status = status
            continue

        data = result.get("data") or {}
        records = data.get("results")
        if not isinstance(records, list) or not records:
            records = [data]

        for rec in records:
            if not isinstance(rec, dict):
                continue

            rid = str(rec.get("id") or "").strip()
            rmob = str(rec.get("mobile") or "").strip()
            key = (rid, rmob)
            if key in seen_record_keys:
                continue
            seen_record_keys.add(key)
            all_records.append(rec)

            if len(all_records) >= MAX_RECORDS_RETURNED:
                queue.clear()
                break

            alts, ids = _extract_alts_and_ids(rec)

            for alt in alts:
                norm = normalize_mobile(alt)
                if norm and norm not in seen_mobiles:
                    queue.append(("mobile", norm))

            for iid in ids:
                if len(iid) == 12 and iid not in seen_ids:
                    queue.append(("id", iid))

    if all_records:
        final_status = "ok"
    elif first_error_status:
        final_status = first_error_status
    else:
        final_status = "not_found"

    return all_records, searched, final_status


# ------------------------------------------------------------------
# Audit log + result sender
# ------------------------------------------------------------------

async def _send_search_log(context, config, user, query, status, count=None, searched=None):
    if not getattr(config, "log_chat_id", None):
        return

    username = f"@{user.username}" if getattr(user, "username", None) else "No username"
    full_name = getattr(user, "full_name", None) or "Unknown"
    now = datetime.now(IST).strftime("%d %b %Y, %I:%M:%S %p IST")

    status_map = {
        "ok": "✅ Found",
        "not_found": "❌ Not Found",
        "auth_failed": "🔐 API Auth Failed",
        "timeout": "⏱ Timeout",
        "unavailable": "🛠 API Unavailable",
    }
    pretty_status = status_map.get(status, f"⚠️ {status}")

    extra = f"\n📚 Records: {count}" if count is not None else ""

    expansion_info = ""
    if searched:
        mobiles = searched.get("mobiles") or []
        ids = searched.get("ids") or []
        if len(mobiles) > 1 or ids:
            expansion_info = f"\n🔁 Expanded: {len(mobiles)} mobiles, {len(ids)} IDs"

    msg = (
        "🔎 SEARCH LOG\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 User: {full_name}\n"
        f"🔗 Username: {username}\n"
        f"🆔 Telegram ID: {user.id}\n"
        f"📱 Searched: {query}\n"
        f"📌 Result: {pretty_status}"
        f"{extra}"
        f"{expansion_info}\n"
        f"🕒 Time: {now}"
    )

    try:
        await context.bot.send_message(
            chat_id=config.log_chat_id,
            text=msg,
            disable_web_page_preview=True,
        )
    except Exception:
        logger.exception("Failed to send search log")


async def _send_results(update, records, header_line, is_admin, db_module, user):
    total = len(records)

    if is_admin:
        credit_line = "\n💳 Credits: Unlimited (Admin)"
    else:
        credit_line = f"\n💳 Credits left: {db_module.get_credits(user.id)}"

    await update.effective_message.reply_text(
        f"✅ MATCH FOUND\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{header_line}\n"
        f"📚 Records found: {total}"
        f"{credit_line}"
    )

    chunks = []
    current = ""

    for index, record in enumerate(records, start=1):
        block = _format_record(record, index, total)
        if current and len(current) + len(block) + 2 > 3800:
            chunks.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}".strip() if current else block

    if current:
        chunks.append(current)

    for i, chunk in enumerate(chunks):
        markup = _result_keyboard() if i == len(chunks) - 1 else None
        await update.effective_message.reply_text(
            chunk,
            reply_markup=markup,
            disable_web_page_preview=True,
        )


# ------------------------------------------------------------------
# Mobile lookup flow
# ------------------------------------------------------------------

async def _perform_lookup(update, context, raw_mobile, config, db_module, api_client):
    user = update.effective_user
    if not user:
        return

    mobile = normalize_mobile(raw_mobile)
    if not mobile:
        await update.effective_message.reply_text(
            "❌ INVALID NUMBER\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Please send a valid 10-digit Indian mobile number.\n\n"
            "Example: 9876543210",
            reply_markup=_result_keyboard(),
        )
        return

    if not check_rate_limit(user.id, config.rate_limit_per_minute):
        await update.effective_message.reply_text(
            "⏳ RATE LIMIT REACHED\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Too many searches in a short time.\n"
            "Please wait a little and try again.",
            reply_markup=_result_keyboard(),
        )
        return

    is_admin = user.id in getattr(config, "admin_ids", [])
    credit_reserved = False

    if not is_admin and not db_module.has_active_bot_pass(user.id):
        credit_reserved = db_module.consume_credit(user.id, 1)
        if not credit_reserved:
            await update.effective_message.reply_text(
                "💳 LIMIT OVER\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Your credits are finished. Buy credits or activate an unlimited plan to continue.",
                reply_markup=_limit_purchase_keyboard(),
            )
            return

    logger.info(
        "Expanded lookup telegram_id=%s mobile=%s",
        user.id, mask_mobile(mobile),
    )

    loading = await update.effective_message.reply_text(
        "🔍 SEARCHING DATABASE...\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"Number: {mask_mobile(mobile)}\n"
        "Fetching related records (alternate numbers + IDs)\n"
        "Please wait."
    )

    try:
        records, searched, status = await _expanded_lookup_from_seed(
            api_client, ("mobile", mobile)
        )
    except Exception:
        logger.exception("Expanded lookup crashed")
        records, searched, status = [], {"mobiles": [mobile], "ids": []}, "error"

    # Refund on hard failures
    refundable = {"auth_failed", "timeout", "unavailable", "error"}
    if credit_reserved and status in refundable:
        db_module.add_credits(user.id, 1)
        credit_reserved = False

    # Audit log
    try:
        await _send_search_log(
            context, config, user, mobile, status,
            count=(len(records) if status == "ok" else None),
            searched=searched,
        )
    except Exception:
        logger.exception("Failed to send search log")

    try:
        await loading.delete()
    except Exception:
        pass

    # ---- Response ----

    if status == "ok" and records:
        expansion_note = ""
        extra_mobiles = max(0, len(searched.get("mobiles", [])) - 1)
        extra_ids = len(searched.get("ids", []))
        if extra_mobiles or extra_ids:
            expansion_note = (
                f"\n🔁 Also searched: {extra_mobiles} alt mobile(s), "
                f"{extra_ids} ID(s)"
            )

        await _send_results(
            update,
            records,
            header_line=f"📱 Number: {mobile}{expansion_note}",
            is_admin=is_admin,
            db_module=db_module,
            user=user,
        )
        return

    if status == "not_found":
        await update.effective_message.reply_text(
            "🔎 NO RECORD FOUND\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"No matching record was found for {mobile}.",
            reply_markup=_result_keyboard(),
        )
    elif status == "auth_failed":
        await update.effective_message.reply_text(
            "🔐 SERVICE AUTHENTICATION ERROR\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "The lookup service could not authenticate with the database API.\n"
            "Please contact the administrator."
        )
    elif status == "timeout":
        await update.effective_message.reply_text(
            "⏱ REQUEST TIMED OUT\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "The database took too long to respond.\n"
            "Please try again.",
            reply_markup=_result_keyboard(),
        )
    elif status == "unavailable":
        await update.effective_message.reply_text(
            "🛠 SERVICE TEMPORARILY UNAVAILABLE\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "The database server is currently unreachable.\n"
            "Please try again later."
        )
    else:
        await update.effective_message.reply_text(
            "⚠️ LOOKUP FAILED\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Something went wrong while processing the request.\n"
            "Please try again.",
            reply_markup=_result_keyboard(),
        )


# ------------------------------------------------------------------
# Register handlers
# ------------------------------------------------------------------

def register_lookup_handlers(application, config, db_module, api_client):

    @credit_user_only(config, db_module)
    async def lookup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.effective_message.reply_text(
                "🔎 MOBILE LOOKUP\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Use:\n"
                "/lookup 9876543210",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "🔎 Search Mobile Number",
                        callback_data="search_mobile",
                    )]
                ]),
            )
            return

        await _perform_lookup(
            update, context, context.args[0],
            config, db_module, api_client,
        )

    @credit_user_only(config, db_module)
    async def search_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        await query.message.reply_text(
            "🔎 ENTER MOBILE NUMBER\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Send a valid 10-digit mobile number.\n\n"
            "Example: 9876543210",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "❌ Cancel Search",
                    callback_data="cancel_search",
                )]
            ]),
        )
        return WAITING_MOBILE

    @credit_user_only(config, db_module)
    async def receive_mobile(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await _perform_lookup(
            update, context, update.message.text,
            config, db_module, api_client,
        )
        return ConversationHandler.END

    @credit_user_only(config, db_module)
    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.callback_query:
            await update.callback_query.answer()
            message = update.callback_query.message
        else:
            message = update.effective_message

        await message.reply_text(
            "❎ SEARCH CANCELLED",
            reply_markup=main_menu(),
        )
        return ConversationHandler.END

    @credit_user_only(config, db_module)
    async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.effective_message.reply_text(
                "🆔 INTERNAL ID LOOKUP\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Use:\n"
                "/id 123456789012\n\n"
                "Only an exact 12-digit ID is accepted."
            )
            return

        record_id = context.args[0].strip()

        if not record_id.isdigit() or len(record_id) != 12:
            await update.effective_message.reply_text(
                "❌ INVALID ID\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Please enter exactly 12 digits.\n"
                "Partial IDs and wildcards are not accepted."
            )
            return

        user = update.effective_user
        if not user:
            return

        if not check_rate_limit(user.id, config.rate_limit_per_minute):
            await update.effective_message.reply_text(
                "⏳ RATE LIMIT REACHED\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Too many searches in a short time.\n"
                "Please wait and try again."
            )
            return

        is_admin = user.id in getattr(config, "admin_ids", [])
        credit_reserved = False

        if not is_admin and not db_module.has_active_bot_pass(user.id):
            credit_reserved = db_module.consume_credit(user.id, 1)
            if not credit_reserved:
                await update.effective_message.reply_text(
                    "💳 LIMIT OVER\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "Your credits are finished. Buy credits or activate an unlimited plan to continue.",
                    reply_markup=_limit_purchase_keyboard(),
                )
                return

        logger.info(
            "ID lookup telegram_id=%s id=********%s",
            user.id, record_id[-4:],
        )

        loading = await update.effective_message.reply_text(
            "🔍 SEARCHING DATABASE...\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"ID: ********{record_id[-4:]}\n"
            "Fetching related records.\n"
            "Please wait."
        )

        try:
            records, searched, status = await _expanded_lookup_from_seed(
                api_client, ("id", record_id)
            )
        except Exception:
            logger.exception("ID expanded lookup crashed")
            records, searched, status = [], {"mobiles": [], "ids": []}, "error"

        refundable = {"auth_failed", "timeout", "unavailable", "error"}
        if credit_reserved and status in refundable:
            db_module.add_credits(user.id, 1)
            credit_reserved = False

        try:
            await loading.delete()
        except Exception:
            pass

        if status == "ok" and records:
            try:
                await _send_search_log(
                    context, config, user, f"ID:{record_id}", "ok",
                    count=len(records), searched=searched,
                )
            except Exception:
                logger.exception("Failed to send ID search log")

            expansion_note = ""
            extra_mobiles = len(searched.get("mobiles", []))
            extra_ids = max(0, len(searched.get("ids", [])) - 1)
            if extra_mobiles or extra_ids:
                expansion_note = (
                    f"\n🔁 Also searched: {extra_mobiles} mobile(s), "
                    f"{extra_ids} ID(s)"
                )

            await _send_results(
                update,
                records,
                header_line=f"🆔 ID: {record_id}{expansion_note}",
                is_admin=is_admin,
                db_module=db_module,
                user=user,
            )
            return

        if status == "not_found":
            try:
                await _send_search_log(
                    context, config, user, f"ID:{record_id}", "not_found",
                    count=0, searched=searched,
                )
            except Exception:
                logger.exception("Failed to send ID search log")

            await update.effective_message.reply_text(
                "🔎 NO RECORD FOUND\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"No matching record was found for ID {record_id}.",
                reply_markup=_result_keyboard(),
            )
        elif status == "auth_failed":
            await update.effective_message.reply_text(
                "🔐 SERVICE AUTHENTICATION ERROR\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "The database API rejected this bot's API key."
            )
        elif status == "timeout":
            await update.effective_message.reply_text(
                "⏱ REQUEST TIMED OUT\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "The database took too long to respond.",
                reply_markup=_result_keyboard(),
            )
        elif status == "unavailable":
            await update.effective_message.reply_text(
                "🛠 SERVICE TEMPORARILY UNAVAILABLE\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "The database server is currently unreachable."
            )
        else:
            await update.effective_message.reply_text(
                "⚠️ LOOKUP FAILED\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Something went wrong while processing the ID lookup.",
                reply_markup=_result_keyboard(),
            )

    application.add_handler(CommandHandler("lookup", lookup_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler("record", id_command))

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(search_button, pattern=r"^search_mobile$")
        ],
        states={
            WAITING_MOBILE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_mobile),
                CallbackQueryHandler(cancel, pattern=r"^cancel_search$"),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    application.add_handler(conv)

    # NOTE: main_menu / help_menu / bot_status callbacks are handled
    # in handlers/start.py via menu_callback. Do not duplicate them here.