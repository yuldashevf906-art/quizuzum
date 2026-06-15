import base64
import hashlib
import json
import os
import re
import sqlite3
import textwrap
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

import fitz  # PyMuPDF
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"
DB_PATH = DATA_DIR / "testchi_ai.db"

DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct").strip()
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
APP_NAME = os.getenv("APP_NAME", "Testchi AI")
INITIAL_CREDITS = int(os.getenv("INITIAL_CREDITS", "30"))
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "14000"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "90"))
FREE_STARTER_USES = int(os.getenv("FREE_STARTER_USES", "5"))
FREE_COOLDOWN_DAYS = int(os.getenv("FREE_COOLDOWN_DAYS", "7"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8010").rstrip("/")
PAYME_MERCHANT_ID = os.getenv("PAYME_MERCHANT_ID", "").strip()
PAYME_SECRET_KEY = os.getenv("PAYME_SECRET_KEY", "").strip()
PAYME_CHECKOUT_URL = os.getenv("PAYME_CHECKOUT_URL", "https://checkout.paycom.uz").rstrip("/")
CLICK_MERCHANT_ID = os.getenv("CLICK_MERCHANT_ID", "").strip()
CLICK_SERVICE_ID = os.getenv("CLICK_SERVICE_ID", "").strip()
CLICK_SECRET_KEY = os.getenv("CLICK_SECRET_KEY", "").strip()
CLICK_PAY_URL = os.getenv("CLICK_PAY_URL", "https://my.click.uz/services/pay").rstrip("/")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_WEBAPP_URL = os.getenv("TELEGRAM_WEBAPP_URL", PUBLIC_BASE_URL).rstrip("/")
TELEGRAM_STARS_1 = 40
TELEGRAM_STARS_7 = 140
TELEGRAM_STARS_30 = 250

app = FastAPI(title=APP_NAME, version="3.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class TopicTestRequest(BaseModel):
    topic: str = Field(..., min_length=2, max_length=400)
    language: str = "uz"
    count: int = Field(10, ge=3, le=50)
    level: str = "medium"


class FlashcardRequest(BaseModel):
    source: str = Field(..., min_length=2, max_length=16000)
    source_type: str = "topic"  # topic/text
    language: str = "uz"
    count: int = Field(10, ge=3, le=50)
    level: str = "medium"


class ItemSave(BaseModel):
    item_type: str
    title: str
    payload: Dict[str, Any]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS library (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL DEFAULT 'legacy',
                item_type TEXT NOT NULL,
                title TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL DEFAULT 'legacy',
                provider TEXT NOT NULL,
                plan TEXT NOT NULL,
                days INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL,
                provider_transaction_id TEXT,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                paid_at TEXT
            )
            """
        )
        for table in ("library", "payments"):
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if "owner_id" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'legacy'")
        row = conn.execute("SELECT value FROM settings WHERE key='credits'").fetchone()
        if not row:
            conn.execute("INSERT INTO settings(key,value) VALUES('credits', ?)", (str(INITIAL_CREDITS),))
        conn.commit()


init_db()


DEFAULT_OWNER = "legacy"


def clean_owner_id(value: str) -> str:
    value = str(value or "").strip()
    value = re.sub(r"[^A-Za-z0-9:_-]+", "", value)[:80]
    return value or DEFAULT_OWNER


def request_owner(request: Optional[Request] = None, owner_id: str = "") -> str:
    if owner_id:
        return clean_owner_id(owner_id)
    if request is None:
        return DEFAULT_OWNER
    value = request.headers.get("x-user-key") or request.query_params.get("u") or ""
    return clean_owner_id(value)


def owner_setting_key(owner_id: str, key: str) -> str:
    return f"user:{clean_owner_id(owner_id)}:{key}"


def get_credits(owner_id: str = DEFAULT_OWNER) -> int:
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (owner_setting_key(owner_id, "credits"),)).fetchone()
        return int(row["value"]) if row else INITIAL_CREDITS


def set_credits(value: int, owner_id: str = DEFAULT_OWNER) -> None:
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?, ?)", (owner_setting_key(owner_id, "credits"), str(max(0, value))))
        conn.commit()


def get_setting(key: str, default: str = "", owner_id: str = DEFAULT_OWNER) -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (owner_setting_key(owner_id, key),)).fetchone()
    return str(row["value"]) if row else default


def set_setting(key: str, value: str, owner_id: str = DEFAULT_OWNER) -> None:
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (owner_setting_key(owner_id, key), value))
        conn.commit()


PLAN_PRICES = {
    1: 6900,
    7: 24900,
    30: 44900,
}


def plan_amount(days: int, requested_amount: Optional[int] = None) -> int:
    days = int(days)
    if requested_amount and requested_amount > 0:
        return int(requested_amount)
    return PLAN_PRICES.get(days, 44900 if days >= 30 else 24900)


def stars_amount(days: int, requested_amount: Optional[int] = None) -> int:
    if int(days) >= 30:
        return TELEGRAM_STARS_30
    if int(days) >= 7:
        return TELEGRAM_STARS_7
    return TELEGRAM_STARS_1


def today_paid_counts() -> Dict[str, int]:
    now_utc = datetime.now(timezone.utc)
    tashkent = timezone(timedelta(hours=5))
    local_now = now_utc.astimezone(tashkent)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = local_start.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    end_utc = (local_start + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    counts = {"1": 0, "7": 0, "30": 0}
    with db() as conn:
        rows = conn.execute(
            """
            SELECT days, COUNT(DISTINCT owner_id) AS c
            FROM payments
            WHERE status='paid' AND paid_at>=? AND paid_at<?
            GROUP BY days
            """,
            (start_utc, end_utc),
        ).fetchall()
    for row in rows:
        days = str(int(row["days"]))
        if days in counts:
            counts[days] = int(row["c"])
    return counts


def telegram_api(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=400, detail="TELEGRAM_BOT_TOKEN .env ichida yo'q")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Telegram bilan aloqa bo'lmadi: {e}")
    try:
        data = r.json()
    except Exception:
        data = {"ok": False, "description": r.text}
    if not data.get("ok"):
        raise HTTPException(status_code=502, detail=f"Telegram API xatosi: {data}")
    return data


def set_telegram_menu_button() -> Dict[str, Any]:
    return telegram_api("setChatMenuButton", {
        "menu_button": {
            "type": "web_app",
            "text": "Ochish",
            "web_app": {"url": TELEGRAM_WEBAPP_URL},
        }
    })


def activate_premium(days: int, plan: str, owner_id: str = DEFAULT_OWNER) -> str:
    current = premium_until(owner_id)
    start = current if current and current > datetime.now() else datetime.now()
    until = start + timedelta(days=max(1, min(365, int(days))))
    set_setting("premium_until", until.isoformat(timespec="seconds"), owner_id)
    set_setting("premium_plan", plan[:80], owner_id)
    return until.isoformat(timespec="seconds")


def create_payment(provider: str, days: int, amount: int, plan: str, owner_id: str = DEFAULT_OWNER) -> Dict[str, Any]:
    payment_id = str(uuid.uuid4())
    data = {
        "id": payment_id,
        "owner_id": clean_owner_id(owner_id),
        "provider": provider,
        "plan": plan,
        "days": int(days),
        "amount": int(amount),
        "status": "pending",
        "provider_transaction_id": "",
        "payload": {},
        "created_at": now_iso(),
        "paid_at": "",
    }
    with db() as conn:
        conn.execute(
            """
            INSERT INTO payments(id,owner_id,provider,plan,days,amount,status,provider_transaction_id,payload,created_at,paid_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data["id"], data["owner_id"], data["provider"], data["plan"], data["days"], data["amount"],
                data["status"], data["provider_transaction_id"], json.dumps(data["payload"]),
                data["created_at"], data["paid_at"],
            ),
        )
        conn.commit()
    return data


def get_payment(payment_id: str) -> Optional[Dict[str, Any]]:
    with db() as conn:
        row = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
    if not row:
        return None
    data = dict(row)
    try:
        data["payload"] = json.loads(data.get("payload") or "{}")
    except Exception:
        data["payload"] = {}
    return data


def update_payment(payment_id: str, **fields: Any) -> None:
    if not fields:
        return
    allowed = {"status", "provider_transaction_id", "payload", "paid_at"}
    updates = []
    values = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        updates.append(f"{key}=?")
        values.append(json.dumps(value, ensure_ascii=False) if key == "payload" else value)
    if not updates:
        return
    values.append(payment_id)
    with db() as conn:
        conn.execute(f"UPDATE payments SET {', '.join(updates)} WHERE id=?", values)
        conn.commit()


def merge_payment_payload(payment_id: str, extra: Dict[str, Any]) -> Dict[str, Any]:
    payment = get_payment(payment_id)
    payload = (payment or {}).get("payload") or {}
    payload.update(extra)
    update_payment(payment_id, payload=payload)
    return payload


def payment_by_provider_transaction(provider_transaction_id: str) -> Optional[Dict[str, Any]]:
    if not provider_transaction_id:
        return None
    with db() as conn:
        row = conn.execute("SELECT id FROM payments WHERE provider_transaction_id=?", (provider_transaction_id,)).fetchone()
    return get_payment(row["id"]) if row else None


def mark_payment_paid(payment_id: str, provider_transaction_id: str = "", payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payment = get_payment(payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment topilmadi")
    if payment["status"] != "paid":
        owner_id = clean_owner_id(payment.get("owner_id") or DEFAULT_OWNER)
        until = activate_premium(int(payment["days"]), str(payment["plan"]), owner_id)
        merged_payload = payment.get("payload") or {}
        if payload:
            merged_payload.update(payload)
        merged_payload["premium_until"] = until
        merged_payload["paid_time"] = int(time.time() * 1000)
        update_payment(
            payment_id,
            status="paid",
            provider_transaction_id=provider_transaction_id or payment.get("provider_transaction_id") or "",
            payload=merged_payload,
            paid_at=now_iso(),
        )
    fresh = get_payment(payment_id) or payment
    fresh["quota"] = quota_status(clean_owner_id(fresh.get("owner_id") or DEFAULT_OWNER))
    return fresh


def parse_dt(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def get_int_setting(key: str, default: int, owner_id: str = DEFAULT_OWNER) -> int:
    try:
        return int(get_setting(key, str(default), owner_id) or default)
    except ValueError:
        return default


def premium_until(owner_id: str = DEFAULT_OWNER) -> Optional[datetime]:
    return parse_dt(get_setting("premium_until", owner_id=owner_id))


def is_premium_active(owner_id: str = DEFAULT_OWNER) -> bool:
    until = premium_until(owner_id)
    return bool(until and until > datetime.now())


def quota_status(owner_id: str = DEFAULT_OWNER) -> Dict[str, Any]:
    now = datetime.now()
    owner_id = clean_owner_id(owner_id)
    until = premium_until(owner_id)
    premium_active = bool(until and until > now)
    cooldown = parse_dt(get_setting("free_cooldown_until", owner_id=owner_id))
    free_remaining = get_int_setting("free_remaining", FREE_STARTER_USES, owner_id)
    if not premium_active and free_remaining <= 0 and cooldown and cooldown <= now:
        free_remaining = 1
        set_setting("free_remaining", "1", owner_id)
        set_setting("free_cooldown_until", "", owner_id)
        cooldown = None
    can_generate = premium_active or free_remaining > 0
    return {
        "plan": "premium" if premium_active else "standard",
        "premium_active": premium_active,
        "premium_until": until.isoformat(timespec="seconds") if until else "",
        "free_remaining": max(0, free_remaining),
        "free_cooldown_until": cooldown.isoformat(timespec="seconds") if cooldown else "",
        "can_generate": can_generate,
    }


def check_credit(amount: int = 1, owner_id: str = DEFAULT_OWNER) -> None:
    status = quota_status(owner_id)
    if status["premium_active"] or status["free_remaining"] > 0:
        return None
    until = status.get("free_cooldown_until") or ""
    raise HTTPException(
        status_code=402,
        detail=f"Bepul limit tugadi. Premium oling yoki {until[:10]} dan keyin qayta urinib koвЂring.",
    )


def deduct_credit(amount: int = 1, owner_id: str = DEFAULT_OWNER) -> None:
    owner_id = clean_owner_id(owner_id)
    if is_premium_active(owner_id):
        return None
    remaining = max(0, get_int_setting("free_remaining", FREE_STARTER_USES, owner_id) - 1)
    set_setting("free_remaining", str(remaining), owner_id)
    if remaining <= 0:
        cooldown_until = datetime.now() + timedelta(days=FREE_COOLDOWN_DAYS)
        set_setting("free_cooldown_until", cooldown_until.isoformat(timespec="seconds"), owner_id)


def save_item(item_type: str, title: str, payload: Dict[str, Any], owner_id: str = DEFAULT_OWNER) -> Dict[str, Any]:
    item_id = str(uuid.uuid4())
    created_at = now_iso()
    data = {
        "id": item_id,
        "owner_id": clean_owner_id(owner_id),
        "item_type": item_type,
        "title": title.strip()[:180] or "Untitled",
        "payload": payload,
        "created_at": created_at,
    }
    with db() as conn:
        conn.execute(
            "INSERT INTO library(id,owner_id,item_type,title,payload,created_at) VALUES(?,?,?,?,?,?)",
            (item_id, data["owner_id"], item_type, data["title"], json.dumps(payload, ensure_ascii=False), created_at),
        )
        conn.commit()
    return data


def row_to_item(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "owner_id": row["owner_id"] if "owner_id" in row.keys() else DEFAULT_OWNER,
        "item_type": row["item_type"],
        "title": row["title"],
        "payload": json.loads(row["payload"]),
        "created_at": row["created_at"],
    }


def clean_text(text: str) -> str:
    """Clean text but keep useful line breaks. Collapsing everything into one line
    makes PDF tests/options mix together and creates nonsense questions."""
    text = (text or "").replace("\x00", "")
    text = text.replace("\r", "\n")
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lines.append(line)
    # keep structure, but remove huge blank gaps
    text = "\n".join(lines).strip()
    return text[:MAX_CONTEXT_CHARS]


def extract_pdf_text(path: Path) -> str:
    parts: List[str] = []
    try:
        doc = fitz.open(path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"PDF ochilmadi: {e}")
    for page in doc:
        txt = page.get_text("text") or ""
        if txt.strip():
            parts.append(txt)
    doc.close()
    text = clean_text("\n".join(parts))
    if len(text) < 100:
        raise HTTPException(
            status_code=400,
            detail="PDF ichidan yetarli matn topilmadi. Skan/foto PDF boвЂlsa, OCR kerak boвЂladi.",
        )
    return text


def extract_json(raw: str) -> Dict[str, Any]:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("AI boвЂsh javob qaytardi")
    raw = re.sub(r"^```(?:json)?", "", raw.strip(), flags=re.IGNORECASE).strip()
    raw = re.sub(r"```$", "", raw.strip()).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def ensure_groq_key() -> None:
    if not GROQ_API_KEY:
        raise HTTPException(
            status_code=400,
            detail="Groq API key ulanmagan. Papkadagi .env faylga GROQ_API_KEY=... qoвЂying va run_app.bat ni qayta ishga tushiring.",
        )


def language_name(code: str) -> str:
    return {
        "auto": "Detected language from the source",
        "uz": "Uzbek Latin",
        "ru": "Russian",
        "en": "English",
    }.get(code, "Uzbek Latin")


def groq_json(system: str, user: str, max_tokens: int = 4096) -> Dict[str, Any]:
    ensure_groq_key()
    url = f"{GROQ_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.22,
        "max_tokens": max_tokens,
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Groq bilan aloqa boвЂlmadi: {e}")
    if r.status_code >= 400:
        try:
            err = r.json()
        except Exception:
            err = r.text
        raise HTTPException(status_code=502, detail=f"Groq xatosi: {err}")
    data = r.json()
    content = data["choices"][0]["message"]["content"]
    try:
        return extract_json(content)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI JSON formatda qaytarmadi: {e}. Raw: {content[:500]}")


def groq_vision_json(system: str, prompt: str, image_bytes: bytes, mime_type: str, max_tokens: int = 4096) -> Dict[str, Any]:
    ensure_groq_key()
    if not mime_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Faqat rasm fayl yuklang.")
    if len(image_bytes) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Rasm hajmi 8 MB dan katta bo'lmasin.")
    url = f"{GROQ_BASE_URL}/chat/completions"
    data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode()}"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_VISION_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        "temperature": 0.15,
        "max_tokens": max_tokens,
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Rasmni AI o'qiy olmadi: {e}")
    if r.status_code >= 400:
        try:
            err = r.json()
        except Exception:
            err = r.text
        raise HTTPException(status_code=502, detail=f"Vision AI xatosi: {err}")
    content = r.json()["choices"][0]["message"]["content"]
    try:
        return extract_json(content)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Rasm javobi JSON formatda emas: {e}. Raw: {content[:500]}")


TEST_SYSTEM = """
You are Testchi AI, a strict professional quiz generator for Uzbek, Russian and English users.
Return ONLY valid JSON. No markdown. No comments.

CRITICAL RULES:
- Create meaningful educational MCQ questions only from the provided topic/context.
- If the input is only a topic, expand it using reliable school-level/general educational knowledge about that topic.
- Do NOT create generic/filler questions such as: "Berilgan materialda ... tushunchasi qaysi mazmun bilan bog'liq?"
- Do NOT ask meta questions about the source text, material, paragraph, author, or "what is this topic about".
- Do NOT put another question inside an option.
- Do NOT put answer keys, option lists, or strings like "A) 1,2,3" inside an option.
- Each option must be a short possible answer, not a sentence like "Mavzuga aloqasi bo'lmagan tasodifiy fikr".
- If the source is an existing test PDF, rewrite it cleanly: one question + four clean options. Do not mix several questions together.
- Questions must be clear, useful and specific: facts, definitions, causes, results, comparisons, examples, dates, formulas or key concepts.
- Wrong options must be plausible, same category as the right option, and not silly/random.
- The requested output language is mandatory for EVERY generated field: title, summary, questions, options and explanations.
- Uzbek output must be natural Uzbek Latin only. Russian output must be Russian only. English output must be English only.
- Never mix Uzbek, Russian and English in one result. If the source language differs, translate the generated result into the requested language.
- Exactly 4 options per question.
- correct_index must be 0, 1, 2 or 3.
- Explanation must briefly explain why the correct answer is correct.
""".strip()

FLASH_SYSTEM = """
You are Testchi AI, a strict flashcard generator.
Return ONLY valid JSON. No markdown. No comments.
Cards must be useful for memorization. Each card has front question/term, back answer/explanation, and hint.
If the input is only a topic, expand it using reliable school-level/general educational knowledge.
Front side must be one short term or one direct question. Back side must be a clear answer in 1-3 short sentences.
Do not make vague cards such as "Bu mavzu nima haqida?".
The requested output language is mandatory for every field: title, front, back, hint and tag.
Uzbek output must be natural Uzbek Latin only. Russian output must be Russian only. English output must be English only.
Never mix Uzbek, Russian and English in one result. Translate source content when needed.
""".strip()



BAD_OPTION_PHRASES = [
    "mavzuga aloqasi bo'lmagan", "mavzuga aloqasi boвЂlmagan", "tasodifiy fikr",
    "hech qanday izohsiz", "faqat nom sifatida", "berilgan materialda",
    "andijon a)", "javob:", "to'g'ri javob", "toвЂgвЂri javob",
]


def strip_option_label(value: str) -> str:
    value = str(value or "").strip()
    value = re.sub(r"^[A-Da-d][\)\].:\-]\s*", "", value).strip()
    return value


BAD_QUESTION_PHRASES = [
    "berilgan material", "berilgan matn", "ushbu material", "ushbu matn",
    "manbada", "matnda", "materialda", "qaysi mazmun bilan bog",
    "nima haqida so'z yuritiladi", "nima haqida gap boradi",
]


def is_clean_question(text: str) -> bool:
    t = str(text or "").strip()
    low = t.lower()
    if len(t) < 12 or len(t) > 260:
        return False
    if any(p in low for p in BAD_QUESTION_PHRASES):
        return False
    if low.count("?") > 1:
        return False
    return True


def is_clean_option(text: str) -> bool:
    t = strip_option_label(text)
    low = t.lower()
    if len(t) < 2 or len(t) > 140:
        return False
    if "?" in t:
        return False
    if re.search(r"\b[A-Da-d][\)\].:]", t):
        return False
    if re.search(r"\b\d+\s*,\s*\d+\s*,", t):
        return False
    if any(p in low for p in BAD_OPTION_PHRASES):
        return False
    return True


def normalize_questions(raw_questions: Any, count: int) -> List[Dict[str, Any]]:
    cleaned: List[Dict[str, Any]] = []
    seen = set()
    if not isinstance(raw_questions, list):
        return cleaned
    for q in raw_questions:
        if not isinstance(q, dict):
            continue
        question = str(q.get("question", "")).strip()
        if not is_clean_question(question):
            continue
        opts = q.get("options") or []
        if not isinstance(opts, list) or len(opts) != 4:
            continue
        opts = [strip_option_label(x) for x in opts[:4]]
        if len(set(o.lower() for o in opts)) != 4:
            continue
        if not all(is_clean_option(o) for o in opts):
            continue
        try:
            ci = int(q.get("correct_index", q.get("answer_index", 0)))
        except Exception:
            ci = 0
        if ci < 0 or ci > 3:
            continue
        key = re.sub(r"\W+", "", question.lower())[:90]
        if key in seen:
            continue
        seen.add(key)
        cleaned.append({
            "question": question,
            "options": opts,
            "correct_index": ci,
            "answer_index": ci,
            "explanation": str(q.get("explanation", "")).strip()[:500],
        })
        if len(cleaned) >= count:
            break
    return cleaned

def make_test_from_context(title: str, context: str, count: int, language: str, level: str, source: str) -> Dict[str, Any]:
    language = language if language in {"uz", "ru", "en"} else "uz"
    count = max(3, min(50, int(count)))
    context = clean_text(context)
    level_policy = {
        "easy": "Easy still must be meaningful: no childish wording, no obvious joke answers.",
        "medium": "Medium must be serious exam style for school/college students. Avoid overly obvious one-word questions.",
        "hard": "Hard must require reasoning, comparison, chronology, cause-effect, or precise knowledge. Make distractors very plausible.",
    }.get(level, "Medium must be serious exam style for school/college students.")
    source_policy = (
        "TOPIC MODE: The input may be only a short topic. Build a complete useful quiz from standard educational knowledge about this topic. "
        "Do not complain about missing context. Do not ask what the topic means."
        if source == "topic"
        else
        "PDF/TEXT MODE: Use the extracted text as the main source. First mentally clean the text, ignore page numbers/headers/noise, "
        "then create questions from the real concepts in the text. If the PDF already contains tests, rewrite them cleanly."
    )
    style_policy = (
        "Make the quiz understandable for a real student. Mix question types: definition, fact, cause/result, comparison, example/application, sequence/date if relevant. "
        "Each question must stand alone without needing to read the original paragraph. Avoid vague words like 'material', 'text', 'given source'."
    )
    user_base = f"""
Create {count} high-quality multiple choice test questions.
Language: {language_name(language)}.
Difficulty: {level}.
Title/topic: {title}
Source type: {source}

SOURCE POLICY:
{source_policy}

QUALITY STYLE:
{style_policy}

DIFFICULTY POLICY:
{level_policy}

IMPORTANT:
- Use the useful meaning from the source/topic, not random unrelated trivia.
- Do not make nonsense options.
- Do not create options that are questions.
- Do not combine several answer choices into one option.
- If the context is messy PDF text, first understand it, then create clean questions.
- Correct answers must be unambiguous.
- Explanations must be written like a teacher explaining to a student.
- Avoid childish, too obvious, or primitive questions. Make it feel like a real assessment.
- Distractors must be close enough that the student has to know the topic, not guess instantly.

Context/topic text:
{context}

Return ONLY this exact JSON shape:
{{
  "title": "short title",
  "language": "{language}",
  "level": "{level}",
  "summary": "2-3 sentence useful summary",
  "questions": [
    {{
      "question": "clear specific question",
      "options": ["clean option A", "clean option B", "clean option C", "clean option D"],
      "correct_index": 0,
      "explanation": "short explanation"
    }}
  ]
}}
""".strip()

    last_error = ""
    for attempt in range(2):
        extra = "" if attempt == 0 else """

Your previous result contained invalid/generic options. Regenerate from scratch.
BAD EXAMPLES TO AVOID:
- Option: "A. AQSH prezidenti Richard Nikson..." inside another question
- Option: "D. Andijon A) 2, 3, 4..."
- Option: "Mavzuga aloqasi bo'lmagan tasodifiy fikr"
- Question: "Berilgan materialda ... tushunchasi qaysi mazmun bilan bog'liq?"
Every option must be a real short answer.
"""
        data = groq_json(TEST_SYSTEM, user_base + extra, max_tokens=7000)
        cleaned = normalize_questions(data.get("questions"), count)
        if len(cleaned) >= min(3, count):
            return {
                "title": str(data.get("title") or title).strip()[:180],
                "language": language,
                "level": level,
                "summary": str(data.get("summary") or "").strip(),
                "questions": cleaned[:count],
                "source": source,
            }
        last_error = f"AI {len(cleaned)} ta toza savol qaytardi, kerak: {count}"
    raise HTTPException(
        status_code=502,
        detail=f"AI savollarni sifatsiz formatda qaytardi. Qayta urinib koвЂring yoki PDF/mavzuni aniqroq bering. {last_error}",
    )


def is_clean_card(front: str, back: str) -> bool:
    f = str(front or "").strip().lower()
    b = str(back or "").strip().lower()
    if len(f) < 2 or len(f) > 120:
        return False
    if len(b) < 2 or len(b) > 700:
        return False
    if any(p in f for p in ["bu mavzu nima", "nima haqida", "berilgan material", "ushbu matn"]):
        return False
    return True


def normalize_cards(raw_cards: Any, count: int) -> List[Dict[str, Any]]:
    cleaned: List[Dict[str, Any]] = []
    seen = set()
    if not isinstance(raw_cards, list):
        return cleaned
    for c in raw_cards:
        if not isinstance(c, dict):
            continue
        front = str(c.get("front", "")).strip()
        back = str(c.get("back", "")).strip()
        if not is_clean_card(front, back):
            continue
        key = re.sub(r"\W+", "", front.lower())[:80]
        if key in seen:
            continue
        seen.add(key)
        cleaned.append({
            "front": front,
            "back": back,
            "hint": str(c.get("hint", "")).strip()[:160],
            "tag": str(c.get("tag", "General")).strip()[:60],
        })
        if len(cleaned) >= count:
            break
    return cleaned


def normalize_image_cards(data: Dict[str, Any], count: int) -> List[Dict[str, Any]]:
    cards = data.get("cards")
    if not isinstance(cards, list) and isinstance(data.get("entries"), list):
        cards = data.get("entries")
    normalized = []
    if isinstance(cards, list):
        for card in cards:
            if isinstance(card, dict) and "front" not in card:
                front = card.get("word") or card.get("term") or card.get("source") or card.get("question")
                back = card.get("meaning") or card.get("translation") or card.get("answer") or card.get("back")
                card = {
                    "front": front,
                    "back": back,
                    "hint": card.get("hint") or card.get("note") or "",
                    "tag": card.get("tag") or "Vocabulary",
                }
            normalized.append(card)
    return normalize_cards(normalized, count)


def make_flashcards(source_text: str, count: int, language: str, level: str, source_type: str) -> Dict[str, Any]:
    language = language if language in {"uz", "ru", "en"} else "uz"
    count = max(3, min(50, int(count)))
    source_text = clean_text(source_text)
    source_policy = (
        "TOPIC MODE: If this is a short topic, create cards from standard educational knowledge about it. Do not complain about missing text."
        if len(source_text) < 180
        else
        "TEXT MODE: Extract the most important terms, facts, definitions, causes, dates and examples from the text. Ignore noise."
    )
    user = f"""
Create {count} useful flashcards for memorization.
Language: {language_name(language)}.
Difficulty: {level}.
Source type: {source_type}.

SOURCE POLICY:
{source_policy}

Rules:
- Front side = short question/term only.
- Back side = clear answer/explanation.
- Do not put long paragraphs on the front.
- Do not repeat the same card.
- Prefer important concepts over tiny details.
- Use simple student-friendly language.

Source/topic:
{source_text}

Return this exact JSON shape:
{{
  "title": "short title",
  "language": "{language}",
  "level": "{level}",
  "cards": [
    {{
      "front": "question or term",
      "back": "answer or explanation",
      "hint": "short hint",
      "tag": "main category"
    }}
  ]
}}
""".strip()
    data: Dict[str, Any] = {}
    cleaned: List[Dict[str, Any]] = []
    last_error = ""
    for attempt in range(2):
        extra = "" if attempt == 0 else "\nRegenerate from scratch. Avoid vague cards. Every card must teach one concrete fact or concept."
        data = groq_json(FLASH_SYSTEM, user + extra, max_tokens=5500)
        cleaned = normalize_cards(data.get("cards"), count)
        if len(cleaned) >= min(3, count):
            break
        last_error = f"AI {len(cleaned)} ta toza kartochka qaytardi"
    if len(cleaned) < 3:
        raise HTTPException(status_code=502, detail="AI kartochka formatini notoвЂgвЂri qaytardi. Qayta urinib koвЂring.")
    return {
        "title": str(data.get("title") or source_text[:60]).strip()[:180],
        "language": language,
        "level": level,
        "cards": cleaned,
        "source": source_type,
    }


def make_flashcards_from_image(image_bytes: bytes, mime_type: str, count: int, language: str, level: str) -> Dict[str, Any]:
    language = language if language in {"auto", "uz", "ru", "en"} else "uz"
    requested_count = int(count or 0)
    auto_count = requested_count <= 0
    count = 50 if auto_count else max(3, min(50, requested_count))
    count_line = "Create one card for every clear useful item you can read, up to 50 cards." if auto_count else f"Create {count} practical flashcards from the visible content."
    language_line = (
        "Detect the language pair or language from the image. Preserve the natural direction in the image: English-Russian stays English-Russian, Uzbek-English stays Uzbek-English, etc."
        if language == "auto"
        else f"Output language: {language_name(language)}."
    )
    prompt = f"""
Read the uploaded photo carefully. It may be a vocabulary page, notebook, textbook table, screenshot, or printed list.
{count_line}
{language_line}
Difficulty: {level}.

Rules:
- First extract useful visible words, terms, definitions, translations, examples, dates, formulas or pairs.
- If the image is a numbered vocabulary table, create one flashcard for every readable numbered row. Do not stop early when answers are short.
- Count the visible numbered rows and set "expected_count" to that number.
- If a row has a readable source word but the meaning/translation is missing, hidden, cut off, or blank, infer the correct translation yourself and mark the hint as "Tarjima AI tomonidan to'ldirildi".
- For vocabulary: front = source word/phrase as shown, back = matching translation/meaning from the image. Do not translate into another language unless the image itself implies it.
- Short meanings are valid answers: keep entries like "Oyoq", "Erta", "Biroz", "Rahmat".
- For school notes: front = precise question or key term, back = concise answer.
- Ignore page noise, icons, page numbers, ads, watermarks and repeated headers.
- Do not invent unrelated content. If something is unreadable, skip it.
- Cards must be concrete and useful, not childish and not vague.
- Return ONLY JSON with fields: title, summary, expected_count, cards.
- cards is an array of objects: front, back, hint, tag. Include all visible numbered entries in order.
""".strip()
    data = groq_vision_json(FLASH_SYSTEM, prompt, image_bytes, mime_type, max_tokens=7000)
    cleaned = normalize_image_cards(data, count)
    try:
        expected_count = min(50, int(data.get("expected_count") or 0))
    except Exception:
        expected_count = 0
    if expected_count and len(cleaned) < min(expected_count, count):
        retry_prompt = prompt + f"""

You returned only {len(cleaned)} usable cards, but the image appears to contain {expected_count} numbered entries.
Re-read the image from top to bottom and return ALL numbered rows as cards.
For rows with hidden/missing meanings, infer the translation instead of skipping the row.
Do not merge rows. Do not skip short meanings.
"""
        data2 = groq_vision_json(FLASH_SYSTEM, retry_prompt.strip(), image_bytes, mime_type, max_tokens=8000)
        cleaned2 = normalize_image_cards(data2, count)
        if len(cleaned2) > len(cleaned):
            data = data2
            cleaned = cleaned2
    minimum = 1 if auto_count else min(3, count)
    if len(cleaned) < minimum:
        raise HTTPException(status_code=502, detail="Rasmdan yetarli aniq kartochka chiqmadi. Rasmni yaqinroq va tiniqroq oling.")
    return {
        "title": str(data.get("title") or "Rasmdan kartochkalar").strip()[:180],
        "language": language,
        "level": level,
        "cards": cleaned,
        "summary": str(data.get("summary") or "Rasm ichidagi matndan yaratilgan kartochkalar.").strip()[:500],
        "source": "image",
    }


@app.get("/")
def home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/result/{item_id}")
def result_page(item_id: str) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health(request: Request) -> Dict[str, Any]:
    owner_id = request_owner(request)
    return {
        "ok": True,
        "app": APP_NAME,
        "ai_provider": "Groq" if bool(GROQ_API_KEY) else "not_configured",
        "model": GROQ_MODEL,
        "credits": get_credits(owner_id),
        "quota": quota_status(owner_id),
        "message": "Groq ulangan" if GROQ_API_KEY else "Groq API key .env ichida yoвЂq",
    }


@app.get("/api/credits")
def credits(request: Request) -> Dict[str, Any]:
    owner_id = request_owner(request)
    return {"credits": get_credits(owner_id)}


@app.post("/api/topic-test")
def topic_test(req: TopicTestRequest, request: Request) -> Dict[str, Any]:
    owner_id = request_owner(request)
    ensure_groq_key()
    check_credit(1, owner_id)
    result = make_test_from_context(req.topic, req.topic, req.count, req.language, req.level, "topic")
    deduct_credit(1, owner_id)
    item = save_item("test", result["title"], result, owner_id)
    return {"item": item, "credits": get_credits(owner_id)}


@app.post("/api/pdf-test")
async def pdf_test(
    request: Request,
    file: UploadFile = File(...),
    language: str = Form("uz"),
    count: int = Form(10),
    level: str = Form("medium"),
) -> Dict[str, Any]:
    owner_id = request_owner(request)
    ensure_groq_key()
    check_credit(1, owner_id)
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Faqat PDF fayl yuklang.")
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", file.filename)[:120]
    path = UPLOAD_DIR / f"{uuid.uuid4()}_{safe_name}"
    with open(path, "wb") as f:
        f.write(await file.read())
    text = extract_pdf_text(path)
    title = Path(file.filename).stem
    result = make_test_from_context(title, text, max(3, min(50, count)), language, level, "pdf")
    result["filename"] = file.filename
    deduct_credit(1, owner_id)
    item = save_item("test", result["title"], result, owner_id)
    return {"item": item, "credits": get_credits(owner_id)}


@app.post("/api/flashcards")
def flashcards(req: FlashcardRequest, request: Request) -> Dict[str, Any]:
    owner_id = request_owner(request)
    ensure_groq_key()
    check_credit(1, owner_id)
    source = clean_text(req.source)
    result = make_flashcards(source, req.count, req.language, req.level, req.source_type)
    deduct_credit(1, owner_id)
    item = save_item("flashcards", result["title"], result, owner_id)
    return {"item": item, "credits": get_credits(owner_id)}


@app.post("/api/flashcards-image")
async def flashcards_image(
    request: Request,
    file: UploadFile = File(...),
    language: str = Form("auto"),
    count: int = Form(0),
    level: str = Form("medium"),
) -> Dict[str, Any]:
    owner_id = request_owner(request)
    ensure_groq_key()
    check_credit(1, owner_id)
    mime_type = file.content_type or ""
    if not mime_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Faqat rasm fayl yuklang.")
    image_bytes = await file.read()
    result = make_flashcards_from_image(image_bytes, mime_type, count, language, level)
    deduct_credit(1, owner_id)
    item = save_item("flashcards", result["title"], result, owner_id)
    return {"item": item, "credits": get_credits(owner_id)}


@app.get("/api/library")
def library(request: Request) -> Dict[str, Any]:
    owner_id = request_owner(request)
    with db() as conn:
        rows = conn.execute("SELECT * FROM library WHERE owner_id=? ORDER BY created_at DESC LIMIT 200", (owner_id,)).fetchall()
    return {"items": [row_to_item(r) for r in rows], "credits": get_credits(owner_id)}


@app.get("/api/item/{item_id}")
def get_item(item_id: str, request: Request) -> Dict[str, Any]:
    owner_id = request_owner(request)
    with db() as conn:
        row = conn.execute("SELECT * FROM library WHERE id=? AND owner_id=?", (item_id, owner_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Topilmadi")
    return {"item": row_to_item(row)}


@app.delete("/api/item/{item_id}")
def delete_item(item_id: str, request: Request) -> Dict[str, Any]:
    owner_id = request_owner(request)
    with db() as conn:
        conn.execute("DELETE FROM library WHERE id=? AND owner_id=?", (item_id, owner_id))
        conn.commit()
    return {"ok": True}


@app.get("/api/export/{item_id}.txt")
def export_txt(item_id: str, request: Request) -> PlainTextResponse:
    owner_id = request_owner(request)
    with db() as conn:
        row = conn.execute("SELECT * FROM library WHERE id=? AND owner_id=?", (item_id, owner_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Topilmadi")
    item = row_to_item(row)
    p = item["payload"]
    lines = [item["title"], f"Created: {item['created_at']}", ""]
    if item["item_type"] == "test":
        lines.append(p.get("summary", ""))
        lines.append("")
        for i, q in enumerate(p.get("questions", []), 1):
            lines.append(f"{i}. {q['question']}")
            letters = "ABCD"
            for idx, opt in enumerate(q["options"]):
                mark = "*" if idx == q["correct_index"] else " "
                lines.append(f"   {letters[idx]}) {opt} {mark}")
            lines.append(f"   Izoh: {q.get('explanation','')}")
            lines.append("")
    else:
        for i, c in enumerate(p.get("cards", []), 1):
            lines.append(f"{i}. FRONT: {c['front']}")
            lines.append(f"   BACK: {c['back']}")
            if c.get("hint"):
                lines.append(f"   Hint: {c['hint']}")
            lines.append("")
    filename = re.sub(r"[^\w.-]+", "_", item["title"])[:80] or "export"
    return PlainTextResponse("\n".join(lines), headers={"Content-Disposition": f"attachment; filename={filename}.txt"})


@app.post("/api/dev/add-credits")
def add_credits(request: Request, amount: int = 20) -> Dict[str, Any]:
    # MVP helper. Later this route should be protected by admin/payment.
    owner_id = request_owner(request)
    set_credits(get_credits(owner_id) + max(1, min(500, amount)), owner_id)
    return {"credits": get_credits(owner_id)}


@app.post("/api/premium/buy")
def buy_premium(req: Dict[str, Any], request: Request) -> Dict[str, Any]:
    owner_id = request_owner(request)
    days = max(1, min(365, int(req.get("days") or 7)))
    plan = str(req.get("plan") or f"{days} kun")[:80]
    provider = str(req.get("provider") or "click").lower()
    requested_amount = int(req.get("amount") or 0)
    amount = stars_amount(days, requested_amount) if provider == "stars" else plan_amount(days, requested_amount)
    payment = create_payment(provider, days, amount, plan, owner_id)
    if provider == "payme":
        if not PAYME_MERCHANT_ID:
            raise HTTPException(status_code=400, detail="PAYME_MERCHANT_ID .env ichida yoвЂq")
        payload = f"m={PAYME_MERCHANT_ID};ac.order_id={payment['id']};a={amount * 100};c={PUBLIC_BASE_URL}/"
        encoded_payload = quote(base64.b64encode(payload.encode()).decode(), safe="")
        pay_url = f"{PAYME_CHECKOUT_URL}/{encoded_payload}"
    elif provider == "stars":
        language = request.headers.get("x-app-language", "uz").lower()
        description = {
            "ru": "Возможности Testchi AI Premium",
            "en": "Testchi AI Premium features",
        }.get(language, "Testchi AI Premium imkoniyatlari")
        result = telegram_api("createInvoiceLink", {
            "title": f"Premium - {plan}",
            "description": description,
            "payload": payment["id"],
            "currency": "XTR",
            "prices": [{"label": plan, "amount": amount}],
        })
        pay_url = str(result.get("result") or "")
    elif provider == "click":
        if not CLICK_MERCHANT_ID or not CLICK_SERVICE_ID:
            raise HTTPException(status_code=400, detail="CLICK_MERCHANT_ID yoki CLICK_SERVICE_ID .env ichida yoвЂq")
        pay_url = f"{CLICK_PAY_URL}?{urlencode({'service_id': CLICK_SERVICE_ID, 'merchant_id': CLICK_MERCHANT_ID, 'amount': amount, 'transaction_param': payment['id'], 'return_url': PUBLIC_BASE_URL + '/'})}"
    else:
        raise HTTPException(status_code=400, detail="provider stars, payme yoki click bolishi kerak")
    merge_payment_payload(payment["id"], {"pay_url": pay_url})
    return {"ok": True, "provider": provider, "payment_id": payment["id"], "pay_url": pay_url, "amount": amount}


@app.get("/api/payment/{payment_id}")
def payment_status(payment_id: str, request: Request) -> Dict[str, Any]:
    owner_id = request_owner(request)
    payment = get_payment(payment_id)
    if not payment or clean_owner_id(payment.get("owner_id") or DEFAULT_OWNER) != owner_id:
        raise HTTPException(status_code=404, detail="Payment topilmadi")
    return {
        "id": payment["id"],
        "provider": payment["provider"],
        "status": payment["status"],
        "amount": payment["amount"],
        "paid_at": payment.get("paid_at") or "",
        "quota": quota_status(owner_id),
    }


@app.post("/api/telegram/webhook")
async def telegram_webhook(request: Request) -> Dict[str, Any]:
    update = await request.json()
    pre_checkout = update.get("pre_checkout_query")
    if pre_checkout:
        telegram_api("answerPreCheckoutQuery", {"pre_checkout_query_id": pre_checkout.get("id"), "ok": True})
        return {"ok": True}
    message = update.get("message") or {}
    successful_payment = message.get("successful_payment") or {}
    if successful_payment:
        payment_id = str(successful_payment.get("invoice_payload") or "")
        payment = get_payment(payment_id)
        if payment and payment["provider"] == "stars":
            mark_payment_paid(payment_id, str(successful_payment.get("telegram_payment_charge_id") or ""), {
                "telegram_successful_payment": successful_payment,
                "telegram_user_id": (message.get("from") or {}).get("id"),
                "currency": successful_payment.get("currency"),
                "total_amount": successful_payment.get("total_amount"),
            })
        return {"ok": True}
    text = str(message.get("text") or "").strip()
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id and text.startswith("/start"):
        telegram_api("sendMessage", {
            "chat_id": chat_id,
            "text": "Testchi AI tayyor. Platformani ochish uchun pastdagi tugmani bosing.",
            "reply_markup": {
                "inline_keyboard": [[{
                    "text": "Testchi AI ni ochish",
                    "web_app": {"url": TELEGRAM_WEBAPP_URL},
                }]]
            },
        })
        return {"ok": True}
    return {"ok": True}


@app.get("/api/telegram/set-webhook")
@app.post("/api/telegram/set-webhook")
def telegram_set_webhook() -> Dict[str, Any]:
    webhook_url = f"{PUBLIC_BASE_URL}/api/telegram/webhook"
    webhook_result = telegram_api("setWebhook", {"url": webhook_url})
    menu_result = set_telegram_menu_button()
    return {
        "ok": True,
        "webhook": webhook_result.get("result"),
        "menu_button": menu_result.get("result"),
        "url": webhook_url,
        "webapp_url": TELEGRAM_WEBAPP_URL,
    }


@app.get("/api/telegram/set-menu-button")
@app.post("/api/telegram/set-menu-button")
def telegram_set_menu_button() -> Dict[str, Any]:
    result = set_telegram_menu_button()
    return {"ok": True, "result": result.get("result"), "webapp_url": TELEGRAM_WEBAPP_URL}


@app.get("/api/telegram/webhook-info")
def telegram_webhook_info() -> Dict[str, Any]:
    result = telegram_api("getWebhookInfo", {})
    return {"ok": True, "result": result.get("result")}


@app.post("/api/telegram/profile")
def telegram_profile(req: Dict[str, Any]) -> Dict[str, Any]:
    user = req.get("user") or {}
    user_id = str(user.get("id") or "")
    name = str(user.get("username") or " ".join(str(user.get(k) or "") for k in ("first_name", "last_name")).strip() or "Telegram user")
    photo_url = str(user.get("photo_url") or "")
    if user_id and TELEGRAM_BOT_TOKEN and not photo_url:
        photo_url = f"/api/telegram/photo/{user_id}"
    return {"ok": True, "id": user_id, "name": name, "photo_url": photo_url}


@app.get("/api/telegram/photo/{user_id}")
def telegram_photo(user_id: str) -> Response:
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=404, detail="Telegram token yo'q")
    photos = telegram_api("getUserProfilePhotos", {"user_id": int(user_id), "limit": 1}).get("result") or {}
    items = photos.get("photos") or []
    if not items:
        raise HTTPException(status_code=404, detail="Profil rasmi topilmadi")
    best = sorted(items[0], key=lambda x: int(x.get("file_size") or 0))[-1]
    file_info = telegram_api("getFile", {"file_id": best.get("file_id")}).get("result") or {}
    file_path = file_info.get("file_path")
    if not file_path:
        raise HTTPException(status_code=404, detail="Profil rasmi fayli topilmadi")
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Profil rasmi olinmadi: {e}")
    return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"))


def payme_error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": {"ru": message, "uz": message, "en": message}}}


def payme_ok(request_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def payme_transaction_result(payment: Dict[str, Any]) -> Dict[str, Any]:
    payload = payment.get("payload") or {}
    status = str(payment.get("status") or "")
    state = 2 if status == "paid" else -1 if status == "cancelled" else 1
    return {
        "create_time": int(payload.get("payme_create_time") or 0),
        "perform_time": int(payload.get("paid_time") or 0),
        "cancel_time": int(payload.get("cancel_time") or 0),
        "transaction": payment["id"],
        "state": state,
        "reason": payload.get("cancel_reason"),
    }


def payme_authorized(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("basic ") or not PAYME_SECRET_KEY:
        return False
    try:
        raw = base64.b64decode(auth.split(" ", 1)[1]).decode()
    except Exception:
        return False
    return raw == f"Paycom:{PAYME_SECRET_KEY}"


@app.post("/api/payme/callback")
async def payme_callback(request: Request) -> Dict[str, Any]:
    body = await request.json()
    request_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}
    if not payme_authorized(request):
        return payme_error(request_id, -32504, "Insufficient privileges")
    account = params.get("account") or {}
    payment_id = str(account.get("order_id") or "")
    payment = get_payment(payment_id)
    if method == "CheckPerformTransaction":
        if not payment:
            return payme_error(request_id, -31050, "Order not found")
        if int(params.get("amount") or 0) != int(payment["amount"]) * 100:
            return payme_error(request_id, -31001, "Incorrect amount")
        if payment["status"] in {"paid", "cancelled"}:
            return payme_error(request_id, -31008, "Order is not payable")
        return payme_ok(request_id, {"allow": True})
    if method == "CreateTransaction":
        if not payment:
            return payme_error(request_id, -31050, "Order not found")
        if int(params.get("amount") or 0) != int(payment["amount"]) * 100:
            return payme_error(request_id, -31001, "Incorrect amount")
        provider_tid = str(params.get("id") or "")
        existing_tid = str(payment.get("provider_transaction_id") or "")
        if payment["status"] == "paid":
            return payme_error(request_id, -31008, "Order already paid")
        if payment["status"] == "cancelled":
            return payme_error(request_id, -31008, "Order cancelled")
        if existing_tid and existing_tid != provider_tid:
            return payme_error(request_id, -31008, "Another transaction already exists")
        create_time = int((payment.get("payload") or {}).get("payme_create_time") or int(time.time() * 1000))
        merge_payment_payload(payment_id, {"payme": params, "payme_create_time": create_time})
        update_payment(payment_id, provider_transaction_id=provider_tid)
        return payme_ok(request_id, {"create_time": create_time, "transaction": payment_id, "state": 1})
    if method == "PerformTransaction":
        provider_tid = str(params.get("id") or "")
        target = payment
        if not target and provider_tid:
            target = payment_by_provider_transaction(provider_tid)
        if not target:
            return payme_error(request_id, -31003, "Transaction not found")
        if target["status"] == "cancelled":
            return payme_error(request_id, -31008, "Transaction cancelled")
        if provider_tid and target.get("provider_transaction_id") and str(target.get("provider_transaction_id")) != provider_tid:
            return payme_error(request_id, -31003, "Transaction not found")
        paid = mark_payment_paid(target["id"], provider_tid, {"payme_perform": params})
        perform_time = int((paid.get("payload") or {}).get("paid_time") or int(time.time() * 1000))
        return payme_ok(request_id, {"transaction": paid["id"], "perform_time": perform_time, "state": 2})
    if method == "CheckTransaction":
        provider_tid = str(params.get("id") or "")
        target = payment_by_provider_transaction(provider_tid)
        if not target:
            return payme_error(request_id, -31003, "Transaction not found")
        return payme_ok(request_id, payme_transaction_result(target))
    if method == "CancelTransaction":
        provider_tid = str(params.get("id") or "")
        target = payment_by_provider_transaction(provider_tid)
        if not target:
            return payme_error(request_id, -31003, "Transaction not found")
        if target["status"] == "paid":
            return payme_error(request_id, -31007, "Service already provided")
        if target["status"] != "cancelled":
            cancel_time = int(time.time() * 1000)
            merge_payment_payload(target["id"], {"payme_cancel": params, "cancel_time": cancel_time, "cancel_reason": params.get("reason")})
            update_payment(target["id"], status="cancelled")
        else:
            cancel_time = int((target.get("payload") or {}).get("cancel_time") or int(time.time() * 1000))
        return payme_ok(request_id, {"transaction": target["id"], "cancel_time": cancel_time, "state": -1})
    return payme_error(request_id, -32601, "Method not found")


def click_response(error: int, error_note: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = {"error": error, "error_note": error_note}
    if extra:
        data.update(extra)
    return data


def click_sign_valid(data: Dict[str, Any]) -> bool:
    if not CLICK_SECRET_KEY:
        return False
    source = (
        str(data.get("click_trans_id", "")) + str(data.get("service_id", "")) + CLICK_SECRET_KEY +
        str(data.get("merchant_trans_id", "")) + str(data.get("amount", "")) +
        str(data.get("action", "")) + str(data.get("sign_time", ""))
    )
    return hashlib.md5(source.encode()).hexdigest() == str(data.get("sign_string", "")).lower()


@app.post("/api/click/prepare")
async def click_prepare(request: Request) -> Dict[str, Any]:
    form = dict(await request.form())
    payment_id = str(form.get("merchant_trans_id") or "")
    payment = get_payment(payment_id)
    if not click_sign_valid(form):
        return click_response(-1, "SIGN CHECK FAILED")
    if not payment:
        return click_response(-5, "ORDER NOT FOUND")
    if int(float(form.get("amount") or 0)) != int(payment["amount"]):
        return click_response(-2, "INCORRECT AMOUNT")
    merge_payment_payload(payment_id, {"click_prepare": form})
    update_payment(payment_id, provider_transaction_id=str(form.get("click_trans_id") or ""))
    return click_response(0, "Success", {"click_trans_id": form.get("click_trans_id"), "merchant_trans_id": payment_id, "merchant_prepare_id": payment_id})


@app.post("/api/click/complete")
async def click_complete(request: Request) -> Dict[str, Any]:
    form = dict(await request.form())
    payment_id = str(form.get("merchant_trans_id") or form.get("merchant_prepare_id") or "")
    payment = get_payment(payment_id)
    if not click_sign_valid(form):
        return click_response(-1, "SIGN CHECK FAILED")
    if not payment:
        return click_response(-5, "ORDER NOT FOUND")
    if str(form.get("error") or "0") != "0":
        merge_payment_payload(payment_id, {"click_complete": form, "cancel_time": int(time.time() * 1000)})
        update_payment(payment_id, status="cancelled")
        return click_response(0, "Payment cancelled")
    mark_payment_paid(payment_id, str(form.get("click_trans_id") or ""), {"click_complete": form})
    return click_response(0, "Success", {"click_trans_id": form.get("click_trans_id"), "merchant_trans_id": payment_id, "merchant_confirm_id": payment_id})




# ----------------------- Old mobile interface compatible API -----------------------

def normalize_old_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Old UI uses answer_index. Groq core uses correct_index."""
    data = json.loads(json.dumps(payload, ensure_ascii=False))
    if isinstance(data.get("questions"), list):
        for q in data["questions"]:
            if "answer_index" not in q:
                q["answer_index"] = int(q.get("correct_index", 0) or 0)
            if "correct_index" not in q:
                q["correct_index"] = int(q.get("answer_index", 0) or 0)
    return data


def old_item_summary(item: Dict[str, Any]) -> Dict[str, Any]:
    payload = item.get("payload", {}) or {}
    item_type = item.get("item_type", "")
    if item_type == "test":
        simple_type = "test"
        count = len(payload.get("questions", []) or [])
    else:
        simple_type = "flashcards"
        count = len(payload.get("cards", []) or [])
    return {
        "id": item.get("id"),
        "type": simple_type,
        "item_type": item_type,
        "title": item.get("title", ""),
        "subtitle": payload.get("summary") or item.get("created_at", ""),
        "count": count,
        "created_at": item.get("created_at", ""),
    }


@app.get("/api/profile")
def profile_old(request: Request) -> Dict[str, Any]:
    owner_id = request_owner(request)
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM library WHERE owner_id=?", (owner_id,)).fetchone()
    quota = quota_status(owner_id)
    return {
        "username": "",
        "items": int(row["c"] if row else 0),
        "plan": quota["plan"],
        "premium_active": quota["premium_active"],
        "premium_until": quota["premium_until"],
        "free_remaining": quota["free_remaining"],
        "free_cooldown_until": quota["free_cooldown_until"],
        "can_generate": quota["can_generate"],
        "purchases_today": today_paid_counts(),
    }


@app.post("/api/credits/add")
def add_credits_old(req: Dict[str, Any]) -> Dict[str, Any]:
    raise HTTPException(status_code=410, detail="Kredit tizimi o'chirilgan. Premium Telegram Stars orqali ulanadi.")


@app.post("/api/generate/topic-test")
def topic_test_old(req: TopicTestRequest, request: Request) -> Dict[str, Any]:
    owner_id = request_owner(request)
    ensure_groq_key()
    check_credit(1, owner_id)
    result = make_test_from_context(req.topic, req.topic, req.count, req.language, req.level, "topic")
    result = normalize_old_payload(result)
    deduct_credit(1, owner_id)
    item = save_item("test", result["title"], result, owner_id)
    return {"id": item["id"], "data": result, "item": item, "credits": get_credits(owner_id)}


@app.post("/api/generate/flashcards")
def flashcards_old(req: Dict[str, Any], request: Request) -> Dict[str, Any]:
    owner_id = request_owner(request)
    ensure_groq_key()
    check_credit(1, owner_id)
    text = clean_text(str(req.get("text") or req.get("source") or ""))
    if len(text) < 2:
        raise HTTPException(status_code=400, detail="Matn yoki mavzu yozing")
    language = str(req.get("language") or "uz")
    count = int(req.get("count") or 10)
    level = str(req.get("level") or "medium")
    result = make_flashcards(text, max(3, min(50, count)), language, level, "topic/text")
    deduct_credit(1, owner_id)
    item = save_item("flashcards", result["title"], result, owner_id)
    return {"id": item["id"], "data": result, "item": item, "credits": get_credits(owner_id)}


@app.post("/api/generate/flashcards-image")
async def flashcards_image_old(
    request: Request,
    file: UploadFile = File(...),
    language: str = Form("auto"),
    count: int = Form(0),
    level: str = Form("medium"),
) -> Dict[str, Any]:
    owner_id = request_owner(request)
    ensure_groq_key()
    check_credit(1, owner_id)
    mime_type = file.content_type or ""
    if not mime_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Faqat rasm fayl yuklang.")
    image_bytes = await file.read()
    result = make_flashcards_from_image(image_bytes, mime_type, int(count or 0), language, level)
    deduct_credit(1, owner_id)
    item = save_item("flashcards", result["title"], result, owner_id)
    return {"id": item["id"], "data": result, "item": item, "credits": get_credits(owner_id)}


@app.post("/api/generate/pdf-test")
async def pdf_test_old(
    request: Request,
    file: UploadFile = File(...),
    language: str = Form("uz"),
    count: int = Form(10),
    level: str = Form("medium"),
) -> Dict[str, Any]:
    owner_id = request_owner(request)
    ensure_groq_key()
    check_credit(1, owner_id)
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Faqat PDF fayl yuklang.")
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", file.filename)[:120]
    path = UPLOAD_DIR / f"{uuid.uuid4()}_{safe_name}"
    with open(path, "wb") as f:
        f.write(await file.read())
    text = extract_pdf_text(path)
    result = make_test_from_context(Path(file.filename).stem, text, max(3, min(50, count)), language, level, "pdf")
    result["filename"] = file.filename
    result = normalize_old_payload(result)
    deduct_credit(1, owner_id)
    item = save_item("test", result["title"], result, owner_id)
    return {"id": item["id"], "data": result, "item": item, "credits": get_credits(owner_id)}


# Override old UI library shape. This route is intentionally defined after the core /api/library in code order.
@app.get("/api/library/old")
def library_old_explicit(request: Request) -> Dict[str, Any]:
    owner_id = request_owner(request)
    with db() as conn:
        rows = conn.execute("SELECT * FROM library WHERE owner_id=? ORDER BY created_at DESC LIMIT 200", (owner_id,)).fetchall()
    items = [old_item_summary(row_to_item(r)) for r in rows]
    return {"items": items, "credits": get_credits(owner_id)}


@app.get("/api/item/old/{item_id}")
def get_item_old_explicit(item_id: str, request: Request) -> Dict[str, Any]:
    owner_id = request_owner(request)
    with db() as conn:
        row = conn.execute("SELECT * FROM library WHERE id=? AND owner_id=?", (item_id, owner_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Topilmadi")
    item = row_to_item(row)
    payload = normalize_old_payload(item["payload"])
    return {"id": item["id"], "type": "test" if item["item_type"] == "test" else "flashcards", "data": payload, **item}


@app.get("/api/export/txt/{item_id}")
def export_txt_old(item_id: str, request: Request):
    return export_txt(item_id, request)


@app.exception_handler(HTTPException)
def http_error_handler(request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "detail": exc.detail})


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8010"))
    url = f"http://127.0.0.1:{port}"
    print(f"Testchi AI ishga tushdi: {url}")
    uvicorn.run("main:app", host=host, port=port, reload=False)

