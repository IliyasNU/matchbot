"""A durable profile/settings wizard. Draft edits never overwrite a live profile."""

import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from db.models import (
    GENDERS,
    INTERESTS,
    MAX_INTERESTS,
    PREFERENCES,
    PURPOSES,
    age_range,
    clean_text,
    read_interests,
    read_purpose,
)
from i18n import tr

STEPS = ["consent", "name", "age", "gender", "seeking", "city", "purpose", "interests", "bio", "photo", "preview"]


class ProfileFlow:
    def __init__(self, service):
        self.s = service
        self.db = service.db

    async def begin(self, uid, editing=False):
        user = self.db.user(uid)
        data = (
            {
                key: user[key]
                for key in (
                    "name",
                    "age",
                    "gender",
                    "seeking",
                    "city",
                    "purpose",
                    "interests",
                    "bio",
                    "photo",
                    "consent",
                )
            }
            if editing and user["photo"]
            else {}
        )
        data["editing"] = bool(data)
        await self.prompt(uid, "name" if data["editing"] else "consent", data)

    async def prompt(self, uid, step, data):
        lang = self.db.user(uid)["lang"]
        # Defaults let existing registration/edit drafts resume across the upgrade.
        data["purpose"] = read_purpose(data.get("purpose"))
        data["interests"] = read_interests(data.get("interests"))
        data["nonce"] = secrets.token_hex(5)
        self.db.save_session(uid, step, data)

        def button(key, value):
            return InlineKeyboardButton(tr(lang, key), callback_data=f"w:{data['nonce']}:{value}")

        rows = []
        if step == "consent":
            rows = [[button("agree", "agree")]]
        elif step in {"gender", "seeking"}:
            rows = [[button(key, key)] for key in ("male", "female", "other")]
            if step == "seeking":
                rows.append([button("any", "any")])
        elif step == "purpose":
            rows = [[button("purpose_" + key, key)] for key in ("relationship", "friendship", "chat", "any")]
        elif step == "interests":
            buttons = [
                InlineKeyboardButton(
                    ("✅ " if key in data["interests"] else "") + tr(lang, "interest_" + key),
                    callback_data=f"w:{data['nonce']}:interest_{key}",
                )
                for key in INTERESTS
            ]
            rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
            rows.append([button("done", "done"), button("skip", "skip")])
        elif step == "bio":
            rows = [[button("skip", "skip")]]
        elif step == "preview":
            await self.s.card(uid, data)
            rows = [[button("publish", "publish")]]
        elif step == "location":
            rows = [[button("local", "local"), button("everywhere", "everywhere")]]
        elif step == "delete_confirm":
            rows = [[button("delete_yes", "delete")]]
        if data.get("editing") and step in STEPS[1:-1]:
            rows.append([button("keep", "keep")])
        rows.append([InlineKeyboardButton(tr(lang, "cancel"), callback_data="nav:cancel")])
        text = tr(lang, step)
        if step == "interests":
            selected = ", ".join(tr(lang, "interest_" + key) for key in data["interests"]) or tr(lang, "interests_none")
            text += "\n\n" + tr(lang, "interests_selected", interests=selected)
        await self.s.send(uid, text, reply_markup=InlineKeyboardMarkup(rows))

    async def accept(self, uid, message=None, nonce=None, choice=None):
        session = self.db.session(uid)
        lang = self.db.user(uid)["lang"]
        if not session or (nonce is not None and nonce != session[1].get("nonce")):
            await self.s.send(uid, tr(lang, "stale"))
            return
        step, data = session
        # A callback can arrive for a draft saved by an earlier bot version,
        # without first passing through prompt() after the deployment.
        data["purpose"] = read_purpose(data.get("purpose"))
        data["interests"] = read_interests(data.get("interests"))
        text = message.text.strip() if message and message.text else ""
        try:
            if step == "delete_confirm":
                if choice != "delete":
                    raise ValueError()
                self.db.delete_user(uid)
                await self.s.send(uid, tr(lang, "deleted"))
                return
            if step == "range":
                data["low"], data["high"] = age_range(text)
                await self.prompt(uid, "location", data)
                return
            if step == "location":
                if choice not in {"local", "everywhere"}:
                    raise ValueError()
                self.db.set_filters(uid, data["low"], data["high"], choice == "local")
                self.db.clear_session(uid)
                await self.s.send(uid, tr(lang, "filters_saved"))
                await self.s.menu(uid)
                return
            if choice == "keep" and data.get("editing") and step in STEPS[1:-1]:
                if step in {"purpose", "interests"}:
                    # In multi-select, Keep means the saved value, even after toggles.
                    data[step] = self.db.user(uid)[step]
            elif step == "consent" and choice == "agree":
                data["consent"] = 1
            elif step == "name":
                data["name"] = clean_text(text, 1, 40)
            elif step == "age":
                data["age"] = int(text)
                if not 18 <= data["age"] <= 100:
                    raise ValueError()
            elif step == "gender" and choice in GENDERS:
                data["gender"] = choice
            elif step == "seeking" and choice in PREFERENCES:
                data["seeking"] = choice
            elif step == "city":
                data["city"] = clean_text(text, 2, 60)
            elif step == "purpose" and choice in PURPOSES:
                data["purpose"] = choice
            elif step == "interests":
                selected = read_interests(data.get("interests"))
                if choice in {"done", "skip"}:
                    data["interests"] = [] if choice == "skip" else selected
                elif choice and choice.startswith("interest_"):
                    key = choice.removeprefix("interest_")
                    if key not in INTERESTS:
                        raise ValueError()
                    if key in selected:
                        selected.remove(key)
                    elif len(selected) < MAX_INTERESTS:
                        selected.append(key)
                    else:
                        await self.s.send(uid, tr(lang, "interests_limit"))
                    data["interests"] = selected
                    await self.prompt(uid, "interests", data)
                    return
                else:
                    raise ValueError()
            elif step == "bio":
                if not text and choice != "skip":
                    raise ValueError()
                data["bio"] = "" if choice == "skip" else clean_text(text, 1, 500)
            elif step == "photo" and message and message.photo:
                data["photo"] = message.photo[-1].file_id
            elif step == "preview" and choice == "publish":
                self.db.save_profile(uid, data)
                await self.s.send(uid, tr(lang, "saved"))
                await self.s.menu(uid)
                return
            else:
                raise ValueError()
        except (ValueError, TypeError):
            await self.s.send(uid, tr(lang, "age_invalid" if step == "age" else "invalid"))
            return
        await self.prompt(uid, STEPS[STEPS.index(step) + 1], data)
