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
