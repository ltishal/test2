import unittest

from engine.constructor import (apply_blueprints, apply_character_blueprints,
                                attach_item_blueprints, attach_learned_spell,
                                learned_spell_from_story, learn_blueprint, recipes)
from engine.combat import hero_ability
from engine.economy import confirmed_purchase, current_offers, merge_offers, settle_offer
from engine.effects import complete_hero_damage, resolve_effects
from engine.world import restore_scene_people


class AlphaFourTest(unittest.TestCase):
    def test_five_blueprint_types_from_one_turn(self):
        state = {'level': 1, 'race': 'Сумерник', 'class': 'Звездочёт', 'stats': {'int': 14, 'con': 12},
                 'inventory': ['Ледяная игла']}
        story = 'Записаны Сумерник, Звездочёт, Искра зари, Рывок тени и Ледяная игла.'
        blueprints = [
            {'kind': 'race', 'name': 'Сумерник', 'mechanics': {'bonuses': {'wis': 2}}},
            {'kind': 'class', 'name': 'Звездочёт', 'mechanics': {'hp_die': 8, 'casting_stat': 'wis', 'bonuses': {'int': 1}}},
            {'kind': 'item', 'name': 'Ледяная игла', 'mechanics': {'slot': 'weapon', 'dice': '1d4', 'element': 'cold', 'stat': 'dex'}},
            {'kind': 'spell', 'name': 'Искра зари', 'mechanics': {'dice': '1d4', 'element': 'fire', 'cost': 2, 'cooldown': 2}},
            {'kind': 'ability', 'name': 'Рывок тени', 'mechanics': {'dice': '1d6', 'element': 'shadow'}},
        ]
        proposals = [{**blueprint, 'evidence': story} for blueprint in blueprints]
        self.assertEqual(len(apply_blueprints(state, proposals, story)), 5)
        original_ids = {record['id'] for record in recipes(state)}
        self.assertEqual(apply_blueprints(state, proposals, story), [])
        self.assertEqual({record['id'] for record in recipes(state)}, original_ids)
        attach_item_blueprints(state)
        apply_character_blueprints(state)
        apply_character_blueprints(state)
        self.assertEqual(state['inventory'][0]['mechanics']['element'], 'cold')
        self.assertEqual((state['stats']['int'], state['stats']['wis']), (15, 12))
        self.assertEqual((state['hp'], state['max_hp'], state['casting_stat']), (9, 9, 'wis'))
        learn_blueprint(state, 'Искра зари')
        self.assertEqual(hero_ability(state, 'Искра зари')['cost'], 2)
        self.assertEqual(hero_ability(state, 'Искра зари')['cooldown'], 2)
        with self.assertRaisesRegex(ValueError, 'уже знает'):
            learn_blueprint(state, 'Искра зари')

    def test_rejected_formula_never_enters_recipe_book(self):
        text = 'Показан метеор.'
        state = {'level': 1}
        rejected = apply_blueprints(state, [{'kind': 'spell', 'name': 'метеор', 'evidence': text,
                                             'mechanics': {'dice': '6d20', 'hits': 4, 'automatic': True}}], text)
        self.assertIn('отклонён', rejected[0])
        self.assertEqual(recipes(state), [])

    def test_observed_learned_spell_has_real_cooldown(self):
        state = {'level': 1, 'abilities': [{'name': 'Искра прилива'}]}
        reply = 'Урок завершён. Ты изучила **«Искру прилива»**: один бросок урона **1d4 молнией**, стоимость — **2 маны**, восстановление — **2 раунда**.'
        spells = learned_spell_from_story(reply)
        self.assertEqual(len(spells), 1)
        apply_blueprints(state, spells, reply)
        attach_learned_spell(state, spells)
        move = hero_ability(state, 'Искра прилива')
        self.assertEqual((move['dice'], move['element'], move['cost'], move['cooldown']), ('1d4', 'lightning', 2, 2))
        self.assertEqual(learned_spell_from_story('Ты не изучила «Искру прилива»: 1d4 молнией, 2 маны.'), [])

    def test_price_from_live_shop_scene_is_persisted_without_model_offer(self):
        state = {'scene': {'location': 'Двор'}, 'inventory': [], 'silver': 10}
        reply = 'У прилавка тебя встречает торговец Орен. Он подтверждает цену на табличке: дорожный паёк стоит 3 серебряных, в наличии 2 штуки. Ты пока ничего не покупаешь.'
        state['shop_offers'] = merge_offers(state, None, reply)
        offer, = current_offers(state)
        self.assertEqual((offer['price'], offer['stock']), (3, 2))
        settle_offer(state, offer, 1)
        state['shop_offers'] = merge_offers(state, None, 'Торговец повторяет: «Дорожный паёк — 3 серебряных».')
        remaining, = current_offers(state)
        self.assertEqual((remaining['stock'], state['silver']), (1, 7))
        self.assertEqual(remaining['offer_id'], offer['offer_id'])

    def test_purchase_confirmation_matches_markup_and_requires_actual_payment(self):
        buy = {'item': 'дорожный паёк', 'price': 3, 'currency': 'silver', 'quantity': 1,
               'evidence': 'incorrect snippet'}
        reply = 'Орен продаёт дорожный паёк за **3 серебряных**. Покупка состоялась.'
        self.assertIsNotNone(confirmed_purchase(buy, reply))
        self.assertIsNone(confirmed_purchase(buy, 'Если Орен продаёт дорожный паёк за 3 серебряных, сделка возможна.'))

    def test_same_name_different_roles_are_distinct(self):
        scene = {'location': 'Двор'}
        report = 'Торговец Орен предлагает паёк. Другой Орен — страж у ворот.'
        entries = restore_scene_people([], scene, report)
        self.assertEqual({entry['role'] for entry in entries}, {'страж', 'торговец'})
        self.assertEqual(len({entry['id'] for entry in entries}), 2)
        self.assertEqual(restore_scene_people(entries, scene, report), entries)

    def test_separately_stated_damage_survives_invalid_delta_evidence(self):
        state = {'name': 'Ива', 'hp': 3, 'max_hp': 7}
        reply = 'Дротик наносит Иве **1 HP урона**. Маятник ударяет Иву и наносит ещё **1 HP урона**.'
        effects = [{'type': 'damage', 'target': 'hero', 'amount': 1, 'evidence': item}
                   for item in ('неверная фраза про дротик', 'неверная фраза про маятник')]
        changes = complete_hero_damage(state, 'Испытываю мягкие ловушки.', reply, effects)
        updated = resolve_effects(state, changes, reply, 600)
        self.assertEqual(updated['hp'], 1)

    def test_evidence_ignores_rendering_markup_but_not_words(self):
        state = {'name': 'Ива', 'hp': 3, 'max_hp': 7}
        reply = 'Дротик наносит Иве **1 HP урона**.'
        effect = {'type': 'damage', 'target': 'hero', 'amount': 1, 'evidence': 'Иве 1 HP урона'}
        self.assertEqual(resolve_effects(state, [effect], reply, 600)['hp'], 2)
        self.assertEqual(resolve_effects(state, [{**effect, 'evidence': 'Иве 2 HP урона'}], reply, 600)['hp'], 3)
