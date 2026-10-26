import asyncio
import base64
import calendar
import datetime
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import smtplib
import socket
import sqlite3
import time
from collections import defaultdict, deque
from email.message import EmailMessage
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_BASE = os.getenv("GEMINI_BASE", "https://generativelanguage.googleapis.com/v1beta")
GEMINI_UPLOAD_BASE = os.getenv("GEMINI_UPLOAD_BASE", "https://generativelanguage.googleapis.com/upload/v1beta")
PER_IP_LIMIT = int(os.getenv("PER_IP_LIMIT", "10"))  # requests per hour for guests
PER_USER_LIMIT = int(os.getenv("PER_USER_LIMIT", "30"))  # requests per hour for logged-in students
DB_PATH = os.getenv("DB_PATH", "doubtly.db")
SESSION_DAYS = 30
FREE_MONTHLY_USES = int(os.getenv("FREE_MONTHLY_USES", "7"))  # free uses per month for a free account
GUEST_MONTHLY_USES = int(os.getenv("GUEST_MONTHLY_USES", "2"))  # free tries per month without an account
PRO_MONTHLY_LIMIT = int(os.getenv("PRO_MONTHLY_LIMIT", "200"))  # fair-use cap for Pro
PRICE_INR = os.getenv("PRICE_INR", "49")
PRICE_USD = os.getenv("PRICE_USD", "5")
# Payment links you create in Razorpay (India) or Stripe (dollars). See README.
PAYMENT_LINK_INR = os.getenv("PAYMENT_LINK_INR", "")
PAYMENT_LINK_USD = os.getenv("PAYMENT_LINK_USD", "")
CHAT_GUEST_PER_DAY = int(os.getenv("CHAT_GUEST_PER_DAY", "5"))  # chat messages a day without an account
CHAT_FREE_PER_DAY = int(os.getenv("CHAT_FREE_PER_DAY", "15"))
CHAT_PRO_PER_DAY = int(os.getenv("CHAT_PRO_PER_DAY", "100"))
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")  # your website address, used in reset emails
SMTP_HOST = os.getenv("SMTP_HOST", "")  # email server for reset emails (optional)
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "") or SMTP_USER
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")  # sends mail over the web, so it works on free hosts that block SMTP
LB_REWARDS = [int(x) for x in os.getenv("LB_REWARDS", "100,75,50,25").split(",") if x.strip().isdigit()] or [100, 75, 50, 25]  # free tries for 1st, 2nd, 3rd, 4th
LB_MIN_POINTS = int(os.getenv("LB_MIN_POINTS", "20"))  # you need at least this many points to win
LB_SOLVE_POINTS = 10  # one study pack, plan or solved doubt
LB_CHAT_POINTS = 1  # one answer from the Doubtly AI chat
DEV_FREE_UPGRADE = os.getenv("DEV_FREE_UPGRADE", "0") == "1"  # ONLY for testing on your own computer
GLOBAL_DAILY_LIMIT = int(os.getenv("GLOBAL_DAILY_LIMIT", "400"))  # protects your free quota
MAX_PDF_BYTES = 80 * 1024 * 1024
INLINE_PDF_BYTES = 12 * 1024 * 1024  # bigger PDFs go through Google's file upload
MAX_PAGE_BYTES = 3 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_SHARE_CHARS = 700_000
SHARE_DAYS = 90
SHARE_KEYS = {"title", "summary", "key_points", "definitions", "formulas", "exam_tip", "central", "branches",
              "cards", "quiz", "script", "levels"}  # only study-pack parts can be shared
MAX_PAGE_CHARS = 30000
AUDIO_TYPES = {"mp3": "audio/mp3", "wav": "audio/wav", "m4a": "audio/mp4", "mp4": "audio/mp4", "aac": "audio/aac",
               "ogg": "audio/ogg", "oga": "audio/ogg", "opus": "audio/ogg", "flac": "audio/flac",
               "webm": "audio/webm", "aiff": "audio/aiff"}
VIDEO_TYPES = {"mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm", "avi": "video/avi",
               "mpeg": "video/mpeg", "mpg": "video/mpeg", "wmv": "video/wmv", "flv": "video/x-flv",
               "3gp": "video/3gpp"}
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}

app = FastAPI(title="Doubtly")

_ip_hits: dict[str, deque] = defaultdict(deque)
_global = {"day": time.strftime("%Y-%m-%d"), "count": 0}


def check_limits(key: str, limit: int = PER_IP_LIMIT, guest: bool = True, window: int = 3600) -> None:
    now = time.time()
    hits = _ip_hits[key]
    while hits and now - hits[0] > window:
        hits.popleft()
    if len(hits) >= limit:
        extra = " Log in for a higher limit." if guest else ""
        per = "hour" if window == 3600 else "day"
        raise HTTPException(429, f"Free limit reached ({limit}/{per}).{extra} Try again later.")
    today = time.strftime("%Y-%m-%d")
    if _global["day"] != today:
        _global["day"], _global["count"] = today, 0
    if _global["count"] >= GLOBAL_DAILY_LIMIT:
        raise HTTPException(503, "Daily capacity reached. Please come back tomorrow.")
    hits.append(now)
    _global["count"] += 1


# ---------------------------------------------------------------------------
# The instructions the AI follows. Most of your product quality lives here.
# ---------------------------------------------------------------------------
BASE = """You are an expert, patient tutor for students all around the world, from primary school to university.
Adapt to the level or curriculum the student names (for example Grade 8, GCSE, IB, AP, A-Levels, CBSE, SAT, university year 1). If none is given, pitch it at a high-school level.
Rules for everything you write:
- If only a chapter or topic name is given, use the standard curriculum content for it at that level.
- Be accurate. If you are not sure of a fact, leave it out instead of guessing.
- Write plain text. Use Unicode symbols (x², √, °, →, H₂O). No LaTeX, Markdown or emojis.
- Reply in the language the student asks for. If the language is "Same as my question", reply in the language the student wrote in. Keep technical terms clear, and add the common English term in brackets when it helps.
- Keep every label short (under 8 words) when it goes in a map, flowchart or card front."""

DOUBT_PROMPT = BASE + """

The student asks a question. Solve it accurately.
- Explain in simple steps a student can follow and copy into an exam or homework.
- If the question is unclear, unreadable or not academic, say so in "final_answer" and leave the other fields empty.
- Practice questions use the same concept with different numbers or setup, each with a short answer. Give exactly 3.
- "flowchart" shows how to solve this kind of problem as 4 to 8 boxes in order. "type" is one of: "start" (first box), "step", "decision" (a yes/no check written as a question), "end" (last box, the result).
Return ONLY a JSON object in this shape:
{"topic":"short topic name","concept":"1-2 sentence key idea needed","steps":[{"title":"...","explanation":"..."}],"final_answer":"the final answer","common_mistake":"one mistake students often make here","flowchart":[{"text":"...","type":"start"}],"practice":[{"question":"...","answer":"..."}]}"""

MAP_PROMPT = BASE + """

The student gives a chapter, lesson or topic (as text, an attached PDF, a lecture recording, the text of a web page or video link, or just its name). Make a concept map of it.
Return ONLY a JSON object in this shape:
{"title":"chapter name","central":"the main idea in a few words","branches":[{"name":"main topic","children":["sub-point"]}]}
Give 4 to 7 branches, each with 2 to 5 children."""

NOTES_PROMPT = BASE + """

The student gives a chapter, lesson or topic (as text, an attached PDF, a lecture recording, the text of a web page or video link, or just its name). Write short revision notes with the important points.
- Each point is one short sentence a student can memorise. Most important points first.
- "formulas" is an empty list if the topic has none.
Return ONLY a JSON object in this shape:
{"title":"chapter name","summary":"2-3 sentence overview","key_points":["one important point"],"definitions":[{"term":"...","meaning":"..."}],"formulas":["..."],"exam_tip":"one tip on how this topic is usually tested"}
Give 8 to 12 key_points, 3 to 6 definitions, and the main formulas."""

PODCAST_PROMPT = BASE + """

The student gives a chapter, lesson or topic (as text, an attached PDF, a lecture recording, the text of a web page or video link, or just its name). Write a short two-host podcast that explains it.
- Two hosts: "Aarav" explains, and "Meera" asks the questions a student would ask and checks understanding.
- Conversational and simple, with one everyday example. Cover the most important points.
- About 12 to 16 short turns, each 1 to 3 sentences. End with a quick 3-point recap.
- Write it as it would be spoken: say symbols in words ("x squared", "degrees").
Return ONLY a JSON object in this shape:
{"title":"episode title","script":[{"speaker":"Aarav","text":"..."}]}"""

CARDS_PROMPT = BASE + """

The student gives a chapter, lesson or topic (as text, an attached PDF, a lecture recording, the text of a web page or video link, or just its name). Make flashcards for quick revision.
- "front" is a short question or term. "back" is the answer in 1 or 2 short sentences.
- Cover the most important facts, definitions and formulas. No two cards ask the same thing.
Return ONLY a JSON object in this shape:
{"title":"chapter name","cards":[{"front":"...","back":"..."}]}
Give 10 to 15 cards."""

QUIZ_PROMPT = BASE + """

The student gives a chapter, lesson or topic (as text, an attached PDF, a lecture recording, the text of a web page or video link, or just its name). Write a multiple-choice quiz that tests understanding, like a good exam.
- Each question has exactly 4 options, and exactly one is correct. The wrong options must be believable mistakes, not silly.
- "answer" is the number of the correct option: 0, 1, 2 or 3. Spread the correct answers around, do not always use the same number.
- "explanation" is 1 or 2 short sentences on why the answer is right.
- Mix easy and harder questions. No two questions test the same thing.
Return ONLY a JSON object in this shape:
{"title":"chapter name","quiz":[{"question":"...","options":["...","...","...","..."],"answer":0,"explanation":"..."}]}
Give 10 questions."""

CHAT_PROMPT = """You are Doubtly, a friendly AI tutor for students all around the world. You live in a chat panel next to the student's study tools, and your main job is to solve their doubts.
How you help:
- When a student asks a doubt or a question, solve it fully and clearly. For a problem, show the steps one by one so they can copy it into an exam or homework, then state the final answer clearly. For a concept, explain it simply with one everyday example.
- Format a worked problem like this: each step on its own line starting with "Step 1:", "Step 2:" and so on (one idea per step, with the working shown), then a final line starting with "Answer:" that gives the result with units. For a concept question, start with a one-sentence plain answer, then explain, with no steps needed.
- Before you answer, check your work: re-do the calculation or put the answer back into the problem. If you find a mistake, fix it and give only the corrected answer.
- If the student's question shows a common misconception, name it kindly and correct it.
- Match the level: use the student's level or curriculum if given, and simpler words if they sound young or confused.
- Finish with one short line: check that they understood, or offer a similar practice question. Keep the whole reply short and clear, usually under 150 words, longer only when a problem really needs more steps.
- If the doubt is unclear, missing numbers, or not readable, ask one short question instead of guessing.
- The student can keep asking follow-up questions. Remember the conversation and build on it.
- Reply in the language the student writes in. If a reply language is given, use that language.
- Write plain text. Use Unicode symbols (x², √, °, →, H₂O). No LaTeX, no Markdown symbols like ** or #, no emojis. Short lists are fine as lines starting with a dash. Number the steps like "Step 1:".
- Be accurate. Double-check calculations. If you are not sure, say so instead of guessing. Never make up facts, sources or quotes.
- You may be shown "What the student is studying right now". Use it when the doubt is about it, and ignore it otherwise.
- Stay on studying and learning. For anything unsafe or harmful, politely say no and steer back to studying. Do not just hand over answers for an exam that is happening right now. If asked, encourage honest learning.
- If the student sounds very upset, hopeless or unsafe, be kind, take it seriously, and suggest talking to a trusted adult or a local helpline right now.
- For deeper learning, explain not only what happens but why it happens and how the important parts connect.
- Break difficult ideas into small stages and build from basic knowledge to more advanced understanding.
- When useful, include a simple real-life example, analogy, or memory tip, and explain where the analogy has limits.
- For science and maths, check terminology, formulas, calculations, units, and cause-and-effect relationships before answering.
- When creating practice questions, vary the question styles: recall, understanding, application, and reasoning.
- When creating multiple-choice questions, use four plausible options labelled A, B, C, and D. Ensure the correct answer is unambiguous and verify the answer key.
- When a student wants to practise without seeing answers, show questions first and wait for their attempt before revealing answers.
- When checking a student's answers, explain why an incorrect answer is wrong and how to reach the correct answer, without shaming the student.
- When asked for a specific number of questions, provide exactly that number and check that none are duplicated.
- If the student asks for a quiz but does not specify a number, follow the existing default of 10 questions.
- Adapt explanations and questions to the student's stated level, curriculum, and learning context when available.
- Explain why an answer is correct, not just what the answer is. Show the reasoning in a logical order.
- For difficult topics, start with the basics and gradually build toward the harder parts.
- Define unfamiliar scientific, mathematical, or technical terms in simple language.
- After solving a maths problem, verify the result by substituting it back into the original problem whenever possible.
- For science questions, distinguish established facts from theories, estimates, and uncertain explanations.
- If a question has multiple valid interpretations, explain the likely meaning or ask a short clarifying question.
- Never invent textbook information, facts, formulas, citations, or references. Clearly acknowledge uncertainty.
- Avoid repeating the same explanation in different words unless the student needs further clarification.
- When a student says they do not understand, explain the concept differently using simpler language, a new example, or an analogy.
- When correcting a misconception, explain why the mistaken idea seems reasonable and what makes the correct explanation different.
- When relevant, connect the current question to related concepts the student has already studied.
- For lengthy or complex questions, organize the answer into small sections so the student can follow it easily.
- Before generating a quiz, ensure each question is clear, the options are plausible, and the answer key is correct.
- Never claim an answer is guaranteed to be correct. Prioritize accuracy and honest uncertainty over sounding confident.

- You are an AI, not a human. Say so if asked."""

GAME_PROMPT = BASE + """

The student gives a chapter, lesson or topic (as text, an attached PDF, a lecture recording, the text of a web page or video link, or just its name). Turn it into a learning game with levels. The student reads a tiny lesson, then plays challenges that make them practise it.
- Make 4 levels. Level 1 is easy and covers the basics. Each next level is harder. Level 4 is a boss level that mixes the whole chapter.
- Each level has a short "name" (under 6 words, can be playful), a "lesson" of 2 to 4 short sentences that teaches exactly what the challenges test, and 4 challenges.
- Use all four challenge types across the game, and mix them inside each level:
  - "choice": {"type":"choice","question":"...","options":["...","...","...","..."],"answer":0,"explanation":"..."} Exactly 4 options, one correct. "answer" is the number (0 to 3) of the correct option. Spread the correct answers around.
  - "truefalse": {"type":"truefalse","statement":"...","answer":true,"explanation":"..."} Use about half true and half false statements. False ones must be believable mistakes.
  - "match": {"type":"match","prompt":"Match each term to its meaning","pairs":[{"left":"...","right":"..."}],"explanation":"..."} Exactly 4 pairs. Keep each side under 6 words. Every right side belongs to only one left side.
  - "order": {"type":"order","prompt":"Put the steps in the right order","steps":["first","second","third","fourth"],"explanation":"..."} 3 to 5 steps, given in the CORRECT order. Keep each under 10 words.
- "explanation" is one short sentence on why the answer is right.
- Only test what the lesson text or standard curriculum content for the topic covers. Be accurate.
Return ONLY a JSON object in this shape:
{"title":"game title","levels":[{"name":"...","lesson":"...","challenges":[]}]}"""

PACK_PROMPT = BASE + """

The student gives a chapter, lesson or topic (as text, an attached PDF, a lecture recording, the text of a web page or video link, or just its name). Make a complete study pack from it, all in one JSON object.
- "key_points": 8 to 10 important points, one short sentence each, most important first.
- "definitions": 3 to 6 key terms. "formulas": the main formulas or rules, or an empty list if there are none.
- "central" and "branches": a concept map with 4 to 7 branches, each with 2 to 5 short children.
- "cards": 10 to 12 flashcards. "front" is a short question or term, "back" is the answer in 1 or 2 short sentences. No two cards ask the same thing.
- "quiz": 8 multiple-choice questions. Each has exactly 4 options with exactly one correct, believable wrong options, "answer" as the number of the correct option (0 to 3, spread around), and a 1 or 2 sentence "explanation".
- "script": a friendly two-host podcast that explains the topic. "Aarav" explains and "Meera" asks the questions a student would ask. 12 to 14 short turns of 1 to 3 sentences, one everyday example, and a quick 3-point recap at the end. Write it as it would be spoken: say symbols in words ("x squared", "degrees").
- "levels": a learning game with exactly 4 levels (level 1 easy, each next level harder, level 4 a boss level mixing the whole chapter). Each level has "name" (under 6 words, can be playful), "lesson" (2 to 4 short sentences that teach exactly what its challenges test) and "challenges": 4 challenges, mixing these types:
  {"type":"choice","question":"...","options":["...","...","...","..."],"answer":0,"explanation":"..."} exactly 4 options, one correct, "answer" is 0 to 3.
  {"type":"truefalse","statement":"...","answer":true,"explanation":"..."} about half true, half believable false.
  {"type":"match","prompt":"Match each term to its meaning","pairs":[{"left":"...","right":"..."}],"explanation":"..."} exactly 4 pairs, each side under 6 words.
  {"type":"order","prompt":"Put the steps in the right order","steps":["first","second","third"],"explanation":"..."} 3 to 5 steps given in the CORRECT order, each under 10 words.
  "explanation" is one short sentence on why the answer is right.
Return ONLY a JSON object in this shape:
{"title":"chapter name","summary":"2-3 sentence overview","key_points":["..."],"definitions":[{"term":"...","meaning":"..."}],"formulas":["..."],"exam_tip":"one tip on how this topic is usually tested","central":"main idea in a few words","branches":[{"name":"main topic","children":["sub-point"]}],"cards":[{"front":"...","back":"..."}],"quiz":[{"question":"...","options":["...","...","...","..."],"answer":0,"explanation":"..."}],"script":[{"speaker":"Aarav","text":"..."}],"levels":[{"name":"...","lesson":"...","challenges":[]}]}"""

PLAN_PROMPT = BASE + """

The student has an exam coming. Make a realistic day-by-day study plan.
The message gives today's date, the exam date, how many days are left, and the topics or syllabus to cover.
- Use real calendar dates in the form YYYY-MM-DD. The first entry is today. The last entry is the exam day.
- If 28 days or fewer remain, make one entry per day. If more remain, make one entry per week (use the first day of that week) until 14 days before the exam, then one entry per day.
- Cover every topic. Put the hardest or biggest topics early. Mix new learning with revision.
- Include revision of everything in the last 3 days. The day before the exam is light revision and rest. The exam day is only a short checklist.
- Each day has a short "focus" (under 10 words), 2 to 4 specific "tasks" a student can tick off (for example: Read the chapter on motion, then solve 10 questions), and "minutes" (a realistic total, 30 to 180).
- If very few days remain, make the plan short and honest about what to prioritise.
- Give 4 short "exam_tips" for the exam day itself.
Return ONLY a JSON object in this shape:
{"title":"...","overview":"2 short sentences","days":[{"date":"YYYY-MM-DD","focus":"...","tasks":["..."],"minutes":90}],"exam_tips":["..."]}
"""

PROMPTS = {
    "plan": PLAN_PROMPT,
    "pack": PACK_PROMPT,
    "doubt": DOUBT_PROMPT,
    "map": MAP_PROMPT,
    "notes": NOTES_PROMPT,
    "podcast": PODCAST_PROMPT,
    "cards": CARDS_PROMPT,
    "quiz": QUIZ_PROMPT,
    "game": GAME_PROMPT,
}

async def gemini_upload(client: httpx.AsyncClient, data: bytes, mime: str = "application/pdf"):
    """Upload a big PDF or recording to Google (Files API). Returns (uri, name). Google deletes it after 48 hours; we delete it sooner."""
    start = await client.post(
        f"{GEMINI_UPLOAD_BASE}/files",
        headers={
            "x-goog-api-key": GEMINI_API_KEY,
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(len(data)),
            "X-Goog-Upload-Header-Content-Type": mime,
        },
        json={"file": {"display_name": "doubtly-study-material"}},
    )
    start.raise_for_status()
    done = await client.post(
        start.headers["x-goog-upload-url"],
        headers={"X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize"},
        content=data,
    )
    done.raise_for_status()
    info = done.json()["file"]
    for _ in range(30):  # wait until Google has finished reading the file
        state = info.get("state", "ACTIVE")
        if state == "ACTIVE":
            return info["uri"], info["name"]
        if state == "FAILED":
            raise HTTPException(400, "Google could not read that PDF. Try another file.")
        await asyncio.sleep(2)
        g = await client.get(f"{GEMINI_BASE}/{info['name']}", headers={"x-goog-api-key": GEMINI_API_KEY})
        g.raise_for_status()
        info = g.json()
    raise HTTPException(504, "The PDF took too long to process. Try a smaller file.")


# ---------------------------------------------------------------------------
# Accounts: sign up, log in, log out. Passwords are never stored, only a salted
# PBKDF2 hash. A login makes a random token; only its hash is kept in the database.
# ---------------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
SESSION_COOKIE = "doubtly_session"
_auth_hits: dict[str, deque] = defaultdict(deque)


DATABASE_URL = os.getenv("DATABASE_URL", "")  # a Postgres address keeps accounts safe on free hosts (optional)
if DATABASE_URL:
    import psycopg
    from psycopg.rows import dict_row
    DB_ERRORS = (sqlite3.IntegrityError, psycopg.errors.IntegrityError)
else:
    DB_ERRORS = (sqlite3.IntegrityError,)


class PgCon:
    """Makes a Postgres connection behave like the SQLite one the rest of the app was written for."""

    def __init__(self, url: str):
        self.c = psycopg.connect(url, row_factory=dict_row)

    def execute(self, sql: str, params=()):
        sql = sql.replace("?", "%s")
        m = re.match(r"\s*INSERT OR IGNORE INTO (.*)$", sql, re.S)
        if m:
            sql = "INSERT INTO " + m.group(1) + " ON CONFLICT DO NOTHING"
        sql = sql.replace("MAX(count - 1, 0)", "GREATEST(count - 1, 0)")
        return self.c.execute(sql, tuple(params))

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        self.c.rollback() if et else self.c.commit()
        return False

    def close(self):
        self.c.close()


def db():
    if DATABASE_URL:
        return PgCon(DATABASE_URL)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


SQLITE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        salt BLOB NOT NULL,
        pw_hash BLOB NOT NULL,
        created REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sessions (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        expires REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS reset_tokens (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        expires REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS usage (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        month TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, month)
    );
    CREATE TABLE IF NOT EXISTS guest_usage (
        ip_hash TEXT NOT NULL,
        month TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (ip_hash, month)
    );
    CREATE TABLE IF NOT EXISTS shares (
        id TEXT PRIMARY KEY,
        data TEXT NOT NULL,
        created REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS rooms (
        code TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        questions TEXT NOT NULL,
        created REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS room_players (
        id TEXT PRIMARY KEY,
        room TEXT NOT NULL,
        name TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        points INTEGER NOT NULL DEFAULT 0,
        last_correct REAL NOT NULL DEFAULT 0,
        seen REAL NOT NULL,
        joined REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS room_answers (
        room TEXT NOT NULL,
        player TEXT NOT NULL,
        qidx INTEGER NOT NULL,
        choice INTEGER NOT NULL,
        correct INTEGER NOT NULL,
        ts REAL NOT NULL,
        PRIMARY KEY (room, player, qidx)
    );
    CREATE TABLE IF NOT EXISTS room_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room TEXT NOT NULL,
        player TEXT NOT NULL,
        name TEXT NOT NULL,
        text TEXT NOT NULL,
        ts REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS chat_groups (
        code TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        created REAL NOT NULL,
        last_active REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS group_members (
        id TEXT PRIMARY KEY,
        grp TEXT NOT NULL,
        name TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 0,
        seen REAL NOT NULL,
        joined REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS group_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        grp TEXT NOT NULL,
        member TEXT NOT NULL,
        name TEXT NOT NULL,
        text TEXT NOT NULL,
        ts REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS lb_points (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        month TEXT NOT NULL,
        points INTEGER NOT NULL DEFAULT 0,
        updated REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, month)
    );
    CREATE TABLE IF NOT EXISTS lb_rewards (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        month TEXT NOT NULL,
        place INTEGER NOT NULL,
        bonus INTEGER NOT NULL,
        PRIMARY KEY (user_id, month)
    );
    CREATE TABLE IF NOT EXISTS lb_runs (
        month TEXT PRIMARY KEY
    );
    CREATE TABLE IF NOT EXISTS lb_prefs (
        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        hidden INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS friend_codes (
        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        code TEXT NOT NULL UNIQUE
    );
    CREATE TABLE IF NOT EXISTS friend_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_user INTEGER NOT NULL,
        to_user INTEGER NOT NULL,
        created REAL NOT NULL,
        UNIQUE (from_user, to_user)
    );
    CREATE TABLE IF NOT EXISTS friendships (
        user_a INTEGER NOT NULL,
        user_b INTEGER NOT NULL,
        created REAL NOT NULL,
        PRIMARY KEY (user_a, user_b)
    );
    CREATE TABLE IF NOT EXISTS payments (
        payment_id TEXT PRIMARY KEY,
        email TEXT NOT NULL,
        days INTEGER NOT NULL,
        created REAL NOT NULL,
        applied INTEGER NOT NULL DEFAULT 0
    );
"""
PG_SCHEMA = (SQLITE_SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
             .replace("BLOB", "BYTEA").replace(" REAL ", " DOUBLE PRECISION ")
             .replace("user_id INTEGER", "user_id BIGINT"))


def init_db() -> None:
    con = db()
    try:
        with con:
            if DATABASE_URL:
                for stmt in PG_SCHEMA.split(";"):
                    if stmt.strip():
                        con.execute(stmt)
                con.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS pro_until DOUBLE PRECISION NOT NULL DEFAULT 0")
            else:
                con.executescript(SQLITE_SCHEMA)
                cols = [r["name"] for r in con.execute("PRAGMA table_info(users)")]
                if "pro_until" not in cols:  # older databases get the new column
                    con.execute("ALTER TABLE users ADD COLUMN pro_until REAL NOT NULL DEFAULT 0")
    finally:
        con.close()


init_db()


def _hash_pw(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)


def _client_ip(request: Request) -> str:
    return request.headers.get("x-forwarded-for", request.client.host).split(",")[0].strip()


def auth_throttle(ip: str) -> None:
    """At most 10 sign-up or log-in tries per 10 minutes from one address."""
    now = time.time()
    hits = _auth_hits[ip]
    while hits and now - hits[0] > 600:
        hits.popleft()
    if len(hits) >= 10:
        raise HTTPException(429, "Too many tries. Please wait a few minutes and try again.")
    hits.append(now)


def current_user(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    th = hashlib.sha256(token.encode()).hexdigest()
    con = db()
    try:
        return con.execute(
            "SELECT u.id, u.name, u.email, u.pro_until FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires > ?", (th, time.time())).fetchone()
    finally:
        con.close()


def start_session(request: Request, user_id: int, body: dict) -> JSONResponse:
    token = secrets.token_urlsafe(32)
    con = db()
    try:
        with con:
            con.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            con.execute("INSERT INTO sessions (token_hash, user_id, expires) VALUES (?, ?, ?)",
                        (hashlib.sha256(token.encode()).hexdigest(), user_id, time.time() + SESSION_DAYS * 86400))
    finally:
        con.close()
    resp = JSONResponse({"user": body})
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=https, path="/")
    return resp


# ---------------------------------------------------------------------------
# Plans and monthly uses. One study pack, game, quiz or solved doubt is one use.
# A use is only counted when the AI really answered (failed tries are refunded).
# ---------------------------------------------------------------------------
def month_key() -> str:
    return time.strftime("%Y-%m")


def is_pro(u) -> bool:
    return bool(u) and (u["pro_until"] or 0) > time.time()


def require_pro(request: Request):
    """Group chat and friends are Pro features: the person must be logged in and have paid."""
    u = current_user(request)
    if not u:
        raise HTTPException(401, "Log in first. Group chat and Friends are for Doubtly Pro members.")
    if not is_pro(u):
        raise HTTPException(402, "Group chat and Friends are for Doubtly Pro members. Upgrade to use them.")
    return u


def prev_month(m: str) -> str:
    y, mo = int(m[:4]), int(m[5:7]) - 1
    if mo == 0:
        y, mo = y - 1, 12
    return f"{y}-{mo:02d}"


_awards_done = {"month": ""}


def ensure_awards() -> None:
    """Once a month: the top 4 people of last month's leaderboard get bonus free tries for this month."""
    cur = month_key()
    if _awards_done["month"] == cur:
        return
    con = db()
    try:
        with con:
            first = con.execute("INSERT OR IGNORE INTO lb_runs (month) VALUES (?)", (cur,)).rowcount == 1
            if first:
                rows = con.execute("SELECT user_id FROM lb_points WHERE month = ? AND points >= ? "
                                   "ORDER BY points DESC, updated ASC LIMIT ?", (prev_month(cur), LB_MIN_POINTS, len(LB_REWARDS))).fetchall()
                for i, r in enumerate(rows):
                    con.execute("INSERT OR IGNORE INTO lb_rewards (user_id, month, place, bonus) VALUES (?, ?, ?, ?)",
                                (r["user_id"], cur, i + 1, LB_REWARDS[i]))
    finally:
        con.close()
    _awards_done["month"] = cur


def bonus_for(uid: int) -> int:
    con = db()
    try:
        row = con.execute("SELECT bonus FROM lb_rewards WHERE user_id = ? AND month = ?", (uid, month_key())).fetchone()
    finally:
        con.close()
    return row["bonus"] if row else 0


def add_points(u, n: int) -> None:
    """Study points for the world leaderboard. Only logged-in people earn them, and a problem here never breaks an answer."""
    if not u or n <= 0:
        return
    try:
        con = db()
        try:
            with con:
                con.execute("INSERT OR IGNORE INTO lb_points (user_id, month, points, updated) VALUES (?, ?, 0, ?)", (u["id"], month_key(), time.time()))
                con.execute("UPDATE lb_points SET points = points + ?, updated = ? WHERE user_id = ? AND month = ?",
                            (n, time.time(), u["id"], month_key()))
        finally:
            con.close()
    except Exception as e:
        print("[Doubtly] could not save leaderboard points:", e, flush=True)


def _quota(u, ip: str):
    """(table, column, who, monthly limit)"""
    if u:
        ensure_awards()
        return "usage", "user_id", u["id"], (PRO_MONTHLY_LIMIT if is_pro(u) else FREE_MONTHLY_USES) + bonus_for(u["id"])
    return "guest_usage", "ip_hash", hashlib.sha256(ip.encode()).hexdigest(), GUEST_MONTHLY_USES


def usage_info(u, ip: str) -> dict:
    table, col, who, limit = _quota(u, ip)
    con = db()
    try:
        row = con.execute(f"SELECT count FROM {table} WHERE {col} = ? AND month = ?", (who, month_key())).fetchone()
    finally:
        con.close()
    used = row["count"] if row else 0
    return {"plan": "pro" if is_pro(u) else "free" if u else "guest", "used": used, "limit": limit, "left": max(limit - used, 0),
            "bonus": bonus_for(u["id"]) if u else 0}


def reserve_use(u, ip: str):
    table, col, who, limit = _quota(u, ip)
    con = db()
    try:
        with con:
            con.execute(f"INSERT OR IGNORE INTO {table} ({col}, month, count) VALUES (?, ?, 0)", (who, month_key()))
            cur = con.execute(f"UPDATE {table} SET count = count + 1 WHERE {col} = ? AND month = ? AND count < ?",
                              (who, month_key(), limit))
            ok = cur.rowcount == 1
    finally:
        con.close()
    if not ok:
        if not u:
            msg = (f"You have used your {limit} free tries this month. Sign up free to get {FREE_MONTHLY_USES} uses a month, "
                   f"or see Doubtly Pro.")
        elif is_pro(u):
            msg = f"You have reached the {limit} uses for this month. They come back on the 1st."
        else:
            msg = (f"You have used all {limit} free uses this month. They come back on the 1st. "
                   f"Or upgrade to Doubtly Pro (₹{PRICE_INR} or ${PRICE_USD} a month) for {PRO_MONTHLY_LIMIT} uses a month.")
        raise HTTPException(402, msg)
    return (table, col, who, month_key())


def refund_use(res) -> None:
    table, col, who, month = res
    con = db()
    try:
        with con:
            con.execute(f"UPDATE {table} SET count = MAX(count - 1, 0) WHERE {col} = ? AND month = ?", (who, month))
    finally:
        con.close()


class CheckoutIn(BaseModel):
    currency: str = "inr"


@app.post("/api/checkout")
def checkout(data: CheckoutIn, request: Request):
    u = current_user(request)
    if not u:
        raise HTTPException(401, "Log in first, then upgrade.")
    cur = "usd" if data.currency.strip().lower() == "usd" else "inr"
    if DEV_FREE_UPGRADE:  # testing only: gives 30 days of Pro with no payment
        con = db()
        try:
            with con:
                con.execute("UPDATE users SET pro_until = ? WHERE id = ?",
                            (max(time.time(), u["pro_until"] or 0) + 30 * 86400, u["id"]))
        finally:
            con.close()
        return {"pro": True}
    link = PAYMENT_LINK_USD if cur == "usd" else PAYMENT_LINK_INR
    if link.startswith("https://"):
        return {"url": link}
    raise HTTPException(501, "Payments are not switched on yet. Please check back soon.")


class SignupIn(BaseModel):
    name: str = ""
    email: str
    password: str


class LoginIn(BaseModel):
    email: str
    password: str


@app.post("/api/signup")
def signup(data: SignupIn, request: Request):
    auth_throttle(_client_ip(request))
    email = data.email.strip().lower()
    name = " ".join(data.name.split())[:40] or email.split("@")[0][:40]
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise HTTPException(400, "Enter a valid email address.")
    if len(data.password) < 8:
        raise HTTPException(400, "Use a password with at least 8 characters.")
    if len(data.password) > 200:
        raise HTTPException(400, "That password is too long (max 200 characters).")
    salt = secrets.token_bytes(16)
    con = db()
    try:
        with con:
            uid = con.execute("INSERT INTO users (email, name, salt, pw_hash, created) VALUES (?, ?, ?, ?, ?) RETURNING id",
                              (email, name, salt, _hash_pw(data.password, salt), time.time())).fetchone()["id"]
    except DB_ERRORS:
        raise HTTPException(409, "An account with that email already exists. Try logging in.")
    finally:
        con.close()
    return start_session(request, uid, {"name": name, "email": email})


@app.post("/api/login")
def login(data: LoginIn, request: Request):
    auth_throttle(_client_ip(request))
    email = data.email.strip().lower()[:254]
    con = db()
    try:
        row = con.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    finally:
        con.close()
    salt = row["salt"] if row else b"\0" * 16
    ok = hmac.compare_digest(_hash_pw(data.password[:200], salt), row["pw_hash"] if row else b"\0" * 32)
    if not row or not ok:
        raise HTTPException(401, "Wrong email or password.")
    return start_session(request, row["id"], {"name": row["name"], "email": row["email"]})


class ForgotIn(BaseModel):
    email: str


class ResetIn(BaseModel):
    token: str
    password: str


def send_reset_email(to: str, link: str) -> None:
    """Sends the reset link by email. With no email server set up, it prints the link in the black window instead."""
    body = ("Someone asked to reset the password for your Doubtly account.\n\n"
            f"Open this link within 1 hour to choose a new password:\n{link}\n\n"
            "If it was not you, ignore this email. Your password stays the same.")
    if BREVO_API_KEY and SMTP_FROM:
        try:
            r = httpx.post("https://api.brevo.com/v3/smtp/email", timeout=20,
                           headers={"api-key": BREVO_API_KEY, "accept": "application/json"},
                           json={"sender": {"email": SMTP_FROM, "name": "Doubtly AI"}, "to": [{"email": to}],
                                 "subject": "Reset your Doubtly password", "textContent": body})
            if r.status_code >= 300:
                print(f"[Doubtly] Brevo refused the reset email ({r.status_code}): {r.text[:200]}", flush=True)
        except httpx.HTTPError as e:
            print(f"[Doubtly] Could not send reset email: {e}", flush=True)
        return
    if not SMTP_HOST:
        print(f"\n[Doubtly] Password reset link for {to}:\n{link}\n(Set SMTP_HOST in .env to send this by email.)\n", flush=True)
        return
    msg = EmailMessage()
    msg["Subject"] = "Reset your Doubtly password"
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as srv:
            srv.starttls()
            if SMTP_USER:
                srv.login(SMTP_USER, SMTP_PASSWORD)
            srv.send_message(msg)
    except Exception as e:  # never show mail errors to the person asking
        print(f"[Doubtly] Could not send reset email: {e}", flush=True)


@app.post("/api/forgot")
def forgot(data: ForgotIn, request: Request):
    auth_throttle(_client_ip(request))
    email = data.email.strip().lower()[:254]
    con = db()
    try:
        row = con.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if row:
            token = secrets.token_urlsafe(32)
            with con:
                con.execute("DELETE FROM reset_tokens WHERE expires < ? OR user_id = ?", (time.time(), row["id"]))
                con.execute("INSERT INTO reset_tokens (token_hash, user_id, expires) VALUES (?, ?, ?)",
                            (hashlib.sha256(token.encode()).hexdigest(), row["id"], time.time() + 3600))
            base = PUBLIC_URL or str(request.base_url).rstrip("/")
            send_reset_email(email, f"{base}/login#reset={token}")
    finally:
        con.close()
    # The same answer whether or not the email has an account, so nobody can check who is registered.
    return {"ok": True}


@app.post("/api/reset")
def reset_password(data: ResetIn, request: Request):
    auth_throttle(_client_ip(request))
    if len(data.password) < 8:
        raise HTTPException(400, "Use a password with at least 8 characters.")
    if len(data.password) > 200:
        raise HTTPException(400, "That password is too long (max 200 characters).")
    th = hashlib.sha256(data.token.encode()).hexdigest()
    con = db()
    try:
        with con:
            row = con.execute("DELETE FROM reset_tokens WHERE token_hash = ? AND expires > ? RETURNING user_id",
                              (th, time.time())).fetchone()
            if not row:
                raise HTTPException(400, "This reset link is not valid any more. Ask for a new one.")
            uid = row["user_id"]
            salt = secrets.token_bytes(16)
            con.execute("UPDATE users SET salt = ?, pw_hash = ? WHERE id = ?", (salt, _hash_pw(data.password, salt), uid))
            con.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))  # log out every old device
        u = con.execute("SELECT name, email FROM users WHERE id = ?", (uid,)).fetchone()
    finally:
        con.close()
    return start_session(request, uid, {"name": u["name"], "email": u["email"]})


@app.post("/api/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        con = db()
        try:
            with con:
                con.execute("DELETE FROM sessions WHERE token_hash = ?", (hashlib.sha256(token.encode()).hexdigest(),))
        finally:
            con.close()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/api/me")
def me(request: Request):
    u = current_user(request)
    return {
        "user": {"name": u["name"], "email": u["email"]} if u else None,
        "usage": usage_info(u, _client_ip(request)),
        "prices": {"inr": PRICE_INR, "usd": PRICE_USD, "free_uses": FREE_MONTHLY_USES, "pro_uses": PRO_MONTHLY_LIMIT},
    }


# ---------------------------------------------------------------------------
# Reading links safely. The server opens the link for the student, so it must
# never be tricked into opening addresses on your own network or computer.
# ---------------------------------------------------------------------------


def audio_mime(f: UploadFile) -> str:
    ext = (f.filename or "").rsplit(".", 1)[-1].lower()
    ctype = (f.content_type or "").split(";")[0].strip().lower()
    if ext in VIDEO_TYPES and (ctype.startswith("video/") or ext not in AUDIO_TYPES):
        return VIDEO_TYPES[ext]  # a real video file (for example MP4 or MOV)
    if ext in AUDIO_TYPES:
        return AUDIO_TYPES[ext]
    if ctype.startswith("audio/") or ctype.startswith("video/"):
        return ctype
    raise HTTPException(400, "Upload an audio or video file (MP3, WAV, M4A, MP4, MOV, WebM and more).")


def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False
    return True


class _PageText(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form", "iframe", "template"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "blockquote", "pre"}

    def __init__(self):
        super().__init__()
        self.skip = 0
        self.out: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "title":
            self._in_title = True
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == "title":
            self._in_title = False
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip and data.strip():
            self.out.append(data.strip() + " ")


def page_text(html: str) -> str:
    p = _PageText()
    p.feed(html)
    text = re.sub(r"\n\s*\n+", "\n\n", "".join(p.out)).strip()
    if p.title.strip():
        text = p.title.strip() + "\n\n" + text
    return text[:MAX_PAGE_CHARS]


async def fetch_link(url: str):
    """Open a link. Returns ("pdf", bytes) or ("text", str). Raises HTTPException with a friendly message."""
    cur = url
    headers = {"User-Agent": "Mozilla/5.0 (compatible; DoubtlyStudyHelper/1.0)"}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, headers=headers) as client:
            for _ in range(4):  # follow a few redirects, checking every hop
                u = urlparse(cur)
                if u.scheme not in ("http", "https") or not u.hostname:
                    raise HTTPException(400, "Enter a full link that starts with http:// or https://")
                if u.port not in (None, 80, 443):
                    raise HTTPException(400, "That link can't be opened.")
                if not await asyncio.to_thread(_public_host, u.hostname):
                    raise HTTPException(400, "That link can't be opened.")
                async with client.stream("GET", cur) as r:
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        cur = urljoin(cur, r.headers["location"])
                        continue
                    if r.status_code >= 400:
                        raise HTTPException(400, f"The site refused the request ({r.status_code}). Try another link, or paste the text.")
                    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    limit = MAX_PDF_BYTES if ctype == "application/pdf" else MAX_PAGE_BYTES
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > limit:
                            raise HTTPException(400, "That page is too large. Paste just the part you need.")
                    if ctype == "application/pdf":
                        return "pdf", bytes(buf)
                    if ctype in ("text/html", "application/xhtml+xml", "text/plain", ""):
                        body = bytes(buf).decode(r.encoding or "utf-8", errors="replace")
                        text = body[:MAX_PAGE_CHARS] if ctype == "text/plain" else page_text(body)
                        if len(text) < 200:
                            raise HTTPException(400, "Could not read much text from that page (it may need a login or JavaScript). Paste the text instead.")
                        return "text", text
                    raise HTTPException(400, "That link is not a web page or a PDF.")
    except httpx.HTTPError:
        raise HTTPException(400, "Could not open that link. Check it and try again.")
    raise HTTPException(400, "That link redirects too many times.")


# ---------------------------------------------------------------------------


@app.post("/api/solve")
async def solve(
    request: Request,
    mode: str = Form("pack"),
    question: str = Form(""),
    subject: str = Form("Any subject"),
    level: str = Form(""),
    language: str = Form("Same as my question"),
    url: str = Form(""),
    pdf: UploadFile | None = File(None),
    audio: UploadFile | None = File(None),
    image: UploadFile | None = File(None),
    exam_date: str = Form(""),
    today: str = Form(""),
):
    if not GEMINI_API_KEY:
        raise HTTPException(500, "Server missing GEMINI_API_KEY. See README.")
    if mode not in PROMPTS:
        mode = "pack"

    question = question.strip()[:4000 if mode == "doubt" else 12000]
    url = url.strip()[:500]
    if url and not re.match(r"^https?://", url, re.I):
        url = "https://" + url

    plan_line = ""
    if mode == "plan":
        try:
            day = datetime.date.fromisoformat(today.strip()) if today.strip() else datetime.date.today()
            if abs((day - datetime.date.today()).days) > 2:  # trust the student's clock only if it is close to ours
                day = datetime.date.today()
            exam = datetime.date.fromisoformat(exam_date.strip())
        except ValueError:
            raise HTTPException(400, "Choose your exam date.")
        left = (exam - day).days
        if left < 0 or left > 400:
            raise HTTPException(400, "Pick an exam date that is today or later, within the next year.")
        plan_line = f"Today's date: {day.isoformat()}\nExam date: {exam.isoformat()} ({left} days left)\n"

    files: list[tuple[bytes, str]] = []  # (data, mime type)
    has_audio = False
    if pdf is not None and pdf.filename:
        if pdf.content_type != "application/pdf" and not pdf.filename.lower().endswith(".pdf"):
            raise HTTPException(400, "Upload a PDF file.")
        data = await pdf.read()
        if len(data) > MAX_PDF_BYTES:
            raise HTTPException(400, "PDF too large (max 80 MB).")
        if not data.startswith(b"%PDF"):
            raise HTTPException(400, "That file is not a valid PDF.")
        files.append((data, "application/pdf"))
    if image is not None and image.filename:
        data = await image.read()
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(400, "That photo is too big (max 8 MB). Try a smaller one.")
        if data.startswith(b"\x89PNG"):
            imime = "image/png"
        elif data.startswith(b"\xff\xd8\xff"):
            imime = "image/jpeg"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            imime = "image/webp"
        else:
            raise HTTPException(400, "Upload a PNG, JPG or WEBP photo.")
        files.append((data, imime))
    if audio is not None and audio.filename:
        mime = audio_mime(audio)
        data = await audio.read()
        if len(data) > MAX_PDF_BYTES:
            raise HTTPException(400, "Recording or video too large (max 80 MB). For a long video, paste its YouTube link instead.")
        if len(data) < 2000:
            raise HTTPException(400, "That recording is empty. Try recording again.")
        files.append((data, mime))
        has_audio = True
    if not question and not files and not url:
        raise HTTPException(400, "Type a question or chapter, add a PDF, photo or recording, or paste a link.")

    user = current_user(request)
    if user:
        check_limits(f"user:{user['id']}", PER_USER_LIMIT, guest=False)
    else:
        check_limits(_client_ip(request), PER_IP_LIMIT)

    parts = []
    notes = []
    if url:
        if (urlparse(url).hostname or "").lower() in YOUTUBE_HOSTS:
            parts.append({"file_data": {"file_uri": url}})
            notes.append("The student's material is the attached YouTube video.")
        else:
            kind, content = await fetch_link(url)
            if kind == "pdf":
                files.append((content, "application/pdf"))
            else:
                notes.append(f"Material from the web page {url}:\n{content}")
    big_files = [(d, m) for d, m in files if len(d) > INLINE_PDF_BYTES]
    for data, mime in files:
        if len(data) <= INLINE_PDF_BYTES:
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}})
    if any(m == "application/pdf" for _, m in files):
        notes.append("The student's material is in the attached PDF.")
    if any(m.startswith("image/") for _, m in files):
        notes.append("The student's material is in the attached photo (a textbook page, notes or a question). Read all the text and diagrams in it carefully.")
    if has_audio:
        notes.append("The student's material is the attached lecture recording or video. Listen to (and watch) all of it and base the answer on what is taught in it.")

    if language == "Same as my question":
        language = "Same as my question (reply in the language the question is written in)"
    lvl = level.strip()[:80] or "not specified"
    label = "Student's doubt" if mode == "doubt" else "Topics and syllabus to cover" if mode == "plan" else "Chapter or topic"
    source = ("\n\n".join(notes) + "\n\n") if notes else ""
    parts.append({"text": (
        f"Subject: {subject[:40]}\n"
        f"Level or curriculum: {lvl}\n"
        f"{plan_line}"
        f"Reply language: {language[:40]}\n\n"
        f"{source}{label}:\n{question or '(see the material above)'}"
    )})

    api = f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent"
    uploaded: list[str] = []
    reserved = reserve_use(user, _client_ip(request))
    succeeded = False
    try:
        async with httpx.AsyncClient(timeout=600) as client:
            r = await client.post(
                api,
                json={
                    "contents": [{"role": "user", "parts": parts}],
                    "generationConfig": {"responseMimeType": "application/json"},
                },
                headers={"Content-Type": "application/json"},
            )
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        result = json.loads(text)
        succeeded = True
        add_points(user, LB_SOLVE_POINTS)
        return result
    except httpx.HTTPStatusError as e:
        if has_audio and e.response.status_code == 400:
            raise HTTPException(502, "The AI could not read that file. Try an MP3, WAV or MP4 file instead.")
        raise HTTPException(502, f"AI service error ({e.response.status_code}). Try again.")
    except (KeyError, IndexError, json.JSONDecodeError):
        raise HTTPException(502, "Could not read the AI response. Please retry.")
    except httpx.HTTPError:
        raise HTTPException(504, "AI service timed out. Please retry.")
    finally:
        if not succeeded:
            refund_use(reserved)

class ChatMsg(BaseModel):
    role: str
    content: str


class ChatIn(BaseModel):
    messages: list[ChatMsg]
    context: str = ""
    language: str = "Same as my question"
    level: str = ""


class ShareIn(BaseModel):
    pack: dict


_share_hits: dict[str, deque] = defaultdict(deque)


@app.post("/api/share")
def share_pack(data: ShareIn, request: Request):
    ip = _client_ip(request)
    now = time.time()
    hits = _share_hits[ip]
    while hits and now - hits[0] > 3600:
        hits.popleft()
    if len(hits) >= 20:
        raise HTTPException(429, "You shared a lot of packs. Please try again in a while.")
    pack = {k: v for k, v in data.pack.items() if k in SHARE_KEYS}
    blob = json.dumps(pack, ensure_ascii=False)
    if len(blob) > MAX_SHARE_CHARS:
        raise HTTPException(413, "This study pack is too big to share.")
    if not pack.get("title") or not any(pack.get(k) for k in ("key_points", "cards", "quiz", "script", "levels", "branches")):
        raise HTTPException(400, "There is nothing to share yet. Make a study pack first.")
    hits.append(now)
    sid = secrets.token_urlsafe(7)
    con = db()
    try:
        with con:
            con.execute("DELETE FROM shares WHERE created < ?", (now - SHARE_DAYS * 86400,))
            con.execute("INSERT INTO shares (id, data, created) VALUES (?, ?, ?)", (sid, blob, now))
    finally:
        con.close()
    return {"id": sid, "days": SHARE_DAYS}


@app.get("/api/shared/{sid}")
def get_shared(sid: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,20}", sid):
        raise HTTPException(404, "This link is not valid.")
    con = db()
    try:
        row = con.execute("SELECT data, created FROM shares WHERE id = ?", (sid,)).fetchone()
    finally:
        con.close()
    if not row or row["created"] < time.time() - SHARE_DAYS * 86400:
        raise HTTPException(404, "This shared study pack was not found. It may have expired.")
    return json.loads(row["data"])


# ---------------------------------------------------------------------------
# Big chat study: a room that many people join from their own devices. Everyone
# answers the same quiz, earns points and sees a live leaderboard and chat.
# The correct answers stay on the server until a player has answered.
# ---------------------------------------------------------------------------
ROOM_HOURS = 24
ROOM_MAX_PLAYERS = 50
ROOM_POINTS, ROOM_FIRST_BONUS = 10, 5
ROOM_CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_room_create_hits: dict[str, deque] = defaultdict(deque)
_chat_last: dict[str, deque] = defaultdict(deque)
_room_join_hits: dict[str, deque] = defaultdict(deque)


class RoomCreateIn(BaseModel):
    name: str
    title: str = ""
    questions: list[dict]


class RoomJoinIn(BaseModel):
    name: str


class RoomAnswerIn(BaseModel):
    pid: str
    qidx: int
    choice: int


class RoomChatIn(BaseModel):
    pid: str
    text: str


def _clean_name(name: str) -> str:
    n = " ".join(name.split())[:20]
    if not n:
        raise HTTPException(400, "Type a name so your friends know who you are.")
    return n


def _clean_questions(raw: list[dict]) -> list[dict]:
    out = []
    for q in raw[:30]:
        try:
            text = str(q.get("question", "")).strip()[:500]
            opts = [str(o).strip()[:200] for o in q.get("options", [])][:6]
            ans = int(q.get("answer"))
            exp = str(q.get("explanation", "")).strip()[:500]
        except (TypeError, ValueError, AttributeError):
            continue
        if text and len(opts) >= 2 and all(opts) and 0 <= ans < len(opts):
            out.append({"question": text, "options": opts, "answer": ans, "explanation": exp})
    if not out:
        raise HTTPException(400, "This study pack has no quiz questions. Make a study pack with a quiz first.")
    return out


def _room_code() -> str:
    return "".join(secrets.choice(ROOM_CODE_CHARS) for _ in range(6))


def _load_room(con, code: str):
    code = code.strip().upper()
    row = con.execute("SELECT code, title, questions, created FROM rooms WHERE code = ?", (code,)).fetchone()
    if not row or row["created"] < time.time() - ROOM_HOURS * 3600:
        raise HTTPException(404, "That room was not found. Check the code, or ask your friend for a new one.")
    return row


def _auth_player(con, code: str, pid: str, token: str):
    row = con.execute("SELECT * FROM room_players WHERE id = ? AND room = ?", (pid, code)).fetchone()
    ok = row and hmac.compare_digest(row["token_hash"], hashlib.sha256((token or "").encode()).hexdigest())
    if not ok:
        raise HTTPException(403, "You are not in this room. Join it again.")
    return row


def _add_player(con, code: str, name: str) -> dict:
    n = con.execute("SELECT COUNT(*) AS n FROM room_players WHERE room = ?", (code,)).fetchone()["n"]
    if n >= ROOM_MAX_PLAYERS:
        raise HTTPException(409, f"This room is full ({ROOM_MAX_PLAYERS} people).")
    taken = con.execute("SELECT 1 AS x FROM room_players WHERE room = ? AND LOWER(name) = ?", (code, name.lower())).fetchone()
    if taken:
        raise HTTPException(409, "Someone in this room already uses that name. Pick another.")
    pid, token, now = secrets.token_urlsafe(6), secrets.token_urlsafe(24), time.time()
    con.execute("INSERT INTO room_players (id, room, name, token_hash, seen, joined) VALUES (?, ?, ?, ?, ?, ?)",
                (pid, code, name, hashlib.sha256(token.encode()).hexdigest(), now, now))
    return {"pid": pid, "token": token, "name": name}


@app.post("/api/room")
def room_create(data: RoomCreateIn, request: Request):
    ip, now = _client_ip(request), time.time()
    hits = _room_create_hits[ip]
    while hits and now - hits[0] > 3600:
        hits.popleft()
    if len(hits) >= 10:
        raise HTTPException(429, "You made a lot of rooms. Please try again in a while.")
    name = _clean_name(data.name)
    qs = _clean_questions(data.questions)
    title = " ".join(data.title.split())[:80] or "Study room"
    con = db()
    try:
        with con:
            old = now - ROOM_HOURS * 3600
            for t, col in (("room_answers", "room"), ("room_messages", "room"), ("room_players", "room")):
                con.execute(f"DELETE FROM {t} WHERE {col} IN (SELECT code FROM rooms WHERE created < ?)", (old,))
            con.execute("DELETE FROM rooms WHERE created < ?", (old,))
            for _ in range(8):
                code = _room_code()
                if not con.execute("SELECT 1 AS x FROM rooms WHERE code = ?", (code,)).fetchone():
                    break
            con.execute("INSERT INTO rooms (code, title, questions, created) VALUES (?, ?, ?, ?)",
                        (code, title, json.dumps(qs, ensure_ascii=False), now))
            me = _add_player(con, code, name)
    finally:
        con.close()
    hits.append(now)
    return {"code": code, **me}


@app.get("/api/room/{code}/info")
def room_info(code: str):
    con = db()
    try:
        r = _load_room(con, code)
        n = con.execute("SELECT COUNT(*) AS n FROM room_players WHERE room = ?", (r["code"],)).fetchone()["n"]
    finally:
        con.close()
    return {"code": r["code"], "title": r["title"], "players": n, "questions": len(json.loads(r["questions"]))}


@app.post("/api/room/{code}/join")
def room_join(code: str, data: RoomJoinIn, request: Request):
    ip, now = _client_ip(request), time.time()  # a whole class can share one school address, so this limit is generous
    hits = _room_join_hits[ip]
    while hits and now - hits[0] > 600:
        hits.popleft()
    if len(hits) >= 120:
        raise HTTPException(429, "Too many people joined from this network. Please wait a few minutes.")
    hits.append(now)
    name = _clean_name(data.name)
    con = db()
    try:
        with con:
            r = _load_room(con, code)
            me = _add_player(con, r["code"], name)
    finally:
        con.close()
    return {"code": r["code"], **me}


@app.get("/api/room/{code}")
def room_state(code: str, pid: str, request: Request, after: int = 0, full: int = 0):
    token = request.headers.get("x-token", "")
    con = db()
    try:
        with con:
            r = _load_room(con, code)
            code = r["code"]
            me = _auth_player(con, code, pid, token)
            con.execute("UPDATE room_players SET seen = ? WHERE id = ?", (time.time(), pid))
        qs = json.loads(r["questions"])
        players = con.execute("SELECT id, name, points, seen FROM room_players WHERE room = ? "
                              "ORDER BY points DESC, last_correct ASC, joined ASC", (code,)).fetchall()
        counts = {p["player"]: p["n"] for p in con.execute(
            "SELECT player, COUNT(*) AS n FROM room_answers WHERE room = ? GROUP BY player", (code,)).fetchall()}
        mine = con.execute("SELECT qidx, choice, correct FROM room_answers WHERE room = ? AND player = ? ORDER BY qidx",
                           (code, pid)).fetchall()
        if after > 0:
            msgs = con.execute("SELECT id, name, text, ts FROM room_messages WHERE room = ? AND id > ? ORDER BY id LIMIT 100",
                               (code, after)).fetchall()
        else:
            msgs = list(reversed(con.execute("SELECT id, name, text, ts FROM room_messages WHERE room = ? "
                                             "ORDER BY id DESC LIMIT 50", (code,)).fetchall()))
    finally:
        con.close()
    now = time.time()
    out = {
        "code": code, "title": r["title"], "total": len(qs), "me": pid,
        "players": [{"id": p["id"], "name": p["name"], "points": p["points"], "done": counts.get(p["id"], 0),
                     "online": now - p["seen"] < 12} for p in players],
        "mine": [{"q": a["qidx"], "choice": a["choice"], "correct": bool(a["correct"]),
                  "answer": qs[a["qidx"]]["answer"], "explanation": qs[a["qidx"]]["explanation"]} for a in mine
                 if a["qidx"] < len(qs)],
        "messages": [{"id": m["id"], "name": m["name"], "text": m["text"], "ts": m["ts"]} for m in msgs],
    }
    if full:  # the questions, without the right answers
        out["questions"] = [{"question": q["question"], "options": q["options"]} for q in qs]
    return out


@app.post("/api/room/{code}/answer")
def room_answer(code: str, data: RoomAnswerIn, request: Request):
    token = request.headers.get("x-token", "")
    con = db()
    try:
        with con:
            r = _load_room(con, code)
            code = r["code"]
            _auth_player(con, code, data.pid, token)
            qs = json.loads(r["questions"])
            if not 0 <= data.qidx < len(qs):
                raise HTTPException(400, "That question does not exist.")
            q = qs[data.qidx]
            if not 0 <= data.choice < len(q["options"]):
                raise HTTPException(400, "That option does not exist.")
            if con.execute("SELECT 1 AS x FROM room_answers WHERE room = ? AND player = ? AND qidx = ?",
                           (code, data.pid, data.qidx)).fetchone():
                raise HTTPException(409, "You already answered this question.")
            correct = data.choice == q["answer"]
            now, gained, first = time.time(), 0, False
            if correct:
                first = con.execute("SELECT COUNT(*) AS n FROM room_answers WHERE room = ? AND qidx = ? AND correct = 1",
                                    (code, data.qidx)).fetchone()["n"] == 0
                gained = ROOM_POINTS + (ROOM_FIRST_BONUS if first else 0)
            con.execute("INSERT INTO room_answers (room, player, qidx, choice, correct, ts) VALUES (?, ?, ?, ?, ?, ?)",
                        (code, data.pid, data.qidx, data.choice, 1 if correct else 0, now))
            if correct:
                con.execute("UPDATE room_players SET points = points + ?, last_correct = ? WHERE id = ?", (gained, now, data.pid))
            total = con.execute("SELECT points FROM room_players WHERE id = ?", (data.pid,)).fetchone()["points"]
    finally:
        con.close()
    return {"correct": correct, "answer": q["answer"], "explanation": q["explanation"], "gained": gained, "first": first, "total": total}


@app.post("/api/room/{code}/chat")
def room_chat(code: str, data: RoomChatIn, request: Request):
    token = request.headers.get("x-token", "")
    text = " ".join(data.text.split())[:300]
    if not text:
        raise HTTPException(400, "Type a message first.")
    now = time.time()
    h = _chat_last[data.pid]
    while h and now - h[0] > 10:
        h.popleft()
    if len(h) >= 5:
        raise HTTPException(429, "Slow down a little. Try again in a few seconds.")
    con = db()
    try:
        with con:
            r = _load_room(con, code)
            code = r["code"]
            me = _auth_player(con, code, data.pid, token)
            con.execute("INSERT INTO room_messages (room, player, name, text, ts) VALUES (?, ?, ?, ?, ?)",
                        (code, data.pid, me["name"], text, now))
    finally:
        con.close()
    h.append(now)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Group chat: a group stays open (it is only removed after 90 days with no
# messages). Anyone with the code can join. Messages are plain text only.
# ---------------------------------------------------------------------------
GROUP_MAX_MEMBERS = 100
GROUP_KEEP_MESSAGES = 500
GROUP_IDLE_DAYS = 90
_group_create_hits: dict[str, deque] = defaultdict(deque)
_group_join_hits: dict[str, deque] = defaultdict(deque)
_group_chat_last: dict[str, deque] = defaultdict(deque)


class GroupCreateIn(BaseModel):
    name: str
    title: str = ""


class GroupJoinIn(BaseModel):
    name: str


class GroupChatIn(BaseModel):
    pid: str
    text: str


class GroupLeaveIn(BaseModel):
    pid: str


def _load_group(con, code: str):
    code = code.strip().upper()
    row = con.execute("SELECT code, title, created, last_active FROM chat_groups WHERE code = ?", (code,)).fetchone()
    if not row or row["last_active"] < time.time() - GROUP_IDLE_DAYS * 86400:
        raise HTTPException(404, "That group was not found. Check the code, or ask your friend for it again.")
    return row


def _auth_member(con, code: str, pid: str, token: str):
    row = con.execute("SELECT * FROM group_members WHERE id = ? AND grp = ?", (pid, code)).fetchone()
    ok = row and hmac.compare_digest(row["token_hash"], hashlib.sha256((token or "").encode()).hexdigest())
    if not ok:
        raise HTTPException(403, "You are not in this group. Join it again with the code.")
    return row


def _add_member(con, code: str, name: str, admin: bool = False) -> dict:
    n = con.execute("SELECT COUNT(*) AS n FROM group_members WHERE grp = ?", (code,)).fetchone()["n"]
    if n >= GROUP_MAX_MEMBERS:
        raise HTTPException(409, f"This group is full ({GROUP_MAX_MEMBERS} people).")
    if con.execute("SELECT 1 AS x FROM group_members WHERE grp = ? AND LOWER(name) = ?", (code, name.lower())).fetchone():
        raise HTTPException(409, "Someone in this group already uses that name. Pick another.")
    pid, token, now = secrets.token_urlsafe(6), secrets.token_urlsafe(24), time.time()
    con.execute("INSERT INTO group_members (id, grp, name, token_hash, is_admin, seen, joined) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (pid, code, name, hashlib.sha256(token.encode()).hexdigest(), 1 if admin else 0, now, now))
    return {"pid": pid, "token": token, "name": name}


def _group_hit(bucket: dict, key: str, limit: int, window: int, msg: str) -> None:
    now = time.time()
    hits = bucket[key]
    while hits and now - hits[0] > window:
        hits.popleft()
    if len(hits) >= limit:
        raise HTTPException(429, msg)
    hits.append(now)


@app.post("/api/group")
def group_create(data: GroupCreateIn, request: Request):
    require_pro(request)
    _group_hit(_group_create_hits, _client_ip(request), 10, 3600, "You made a lot of groups. Please try again in a while.")
    name = _clean_name(data.name)
    title = " ".join(data.title.split())[:50] or "Study group"
    con = db()
    try:
        with con:
            old = time.time() - GROUP_IDLE_DAYS * 86400
            for t, col in (("group_messages", "grp"), ("group_members", "grp")):
                con.execute(f"DELETE FROM {t} WHERE {col} IN (SELECT code FROM chat_groups WHERE last_active < ?)", (old,))
            con.execute("DELETE FROM chat_groups WHERE last_active < ?", (old,))
            for _ in range(20):
                code = _room_code()
                if not con.execute("SELECT 1 AS x FROM chat_groups WHERE code = ?", (code,)).fetchone():
                    break
            else:
                raise HTTPException(503, "Could not make a code. Please try again.")
            now = time.time()
            con.execute("INSERT INTO chat_groups (code, title, created, last_active) VALUES (?, ?, ?, ?)", (code, title, now, now))
            me = _add_member(con, code, name, admin=True)
    finally:
        con.close()
    return {"code": code, "title": title, **me}


@app.get("/api/group/{code}/info")
def group_info(code: str):
    con = db()
    try:
        g = _load_group(con, code)
        n = con.execute("SELECT COUNT(*) AS n FROM group_members WHERE grp = ?", (g["code"],)).fetchone()["n"]
    finally:
        con.close()
    return {"code": g["code"], "title": g["title"], "members": n}


@app.post("/api/group/{code}/join")
def group_join(code: str, data: GroupJoinIn, request: Request):
    require_pro(request)
    _group_hit(_group_join_hits, _client_ip(request), 120, 600, "Too many people joined from this network. Please wait a few minutes.")
    name = _clean_name(data.name)
    con = db()
    try:
        with con:
            g = _load_group(con, code)
            me = _add_member(con, g["code"], name)
    finally:
        con.close()
    return {"code": g["code"], "title": g["title"], **me}


@app.get("/api/group/{code}")
def group_state(code: str, pid: str, request: Request, after: int = 0):
    require_pro(request)
    token = request.headers.get("x-token", "")
    con = db()
    try:
        with con:
            g = _load_group(con, code)
            code = g["code"]
            _auth_member(con, code, pid, token)
            con.execute("UPDATE group_members SET seen = ? WHERE id = ?", (time.time(), pid))
        members = con.execute("SELECT id, name, is_admin, seen FROM group_members WHERE grp = ? ORDER BY joined", (code,)).fetchall()
        if after > 0:
            msgs = con.execute("SELECT id, member, name, text, ts FROM group_messages WHERE grp = ? AND id > ? ORDER BY id LIMIT 100",
                               (code, after)).fetchall()
        else:
            msgs = list(reversed(con.execute("SELECT id, member, name, text, ts FROM group_messages WHERE grp = ? "
                                             "ORDER BY id DESC LIMIT 60", (code,)).fetchall()))
    finally:
        con.close()
    now = time.time()
    return {
        "code": code, "title": g["title"], "me": pid,
        "members": [{"id": m["id"], "name": m["name"], "admin": bool(m["is_admin"]), "online": now - m["seen"] < 12} for m in members],
        "messages": [{"id": m["id"], "mine": m["member"] == pid, "name": m["name"], "text": m["text"], "ts": m["ts"]} for m in msgs],
    }


@app.post("/api/group/{code}/chat")
def group_chat(code: str, data: GroupChatIn, request: Request):
    require_pro(request)
    token = request.headers.get("x-token", "")
    text = " ".join(data.text.split())[:500]
    if not text:
        raise HTTPException(400, "Type a message first.")
    _group_hit(_group_chat_last, data.pid, 5, 10, "Slow down a little. Try again in a few seconds.")
    con = db()
    try:
        with con:
            g = _load_group(con, code)
            code = g["code"]
            me = _auth_member(con, code, data.pid, token)
            now = time.time()
            con.execute("INSERT INTO group_messages (grp, member, name, text, ts) VALUES (?, ?, ?, ?, ?)",
                        (code, data.pid, me["name"], text, now))
            con.execute("UPDATE chat_groups SET last_active = ? WHERE code = ?", (now, code))
            keep = con.execute("SELECT id FROM group_messages WHERE grp = ? ORDER BY id DESC LIMIT 1 OFFSET ?",
                               (code, GROUP_KEEP_MESSAGES)).fetchone()
            if keep:
                con.execute("DELETE FROM group_messages WHERE grp = ? AND id <= ?", (code, keep["id"]))
    finally:
        con.close()
    return {"ok": True}


@app.post("/api/group/{code}/leave")
def group_leave(code: str, data: GroupLeaveIn, request: Request):
    require_pro(request)
    token = request.headers.get("x-token", "")
    con = db()
    try:
        with con:
            g = _load_group(con, code)
            _auth_member(con, g["code"], data.pid, token)
            con.execute("DELETE FROM group_members WHERE id = ?", (data.pid,))
    finally:
        con.close()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Friends: every Pro member has a friend code (not an IP address - those are private
# and change). Share your code, someone sends a request with it, and you accept.
# ---------------------------------------------------------------------------
FRIEND_REQ_PER_HOUR = 20
_friend_hits: dict[str, deque] = defaultdict(deque)


class FriendReqIn(BaseModel):
    code: str


class FriendRemoveIn(BaseModel):
    user_id: int


def _show_code(c: str) -> str:
    return f"{c[:4]}-{c[4:]}"


def _my_friend_code(con, uid: int) -> str:
    row = con.execute("SELECT code FROM friend_codes WHERE user_id = ?", (uid,)).fetchone()
    if row:
        return row["code"]
    for _ in range(30):
        code = "".join(secrets.choice(ROOM_CODE_CHARS) for _ in range(8))
        con.execute("INSERT OR IGNORE INTO friend_codes (user_id, code) VALUES (?, ?)", (uid, code))
        row = con.execute("SELECT code FROM friend_codes WHERE user_id = ?", (uid,)).fetchone()
        if row:
            return row["code"]
    raise HTTPException(503, "Could not make your friend code. Please try again.")


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


@app.get("/api/friends")
def friends_list(request: Request):
    u = require_pro(request)
    con = db()
    try:
        with con:
            code = _my_friend_code(con, u["id"])
        fr = con.execute(
            "SELECT u.id, u.name FROM friendships f JOIN users u ON u.id = CASE WHEN f.user_a = ? THEN f.user_b ELSE f.user_a END "
            "WHERE f.user_a = ? OR f.user_b = ? ORDER BY f.created DESC", (u["id"], u["id"], u["id"])).fetchall()
        inc = con.execute("SELECT r.id, u.name FROM friend_requests r JOIN users u ON u.id = r.from_user "
                          "WHERE r.to_user = ? ORDER BY r.created DESC LIMIT 50", (u["id"],)).fetchall()
        out = con.execute("SELECT r.id, u.name FROM friend_requests r JOIN users u ON u.id = r.to_user "
                          "WHERE r.from_user = ? ORDER BY r.created DESC LIMIT 50", (u["id"],)).fetchall()
    finally:
        con.close()
    return {"my_code": _show_code(code), "friends": [{"id": r["id"], "name": r["name"]} for r in fr],
            "incoming": [{"id": r["id"], "name": r["name"]} for r in inc],
            "outgoing": [{"id": r["id"], "name": r["name"]} for r in out]}


@app.post("/api/friends/request")
def friend_request(data: FriendReqIn, request: Request):
    u = require_pro(request)
    _group_hit(_friend_hits, str(u["id"]), FRIEND_REQ_PER_HOUR, 3600, "You sent a lot of requests. Please try again in a while.")
    code = "".join(ch for ch in data.code.upper() if ch.isalnum())
    if len(code) != 8:
        raise HTTPException(400, "A friend code has 8 letters and numbers, like ABCD-2345.")
    con = db()
    try:
        with con:
            row = con.execute("SELECT user_id FROM friend_codes WHERE code = ?", (code,)).fetchone()
            if not row:
                raise HTTPException(404, "No one has that friend code. Check it and try again.")
            other = row["user_id"]
            if other == u["id"]:
                raise HTTPException(400, "That is your own code. Send it to a friend instead.")
            a, b = _pair(u["id"], other)
            if con.execute("SELECT 1 AS x FROM friendships WHERE user_a = ? AND user_b = ?", (a, b)).fetchone():
                raise HTTPException(409, "You are already friends.")
            name = con.execute("SELECT name FROM users WHERE id = ?", (other,)).fetchone()["name"]
            back = con.execute("SELECT id FROM friend_requests WHERE from_user = ? AND to_user = ?", (other, u["id"])).fetchone()
            if back:  # they already asked you, so this makes you friends
                con.execute("DELETE FROM friend_requests WHERE id = ?", (back["id"],))
                con.execute("INSERT OR IGNORE INTO friendships (user_a, user_b, created) VALUES (?, ?, ?)", (a, b, time.time()))
                return {"status": "friends", "name": name}
            if con.execute("SELECT 1 AS x FROM friend_requests WHERE from_user = ? AND to_user = ?", (u["id"], other)).fetchone():
                raise HTTPException(409, "You already sent that person a request. Wait for them to accept.")
            con.execute("INSERT INTO friend_requests (from_user, to_user, created) VALUES (?, ?, ?)", (u["id"], other, time.time()))
    finally:
        con.close()
    return {"status": "sent", "name": name}


@app.post("/api/friends/{rid}/accept")
def friend_accept(rid: int, request: Request):
    u = require_pro(request)
    con = db()
    try:
        with con:
            r = con.execute("SELECT from_user FROM friend_requests WHERE id = ? AND to_user = ?", (rid, u["id"])).fetchone()
            if not r:
                raise HTTPException(404, "That request is gone.")
            a, b = _pair(u["id"], r["from_user"])
            con.execute("INSERT OR IGNORE INTO friendships (user_a, user_b, created) VALUES (?, ?, ?)", (a, b, time.time()))
            con.execute("DELETE FROM friend_requests WHERE id = ?", (rid,))
    finally:
        con.close()
    return {"ok": True}


@app.post("/api/friends/{rid}/decline")
def friend_decline(rid: int, request: Request):
    """The person who got the request says no, or the person who sent it takes it back."""
    u = require_pro(request)
    con = db()
    try:
        with con:
            con.execute("DELETE FROM friend_requests WHERE id = ? AND (to_user = ? OR from_user = ?)", (rid, u["id"], u["id"]))
    finally:
        con.close()
    return {"ok": True}


@app.post("/api/friends/remove")
def friend_remove(data: FriendRemoveIn, request: Request):
    u = require_pro(request)
    a, b = _pair(u["id"], data.user_id)
    con = db()
    try:
        with con:
            con.execute("DELETE FROM friendships WHERE user_a = ? AND user_b = ?", (a, b))
    finally:
        con.close()
    return {"ok": True}


@app.post("/api/chat")
async def chat(data: ChatIn, request: Request):
    """The Doubtly AI chat panel: solves a student's doubts and answers follow-ups."""
    if not GEMINI_API_KEY:
        raise HTTPException(500, "Server missing GEMINI_API_KEY. See README.")
    msgs: list[dict] = []
    for m in data.messages[-12:]:
        role = "user" if m.role == "user" else "model" if m.role in ("assistant", "model") else None
        text = m.content.strip()[:2000]
        if not role or not text:
            continue
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["parts"][0]["text"] += "\n" + text
        else:
            msgs.append({"role": role, "parts": [{"text": text}]})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    if not msgs or msgs[-1]["role"] != "user":
        raise HTTPException(400, "Type your doubt first.")

    user = current_user(request)
    if user:
        check_limits(f"chat:user:{user['id']}", CHAT_PRO_PER_DAY if is_pro(user) else CHAT_FREE_PER_DAY, guest=False, window=86400)
    else:
        check_limits("chat:" + _client_ip(request), CHAT_GUEST_PER_DAY, window=86400)

    lang = data.language.strip()[:40]
    lang = "Same as the student's message (reply in the language they write in)" if lang in ("", "Same as my question") else lang
    system = CHAT_PROMPT + f"\n\nReply language: {lang}\nStudent's level or curriculum: {data.level.strip()[:80] or 'not specified'}"
    if data.context.strip(): 
        system += "\n\nWhat the student is studying right now (from their study tools):\n" + data.context.strip()[:4000]     
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": msgs,
        "generationConfig": {"temperature": 0.4},
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent",
                params={"key": GEMINI_API_KEY},
                json=payload
            )
        r.raise_for_status()
        reply = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        if not reply:
            raise KeyError("empty")
        add_points(user, LB_CHAT_POINTS)
        return {"reply": reply[:6000]}

    except httpx.HTTPStatusError as e:
        print("Gemini API error:", e.response.status_code, e.response.text[:2000])
        raise HTTPException(502, f"AI service error ({e.response.status_code}). Try again.")
    except (KeyError, IndexError, json.JSONDecodeError):
        raise HTTPException(502, "Doubtly could not answer that. Try asking it a different way.")
    except httpx.HTTPError:
        raise HTTPException(504, "AI service timed out. Please retry.")

# ---------------------------------------------------------------------------
# World leaderboard: the people who study the most this month. Points come from
# real answers only. On the 1st, the top 4 of last month get bonus free tries.
# ---------------------------------------------------------------------------
class VisibilityIn(BaseModel):
    hidden: bool


def _lb_name(name: str, hidden, uid: int) -> str:
    return f"Student {1000 + (uid * 7919) % 9000}" if hidden else (name or "Student")[:20]


@app.get("/api/leaderboard")
def leaderboard(request: Request):
    ensure_awards()
    u = current_user(request)
    mk, now = month_key(), time.localtime()
    con = db()
    try:
        top = con.execute(
            "SELECT p.user_id, p.points, u.name, COALESCE(f.hidden, 0) AS hidden FROM lb_points p "
            "JOIN users u ON u.id = p.user_id LEFT JOIN lb_prefs f ON f.user_id = p.user_id "
            "WHERE p.month = ? AND p.points > 0 ORDER BY p.points DESC, p.updated ASC LIMIT 20", (mk,)).fetchall()
        me, hidden, my_bonus = None, False, None
        if u:
            row = con.execute("SELECT points, updated FROM lb_points WHERE user_id = ? AND month = ?", (u["id"], mk)).fetchone()
            if row and row["points"] > 0:
                ahead = con.execute("SELECT COUNT(*) AS n FROM lb_points WHERE month = ? AND (points > ? OR (points = ? AND updated < ?))",
                                    (mk, row["points"], row["points"], row["updated"])).fetchone()["n"]
                me = {"rank": ahead + 1, "points": row["points"]}
            pref = con.execute("SELECT hidden FROM lb_prefs WHERE user_id = ?", (u["id"],)).fetchone()
            hidden = bool(pref and pref["hidden"])
            b = con.execute("SELECT place, bonus FROM lb_rewards WHERE user_id = ? AND month = ?", (u["id"], mk)).fetchone()
            my_bonus = {"place": b["place"], "bonus": b["bonus"]} if b else None
        winners = con.execute(
            "SELECT r.place, r.bonus, r.user_id, u.name, COALESCE(f.hidden, 0) AS hidden FROM lb_rewards r "
            "JOIN users u ON u.id = r.user_id LEFT JOIN lb_prefs f ON f.user_id = r.user_id "
            "WHERE r.month = ? ORDER BY r.place", (mk,)).fetchall()
    finally:
        con.close()
    days_in = calendar.monthrange(now.tm_year, now.tm_mon)[1]
    return {
        "month": mk, "days_left": days_in - now.tm_mday + 1, "rewards": LB_REWARDS, "min_points": LB_MIN_POINTS,
        "per": {"use": LB_SOLVE_POINTS, "chat": LB_CHAT_POINTS},
        "top": [{"rank": i + 1, "name": _lb_name(r["name"], r["hidden"], r["user_id"]), "points": r["points"],
                 "you": bool(u) and r["user_id"] == u["id"]} for i, r in enumerate(top)],
        "me": me, "hidden": hidden, "my_bonus": my_bonus, "logged_in": bool(u),
        "last_month": prev_month(mk),
        "winners": [{"place": w["place"], "bonus": w["bonus"], "name": _lb_name(w["name"], w["hidden"], w["user_id"])} for w in winners],
    }


@app.post("/api/leaderboard/visibility")
def leaderboard_visibility(data: VisibilityIn, request: Request):
    u = current_user(request)
    if not u:
        raise HTTPException(401, "Log in first.")
    con = db()
    try:
        with con:
            con.execute("INSERT OR IGNORE INTO lb_prefs (user_id, hidden) VALUES (?, 0)", (u["id"],))
            con.execute("UPDATE lb_prefs SET hidden = ? WHERE user_id = ?", (1 if data.hidden else 0, u["id"]))
    finally:
        con.close()
    return {"hidden": data.hidden}


@app.get("/")
def home():
    return FileResponse("static/index.html")


# Every tool has its own page: Notes, Concept map, Podcast, Flashcards, Quiz, Play and Solve doubt.
TOOL_PAGES = ["notes", "concept-map", "podcast", "flashcards", "quiz", "play", "solve-doubt", "exam-plan", "study-room", "leaderboard", "groups"]


def _tool_route(name: str):
    def page():
        return FileResponse(f"static/{name}.html")
    return page


for _name in TOOL_PAGES:
    app.add_api_route("/" + _name, _tool_route(_name), methods=["GET"], include_in_schema=False)


@app.get("/upgrade")
def upgrade_page():
    return FileResponse("static/upgrade.html")


@app.get("/login")
def login_page():
    return FileResponse("static/login.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
