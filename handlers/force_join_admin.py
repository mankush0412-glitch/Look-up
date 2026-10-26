from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from .common import admin_only


def _parse_chat_identifier(value: str):
    value = value.strip()
    if value.lstrip("-").isdigit():
        return int(value)
    return value


def register_force_join_handlers(application, config, db_module):

    @admin_only(config)
    async def force_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.effective_message.reply_text(
                "Use:\n"
                "/forceadd @channelusername\n"
                "or\n"
                "/forceadd -1001234567890\n\n"
                "Bot ko channel/group me admin banana zaroori hai."
            )
            return

        identifier = _parse_chat_identifier(context.args[0])

        try:
            chat = await context.bot.get_chat(identifier)
        except Exception as exc:
            await update.effective_message.reply_text(
                "❌ Chat access nahi ho paya.\n"
                "Ensure bot chat me added/admin ho.\n\n"
                f"Error: {type(exc).__name__}"
            )
            return

        if getattr(chat, "username", None):
            invite_link = f"https://t.me/{chat.username}"
        else:
            try:
                invite = await context.bot.create_chat_invite_link(
                    chat_id=chat.id,
                    name="Lookup Bot Force Join",
                )
                invite_link = invite.invite_link
            except Exception as exc:
                await update.effective_message.reply_text(
                    "❌ Invite link generate nahi ho paya.\n"
                    "Private channel/group me bot ko admin + invite users permission dena hoga.\n\n"
                    f"Error: {type(exc).__name__}"
                )
                return

        title = getattr(chat, "title", None) or getattr(chat, "full_name", None) or str(chat.id)

        db_module.add_force_channel(
            chat_id=chat.id,
            title=title,
            invite_link=invite_link,
            username=getattr(chat, "username", None),
        )

        await update.effective_message.reply_text(
            "✅ FORCE JOIN ADDED\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Chat: {title}\n"
            f"ID: {chat.id}\n"
            f"Link: {invite_link}"
        )

    @admin_only(config)
    async def force_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.effective_message.reply_text(
                "Use: /forceremove <chat_id>\n"
                "Chat IDs dekhne ke liye /forcelist"
            )
            return

        try:
            chat_id = int(context.args[0])
        except ValueError:
            await update.effective_message.reply_text(
                "❌ /forceremove me numeric chat ID use karo."
            )
            return

        if db_module.remove_force_channel(chat_id):
            await update.effective_message.reply_text(
                f"✅ Force join removed: {chat_id}"
            )
        else:
            await update.effective_message.reply_text(
                "❌ Ye chat force-join list me nahi mila."
            )

    @admin_only(config)
    async def force_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
        channels = db_module.list_force_channels()

        if not channels:
            await update.effective_message.reply_text(
                "📭 Koi force-join channel/group configured nahi hai."
            )
            return

        lines = ["📢 FORCE JOIN LIST", "━━━━━━━━━━━━━━━━━━━━"]
        keyboard = []

        for index, channel in enumerate(channels, 1):
            chat_id = channel["chat_id"]
            title = channel.get("title") or str(chat_id)
            link = channel.get("invite_link") or "No link"
            lines.append(
                f"{index}. {title}\n"
                f"   ID: {chat_id}\n"
                f"   Link: {link}"
            )
            keyboard.append([
                InlineKeyboardButton(
                    f"❌ Remove {title[:24]}",
                    callback_data=f"force_remove:{chat_id}",
                )
            ])

        await update.effective_message.reply_text(
            "\n\n".join(lines),
            reply_markup=InlineKeyboardMarkup(keyboard),
            disable_web_page_preview=True,
        )

    @admin_only(config)
    async def remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        try:
            chat_id = int(query.data.split(":", 1)[1])
        except Exception:
            return

        removed = db_module.remove_force_channel(chat_id)

        if removed:
            await query.message.reply_text(f"✅ Force join removed: {chat_id}")
        else:
            await query.message.reply_text("❌ Chat already removed/not found.")

    application.add_handler(CommandHandler("forceadd", force_add))
    application.add_handler(CommandHandler("forceremove", force_remove))
    application.add_handler(CommandHandler("forcelist", force_list))
    application.add_handler(CommandHandler("forcechannels", force_list))
    application.add_handler(
        CallbackQueryHandler(remove_callback, pattern=r"^force_remove:-?\d+$")
    )
