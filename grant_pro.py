"""Give a student Doubtly Pro by hand, for example after you see their payment.

Usage (in the Doubtly folder, with the app stopped or running, both are fine):
    python grant_pro.py student@email.com          # 30 days of Pro
    python grant_pro.py student@email.com 60       # 60 days of Pro
    python grant_pro.py student@email.com 0        # remove Pro
"""
import os
import sqlite3
import sys
import time

from dotenv import load_dotenv

load_dotenv()
DB_PATH = os.getenv("DB_PATH", "doubtly.db")

if len(sys.argv) < 2:
    sys.exit(__doc__)
email = sys.argv[1].strip().lower()
days = int(sys.argv[2]) if len(sys.argv) > 2 else 30

con = sqlite3.connect(DB_PATH)
row = con.execute("SELECT id, pro_until FROM users WHERE email = ?", (email,)).fetchone()
if not row:
    sys.exit(f"No account with the email {email}. Ask them to sign up first.")
until = 0 if days <= 0 else max(time.time(), row[1] or 0) + days * 86400
with con:
    con.execute("UPDATE users SET pro_until = ? WHERE id = ?", (until, row[0]))
print(f"{email}: Pro removed." if days <= 0 else f"{email}: Pro until {time.strftime('%d %b %Y', time.localtime(until))}.")
