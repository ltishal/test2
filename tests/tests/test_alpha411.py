import unittest

from engine.combat import combat_turn
from engine.economy import confirmed_purchase, current_offers, merge_offers, settle_offer
from engine.world import restore_scene_people


class Alpha411Test(unittest.TestCase):
    def test_legacy_quote_reuses_offer_id_and_corrects_stock(self):
        state = {'scene': {'location': 'Тренировочный двор'}, 'silver': 10, 'inventory': [],
                 'shop_offers': [{'offer_id': 'old-quote', 'location': 'Тренировочный двор',
                                  'item': 'Один дорожный паёк', 'currency': 'silver', 'price': 3, 'stock': 1}]}
        reply = ('Орен указывает на корзину у прилавка: «Один дорожный паёк — 3 серебряных. Осталось два». '
                 'Ты осматриваешь свёртки, но ничего не покупаешь; 10 серебряных монет остаются при тебе.')
        state['shop_offers'] = merge_offers(state, [], reply)
        offer, = current_offers(state)
        self.assertEqual((offer['offer_id'], offer['stock'], state['silver']), ('old-quote', 2, 10))
        settle_offer(state, offer, 1)
        self.assertEqual((current_offers(state)[0]['stock'], state['silver']), (1, 7))

    def test_explicit_stock_overrules_incomplete_model_count(self):
        state = {'scene': {'location': 'Тренировочный двор'}}
        quote = 'Торговец Орен: «Один дорожный паёк — 3 серебряных. Осталось два».'
        suggested = {'item': 'дорожный паёк', 'currency': 'silver', 'price': 3, 'stock': 1, 'evidence': quote}
        state['shop_offers'] = merge_offers(state, [suggested], quote)
        offer, = current_offers(state)
        self.assertEqual(offer['stock'], 2)

    def test_opening_stock_before_price(self):
        state = {'scene': {'location': 'Тренировочный двор'}}
        reply = 'У лавки Орен приветственно кивает: у него есть два дорожных пайка по 3 серебряных каждый.'
        state['shop_offers'] = merge_offers(state, [], reply)
        offer, = current_offers(state)
        self.assertEqual((offer['stock'], offer['price']), (2, 3))

    def test_purchase_accepts_adjacent_receipt_and_rejects_offer(self):
        quote = 'Орен называет цену одного дорожного пайка — 3 серебряных.'
        proposal = {'item': 'дорожный паёк', 'currency': 'silver', 'quantity': 1, 'price': 3, 'evidence': quote}
        self.assertIsNone(confirmed_purchase(proposal, quote))
        self.assertIsNone(confirmed_purchase(proposal, quote + ' Ты ничего не покупаешь.'))
        verified = confirmed_purchase(proposal, quote + ' Ты платишь, и Орен вручает тебе свёрток.')
        self.assertIn('вручает', verified['evidence'])
        self.assertIsNone(confirmed_purchase(proposal, quote + ' Если платишь, Орен отдаёт свёрток.'))

    def test_live_escape_keeps_hp_and_excludes_assault(self):
        scene = {'location': 'Лесная дорога к Фандалину'}
        entries = [
            {'id': 'forest', 'name': 'гоблин у леса', 'kind': 'creature', 'location': scene['location'],
             'hp': 8, 'max_hp': 8, 'status': 'враждебен'},
            {'id': 'cart', 'name': 'гоблин у повозки', 'kind': 'creature', 'location': scene['location'],
             'hp': 0, 'max_hp': 8, 'status': 'побеждён (0 HP)'},
            {'id': 'branch', 'name': 'гоблин у ветвей', 'kind': 'creature', 'location': scene['location'],
             'hp': 0, 'max_hp': 8, 'status': 'побеждён (0 HP)'},
        ]
        restore_scene_people(entries, scene, 'Кажется, гоблин ушёл в лес.')
        self.assertEqual(entries[0]['status'], 'враждебен')
        restore_scene_people(entries, scene, 'Гоблин ушёл в лес; ты его не преследуешь и не атакуешь.')
        self.assertEqual((entries[0]['status'], entries[0]['hp']), ('сбежал', 8))
        self.assertEqual([entry['hp'] for entry in entries], [8, 0, 0])
        state = {'scene': scene, 'world_entities': entries, 'hp': 12, 'max_hp': 12}
        with self.assertRaisesRegex(ValueError, 'недоступен'):
            combat_turn(state, '/attack', 'forest', lambda *args: 20)
