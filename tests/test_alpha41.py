import unittest

from engine.combat import combat_turn
from engine.constructor import learned_spell_from_story
from engine.economy import confirmed_purchase, current_offers, merge_offers, settle_offer
from engine.world import combat_ready, merge_entities, restore_scene_people


class Alpha41Test(unittest.TestCase):
    def test_caravan_opening_recovers_named_npcs_and_distinct_opponents(self):
        reply = (
            'Дворф Гундрен Роксвик едет рядом с повозкой; его спутник Сильдар Халвин держится неподалёку. '
            'Из-за укрытий показываются трое гоблинов с луками. Один уже целится в Гундрена, '
            'другой перекрывает дорогу, а третий пятится к лесу.'
        )
        scene = {'location': 'Трибойрская тропа'}
        entities = restore_scene_people([], scene, reply, hero_name='Кочка')
        self.assertEqual({e['name'] for e in entities if e['kind'] == 'npc'},
                         {'Гундрен Роксвик', 'Сильдар Халвин'})
        creatures = [e for e in entities if e['kind'] == 'creature']
        self.assertEqual(len({e['id'] for e in creatures}), 3)
        self.assertEqual([e['max_hp'] for e in creatures], [8, 8, 8])
        later = 'Гоблин-засадник у повозки падает, второй прячется за ветвями.'
        updated = merge_entities({'scene': scene, 'world_entities': entities}, [
            {'name': 'гоблин-засадник у повозки', 'kind': 'creature', 'location': scene['location'],
             'status': 'побеждён', 'evidence': later}], later)
        self.assertEqual(len(updated), 5)
        by_position = {e['encounter_position']: e for e in updated if e['kind'] == 'creature'}
        self.assertEqual(by_position['повозка']['hp'], 0)
        self.assertEqual(by_position['лес']['hp'], 8)
        self.assertEqual(by_position['лес']['life_state'], 'alive')

    def test_named_creatures_and_legacy_health(self):
        scene = {'location': 'Амбар'}
        entries = restore_scene_people([], scene,
            'В амбаре три глиняных голема: «Глиняный Первый», «Глиняный Второй» и «Глиняный Третий».')
        self.assertEqual([e['hp'] for e in entries], [8, 8, 8])
        legacy = {'id': 'old', 'kind': 'creature', 'hp': 2, 'max_hp': 11, 'threat': 'minor'}
        combat_ready(legacy, 'normal')
        self.assertEqual((legacy['hp'], legacy['max_hp'], legacy['threat']), (2, 11, 'minor'))

    def test_distinct_legacy_npc_by_role_in_traits(self):
        entries = [{'id': 'old', 'kind': 'npc', 'name': 'Орен', 'location': 'Двор',
                    'traits': 'страж у ворот'}]
        reply = 'Торговец Орен предлагает паёк. Орен — страж у ворот.'
        restore_scene_people(entries, {'location': 'Двор'}, reply)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]['traits'], 'страж у ворот')
        self.assertEqual(entries[1]['role'], 'торговец')

    def test_direct_bow_shots_use_dex_and_consume_exactly_one(self):
        state = {'name': 'Мира', 'hp': 12, 'max_hp': 12, 'stats': {'str': 11, 'dex': 16},
                 'weapon_in_hand': 'длинный лук', 'scene': {'location': 'Амбар'},
                 'inventory': [{'id': 'arrows', 'name': 'стрелы', 'quantity': 2}, 'длинный лук'],
                 'world_entities': [
                     {'id': 'clay', 'name': 'Глиняный Первый', 'kind': 'creature', 'hp': 8, 'max_hp': 8,
                      'threat': 'minor', 'location': 'Амбар'}]}
        dice = iter([20, 1, 1, 1])
        missed, report = combat_turn(state, '/attack', 'clay', lambda *_: next(dice))
        self.assertEqual(missed['inventory'][0]['quantity'], 1)
        self.assertIn('1+5 = 6', report)
        self.assertIn('Промах', report)
        dice = iter([20, 3, 1])
        hit, report = combat_turn(missed, '/attack', 'clay', lambda *_: next(dice))
        self.assertEqual(hit['inventory'], ['длинный лук'])
        self.assertEqual(hit['world_entities'][0]['hp'], 5)
        self.assertIn('20+5', report)
        self.assertEqual(state['inventory'][0]['quantity'], 2)
        with self.assertRaisesRegex(ValueError, 'нет боеприпасов'):
            combat_turn(hit, '/attack', 'clay', lambda *_: 10)

    def test_defeated_before_own_turn_does_not_spend_arrow(self):
        state = {'name': 'Мира', 'hp': 1, 'max_hp': 12, 'weapon_in_hand': 'длинный лук',
                 'inventory': ['стрела'], 'scene': {'location': 'Амбар'},
                 'world_entities': [{'id': 'enemy', 'name': 'Голем', 'kind': 'creature', 'hp': 8,
                                     'max_hp': 8, 'threat': 'minor', 'location': 'Амбар'}]}
        dice = iter([1, 20, 20, 4])
        updated, _ = combat_turn(state, '/attack', 'enemy', lambda *_: next(dice))
        self.assertEqual(updated['hp'], 0)
        self.assertEqual(updated['inventory'], ['стрела'])

    def test_stock_in_adjacent_sentence_and_refused_purchase(self):
        state = {'scene': {'location': 'Лавка'}, 'inventory': [], 'silver': 10}
        quote = 'Дорожный паёк стоит 3 серебряных'
        text = f'Орен показывает табличку: {quote}. Торговец подтверждает, что у него есть две штуки.'
        state['shop_offers'] = merge_offers(state, [
            {'item': 'дорожный паёк', 'currency': 'silver', 'price': 3, 'stock': 1, 'evidence': quote}], text)
        offer, = current_offers(state)
        self.assertEqual(offer['stock'], 2)
        settle_offer(state, offer, 2)
        state['shop_offers'] = merge_offers(state, [], f'Торговец повторяет: {quote}.')
        self.assertEqual(current_offers(state), [])
        self.assertIsNone(confirmed_purchase({**offer, 'evidence': quote},
                                             text + ' Ты отказываешься. Покупка не состоялась.'))

    def test_npc_speech_does_not_teach_hero(self):
        reply = '— Я изучил «Луч причала»: 1d4 молнией, стоимость 2 маны, перезарядка — 2 раунда.'
        self.assertEqual(learned_spell_from_story(reply, 'Ника'), [])
        reply = 'Ника завершает урок и изучает «Искру тумана»: 1d4 урона молнией, стоит две маны и перезаряжается за два раунда.'
        spell, = learned_spell_from_story(reply, 'Ника')
        self.assertEqual(spell['mechanics'], {'dice': '1d4', 'element': 'lightning', 'cost': 2, 'cooldown': 2})
