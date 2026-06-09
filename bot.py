import os
import re
import json
import logging
import tempfile
import requests
from datetime import datetime

from groq import Groq
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, ConversationHandler
)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_KEY        = os.environ["GROQ_API_KEY"]
APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL", "")
SCRIPT_TOKEN    = os.environ.get("SCRIPT_TOKEN", "TRUSTME_SECRET_2025")
ALLOWED_USERS   = set(os.environ.get("ALLOWED_USER_IDS", "").split(","))

groq_client = Groq(api_key=GROQ_KEY)
MODEL = "llama-3.3-70b-versatile"

WAITING_FOCUS = 1

FOLDERS = {
    "Knowledge/Product":       "Product specs, PRD, roadmap, features, UX",
    "Knowledge/Sharia":        "Sharia standards, fatwa, AAOIFI, IFSB, halal screening",
    "Knowledge/Market":        "Competitors, market research, fintech trends, analysis",
    "Knowledge/Strategy":      "Pitch decks, OKR, fundraising, vision, investors",
    "Knowledge/User_Insights": "User feedback, research, interviews, surveys",
    "Operations/Decisions":    "Team decisions, choices, rationale",
    "Operations/Meetings":     "Meeting notes, agendas, minutes",
    "Operations/Processes":    "SOPs, workflows, instructions, guides",
    "Operations/Agents":       "AI agent instructions and configurations",
    "Operations/HR":           "Team structure, roles, onboarding",
    "Company/Product_Tech":    "Product and technology department docs",
    "Company/Compliance":      "Compliance and Sharia department docs",
    "Company/Banking_Ops":     "Banking operations department docs",
    "Company/Investments":     "Investments and Halal ETF department docs",
    "Company/Marketing":       "Marketing and growth department docs",
    "Company/Finance_IR":      "Finance and investor relations docs",
    "Company/HR":              "HR and org structure docs",
}
FOLDERS_TEXT = "\n".join([f"- {k}: {v}" for k, v in FOLDERS.items()])


def is_allowed(user_id):
    if not ALLOWED_USERS or ALLOWED_USERS == {""}:
        return True
    return str(user_id) in ALLOWED_USERS


def fetch_url_content(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; TrustMeBot/1.0)"}
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        text = re.sub(r"<script[^>]*>.*?</script>", "", resp.text, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>",   "", text,      flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+",     " ", text).strip()
        return text[:40000]
    except Exception as e:
        return f"[Could not fetch URL: {e}]"


def extract_file_content(tmp_path, filename):
    fname = filename.lower()
    try:
        if fname.endswith(".pdf"):
            try:
                import fitz
                doc = fitz.open(tmp_path)
                return "\n".join([p.get_text() for p in doc])[:40000]
            except ImportError:
                pass
        if fname.endswith(".docx"):
            try:
                from docx import Document as DocxDoc
                doc = DocxDoc(tmp_path)
                return "\n".join([p.text for p in doc.paragraphs])[:40000]
            except ImportError:
                pass
        with open(tmp_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(40000)
    except Exception as e:
        return f"[Could not extract content: {e}]"


def process_with_ai(content, source, focus):
    pages  = max(1, len(content.split()) // 250)
    n_tags = "15-20" if pages >= 100 else "10-15" if pages >= 50 else "5-10" if pages >= 10 else "3-5"
    today  = datetime.now().strftime("%Y-%m-%d")

    prompt = f"""You are the NeoBank Knowledge Agent. Process this material and return ONLY valid JSON — no markdown, no explanation.

SOURCE: {source}
USER FOCUS: {focus if focus else "General — extract what is most relevant for TrustMe Islamic Digital Bank"}
DATE: {today}

MATERIAL:
{content[:30000]}

Available folders:
{FOLDERS_TEXT}

Return ONLY this JSON:
{{
  "title": "concise English title",
  "folder": "one folder path from the list",
  "summary": "3-5 sentences focused on TrustMe relevance",
  "key_insights": ["insight 1", "insight 2", "insight 3"],
  "hashtags": [{n_tags} strings like "#topic_sharia" "#type_article" "#dept_product" "#key_halal_etf" "#lang_en"],
  "importance": <1-10>,
  "importance_reason": "one sentence",
  "credibility": <1-10>,
  "credibility_reason": "one sentence",
  "version": "V1.0",
  "date": "{today}"
}}

Rules: importance 9-10 only for Sharia/strategy/foundational; credibility 4-6 for interviews/podcasts/social; 3-7 insights; all English."""

    response = groq_client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"^```\s*",     "", raw)
    raw = re.sub(r"\s*```$",     "", raw)
    return json.loads(raw)


def save_to_drive(data, source, content):
    """Send document to Apps Script which saves it to Drive."""
    if not APPS_SCRIPT_URL:
        return None, "Apps Script URL not configured"

    payload = {
        "token":       SCRIPT_TOKEN,
        "action":      "create_doc",
        "title":       data.get("title", "Untitled"),
        "folder":      data.get("folder", "Knowledge/Market"),
        "content":     f"SUMMARY:\n{data.get('summary','')}\n\nKEY INSIGHTS:\n" +
                       "\n".join([f"{i+1}. {t}" for i,t in enumerate(data.get('key_insights',[]))]) +
                       f"\n\nSOURCE CONTENT:\n{content[:15000]}",
        "source":      source,
        "importance":  data.get("importance", 5),
        "credibility": data.get("credibility", 5),
        "date":        data.get("date", datetime.now().strftime("%Y-%m-%d")),
        "version":     data.get("version", "V1.0"),
        "hashtags":    data.get("hashtags", []),
        "summary":     data.get("summary", ""),
        "key_insights": data.get("key_insights", []),
    }

    try:
        resp = requests.post(APPS_SCRIPT_URL, json=payload, timeout=30)
        logger.info(f"Apps Script response [{resp.status_code}]: {resp.text[:500]}")
        result = resp.json()
        if result.get("ok"):
            return result.get("file_url"), None
        else:
            return None, result.get("error", "Unknown error from Apps Script")
    except Exception as e:
        return None, str(e)


def esc(text):
    """Escape Telegram Markdown (legacy) special chars in dynamic text."""
    if text is None:
        return ""
    for ch in ("_", "*", "`", "["):
        text = str(text).replace(ch, "\\" + ch)
    return text


def format_preview(data, source):
    tags     = "  ".join(data.get("hashtags", []))
    insights = "\n".join([f"  {i+1}. {esc(t)}" for i, t in enumerate(data.get("key_insights", []))])
    imp      = data.get("importance",  0)
    cred     = data.get("credibility", 0)
    return (
        f"📄 *{esc(data.get('title','Untitled'))}*\n\n"
        f"📁 *Folder:* `{data.get('folder','—')}`\n"
        f"📅 {data.get('date','—')}  |  🏷 {data.get('version','V1.0')}\n\n"
        f"─────────────────────\n"
        f"📝 *Summary*\n{esc(data.get('summary','—'))}\n\n"
        f"💡 *Key Insights*\n{insights}\n\n"
        f"🏷 *Hashtags*\n{esc(tags)}\n\n"
        f"─────────────────────\n"
        f"⭐️ *Importance:* {imp}/10  {'🟠'*imp}{'⚪️'*(10-imp)}\n"
        f"_{esc(data.get('importance_reason',''))}_\n\n"
        f"✅ *Credibility:* {cred}/10  {'🔵'*cred}{'⚪️'*(10-cred)}\n"
        f"_{esc(data.get('credibility_reason',''))}_\n\n"
        f"🔗 *Source:* {esc(source)}"
    )


# ── Command handlers ──────────────────────────────────────

async def start(update, context):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔️ Access restricted.")
        return
    await update.message.reply_text(
        "👋 *NeoBank Knowledge Agent*\n_Powered by Groq · Saves to Google Drive_\n\n"
        "Send me:\n"
        "🔗 *Link* — article, YouTube, podcast\n"
        "📎 *File* — PDF, DOCX, TXT\n"
        "💬 *Text* — paste directly\n\n"
        "/status · /list · /help",
        parse_mode="Markdown"
    )

async def help_cmd(update, context):
    await start(update, context)

async def status(update, context):
    if not is_allowed(update.effective_user.id):
        return
    docs  = context.bot_data.get("docs", [])
    total = len(docs)
    if not total:
        await update.message.reply_text("📊 No documents yet. Send your first link!", parse_mode="Markdown")
        return
    avg_imp  = round(sum(d.get("importance",  0) for d in docs) / total, 1)
    avg_cred = round(sum(d.get("credibility", 0) for d in docs) / total, 1)
    folders  = {}
    for d in docs:
        folders[d.get("folder","Unknown")] = folders.get(d.get("folder","Unknown"), 0) + 1
    fl = "\n".join([f"  • `{f}` — {c}" for f, c in sorted(folders.items())])
    drive_status = "✅ Connected" if APPS_SCRIPT_URL else "⚠️ Not configured"
    await update.message.reply_text(
        f"📊 *Knowledge Base*\n\n"
        f"📄 *{total}* documents\n"
        f"⭐️ Avg importance: *{avg_imp}/10*\n"
        f"✅ Avg credibility: *{avg_cred}/10*\n"
        f"🗂 Google Drive: {drive_status}\n\n"
        f"📁 *Folders:*\n{fl}",
        parse_mode="Markdown"
    )

async def list_docs(update, context):
    if not is_allowed(update.effective_user.id):
        return
    docs = context.bot_data.get("docs", [])
    if not docs:
        await update.message.reply_text("No documents yet.")
        return
    lines = []
    for i, d in enumerate(docs[-10:][::-1], 1):
        drive_link = f" · [Drive]({d.get('drive_url','')})" if d.get('drive_url') else ""
        lines.append(
            f"{i}. *{d.get('title','Untitled')}*\n"
            f"   `{d.get('folder','—')}` | ⭐️{d.get('importance','—')} ✅{d.get('credibility','—')} | {d.get('date','—')}{drive_link}"
        )
    await update.message.reply_text(
        "📋 *Recent Documents*\n\n" + "\n\n".join(lines),
        parse_mode="Markdown", disable_web_page_preview=True
    )


# ── Receive material ──────────────────────────────────────

async def receive_message(update, context):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔️ Access restricted.")
        return
    msg = update.message
    if msg.document or msg.audio:
        f = msg.document or msg.audio
        context.user_data.update({"pending_source": f"File: {f.file_name}", "pending_type": "file",
                                   "pending_file_id": f.file_id, "pending_file_name": f.file_name})
    elif msg.voice:
        context.user_data.update({"pending_source": "Voice message", "pending_type": "voice",
                                   "pending_file_id": msg.voice.file_id})
    elif msg.text:
        t = msg.text.strip()
        if re.match(r"https?://\S+", t):
            context.user_data.update({"pending_source": t, "pending_type": "url", "pending_url": t})
        else:
            context.user_data.update({"pending_source": "Direct text", "pending_type": "text", "pending_text": t})
    else:
        await msg.reply_text("Please send a link, file, or text.")
        return
    await msg.reply_text(
        "🎯 *What should I focus on?*\n\nTell me what's important for TrustMe, or /skip for default.",
        parse_mode="Markdown"
    )
    return WAITING_FOCUS


async def receive_focus(update, context):
    if not is_allowed(update.effective_user.id):
        return ConversationHandler.END
    focus = "" if update.message.text.strip().startswith("/skip") else update.message.text.strip()
    await update.message.reply_text("⏳ Analysing...")

    ptype  = context.user_data.get("pending_type")
    source = context.user_data.get("pending_source", "Unknown")
    content = ""

    try:
        if ptype == "url":
            await update.message.reply_text("🌐 Fetching URL...")
            content = fetch_url_content(context.user_data["pending_url"])
        elif ptype == "text":
            content = context.user_data["pending_text"]
        elif ptype in ("file", "voice"):
            tg_file = await context.bot.get_file(context.user_data["pending_file_id"])
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                await tg_file.download_to_drive(tmp.name)
                tmp_path = tmp.name
            if ptype == "voice":
                content = f"[Voice message — transcription coming in future update. File ID: {context.user_data['pending_file_id']}]"
            else:
                content = extract_file_content(tmp_path, context.user_data.get("pending_file_name", "file.txt"))
            os.unlink(tmp_path)

        if len(content) < 50:
            await update.message.reply_text("⚠️ Could not extract content. Try pasting text directly.")
            return ConversationHandler.END

        data = process_with_ai(content, source, focus)
        context.user_data["pending_data"]    = data
        context.user_data["pending_content"] = content

        keyboard = [
            [InlineKeyboardButton("✅ Save to Drive", callback_data="confirm_save"),
             InlineKeyboardButton("✏️ Edit scores",   callback_data="edit_scores")],
            [InlineKeyboardButton("🔄 Change folder", callback_data="change_folder"),
             InlineKeyboardButton("❌ Discard",        callback_data="discard")],
        ]
        preview = format_preview(data, source) + "\n\n─────────────────────\n✅ Save to Google Drive?"
        try:
            await update.message.reply_text(
                preview, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception:
            # Markdown failed — resend as plain text so the buttons still work
            plain = re.sub(r"[*_`\[\]]", "", preview)
            await update.message.reply_text(
                plain, reply_markup=InlineKeyboardMarkup(keyboard)
            )

    except json.JSONDecodeError:
        await update.message.reply_text("⚠️ Processing error. Please try again.")
    except Exception as e:
        logger.error(e)
        await update.message.reply_text(f"⚠️ Error: {str(e)[:200]}")

    return ConversationHandler.END


# ── Callback handlers ─────────────────────────────────────

async def button_callback(update, context):
    query = update.callback_query
    await query.answer()
    pd  = context.user_data.get("pending_data", {})
    pc  = context.user_data.get("pending_content", "")
    src = context.user_data.get("pending_source", "Unknown")

    if query.data == "confirm_save":
        await query.message.reply_text("💾 Saving to Google Drive...")

        drive_url, error = save_to_drive(pd, src, pc)

        title  = pd.get("title", "Untitled")
        folder = pd.get("folder", "Knowledge/Market")

        context.bot_data.setdefault("docs", []).append({
            "title":       title,
            "folder":      folder,
            "importance":  pd.get("importance",  5),
            "credibility": pd.get("credibility", 5),
            "date":        pd.get("date", datetime.now().strftime("%Y-%m-%d")),
            "source":      src,
            "drive_url":   drive_url or "",
        })

        if drive_url:
            await query.message.reply_text(
                f"✅ Saved to Google Drive!\n\n"
                f"📁 {folder}\n"
                f"📄 {title}\n"
                f"🔗 {drive_url}\n\n"
                f"INDEX and CHANGELOG updated.",
                disable_web_page_preview=True
            )
        else:
            await query.message.reply_text(
                f"⚠️ Drive save failed: {error}\n\n"
                f"Document processed but not saved to Drive. Check Apps Script configuration.",
                parse_mode="Markdown"
            )

        await query.edit_message_reply_markup(reply_markup=None)
        context.user_data.clear()

    elif query.data == "discard":
        await query.edit_message_text("❌ Discarded.")
        context.user_data.clear()

    elif query.data == "edit_scores":
        await query.message.reply_text(
            f"✏️ Send: `importance 8` or `credibility 6`\n\n"
            f"Current — ⭐️{pd.get('importance','—')}/10  ✅{pd.get('credibility','—')}/10",
            parse_mode="Markdown"
        )

    elif query.data == "change_folder":
        kb = [[InlineKeyboardButton(f, callback_data=f"folder_{f}")] for f in FOLDERS]
        await query.message.reply_text("📁 *Choose folder:*", parse_mode="Markdown",
                                        reply_markup=InlineKeyboardMarkup(kb))

    elif query.data.startswith("folder_"):
        context.user_data["pending_data"]["folder"] = query.data[7:]
        await query.edit_message_text(f"📁 Folder: `{query.data[7:]}`\n\nSend `confirm` to save.",
                                       parse_mode="Markdown")


async def handle_score_edit(update, context):
    if not is_allowed(update.effective_user.id):
        return
    text = update.message.text.strip().lower()
    pd   = context.user_data.get("pending_data")
    if not pd:
        return
    m = re.match(r"(importance|credibility)\s+(\d+)", text)
    if m:
        val = int(m.group(2))
        if 1 <= val <= 10:
            pd[m.group(1)] = val
            context.user_data["pending_data"] = pd
            await update.message.reply_text(
                f"✅ *{m.group(1).capitalize()}* → *{val}/10*\n\nSend `confirm` to save.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("⚠️ Score must be 1–10.")
    elif text == "confirm":
        src = context.user_data.get("pending_source", "Unknown")
        pc  = context.user_data.get("pending_content", "")
        await update.message.reply_text("💾 Saving to Google Drive...")
        drive_url, error = save_to_drive(pd, src, pc)
        title  = pd.get("title", "Untitled")
        folder = pd.get("folder", "Knowledge/Market")
        context.bot_data.setdefault("docs", []).append({
            "title": title, "folder": folder,
            "importance": pd.get("importance", 5), "credibility": pd.get("credibility", 5),
            "date": pd.get("date", datetime.now().strftime("%Y-%m-%d")),
            "source": src, "drive_url": drive_url or "",
        })
        if drive_url:
            await update.message.reply_text(
                f"✅ Saved!\n📁 {folder}\n📄 {title}\n🔗 {drive_url}",
                disable_web_page_preview=True
            )
        else:
            await update.message.reply_text(f"⚠️ Drive error: {error}")
        context.user_data.clear()


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_message),
            MessageHandler(filters.Document.ALL,             receive_message),
            MessageHandler(filters.VOICE,                    receive_message),
            MessageHandler(filters.AUDIO,                    receive_message),
        ],
        states={WAITING_FOCUS: [MessageHandler(filters.TEXT, receive_focus)]},
        fallbacks=[CommandHandler("skip", receive_focus)],
    )
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("help",   help_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("list",   list_docs))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_score_edit))
    logger.info("NeoBank Knowledge Bot (Phase 2 — Groq + Drive) starting...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
