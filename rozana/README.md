# Rozana: a planner that follows your actual day

A Telegram bot. You tell it when you wake up, and it plans the rest of the day
around your classes and your goals. When something changes (late wake-up, a
movie with friends, a cancelled class), it rebuilds the plan. After each block
it asks whether you did it, using one-tap buttons.

The planning logic is plain rules, so the bot works with no AI. Gemini is
optional. It reads free-form messages it doesn't recognise and timetable photos.

---

## 1. Run it on your Oracle VM (about 10 minutes)

**a. Create the bot.** In Telegram, open @BotFather, send `/newbot`, and copy the token.

**b. Copy the folder to the VM** (run this from your laptop):
```bash
scp -r rozana ubuntu@automatedfinance.duckdns.org:/home/ubuntu/
```

**c. Install it on the VM:**
```bash
ssh ubuntu@automatedfinance.duckdns.org
cd ~/rozana
sudo apt-get install -y python3-venv
python3 -m venv venv
venv/bin/pip install -r requirements.txt pytest
venv/bin/python -m pytest -q tests       # should say "8 passed"
cp .env.example .env && nano .env        # paste TELEGRAM_TOKEN, optionally GEMINI_API_KEY
```

**d. Keep it running 24/7 and restart it after reboots:**
```bash
sudo cp rozana.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rozana
journalctl -u rozana -f                  # live logs, Ctrl+C to exit
```

**e. Claim it.** Send `/start` to your bot. The first chat that does this becomes
the owner, and the bot ignores everyone else. To be safe, put your chat ID in
`OWNER_CHAT_ID`. The logs print it the first time you send `/start`.

**f. Set it up once:**
- Send a photo of your timetable (needs the Gemini key). Or add classes one by one:
  `/addclass mon 10:00 11:30 Operations`
- Check `/goals` and adjust anything: `/goal Exercise min 40 ideal 60 window morning`
- Set your usual bedtime: `/bed 01:00`
- Lunch (1:00–1:45) and dinner (8:30–9:15) are preloaded as daily blocks. Change
  them with `/timetable`, `/delclass`, and `/addclass`.

Update later: copy the new files over, then run `sudo systemctl restart rozana`.
Your data is in `rozana.db`, which the update doesn't touch. Back it up now and then.

---

## 2. Daily use

| You send | What happens |
|---|---|
| `up` | Plans from now until bedtime |
| `movie 8 to 11`, `football 6-7pm`, `dinner at 9 for 2h` | Adds the event and re-plans. A movie counts as Fun, so Fun drops off the plan |
| `OB moved to 3 to 4` / `cancel OB` | Changes today's class only |
| `sleep at 12:30` | Sets tonight's bedtime and re-plans |
| `did gym 40` | Logs something you did off-plan |
| `sick today` / `travelling` / `off day` | Excused day: no targets, no debt. Has an Undo button |
| `/plan`  `/week`  `/goals`  `/timetable`  `/events` | Views |
| `/sprint Test prep 180 until thu` | Short-term goal that gets top priority until its end date |
| `/retire Fun` | Stops a goal. Its history is kept |

The bot messages you 5 minutes before each block and asks
**✅ Done / 🌓 Half / ⏰ Later / ⏭ Skip** when the block ends. Twenty minutes
before bedtime it sends a wrap-up of the day and your week.

---

## 3. How it works

```
Telegram message ─▶ parse.py ─▶ core.py ─▶ planner.py ─▶ plan saved in db.py ─▶ reminders (bot.py)
                     (rules)     (logic)    (the brain)
                       │
                       └─ not understood? ─▶ ai.py (Gemini) rewrites it into a simple form ─▶ parse.py again
```

**planner.py** is the brain. It's about 150 lines of plain Python:
1. Free time = now until bedtime, minus classes, meals, and events, with a 10-minute gap after each.
2. **Budget first.** Every goal gets its *minimum*, highest priority first. Spare
   time then raises goals toward their *ideal*, also by priority. Fun is last in
   line, so it's the first thing squeezed on a tight day.
3. **Then place.** Each goal's total goes into the slot that best matches its
   preferred window (Exercise prefers evening), split into sessions of at most
   about 90 minutes with 5-minute breaks between them.
4. Any minimum that still doesn't fit shows up as "Didn't fit today". It isn't
   marked as a failure.

**The catch-up rule** is in `core.py`, in `carry()`:
- If you slack on a minimum, **half** of it comes back as catch-up.
- Catch-up is capped at **+30 minutes per goal per day**.
- Debt is forgotten after **7 days**, and time you do above the minimum pays it off.
- Sick, travel, and off days are excused, but only if you say so on the day.
- Fun never creates debt.

**What the AI does and doesn't do.** It only *translates*. A message like
"bro got dragged to a party, back by 11" becomes `Party 20:00 to 23:00`. The
rule parser then handles it, and you see "🤖 Read as: …" so you can catch mistakes.
The AI never changes your plan directly.

**Time is stored in UTC.** Every moment is saved in UTC and shown in `ROZANA_TZ`.
If you move countries, you change that one setting. Old history stays correct.

**Long polling instead of a webhook.** The bot asks Telegram for new messages
rather than Telegram calling your server, so there's no SSL certificate that can
expire and break it. That's the same webhook problem that broke the finance bot.

---

## 4. Tweak the rules

All the knobs are at the top of `core.py` and `planner.py`:
`DEBT_SHARE`, `DEBT_CAP`, `DEBT_WINDOW`, `DAY_CUTOFF_H` (when a "day" ends,
4am by default), `GAP_AFTER_FIXED`, `BREAK_AFTER_ITEM`, and the time `WINDOWS`.
After changing anything, run `python -m pytest -q tests`. The tests simulate real
days: a noon wake-up with class and a movie, a missed morning, a sick day
followed by slacking, and more.
