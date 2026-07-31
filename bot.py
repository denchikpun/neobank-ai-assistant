import os
import re
import json
import logging
import tempfile
import requests
from datetime import datetime

from groq import Groq
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
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
TAVILY_KEY      = os.environ.get("TAVILY_API_KEY", "")
ALLOWED_USERNAMES = set(
    u.strip().lstrip("@").lower()
    for u in os.environ.get("ALLOWED_USERNAMES", "denya951,daukaz,kompassito").split(",")
    if u.strip()
)

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


def is_allowed(update):
    """Access is granted only to Telegram usernames in the allowlist."""
    user = getattr(update, "effective_user", None)
    if user is None:
        return False
    uname = (user.username or "").lower()
    # empty allowlist = open (safety fallback); normally it's populated
    if not ALLOWED_USERNAMES:
        return True
    return uname in ALLOWED_USERNAMES


async def deny(update):
    """Tell an unauthorized user they don't have access."""
    try:
        await update.message.reply_text(
            "⛔️ Access denied. You are not authorized to use this bot.\n"
            "Contact an administrator to request access."
        )
    except Exception:
        pass


VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "tiktok.com",
               "instagram.com", "facebook.com/watch", "twitch.tv")


def is_video_url(url):
    u = url.lower()
    return any(h in u for h in VIDEO_HOSTS)


def fetch_url_content(url):
    """Return extracted text, or None if the page could not be read."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; TrustMeBot/1.0)"}
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        html = resp.text

        # 1) Try trafilatura — extracts main article, strips nav/footer/menus
        try:
            import trafilatura
            extracted = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=False,
                no_fallback=False,
            )
            if extracted and len(extracted.strip()) > 200:
                return extracted.strip()[:40000]
        except Exception as e:
            logger.info(f"trafilatura failed, falling back: {e}")

        # 2) Fallback — crude tag strip (last resort)
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>",   "", text, flags=re.DOTALL)
        text = re.sub(r"<nav[^>]*>.*?</nav>",       "", text, flags=re.DOTALL)
        text = re.sub(r"<footer[^>]*>.*?</footer>", "", text, flags=re.DOTALL)
        text = re.sub(r"<header[^>]*>.*?</header>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+",     " ", text).strip()
        # only accept if we actually got meaningful text
        if len(text) > 200:
            return text[:40000]
        return None
    except Exception as e:
        logger.info(f"fetch_url_content failed for {url}: {e}")
        return None


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
        if fname.endswith((".xlsx", ".xlsm", ".xls")):
            try:
                import openpyxl
            except ImportError as e:
                extract_file_content.last_error = f"openpyxl not installed: {e}"
                logger.info(extract_file_content.last_error)
                return None
            try:
                wb = openpyxl.load_workbook(tmp_path, read_only=True, data_only=True)
                out = []
                for ws in wb.worksheets:
                    out.append(f"=== Sheet: {ws.title} ===")
                    for row in ws.iter_rows(values_only=True):
                        cells = [str(c) for c in row if c is not None]
                        if cells:
                            out.append(" | ".join(cells))
                    if sum(len(x) for x in out) > 40000:
                        break
                wb.close()
                text = "\n".join(out).strip()
                if text:
                    return text[:40000]
                extract_file_content.last_error = "workbook opened but no readable cells found"
                logger.info(extract_file_content.last_error)
                return None
            except Exception as e:
                extract_file_content.last_error = f"openpyxl read error: {type(e).__name__}: {e}"
                logger.info(extract_file_content.last_error)
                return None
        if fname.endswith(".csv"):
            with open(tmp_path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read(40000)
        with open(tmp_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(40000)
    except Exception as e:
        extract_file_content.last_error = f"{type(e).__name__}: {e}"
        logger.info(f"extract_file_content failed for {filename}: {e}")
        return None

extract_file_content.last_error = ""


def process_with_ai(content, source, focus):
    pages  = max(1, len(content.split()) // 250)
    n_tags = "15-20" if pages >= 100 else "10-15" if pages >= 50 else "5-10" if pages >= 10 else "3-5"
    today  = datetime.now().strftime("%Y-%m-%d")

    prompt = f"""You are the NeoBank Knowledge Agent. Process this material and return ONLY valid JSON — no markdown, no explanation.

LANGUAGE RULE: The input material may be in ANY language (Russian, English, Arabic, etc.). You must UNDERSTAND it in its original language, but ALWAYS write every output field (title, summary, key_insights, hashtags) in ENGLISH. Never output non-English text.

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
                       f"\n\nSOURCE CONTENT (cleaned excerpt):\n{content[:6000]}",
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
            return result.get("file_url"), result.get("file_id"), None
        else:
            return None, None, result.get("error", "Unknown error from Apps Script")
    except Exception as e:
        return None, None, str(e)


def fetch_index_list():
    """Fetch the list of all documents from INDEX via Apps Script."""
    try:
        resp = requests.post(APPS_SCRIPT_URL, json={
            "token": SCRIPT_TOKEN, "action": "list_index"
        }, timeout=30)
        result = resp.json()
        if result.get("ok"):
            return result.get("docs", []), None
        return None, result.get("error", "Unknown error")
    except Exception as e:
        return None, str(e)


def fetch_docs_content(file_ids):
    """Fetch the full text of documents by a list of IDs."""
    try:
        resp = requests.post(APPS_SCRIPT_URL, json={
            "token": SCRIPT_TOKEN, "action": "read_docs", "file_ids": file_ids
        }, timeout=45)
        result = resp.json()
        if result.get("ok"):
            return result.get("docs", []), None
        return None, result.get("error", "Unknown error")
    except Exception as e:
        return None, str(e)


def pick_relevant_docs(question, index_docs):
    """Step 1: Groq selects relevant documents from the INDEX list."""
    catalog = "\n".join([
        f"{i+1}. [{d.get('folder','')}] {d.get('title','')} (id: {d.get('file_id','')}) {d.get('scores','')}"
        for i, d in enumerate(index_docs) if d.get('file_id')
    ])
    prompt = f"""You are a search assistant for the TrustMe knowledge base. The user question may be in any language — understand it regardless. User question:
"{question}"

Here is the document catalog (number, folder, title, id):
{catalog}

Select up to 3 MOST relevant documents to answer the question.
Return ONLY JSON, no explanation:
{{"file_ids": ["id1", "id2"], "reason": "briefly why"}}
If nothing fits — return {{"file_ids": [], "reason": "..."}}."""

    response = groq_client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```json\s*|^```\s*|\s*```$", "", raw)
    return json.loads(raw)


def answer_from_docs(question, docs_content):
    """Step 2: Groq answers the question using the full text of selected documents."""
    context_text = ""
    for d in docs_content:
        if d.get("content"):
            context_text += f"\n\n=== DOCUMENT: {d.get('title','')} ===\n{d['content']}"

    prompt = f"""You are the TrustMe knowledge base assistant. Answer the user's question RELYING ONLY on the provided documents.
If the documents don't contain the answer — say so honestly, don't make things up.

QUESTION: {question}

DOCUMENTS:{context_text}

Answer in English (even if the documents or question are in another language), to the point. At the end, note which documents you relied on."""

    response = groq_client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4,
    )
    return response.choices[0].message.content.strip()


async def ask_knowledge_base(update, context):
    """Handle a 'Help, ...' question — search and answer from the knowledge base."""
    if not is_allowed(update):
        await deny(update)
        return
    text = update.message.text.strip()
    # remove the 'Help' trigger and leading punctuation
    question = re.sub(r"^help[,!\s]*", "", text, flags=re.IGNORECASE).strip()
    if not question:
        await update.message.reply_text(
            "Type a question after «Help», for example:\n"
            "«Help, what materials do we have on KYC?»"
        )
        return

    await update.message.reply_text("🔎 Searching the knowledge base...")

    # Step 1 — get the catalog
    index_docs, error = fetch_index_list()
    if error:
        await update.message.reply_text(f"⚠️ Could not read INDEX: {error}")
        return
    if not index_docs:
        await update.message.reply_text("The knowledge base is empty — nothing to search.")
        return

    # Step 2 — pick relevant
    try:
        pick = pick_relevant_docs(question, index_docs)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Search error: {str(e)[:200]}")
        return

    file_ids = pick.get("file_ids", [])
    if not file_ids:
        await update.message.reply_text(
            "Nothing relevant found in the base for this query.\n"
            f"_{pick.get('reason','')}_",
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text(f"📖 Reading {len(file_ids)} document(s)...")

    # Step 3 — read full text
    docs_content, error = fetch_docs_content(file_ids)
    if error:
        await update.message.reply_text(f"⚠️ Could not read documents: {error}")
        return

    # Step 4 — answer
    try:
        answer = answer_from_docs(question, docs_content)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Answer generation error: {str(e)[:200]}")
        return

    # source references
    sources = "\n".join([
        f"• {d.get('title','')}" for d in docs_content if d.get("content")
    ])
    full = answer
    if len(full) > 3500:
        full = full[:3500] + "..."
    await update.message.reply_text(full, disable_web_page_preview=True)


# ── Research feature ───────────────────────────────────────

def tavily_search(query, max_results=4):
    """Search the web via Tavily — returns list of {title, url, content}."""
    if not TAVILY_KEY:
        return None, "Tavily API key not configured"
    try:
        resp = requests.post("https://api.tavily.com/search", json={
            "api_key": TAVILY_KEY,
            "query": query,
            "search_depth": "advanced",
            "max_results": max_results,
            "include_answer": False,
        }, timeout=40)
        resp.raise_for_status()
        data = resp.json()
        results = [{
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", "")
        } for r in data.get("results", [])]
        return results, None
    except Exception as e:
        return None, str(e)


def plan_research_queries(topic, user_material):
    """Groq turns the topic into 2-3 focused web search queries."""
    prompt = f"""You are a research assistant for TrustMe (Islamic digital bank).
The user wants to research this topic: "{topic}"
{f'They also provided this material as context: {user_material[:2000]}' if user_material else ''}

Generate 2-3 focused web search queries (in English) that will find the most useful, factual information.
Return ONLY JSON: {{"queries": ["query 1", "query 2", "query 3"]}}"""
    response = groq_client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        response_format={"type": "json_object"},
    )
    raw = re.sub(r"^```json\s*|^```\s*|\s*```$", "", response.choices[0].message.content.strip())
    return json.loads(raw).get("queries", [topic])


def synthesize_research(topic, user_material, web_results):
    """Groq writes the FOUND ONLINE section from web results, with inline source refs."""
    sources_block = ""
    for i, r in enumerate(web_results, 1):
        sources_block += f"\n[Source {i}] {r['title']} ({r['url']})\n{r['content'][:2000]}\n"

    prompt = f"""You are a research analyst for TrustMe (Islamic digital bank).
Topic: "{topic}"

You have these web sources:
{sources_block}

Write a clear, factual research summary in ENGLISH based ONLY on these sources.
- Organize by sub-topic with short headings
- After each claim, cite the source number like [Source 1]
- Be concise and factual, no fluff
- If sources conflict, note it
Do NOT invent facts not in the sources."""
    response = groq_client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4,
    )
    return response.choices[0].message.content.strip()


def pick_research_folder(topic):
    """Groq picks the best folder for the research document."""
    prompt = f"""Topic of a research document: "{topic}"
Available folders:
{FOLDERS_TEXT}

Pick the single best-fit folder. Return ONLY JSON: {{"folder": "Knowledge/Market"}}"""
    try:
        response = groq_client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        raw = re.sub(r"^```json\s*|^```\s*|\s*```$", "", response.choices[0].message.content.strip())
        folder = json.loads(raw).get("folder", "Knowledge/Market")
        return folder if folder in FOLDERS else "Knowledge/Market"
    except Exception:
        return "Knowledge/Market"


def build_research_document(topic, user_material, web_results, synthesis):
    """Assemble the final research document body with clear sections."""
    today = datetime.now().strftime("%Y-%m-%d")
    parts = []
    parts.append("RESEARCH DOCUMENT")
    parts.append(f"Topic: {topic}")
    parts.append(f"Date: {today}")
    parts.append("Generated by: TrustMe Knowledge Agent (web research)")
    parts.append("=" * 50)
    parts.append("")
    parts.append("SECTION 1 — FROM USER")
    parts.append("(Material and request provided by the team member)")
    parts.append("")
    parts.append(f"Request: {topic}")
    if user_material:
        parts.append("")
        parts.append("Provided material:")
        parts.append(user_material[:5000])
    else:
        parts.append("(No material attached — topic-only research request)")
    parts.append("")
    parts.append("=" * 50)
    parts.append("")
    parts.append("SECTION 2 — FOUND ONLINE")
    parts.append("(Researched by the agent from open web sources)")
    parts.append("")
    parts.append(synthesis)
    parts.append("")
    parts.append("=" * 50)
    parts.append("")
    parts.append("SECTION 3 — SOURCES")
    parts.append("")
    for i, r in enumerate(web_results, 1):
        parts.append(f"[Source {i}] {r['title']}")
        parts.append(f"    {r['url']}")
    return "\n".join(parts)


def save_research_to_drive(data, source, content):
    """Save a research document via Apps Script create_doc."""
    if not APPS_SCRIPT_URL:
        return None, None, "Apps Script URL not configured"
    payload = {
        "token": SCRIPT_TOKEN, "action": "create_doc",
        "title": data["title"], "folder": data["folder"], "content": content,
        "source": source, "importance": data["importance"], "credibility": data["credibility"],
        "date": data["date"], "version": data["version"], "hashtags": data["hashtags"],
        "summary": data["summary"], "key_insights": data["key_insights"],
    }
    try:
        resp = requests.post(APPS_SCRIPT_URL, json=payload, timeout=40)
        logger.info(f"Research save response [{resp.status_code}]: {resp.text[:300]}")
        result = resp.json()
        if result.get("ok"):
            return result.get("file_url"), result.get("file_id"), None
        return None, None, result.get("error", "Unknown error")
    except Exception as e:
        return None, None, str(e)


async def research(update, context):
    """Handle 'Research: <topic>' — web research compiled into a Drive document."""
    if not is_allowed(update):
        await deny(update)
        return
    text = update.message.text.strip()
    topic = re.sub(r"^research[:,!\s]*", "", text, flags=re.IGNORECASE).strip()
    user_material = context.user_data.get("pending_text", "") or ""

    if not topic:
        await update.message.reply_text(
            "Type a topic after «Research:», for example:\n"
            "«Research: halal ETF competitors and their fee structures»"
        )
        return

    if not TAVILY_KEY:
        await update.message.reply_text(
            "⚠️ Web research is not configured yet (missing Tavily key). "
            "Add TAVILY_API_KEY in Railway and redeploy."
        )
        return

    await update.message.reply_text(f"🔬 Researching: {topic}\nPlanning search queries...")

    try:
        queries = plan_research_queries(topic, user_material)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Could not plan research: {str(e)[:200]}")
        return

    all_results = []
    seen_urls = set()
    for q in queries[:3]:
        await update.message.reply_text(f"🌐 Searching: {q}")
        results, err = tavily_search(q, max_results=4)
        if results:
            for r in results:
                if r["url"] not in seen_urls and r.get("content"):
                    seen_urls.add(r["url"])
                    all_results.append(r)

    if not all_results:
        await update.message.reply_text("No web results found for this topic. Try rephrasing.")
        return

    all_results = all_results[:8]
    await update.message.reply_text(f"📚 Found {len(all_results)} sources. Synthesizing...")

    try:
        synthesis = synthesize_research(topic, user_material, all_results)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Synthesis error: {str(e)[:200]}")
        return

    folder = pick_research_folder(topic)
    body = build_research_document(topic, user_material, all_results, synthesis)
    title = f"Research — {topic[:60]}"

    await update.message.reply_text(f"💾 Saving research to {folder}...")
    payload_data = {
        "title": title, "folder": folder,
        "summary": f"Web research on: {topic}", "key_insights": [],
        "importance": 6, "credibility": 6,
        "date": datetime.now().strftime("%Y-%m-%d"), "version": "V1.0",
        "hashtags": ["#research", "#type_research", "#lang_en"],
    }
    drive_url, file_id, error = save_research_to_drive(payload_data, "Web research", body)

    if drive_url:
        context.bot_data.setdefault("docs", []).append({
            "title": title, "folder": folder, "importance": 6, "credibility": 6,
            "date": datetime.now().strftime("%Y-%m-%d"), "source": "Web research",
            "drive_url": drive_url, "file_id": file_id or "",
        })
        chat_preview = synthesis if len(synthesis) <= 3000 else synthesis[:3000] + "..."
        await update.message.reply_text(
            f"✅ Research saved\n📁 {folder}\n📄 {title}\n🔗 {drive_url}",
            disable_web_page_preview=True
        )
        await update.message.reply_text(chat_preview, disable_web_page_preview=True)
        context.user_data.clear()
    else:
        await update.message.reply_text(f"⚠️ Could not save research: {error}")


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
    if not is_allowed(update):
        await deny(update)
        return
    await update.message.reply_text(
        "👋 *NeoBank Knowledge Agent*\n"
        "_TrustMe Knowledge Base · Groq + Google Drive_\n\n"
        "I help collect and store the team's knowledge in Google Drive.\n\n"
        "*What I can do:*\n"
        "📎 Accept a link, file, or text\n"
        "🧠 Analyze and sort it into folders\n"
        "🗂 Update indexes and changelog\n"
        "↩️ Roll back a mistaken save\n\n"
        "*How to start:* just send me a link, file, or text.\n\n"
        "💡 To ask the knowledge base — start your message with the word «Help»:\n"
        "_«Help, what materials do we have on KYC?»_\n\n"
        "🔬 To run web research — start with «Research:»:\n"
        "_«Research: halal ETF competitors and fees»_\n\n"
        "Full guide — /help\n"
        "Command list — the «☰» or «/» button next to the input field.",
        parse_mode="Markdown"
    )


async def help_cmd(update, context):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "📖 *NeoBank Knowledge Agent — Guide*\n\n"
        "*1. How to add material*\n"
        "Send the bot:\n"
        "🔗 a link — article, page, post\n"
        "📎 a file — PDF, DOCX, TXT\n"
        "💬 text — just type or paste\n\n"
        "*2. What happens*\n"
        "The bot will ask what to focus on. Then two paths:\n"
        "• Type a focus or /skip → the bot analyzes via AI, "
        "suggests a folder, tags and scores, shows a preview\n"
        "• «📥 Save without analysis» button → pick a folder, "
        "the file is stored as-is, unprocessed\n\n"
        "*3. Confirmation*\n"
        "In the preview you can: save, change folder, adjust scores, or discard.\n"
        "After saving, the document goes to the right Drive folder, "
        "and the indexes and changelog update automatically.\n\n"
        "*4. Rollback*\n"
        "/undo — roll back the last save\n"
        "/rollback <ID> — roll back a document by ID (from a link or /list)\n"
        "Rolled-back documents go to _Archive — they are not deleted and can be restored.\n\n"
        "*5. Questions to the knowledge base*\n"
        "Start your message with the word «Help» — the bot finds the relevant documents and answers from their content:\n"
        "_«Help, what potential KYC partners do we have?»_\n"
        "_«Help, find materials on MoonPay»_\n\n"
        "*6. Web research*\n"
        "Start your message with «Research:» — the bot searches the open web, "
        "compiles findings with source links, and saves a document split into "
        "FROM USER / FOUND ONLINE / SOURCES:\n"
        "_«Research: Sharia-compliant stablecoin regulation in UAE»_\n\n"
        "*7. Commands*\n"
        "/start — welcome\n"
        "/help — this guide\n"
        "/status — knowledge base stats\n"
        "/list — recent documents\n"
        "/undo — roll back the last save\n"
        "/rollback — roll back by ID\n\n"
        "*Storage rules:*\n"
        "• Old content is never deleted — it moves to _Archive\n"
        "• Every action is logged in CHANGELOG\n"
        "• File name: Title_Version_Date",
        parse_mode="Markdown"
    )

async def status(update, context):
    if not is_allowed(update):
        return
    await update.message.reply_text("📊 Reading the knowledge base...")

    index_docs, error = fetch_index_list()
    if error:
        await update.message.reply_text(f"⚠️ Could not read INDEX: {error}")
        return
    # only count real registry rows (those carrying a folder)
    docs = [d for d in (index_docs or []) if d.get("folder")]
    total = len(docs)
    if not total:
        await update.message.reply_text("📊 No documents in the knowledge base yet. Send your first link!")
        return

    # parse Imp/Cred from the 'scores' field (e.g. "Imp: 6  Cred: 8")
    imp_vals, cred_vals = [], []
    folders = {}
    for d in docs:
        folders[d.get("folder", "Unknown")] = folders.get(d.get("folder", "Unknown"), 0) + 1
        s = d.get("scores", "")
        mi = re.search(r"Imp:\s*(\d+)", s)
        mc = re.search(r"Cred:\s*(\d+)", s)
        if mi: imp_vals.append(int(mi.group(1)))
        if mc: cred_vals.append(int(mc.group(1)))

    avg_imp  = round(sum(imp_vals) / len(imp_vals), 1) if imp_vals else "—"
    avg_cred = round(sum(cred_vals) / len(cred_vals), 1) if cred_vals else "—"
    fl = "\n".join([f"  • {f} — {c}" for f, c in sorted(folders.items())])

    await update.message.reply_text(
        f"📊 Knowledge Base\n\n"
        f"📄 {total} documents\n"
        f"🗂 {len(folders)} folders\n"
        f"⭐️ Avg importance: {avg_imp}/10\n"
        f"✅ Avg credibility: {avg_cred}/10\n\n"
        f"📁 By folder:\n{fl}",
        disable_web_page_preview=True
    )

async def list_docs(update, context):
    if not is_allowed(update):
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


def save_raw_to_drive(folder, title, text_content, source):
    """Save a document to Drive without AI analysis."""
    if not APPS_SCRIPT_URL:
        return None, "Apps Script URL not configured"
    payload = {
        "token": SCRIPT_TOKEN,
        "action": "create_doc",
        "title": title,
        "folder": folder,
        "content": text_content or "(file saved without processing)",
        "source": source,
        "importance": 0,
        "credibility": 0,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "version": "V1.0",
        "hashtags": ["#raw_unprocessed"],
        "summary": "Saved without analysis",
        "key_insights": [],
    }
    try:
        resp = requests.post(APPS_SCRIPT_URL, json=payload, timeout=30)
        logger.info(f"Raw save response [{resp.status_code}]: {resp.text[:300]}")
        result = resp.json()
        if result.get("ok"):
            return result, None
        return None, result.get("error", "Unknown error")
    except Exception as e:
        return None, str(e)


def rollback_in_drive(file_id):
    """Ask Apps Script to move a document to _Archive."""
    if not APPS_SCRIPT_URL:
        return None, "Apps Script URL not configured"
    try:
        resp = requests.post(APPS_SCRIPT_URL, json={
            "token": SCRIPT_TOKEN, "action": "rollback", "file_id": file_id
        }, timeout=30)
        logger.info(f"Rollback response [{resp.status_code}]: {resp.text[:300]}")
        result = resp.json()
        if result.get("ok"):
            return result, None
        return None, result.get("error", "Unknown error")
    except Exception as e:
        return None, str(e)


async def undo(update, context):
    """Roll back the last created document — move it to _Archive."""
    if not is_allowed(update):
        return
    docs = context.bot_data.get("docs", [])
    # find the last document with a file_id that hasn't been rolled back yet
    target = None
    for d in reversed(docs):
        if d.get("file_id") and not d.get("rolled_back"):
            target = d
            break
    if not target:
        await update.message.reply_text("Nothing to roll back — no recent saves with an ID.")
        return

    await update.message.reply_text(f"↩️ Rolling back: {target.get('title','Untitled')}...")
    result, error = rollback_in_drive(target["file_id"])
    if result:
        target["rolled_back"] = True
        await update.message.reply_text(
            f"✅ Rolled back to _Archive\n"
            f"📄 {result.get('title', target.get('title'))}\n"
            f"📁 {result.get('from','—')} → _Archive\n\n"
            f"The document is not deleted — it can be restored from _Archive.",
            disable_web_page_preview=True
        )
    else:
        await update.message.reply_text(f"⚠️ Could not roll back: {error}")


async def rollback(update, context):
    """Roll back a document by ID: /rollback <file_id>"""
    if not is_allowed(update):
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /rollback <document ID>\n\n"
            "The ID can be taken from the document link in INDEX or from /list."
        )
        return
    raw = args[0].strip()
    # remove angle brackets, quotes, spaces
    raw = raw.strip("<>\"' ")
    # if a full link was pasted — extract the ID from /d/.../ or ?id=
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", raw) or re.search(r"[?&]id=([a-zA-Z0-9_-]+)", raw)
    file_id = m.group(1) if m else raw
    await update.message.reply_text(f"↩️ Rolling back document {file_id}...")
    result, error = rollback_in_drive(file_id)
    if result:
        # mark in history if present
        for d in context.bot_data.get("docs", []):
            if d.get("file_id") == file_id:
                d["rolled_back"] = True
        await update.message.reply_text(
            f"✅ Rolled back to _Archive\n"
            f"📄 {result.get('title','—')}\n"
            f"📁 {result.get('from','—')} → _Archive\n\n"
            f"The document is not deleted — it can be restored from _Archive.",
            disable_web_page_preview=True
        )
    else:
        await update.message.reply_text(f"⚠️ Could not roll back: {error}")


# ── Receive material ──────────────────────────────────────

async def receive_message(update, context):
    if not is_allowed(update):
        await deny(update)
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
    kb = [[InlineKeyboardButton("📥 Save without analysis", callback_data="raw_save")]]
    await msg.reply_text(
        "🎯 *What should I focus on?*\n\n"
        "Tell me what's important for TrustMe, or /skip for default analysis.\n"
        "Or save the file as-is, without processing:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    return WAITING_FOCUS


async def receive_focus(update, context):
    if not is_allowed(update):
        return ConversationHandler.END
    focus = "" if update.message.text.strip().startswith("/skip") else update.message.text.strip()
    await update.message.reply_text("⏳ Analysing...")

    ptype  = context.user_data.get("pending_type")
    source = context.user_data.get("pending_source", "Unknown")
    content = ""

    try:
        if ptype == "url":
            url = context.user_data["pending_url"]
            # Video links can't be read — bot has no transcript access
            if is_video_url(url):
                await update.message.reply_text(
                    "🎬 This looks like a video link. I can't read video or audio content — "
                    "I only process web articles, PDFs, DOCX and text.\n\n"
                    "What you can do:\n"
                    "• Paste the video's transcript or description as text\n"
                    "• Send an article about the same topic\n"
                    "• Use «Research: <topic>» to gather written sources on it"
                )
                return ConversationHandler.END
            await update.message.reply_text("🌐 Fetching URL...")
            content = fetch_url_content(url)
        elif ptype == "text":
            content = context.user_data["pending_text"]
        elif ptype in ("file", "voice"):
            tg_file = await context.bot.get_file(context.user_data["pending_file_id"])
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                await tg_file.download_to_drive(tmp.name)
                tmp_path = tmp.name
            if ptype == "voice":
                await update.message.reply_text(
                    "🎙 I can't transcribe voice messages yet. Please send text, a link, or a file."
                )
                os.unlink(tmp_path)
                return ConversationHandler.END
            else:
                content = extract_file_content(tmp_path, context.user_data.get("pending_file_name", "file.txt"))
            os.unlink(tmp_path)

        # fetch/extract failed or returned nothing usable — stop, don't save junk
        if not content or len(content.strip()) < 50:
            if ptype == "url":
                await update.message.reply_text(
                    "⚠️ I couldn't read that page. It may block automated access, require login, "
                    "or be a video/app page with no article text.\n\n"
                    "Try pasting the text directly, or send a different link."
                )
            else:
                detail = getattr(extract_file_content, "last_error", "") or "unknown"
                await update.message.reply_text(
                    "⚠️ Couldn't extract readable content from that file.\n"
                    f"Reason: {detail}\n\n"
                    "Try a different format (PDF, DOCX, XLSX, TXT) or paste the text."
                )
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

        drive_url, file_id, error = save_to_drive(pd, src, pc)

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
            "file_id":     file_id or "",
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

    elif query.data == "raw_save":
        # save without analysis — folder picker first
        kb = [[InlineKeyboardButton(f, callback_data=f"rawfolder_{f}")] for f in FOLDERS]
        await query.message.reply_text(
            "📁 Choose a folder to save the file without processing:",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif query.data.startswith("rawfolder_"):
        folder = query.data[len("rawfolder_"):]
        await query.edit_message_text(f"📥 Saving to {folder} without analysis...")

        ptype = context.user_data.get("pending_type")
        fname = context.user_data.get("pending_file_name", "")
        text_content = ""
        tg_file_id = context.user_data.get("pending_file_id")

        # for text/link — save as a text document;
        # for a file — take the name and content as-is
        if ptype == "text":
            text_content = context.user_data.get("pending_text", "")
            title = (text_content[:50] + "...") if len(text_content) > 50 else (text_content or "Note")
        elif ptype == "url":
            text_content = context.user_data.get("pending_url", "")
            title = "Link: " + text_content[:60]
        else:
            title = fname or "Uploaded file"

        result, error = save_raw_to_drive(folder, title, text_content, src)
        if result and result.get("ok"):
            context.bot_data.setdefault("docs", []).append({
                "title": title, "folder": folder, "importance": "—", "credibility": "—",
                "date": datetime.now().strftime("%Y-%m-%d"), "source": src,
                "drive_url": result.get("file_url", ""), "file_id": result.get("file_id", ""),
            })
            await query.message.reply_text(
                f"✅ Saved without analysis\n📁 {folder}\n📄 {title}\n🔗 {result.get('file_url','')}",
                disable_web_page_preview=True
            )
        else:
            await query.message.reply_text(f"⚠️ Could not save: {error}")
        context.user_data.clear()

    elif query.data.startswith("folder_"):
        context.user_data["pending_data"]["folder"] = query.data[7:]
        await query.edit_message_text(f"📁 Folder: `{query.data[7:]}`\n\nSend `confirm` to save.",
                                       parse_mode="Markdown")


async def handle_score_edit(update, context):
    if not is_allowed(update):
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
        drive_url, file_id, error = save_to_drive(pd, src, pc)
        title  = pd.get("title", "Untitled")
        folder = pd.get("folder", "Knowledge/Market")
        context.bot_data.setdefault("docs", []).append({
            "title": title, "folder": folder,
            "importance": pd.get("importance", 5), "credibility": pd.get("credibility", 5),
            "date": pd.get("date", datetime.now().strftime("%Y-%m-%d")),
            "source": src, "drive_url": drive_url or "", "file_id": file_id or "",
        })
        if drive_url:
            await update.message.reply_text(
                f"✅ Saved!\n📁 {folder}\n📄 {title}\n🔗 {drive_url}",
                disable_web_page_preview=True
            )
        else:
            await update.message.reply_text(f"⚠️ Drive error: {error}")
        context.user_data.clear()


async def setup_commands(app):
    """Register the command menu (the «/» button in Telegram)."""
    await app.bot.set_my_commands([
        BotCommand("start",    "Welcome and overview"),
        BotCommand("help",     "How to use the bot"),
        BotCommand("status",   "Knowledge base stats"),
        BotCommand("list",     "Recent documents"),
        BotCommand("undo",     "Roll back the last save"),
        BotCommand("rollback", "Roll back a document by ID"),
    ])
    logger.info("Bot command menu registered")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(setup_commands).build()

    # «Help ...» — a question to the knowledge base (not material to save)
    pomogi_filter = filters.TEXT & filters.Regex(r"(?i)^\s*help")
    # «Research: ...» — web research compiled into a document
    research_filter = filters.TEXT & filters.Regex(r"(?i)^\s*research[:\s]")

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & ~filters.COMMAND & ~pomogi_filter & ~research_filter, receive_message),
            MessageHandler(filters.Document.ALL,             receive_message),
            MessageHandler(filters.VOICE,                    receive_message),
            MessageHandler(filters.AUDIO,                    receive_message),
        ],
        states={WAITING_FOCUS: [MessageHandler(filters.TEXT & ~pomogi_filter & ~research_filter, receive_focus)]},
        fallbacks=[CommandHandler("skip", receive_focus)],
    )
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("help",   help_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("list",   list_docs))
    app.add_handler(CommandHandler("undo",     undo))
    app.add_handler(CommandHandler("rollback", rollback))
    # knowledge base question — before conv, to intercept first
    app.add_handler(MessageHandler(pomogi_filter, ask_knowledge_base))
    # web research — before conv
    app.add_handler(MessageHandler(research_filter, research))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~pomogi_filter & ~research_filter, handle_score_edit))
    logger.info("NeoBank Knowledge Bot (Phase 2 — Groq + Drive) starting...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
