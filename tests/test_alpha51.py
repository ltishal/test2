import unittest

from campaign import check_instructions_in_card
from engine.combat import narrated_ranged_check, narrated_weapon, shot_cover
from engine.economy import confirmed_purchase, current_offers, exchange_from_story, merge_offers, settle_offer
from engine.equipment import item_spec
from engine.world import merge_entities


class Alpha51Test(unittest.TestCase):
    def setUp(self):
        self.state = {'scene': {'location': 'Двор'}, 'stats': {'dex': 16}, 'silver': 10,
                      'inventory': ['длинный лук'],
                      'world_entities': [{'id': 'g', 'kind': 'creature', 'name': 'Гоблин за ветвями',
                                          'location': 'Двор', 'threat': 'minor', 'hp': 8, 'max_hp': 8}]}

    def test_shot_with_explicit_cover_uses_one_dc(self):
        action = 'Беру длинный лук и выпускаю одну стрелу в гоблина за ветвями. Стрелу не подбираю.'
        announce = ('Гоблин за ветвями частично укрыт. Половинное укрытие даёт +2 к защите цели. '
                    'Атака: **DC 14**. Проверка: **/roll 1d20+5, DC 14**. Итог открыт.')
        weapon = narrated_weapon(self.state, action)
        check = narrated_ranged_check(self.state, weapon, action, announce, {'target': 'Гоблин за ветвями'})
        self.assertEqual((check['dc'], check['world_target_id']), (12, 'g'))
        text = check_instructions_in_card(announce, check)
        self.assertIn('DC 12', text)
        self.assertNotIn('DC 14', text)
        self.assertNotIn('/roll', text)
        self.assertEqual(text.count('**'), 2)
        self.assertEqual(shot_cover('Если бы было половинное укрытие, защита цели выросла бы.'), 0)
        self.assertEqual(shot_cover('Ты сам в половинном укрытии. Цель на виду.', 'Гоблин'), 0)

    def test_aim_and_negative_shot_consume_nothing(self):
        for action in ('Не стреляю.', 'Если найду цель, стреляю.', 'Не выпускаю стрелу.', 'Хочу выстрелить, но только целюсь.'):
            with self.subTest(action=action):
                self.assertIsNone(narrated_weapon(self.state, action))

    def test_starting_armor_variant(self):
        self.assertEqual(item_spec({}, 'Кожаный доспех'), item_spec({}, 'кожаная броня'))

    def test_unrated_training_creatures_get_minor_health(self):
        reply = 'Первый и Второй стоят у доски. Оба учебных существа целы.'
        proposals = [{'kind': 'creature', 'name': name, 'description': 'Целое учебное существо-голем.',
                      'evidence': 'Оба учебных существа целы.', 'location': 'Двор'}
                     for name in ('Первый', 'Второй')]
        entities = merge_entities({'scene': {'location': 'Двор'}}, proposals, reply)
        self.assertEqual([(entity['hp'], entity['max_hp']) for entity in entities], [(8, 8), (8, 8)])

    def test_confirmation_needs_a_completed_transfer(self):
        proposal = {'item': 'дорожный паёк', 'quantity': 1, 'price': 5, 'currency': 'silver',
                    'evidence': 'Торговец принимает пять серебряных и вручает дорожный паёк.'}
        self.assertIsNotNone(confirmed_purchase(proposal, proposal['evidence']))
        self.assertIsNone(confirmed_purchase(proposal, 'Если согласишься, торговец вручит паёк.'))
        action = 'Соглашаюсь. Отдаю ровно одну серебряную монету, получаю ровно десять медных.'
        reply = 'Торговец принимает **одну серебряную** и отсчитывает **десять медных**. Размен состоялся.'
        self.assertEqual(exchange_from_story(action, reply), {'from': 'silver', 'to': 'copper', 'amount': 1})
        self.assertIsNone(exchange_from_story(action, 'Торговец готов отсчитать десять медных.'))

    def test_market_replays_two_live_scenes(self):
        samples = (
            ('Торговец при воротах отвечает: «Дорожный паёк — 5 серебряных». Он сверяется с полкой и сообщает: осталось **две штуки**. Ты ничего не покупаешь.', 2, 5),
            ('Торговец Орен поправляет товары на прилавке. Перед ним — два дорожных пайка, по 3 серебряных каждый.', 2, 3),
            ('На табличке у прилавка указано: дорожный паёк — 3 серебряных; в продаже осталось два. Орен подтверждает цену.', 2, 3),
            ('Торговец пересчитывает пайки, сверяясь с полкой за прилавком. Их оказалось три; он показывает деревянную табличку с ценой: 5 серебряных за штуку.', 3, 5),
        )
        for story, count, price in samples:
            with self.subTest(story=story):
                state = {**self.state, 'inventory': []}
                state['shop_offers'] = merge_offers(state, [], story)
                offer, = current_offers(state)
                self.assertEqual((offer['stock'], offer['price']), (count, price))
                settle_offer(state, offer, 1)
                self.assertEqual((current_offers(state)[0]['stock'], state['silver']), (count-1, 10-price))
