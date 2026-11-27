import unittest

from engine.combat import combat_turn
from engine.economy import current_offers, merge_offers, settle_offer
from engine.world import is_present, restore_scene_people


class Alpha412Test(unittest.TestCase):
    def test_market_recovers_live_replies(self):
        state = {'scene': {'location': 'Тренировочный двор'}, 'silver': 10, 'inventory': []}
        opening = ('Тренировочный двор залит серым утренним светом. Орен выставил на прилавок два '
                   'дорожных пайка и называет цену: 3 серебряных за каждый. У тебя ровно 10 серебряных монет; '
                   'ты пока ничего не покупаешь.')
        state['shop_offers'] = merge_offers(state, None, opening)
        offer, = current_offers(state)
        self.assertEqual((offer['stock'], offer['price']), (2, 3))
        first_id = offer['offer_id']
        price_only = 'Один дорожный паёк стоит 3 серебряных. У тебя по-прежнему **10 серебряных монет**.'
        state['shop_offers'] = merge_offers(state, [], price_only, action='Уточняю цену одного дорожного пайка.')
        offer, = current_offers(state)
        self.assertEqual((offer['stock'], offer['offer_id']), (2, first_id))
        settle_offer(state, offer, 1)
        self.assertEqual(state['silver'], 7)
        sign = ('Орен поворачивает к тебе дощечку с меловой надписью: **«Дорожный паёк — 3 серебряных»**. '
                'После продажи одного из двух у него остался **1 паёк**. Ты ничего не покупаешь.')
        state['shop_offers'] = merge_offers(state, None, sign)
        offer, = current_offers(state)
        self.assertEqual((offer['stock'], offer['price'], offer['offer_id']), (1, 3, first_id))

    def test_price_only_sign_after_named_display(self):
        state = {'scene': {'location': 'Лавка'}}
        reply = ('Дождь стучит по навесу прилавка Орена. Перед тобой лежат два дорожных пайка; '
                 'табличка гласит: **3 серебряных за каждый**. Покупки не было.')
        state['shop_offers'] = merge_offers(state, [], reply)
        offer, = current_offers(state)
        self.assertEqual((offer['stock'], offer['price']), (2, 3))
        unrelated = 'Табличка гласит: 3 серебряных за каждый.'
        self.assertEqual(merge_offers({'scene': state['scene']}, [], unrelated), [])

    def test_requesting_price_does_not_confirm_it_without_a_quote(self):
        state = {'scene': {'location': 'Лавка'}}
        self.assertEqual(merge_offers(state, [], 'Орен просит подождать.',
                                      action='Уточняю цену дорожного пайка.'), [])
        reply = 'Если дорожный паёк стоит 3 серебряных, это могла бы быть хорошая цена.'
        self.assertEqual(merge_offers(state, [], reply, action='Уточняю цену пайка.'), [])

    def test_adjacent_stock_for_a_different_item_does_not_update_quote(self):
        state = {'scene': {'location': 'Лавка'}}
        reply = 'Торговец показывает ценник: «Дорожный паёк — 3 серебряных». На прилавке остаются оба зелья.'
        state['shop_offers'] = merge_offers(state, [], reply)
        offer, = current_offers(state)
        self.assertEqual((offer['stock'], offer['price']), (1, 3))

    def test_both_narrative_and_saved_escape_status_prevent_attack(self):
        scene = {'location': 'Дорога на Фандалин'}
        enemy = {'id': 'goblin', 'kind': 'creature', 'name': 'Гоблин у леса',
                 'location': scene['location'], 'hp': 8, 'max_hp': 8, 'threat': 'minor'}
        state = {'scene': scene, 'hp': 12, 'max_hp': 12,
                 'world_entities': [{**enemy, 'status': 'скрылся в лесу'}]}
        with self.assertRaisesRegex(ValueError, 'недоступен'):
            combat_turn(state, '/attack', 'goblin', lambda *_: 20)
        self.assertTrue(is_present({**enemy, 'status': 'скрылся за повозкой'}))
        entries = [{**enemy, 'status': 'враждебен'}]
        reply = ('Гундрен Роксвик удерживает поводья. Из леса доносится свист. '
                 'Гоблин вздрагивает и скрывается между стволами.')
        restore_scene_people(entries, scene, reply)
        self.assertEqual((entries[0]['status'], entries[0]['hp']), ('сбежал', 8))
        self.assertEqual(entries[1]['name'], 'Гундрен Роксвик')
        with self.assertRaisesRegex(ValueError, 'недоступен'):
            combat_turn({**state, 'world_entities': entries}, '/attack', 'goblin', lambda *_: 20)
