"""Server-side records of model usage for each game turn."""

import os
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from uuid import UUID, uuid4

from flask import Response, current_app, render_template, request


def state_turn_id(header):
    try:
        return UUID(header).hex if header else uuid4().hex
    except ValueError:
        return uuid4().hex


class TokenUsage:
    def __init__(self, path):
        self.path = path

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            db.execute("""
                CREATE TABLE IF NOT EXISTS model_usage (
                    id INTEGER PRIMARY KEY, turn_id TEXT NOT NULL,
                    recorded_at TEXT NOT NULL, stage TEXT NOT NULL,
                    model TEXT NOT NULL, input_tokens INTEGER,
                    output_tokens INTEGER, total_tokens INTEGER
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS usage_turn_idx ON model_usage (turn_id)")
        except sqlite3.Error:
            db.close()
            raise
        return db

    def record(self, turn_id, stage, model, usage):
        counts = tuple(getattr(usage, field, None) for field in
                       ("prompt_tokens", "completion_tokens", "total_tokens"))
        if not all(isinstance(value, int) and value >= 0 for value in counts):
            counts = (None, None, None)
        try:
            with closing(self._connect()) as db, db:
                db.execute("""
                    INSERT INTO model_usage
                      (turn_id, recorded_at, stage, model, input_tokens, output_tokens, total_tokens)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (turn_id, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                      stage, model, *counts))
        except sqlite3.Error:
            current_app.logger.exception("Не удалось записать расход токенов")

    def recent_turns(self, page):
        with closing(self._connect()) as db:
            rows = db.execute("""
                WITH recent_turns AS (
                    SELECT turn_id, MIN(id) AS first_id
                    FROM model_usage GROUP BY turn_id
                    ORDER BY first_id DESC LIMIT 51 OFFSET ?
                )
                SELECT model_usage.* FROM recent_turns
                JOIN model_usage USING (turn_id)
                ORDER BY recent_turns.first_id DESC, model_usage.id
            """, ((page - 1) * 50,)).fetchall()
        turns = {}
        for row in rows:
            turn = turns.setdefault(row["turn_id"], {
                "date": row["recorded_at"], "total": 0, "input": 0, "output": 0,
                "missing": 0, "calls": [],
            })
            turn["calls"].append(row)
            if row["total_tokens"] is None:
                turn["missing"] += 1
            else:
                turn["total"] += row["total_tokens"]
                turn["input"] += row["input_tokens"]
                turn["output"] += row["output_tokens"]
        return list(turns.values())[:50], len(turns) > 50

    def dashboard(self):
        password = os.environ.get("TOKEN_ADMIN_PASSWORD", "")
        auth = request.authorization
        headers = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex",
                   "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"}
        if not password:
            return Response("Not found", 404, headers)
        if (not auth or auth.type != "basic" or auth.username != "admin"
                or not secrets.compare_digest((auth.password or "").encode(), password.encode())):
            headers["WWW-Authenticate"] = 'Basic realm="Token usage", charset="UTF-8"'
            return Response("Authentication required", 401, headers)
        page = max(1, min(request.args.get("page", 1, type=int), 1_000_000))
        try:
            turns, more = self.recent_turns(page)
        except sqlite3.Error:
            current_app.logger.exception("Не удалось прочитать расход токенов")
            return Response("Token usage temporarily unavailable", 503, headers)
        return Response(render_template("token_usage.html", turns=turns, page=page, more=more),
                        headers=headers)
