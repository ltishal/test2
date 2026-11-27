"""Core route/SQLite integration with a fake model (no external API calls).
If Flask/OpenAI are unavailable, minimal import stubs are used, not HTTP tests.
"""
import importlib.util
import logging
import os
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from engine.turn_protocol import START, END


class InlineIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.stack = ExitStack()
        cls.stack.enter_context(patch.dict(os.environ, {'OPENAI_API_KEY':'test-only','YANDEX_API_KEY':'','SAVES_DIR':cls.temp.name,'INLINE_STATE_ENABLED':'1','OPENAI_BASE_URL':'http://invalid.invalid'}))
        modules={}
        if importlib.util.find_spec('flask') is None:
            fake=types.ModuleType('flask')
            class Flask:
                logger=logging.getLogger('rpg-test')
                def __init__(self,*a): pass
                def add_url_rule(self,*a,**kw): pass
                def route(self,*a,**kw): return lambda f:f
                def errorhandler(self,*a,**kw): return lambda f:f
                def before_request(self,f): return f
            fake.Flask=Flask
            for n in ('request','jsonify','Response','render_template','stream_with_context','current_app'):
                setattr(fake,n,object())
            modules['flask']=fake
        if importlib.util.find_spec('openai') is None:
            fake=types.ModuleType('openai'); fake.OpenAI=lambda **kwargs:object(); modules['openai']=fake
        with patch.dict(sys.modules,modules):
            spec=importlib.util.spec_from_file_location('rpg_test_app',Path(__file__).resolve().parents[1]/'app.py')
            mod=importlib.util.module_from_spec(spec)
            sys.modules[spec.name]=mod
            spec.loader.exec_module(mod)
        cls.mod=mod

    @classmethod
    def tearDownClass(cls):
        cls.mod._SUMMARY_EXECUTOR.shutdown(wait=True)
        cls.stack.close(); cls.temp.cleanup()

    def setUp(self):
        self.m=self.mod; self.owner='owner'; self.party=uuid4().hex; self.turn=uuid4().hex
        self.m.PARTIES.create(self.owner,self.party,{**self.m.DEFAULT_STATE,'name':'Лис','race':'Эльф','class':'Маг','background':'Скиталец'},[])
        self.data={'party_id':self.party,'request_id':self.turn,'revision':0,'action':{'content':'Осматриваю лес.'}}

    def stream(self, wire):
        class FakeStream:
            def __iter__(self):
                for char in wire:
                    yield types.SimpleNamespace(usage=None,choices=[types.SimpleNamespace(finish_reason=None,delta=types.SimpleNamespace(content=char))])
                yield types.SimpleNamespace(usage=None,choices=[types.SimpleNamespace(finish_reason='stop',delta=types.SimpleNamespace(content=None))])
            def close(self): pass
        req=types.SimpleNamespace(get_json=lambda **kw:self.data,headers={},cookies={})
        with patch.multiple(self.m,request=req,_owner=lambda:self.owner,_check_presence=lambda *a,**kw:{'admitted':True},_finish_chat_presence=lambda *a:None,_start_stream=lambda *a,**kw:FakeStream(),Response=lambda value,**kw:value,stream_with_context=lambda value:value):
            return ''.join(self.m.api_chat())

    def commit(self):
        with patch.multiple(self.m,_owner=lambda:self.owner,_snapshot_response=lambda x:x,_schedule_summary=lambda *a,**kw:None):
            return self.m._commit_party_turn(self.data)

    def test_one_call_persists_world_and_retries_do_not_repeat(self):
        story='Перед тобой возникает Зеркальный волк.'
        wire=story+START+'{"world_entities":[{"kind":"creature","name":"Зеркальный волк","threat":"normal","evidence":"'+story+'"}],"scene":{"location":"Лес"}}'+END
        self.assertEqual(self.stream(wire),story+self.m.TURN_COMPLETE)
        with patch.object(self.m,'_measured_completion',side_effect=AssertionError('Second model call not allowed')):
            saved=self.commit(); replay=self.commit()
        self.assertEqual(saved['revision'],1)
        self.assertEqual(saved,replay)
        self.assertEqual(saved['state']['world_entities'][0]['name'],'Зеркальный волк')
        self.assertEqual(saved['messages'][-1]['content'],story)
        self.assertNotIn('_state_delta',saved['messages'][-1])

    def test_broken_metadata_uses_old_extractor(self):
        story='Ты входишь в город.'
        self.assertEqual(self.stream(story+START+'{broken'+END),story+self.m.TURN_COMPLETE)
        result=types.SimpleNamespace(choices=[types.SimpleNamespace(finish_reason='stop',message=types.SimpleNamespace(content='{"scene":{"location":"Город"}}'))])
        with patch.object(self.m,'_measured_completion',return_value=result) as call:
            saved=self.commit()
        self.assertEqual(call.call_count,1)
        self.assertEqual(saved['state']['scene']['location'],'Город')

    def test_inline_signed_npc_damage(self):
        state={**self.m.DEFAULT_STATE,'race':'Эльф','class':'Маг','world_entities':[{'id':'wolf','name':'Волк','kind':'creature','hp':20,'max_hp':20}], 'pending_check':{'id':'check','kind':'damage','expr':'1d8','target':'Волк','world_target_id':'wolf','effect':''}}
        from campaign import roll_result
        roll=roll_result(state['pending_check'],{'expr':'1d8','rolls':[7],'check_id':'check'})
        history=[{'role':'user','content':'[[БРОСОК]] 1d8 = 7.'},{'role':'assistant','content':'Волк отступает.'}]
        with patch.object(self.m,'_measured_completion',side_effect=AssertionError('Unexpected call')):
            updated=self.m.extract_state(history,state,roll and 'test',roll,inline_delta={})
        self.assertEqual(updated['world_entities'][0]['hp'],13)
        self.assertEqual(state['world_entities'][0]['hp'],20)
        self.assertEqual(updated['hp'],state['hp'])
        self.assertIsNone(updated['pending_check'])

    def test_effects_commit_and_retry(self):
        import json
        story='На тебя падает проклятие, лишающее сил.'
        delta={'effects':[{'type':'damage','target':'hero','amount':3,'evidence':story}], 'hp_delta':-3,'hp_event':story}
        self.assertEqual(self.stream(story+START+json.dumps(delta,ensure_ascii=False)+END),story+self.m.TURN_COMPLETE)
        with patch.object(self.m,'_measured_completion',side_effect=AssertionError('Unexpected call')):
            saved=self.commit(); replay=self.commit()
        self.assertEqual(saved['state']['hp'],self.m.DEFAULT_STATE['hp']-3)
        self.assertEqual(saved,replay)
        self.assertIn('HP',saved['messages'][-1]['resolution'])

    def test_effects_clock_and_signed_damage(self):
        state={**self.m.DEFAULT_STATE,'race':'Эльф','class':'Маг','world_minute':100,'hp':10,'max_hp':10,
               'pending_check':{'id':'c','kind':'damage','expr':'1d8','effect':'hero_damage'}}
        from campaign import roll_result
        roll=roll_result(state['pending_check'],{'expr':'1d8','rolls':[4],'check_id':'c'})
        story='Удар наносит четыре единицы урона.'
        history=[{'role':'user','content':'[[БРОСОК]] 1d8 = 4.'},{'role':'assistant','content':story}]
        delta={'effects':[{'type':'damage','target':'hero','amount':4,'evidence':story}]}
        updated=self.m.extract_state(history,state,'test',roll,inline_delta=delta)
        self.assertEqual(updated['hp'],6)
        self.assertIn('не применён',updated['effect_log'][0])

    def test_effect_fields_not_overwritten_by_fallback(self):
        state={**self.m.DEFAULT_STATE,'race':'Эльф','class':'Маг','mana':2,'max_mana':10}
        history=[{'role':'user','content':'Смотрю.'},{'role':'assistant','content':'Лес тих.'}]
        result=types.SimpleNamespace(choices=[types.SimpleNamespace(finish_reason='stop',message=types.SimpleNamespace(content='{"mana":999,"active_effects":[{"id":"bad"}],"hero_form":{"form":"bad"}}'))])
        with patch.object(self.m,'_measured_completion',return_value=result):
            updated=self.m.extract_state(history,state,'test')
        self.assertEqual(updated['mana'],2); self.assertEqual(updated['active_effects'],[]); self.assertNotIn('hero_form',updated)

    def test_rejected_effect_is_not_in_published_story(self):
        import json
        story = 'Мост замерзает.'
        delta = {'effects': [{'type': 'environment', 'target': 'unknown', 'environment': 'лёд', 'evidence': story}]}
        self.stream(story + START + json.dumps(delta, ensure_ascii=False) + END)
        with patch.object(self.m, '_measured_completion', side_effect=AssertionError('Unexpected call')):
            saved = self.commit()
        self.assertNotIn(story, saved['messages'][-1]['content'])
        self.assertIn('Цель не найдена', saved['messages'][-1]['resolution'])

    def test_long_narration_preserves_the_player_action_for_the_clock(self):
        state = {**self.m.DEFAULT_STATE, 'race': 'Эльф', 'class': 'Маг', 'world_minute': 600}
        history = [
            {'role': 'user', 'content': '/wait 10'},
            {'role': 'assistant', 'content': 'Мир продолжается. ' * 180},
        ]
        updated = self.m.extract_state(history, state, inline_delta={})
        self.assertEqual(updated['world_minute'], 610)

    def test_paralysis_blocks_attack_and_expires_during_wait(self):
        state = {**self.m.DEFAULT_STATE, 'race': 'Эльф', 'class': 'Маг', 'world_minute': 600,
                 'active_effects': [{'id': 'hold', 'type': 'control', 'target': 'hero',
                                     'control': 'paralysis', 'expires': 605}]}
        history = [{'role': 'user', 'content': 'Атакую.'}, {'role': 'assistant', 'content': 'Герой атакует.'}]
        with self.assertRaisesRegex(ValueError, 'паралич'):
            self.m.extract_state(history, state, inline_delta={})
        history = [{'role': 'user', 'content': '/wait 5'}, {'role': 'assistant', 'content': 'Проходит пять минут.'}]
        updated = self.m.extract_state(history, state, inline_delta={})
        self.assertEqual(updated['active_effects'], [])

    def test_observed_compound_damage_is_committed_when_inline_mechanics_are_missing(self):
        state = {**self.m.DEFAULT_STATE, 'name': 'Лея', 'race': 'человек', 'class': 'маг',
                 'hp': 7, 'max_hp': 7}
        story = ('Тренировка заканчивается двумя отдельными попаданиями по Лее:\n\n'
                 '- Мягкий дротик попадает на тренировочной отметке: **1 HP урона**.\n'
                 '- Пружинный маятник срабатывает независимо и наносит ещё **2 HP урона**.')
        history = [{'role': 'user', 'content': 'Запускаю два тренажёра.'},
                   {'role': 'assistant', 'content': story}]
        next_state = self.m.extract_state(history, state, inline_delta={})
        self.assertEqual(next_state['hp'], 4)
        self.assertEqual(len(next_state['effect_log']), 2)
        self.assertEqual(self.m.extract_state(history, next_state, inline_delta={})['hp'], 4)
        partial = {'effects': [{'type': 'damage', 'target': 'hero', 'amount': 1,
                               'evidence': 'Мягкий дротик попадает на тренировочной отметке'}]}
        self.assertEqual(self.m.extract_state(history, state, inline_delta=partial)['hp'], 4)
        history[0]['content'] = 'Напомни, сколько тренировочного урона я получила.'
        self.assertEqual(self.m.extract_state(history, state, inline_delta={})['hp'], 7)

    def test_observed_exchange_and_insufficient_funds_without_poisoned_turn(self):
        state = {**self.m.DEFAULT_STATE, 'name': 'Лея', 'race': 'человек', 'class': 'маг',
                 'gold': 10}
        action = 'Соглашаюсь на размен: отдаю ровно одну золотую монету и принимаю ровно десять серебряных.'
        story = 'Геллен принимает одну золотую монету и отсчитывает десять серебряных. Обмен состоялся.'
        history = [{'role': 'user', 'content': action}, {'role': 'assistant', 'content': story}]
        after = self.m.extract_state(history, state, inline_delta={})
        self.assertEqual((after['gold'], after['silver']), (9, 10))
        story = 'Геллен отдал тебе дорожный паёк за три серебряных.'
        history = [{'role': 'user', 'content': 'Покупаю паёк.'}, {'role': 'assistant', 'content': story}]
        delta = {'purchase': {'item': 'паёк', 'quantity': 1, 'currency': 'silver', 'price': 3, 'evidence': story}}
        rejected = self.m.extract_state(history, state, inline_delta=delta)
        self.assertEqual(rejected['inventory'], [])
        self.assertIn('Покупка не состоялась', rejected['effect_log'][-1])
        bought = self.m.extract_state(history, after, inline_delta=delta)
        self.assertEqual((bought['gold'], bought['silver']), (9, 7))

    def test_existing_peaceful_npc_is_bound_to_an_announced_roll_without_dc(self):
        state = {**self.m.DEFAULT_STATE, 'name': 'Лея', 'race': 'человек', 'class': 'маг',
                 'hp': 7, 'max_hp': 7, 'scene': {'location': 'Двор'},
                 'world_entities': [{'id': 'mara', 'name': 'Мара', 'kind': 'npc', 'location': 'Двор'}]}
        story = 'Мара начинает первой. Бросок за Мару: **/roll 1d20+2**. Пока бросок не разрешён, исход удара открыт.'
        history = [{'role': 'user', 'content': 'Начинаю учебный спарринг.'},
                   {'role': 'assistant', 'content': story}]
        updated = self.m.extract_state(history, state, inline_delta={})
        npc, = updated['world_entities']
        self.assertEqual(npc['id'], 'mara')
        self.assertGreater(npc['hp'], 0)
        check = updated['pending_check']
        self.assertEqual((check['actor_id'], check['kind'], check['dc']), ('mara', 'attack', 10))
        from campaign import roll_result, rules_result
        roll = roll_result(check, {'expr': '1d20+2', 'rolls': [12], 'check_id': check['id'], 'npc_attack_damage': 4})
        self.assertTrue(roll['success'])
        self.assertEqual(rules_result(updated, roll)['hp_after'], 7)

    def test_authoritative_http_command_skips_model_and_commits_only_once(self):
        if not hasattr(self.m.app, 'test_client'):
            self.skipTest('Flask is required to test the HTTP interface')
        self.data['action'] = {'content': '/attack Мара'}
        state = {**self.m.DEFAULT_STATE, 'name': 'Лея', 'race': 'человек', 'class': 'маг',
                 'scene': {'location': 'Двор'}, 'world_entities': [
                     {'id': 'mara', 'name': 'Мара', 'kind': 'npc', 'location': 'Двор', 'hp': 12, 'max_hp': 12}
                 ]}
        token = 'A' * 43
        party_id = uuid4().hex
        self.m.PARTIES.create(self.m._owner(token), party_id, state, [])
        self.data['party_id'] = party_id
        with self.m.app.test_client() as http, patch.multiple(
            self.m, _check_presence=lambda *args, **kwargs: {'admitted': True},
            _schedule_summary=lambda *args, **kwargs: None,
        ), patch.object(self.m, '_start_stream', side_effect=AssertionError('Model should not narrate direct action')), patch.object(
            self.m, '_measured_completion', side_effect=AssertionError('Model should not resolve direct action')
        ):
            http.set_cookie(self.m.OWNER_COOKIE, token)
            first = http.post('/api/chat', json=self.data)
            self.assertIn('Инициатива', first.get_data(as_text=True))
            pending = http.get('/api/parties/' + party_id).get_json()
            self.assertNotIn('_engine_state', pending['draft']['reply'])
            committed = http.post('/api/state', json=self.data).get_json()
            replay = http.post('/api/state', json=self.data).get_json()
        self.assertEqual(committed['revision'], 1)
        self.assertEqual(committed, replay)
        self.assertNotIn('_engine_state', committed['messages'][-1])

    def test_alpha41_ranged_hit_and_miss_are_signed_and_spent_once(self):
        import json
        if not hasattr(self.m.app, 'test_client'):
            self.skipTest('Flask is required to test the HTTP interface')
        token = 'R' * 43
        pid = uuid4().hex
        state = {**self.m.DEFAULT_STATE, 'name': 'Торвин', 'race': 'Дворф', 'class': 'Следопыт',
                 'stats': {'str': 11, 'dex': 16}, 'scene': {'location': 'Тропа'},
                 'inventory': ['длинный лук', {'id': 'arrows', 'name': 'стрелы', 'quantity': 20}],
                 'world_entities': [
                     {'id': 'forest', 'kind': 'creature', 'name': 'Лесной гоблин', 'location': 'Тропа',
                      'hp': 8, 'max_hp': 8, 'threat': 'minor'},
                     {'id': 'road', 'kind': 'creature', 'name': 'Дорожный гоблин', 'location': 'Тропа',
                      'hp': 8, 'max_hp': 8, 'threat': 'minor'}]}
        self.m.PARTIES.create(self.m._owner(token), pid, state, [])
        story = 'Атака из длинного лука по лесному гоблину: **/roll 1d20+3**.'
        with self.m.app.test_client() as http, patch.multiple(
            self.m, _check_presence=lambda *a, **kw: {'admitted': True},
            _schedule_summary=lambda *a, **kw: None,
        ):
            http.set_cookie(self.m.OWNER_COOKIE, token)

            def advance(revision, action, narration, changes):
                wire = narration + START + json.dumps(changes, ensure_ascii=False) + END
                body = {'party_id': pid, 'revision': revision, 'request_id': uuid4().hex, 'action': action}
                with patch.object(self.m, '_start_stream', side_effect=lambda *a, **kw: fake_stream(wire)):
                    response = http.post('/api/chat', json=body)
                    self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                    self.assertIn(self.m.TURN_COMPLETE, response.get_data(as_text=True))
                saved = http.post('/api/state', json=body)
                self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
                self.assertEqual(saved.json, http.post('/api/state', json=body).json)
                return saved.json

            def fake_stream(wire):
                class Stream(list):
                    def close(self):
                        pass
                return Stream([types.SimpleNamespace(usage=None, choices=[
                    types.SimpleNamespace(finish_reason='stop', delta=types.SimpleNamespace(content=wire))
                ])])

            move = {'content': 'Беру длинный лук и выпускаю одну обычную стрелу в лесного гоблина.'}
            first = advance(0, move, story, {})
            check = first['state']['pending_check']
            self.assertEqual((check['expr'], check['world_target_id'], check['dc']), ('1d20+5', 'forest', 10))
            self.assertNotIn('/roll', first['messages'][-1]['content'])
            self.assertEqual(first['state']['inventory'][1]['quantity'], 20)
            self.assertEqual(http.post('/api/roll', json={'party_id': pid, 'revision': 1,
                                                         'expr': '1d20+3'}).status_code, 400)
            with patch.object(self.m, 'roll_dice', side_effect=[
                {'expr': '1d20+5', 'rolls': [13]}, {'expr': '1d8', 'total': 4}
            ]):
                rolled = http.post('/api/roll', json={'party_id': pid, 'revision': 1, 'expr': '1d20+5'})
            self.assertTrue(rolled.json['success'])
            hit = 'Стрела попадает в цель. Теперь брось урон: /roll 1d8.'
            second = advance(1, {'content': 'Бросаю.', 'roll_receipt': rolled.json['receipt']}, hit,
                             {'pending_check': {'kind': 'damage', 'target': 'Лесной гоблин'},
                              'inventory_remove': [{'name': 'стрелы', 'quantity': 1}],
                              'resource_events': {'inventory': hit}})
            self.assertEqual(second['state']['inventory'][1]['quantity'], 19)
            self.assertEqual([e['hp'] for e in second['state']['world_entities']], [4, 8])
            self.assertIsNone(second['state']['pending_check'])
            self.assertNotIn('/roll', second['messages'][-1]['content'])
            third = advance(2, move, story, {})
            self.assertEqual(third['state']['inventory'][1]['quantity'], 19)
            with patch.object(self.m, 'roll_dice', return_value={'expr': '1d20+5', 'rolls': [1]}):
                rolled = http.post('/api/roll', json={'party_id': pid, 'revision': 3, 'expr': '1d20+5'})
            self.assertFalse(rolled.json['success'])
            miss = 'Стрела проходит мимо.'
            fourth = advance(3, {'content': 'Бросаю.', 'roll_receipt': rolled.json['receipt']}, miss,
                             {'world_entities': [{'id': 'forest', 'status': 'побеждён', 'evidence': miss}]})
            self.assertEqual(fourth['state']['inventory'][1]['quantity'], 18)
            self.assertEqual([e['hp'] for e in fourth['state']['world_entities']], [4, 8])
            self.assertIn('промах', fourth['messages'][-1]['resolution'])
            self.assertEqual(http.get('/api/parties/' + pid).json['state'], fourth['state'])

    def test_alpha411_miss_at_unregistered_target_spends_arrow(self):
        from campaign import roll_result
        state = {**self.m.DEFAULT_STATE, 'name': 'Кочка', 'race': 'дворф', 'class': 'следопыт',
                 'stats': {'dex': 16}, 'weapon_in_hand': 'длинный лук',
                 'inventory': ['длинный лук', {'id': 'arrows', 'name': 'обычные стрелы', 'quantity': 16}]}
        history = [{'role': 'user', 'content': 'Беру длинный лук и выпускаю одну стрелу в Стального Манекена.'},
                   {'role': 'assistant', 'content': 'Атака длинным луком по Стальному Манекену: DC 16, /roll 1d20+5.'}]
        announced = self.m.extract_state(history, state, inline_delta={
            'pending_check': {'kind': 'attack', 'target': 'Стальной Манекен'}})
        check = announced['pending_check']
        self.assertEqual((check['dc'], check['ammo']), (16, 'arrow'))
        self.assertNotIn('world_target_id', check)
        self.assertEqual(announced['inventory'][1]['quantity'], 16)
        for die, expected_check in ((10, None), (15, 'damage')):
            result = roll_result(check, {'check_id': check['id'], 'expr': '1d20+5', 'rolls': [die]})
            reply = 'Стрела отскакивает от брони.' if die == 10 else 'Стрела попадает. Урон: /roll 1d8.'
            resolved = history + [{'role': 'user', 'content': f'[[БРОСОК]] 1d20+5 = {die + 5}.'},
                                  {'role': 'assistant', 'content': reply}]
            updated = self.m.extract_state(resolved, announced, result=result, inline_delta={
                'pending_check': {'kind': 'damage'}, 'inventory_remove': [{'name': 'обычные стрелы', 'quantity': 1}],
                'resource_events': {'inventory': reply}})
            self.assertEqual(updated['inventory'][1]['quantity'], 15)
            self.assertEqual((updated['pending_check'] or {}).get('kind'), expected_check)

    def test_alpha41_spell_formula_is_not_a_hit_and_completed_rest_moves_clock(self):
        state = {**self.m.DEFAULT_STATE, 'name': 'Ника', 'race': 'Эльф', 'class': 'Маг',
                 'hp': 4, 'max_hp': 7, 'mana': 1, 'max_mana': 6, 'world_minute': 700}
        story = ('Ника завершает урок и изучает «Искру тумана»: заклинание наносит 1d4 урона молнией, '
                 'стоит две маны и перезаряжается за два раунда. Она не применяет его.')
        history = [{'role': 'user', 'content': 'Заканчиваю урок.'},
                   {'role': 'assistant', 'content': story}]
        updated = self.m.extract_state(history, state, inline_delta={'hp_delta': -4, 'hp_event': story})
        self.assertEqual(updated['hp'], 4)
        self.assertEqual(updated['abilities'][0]['mechanics']['cooldown'], 2)
        narration = 'Восемь часов проходят спокойно. Отдых завершён.'
        history = [{'role': 'user', 'content': 'Устраиваю долгий отдых.'},
                   {'role': 'assistant', 'content': narration}]
        for delta in ({}, {'elapsed': {'minutes': 480, 'evidence': narration}}):
            rested = self.m.extract_state(history, updated, inline_delta=delta)
            self.assertEqual((rested['world_minute'], rested['hp'], rested['mana']), (1180, 7, 6))
        downed = self.m.extract_state(history, {**updated, 'hp': 0}, inline_delta={})
        self.assertEqual((downed['world_minute'], downed['hp']), (1180, 0))

    def test_alpha411_narrated_purchase_settles_against_starting_stock(self):
        from engine.economy import current_offers, merge_offers
        state = {**self.m.DEFAULT_STATE, 'name': 'Ника', 'race': 'человек', 'class': 'следопыт',
                 'silver': 10, 'scene': {'location': 'Двор'}}
        state['shop_offers'] = merge_offers(state, [],
            'Торговец Орен: «Дорожный паёк — 3 серебряных. Осталось два».')
        receipt = 'Орен называет цену дорожного пайка — 3 серебряных. Ты платишь и получаешь паёк.'
        remaining = 'Торговец подтверждает: «Дорожный паёк — 3 серебряных. Осталась одна штука».'
        history = [{'role': 'user', 'content': 'Покупаю один паёк.'},
                   {'role': 'assistant', 'content': receipt + ' ' + remaining}]
        delta = {'purchase': {'item': 'дорожного пайка', 'price': 3, 'currency': 'silver',
                              'quantity': 1, 'evidence': receipt}}
        updated = self.m.extract_state(history, state, inline_delta=delta)
        self.assertEqual((updated['silver'], updated['inventory'][0]['quantity']), (7, 1))
        offer, = current_offers(updated)
        self.assertEqual(offer['stock'], 1)
        self.assertEqual(self.m.extract_state(history, updated, inline_delta=delta)['silver'], 7)

    def test_alpha413_counter_stock_and_both_rations_survive_shop_command(self):
        state = {**self.m.DEFAULT_STATE, 'name': 'Ника', 'race': 'человек', 'class': 'маг',
                 'silver': 10, 'scene': {'location': 'Двор'}}
        opening = ('У прилавка Орена лежат два дорожных пайка, каждый обёрнут промасленной тканью; '
                   'сквозь неё проступают очертания чёрного хлеба и солонины. Осмотр не находит ничего '
                   'необычного: товар цел, цена — 3 серебряных за каждый. Ника ничего не покупает.')
        recheck = ('Орен пододвигает к краю прилавка дощечку с выведенной мелом ценой: '
                   '**дорожный паёк — 3 серебряных**. На прилавке остаются оба пайка. Покупки не было.')
        for reply in (opening, recheck):
            with self.subTest(reply=reply):
                history = [{'role': 'user', 'content': 'Уточняю у Орена цену и остаток. Пока ничего не покупаю.'},
                           {'role': 'assistant', 'content': reply}]
                updated = self.m.extract_state(history, state, inline_delta={})
                _, shop = self.m._engine_command(updated, '/shop', None)
                self.assertIn('в наличии: 2', shop)
                bought, _ = self.m._engine_command(updated, '/buy дорожный паёк', None)
                self.assertEqual(bought['silver'], 7)
                _, shop = self.m._engine_command(bought, '/shop', None)
                self.assertIn('в наличии: 1', shop)

    def test_alpha5_group_turn_commits_once_and_switches_after_reload(self):
        from unittest.mock import patch
        if not hasattr(self.m.app, 'test_client'):
            self.skipTest('Flask is required to test the HTTP interface')
        state = {**self.m.DEFAULT_STATE, 'name': 'Кочка', 'race': 'дворф', 'class': 'следопыт',
                 'hp': 40, 'max_hp': 40, 'scene': {'location': 'Тропа'},
                 'weapon_in_hand': 'длинный лук', 'stats': {'dex': 16},
                 'inventory': [{'id': 'arrows', 'name': 'стрелы', 'quantity': 20}],
                 'world_entities': [
                     {'id': ident, 'name': ident, 'kind': 'creature', 'location': 'Тропа',
                      'hp': hp, 'max_hp': 8, 'threat': 'minor', 'status': 'враждебен'}
                     for ident, hp in [('cart', 1), ('branch', 8), ('forest', 8)]
                 ]}
        party_id = uuid4().hex
        with self.m.app.test_client() as client, patch.multiple(
            self.m, _check_presence=lambda *a, **kw: {'admitted': True},
            _schedule_summary=lambda *a, **kw: None,
        ), patch.object(self.m, '_start_stream', side_effect=AssertionError('Model should not roll initiative')):
            token = 'G' * 43
            client.set_cookie(self.m.OWNER_COOKIE, token)
            self.m.PARTIES.create(self.m._owner(token), party_id, state, [])

            def commit(revision, target, numbers):
                payload = {'party_id': party_id, 'request_id': uuid4().hex, 'revision': revision,
                           'action': {'content': '/attack ' + target}}
                with patch('engine.combat.randint', side_effect=numbers):
                    chat = client.post('/api/chat', json=payload)
                    self.assertEqual(chat.status_code, 200, chat.get_data(as_text=True))
                    self.assertIn(self.m.TURN_COMPLETE, chat.get_data(as_text=True))
                committed = client.post('/api/state', json=payload)
                self.assertEqual(committed.status_code, 200, committed.get_data(as_text=True))
                self.assertEqual(committed.json, client.post('/api/state', json=payload).json)
                return committed.json

            first = commit(0, 'cart', [20, 10, 9, 8, 11, 3, 1, 1])
            reloaded = client.get('/api/parties/' + party_id).json
            self.assertEqual(first['state'], reloaded['state'])
            initiative = reloaded['state']['combat']['initiative']
            second = commit(1, 'forest', [11, 2, 1, 1])['state']
            self.assertEqual(second['combat']['initiative'], initiative)
            self.assertEqual((second['combat']['round'], second['inventory'][0]['quantity']), (2, 18))
            self.assertEqual([npc['hp'] for npc in second['world_entities']], [0, 8, 6])
            self.assertEqual(second['xp'], 25)

    def test_alpha51_spoken_formula_is_attached_to_learned_card(self):
        from engine.combat import hero_ability
        state = {**self.m.DEFAULT_STATE, 'name': 'Руна', 'race': 'человек', 'class': 'маг',
                 'hp': 12, 'max_hp': 12}
        reply = ('Лея диктует: «Запиши так: **1d4 урона холодом, одна мана, перезарядка один раунд**». '
                 'Ты переписываешь формулу. Лея проверяет запись: «Теперь ты знаешь “Искру тумана”». '
                 'Урок завершён. Заклинание изучено, но в этом ходу не использовано.')
        history = [{'role': 'user', 'content': 'Завершаю урок.'}, {'role': 'assistant', 'content': reply}]
        updated = self.m.extract_state(history, state, inline_delta={'abilities': [
            {'name': 'Искра тумана', 'description': '1d4 холодом', 'recharge': 'unlimited'}]})
        move = hero_ability(updated, 'Искра тумана')
        self.assertEqual((move['element'], move['cost'], move['cooldown'], updated['hp']), ('cold', 1, 1, 12))
        history[-1]['content'] = reply.replace('Урок завершён. Заклинание изучено', 'Урок не завершён. Заклинание не изучено')
        incomplete = self.m.extract_state(history, state, inline_delta={})
        self.assertEqual(incomplete['abilities'], [])

    def test_alpha51_fire_without_retrieval_and_rest_recap(self):
        state = {**self.m.DEFAULT_STATE, 'name': 'Руна', 'race': 'человек', 'class': 'маг',
                 'hp': 17, 'max_hp': 24, 'inventory': [
                     'длинный лук', {'id': 'arrows', 'name': 'обычные стрелы', 'quantity': 20}]}
        history = [{'role': 'user', 'content': 'Беру длинный лук и выпускаю одну стрелу по мишени. Стрелу не подбираю.'},
                   {'role': 'assistant', 'content': 'Ты выпускаешь стрелу в мишень. Стрела врезается в манекен.'}]
        updated = self.m.extract_state(history, state, inline_delta={})
        self.assertEqual(updated['inventory'][1]['quantity'], 19)
        self.assertEqual(self.m._synchronize_recap('В инвентаре осталось 18 обычных стрел.', updated,
                                                   {'check': {'ammo': 'arrow'}}),
                         'В инвентаре осталось 19 обычных стрел.')
        reply = 'Восемь часов спустя отдых завершён. Текущие HP по листу: **17 из 24**.'
        history[-2]['content'] = 'Устраиваю долгий отдых и сплю восемь часов.'
        history[-1]['content'] = reply
        rested = self.m.extract_state(history, {**updated, 'world_minute': 60}, inline_delta={})
        self.assertEqual((rested['world_minute'], rested['hp']), (540, 24))
        self.assertIn('**24/24**', self.m._synchronize_recap(reply, rested, None))

    def test_alpha51_named_npc_attack_against_armor_binds_and_resolves(self):
        from campaign import roll_result, rules_result
        state = {**self.m.DEFAULT_STATE, 'name': 'Кочка', 'race': 'дворф', 'class': 'следопыт',
                 'hp': 13, 'max_hp': 24, 'armor_equipped': 'кожаный доспех', 'stats': {'dex': 16},
                 'scene': {'location': 'Тропа'}, 'world_entities': [
                     {'id': 'forest', 'name': 'Гоблин у леса', 'kind': 'creature',
                      'location': 'Тропа', 'hp': 8, 'max_hp': 8, 'threat': 'minor'}]}
        history = [{'role': 'user', 'content': 'Говорю с Сильдаром.'},
                   {'role': 'assistant', 'content': 'Гоблин у леса бросается ближе и атакует Кочку. '
                                                     'Бросок атаки: 1d20+2 против КБ 13. Команда: /roll 1d20+2'}]
        next_state = self.m.extract_state(history, state, inline_delta={})
        check = next_state['pending_check']
        self.assertEqual((check['kind'], check['actor_id'], check['dc']), ('attack', 'forest', 14))
        result = roll_result(check, {'expr': '1d20+2', 'rolls': [17], 'check_id': check['id'],
                                     'npc_attack_damage': 3})
        self.assertEqual(rules_result(next_state, result)['hp_after'], 10)

    def test_alpha51_exchange_accepts_formatted_settlement_and_empty_shop(self):
        state = {**self.m.DEFAULT_STATE, 'name': 'Кочка', 'race': 'дворф', 'class': 'следопыт',
                 'silver': 5, 'scene': {'location': 'Застава'}, 'shop_offers': [
                     {'offer_id': 'ration', 'location': 'Застава', 'item': 'паёк', 'stock': 0,
                      'currency': 'silver', 'price': 5}
                 ]}
        direct, _ = self.m._engine_command(state, '/exchange silver 1 copper', None)
        self.assertEqual((direct['silver'], direct['copper']), (4, 10))
        action = 'Соглашаюсь. Отдаю ровно одну серебряную монету, получаю ровно десять медных.'
        reply = 'Торговец принимает **одну серебряную** и отсчитывает **десять медных**. Размен состоялся.'
        history = [{'role': 'user', 'content': action}, {'role': 'assistant', 'content': reply}]
        for evidence in ('Торговец принимает одну серебряную и отсчитывает десять медных.', 'Размен завершён.'):
            with self.subTest(evidence=evidence):
                updated = self.m.extract_state(history, state, inline_delta={
                    'exchange': {'from': 'silver', 'to': 'copper', 'amount': 1, 'evidence': evidence}})
                self.assertEqual((updated['silver'], updated['copper']), (4, 10))
        history[-1]['content'] = 'Торговец готов отсчитать десять медных.'
        self.assertEqual(self.m.extract_state(history, state, inline_delta={})['silver'], 5)

    def test_alpha51_sealed_letter_survives_handout_and_goes_to_named_guard(self):
        state = {**self.m.DEFAULT_STATE, 'name': 'Ива', 'race': 'человек', 'class': 'маг',
                 'scene': {'location': 'Портовый архив'}, 'inventory': []}
        opening = 'Страж Орен несёт караул; торговец Орен держит прилавок. Архивистка Дара хранит письмо.'
        state = self.m.extract_state([{'role': 'user', 'content': 'Осматриваюсь.'},
                                     {'role': 'assistant', 'content': opening}], state, inline_delta={})
        self.assertEqual(len([npc for npc in state['world_entities'] if npc['name'] == 'Орен']), 2)
        self.assertIn('Дара', [npc['name'] for npc in state['world_entities']])
        action = 'Прошу дать запечатанное письмо. Принимаю его, если Дара согласна.'
        reply = 'Затем протягивает запечатанный конверт. Печать цела; теперь письмо у тебя.'
        history = [{'role': 'user', 'content': action}, {'role': 'assistant', 'content': reply}]
        received = self.m.extract_state(history, state, inline_delta={})
        self.assertEqual(len(received['inventory']), 1)
        self.assertEqual(self.m.extract_state(history, received, inline_delta={})['inventory'], received['inventory'])
        history = [{'role': 'user', 'content': 'Передаю стражу запечатанное письмо от Дары.'},
                   {'role': 'assistant', 'content': 'Страж Орен принимает запечатанный конверт и проверяет печать.'}]
        delivered = self.m.extract_state(history, received, inline_delta={})
        self.assertEqual(delivered['inventory'], [])
        self.assertIn('Страж Орен', [fact.get('holder') for fact in delivered['facts']])
        history[-1]['content'] = 'Страж Орен отказывается, письмо остаётся у тебя.'
        self.assertEqual(self.m.extract_state(history, received, inline_delta={})['inventory'], received['inventory'])

    def test_save_name_preserves_cyrillic_and_never_includes_path_separators(self):
        self.assertEqual(self.m._safe_name('Тест Alpha 2.1 — Лея'), 'Тест Alpha 2.1 — Лея')
        self.assertEqual(self.m._safe_name('../..'), '')
        self.assertLessEqual(len(self.m._safe_name('я' * 300).encode()), 128)

    def test_signed_npc_attack_damage_survives_http_roll_and_turn_commit(self):
        if not hasattr(self.m.app, 'test_client'):
            self.skipTest('Flask is required to test the HTTP interface')
        token = 'B' * 43
        owner = self.m._owner(token)
        check = {'id': 'bound-attack', 'actor_id': 'wolf', 'kind': 'attack', 'expr': '1d20+3', 'dc': 10}
        state = {**self.m.DEFAULT_STATE, 'name': 'Лея', 'race': 'человек', 'class': 'маг',
                 'hp': 7, 'max_hp': 7, 'pending_check': check,
                 'world_entities': [{'id': 'wolf', 'name': 'Волк', 'kind': 'creature',
                                     'hp': 10, 'max_hp': 10, 'threat': 'minor'}]}
        party_id = uuid4().hex
        self.data['party_id'] = party_id
        self.m.PARTIES.create(owner, party_id, state, [])
        with self.m.app.test_client() as http, patch.multiple(
            self.m, _check_presence=lambda *args, **kwargs: {'admitted': True},
            _schedule_summary=lambda *args, **kwargs: None,
        ), patch.object(self.m.random, 'randint', side_effect=[14, 3]):
            http.set_cookie(self.m.OWNER_COOKIE, token)
            result = http.post('/api/roll', json={**self.data, 'expr': check['expr']}).get_json()
            self.assertEqual(result['npc_attack_damage'], 3)
            action = {'role': 'user', 'content': 'Связанный бросок.', 'roll_receipt': result['receipt']}
            verified = self.m._signed_roll(action, state, [], party={'id': party_id, 'revision': 0})
            self.m.PARTIES.stage(owner, party_id, 0, self.turn, {**action, 'roll': verified}, {
                'role': 'assistant', 'content': 'Волк успевает вцепиться в рукав.', '_state_delta': {},
            })
            saved = http.post('/api/state', json=self.data).get_json()
        self.assertIsNone(saved['state']['pending_check'])
        self.assertEqual(saved['state']['hp'], 4)

    def test_alpha31_direct_equipment_clock_and_legacy_inventory(self):
        state={**self.m.DEFAULT_STATE,'race':'Эльф','class':'Маг','name':'Лис','world_minute':600,'world_second':54,
               'scene':{'location':'Лес'},'inventory':[{'id':'blade','name':'Ледяная игла','mechanics':{'slot':'weapon','dice':'2d6+1','element':'cold','stat':'dex'}}]}
        state['active_effects']=[{'id':'p','type':'schedule','target':'hero','name':'Яд','operation':'damage','amount':1,'interval':1,'next_tick':601,'remaining':1}]
        updated,_=self.m._engine_command(state,'/equip blade',None)
        self.assertEqual(updated['weapon_in_hand'],'Ледяная игла')
        self.assertEqual(updated['weapon_equipped_id'],'blade')
        self.assertEqual((updated['world_minute'],updated['world_second']),(601,0))
        self.assertEqual(updated['hp'],state['hp']-1)
        self.assertEqual(updated['active_effects'],[])
        view,_=self.m._engine_command(updated,'/wallet',None)
        self.assertEqual(view['world_minute'],601)
        self.assertEqual(view['world_second'],0)

    def test_alpha31_model_receives_resources_and_cooldown(self):
        state={**self.m.DEFAULT_STATE,'name':'Лис','race':'Эльф','class':'Маг','mana':4,'max_mana':12,
               'combat_rounds':8,'ability_cooldowns':{'искры':11},'world_minute':600,'world_second':42}
        self.party=uuid4().hex; self.data['party_id']=self.party
        self.m.PARTIES.create(self.owner,self.party,state,[])
        captured=[]; original=self.m._messages_with_memory
        def capture(messages,*args,**kwargs):
            captured.extend(messages)
            return original(messages,*args,**kwargs)
        with patch.object(self.m,'_messages_with_memory',side_effect=capture):
            self.stream('Лес тих.'+START+'{}'+END)
        joined='\n'.join(m['content'] for m in captured)
        self.assertIn('"mana": 4',joined)
        self.assertIn('"max_mana": 12',joined)
        self.assertIn('"искры": 2',joined)
        self.assertIn('"second": 42',joined)

    def test_alpha31_diag_is_closed_and_get_never_pings(self):
        req=types.SimpleNamespace(authorization=None,method='GET')
        with patch.dict(os.environ,{'TOKEN_ADMIN_PASSWORD':''}),patch.multiple(self.m,request=req,Response=lambda *args:args):
            self.assertEqual(self.m.api_diag()[1],404)
        with patch.dict(os.environ,{'TOKEN_ADMIN_PASSWORD':'secret'}),patch.multiple(self.m,request=req,Response=lambda *args:args):
            self.assertEqual(self.m.api_diag()[1],401)
        req.authorization=types.SimpleNamespace(type='basic',username='admin',password='secret')
        with patch.dict(os.environ,{'TOKEN_ADMIN_PASSWORD':'secret'}),patch.multiple(self.m,request=req,jsonify=lambda x:x),patch.object(self.m,'_paid_completion',side_effect=AssertionError('GET must not generate')):
            self.assertIn('model',self.m.api_diag())

    def test_alpha31_legacy_paid_endpoints_default_denied(self):
        req=types.SimpleNamespace(get_json=lambda **kw:{'messages':[{'role':'user','content':'test'}]},cookies={},headers={})
        with patch.multiple(self.m,request=req,jsonify=lambda x:x,ALLOW_LEGACY_LLM=False,_owner=lambda:'owner'):
            self.assertEqual(self.m.api_chat()[1],403)
            self.assertEqual(self.m.api_state()[1],403)

    def test_alpha31_budget_precedes_provider_and_kill_switch(self):
        from engine.api_budget import ApiBudget, BudgetExceeded
        fake=types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=lambda **kw:'ok')))
        with tempfile.TemporaryDirectory() as path:
            guard=ApiBudget(Path(path)/'budget.sqlite3',global_calls=1,owner_calls=1,global_units=100)
            with patch.multiple(self.m,API_BUDGET=guard,client=fake,_owner=lambda:'owner'),patch.dict(os.environ,{'API_ENABLED':'1'}):
                self.assertEqual(self.m._paid_completion(messages=[{'content':'x'}],max_tokens=10),'ok')
                with self.assertRaises(BudgetExceeded): self.m._paid_completion(messages=[{'content':'x'}],max_tokens=10)
            with patch.multiple(self.m,client=fake),patch.dict(os.environ,{'API_ENABLED':'0'}):
                with self.assertRaisesRegex(BudgetExceeded,'отключена'): self.m._paid_completion(messages=[],max_tokens=1)

    def test_confirmed_long_rest_restores_health_and_keeps_new_damage(self):
        state = {**self.m.DEFAULT_STATE, 'name': 'Ива', 'race': 'человек', 'class': 'маг',
                 'hp': 3, 'max_hp': 7, 'mana': 10, 'max_mana': 12, 'world_minute': 1088}
        finished = 'Долгий отдых завершён.'
        incident = 'Дротик наносит Иве 1 HP урона.'
        reply = finished + ' ' + incident
        history = [{'role': 'user', 'content': 'Отдыхаю.'}, {'role': 'assistant', 'content': reply}]
        changes = {'rest_event': {'kind': 'long_rest', 'evidence': finished},
                   'effects': [{'type': 'damage', 'target': 'hero', 'amount': 1, 'evidence': incident}]}
        updated = self.m.extract_state(history, state, inline_delta=changes)
        self.assertEqual((updated['hp'], updated['mana']), (6, 12))

    def test_custom_recipe_registered_and_learned_by_server_command(self):
        state = {**self.m.DEFAULT_STATE, 'name': 'Ива', 'race': 'человек', 'class': 'маг', 'world_minute': 1088}
        reply = 'Тренер записал проект «Рывок тени».'
        item = {'kind': 'ability', 'name': 'Рывок тени', 'evidence': reply,
                'mechanics': {'dice': '1d4', 'element': 'shadow', 'cost': 2, 'cooldown': 2}}
        history = [{'role': 'user', 'content': 'Разрабатываю способность.'}, {'role': 'assistant', 'content': reply}]
        catalog = self.m.extract_state(history, state, inline_delta={'blueprints': [item]})
        self.assertIn('Рывок тени', self.m._engine_command(catalog, '/design', None)[1])
        learned, response = self.m._engine_command(catalog, '/learn Рывок тени', None)
        self.assertIn('Изучено', response)
        self.assertEqual(learned['world_minute'], 1098)
        self.assertEqual(learned['abilities'][0]['mechanics']['cooldown'], 2)
        self.assertEqual(catalog['abilities'], [])

    def test_memory_distinguishes_intent_from_committed_trade(self):
        dialogue = self.m._dialogue_messages([
            {'role': 'user', 'content': 'Покупаю паёк.'},
            {'role': 'assistant', 'content': 'Сцена продолжается.', 'resolution': 'Отклонено: покупка не состоялась.'},
        ])
        self.assertIn('[Итог движка] Отклонено', dialogue[1]['content'])
