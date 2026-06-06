# 📢 BDcirculars Telegram Bot

An automated bot that fetches posts from a Facebook page via RSS and publishes them to a Telegram channel — including images, smart caption splitting, and overflow content posted as comments.

Runs daily via **GitHub Actions**. Zero maintenance once set up.

---

## ✨ Features

- 📷 **Images only** — skips text-only posts
- 🖼️ **Media groups** — posts up to 10 images per entry as an album
- ✂️ **Smart caption split** — if caption exceeds Telegram's 1024-char limit, splits at a natural linebreak and posts the rest as a comment
- 💬 **Comment overflow** — extra images (beyond 10) are posted as replies in the discussion group
- 🗂️ **Seen-items tracking** — never reposts the same entry; file stays small by auto-trimming old hashes
- 📩 **DM report** — sends you a summary (posted / skipped / errors) after every run
- ⏰ **Scheduled via GitHub Actions** — runs once daily, no server needed

---

## 🗂️ Project Structure

```
.
├── rss-to-telegram-bot.py     # main bot script
├── requirements.txt           # Python dependencies
├── seen_items.json            # tracks already-posted entries (auto-updated)
└── .github/
    └── workflows/
        └── rss_bot.yml        # GitHub Actions workflow
```

---

## ⚙️ Setup

### 1. Fork / clone this repo

### 2. Create a Telegram bot
- Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token
- Add the bot as **Administrator** in your channel
- Link a **Discussion Group** to the channel (Channel Settings → Discussion)
- Add the bot as a member of the discussion group

### 3. Get your IDs
| What | Value |
|---|---|
| Bot token | From [@BotFather](https://t.me/BotFather) |
| Channel ID | `@yourchannel` or numeric ID |
| Group ID | `@yourgroup` numeric ID |
| Your User ID | `@yourprofile` or numeric ID |

- You can use [@userinfobot](https://t.me/userinfobot) to get your User/Channel/Group ID easily.

### 4. Add GitHub Secrets
Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**

| Secret | Value |
|---|---|
| `TG_BOT_TOKEN` | Bot token from BotFather |
| `TG_CHANNEL_ID` | Your channel ID |
| `TG_GROUP_ID` | Linked discussion group ID |
| `TG_ADMIN_ID` | Your personal Telegram user ID |
| `RSS_URL` | Your feed URL |

### 5. Create an empty seen_items.json
```bash
echo "[]" > seen_items.json
git add seen_items.json
git commit -m "init seen_items"
git push
```

### 6. Trigger a test run
Go to **Actions → RSS Telegram Bot → Run workflow** to test before waiting for the schedule.

---

## 🕐 Schedule

The bot runs daily at **12:00 PM Bangladesh Standard Time** (UTC+6 → `0 6 * * *`).

To change the time, edit `.github/workflows/rss_bot.yml`:
```yaml
- cron: "0 6 * * *"   # 12:00 PM BST
```
Use [crontab.guru](https://crontab.guru) to build your preferred schedule.

---

## 📦 Dependencies

```
python-telegram-bot==21.*
feedparser>=6.0
requests>=2.31
beautifulsoup4>=4.12
lxml>=5.0
```

Install locally:
```bash
pip install -r requirements.txt
```

---

## 🔒 Environment Variables

| Variable | Required | Description |
|---|---|---|
| `TG_BOT_TOKEN` | ✅ | Telegram bot token |
| `TG_CHANNEL_ID` | ✅ | Target channel ID |
| `TG_GROUP_ID` | optional | Linked discussion group ID (for comments) |
| `TG_ADMIN_ID` | optional | Your Telegram user ID (for DM reports) |
| `RSS_URL` | ✅ | RSS feed URL |

---

## 🤖 How It Works

```
GitHub Actions (cron)
        ↓
Fetch RSS feed
        ↓
Filter out already-seen entries  (seen_items.json)
        ↓
For each new entry:
  → Skip if no images
  → Post photos to channel
  → If caption too long → split smartly → post remainder as comment
  → If images > 10 → post overflow as comment
        ↓
Trim seen_items.json to current feed size
        ↓
Commit seen_items.json back to repo
        ↓
Send DM report to admin
```

---

## 📄 License

MIT
