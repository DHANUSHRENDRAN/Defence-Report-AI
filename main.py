"""
Intel Engine — FastAPI Backend
Deploy: Render.com
"""

import os, asyncio, time, json, io, re
from datetime import datetime
from typing import List, Tuple

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from dotenv import load_dotenv
from groq import Groq
from supabase import create_client, Client
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
#  CLIENTS
# ─────────────────────────────────────────────────────────────────────────────
groq_client: Groq = Groq(api_key=os.getenv("GROQ_API_KEY"))
supa: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_KEY")   # service role key — backend only
)

# ─────────────────────────────────────────────────────────────────────────────
#  TPM SAFE ZONES
#
#  Groq Free Tier hard limits:
#    llama-3.3-70b-versatile  → 6,000 TPM  | 30 RPM
#    llama-3.1-8b-instant     → 30,000 TPM | 30 RPM
#
#  We target 72% of limit as safe ceiling:
#    70b  → 4,320 TPM safe  |  2 brain calls × 1,100 tokens = 2,200 TPM  ✓
#    8b   → 21,600 TPM safe |  N writer calls × 850 tokens with 5s delays ✓
# ─────────────────────────────────────────────────────────────────────────────
BRAIN        = "llama-3.3-70b-versatile"
WRITER       = "llama-3.1-8b-instant"
BRAIN_MAXTOK = 1100    # plan + outline calls: 2 × 1100 = 2,200 TPM brain
WRITE_MAXTOK = 850     # per chapter; 8 chapters × 850 = 6,800 TPM writer
WRITE_DELAY  = 5.0     # seconds between writer calls (TPM guard)

# ─────────────────────────────────────────────────────────────────────────────
#  DOMAIN FILTERS
# ─────────────────────────────────────────────────────────────────────────────
BLOCKLIST = {
    "facebook.com", "youtube.com", "twitter.com", "x.com", "instagram.com",
    "tiktok.com", "reddit.com", "pinterest.com", "quora.com", "linkedin.com",
    "amazon.com", "flipkart.com", "ebay.com", "walmart.com", "etsy.com",
    "shopify.com", "aliexpress.com", "snapchat.com", "whatsapp.com",
}
PRIORITY: dict[str, int] = {
    "janes.com": 10, "rand.org": 10, "sipri.org": 10, "defenseone.com": 9,
    "reuters.com": 9, "apnews.com": 9, "bloomberg.com": 8, "ft.com": 8,
    "bbc.com": 8, "theguardian.com": 7, "foreignpolicy.com": 9,
    "thediplomat.com": 8, "orfonline.org": 8, "idsa.in": 8, "mei.edu": 8,
    "washingtonpost.com": 7, "nytimes.com": 7, "economist.com": 9,
    "aljazeera.com": 8, "dw.com": 7, "bbc.co.uk": 8, "gov.uk": 7,
    "nato.int": 9, "mod.gov": 8, "defensenews.com": 9,
}

def is_blocked(url: str) -> bool:
    return any(b in url for b in BLOCKLIST)

def score_url(url: str) -> int:
    for domain, score in PRIORITY.items():
        if domain in url:
            return score
    return 2  # neutral unknown source

# ─────────────────────────────────────────────────────────────────────────────
#  REGION DETECTION
# ─────────────────────────────────────────────────────────────────────────────
_REGION_MAP: dict[str, list[str]] = {
    "gb": ["uk", "britain", "british", "eurofighter", "bae systems", "raf", "royal navy",
           "downing street", "moi uk"],
    "in": ["india", "indian", "tejas", "drdo", "isro", "modi", "delhi", "mumbai",
           "indian army", "iaf", "ins ", "rupee"],
    "de": ["germany", "german", "bundeswehr", "luftwaffe", "berlin", "scholz"],
    "fr": ["france", "french", "rafale", "macron", "paris", "armée"],
    "cn": ["china", "chinese", "pla", "beijing", "xi jinping", "renminbi", "taiwan strait"],
    "ru": ["russia", "russian", "kremlin", "moscow", "putin", "sukhoi", "rosoboronexport"],
    "pk": ["pakistan", "islamabad", "karachi", "rawalpindi"],
    "il": ["israel", "idf", "mossad", "jerusalem", "tel aviv", "iron dome"],
    "ua": ["ukraine", "ukrainian", "kyiv", "zelensky", "azov"],
    "jp": ["japan", "japanese", "jsdf", "tokyo", "yen"],
    "au": ["australia", "australian", "canberra", "aukus", "asio"],
    "us": ["america", "american", "pentagon", "washington", "us army", "nato",
           "cia", "state department", "white house"],
    "sa": ["saudi", "riyadh", "aramco", "mbs", "gcc"],
    "tr": ["turkey", "turkish", "erdogan", "ankara", "bayraktar"],
    "ir": ["iran", "iranian", "tehran", "irgc", "ayatollah"],
}

def detect_regions(topic: str) -> list[str]:
    t = topic.lower()
    found = [code for code, kws in _REGION_MAP.items() if any(k in t for k in kws)]
    if not found:
        found = ["us"]
    return list(set(found))[:4]          # max 4 regions

# ─────────────────────────────────────────────────────────────────────────────
#  SERPER SEARCH  (Google with gl= country param)
# ─────────────────────────────────────────────────────────────────────────────
async def serper_search(query: str, gl: str) -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://google.serper.dev/search",
                headers={
                    "X-API-KEY": os.getenv("SERPER_API_KEY"),
                    "Content-Type": "application/json",
                },
                json={"q": query, "gl": gl, "num": 5},
            )
            data = resp.json()
            return [r["link"] for r in data.get("organic", []) if "link" in r]
    except Exception:
        return []

# ─────────────────────────────────────────────────────────────────────────────
#  ASYNC SCRAPER
# ─────────────────────────────────────────────────────────────────────────────
_SCRAPE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

async def scrape_url(url: str) -> Tuple[str, str]:
    try:
        async with httpx.AsyncClient(
            timeout=8, follow_redirects=True, headers=_SCRAPE_HEADERS
        ) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                # Remove nav, footer, ads
                for tag in soup.find_all(["nav", "footer", "script", "style", "aside"]):
                    tag.decompose()
                segs = [
                    el.get_text(" ", strip=True)
                    for el in soup.find_all(["p", "h2", "h3"])
                    if len(el.get_text(strip=True)) > 40     # skip tiny fragments
                ]
                content = "\n".join(segs)[:3000]
                return url, content
    except Exception:
        pass
    return url, ""

# ─────────────────────────────────────────────────────────────────────────────
#  LLM — PLANNER  (brain 70b)
# ─────────────────────────────────────────────────────────────────────────────
def llm_plan(topic: str) -> dict:
    prompt = f"""Analyze this research request: "{topic}"

Return ONLY valid JSON, no markdown:
{{
  "queries": ["4-7 word query 1", "4-7 word query 2", "4-7 word query 3", "4-7 word query 4", "4-7 word query 5"],
  "regions": ["us", "gb"],
  "target_word_count": 1500,
  "domain": "defence"
}}

Rules:
- queries: 5 distinct, specific search terms optimised for this topic
- regions: 2-letter ISO codes most geopolitically relevant, max 4
- target_word_count: parse explicitly if mentioned (e.g. "800 words" → 800), else default 1500
- domain: one of defence / geopolitics / economy / technology / general"""

    resp = groq_client.chat.completions.create(
        model=BRAIN,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=BRAIN_MAXTOK,
        temperature=0.15,
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content)

# ─────────────────────────────────────────────────────────────────────────────
#  LLM — OUTLINE  (brain 70b)
# ─────────────────────────────────────────────────────────────────────────────
def llm_outline(topic: str, intel_preview: str, word_count: int, domain: str) -> dict:
    n_chapters = 3 if word_count <= 900 else (5 if word_count <= 2500 else 7)
    wpch = max(150, word_count // n_chapters)

    prompt = f"""Topic: "{topic}"
Domain: {domain}
Target: {word_count} words | Chapters: {n_chapters}
Intel sample: {intel_preview[:1200]}

Return ONLY valid JSON, no markdown:
{{
  "title": "Precise analytical title",
  "subtitle": "One-sentence descriptor",
  "section_titles": ["Chapter title 1", "Chapter title 2", ...],
  "words_per_chapter": {wpch}
}}

Chapter titles must be specific to the topic content from intel, not generic."""

    resp = groq_client.chat.completions.create(
        model=BRAIN,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=BRAIN_MAXTOK,
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content)

# ─────────────────────────────────────────────────────────────────────────────
#  LLM — CHAPTER WRITER  (writer 8b)
# ─────────────────────────────────────────────────────────────────────────────
def llm_write_chapter(topic: str, chapter: str, intel: str, words: int, domain: str) -> str:
    system = {
        "defence":     "You are a senior defence intelligence analyst. Cite budgets, platforms, doctrines, and timelines precisely. Never pad.",
        "geopolitics": "You are a senior geopolitical analyst at a policy institute. Cite actors, dates, treaties, power dynamics. Be precise.",
        "economy":     "You are a senior economic analyst. Cite GDP figures, trade volumes, policy changes, and market reactions. Be precise.",
        "technology":  "You are a senior tech analyst. Cite specs, timelines, companies, and adoption data. Be precise.",
        "general":     "You are a senior research analyst. Be precise, cite data, avoid padding.",
    }.get(domain, "You are a senior research analyst. Be precise, cite data, avoid padding.")

    resp = groq_client.chat.completions.create(
        model=WRITER,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content":
                f"INTEL DATABASE:\n{intel}\n\n"
                f"MASTER TOPIC: {topic}\n"
                f"CHAPTER TO WRITE: {chapter}\n"
                f"TARGET LENGTH: ~{words} words\n\n"
                f"Write ONLY this chapter under a markdown ## heading. "
                f"Draw facts directly from the intel. Do not invent figures. "
                f"Match target word count closely — do NOT overwrite."},
        ],
        max_tokens=WRITE_MAXTOK,
        temperature=0.35,
    )
    return resp.choices[0].message.content

# ─────────────────────────────────────────────────────────────────────────────
#  AUTH  (Supabase JWT)
# ─────────────────────────────────────────────────────────────────────────────
async def get_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or malformed Authorization header")
    token = authorization[7:]
    try:
        user_resp = supa.auth.get_user(token)
        return user_resp.user
    except Exception:
        raise HTTPException(401, "Invalid or expired token")

# ─────────────────────────────────────────────────────────────────────────────
#  DOCX BUILDER
# ─────────────────────────────────────────────────────────────────────────────
def build_docx(
    title: str, subtitle: str, content: str, sources: list[str],
    topic: str, word_count: int
) -> bytes:
    doc = Document()

    # Page margins
    for section in doc.sections:
        section.top_margin    = Inches(1.0)
        section.bottom_margin = Inches(1.0)
        section.left_margin   = Inches(1.2)
        section.right_margin  = Inches(1.2)

    # Title
    h = doc.add_heading(title, 0)
    h.alignment = WD_ALIGN_PARAGRAPH.LEFT
    for run in h.runs:
        run.font.size = Pt(26)
        run.font.color.rgb = RGBColor(0x00, 0x00, 0x00)

    # Subtitle
    if subtitle:
        sub = doc.add_paragraph(subtitle)
        sub.runs[0].font.size = Pt(12)
        sub.runs[0].font.italic = True

    # Metadata bar
    meta_text = (
        f"Topic: {topic}   |   Words: {word_count:,}   |   "
        f"Generated: {datetime.now().strftime('%d %b %Y, %H:%M')}"
    )
    meta = doc.add_paragraph(meta_text)
    meta.runs[0].font.size = Pt(9)
    meta.runs[0].font.color.rgb = RGBColor(0x88, 0x88, 0x88)
    doc.add_paragraph()  # spacer

    # Content — parse markdown
    for line in content.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("## "):
            doc.add_heading(stripped[3:], level=1)
        elif stripped.startswith("### "):
            doc.add_heading(stripped[4:], level=2)
        elif stripped.startswith("**") and stripped.endswith("**") and len(stripped) > 4:
            p = doc.add_paragraph()
            run = p.add_run(stripped.strip("*"))
            run.bold = True
        elif re.match(r"^[-*•]\s", stripped):
            doc.add_paragraph(stripped[2:], style="List Bullet")
        else:
            doc.add_paragraph(stripped)

    # Sources
    if sources:
        doc.add_heading("Sources", level=1)
        for src in sources:
            doc.add_paragraph(src, style="List Bullet")

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.getvalue()

# ─────────────────────────────────────────────────────────────────────────────
#  APP
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Intel Engine API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class GenerateRequest(BaseModel):
    topic: str

# ── /generate ─────────────────────────────────────────────────────────────────
@app.post("/generate")
async def generate_report(req: GenerateRequest, user=Depends(get_user)):
    topic = req.topic.strip()
    if not topic:
        raise HTTPException(400, "Topic cannot be empty")

    # 1. PLAN ──────────────────────────────────────────────────────────────────
    plan       = llm_plan(topic)
    queries    = plan.get("queries", [topic])[:5]
    regions    = plan.get("regions", ["us"])[:4]
    word_count = int(plan.get("target_word_count", 1500))
    domain     = plan.get("domain", "general")

    # Auto-detect additional regions from topic text
    detected = detect_regions(topic)
    regions  = list(set(regions + detected))[:4]

    # 2. MULTI-REGION SERPER SEARCH  (parallel) ────────────────────────────────
    # Use max 2 regions per query to stay within Serper free quota (2500/month)
    search_tasks = [
        serper_search(q, r)
        for q in queries
        for r in regions[:2]
    ]
    search_results = await asyncio.gather(*search_tasks)

    # Deduplicate + score + filter
    seen: set[str] = set()
    scored: list[tuple[str, int]] = []
    for url_list in search_results:
        for url in url_list:
            if url in seen or is_blocked(url):
                continue
            seen.add(url)
            scored.append((url, score_url(url)))

    scored.sort(key=lambda x: x[1], reverse=True)
    top_urls = [u[0] for u in scored[:10]]

    # 3. PARALLEL SCRAPE ───────────────────────────────────────────────────────
    scraped_pairs = await asyncio.gather(*[scrape_url(u) for u in top_urls])

    intel_vault    = ""
    used_sources   = []
    for url, text in scraped_pairs:
        if text:
            intel_vault  += f"\n--- SOURCE: {url} ---\n{text}\n"
            used_sources .append(url)
    intel_vault = intel_vault[:28000]   # ~7k tokens — safe context

    # 4. OUTLINE ───────────────────────────────────────────────────────────────
    outline   = llm_outline(topic, intel_vault, word_count, domain)
    chapters  = outline.get("section_titles", [])
    wpch      = int(outline.get("words_per_chapter", max(150, word_count // max(len(chapters), 1))))

    # 5. WRITE CHAPTERS  (sequential, TPM-guarded) ─────────────────────────────
    full_content = ""
    for i, chapter in enumerate(chapters):
        if i > 0:
            time.sleep(WRITE_DELAY)
        try:
            text = llm_write_chapter(topic, chapter, intel_vault, wpch, domain)
            full_content += f"\n\n{text}"
        except Exception as exc:
            full_content += f"\n\n## {chapter}\n*Generation interrupted: {exc}*"

    actual_wc = len(full_content.split())

    # 6. SAVE TO SUPABASE ──────────────────────────────────────────────────────
    record = {
        "user_id":   str(user.id),
        "title":     outline.get("title",    topic),
        "subtitle":  outline.get("subtitle", ""),
        "content":   full_content.strip(),
        "topic":     topic,
        "word_count": actual_wc,
        "sources":   used_sources,
    }
    saved     = supa.table("reports").insert(record).execute()
    report_id = saved.data[0]["id"] if saved.data else None

    return {
        "id":        report_id,
        "title":     outline.get("title",    topic),
        "subtitle":  outline.get("subtitle", ""),
        "content":   full_content.strip(),
        "word_count": actual_wc,
        "sources":   used_sources,
        "timestamp": datetime.now().isoformat(),
    }

# ── /reports ──────────────────────────────────────────────────────────────────
@app.get("/reports")
async def list_reports(user=Depends(get_user)):
    result = (
        supa.table("reports")
        .select("id, title, subtitle, topic, word_count, created_at")
        .eq("user_id", str(user.id))
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    )
    return result.data

# ── /reports/{id} ─────────────────────────────────────────────────────────────
@app.get("/reports/{report_id}")
async def get_report(report_id: str, user=Depends(get_user)):
    result = (
        supa.table("reports")
        .select("*")
        .eq("id",      report_id)
        .eq("user_id", str(user.id))
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(404, "Report not found")
    return result.data

# ── /reports/{id}/docx ────────────────────────────────────────────────────────
@app.get("/reports/{report_id}/docx")
async def download_docx(report_id: str, user=Depends(get_user)):
    result = (
        supa.table("reports")
        .select("*")
        .eq("id",      report_id)
        .eq("user_id", str(user.id))
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(404, "Report not found")
    r = result.data

    docx_bytes = build_docx(
        title=r["title"],
        subtitle=r.get("subtitle", ""),
        content=r["content"],
        sources=r.get("sources", []),
        topic=r["topic"],
        word_count=r.get("word_count", 0),
    )
    safe_name = re.sub(r"[^a-z0-9_-]", "_", r["title"].lower())[:40]
    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.docx"'},
    )

# ── /reports/{id}  DELETE ─────────────────────────────────────────────────────
@app.delete("/reports/{report_id}")
async def delete_report(report_id: str, user=Depends(get_user)):
    supa.table("reports").delete()\
        .eq("id", report_id)\
        .eq("user_id", str(user.id))\
        .execute()
    return {"deleted": True}

# ── healthcheck ───────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "ts": datetime.now().isoformat()}