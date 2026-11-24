"""Telegram side: receives messages and button taps, sends reminders.

Uses long polling (the bot asks Telegram for new messages), so there is no
webhook and no SSL certificate to expire.

Env vars: TELEGRAM_TOKEN (required), OWNER_CHAT_ID (optional, else the first
person to send /start becomes the owner), GEMINI_API_KEY (optional),
ROZANA_DB (default rozana.db), ROZANA_TZ (default Asia/Kolkata).
"""
from __future__ import annotations

import html
import logging
import os
from datetime import timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from . import ai
from .core import App, Reply
from .db import DB

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("rozana")
for noisy in ("apscheduler", "httpx"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

db = DB(os.getenv("ROZANA_DB", "rozana.db"))
app = App(db, tz=os.getenv("ROZANA_TZ", "Asia/Kolkata"))
if os.getenv("OWNER_CHAT_ID"):
    db.set("owner", os.getenv("OWNER_CHAT_ID"))


def owner() -> int | None:
    v = db.get("owner")
    return int(v) if v else None


def markup(r: Reply):
    if not r.buttons:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in r.buttons])


async def send(ctx: ContextTypes.DEFAULT_TYPE, r: Reply):
    await ctx.bot.send_message(owner(), r.text, parse_mode=ParseMode.HTML, reply_markup=markup(r))


def reschedule(jq):
    """Drop all pending reminders and rebuild them from the current plan."""
    for j in jq.jobs():
        if j.name and (j.name.startswith("blk:") or j.name == "wrap"):
            j.schedule_removal()
    now = app.now()
    for when, kind, bid in app.due_jobs():
        jq.run_once(job_block, when=max(when, now + timedelta(seconds=2)),
                    data=(kind, bid), name=f"blk:{kind}:{bid}")
    day = app.day_of(now)
    if app.day_row(day)["wake_utc"]:
        wrap_at = app.bedtime(day) - timedelta(minutes=20)
        if wrap_at > now:
            jq.run_once(job_wrap, when=wrap_at, name="wrap")


async def job_block(ctx: ContextTypes.DEFAULT_TYPE):
    kind, bid = ctx.job.data
    if kind == "remind":
        t = app.remind_text(bid)
        if t:
            await ctx.bot.send_message(owner(), t, parse_mode=ParseMode.HTML)
    else:
        r = app.checkin(bid)
        if r:
            await send(ctx, r)


async def job_wrap(ctx: ContextTypes.DEFAULT_TYPE):
    await ctx.bot.send_message(owner(), app.wrapup(), parse_mode=ParseMode.HTML)


def allowed(update: Update) -> bool:
    return owner() is not None and update.effective_chat.id == owner()


async def on_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if owner() is None:
        db.set("owner", update.effective_chat.id)
        log.info("owner set to %s", update.effective_chat.id)
    if not allowed(update):
        return
    r = app.command("help", "")
    await update.message.reply_text(r.text, parse_mode=ParseMode.HTML)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    text = update.message.text or ""
    r = app.handle(text)
    if r.text.startswith("I didn't get that") and ai.enabled():
        line = await ai.rewrite(text, app.now().strftime("%a %H:%M"))
        if line:
            r2 = app.handle(line)
            if not r2.text.startswith("I didn't get that"):
                r2.text = f"🤖 Read as: <i>{html.escape(line)}</i>\n" + r2.text
                r = r2
    await update.message.reply_text(r.text, parse_mode=ParseMode.HTML, reply_markup=markup(r))
    reschedule(ctx.job_queue)


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    if not ai.enabled():
        await update.message.reply_text("Photo reading needs GEMINI_API_KEY. Meanwhile: /addclass mon 10:00 11:30 Operations")
        return
    await update.message.reply_text("Reading your timetable…")
    f = await update.message.photo[-1].get_file()
    img = bytes(await f.download_as_bytearray())
    try:
        lines = await ai.read_timetable(img)
    except Exception as e:  # network / quota
        log.warning("timetable read failed: %s", e)
        lines = []
    if not lines:
        await update.message.reply_text("Couldn't read it. Try a sharper, straight-on photo, or use /addclass.")
        return
    db.set("pending_timetable", "\n".join(lines))
    preview = "\n".join(html.escape(l) for l in lines[:40])
    r = Reply(f"<b>Here's what I read:</b>\n{preview}\n\nSave this as your weekly timetable? "
              "(replaces saved classes, keeps meals)",
              [[("✅ Save", "tt_save"), ("✖️ Cancel", "tt_cancel")]])
    await send(ctx, r)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not allowed(update):
        await q.answer()
        return
    await q.answer()
    r = app.button(q.data)
    try:
        await q.edit_message_reply_markup(None)       # remove buttons once used
    except Exception:
        pass
    await send(ctx, r)
    reschedule(ctx.job_queue)


async def on_boot(application: Application):
    if owner():
        reschedule(application.job_queue)
        log.info("reminders restored")


def main():
    token = os.environ["TELEGRAM_TOKEN"]
    application = Application.builder().token(token).post_init(on_boot).build()
    application.add_handler(CommandHandler("start", on_start))
    application.add_handler(MessageHandler(filters.PHOTO, on_photo))
    application.add_handler(MessageHandler(filters.TEXT, on_text))   # includes /plan, /week etc.
    application.add_handler(CallbackQueryHandler(on_button))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
