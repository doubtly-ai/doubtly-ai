"""Doubtly on Telegram. Students send a doubt (text or a photo of a question) and get it solved step by step.

How to run (see README, "Telegram bot"):
    set TELEGRAM_BOT_TOKEN=...   (or put it in .env)
    python telegram_bot.py
It reuses the same GEMINI_API_KEY and the same chat instructions as the website.
"""
import base64
import os
import time
from collections import defaultdict, deque

import httpx

from app import CHAT_PROMPT, GEMINI_API_KEY, GEMINI_BASE, GEMINI_MODEL  # also loads your .env

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG = os.getenv("TELEGRAM_BASE", "https://api.telegram.org").rstrip("/")
PER_DAY = int(os.getenv("TELEGRAM_PER_DAY", "30"))  # messages a day for each person
HISTORY_TURNS = 6
WELCOME = ("Hi! I am Doubtly AI. 📚\n\nSend me any doubt, from any subject, and I will solve it step by step. "
           "You can also send a photo of the question.\n\n/new starts a fresh chat.")

history: dict[int, list] = defaultdict(list)
hits: dict[int, deque] = defaultdict(deque)


def allowed(chat: int) -> bool:
    now = time.time()
    h = hits[chat]
    while h and now - h[0] > 86400:
        h.popleft()
    if len(h) >= PER_DAY:
        return False
    h.append(now)
    return True


def ask_gemini(chat: int, text: str, image: tuple[bytes, str] | None = None) -> str:
    parts = []
    if image:
        parts.append({"inline_data": {"mime_type": image[1], "data": base64.b64encode(image[0]).decode()}})
    parts.append({"text": text or "Solve the question in this photo."})
    contents = history[chat][-HISTORY_TURNS * 2:] + [{"role": "user", "parts": parts}]
    r = httpx.post(f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent", params={"key": GEMINI_API_KEY}, timeout=120,
                   json={"system_instruction": {"parts": [{"text": CHAT_PROMPT}]}, "contents": contents,
                         "generationConfig": {"temperature": 0.3}})
    r.raise_for_status()
    answer = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    # keep the text only in memory (never the photo), so the history stays small
    history[chat] = (history[chat] + [{"role": "user", "parts": [{"text": text or "(a photo)"}]},
                                      {"role": "model", "parts": [{"text": answer}]}])[-HISTORY_TURNS * 2:]
    return answer


def send(chat: int, text: str) -> None:
    for i in range(0, len(text), 4000):  # Telegram's message limit is 4096 characters
        httpx.post(f"{TG}/bot{TOKEN}/sendMessage", json={"chat_id": chat, "text": text[i:i + 4000]}, timeout=30)


def download_photo(msg: dict) -> tuple[bytes, str] | None:
    photos = msg.get("photo")
    if not photos:
        return None
    best = max(photos, key=lambda p: p.get("file_size", 0))
    info = httpx.get(f"{TG}/bot{TOKEN}/getFile", params={"file_id": best["file_id"]}, timeout=30).json()["result"]
    data = httpx.get(f"{TG}/file/bot{TOKEN}/{info['file_path']}", timeout=60).content
    if len(data) > 8 * 1024 * 1024:
        return None
    return data, "image/jpeg"


def handle(msg: dict) -> None:
    chat = msg["chat"]["id"]
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if text.startswith("/start"):
        send(chat, WELCOME)
        return
    if text.startswith("/new"):
        history.pop(chat, None)
        send(chat, "Fresh start! Send your next doubt.")
        return
    image = download_photo(msg) if msg.get("photo") else None
    if not text and not image:
        send(chat, "Send me your doubt as text, or a photo of the question.")
        return
    if not allowed(chat):
        send(chat, f"You have used your {PER_DAY} messages for today. Come back tomorrow!")
        return
    try:
        send(chat, ask_gemini(chat, text[:4000], image))
    except (httpx.HTTPError, KeyError, IndexError):
        hits[chat].pop()  # a failed try does not count
        send(chat, "Sorry, I could not answer that right now. Please try again in a minute.")


def main() -> None:
    if not TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN first. See README, 'Telegram bot'.")
    if not GEMINI_API_KEY:
        raise SystemExit("Set GEMINI_API_KEY in your .env file first.")
    print("Doubtly Telegram bot is running. Press Ctrl+C to stop.")
    offset = 0
    while True:
        try:
            r = httpx.get(f"{TG}/bot{TOKEN}/getUpdates", params={"timeout": 30, "offset": offset}, timeout=45)
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                if u.get("message"):
                    try:
                        handle(u["message"])
                    except Exception as e:  # one bad message must never stop the bot
                        print("Could not handle a message:", e, flush=True)
        except httpx.HTTPError:
            time.sleep(5)


if __name__ == "__main__":
    main()
