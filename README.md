# TrustMe Knowledge Bot — Gemini Edition

Telegram bot powered by Gemini 1.5 Pro. Processes links, files and text into structured knowledge base documents.

## Setup in 4 steps

### 1. Create Telegram bot
Open @BotFather → /newbot → copy Bot Token

### 2. Get Gemini API key
Go to aistudio.google.com → Get API key → copy it
(Free tier: 15 requests/min, 1M tokens/day — enough for the team)

### 3. Get your Telegram User ID
Open @userinfobot → /start → copy your ID

### 4. Deploy to Railway
- Push this folder to GitHub
- railway.app → New Project → GitHub repo
- Add Variables:
  TELEGRAM_BOT_TOKEN = ...
  GEMINI_API_KEY     = ...
  ALLOWED_USER_IDS   = den_id,chingiz_id

## Usage

| Send | Result |
|------|--------|
| URL | Fetches + analyses article |
| File (PDF/TXT) | Extracts + analyses text |
| Text | Analyses directly |
| /skip | Skip focus question |
| importance 8 | Edit importance score |
| credibility 6 | Edit credibility score |
| confirm | Save with edited scores |
| /status | Knowledge base stats |
| /list | Recent documents |

## Phase 2 — Google Drive auto-upload
Add: GOOGLE_CREDENTIALS_JSON + GOOGLE_DRIVE_FOLDER_ID
Bot will auto-create Docs and update INDEX files.
