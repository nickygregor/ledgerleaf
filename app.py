"""Ledgerleaf: single-user, loopback-only spending journal. Python 3.11+."""
import argparse
import calendar
import csv
import io
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

STATIC = Path(__file__).parent / "web"
CATEGORIES = ("Food", "Housing", "Transport", "Learning", "Health", "Leisure", "Other")


def cents(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount > 1000000:
            raise ValueError("Amount must be between 0.01 and 1,000,000.")
        if amount != amount.quantize(Decimal("0.01")):
            raise ValueError("Use at most two decimal places.")
        return int(amount * 100)
    except (InvalidOperation, TypeError):
        raise ValueError("Enter a valid amount.") from None


def month_range(month):
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError("Month must use YYYY-MM.")
    first = date.fromisoformat(month + "-01")
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return first.isoformat(), last.isoformat()


class Ledger:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS entries (
                    id INTEGER PRIMARY KEY, day TEXT NOT NULL,
                    merchant TEXT NOT NULL, category TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0), note TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS entries_day ON entries(day);
                CREATE TABLE IF NOT EXISTS budgets (month TEXT PRIMARY KEY, amount_cents INTEGER NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object.")
        day = str(payload.get("day", ""))
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("Date must use YYYY-MM-DD.")
        merchant = str(payload.get("merchant", "")).strip()
        category = payload.get("category")
        note = str(payload.get("note", "")).strip()
        if not merchant or len(merchant) > 100 or len(note) > 500:
            raise ValueError("Merchant is required (100 characters max); note is 500 characters max.")
        if category not in CATEGORIES:
            raise ValueError("Choose a supported category.")
        amount = cents(payload.get("amount"))
        with self.connect() as db:
            result = db.execute("INSERT INTO entries(day,merchant,category,amount_cents,note) VALUES(?,?,?,?,?)",
                                (day, merchant, category, amount, note))
            return result.lastrowid

    def remove(self, entry_id):
        with self.connect() as db:
            return db.execute("DELETE FROM entries WHERE id=?", (entry_id,)).rowcount > 0

    def budget(self, month, amount):
        month_range(month)
        amount = cents(amount)
        with self.connect() as db:
            db.execute("INSERT INTO budgets VALUES(?,?) ON CONFLICT(month) DO UPDATE SET amount_cents=excluded.amount_cents", (month, amount))

    def report(self, month, today=None):
        first, last = month_range(month)
        today = today or date.today()
        with self.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM entries WHERE day BETWEEN ? AND ? ORDER BY day DESC,id DESC", (first,last))]
            budget_row = db.execute("SELECT amount_cents FROM budgets WHERE month=?", (month,)).fetchone()
        total = sum(row["amount_cents"] for row in rows)
        categories = {name: sum(row["amount_cents"] for row in rows if row["category"] == name) for name in CATEGORIES}
        budget = budget_row[0] if budget_row else None
        # Actual-to-date pace; future-dated entries do not distort current-month projection.
        elapsed_spend = sum(row["amount_cents"] for row in rows if row["day"] <= today.isoformat())
        days = int(last[-2:])
        current = month == today.strftime("%Y-%m")
        projection = round(elapsed_spend / today.day * days) if current else None
        return {"month": month, "entries": rows, "total": total, "budget": budget,
                "remaining": budget-total if budget is not None else None,
                "projection": projection, "categories": categories,
                "daily": [{"day": f"{month}-{d:02}", "amount": sum(r["amount_cents"] for r in rows if r["day"] == f"{month}-{d:02}")} for d in range(1, days+1)]}

    def demo(self):
        month = date.today().strftime("%Y-%m")
        # Transactional and idempotent: never seed into a nonempty journal.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]:
                raise ValueError("Demo data can only be added to an empty journal.")
            sample = [(1,"Campus groceries","Food",4860),(2,"Monthly transit","Transport",4000),
                      (3,"Python book","Learning",3200),(4,"Coffee with friends","Food",1275),
                      (5,"Apartment rent","Housing",65000),(6,"Climbing gym","Health",2400),
                      (7,"Movie night","Leisure",1850),(8,"Groceries","Food",5520)]
            db.executemany("INSERT INTO entries(day,merchant,category,amount_cents,note) VALUES(?,?,?,?,?)",
                           [(f"{month}-{min(d,date.today().day):02}",m,c,a,"Illustrative demo entry") for d,m,c,a in sample])
            db.execute("INSERT OR IGNORE INTO budgets VALUES(?,?)", (month,140000))


def csv_export(report):
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(["date","merchant","category","amount_usd","note"])
    def safe(value):
        # Prevent spreadsheet formula execution when opening exported user text.
        return "'"+value if value.lstrip().startswith(("=","+","-","@")) else value
    for row in report["entries"]:
        writer.writerow([row["day"],safe(row["merchant"]),row["category"],f'{row["amount_cents"]/100:.2f}',safe(row["note"])])
    return out.getvalue().encode()


def handler(ledger):
    class Handler(BaseHTTPRequestHandler):
        def send(self, status, body, mime="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def valid_host(self):
            return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def do_GET(self):
            if not self.valid_host():
                return self.send(403,{"error":"Loopback access only."})
            path = urlsplit(self.path)
            try:
                if path.path in ("/api/report","/api/export"):
                    month = parse_qs(path.query).get("month",[date.today().strftime("%Y-%m")])[0]
                    report = ledger.report(month)
                    if path.path.endswith("export"):
                        return self.send(200,csv_export(report),"text/csv; charset=utf-8")
                    return self.send(200,report)
                files = {"/":"index.html","/app.js":"app.js","/style.css":"style.css"}
                if path.path not in files:
                    return self.send(404,{"error":"Not found"})
                target = STATIC / files[path.path]
                mime = {".html":"text/html", ".js":"text/javascript", ".css":"text/css"}[target.suffix]
                self.send(200,target.read_bytes(),mime+"; charset=utf-8")
            except (ValueError, OverflowError) as exc:
                self.send(400,{"error":str(exc)})

        def do_POST(self):
            origin = self.headers.get("Origin")
            if not self.valid_host() or (origin and origin != "http://"+self.headers.get("Host", "")):
                return self.send(403,{"error":"Origin not allowed."})
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.send(415,{"error":"Use application/json."})
            try:
                length = int(self.headers.get("Content-Length","0"))
                if length < 1 or length > 8192:
                    raise ValueError("Request must be between 1 and 8192 bytes.")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload,dict):
                    raise ValueError("Expected a JSON object.")
                if self.path == "/api/entries":
                    return self.send(201,{"id":ledger.add(payload)})
                if self.path == "/api/delete":
                    removed = ledger.remove(int(payload["id"]))
                    return self.send(200 if removed else 404,{"removed":removed})
                if self.path == "/api/budget":
                    ledger.budget(payload["month"],payload["amount"])
                elif self.path == "/api/demo":
                    ledger.demo()
                else:
                    return self.send(404,{"error":"Not found"})
                self.send(200,{"ok":True})
            except (ValueError, KeyError, TypeError) as exc:
                self.send(400,{"error":str(exc)})
    return Handler

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port",type=int,default=8101)
    parser.add_argument("--db",default=str(Path(__file__).with_name("ledger.sqlite3")))
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1",args.port),handler(Ledger(args.db)))
    print(f"Ledgerleaf → http://127.0.0.1:{server.server_port}",flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
