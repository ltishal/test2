import json
import unittest

from engine.combat import combat_context, combat_status, combat_turn
from engine.world import merge_entities, restore_scene_people


class GroupCombatTest(unittest.TestCase):
    def setUp(self):
        self.state = {
            'name': 'Кочка', 'hp': 30, 'max_hp': 30, 'xp': 0,
            'world_minute': 480, 'scene': {'location': 'Тропа'},
            'stats': {'dex': 16, 'str': 10}, 'weapon_in_hand': 'длинный лук',
            'inventory': [{'id': 'arrows', 'name': 'стрелы', 'quantity': 20}],
            'world_entities': [
                {'id': ident, 'name': name, 'kind': 'creature', 'location': 'Тропа',
                 'hp': hp, 'max_hp': 8, 'threat': 'minor', 'status': status}
                for ident, name, hp, status in [
                    ('cart', 'Гоблин у повозки', 1, 'враждебен'),
                    ('branch', 'Гоблин за ветвями', 8, 'враждебен'),
                    ('forest', 'Гоблин у леса', 8, 'враждебен'),
                    ('owl', 'Сова', 8, 'наблюдает'),
                ]
            ],
        }

    def attack(self, state, target, rolls):
        numbers = iter(rolls)
        return combat_turn(state, '/attack', target, lambda *_: next(numbers))

    def test_switch_targets_keep_initiative_and_fled_outcome(self):
        updated, log = self.attack(self.state, 'cart', [20, 10, 9, 8, 12, 3, 1, 1])
        self.assertEqual(updated['combat']['initiative'], [None, 'cart', 'branch', 'forest'])
        self.assertEqual(updated['combat']['target_id'], 'branch')
        self.assertEqual([row['hp'] for row in updated['world_entities']], [0, 8, 8, 8])
        self.assertIn('другие противники', log)
        self.assertEqual(updated['xp'], 25)
        self.assertEqual(updated['inventory'][0]['quantity'], 19)
        self.assertEqual(updated['world_second'], 6)
        self.assertNotIn('Атака: Сова', log)

        loaded = json.loads(json.dumps(updated))
        next_turn, log = self.attack(loaded, 'forest', [11, 2, 1, 1])
        self.assertEqual(next_turn['combat']['initiative'], loaded['combat']['initiative'])
        self.assertEqual(next_turn['combat']['target_id'], 'forest')
        self.assertEqual(next_turn['combat']['round'], 2)
        self.assertEqual([row['hp'] for row in next_turn['world_entities']], [0, 8, 6, 8])
        self.assertEqual((next_turn['inventory'][0]['quantity'], next_turn['xp']), (18, 25))
        self.assertNotIn('Инициатива', log)
        restore_scene_people(next_turn['world_entities'], next_turn['scene'], 'Третий гоблин ушёл в лес.')
        battle = combat_context(next_turn)
        self.assertEqual(battle['target_id'], 'branch')
        self.assertEqual([e['status'] for e in combat_status(next_turn, battle)], ['defeated', 'active', 'left'])
        with self.assertRaisesRegex(ValueError, 'недоступен'):
            self.attack(next_turn, 'forest', [])
        finished, log = self.attack(next_turn, 'branch', [20, 8])
        self.assertIsNone(finished['combat'])
        self.assertEqual(finished['xp'], 50)
        self.assertIn('Бой завершён', log)

    def test_legacy_duel_keeps_order_and_cooldowns(self):
        self.state['combat'] = {'target_id': 'branch', 'location': 'Тропа', 'round': 2,
                                'hero_first': False, 'npc_cooldowns': {'удар': 5}}
        updated, log = self.attack(self.state, 'branch', [1, 1])
        self.assertEqual(updated['combat']['enemy_cooldowns'], {'branch': {'удар': 5}})
        self.assertEqual(updated['combat']['initiative'], ['branch', None])
        self.assertEqual(updated['combat']['round'], 3)
        self.assertNotIn('Инициатива', log)
        self.assertEqual(updated['inventory'][0]['quantity'], 19)

    def test_escaped_actor_remains_out_until_a_grounded_return(self):
        self.state['world_entities'][2]['status'] = 'скрылся в лесу'
        reply = 'Из леса слышен шум, но героя никто не атакует.'
        change = {'id': 'forest', 'status': 'враждебен', 'evidence': reply}
        updated = merge_entities(self.state, [change], reply)
        self.assertEqual(updated[2]['status'], 'скрылся в лесу')
        reply = 'Гоблин у леса возвращается на дорогу.'
        updated = merge_entities(self.state, [{**change, 'evidence': reply}], reply)
        self.assertEqual((updated[2]['status'], updated[2]['hp']), ('враждебен', 8))

    def test_explicit_targets_only_and_retreat(self):
        updated, _ = self.attack(self.state, 'branch | forest', [20, 9, 8, 1, 1, 1])
        self.assertEqual(updated['combat']['enemy_ids'], ['branch', 'forest'])
        with self.assertRaisesRegex(ValueError, 'участника'):
            self.attack(updated, 'owl', [])
        after, log = combat_turn(updated, '/retreat', '', lambda *_: 20)
        self.assertIsNone(after['combat'])
        self.assertIn('Герой выходит из боя', log)
        self.assertEqual(after['inventory'], updated['inventory'])
