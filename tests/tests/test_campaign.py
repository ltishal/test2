import json
import unittest

from campaign import (
    active_check, apply_ability_event, initial_identity, merge_facts, merge_scene, normalize_ability,
    advance_clock, check_instructions_in_card, close_overdue_events, facts_context, pending_check,
    record_npc_actions, roll_result, rules_result, synchronize_clock, update_agenda, wait_plan,
)


class CampaignTest(unittest.TestCase):
    def test_legacy_check_survives_request_with_trimmed_history(self):
        reply = {"role": "assistant", "content": "Ловкость, DC 12. Команда: /roll 2d20kh1+1"}
        history = [{"role": "user", "content": "Иду по балке."}, reply]
        check = active_check({}, history)
        self.assertEqual(check["id"], active_check({}, history[-1:])["id"])
        restored = json.loads(json.dumps({"pending_check": check}))
        check = active_check(restored, [])
        roll = {"expr": "2d20kh1+1", "rolls": [15, 2], "check_id": check["id"]}
        result = roll_result(check, roll)
        self.assertEqual(result["total"], 16)
        self.assertTrue(result["success"])
        self.assertIsNone(active_check({"pending_check": None}, history))

    def test_dice_reject_mismatched_check_and_formula(self):
        check = pending_check("Атака копьём против КД 13. /roll 1d20+6")
        self.assertEqual(check["kind"], "attack")
        base = {"expr": "1d20+6", "rolls": [19], "check_id": check["id"]}
        for altered in ({"check_id": "another"}, {"expr": "1d20+4"}, {"rolls": [21]}):
            with self.subTest(altered=altered), self.assertRaises(ValueError):
                roll_result(check, {**base, **altered})
        self.assertEqual(roll_result(check, base)["total"], 25)

    def test_damage_calculation_and_capped_healing(self):
        check = pending_check("Болт попадает в тебя. Урон арбалета: /roll 1d8+2")
        result = roll_result(check, {"expr": check["expr"], "rolls": [1], "check_id": check["id"]})
        self.assertEqual(rules_result({"hp": 18, "max_hp": 18}, result)["hp_after"], 15)
        heal = pending_check("Второе дыхание. Брось лечение: /roll 1d10+3")
        result = roll_result(heal, {"expr": heal["expr"], "rolls": [4], "check_id": heal["id"]})
        self.assertEqual(rules_result({"hp": 15, "max_hp": 18}, result)["hp_after"], 18)

    def test_rest_and_spending(self):
        stance = {"name": "Защитная стойка", "uses_left": 1, "uses_max": 2, "recharge": "short_rest"}
        passive = {"name": "Сопротивление кислоте", "recharge": "passive"}
        spent, ok = apply_ability_event([stance, passive], "Защитная стойка")
        self.assertTrue(ok)
        self.assertEqual(spent[0]["uses_left"], 0)
        self.assertFalse(apply_ability_event(spent, "Защитная стойка")[1])
        restored, _ = apply_ability_event(spent, rest="short_rest")
        self.assertEqual(restored, [{**stance, "uses_left": 2}, passive])

    def test_dragon_breath_matches_legacy_name_with_a_damage_description(self):
        record = {"name": "Дыхание дракона, линия кислоты 1d6", "uses_max": 1,
                  "uses_left": 1, "recharge": "short_rest"}
        updated, used = apply_ability_event([record], "Дыхание дракона")
        self.assertTrue(used)
        self.assertEqual(updated[0]["uses_left"], 0)
        self.assertFalse(apply_ability_event(updated, "Дыхание дракона")[1])
        self.assertEqual(apply_ability_event(updated, rest="short_rest")[0][0]["uses_left"], 1)

    def test_legacy_unlimited_ability_has_no_charge_counter(self):
        legacy = {
            "name": "**Мистический заряд 1d10&#x20;**",
            "uses_max": -1,
            "uses_left": -1,
            "recharge": "short_rest",
        }
        normalized = {"name": "Мистический заряд 1d10", "recharge": "unlimited"}
        self.assertEqual(normalize_ability(legacy), normalized)
        self.assertEqual(apply_ability_event([legacy], "Мистический заряд 1d10"), ([normalized], True))
        self.assertEqual(apply_ability_event([normalized], rest="short_rest")[0], [normalized])
        passive = {**legacy, "name": "Сопротивление", "recharge": "passive"}
        self.assertEqual(normalize_ability(passive), {"name": "Сопротивление", "recharge": "passive"})

    def test_character_identity_keeps_chosen_fields_and_fills_skipped_fields(self):
        reply = "[[ЛИСТ]]\n👤 Лис — Эльф · Следопыт · Скиталец\n[[/ЛИСТ]]"
        selected = {"name": "Пустой", "race": "", "class": "Ловец ветра"}
        self.assertEqual(initial_identity(reply, selected), {
            "name": "Лис", "race": "Эльф", "class": "Ловец ветра", "background": "Скиталец",
        })
        self.assertEqual(initial_identity("Выберите расу.", selected), {})
        self.assertEqual(initial_identity(reply.removesuffix("[[/ЛИСТ]]"), selected), {})

    def test_wait_targets_and_event_once(self):
        state = {"world_minute": 1320}
        schedule = [{"id": "tavin", "title": "Ответ Тавина", "in_minutes": 60, "evidence": "Через час"}]
        grounded = lambda text: text in ("Через час", "Тавин отказался")
        state["agenda"] = update_agenda(state, schedule, [], 1320, grounded)
        self.assertEqual(wait_plan(state, "/wait next")["until"], 1380)
        self.assertEqual(wait_plan(state, "Жду до рассвета.")["label"], "День 2, 06:00")
        outcome = [{"id": "tavin", "outcome": "Тавин отказался", "evidence": "Тавин отказался"}]
        early = update_agenda(state, [], outcome, 1350, grounded)
        self.assertEqual(early[0]["status"], "scheduled")
        state["agenda"] = update_agenda(state, schedule, outcome, 1380, grounded)
        self.assertEqual(state["agenda"][0]["status"], "resolved")
        self.assertEqual(len(state["agenda"]), 1)
        with self.assertRaises(ValueError):
            wait_plan(state, "/wait next")

    def test_elapsed_time_is_charged_once_per_check(self):
        start = 17 * 60 + 30
        announcement = "Прошло десять минут. Сделай проверку Расследования, DC 12. /roll 1d20+3"
        elapsed = {"minutes": 10, "evidence": "Прошло десять минут"}
        current = advance_clock(start, announcement, elapsed, None, None, False,
                                lambda phrase: phrase in announcement)
        self.assertEqual(current, 17 * 60 + 40)
        result = "Вы потратили десять минут у конторки, теперь 17:50."
        check = {"clock_spent": 10}
        self.assertEqual(advance_clock(current, result, None, None, check, True, lambda _: True), current)
        result = "На обратный путь ушло ещё пять минут."
        elapsed = {"minutes": 5, "additional": True, "evidence": result}
        self.assertEqual(advance_clock(current, result, elapsed, None, check, True,
                                       lambda phrase: phrase in result), current + 5)

    def test_explicit_arrival_and_wait_clock(self):
        reply = "Обещанная встреча в 22:00. К 21:34 Лира снова рядом с Марой."
        self.assertEqual(advance_clock(1284, reply, None, None, None, False, lambda _: False), 1294)
        self.assertEqual(advance_clock(1430, "Сейчас 00:10.", None, None, None, False, lambda _: False), 1450)
        self.assertEqual(advance_clock(1284, "Они назначили время 22:00.",
                                       None, None, None, False, lambda _: False), 1284)
        reply = "Вы ждали с 05:34. Сейчас День 2, 05:34, а встреча в 07:00."
        self.assertEqual(synchronize_clock(reply, 1800),
                         "Вы ждали с 05:34. Сейчас День 2, 06:00, а встреча в 07:00.")

    def test_undated_errand_has_a_check_in_and_can_get_a_late_outcome(self):
        evidence = "Савел уходит искать лекаря."
        patch = {"id": "savel-healer", "title": "Известия от Савела", "location": "Сарай",
                 "check_in": True, "evidence": evidence}
        state = {"world_minute": 1101, "agenda": update_agenda({}, [patch], [], 1101, lambda _: True)}
        event = state["agenda"][0]
        self.assertTrue(event["check_in"])
        self.assertEqual(wait_plan(state, "/wait next")["until"], 1131)
        close_overdue_events(state["agenda"], 1131)
        self.assertTrue(event["no_report"])
        self.assertIn("Сарай", event["outcome"])
        late = {"id": patch["id"], "outcome": "Савел привёл лекаря", "evidence": "Савел привёл лекаря."}
        events = update_agenda(state, [patch], [late], 1140, lambda _: True)
        self.assertEqual(len(events), 1)
        self.assertNotIn("no_report", events[0])
        self.assertEqual(events[0]["outcome"], late["outcome"])

    def test_npc_statements_cannot_replace_confirmed_actions(self):
        quote = "Савел передаёт верёвку и уходит за помощью."
        patch = {"id": "savel-search", "actor": "Савел", "evidence": quote}
        facts = record_npc_actions([], [patch, {**patch, "id": "invalid", "evidence": "Савел вернулся."}],
                                   {"location": "Волнорез"}, 1101, lambda text: text == quote)
        self.assertEqual(len(facts), 1)
        action = facts[0]
        facts = merge_facts(facts, [
            {"id": action["id"], "subject": "Савел", "detail": "Никуда не уходил.", "status": "reported"},
            {"id": "savel-statement", "subject": "Савел", "detail": "Говорит, что не уходил.", "status": "reported"},
        ])
        self.assertEqual(facts_context(facts, "Где искать Савела?")[0], action)
        self.assertEqual((action["detail"], action["location"], action["minute"]), (quote, "Волнорез", 1101))

    def test_duplicate_formula_keeps_a_structured_check(self):
        reply = ("Ловкость рук, DC 15. Команда: /roll 1d20+5."
                 "Фонарь скользит. Ловкость рук, DC 15. **Команда: /roll 1d20+5.**")
        check = pending_check(reply, {"title": "Ловкость рук"})
        self.assertEqual(check["dc"], 15)
        rendered = check_instructions_in_card(reply, check)
        self.assertIn("Фонарь скользит.", rendered)
        self.assertNotIn("/roll", rendered)
        self.assertNotIn("**", rendered)
        self.assertIsNone(pending_check(reply.replace("DC 15. **", "DC 12. **")))
        self.assertIsNone(pending_check(reply.replace("/roll 1d20+5.**", "/roll 1d20+2.**")))

    def test_scene_identity_and_transfer_of_a_clue(self):
        scene = merge_scene({}, {"location": "Мельница", "participants": ["Рагар", "Гонец"]})
        update = merge_scene(scene, {"situation": "Обнаружена сумка."})
        self.assertEqual(scene["id"], update["id"])
        arrived = merge_scene(update, {"location": "Чёрный брод", "participants": ["Рагар", "Начальник"]})
        self.assertNotEqual(scene["id"], arrived["id"])
        facts = merge_facts([], [{"id": "дощечка", "subject": "Дощечка", "detail": "Имя Вейн",
                                  "holder": "Рагар", "status": "confirmed"}])
        facts = merge_facts(facts, [{"id": "дощечка", "holder": "Начальник"}])
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["detail"], "Имя Вейн")
        self.assertEqual(facts[0]["holder"], "Начальник")


if __name__ == "__main__":
    unittest.main()
