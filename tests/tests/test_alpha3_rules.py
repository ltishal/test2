import unittest

from engine.combat import combat_turn, profile
from engine.economy import apply_exchange, apply_purchase, current_offers, merge_offers
from engine.effects import resolve_effects
from engine.world import merge_entities


class AlphaThreeRulesTest(unittest.TestCase):
    def setUp(self):
        self.hero = {"name": "Лея", "hp": 20, "max_hp": 20, "xp": 0, "gold": 10,
                     "silver": 0, "copper": 0, "scene": {"location": "Двор"},
                     "stats": {"dex": 14, "int": 16},
                     "abilities": [{"name": "Искры снов", "uses_max": 2, "uses_left": 2,
                                    "recharge": "long_rest",
                                    "mechanics": {"dice": "1d4", "hits": 2, "element": "fire",
                                                  "cost": 3, "cooldown": 2, "automatic": True}}],
                     "world_entities": [{"id": "guard", "kind": "npc", "name": "Орен",
                                         "location": "Двор", "hp": 20, "max_hp": 20,
                                         "threat": "normal", "combat_profile":
                                             {"resistances": {"fire": "resistant"}}}]}

    def test_server_calculates_hits_resistance_mana_and_cooldown(self):
        numbers = iter((15, 10, 4, 4, 1))
        state, reply = combat_turn(self.hero, "/cast", "Искры снов | Орен",
                                   roller=lambda low, high: next(numbers))
        self.assertEqual(state["world_entities"][0]["hp"], 16)
        self.assertEqual(state["abilities"][0]["uses_left"], 1)
        self.assertEqual(state["mana"], 9)
        self.assertEqual(state["combat"]["round"], 1)
        self.assertIn("2+2", reply)
        self.assertEqual(self.hero["world_entities"][0]["hp"], 20)
        with self.assertRaisesRegex(ValueError, "восстанавливается"):
            combat_turn(state, "/cast", "Искры снов", roller=lambda low, high: low)

    def test_enemy_known_ability_uses_mana_before_hero_if_initiative_is_higher(self):
        target = self.hero["world_entities"][0]
        target["combat_profile"] = {"style": "caster", "abilities":
                                    [{"name": "Зимнее дыхание", "dice": "1d6", "element": "cold",
                                      "cost": 3, "cooldown": 2, "automatic": True}]}
        numbers = iter((1, 20, 6, 1))
        state, reply = combat_turn(self.hero, "/attack", "Орен", roller=lambda low, high: next(numbers))
        self.assertIn("Зимнее дыхание", reply)
        self.assertIn("Первым действует «Орен»", reply)
        self.assertEqual(state["hp"], 14)
        self.assertEqual(state["world_entities"][0]["mana"], 5)

    def test_defeat_records_experience_once(self):
        self.hero["world_entities"][0]["hp"] = 1
        state, reply = combat_turn(self.hero, "/attack", "Орен", roller=lambda low, high: high)
        self.assertEqual(state["xp"], 50)
        self.assertIsNone(state["combat"])
        self.assertIn("Бой завершён", reply)
        with self.assertRaisesRegex(ValueError, "недоступен"):
            combat_turn(state, "/attack", "Орен")

    def test_remote_target_cannot_be_attacked(self):
        self.hero["world_entities"][0]["location"] = "Порт"
        with self.assertRaisesRegex(ValueError, "текущей локации"):
            combat_turn(self.hero, "/attack", "Орен")

    def test_profile_does_not_accept_arbitrary_numbers_or_dice(self):
        built = profile({"threat": "minor"}, {
            "defense": 90, "attack": 999, "resistances": {"fire": "immune", "water": "immune"},
            "abilities": [{"name": "Ошибка", "dice": "99d900"}],
        })
        self.assertEqual(built["defense"], 10)
        self.assertEqual(built["attack"], 2)
        self.assertEqual(built["resistances"], {"fire": "immune"})
        self.assertEqual(built["abilities"], [])

    def test_prices_are_location_scoped_and_exchange_conserves_value(self):
        proof = "Паёк стоит три серебряных."
        self.hero["shop_offers"] = merge_offers(self.hero, [
            {"item": "Паёк", "currency": "silver", "price": 3, "evidence": proof},
        ], proof)
        offer, = current_offers(self.hero)
        self.assertEqual(len(current_offers({**self.hero, "scene": {"location": "Порт"}})), 0)
        with self.assertRaisesRegex(ValueError, "Покупка не состоялась"):
            apply_purchase(self.hero, {**offer, "quantity": 1})
        self.assertNotIn("inventory", self.hero)
        apply_exchange(self.hero, {"from": "gold", "to": "silver", "amount": 1})
        apply_purchase(self.hero, {**offer, "quantity": 1})
        self.assertEqual((self.hero["gold"], self.hero["silver"]), (9, 7))
        self.assertEqual(self.hero["inventory"], [{"name": "Паёк", "quantity": 1}])

    def test_dispersing_an_effect_already_expired_this_turn_is_idempotent(self):
        self.hero["active_effects"] = [
            {"id": "trap", "name": "Ловушка", "type": "control", "target": "hero", "expires": 602}
        ]
        phrase = "Действие старой ловушки прошло."
        state = resolve_effects(self.hero, [{"type": "dispel", "effect_id": "trap", "evidence": phrase}], phrase, 602)
        self.assertEqual(state["active_effects"], [])
        self.assertEqual(state["effect_log"], ["Истёк эффект: Ловушка"])

    def test_introduction_preserves_existing_npc_id_and_patches_closed_access(self):
        self.hero["world_entities"] = [
            {"id": "old-npc", "kind": "npc", "name": "Служитель управы с ящиками", "location": "Двор"},
            {"id": "bridge", "kind": "location", "name": "Старый мост", "status": "open",
             "description": "Въезд перекрыт цепью. «Проход закрыт»."},
        ]
        phrase = "Служитель с ящиками откладывает ящик и отвечает:\n\n— Я Орен."
        result = merge_entities(self.hero, [{"name": "Орен", "kind": "npc", "evidence": "Я Орен."}], phrase)
        self.assertEqual(len(result), 2)
        npc, bridge = result
        self.assertEqual(npc["id"], "old-npc")
        self.assertEqual(npc["name"], "Орен")
        self.assertEqual(npc["aliases"], ["Служитель управы с ящиками"])
        self.assertEqual(bridge["access"], "closed")
