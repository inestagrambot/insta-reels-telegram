# 🎬 Instagram Reels → Telegram

A **100% free** automated Instagram Reels scraper that runs on **GitHub Actions** and posts new
Reels to **Telegram**, with a **Telegram Mini App** GUI for managing the monitored pages.

```
pages.json  ─────►  main.py  ─────►  Telegram chat
(managed by the      (GitHub Action,       (video file, or a link
 Mini App)            every 6 hours)        when the video is too big)
                          │
                          └──►  history.json  (dedup, auto-committed back)
```

---

## ✨ Features

| | |
|---|---|
| 💸 Cost | Runs entirely on free tiers (GitHub Actions + Telegram Bot API) |
| 🕒 Schedule | Every 6 hours via cron, plus manual `workflow_dispatch` |
| 🧠 Smart fallback | Video upload → link message if download fails / file ≥ 49 MB |
| ♻️ Dedup | `history.json` prevents re-sending the same Reel |
| 🛡️ Rate-limit friendly | Max 4 new Reels per page per run (`MAX_NEW_PER_PAGE`) |
| 🧯 Bulletproof | Per-page *and* per-reel `try/except`; one failure never stops the run |
| 📱 Mini App | Add/remove Instagram pages from inside Telegram |

---

## 🚀 Setup (5 minutes)

### 1. Create the Telegram bot and get the chat id

1. Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → copy the **bot token**.
2. Send any message (e.g. `/start`) to your new bot from your account.
3. Get your chat id — open
   `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
   and read `result[0].message.chat.id`.

### 2. Add the repository secrets

**Settings → Secrets and variables → Actions → New repository secret**

| Secret | Required | Value |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | the token from BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | your numeric chat id |
| `IG_SESSIONID` | optional but recommended | `sessionid` cookie from a logged-in Instagram session |
| `IG_USERNAME` | optional | the Instagram account that owns that session |

> **Why `IG_SESSIONID`?** GitHub's runner IPs are datacenter IPs, so Instagram often shows a
> login wall or serves an empty Reels tab to anonymous requests. Adding a `sessionid` cookie
> makes the scraper far more reliable. Grab it from your browser devtools
> (Application → Cookies → `https://www.instagram.com` → `sessionid`) after logging in.
> Treat it like a password — it is a full session token.

### 3. Enable Actions

**Settings → Actions → General → Workflow permissions** → select
**Read and write permissions** (needed so the workflow can commit `history.json`).

Then open the **Actions** tab → *Instagram Reels → Telegram* → **Run workflow** for a first
manual test.

### 4. Publish the Mini App (GitHub Pages)

1. **Settings → Pages** → *Source*: **Deploy from a branch** → *Branch*: **main** / **/ (root)** → **Save**.
2. Your control panel is then available at
   `https://<your-username>.github.io/<repo-name>/`.
3. In Telegram talk to [@BotFather](https://t.me/BotFather) → `/mybots` → pick your bot →
   **Bot Settings → Menu Button → Edit menu button URL** → paste the Pages URL.

### 5. Use the Mini App

Open the bot → tap the menu button. Fill in:

* **GitHub owner** — your username,
* **Repository name** — the repo this code lives in,
* **Branch** — usually `main`,
* **GitHub token** — a *fine-grained* PAT with **Contents: Read and write** on this repo,
  plus **Actions: Read and write** if you want the **Run scraper now** button.

Then press **Save settings** → **Add** your first Instagram handle → **Run scraper now**.

> 🔐 The token is stored only in **Telegram CloudStorage / localStorage on your device**.
> It is never written into `pages.json` or any other file in the repository.

---

## 🗂 Files

| File | Purpose |
|---|---|
| `main.py` | Scraper + Telegram delivery |
| `pages.json` | `["instagram"]` — the monitored usernames, edited by the Mini App |
| `history.json` | `{}` — processed Reel ids per username (auto-committed) |
| `index.html` | Telegram Mini App control panel (GitHub Pages) |
| `requirements.txt` | `requests`, `yt-dlp`, `instaloader` |
| `.github/workflows/scraper.yml` | Cron + manual automation |

---

## ⚙️ Tunables (workflow `env:`)

| Variable | Default | Meaning |
|---|---|---|
| `MAX_NEW_PER_PAGE` | `4` | Max new Reels handled per page, per run |
| `TELEGRAM_MAX_MB` | `49` | Upload ceiling (Telegram bot limit is 50 MB) |
| `HISTORY_LIMIT` | `800` | Ids kept per page in `history.json` |
| `DOWNLOAD_TIMEOUT` | `180` | Seconds allowed for a direct CDN download |
| `SEND_RUN_SUMMARY` | `new` | `always` / `new` / `never` for the end-of-run summary |

---

## 🧰 Troubleshooting

* **No Reels found / empty list** → add the `IG_SESSIONID` secret; datacenter IPs are usually
  the reason. Public accounts work best.
* **Only link messages, never videos** → the video exceeded 49 MB, or Instagram blocked the
  media download. The link fallback is intentional.
* **`Bad credentials` in the Mini App** → the PAT is wrong/expired or lacks *Contents: write*.
* **Workflow did nothing on schedule** → GitHub disables cron on inactive repositories after
  ~60 days; open the Actions tab and re-enable it.
* **`history.json` not committed** → make sure workflow permissions are *Read and write*.

---

## ⚠️ Security & compliance

* Never commit tokens: the repo is public, and GitHub **auto-revokes** leaked `ghp_` tokens.
* Scraping Instagram may conflict with its Terms of Service and with the copyright of the
  creators whose Reels are reposted. Keep the monitored list small, private and lawful —
  this project is intended for personal monitoring (e.g. brands you own, or your own account).
* Rotate the bot token (`/revoke` in BotFather) and your PAT regularly.
