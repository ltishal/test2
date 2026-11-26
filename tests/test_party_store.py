import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from party_store import PartyConflict, PartyMissing, PartyStore


class PartyStoreTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "parties.sqlite3"
        self.store = PartyStore(self.path)
        self.store.initialize()
        self.state = {"name": "Сая", "hp": 9, "scene": {"location": "Дымный Брод"}}
        self.store.create("owner-a", "saya", self.state, [])
        self.action = {"role": "user", "content": "Передаю тубу Мире"}
        self.reply = {"role": "assistant", "content": "Мира принимает тубу."}

    def test_owners_and_characters_are_independent(self):
        self.store.create("owner-b", "ragar", {"name": "Рагар"}, [])
        self.store.create("owner-a", "li", {"name": "Ли"}, [])
        self.assertEqual({x["id"] for x in self.store.list("owner-a")}, {"saya", "li"})
        self.assertEqual([x["id"] for x in self.store.list("owner-b")], ["ragar"])
        with self.assertRaises(PartyMissing):
            self.store.get("owner-b", "saya")
        with self.assertRaises(PartyMissing):
            self.store.stage("owner-b", "saya", 0, "turn", self.action, self.reply)
        with self.assertRaises(PartyMissing):
            self.store.create("owner-b", "saya", {"name": "Рагар"}, [])
        self.assertEqual(self.store.get("owner-a", "saya")["state"], self.state)

    def test_restart_restores_draft_and_commit_is_idempotent(self):
        store = self.store
        store.stage("owner-a", "saya", 0, "turn", self.action, self.reply)
        store = PartyStore(self.path)
        draft = store.get("owner-a", "saya")["draft"]
        self.assertEqual(draft["action"], self.action)
        self.assertEqual(draft["reply"], self.reply)
        state = {**self.state, "xp": 50}
        committed = store.commit("owner-a", "saya", 0, "turn", state, self.reply)
        repeated = store.commit("owner-a", "saya", 0, "turn", {**state, "xp": 100}, self.reply)
        self.assertEqual(committed, repeated)
        self.assertEqual(repeated["revision"], 1)
        self.assertIsNone(repeated["draft"])
        self.assertEqual(repeated["messages"], [self.action, self.reply])
        self.assertEqual(repeated["state"]["xp"], 50)

    def test_other_tab_cannot_commit_from_an_old_revision(self):
        self.store.stage("owner-a", "saya", 0, "turn", self.action, self.reply)
        with self.assertRaises(PartyConflict):
            self.store.stage("owner-a", "saya", 0, "other", self.action, self.reply)
        self.store.commit("owner-a", "saya", 0, "turn", self.state, self.reply)
        with self.assertRaises(PartyConflict):
            self.store.stage("owner-a", "saya", 0, "other", self.action, self.reply)

    def test_concurrent_retries_only_commit_one_result(self):
        self.store.stage("owner-a", "saya", 0, "turn", self.action, self.reply)
        def commit(xp):
            return self.store.commit("owner-a", "saya", 0, "turn", {**self.state, "xp": xp}, self.reply)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first, second = executor.map(commit, [50, 100])
        self.assertEqual(first, second)
        self.assertEqual(first["revision"], 1)
        self.assertEqual(len(first["messages"]), 2)

    def test_cancel_keeps_previous_state_and_durable_summary_never_moves_backwards(self):
        self.store.stage("owner-a", "saya", 0, "turn", self.action, self.reply)
        after = self.store.discard("owner-a", "saya", 0, "turn")
        self.assertIsNone(after["draft"])
        self.assertEqual(after["state"], self.state)
        self.assertEqual(after["revision"], 0)
        self.store.save_summary("saya", "Туба у Миры.", 20)
        self.store.save_summary("saya", "Туба в пути.", 10)
        restored = PartyStore(self.path).summary("saya")
        self.assertEqual(restored, {"text": "Туба у Миры.", "folded": 20})

    def test_notes_update_only_the_current_party(self):
        store = self.store
        store.create("owner-a", "li", {"notes": [{"id": "note", "unread": True}]}, [])
        changed = store.note("owner-a", "li", 0, "note", False)
        self.assertFalse(changed["state"]["notes"][0]["unread"])
        self.assertEqual(changed["revision"], 1)
        with self.assertRaises(PartyConflict):
            store.note("owner-a", "li", 0, "note", True)
        changed = store.note("owner-a", "li", 1, "note", True)
        self.assertEqual(changed["state"]["notes"], [])
        self.assertEqual(store.get("owner-a", "saya")["state"], self.state)
