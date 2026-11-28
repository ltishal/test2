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

    def test_three_quarters_cover_variants_use_the_same_card_dc(self):
        action = 'Стреляю в гоблина за ветвями.'
        weapon = narrated_weapon(self.state, action)
        phrases = ('Гоблин за ветвями получает три четверти укрытия.',
                   'Гоблин за ветвями находится в трёх четвертях укрытия.',
                   'Гоблин за ветвями в укрытии на три четверти.')
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                reply = phrase + ' Сложность: **DC 10**. Команда: **/roll 1d20+5, DC 10**.'
                check = narrated_ranged_check(self.state, weapon, action, reply, {'target': 'Гоблин за ветвями'})
                self.assertEqual((check['dc'], check['world_target_id']), (15, 'g'))
                visible = check_instructions_in_card(reply, check)
                self.assertIn('DC 15', visible)
                self.assertNotIn('DC 10', visible)
        self.assertEqual(shot_cover('Если Гоблин за ветвями останется в трёх четвертях укрытия.', 'Гоблин за ветвями'), 0)
        self.assertEqual(shot_cover('Гоблин за ветвями не находится в трёх четвертях укрытия.', 'Гоблин за ветвями'), 0)

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

    def test_market_keeps_both_priced_items_from_one_quote(self):
        reply = ('Торговец Орен раскладывает на прилавке товары и говорит: '
                 '«Есть один разводной ключ — за 8 серебряных, и три стопорных '
                 'штифта — по 2 серебряных за штуку». Покупки не происходит.')
        state = {**self.state, 'silver': 4}
        state['shop_offers'] = merge_offers(state, [], reply)
        key_offer, pin_offer = current_offers(state)
        self.assertEqual([(offer['stock'], offer['price']) for offer in (key_offer, pin_offer)],
                         [(1, 8), (3, 2)])
        refresh = ('Орен кивает на прилавок: «Разводной ключ — один в наличии, 8 серебряных за штуку. '
                   'Стопорных штифтов — три, по 2 серебряных за штуку».')
        refreshed = merge_offers(state, [], refresh)
        self.assertEqual([(offer['stock'], offer['price']) for offer in refreshed], [(1, 8), (3, 2)])
        self.assertEqual([offer['offer_id'] for offer in refreshed],
                         [key_offer['offer_id'], pin_offer['offer_id']])
        with self.assertRaisesRegex(ValueError, 'в кошельке'):
            settle_offer(state, key_offer, 1)
        self.assertEqual(state['silver'], 4)
        settle_offer(state, pin_offer, 1)
        self.assertEqual(state['silver'], 2)
        self.assertEqual([offer['stock'] for offer in current_offers(state)], [1, 2])

    def test_named_merchant_can_answer_without_repeating_their_role(self):
        action = ('Возвращаюсь во двор мастерской к торговцу Орену. Покупку пока не совершаю. '
                  'Прошу вслух подтвердить ассортимент одной репликой.')
        reply = ('Орен подтверждает: «Разводной ключ — один в наличии, 8 серебряных за штуку. '
                 'Стопорных штифтов — три, по 2 серебряных за штуку». Покупки не было.')
        revised = ('Орен повторяет: «Разводной ключ — 1 штука, 8 серебряных. '
                   'Стопорных штифтов — 3 штуки, по 2 серебряных за штуку».')
        for quote in (reply, revised):
            with self.subTest(quote=quote):
                offers = merge_offers(self.state, [], quote, action=action)
                self.assertEqual([(offer['stock'], offer['price']) for offer in offers], [(1, 8), (3, 2)])
        bulk = 'Орен подтверждает: «Стопорных штифтов — 3 штуки, 6 серебряных за весь набор».'
        self.assertEqual(merge_offers(self.state, [], bulk, action=action), [])

    def test_market_separates_trailing_stock_from_item_name_and_price(self):
        reply = ('Торговец Орен отвечает: «Разводных ключей два — по 8 серебряных за штуку. '
                 'Стопорных штифтов двенадцать — по 2 медных за штуку».')
        state = {**self.state, 'silver': 10, 'copper': 10, 'inventory': []}
        state['shop_offers'] = merge_offers(state, [], reply)
        keys, pins = current_offers(state)
        self.assertEqual([(o['item'], o['stock'], o['price'], o['currency']) for o in (keys, pins)],
                         [('Разводных ключей', 2, 8, 'silver'), ('Стопорных штифтов', 12, 2, 'copper')])
        refreshed = merge_offers(state, [], 'Торговец Орен уточняет: «Разводной ключ — 2 штуки, '
                                 'по 8 серебряных за штуку. Стопорных штифтов — 12 штук, по 2 медных за штуку».')
        self.assertEqual([o['offer_id'] for o in refreshed], [keys['offer_id'], pins['offer_id']])
        settle_offer(state, pins, 2)
        self.assertEqual(state['copper'], 6)
        self.assertEqual([o['stock'] for o in current_offers(state)], [2, 10])

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

    def test_lens_city_market_pairs_each_price_with_its_displayed_stock(self):
        state = {**self.state, 'silver': 12, 'inventory': []}
        story = ('Торговец Тарен отодвигает край брезента на прилавке, показывая один разводной ключ '
                 'и три стопорных штифта. — Ключ — восемь серебряных. Штифты — по два серебряных за штуку. '
                 'Больше сейчас нет. Ты пока ничего не покупаешь.')
        stale = {'item': 'Штифты', 'currency': 'silver', 'price': 2, 'stock': 1,
                 'evidence': 'Штифты — по два серебряных за штуку'}
        state['shop_offers'] = merge_offers(state, [stale], story)
        offer_by_name = {offer['item']: offer for offer in current_offers(state)}
        self.assertEqual({name: (offer['price'], offer['stock']) for name, offer in offer_by_name.items()},
                         {'разводной ключ': (8, 1), 'Штифты': (2, 3)})
        settle_offer(state, offer_by_name['Штифты'], 1)
        repeated = 'Торговец Тарен отвечает. Разводной ключ ещё один, стоит восемь серебряных.'
        state['shop_offers'] = merge_offers(state, [], repeated)
        stored = {offer['offer_id']: offer['stock'] for offer in current_offers(state)}
        self.assertEqual(stored, {offer_by_name['разводной ключ']['offer_id']: 1,
                                  offer_by_name['Штифты']['offer_id']: 2})
        self.assertEqual(state['silver'], 10)
