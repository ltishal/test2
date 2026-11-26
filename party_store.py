"""Private game saves and revision-checked turns, stored on the Railway volume."""

import json
import sqlite3
from contextlib import closing, contextmanager
from time import time


class PartyMissing(Exception):
    pass


class PartyConflict(Exception):
    pass


def _encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class PartyStore:
    def __init__(self, path):
        self.path = path

    def initialize(self):
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS party (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    history TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    folded INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS party_owner_update
                    ON party(owner, updated_at DESC);
                CREATE TABLE IF NOT EXISTS party_turn (
                    party_id TEXT NOT NULL REFERENCES party(id) ON DELETE CASCADE,
                    id TEXT NOT NULL,
                    base_revision INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    reply TEXT NOT NULL,
                    committed_revision INTEGER,
                    PRIMARY KEY(party_id, id)
                );
                CREATE INDEX IF NOT EXISTS party_turn_revision
                    ON party_turn(party_id, committed_revision);
                CREATE UNIQUE INDEX IF NOT EXISTS party_pending_turn
                    ON party_turn(party_id, base_revision) WHERE committed_revision IS NULL;
            """)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _write(self):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def _party(db, owner, party_id):
        row = db.execute("SELECT * FROM party WHERE id=? AND owner=?", (party_id, owner)).fetchone()
        if not row:
            raise PartyMissing("Партия не найдена в этом профиле браузера.")
        return row

    @staticmethod
    def _pending(db, party):
        return db.execute("""
            SELECT * FROM party_turn
            WHERE party_id=? AND base_revision=? AND committed_revision IS NULL
        """, (party["id"], party["revision"])).fetchone()

    @staticmethod
    def _unpack_turn(row):
        if row is None:
            return None
        return {"request_id": row["id"], "revision": row["base_revision"],
                "action": json.loads(row["action"]), "reply": json.loads(row["reply"]),
                "committed_revision": row["committed_revision"]}

    def _snapshot(self, db, row):
        history = json.loads(row["history"])
        turns = db.execute("""SELECT action, reply FROM party_turn WHERE party_id=?
                              AND committed_revision IS NOT NULL ORDER BY committed_revision""",
                           (row["id"],))
        for turn in turns:
            history.extend((json.loads(turn["action"]), json.loads(turn["reply"])))
        return {"id": row["id"], "revision": row["revision"], "state": json.loads(row["state"]),
                "messages": history, "draft": self._unpack_turn(self._pending(db, row)),
                "memory": {"text": row["summary"], "folded": row["folded"]}}

    def create(self, owner, party_id, state, messages, memory=None):
        memory = memory if isinstance(memory, dict) else {}
        text, folded = memory.get("text"), memory.get("folded")
        if (not isinstance(text, str) or type(folded) is not int or folded < 0
                or folded > sum(m.get("role") in ("user", "assistant") for m in messages)):
            text, folded = "", 0
        with self._write() as db:
            db.execute("""INSERT OR IGNORE INTO party(id,owner,state,history,updated_at,summary,folded)
                          VALUES(?,?,?,?,?,?,?)""",
                       (party_id, owner, _encode(state), _encode(messages), time(), text[:8000], folded))
            return self._snapshot(db, self._party(db, owner, party_id))

    def get(self, owner, party_id):
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            return self._snapshot(db, self._party(db, owner, party_id))

    def list(self, owner):
        with closing(self._connect()) as db:
            rows = db.execute("""SELECT id, state, revision, updated_at FROM party
                                 WHERE owner=? ORDER BY updated_at DESC""", (owner,)).fetchall()
        result = []
        for row in rows:
            state = json.loads(row["state"])
            scene = state.get("scene")
            result.append({
                "id": row["id"], "name": state.get("name") or "Новая партия",
                "level": state.get("level"), "updated_at": row["updated_at"],
                "location": scene.get("location") if isinstance(scene, dict) else None,
            })
        return result

    def turn(self, owner, party_id, revision, turn_id):
        """Get the committed base and any previously generated copy of this turn."""
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            row = self._party(db, owner, party_id)
            turn = db.execute("SELECT * FROM party_turn WHERE party_id=? AND id=?",
                              (party_id, turn_id)).fetchone()
            if turn and turn["committed_revision"] is not None:
                return self._snapshot(db, row), self._unpack_turn(turn)
            self._check_revision(row, revision)
            pending = self._pending(db, row)
            if pending and pending["id"] != turn_id:
                raise PartyConflict("В партии уже есть незавершённый ход. Откройте её снова.")
            return self._snapshot(db, row), self._unpack_turn(turn)

    @staticmethod
    def _check_revision(row, expected):
        if row["revision"] != expected:
            raise PartyConflict("В другой вкладке появилась новая версия партии. Откройте её снова.")

    def stage(self, owner, party_id, revision, turn_id, action, reply):
        with self._write() as db:
            party = self._party(db, owner, party_id)
            self._check_revision(party, revision)
            pending = self._pending(db, party)
            if pending:
                if pending["id"] != turn_id or pending["action"] != _encode(action):
                    raise PartyConflict("В партии уже есть другой незавершённый ход.")
                return self._unpack_turn(pending)
            db.execute("""INSERT INTO party_turn(party_id,id,base_revision,action,reply)
                          VALUES(?,?,?,?,?)""", (party_id, turn_id, revision, _encode(action), _encode(reply)))
            return {"action": action, "reply": reply, "request_id": turn_id, "revision": revision}

    def commit(self, owner, party_id, revision, turn_id, state, reply):
        with self._write() as db:
            party = self._party(db, owner, party_id)
            turn = db.execute("SELECT * FROM party_turn WHERE party_id=? AND id=?",
                              (party_id, turn_id)).fetchone()
            if turn and turn["committed_revision"] is not None:
                return self._snapshot(db, party)
            self._check_revision(party, revision)
            if not turn or turn["base_revision"] != revision:
                raise PartyConflict("Готовый ответ этого хода не найден.")
            next_revision = revision + 1
            db.execute("UPDATE party SET revision=?,state=?,updated_at=? WHERE id=?",
                       (next_revision, _encode(state), time(), party_id))
            db.execute("UPDATE party_turn SET committed_revision=?,reply=? WHERE party_id=? AND id=?",
                       (next_revision, _encode(reply), party_id, turn_id))
            return self._snapshot(db, self._party(db, owner, party_id))

    def discard(self, owner, party_id, revision, turn_id):
        with self._write() as db:
            party = self._party(db, owner, party_id)
            self._check_revision(party, revision)
            db.execute("""DELETE FROM party_turn WHERE party_id=? AND id=? AND base_revision=?
                          AND committed_revision IS NULL""", (party_id, turn_id, revision))
            return self._snapshot(db, party)

    def note(self, owner, party_id, revision, note_id, remove):
        with self._write() as db:
            party = self._party(db, owner, party_id)
            self._check_revision(party, revision)
            if self._pending(db, party):
                raise PartyConflict("Сначала завершите текущий ход.")
            state = json.loads(party["state"])
            notes = state.get("notes") or []
            for i, note in enumerate(notes):
                if isinstance(note, dict) and str(note.get("id")) == note_id:
                    if remove:
                        notes.pop(i)
                    else:
                        note["unread"] = False
                    state["notes"] = notes
                    db.execute("UPDATE party SET state=?,revision=revision+1,updated_at=? WHERE id=?",
                               (_encode(state), time(), party_id))
                    break
            return self._snapshot(db, self._party(db, owner, party_id))

    def summary(self, party_id):
        with closing(self._connect()) as db:
            row = db.execute("SELECT summary,folded FROM party WHERE id=?", (party_id,)).fetchone()
        return {"text": row["summary"], "folded": row["folded"]} if row else {"text": "", "folded": 0}

    def save_summary(self, party_id, text, folded):
        with self._write() as db:
            db.execute("UPDATE party SET summary=?,folded=? WHERE id=? AND folded<?",
                       (text, folded, party_id, folded))
