# Doubtly: AI study helper for students everywhere

Type a chapter, add a PDF, link or lecture recording. Get notes, a concept map, flashcards, a quiz, a learning game and a podcast, or ask a question and get it solved step by step.

## Run it on your computer

1. Get a free Gemini API key at https://aistudio.google.com/apikey
2. In this folder:
   ```bash
   python -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   cp .env.example .env             # Windows: copy .env.example .env
   ```
3. Open `.env` and paste your key after `GEMINI_API_KEY=`
4. Start the server:
   ```bash
   uvicorn app:app --reload
   ```
5. Open http://127.0.0.1:8000

## Deploy for free (so students can use it)

1. Push the folder to a **private** GitHub repo (`.env` is already git-ignored).
2. On https://render.com create a **Web Service** from the repo.
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
   - Add environment variable `GEMINI_API_KEY`.
3. Share the link in student WhatsApp / Telegram groups.

Free tiers change and may sleep when idle, so check each provider's current terms.

## PDF size

PDFs up to 80 MB work. Small ones go straight to the AI; big ones are uploaded to Google first and deleted right after your answer is made. Free hosts have little memory, so if a 80 MB upload crashes your server, lower `MAX_PDF_BYTES` in `app.py`.

## Accounts (sign up and log in)

Open http://127.0.0.1:8000/login. Passwords are stored only as salted hashes. Accounts live in a small file called `doubtly.db` next to `app.py` (it is git-ignored). Logged-in students get a higher hourly limit (`PER_USER_LIMIT`, default 30).

On free hosts like Render the disk is wiped on every redeploy, so accounts would disappear. Before you launch for real, move accounts to a hosted database such as Supabase. Password reset by email is not built yet.

## How the pages work

1. On the home page (`/`) the student types a chapter (or adds a PDF, link or lecture recording) and presses **Make study pack**. This is the only time the AI builds the notes, concept map, flashcards, quiz, game and podcast, and it counts as one use.
2. The pack is saved in the student's browser. A "ready" card then links to each part.
3. Each part has its own page that simply shows it, with no typing and no Make button: `/notes`, `/concept-map`, `/flashcards`, `/quiz`, `/play`, `/podcast`. Opening them costs nothing.
4. `/solve-doubt` is the one other page with a form, because a doubt needs a question. The Doubtly AI chat on the right also solves doubts.

If a student opens a page before making a pack, it tells them to make one first. The pages are generated, so if you change the text or style, change `static/index.html` and copy the change into the other pages (or ask Claude Code to do it).

## Doubtly AI chat (right side)

A chat panel with **Doubtly AI** sits on the right of the page (a button in the corner on phones). Students type or speak any doubt and it solves it step by step, with buttons to copy, listen, simplify, get an example or a practice question. The chat is saved in their browser, and a New button starts a fresh one. After you make a study pack, it also knows what the student is studying. Chat messages have their own daily limits (`CHAT_GUEST_PER_DAY` 5, `CHAT_FREE_PER_DAY` 15, `CHAT_PRO_PER_DAY` 100), separate from the monthly study-pack uses.

## Plans and Pro (free uses, then pay)

- Free account: **7 uses a month** (`FREE_MONTHLY_USES`). Guests without an account: 2 tries a month. Uses come back on the 1st.
- Doubtly Pro: **₹49 a month in India, $5 a month elsewhere**, with 200 uses a month. Prices are only shown on the page (`PRICE_INR`, `PRICE_USD`).
- One use is one study pack, game, quiz, notes, map, flashcards, podcast or solved doubt. Failed tries are not counted.
- Open http://127.0.0.1:8000/upgrade to see the Pro page.

### Taking payments
Payments are **not switched on by default**, so nobody can get Pro for free. To start:
1. An adult (a parent or guardian) creates the payment account, because payment companies need KYC. Use **Razorpay** for rupees and **Stripe** for dollars.
2. In each dashboard, create a **Payment Link** for a monthly payment (₹49 and $5).
3. Put the two links in `.env` as `PAYMENT_LINK_INR` and `PAYMENT_LINK_USD`, then restart. The Upgrade button now sends students to those links.
4. When a payment arrives, give that student Pro by running `python grant_pro.py their@email.com`. This is manual, and fine for your first customers. Automatic upgrades (a webhook) are the next job for Claude Code.

To test the Pro flow on your own computer, put `DEV_FREE_UPGRADE=1` in `.env`. **Never use that online.**

## Cost protection (already built in)

- `PER_IP_LIMIT`: requests per hour for guests (default 10), on top of the monthly limits
- `GLOBAL_DAILY_LIMIT`: total requests per day across all users (default 400), so a viral spike can't exhaust your free quota or run up a bill

Limits are kept in memory and reset when the server restarts. That is fine for a first version. Move them to a database when you add accounts.

## Next steps toward a business

1. Get 20 students to use it and note which subject they use most.
2. Add login and saved history (Supabase free tier).
3. Add a paid tier (₹49 to ₹149/month, with higher limits) via Razorpay or Cashfree. If you are under 18, you will need a parent or guardian to set up payments.
4. Specialize: pick one exam or subject and make that experience the best.

## Notes

- AI answers can be wrong. The footer says so, and you should keep it.
- Don't store students' photos or personal data you don't need.
- Never commit your API key.

## Password reset
On the log in page, "Forgot your password?" asks for an email and makes a link that works for 1 hour (one use only).
- On your own computer, with no email settings, the link is printed in the black window where the app runs. Copy it into the browser.
- To send real emails, fill in `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` and `PUBLIC_URL` in `.env`.
- Resetting logs the person out of every other device.

## New in this version
- **Exam plan** (`/exam-plan`): enter the exam date and topics, get a day-by-day checklist. Ticks are remembered.
- **Share a pack**: the "Share this pack" button on the home page makes a link like `/?pack=abc123`. Friends open it and get the notes, quiz, game and more. Links last 90 days.
- **Photo of a question or page**: the "Add a photo" button (PNG, JPG or WEBP, up to 8 MB) works on every page.
- **Telegram bot** (`telegram_bot.py`) and **Postgres accounts** (`DATABASE_URL`), explained below.

## Big chat study (`/study-room`)
Friends on different phones or computers join one room and race through the same quiz.
- One person picks a study pack that has a quiz and presses **Create room**. They get a 6-letter code and an invite link.
- Friends type the code (or open the link) and their name. Up to 50 people per room. Rooms close after 24 hours.
- Each right answer is **10 points**, plus **5 bonus points** for being the first in the room to get that question right. The leaderboard and the room chat update live on every device.
- The right answers stay on the server until a player has answered, so nobody can peek.
- Rooms are private: there is no public list, so only people with the code can join. Tell students not to share the code with strangers. Chat messages are plain text and slow down if someone spams.
- Works with SQLite on your computer and with Postgres online (`DATABASE_URL`).


## Group chat and Friends (Pro only)

Open `/groups` (the 👥 **Group chat** tab). It is for **Pro members only**: a guest is asked to log in, and a free account is asked to upgrade.

**Group chat**
- Make a group, get a 6-letter code (or an invite link), and friends with Pro join with it.
- A group holds up to 100 people and keeps its last 500 messages. A group with no messages for 90 days is removed.
- Messages are plain text only, with a limit on how fast one person can send. It checks for new messages every 2.5 seconds.
- The Pro check runs on every call, so if someone's Pro ends they are locked out of their groups.

**Friends** (the 👫 button at the top of the page)
- Every Pro member has a **friend code** like `ABCD-2345`. It is made by Doubtly. It is NOT an IP address (IP addresses change, are shared by whole schools and are private).
- Type a friend's code and press Send request. They see a red number on the Friends button and press Accept (or Decline). You can cancel a request you sent, and remove a friend later.
- If two people send each other a request, they become friends straight away.
- People only see each other's name, never email. Code guessing is limited to 20 tries an hour.
- A free account can receive a request, but only sees it after upgrading to Pro.

Groups and friends are saved in the same database as everything else, so use `DATABASE_URL` (Postgres) online or they disappear when Render restarts. To try it on your own computer without paying, set `DEV_FREE_UPGRADE=1` in `.env` and use the Upgrade page (never do this online).

## World leaderboard (`/leaderboard`)
The people who study the most this month, from anywhere in the world.
- Only logged-in people earn points: **10** for each study pack, exam plan or solved doubt, and **1** for each Doubtly AI chat answer. A failed answer earns nothing.
- On the 1st of each month the top 4 of the month before get bonus free tries added to the new month: **1st = 100, 2nd = 75, 3rd = 50, 4th = 25**. You need at least 20 points to win. Change this with `LB_REWARDS` (for example `100,75,50,25`) and `LB_MIN_POINTS` in `.env`.
- Ties go to whoever reached the points first. Everyone can hide their name (they then show as "Student ####"). Emails are never shown.
- Free accounts have limited uses, so Pro members (200 uses) will usually top the board. That is also a good reason to upgrade.

## Put it online for free (Render)
Netlify cannot run this app, because Doubtly needs a Python server. Use Render instead.
1. Make a free account at github.com, then a new **private** repository, and upload everything in this folder. Do NOT upload `.env` or `doubtly.db` (the `.gitignore` already skips them).
2. Make a free account at render.com. Choose New > Blueprint (or Web Service), connect your GitHub repository, and pick the free plan.
3. When it asks for `GEMINI_API_KEY`, paste your Google key. Leave `PUBLIC_URL` empty for now.
4. After the first deploy your link looks like `https://doubtly-ai.onrender.com`. Open it.

### Keep accounts safe: free Postgres
Render's free plan wipes `doubtly.db` every time the site sleeps or restarts. To keep accounts, Pro upgrades and shared packs:
1. Make a free Postgres database with a provider such as Neon or Supabase (check their current free plan).
2. Copy its connection address (it starts with `postgresql://`).
3. In Render, open your service, then Environment, and add `DATABASE_URL` with that address. Redeploy.
The tables are created automatically. On your own computer you can leave `DATABASE_URL` empty and it uses the `doubtly.db` file.

### Reset emails on the free plan
Render blocks the normal email ports on the free plan. Use Brevo (free plan) instead: verify a sender email there, make an API key, then set `BREVO_API_KEY` and `SMTP_FROM` (the verified email) and `PUBLIC_URL` (your Render link) in Render's Environment.

### Telegram bot
1. In Telegram, message @BotFather, send `/newbot`, and follow the questions. It gives you a token.
2. Put it in `.env` as `TELEGRAM_BOT_TOKEN=...`.
3. Run `python telegram_bot.py` and leave the window open. Message your bot. 30 messages a day per person (`TELEGRAM_PER_DAY`).
The bot must keep running to answer, so it needs its own always-on place to run when you go live.

Free plan limits (from Render's docs):
- The site sleeps after 15 minutes with no visitors and takes about a minute to wake up.
- Without `DATABASE_URL`, accounts disappear whenever it sleeps or restarts.
- Outgoing email ports are blocked, so use Brevo for reset emails.
