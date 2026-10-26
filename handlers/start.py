import logging
from urllib.parse import quote
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CommandHandler, CallbackQueryHandler, ContextTypes

from .common import authorized_only, ensure_force_join
from .reward import process_reward_claim

logger = logging.getLogger(__name__)


def main_menu():
    # Contact bot is stored in MongoDB so admins can change it without redeploying.
    try:
        import database as _db
        contact_username = _db.get_contact_bot_username()
    except Exception:
        contact_username = ""

    if contact_username:
        draft = quote("Hi, I want to buy SRC. Please share details.")
        src_button = InlineKeyboardButton(
            "🧾 Buy SRC",
            url=f"https://t.me/{contact_username}?text={draft}",
        )
    else:
        src_button = InlineKeyboardButton("🧾 Buy SRC", callback_data="buy_src_unconfigured")

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Search Number", callback_data="search_mobile")],
        [
            InlineKeyboardButton("💳 Balance", callback_data="show_balance"),
            InlineKeyboardButton("🛒 Buy Credits", callback_data="buy_credits"),
        ],
        [src_button],
        [
            InlineKeyboardButton("📦 Order History", callback_data="order_history"),
            InlineKeyboardButton("🔑 My API", callback_data="my_api"),
        ],
        [
            InlineKeyboardButton("📖 Help", callback_data="help_menu"),
            InlineKeyboardButton("🟢 Status", callback_data="bot_status"),
        ],
        [InlineKeyboardButton("🤝 Refer & Earn", callback_data="referral_info")],
    ])


def back_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]
    ])


WELCOME_TEXT = (
    "╔══════════════════════╗\n"
    "      🔐 DATABASE LOOKUP\n"
    "╚══════════════════════╝\n\n"
    "⚡ Fast • Secure • Private\n\n"
    "Use the menu below to search available records.\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━"
)

HELP_TEXT = (
    "📖 HELP CENTER\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n"
    "/start — Main menu\n"
    "/lookup <number> — Direct search\n"
    "/bal or /balance — Credit balance\n"
    "/referral — Referral link\n"
    "/buycredits — View purchase plans\n/plans — View all plans\n/orders — Purchase history\n/myapi — API plan status\n/apidocs — API documentation\n"
    "/ping — Status\n"
    "/cancel — Cancel search"
)

STATUS_TEXT = (
    "🟢 SYSTEM STATUS\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n"
    "✅ Telegram Bot ONLINE\n"
    "✅ Lookup Service ONLINE"
)


def _welcome_text_for_user(user) -> str:
    greeting = f"👋 Welcome, {user.first_name}\n\n" if getattr(user, "first_name", None) else ""
    return greeting + WELCOME_TEXT


async def _send_welcome_message(message, db_module, user, reply_markup=None):
    text = _welcome_text_for_user(user)
    welcome_image = db_module.get_welcome_image_file_id()
    if welcome_image:
        try:
            await message.reply_photo(photo=welcome_image, caption=text, reply_markup=reply_markup)
            return
        except Exception:
            logger.exception("Failed to send welcome image; falling back to text")
    await message.reply_text(text, reply_markup=reply_markup)


async def _show_main_menu_from_callback(query, db_module):
    text = WELCOME_TEXT
    welcome_image = db_module.get_welcome_image_file_id()
    if welcome_image:
        try:
            await query.message.reply_photo(photo=welcome_image, caption=text, reply_markup=main_menu())
            return
        except Exception:
            logger.exception("Failed to send main menu welcome image; falling back to text")
    await _safe_edit_or_reply(query, text, reply_markup=main_menu())


async def _safe_edit_or_reply(query, text, reply_markup=None):
    try:
        await query.message.edit_text(text, reply_markup=reply_markup)
        return
    except Exception as exc:
        err_text = str(exc).lower()
        if "not modified" in err_text:
            return
        logger.warning(
            "edit_text failed (%s): %s — falling back to reply_text",
            type(exc).__name__, exc,
        )
    try:
        await query.message.reply_text(text, reply_markup=reply_markup)
    except Exception:
        logger.exception("reply_text fallback also failed")


def register_start_handlers(application, config, db_module):

    async def _complete_start(update: Update, context: ContextTypes.DEFAULT_TYPE, ref_id: int | None = None):
        user = update.effective_user
        if not user:
            return

        is_new = False
        if not db_module.get_user(user.id):
            is_new = db_module.add_user(user.id)

        if (
            is_new
            and ref_id
            and ref_id != user.id
            and db_module.get_user(ref_id)
            and not db_module.get_referrer(user.id)
        ):
            if db_module.create_referral(ref_id, user.id):
                db_module.add_credits(ref_id, 5)

                await update.effective_message.reply_text(
                    "🎉 Referral applied successfully!\n"
                    "You received 5 starting credits.\n"
                    "The referrer received +5 credits."
                )

                if getattr(config, "log_chat_id", None):
                    try:
                        referrer = await context.bot.get_chat(ref_id)

                        referred_name = user.full_name or user.first_name or str(user.id)
                        referred_username = (
                            f"@{user.username}" if user.username else "No username"
                        )

                        referrer_name = (
                            getattr(referrer, "full_name", None)
                            or getattr(referrer, "first_name", None)
                            or str(ref_id)
                        )
                        referrer_username_raw = getattr(referrer, "username", None)
                        referrer_username = (
                            f"@{referrer_username_raw}"
                            if referrer_username_raw
                            else "No username"
                        )

                        total_referrals = db_module.count_referred_users(ref_id)

                        await context.bot.send_message(
                            chat_id=config.log_chat_id,
                            text=(
                                "🤝 NEW REFERRAL\n"
                                "━━━━━━━━━━━━━━━━━━━━\n\n"
                                "👤 Referrer:\n"
                                f"Name: {referrer_name}\n"
                                f"Username: {referrer_username}\n"
                                f"ID: {ref_id}\n\n"
                                "🆕 Referred User:\n"
                                f"Name: {referred_name}\n"
                                f"Username: {referred_username}\n"
                                f"ID: {user.id}\n\n"
                                "🎁 Reward: +5 Credits\n"
                                f"👥 Total Referrals: {total_referrals}"
                            ),
                        )
                    except Exception as e:
                        logger.exception("Referral log error: %s", e)

        context.user_data.pop("pending_ref_id", None)

        await _send_welcome_message(
            update.effective_message,
            db_module,
            user,
            reply_markup=main_menu(),
        )

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user:
            return

        arg = context.args[0].strip() if context.args else ""

        ref_id = None
        reward_token = None

        if arg.startswith("reward_"):
            reward_token = arg[len("reward_"):]
        elif arg:
            try:
                ref_id = int(arg)
            except ValueError:
                ref_id = None

        if ref_id:
            context.user_data["pending_ref_id"] = ref_id
        if reward_token:
            context.user_data["pending_reward"] = reward_token

        if not await ensure_force_join(update, context, config, db_module):
            return

        pending_reward = context.user_data.pop("pending_reward", None)
        if pending_reward:
            if not db_module.get_user(user.id):
                db_module.add_user(user.id)
            try:
                await process_reward_claim(
                    update, context, pending_reward, db_module, config
                )
            except Exception:
                logger.exception("Reward claim failed")

        await _complete_start(
            update,
            context,
            ref_id=context.user_data.get("pending_ref_id"),
        )

    async def force_join_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await ensure_force_join(update, context, config, db_module):
            return

        query = update.callback_query
        if query:
            try:
                await query.answer("✅ Membership verified")
            except Exception:
                pass

        user = update.effective_user
        if user:
            pending_reward = context.user_data.pop("pending_reward", None)
            if pending_reward:
                if not db_module.get_user(user.id):
                    db_module.add_user(user.id)
                try:
                    await process_reward_claim(
                        update, context, pending_reward, db_module, config
                    )
                except Exception:
                    logger.exception("Reward claim failed")

        await _complete_start(
            update,
            context,
            ref_id=context.user_data.get("pending_ref_id"),
        )

    @authorized_only(config, db_module)
    async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_text(HELP_TEXT, reply_markup=back_menu())

    @authorized_only(config, db_module)
    async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_text(STATUS_TEXT, reply_markup=back_menu())

    @authorized_only(config, db_module)
    async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user:
            return

        if user.id in getattr(config, "admin_ids", set()):
            text = "💳 CREDIT BALANCE\n━━━━━━━━━━━━━━━━━━━━\nCredits: Unlimited (Admin)"
        else:
            api_until = db_module.get_api_plan_until(user.id)
            text = (
                "💳 ACCOUNT STATUS\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"Credits available: {db_module.get_credits(user.id)}\n"
                f"24h Unlimited: {'Active' if db_module.has_active_bot_pass(user.id) else 'Inactive'}\n"
                f"API Plan: {'Active' if api_until and api_until > datetime.now(timezone.utc) else 'Inactive'}"
            )

        await update.effective_message.reply_text(text, reply_markup=back_menu())

    @authorized_only(config, db_module)
    async def balance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user = update.effective_user
        if not query or not user:
            return

        await query.answer()

        if user.id in getattr(config, "admin_ids", set()):
            text = "💳 CREDIT BALANCE\n━━━━━━━━━━━━━━━━━━━━\nCredits: Unlimited (Admin)"
        else:
            api_until = db_module.get_api_plan_until(user.id)
            text = (
                "💳 ACCOUNT STATUS\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"Credits available: {db_module.get_credits(user.id)}\n"
                f"24h Unlimited: {'Active' if db_module.has_active_bot_pass(user.id) else 'Inactive'}\n"
                f"API Plan: {'Active' if api_until and api_until > datetime.now(timezone.utc) else 'Inactive'}"
            )

        await _safe_edit_or_reply(query, text, reply_markup=back_menu())

    @authorized_only(config, db_module)
    async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user:
            return

        bot_username = (await context.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start={user.id}"
        count = db_module.count_referred_users(user.id)

        if user.id in getattr(config, "admin_ids", set()):
            credits_text = "Unlimited (Admin)"
        else:
            credits_text = str(db_module.get_credits(user.id))

        text = (
            "🤝 REFER & EARN\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Your referral link:\n{link}\n\n"
            "Each valid new referral gives you +5 credits.\n"
            "The referred new user starts with 5 credits.\n\n"
            f"👥 Referred users: {count}\n"
            f"💳 Credits: {credits_text}"
        )

        if update.callback_query:
            await update.callback_query.answer()
            await _safe_edit_or_reply(
                update.callback_query, text, reply_markup=back_menu()
            )
        else:
            await update.effective_message.reply_text(text, reply_markup=back_menu())

    @authorized_only(config, db_module)
    async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        if query.data == "main_menu":
            await _show_main_menu_from_callback(query, db_module)
        elif query.data == "help_menu":
            await _safe_edit_or_reply(query, HELP_TEXT, reply_markup=back_menu())
        elif query.data == "bot_status":
            await _safe_edit_or_reply(query, STATUS_TEXT, reply_markup=back_menu())
        elif query.data == "buy_src_unconfigured":
            await _safe_edit_or_reply(
                query,
                "⚠️ SRC contact bot is not configured yet. Please contact admin.",
                reply_markup=back_menu(),
            )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(force_join_check, pattern=r"^check_force_join$"))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("ping", ping))
    application.add_handler(CommandHandler("bal", balance_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("referral", referral_command))

    application.add_handler(
        CallbackQueryHandler(balance_callback, pattern=r"^show_balance$")
    )
    application.add_handler(
        CallbackQueryHandler(referral_command, pattern=r"^referral_info$")
    )
    application.add_handler(
        CallbackQueryHandler(
            menu_callback,
            pattern=r"^(main_menu|help_menu|bot_status|buy_src_unconfigured)$",
        )
    )