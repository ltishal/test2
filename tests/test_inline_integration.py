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
