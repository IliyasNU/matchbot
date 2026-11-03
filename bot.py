"""Run with `python bot.py`. Private chats only; one polling instance per database."""

import argparse
import asyncio
import html
import logging
import time
from contextlib import contextmanager
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

from config import Settings
from db.database import Database
from db.models import normalize_interests, parse_integer
from handlers.browse import BrowseFlow
from handlers.profile import ProfileFlow
from i18n import tr
from update_processor import PerUserUpdateProcessor

LOG = logging.getLogger(__name__)


def contact_link(user, lang):
    username = user.get("username")
    # A username link works across more Telegram privacy configurations.
    url = f"https://t.me/{username}" if username else f"tg://user?id={int(user['id'])}"
    return f'<a href="{html.escape(url, quote=True)}">{tr(lang, "contact")}</a>'


class MatchBot:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings
        self.application = None
        self.profile = ProfileFlow(self)
        self.browse = BrowseFlow(self)
        self.notification_lock = asyncio.Lock()
        self.notification_backoff_until = 0.0

    async def send(self, uid, text, **kwargs):
        return await self.application.bot.send_message(chat_id=uid, text=text, **kwargs)

    async def card(self, uid, profile, keyboard=None):
        caption = f"<b>{html.escape(profile['name'])}, {profile['age']}</b> · {html.escape(profile['city'])}"
        plain_caption = f"{profile['name']}, {profile['age']} · {profile['city']}"
        heading = caption
        lang = self.db.user(uid)["lang"]
        details = []
        if profile.get("purpose", "any") != "any":
            details.append(tr(lang, "purpose_label") + ": " + tr(lang, "purpose_" + profile["purpose"]))
        interests = normalize_interests(profile.get("interests", []))
        if interests:
            details.append(
                tr(lang, "interests_label") + ": " + ", ".join(tr(lang, "interest_" + tag) for tag in interests)
            )
        body = "\n".join(details)
        if profile.get("bio"):
            body = "\n\n".join(part for part in (body, profile["bio"]) if part)
        if body:
            caption += "\n\n" + html.escape(body)
            plain_caption += "\n\n" + body
        # Telegram measures the decoded caption in UTF-16 units; an otherwise
        # valid 500-character emoji bio can exceed its 1024-unit photo limit.
        split_bio = len(plain_caption.encode("utf-16-le")) // 2 > 1024
        try:
            await self.application.bot.send_photo(
                uid,
                profile["photo"],
                caption=heading if split_bio else caption,
                parse_mode="HTML",
                reply_markup=None if split_bio else keyboard,
            )
        except BadRequest:
            # A cached Telegram photo can expire/become invalid. The card stays usable;
            # the owner can replace the photo using /edit.
            await self.send(uid, caption, parse_mode="HTML", reply_markup=keyboard)
        else:
            if split_bio:
                await self.send(uid, html.escape(body), parse_mode="HTML", reply_markup=keyboard)

    async def menu(self, uid):
        user = self.db.user(uid)
        lang = user["lang"]

        def b(key, command=None):
            return InlineKeyboardButton(tr(lang, key), callback_data=f"nav:{command or key}")

        if user["photo"]:
            rows = [
                [b("browse"), b("likes")],
                [b("profile"), b("matches")],
                [b("edit"), b("filters")],
                [b("pause" if user["active"] else "resume"), b("language")],
                [b("blocked_list", "blocked"), b("delete")],
            ]
        else:
            rows = [[b("create", "register")], [b("language")]]
        await self.send(uid, tr(lang, "menu"), reply_markup=InlineKeyboardMarkup(rows))

    async def remove_buttons(self, query):
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass  # The transaction has already committed; an old message may be gone.

    async def handle(self, update, context):
        if not update.effective_user or not update.effective_chat:
            return
        uid = update.effective_user.id
        query = update.callback_query
        if update.effective_chat.type != "private":
            if query:
                await query.answer()
            return
        tg_user = update.effective_user
        user = self.db.touch(uid, tg_user.username, tg_user.language_code or "en")
        lang = user["lang"]
        if not self.db.rate_allowed(uid):
            if query:
                await query.answer(tr(lang, "slow"))
            return
        if query:
            try:
                await query.answer()
            except BadRequest:
                return
            # An administrator may suspend this member while Telegram answers.
            user = self.db.user(uid)
            lang = user["lang"]
        # Deletion/privacy are available even to suspended members.
        raw = getattr(update.effective_message, "text", None) or ""
        command = raw.split()[0].split("@")[0][1:].lower() if raw.startswith("/") else None
        args = raw.split()[1:] if command else []
        data = str(query.data or "") if query else ""
        if data.startswith("nav:"):
            command = data.split(":", 1)[1]
        if user["banned"]:
            deletion = self.db.session(uid)
            if command not in {"delete", "privacy", "id", "cancel"} and not (
                data.startswith("w:") and deletion and deletion[0] == "delete_confirm"
            ):
                await self.send(uid, tr(lang, "banned"))
                return
        if command:
            await self.command(uid, command, args)
        elif data.startswith("lang:"):
            new_lang = data.split(":", 1)[1]
            if new_lang in {"en", "ru"}:
                self.db.set_user(uid, lang=new_lang)
                session = self.db.session(uid)
                if session:
                    await self.profile.prompt(uid, *session)
                else:
                    await self.menu(uid)
        elif data.startswith("w:"):
            parts = data.split(":")
            if len(parts) == 3:
                await self.profile.accept(uid, nonce=parts[1], choice=parts[2])
        elif data.startswith("r:"):
            parts = data.split(":")
            if len(parts) == 3:
                await self.browse.react(uid, parts[1], parts[2], query)
        elif data.startswith("matches:"):
            offset = parse_integer(data.partition(":")[2], minimum=0, maximum=100000)
            if offset is not None:
                await self.show_matches(uid, offset)
        elif data.startswith("unmatch:"):
            target = parse_integer(data.partition(":")[2])
            if target is None:
                return
            if self.db.block_match(uid, target):
                await self.remove_buttons(query)
                await self.send(uid, tr(lang, "blocked"))
            else:
                await self.send(uid, tr(lang, "stale"))
        elif data.startswith("matchreport:"):
            parts = data.split(":")
            if len(parts) not in {2, 3}:
                return
            target = parse_integer(parts[1])
            if target is None:
                return
            if not self.db.is_match(uid, target):
                await self.send(uid, tr(lang, "stale"))
            elif len(parts) == 2:
                await self.send(
                    uid,
                    tr(lang, "report_prompt"),
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton(tr(lang, key), callback_data=f"matchreport:{target}:{key}")]
                            for key in ("spam", "underage", "abuse", "other")
                        ]
                    ),
                )
            elif self.db.report_match(uid, target, parts[2]):
                await self.remove_buttons(query)
                await self.send(uid, tr(lang, "reported"))
        elif data.startswith("unblock:"):
            target = parse_integer(data.partition(":")[2])
            if target is not None and self.db.unblock(uid, target):
                await self.remove_buttons(query)
                await self.send(uid, tr(lang, "unblocked"))
        elif data.startswith("blocked:"):
            offset = parse_integer(data.partition(":")[2], minimum=0, maximum=100000)
            if offset is not None:
                await self.show_blocked(uid, offset)
        elif self.db.session(uid) and not query:
            await self.profile.accept(uid, message=update.effective_message)
        else:
            await self.menu(uid)

    async def command(self, uid, command, args=()):
        user = self.db.user(uid)
        lang = user["lang"]
        if command in {"admin", "stats", "reports", "inspect", "ban", "unban", "resolve"}:
            await self.admin(uid, command, args)
            return
        if command in {"help", "privacy"}:
            await self.send(uid, tr(lang, command))
            return
        if command == "id":
            await self.send(uid, str(uid))
            return
        if command == "language":
            await self.send(
                uid,
                tr(lang, "choose_language"),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("Русский", callback_data="lang:ru"),
                            InlineKeyboardButton("English", callback_data="lang:en"),
                        ],
                    ]
                ),
            )
            return
        if command == "delete":
            await self.profile.prompt(uid, "delete_confirm", {})
            return
        if command == "cancel":
            self.db.clear_session(uid)
            await self.send(uid, tr(lang, "cancelled"))
            if not user["banned"]:
                await self.menu(uid)
            return
        if command == "start":
            session = self.db.session(uid)
            if session:
                await self.profile.prompt(uid, *session)
            else:
                await self.send(uid, tr(lang, "welcome"))
                await self.menu(uid)
            return
        if command in {"register", "edit"}:
            await self.profile.begin(uid, editing=bool(user["photo"]))
            return
        if command == "menu":
            self.db.clear_session(uid)
            await self.menu(uid)
            return
        if not user["photo"]:
            await self.send(uid, tr(lang, "need_profile"))
            await self.menu(uid)
            return
        if command == "profile":
            await self.card(uid, user)
            await self.send(uid, tr(lang, "active_status" if user["active"] else "paused_status"))
            await self.menu(uid)
        elif command in {"browse", "likes"}:
            self.db.clear_session(uid)
            await self.browse.show(uid, "feed" if command == "browse" else "likes")
        elif command == "matches":
            await self.show_matches(uid)
        elif command == "blocked":
            await self.show_blocked(uid)
        elif command == "filters":
            await self.send(
                uid,
                tr(
                    lang,
                    "filter_status",
                    low=user["min_age"],
                    high=user["max_age"],
                    location=tr(lang, "local" if user["local_only"] else "everywhere"),
                ),
            )
            await self.profile.prompt(uid, "range", {})
        elif command in {"pause", "resume"}:
            self.db.clear_session(uid)
            self.db.set_user(uid, active=int(command == "resume"))
            await self.send(uid, tr(lang, "paused" if command == "pause" else "resumed"))
            await self.menu(uid)
        else:
            await self.send(uid, tr(lang, "help"))

    async def show_matches(self, uid, offset=0):
        lang = self.db.user(uid)["lang"]
        rows = self.db.matches(uid, offset)
        shown = False
        for person in rows[:5]:
            person = self.db.match_profile(uid, person["id"])
            if person is None:
                continue
            await self.card(uid, person)
            # Photo delivery yields to other members: a block, deletion, ban or
            # username change must take effect before sending the contact link.
            person = self.db.match_profile(uid, person["id"])
            if person is None:
                continue
            await self.send(
                uid,
                contact_link(person, lang),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(tr(lang, "block"), callback_data=f"unmatch:{person['id']}"),
                            InlineKeyboardButton(tr(lang, "report"), callback_data=f"matchreport:{person['id']}"),
                        ],
                    ]
                ),
            )
            shown = True
        if not shown:
            await self.send(uid, tr(lang, "no_matches"))
        if len(rows) > 5:
            await self.send(
                uid,
                tr(lang, "matches"),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton(tr(lang, "more"), callback_data=f"matches:{offset + 5}")],
                    ]
                ),
            )
        await self.menu(uid)

    async def show_blocked(self, uid, offset=0):
        lang = self.db.user(uid)["lang"]
        rows = self.db.blocked_users(uid, offset)
        if not rows:
            await self.send(uid, tr(lang, "no_blocks"))
        for person in rows[:5]:
            await self.send(
                uid,
                person["name"] or str(person["id"]),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton(tr(lang, "unblock"), callback_data=f"unblock:{person['id']}")],
                    ]
                ),
            )
        if len(rows) > 5:
            await self.send(
                uid,
                tr(lang, "blocked_list"),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton(tr(lang, "more"), callback_data=f"blocked:{offset + 5}")],
                    ]
                ),
            )
        await self.menu(uid)

    async def notifications(self, context=None):
        if self.notification_lock.locked() or time.monotonic() < self.notification_backoff_until:
            return
        async with self.notification_lock:
            for item in self.db.pending_notifications():
                item = self.db.pending_notification(item["id"])
                if item is None:
                    continue
                if not self.db.notification_valid(item):
                    self.db.notification_done(item["id"])
                    continue
                person = self.db.user(item["other"])
                lang = self.db.user(item["recipient"])["lang"]
                text = (
                    tr(lang, "new_like")
                    if item["kind"] == "like"
                    else tr(lang, "match_notice", name=html.escape(person["name"]), contact=contact_link(person, lang))
                )
                try:
                    await self.send(item["recipient"], text, parse_mode="HTML")
                except Forbidden:
                    self.db.set_user(item["recipient"], active=0, reachable=0)
                    self.db.notification_done(item["id"])
                except RetryAfter as exc:
                    delay = (
                        exc.retry_after.total_seconds()
                        if hasattr(exc.retry_after, "total_seconds")
                        else exc.retry_after
                    )
                    self.db.notification_retry(item["id"], float(delay) + 1)
                    self.notification_backoff_until = time.monotonic() + float(delay) + 1
                    break
                except BadRequest:
                    # Permanent Telegram error (deleted chat, invalid recipient, etc.).
                    LOG.warning("Notification %s rejected by Telegram", item["id"])
                    self.db.notification_done(item["id"])
                except TelegramError:
                    self.db.notification_retry(item["id"], min(3600, 2 ** min(item["attempts"] + 1, 12)))
                else:
                    self.db.notification_done(item["id"])

    async def admin(self, uid, command, args):
        if uid not in self.settings.admin_ids:
            await self.send(uid, "Admin access required. /help")
            return
        if command == "stats":
            await self.send(uid, "\n".join(f"{key}: {value}" for key, value in self.db.stats().items()))
        elif command == "reports":
            reports = self.db.reports()
            await self.send(
                uid,
                "\n".join(f"#{r['id']} · from {r['actor']} · target {r['target']} · {r['reason']}" for r in reports)
                or "No open reports.",
            )
        elif command in {"ban", "unban", "resolve", "inspect"}:
            target = parse_integer(args[0]) if len(args) == 1 else None
            if target is None:
                await self.send(uid, f"Usage: /{command} NUMERIC_ID")
                return
            if command == "inspect":
                person = self.db.user(target)
                if person and person["photo"]:
                    await self.card(uid, person)
                    await self.send(uid, f"ID: {target}; banned: {bool(person['banned'])}")
                else:
                    await self.send(uid, "Profile not found.")
            elif command == "resolve":
                await self.send(uid, "Resolved." if self.db.resolve(target) else "Report not found.")
            elif target in self.settings.admin_ids:
                await self.send(uid, "Admin accounts cannot be banned here.")
            else:
                changed = self.db.ban(target, command == "ban")
                await self.send(uid, "Updated. Unbanned members must /resume." if changed else "User not found.")
        else:
            await self.send(
                uid,
                "/stats\n/reports (oldest 20 unresolved)\n/inspect USER_ID\n/ban USER_ID\n/unban USER_ID\n/resolve REPORT_ID",
            )

    async def on_error(self, update, context):
        # Telegram exception strings can contain request URLs/tokens or user content.
        LOG.error("Update failed: %s", type(context.error).__name__)
        if update and update.effective_chat and update.effective_chat.type == "private":
            user = self.db.user(update.effective_chat.id)
            try:
                await self.send(update.effective_chat.id, tr(user["lang"] if user else "en", "error"))
            except TelegramError:
                pass

    async def post_init(self, app):
        commands = [
            "start",
            "browse",
            "likes",
            "matches",
            "profile",
            "edit",
            "filters",
            "pause",
            "resume",
            "language",
            "help",
            "privacy",
            "delete",
        ]
        for lang in ("en", "ru"):
            descriptions = {
                "start": "Menu" if lang == "en" else "Меню",
                "help": "Help" if lang == "en" else "Помощь",
                "privacy": "Privacy" if lang == "en" else "О данных",
            }
            items = [BotCommand(c, descriptions.get(c) or tr(lang, c)) for c in commands]
            await app.bot.set_my_commands(items, language_code=lang)
            if lang == "en":
                await app.bot.set_my_commands(items)
        LOG.info("Bot initialized. Polling for private-chat updates.")

    def build(self):
        processor = PerUserUpdateProcessor(self.settings.max_concurrent_updates)
        app = (
            Application.builder()
            .token(self.settings.token)
            .concurrent_updates(processor)
            .connection_pool_size(self.settings.max_concurrent_updates * 2 + 8)
            .update_queue(asyncio.Queue(maxsize=2048))
            .post_init(self.post_init)
            .build()
        )
        processor.bind(app)
        self.application = app
        app.add_handler(CallbackQueryHandler(self.handle))
        app.add_handler(MessageHandler(filters.ChatType.PRIVATE, self.handle))
        app.add_error_handler(self.on_error)
        app.job_queue.run_repeating(self.notifications, interval=5, first=1, job_kwargs={"max_instances": 1})
        return app


@contextmanager
def instance_lock(path):
    """OS releases this lock even after a crash; stale lock files are harmless."""
    import os

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError("Another bot instance is already using this database.") from exc
    try:
        yield
    finally:
        handle.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Validate configuration/database without contacting Telegram"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
        with instance_lock(str(settings.database) + ".lock"):
            db = Database(settings.database)
            try:
                service = MatchBot(db, settings)
                app = service.build()
                if args.check:
                    print("Configuration, database and handlers OK. No Telegram request made.")
                    if not settings.admin_ids:
                        print("ADMIN_IDS is empty: set an administrator to review reports.")
                else:
                    app.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY], drop_pending_updates=False)
            finally:
                db.close()
    except (ValueError, RuntimeError) as exc:
        parser.exit(1, f"Startup error: {exc}\n")
    except TelegramError as exc:
        parser.exit(
            1,
            f"Telegram startup failed ({type(exc).__name__}). Check the token, network and other running instances.\n",
        )


if __name__ == "__main__":
    main()
