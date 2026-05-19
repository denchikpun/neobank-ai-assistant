import os
import re
import json
import logging
import tempfile
from datetime import datetime

import google.generativeai as genai
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, ConversationHandler
)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = "8994016838:AAEV3yNgxcWl9eEZ28SYWeJ6v9nYBjoSjHI"
GEMINI_KEY     = os.environ["GEMINI_API_KEY"]
ALLOWED_USERS  = set(os.environ.get("ALLOWED_USER_IDS", "").split(","))

genai.configure(api_key=GEMINI_KEY)
model = genai.GenerativeModel("gemini-1.5-pro")

WAITING_FOCUS = 1

FOLDERS = {
    "Knowledge/Sharia":        "Sharia standards, fatwa, AAOIFI, IFSB, halal screening",
    "Knowledge/Product":       "Product specs, PRD, roadmap, features, UX",
    "Knowledge/Market":        "Competitors, market research, fintech trends, analysis",
    "Knowledge/Strategy":      "Pitch decks, OKR, fundraising, vision, investors",
    "Knowledge/User_Insights": "User feedback, research, interviews, surveys",
    "Operations/Decisions":    "Team decisions, choices, rationale",
    "Operations/Meetings":     "Meeting notes, agendas, minutes",
    "Operations/Processes":    "SOPs, workflows, instructions, guides",
    "Operations/Agents":       "AI agent instructions and configurations",
}
FOLDERS_TEXT = "\n".join([f"- {k}: {v}" for k, v in FOLDERS.items()])


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS or ALLOWED_USERS == {""}:
        return True
    return str(user_id) in ALLOWED_USERS


def fetch_url_content(url: str) -> str:
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


def estimate_pages(text: str) -> int:
    return max(1, len(text.split()) // 250)


def hashtag_count(pages: int) -> str:
    if pages >= 100: return "15-20"
    if pages >= 50:  return "10-15"
    if pages >= 10:  return "5-10"
    return "3-5"


def process_with_gemini(content: str, source: str, focus: str) -> dict:
    pages  = estimate_pages(content)
    n_tags = hashtag_count(pages)
    today  = datetime.now().strftime("%Y-%m-%d")

    prompt = f"""You are the TrustMe Knowledge Agent. Process this material and return ONLY valid JSON — no markdown fences, no explanation.

SOURCE: {source}
USER FOCUS: {focus if focus else "General — extract what is most relevant for TrustMe Islamic Digital Bank"}
ESTIMATED PAGES: {pages}
DATE: {today}

MATERIAL:
{content[:30000]}

Available folders:
{FOLDERS_TEXT}

Return ONLY this JSON:
{{
  "title": "concise English title",
  "folder": "one folder path from the list above",
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

    response = model.generate_content(prompt)
    raw = response.text.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"^```\s*",     "", raw)
    raw = re.sub(r"\s*```$",     "", raw)
    return json.loads(raw)


def format_preview(data: dict, source: str) -> str:
    tags     = "  ".join(data.get("hashtags", []))
    insights = "\n".join([f"  {i+1}. {t}" for i, t in enumerate(data.get("key_insights", []))])
    imp      = data.get("importance",  0)
    cred     = data.get("credibility", 0)
    return (
        f"📄 *{data.get('title','Untitled')}*\n\n"
        f"📁 *Folder:* `{data.get('folder','—')}`\n"
        f"📅 {data.get('date','—')}  |  🏷 {data.get('version','V1.0')}\n\n"
        f"─────────────────────\n"
        f"📝 *Summary*\n{data.get('summary','—')}\n\n"
        f"💡 *Key Insights*\n{insights}\n\n"
        f"🏷 *Hashtags*\n{tags}\n\n"
        f"─────────────────────\n"
        f"⭐️ *Importance:* {imp}/10  {'🟠'*imp}{'⚪️'*(10-imp)}\n"
        f"_{data.get('importance_reason','')}_\n\n"
        f"✅ *Credibility:* {cred}/10  {'🔵'*cred}{'⚪️'*(10-cred)}\n"
        f"_{data.get('credibility_reason','')}_\n\n"
        f"🔗 *Source:* {source}"
    )


def format_doc(data: dict, source: str, content: str) -> str:
    tags     = "  ".join(data.get("hashtags", []))
    insights = "\n".join([f"{i+1}. {t}" for i, t in enumerate(data.get("key_insights", []))])
    sep = "━" * 40
    return (
        f"TITLE: {data.get('title','Untitled')}\n"
        f"SOURCE: {source}\n"
        f"DATE ADDED: {data.get('date','—')}\n"
        f"VERSION: {data.get('version','V1.0')}\n"
        f"FOLDER: {data.get('folder','—')}\n\n{sep}\n\n"
        f"IMPORTANCE: {data.get('importance','—')}/10\n{data.get('importance_reason','')}\n\n"
        f"CREDIBILITY: {data.get('credibility','—')}/10\n{data.get('credibility_reason','')}\n\n{sep}\n\n"
        f"HASHTAGS:\n{tags}\n\n{sep}\n\n"
        f"SUMMARY:\n{data.get('summary','—')}\n\n{sep}\n\n"
        f"KEY INSIGHTS:\n{insights}\n\n{sep}\n\n"
        f"FULL CONTENT:\n{content[:20000]}\n\n{sep}\n\n"
        f"VERSION BLOCK:\n"
        f"VERSION:  {data.get('version','V1.0')}\n"
        f"DATE:     {data.get('date','—')}\n"
        f"AUTHOR:   TrustMe Knowledge Agent (Gemini 1.5 Pro)\n"
        f"CHANGES:  Initial creation\nPREV VER: —\n"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔️ Access restricted.")
        return
    await update.message.reply_text(
        "👋 *TrustMe Knowledge Agent*\n_Powered by Gemini 1.5 Pro_\n\n"
        "Send me:\n"
        "🔗 *Link* — article, YouTube, podcast\n"
        "📎 *File* — PDF, DOCX, TXT\n"
        "🎤 *Voice* — voice message\n"
        "💬 *Text* — paste directly\n\n"
        "/status · /list · /help",
        parse_mode="Markdown"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await update.message.reply_text(
        f"📊 *Knowledge Base*\n\n📄 *{total}* documents\n"
        f"⭐️ Avg importance: *{avg_imp}/10*\n✅ Avg credibility: *{avg_cred}/10*\n\n📁 *Folders:*\n{fl}",
        parse_mode="Markdown"
    )

async def list_docs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    docs = context.bot_data.get("docs", [])
    if not docs:
        await update.message.reply_text("No documents yet.")
        return
    lines = [
        f"{i}. *{d.get('title','Untitled')}*\n   `{d.get('folder','—')}` | ⭐️{d.get('importance','—')} ✅{d.get('credibility','—')} | {d.get('date','—')}"
        for i, d in enumerate(docs[-10:][::-1], 1)
    ]
    await update.message.reply_text("📋 *Recent Documents*\n\n" + "\n\n".join(lines), parse_mode="Markdown")


async def receive_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await msg.reply_text("Please send a link, file, voice, or text.")
        return
    await msg.reply_text(
        "🎯 *What should I focus on?*\n\nTell me what matters for TrustMe, or /skip for default.",
        parse_mode="Markdown"
    )
    return WAITING_FOCUS


async def receive_focus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return ConversationHandler.END
    focus = "" if update.message.text.strip().startswith("/skip") else update.message.text.strip()
    await update.message.reply_text("⏳ Processing with Gemini 1.5 Pro...")

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
                content = f"[Voice message — transcription via Whisper coming in Phase 2. File ID: {context.user_data['pending_file_id']}]"
            else:
                try:
                    with open(tmp_path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read(40000)
                except Exception:
                    content = f"[Binary file: {context.user_data.get('pending_file_name','unknown')}]"
            os.unlink(tmp_path)

        if len(content) < 50:
            await update.message.reply_text("⚠️ Could not extract content. Try pasting text directly.")
            return ConversationHandler.END

        data = process_with_gemini(content, source, focus)
        context.user_data["pending_data"]    = data
        context.user_data["pending_content"] = content

        keyboard = [
            [InlineKeyboardButton("✅ Save to Drive", callback_data="confirm_save"),
             InlineKeyboardButton("✏️ Edit scores",   callback_data="edit_scores")],
            [InlineKeyboardButton("🔄 Change folder", callback_data="change_folder"),
             InlineKeyboardButton("❌ Discard",        callback_data="discard")],
        ]
        await update.message.reply_text(
            format_preview(data, source) + "\n\n─────────────────────\n✅ Save to Drive?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    except json.JSONDecodeError:
        await update.message.reply_text("⚠️ Gemini returned unexpected format. Please try again.")
    except Exception as e:
        logger.error(e)
        await update.message.reply_text(f"⚠️ Error: {str(e)[:200]}")

    return ConversationHandler.END


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pd  = context.user_data.get("pending_data", {})
    pc  = context.user_data.get("pending_content", "")
    src = context.user_data.get("pending_source", "Unknown")

    if query.data == "confirm_save":
        title    = pd.get("title", "Untitled")
        folder   = pd.get("folder", "Knowledge/Market")
        version  = pd.get("version", "V1.0")
        date     = pd.get("date", datetime.now().strftime("%Y-%m-%d"))
        fname    = f"{re.sub(r'[^A-Za-z0-9_]','_',title)[:40]}_{version}_{date}"
        context.bot_data.setdefault("docs", []).append({
            "title": title, "folder": folder, "importance": pd.get("importance",5),
            "credibility": pd.get("credibility",5), "date": date, "source": src
        })
        await query.message.reply_document(
            document=format_doc(pd, src, pc).encode("utf-8"),
            filename=f"{fname}.txt",
            caption=f"✅ *Document ready!*\n\n📁 Upload to: `{folder}/`\n📄 `{fname}`\n\n_Phase 2: auto-upload to Drive_",
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


async def handle_score_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        src  = context.user_data.get("pending_source", "Unknown")
        pc   = context.user_data.get("pending_content", "")
        title = pd.get("title","Untitled")
        folder = pd.get("folder","Knowledge/Market")
        version = pd.get("version","V1.0")
        date = pd.get("date", datetime.now().strftime("%Y-%m-%d"))
        fname = f"{re.sub(r'[^A-Za-z0-9_]','_',title)[:40]}_{version}_{date}"
        context.bot_data.setdefault("docs", []).append({
            "title": title, "folder": folder, "importance": pd.get("importance",5),
            "credibility": pd.get("credibility",5), "date": date, "source": src
        })
        await update.message.reply_document(
            document=format_doc(pd, src, pc).encode("utf-8"),
            filename=f"{fname}.txt",
            caption=f"✅ *Saved!*\n📁 `{folder}/`\n⭐️{pd.get('importance')}/10  ✅{pd.get('credibility')}/10",
            parse_mode="Markdown"
        )
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
    logger.info("TrustMe Knowledge Bot (Gemini) starting...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
