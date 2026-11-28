"""Core route/SQLite integration with a fake model (no external API calls).
If Flask/OpenAI are unavailable, minimal import stubs are used, not HTTP tests.
"""
import importlib.util
import json
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

    def stream(self, wire, *, attempts=None):
        class FakeStream:
            def __init__(self, text, finish):
                self.text, self.finish = text, finish

            def __iter__(self):
                for char in self.text:
                    yield types.SimpleNamespace(usage=None, choices=[types.SimpleNamespace(
                        finish_reason=None, delta=types.SimpleNamespace(content=char))])
                if isinstance(self.finish, Exception):
                    raise self.finish
                yield types.SimpleNamespace(usage=None, choices=[types.SimpleNamespace(
                    finish_reason=self.finish, delta=types.SimpleNamespace(content=None))])

            def close(self):
                pass

        streams = iter(FakeStream(*attempt) for attempt in (attempts or [(wire, 'stop')]))
        req = types.SimpleNamespace(get_json=lambda **kw: self.data, headers={}, cookies={})
        with patch.multiple(self.m, request=req, _owner=lambda: self.owner,
                            _check_presence=lambda *a, **kw: {'admitted': True},
                            _finish_chat_presence=lambda *a: None, _start_stream=lambda *a, **kw: next(streams),
                            Response=lambda value, **kw: value, stream_with_context=lambda value: value):
            answer = self.m.api_chat()
            return answer if isinstance(answer, str) else ''.join(answer)

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

    def test_interrupted_quest_turn_retries_safely_and_pays_once(self):
        quest = {'id': 'lens', 'name': 'Линза маяка', 'status': 'active',
                 'stages': [{'text': 'Вернуться с отметкой', 'done': False}],
                 'rewards': {'xp': 40, 'gold': 7}}
        state = {**self.m.DEFAULT_STATE, 'name': 'Эльза', 'race': 'человек', 'class': 'воин',
                 'scene': {'location': 'Маяк'}, 'gold': 2, 'quests': [quest]}
        opening = [{'role': 'assistant', 'content': 'Нора вручает карточку с оттиском белой цапли.'}]
        self.party = uuid4().hex
        self.m.PARTIES.create(self.owner, self.party, state, opening)
        self.data.update(party_id=self.party, action={
            'content': 'Возвращаюсь с отметкой Норы и сдаю «Линзу маяка».'})
        partial = 'Мирон сверяет оттиск и протягивает'
        broken = self.stream('', attempts=[(partial, 'length'), (partial, ConnectionError('lost'))])
        self.assertNotIn(partial, broken)
        self.assertNotIn(self.m.TURN_COMPLETE, broken)
        snapshot = self.m.PARTIES.get(self.owner, self.party)
        self.assertEqual((snapshot['revision'], snapshot['state']['gold'], snapshot['messages']), (0, 2, opening))

        story = 'Мирон сверяет оттиск белой цапли и подтверждает сдачу квеста.\n+40 XP — квест «Линза маяка».'
        completed = {**quest, 'status': 'completed',
                     'stages': [{'text': 'Вернуться с отметкой', 'done': True}]}
        wire = story + START + json.dumps({'quests': [completed]}, ensure_ascii=False) + END
        self.assertEqual(self.stream(wire, attempts=[(partial, 'length'), (wire, 'stop')]),
                         story + self.m.TURN_COMPLETE)
        self.assertEqual(self.stream(''), story + self.m.TURN_COMPLETE)
        saved = self.commit()
        self.assertEqual((saved['revision'], saved['state']['gold'], saved['state']['xp']), (1, 9, 40))
        self.assertEqual(saved['messages'][0], opening[0])
        self.assertEqual(saved['messages'][-1]['content'], story)
        self.assertEqual(self.commit(), saved)

    def test_declined_weapon_blueprint_and_receipt_survive_equipping_and_reload(self):
        story = 'Мирон вручает тебе «Кедровый клинок». Механику «Кедрового клинка» подтвердили.'
        mechanics = {'slot': 'weapon', 'dice': '1d6', 'element': 'physical', 'stat': 'dex'}
        recipe = {'kind': 'item', 'name': 'Кедровый клинок', 'mechanics': mechanics,
                  'evidence': 'Механику «Кедрового клинка» подтвердили.'}
        self.assertEqual(self.stream(story + START + json.dumps({'blueprints': [recipe]}, ensure_ascii=False) + END),
                         story + self.m.TURN_COMPLETE)
        first = self.commit()
        self.assertEqual(self.commit(), first)
        current = self.m.PARTIES.get(self.owner, self.party)['state']
        project, = current['constructor']
        item, = current['inventory']
        self.assertEqual((item['recipe_id'], item['mechanics'], item['quantity']), (project['id'], mechanics, 1))
        equipped, reply = self.m._engine_command(current, '/equip Кедровый клинок', None)
        self.assertIn('Экипировано', reply)
        from engine.equipment import equipped as equipped_item
        self.assertEqual(equipped_item(equipped, 'weapon'), mechanics)
        self.assertIn('Кедровый клинок', self.m._engine_command(current, '/design', None)[1])
        from engine.combat import combat_turn
        equipped['scene'] = {'location': 'Полигон'}
        equipped['world_entities'] = [{'id': 'ash', 'kind': 'creature', 'name': 'Ясень',
                                       'location': 'Полигон', 'hp': 8, 'max_hp': 8, 'threat': 'minor'}]
        with patch('engine.combat.randint', return_value=5):
            _, attack = combat_turn(equipped, '/attack', 'Ясень', roller=lambda low, high: min(19, high))
        self.assertIn('кедровый клинок', attack.casefold())
        earlier = {'role': 'user', 'content': 'Проверяю проект.'}
        for reply, expected in (
            ('Мирон не вручает тебе «Кедровый клинок».', []),
            ('Мирон готов вручить тебе «Кедровый клинок».', []),
            ('Мирон уже вручает тебе «Кедровый клинок».', []),
        ):
            with self.subTest(reply=reply):
                state = {**current, 'inventory': []}
                updated = self.m.extract_state([earlier, {'role': 'assistant', 'content': reply}],
                                              state, inline_delta={})
                self.assertEqual(updated['inventory'], expected)
        grant = 'Мирон вручает тебе «Янтарный кинжал».'
        delta = {'world_entities': [{'kind': 'item', 'name': 'Янтарный кинжал',
                                     'mechanics': mechanics, 'evidence': grant}]}
        updated = self.m.extract_state([earlier, {'role': 'assistant', 'content': grant}],
                                      current, inline_delta=delta)
        self.assertEqual([p['name'] for p in updated['constructor']], ['Кедровый клинок', 'Янтарный кинжал'])
        self.assertEqual(updated['inventory'][-1]['mechanics'], mechanics)
        text = 'Мирон вручает тебе «Медный тесак»: 1d6 физического урона, Ловкость.'
        latest = self.m.extract_state([earlier, {'role': 'assistant', 'content': text}],
                                     current, inline_delta={})
        self.assertEqual(latest['inventory'][-1]['mechanics'], mechanics)
        self.assertEqual(latest['constructor'][-1]['name'], 'Медный тесак')

    def test_weapon_receipt_with_missing_inline_details_is_reconciled_before_commit(self):
        story = 'Мирон вручает тебе «Кедровый клинок» — лёгкое оружие с коротким лезвием.'
        wire = story + START + '{}' + END
        self.assertEqual(self.stream(wire), story + self.m.TURN_COMPLETE)
        mechanics = {'slot': 'weapon', 'dice': '1d6', 'element': 'physical', 'stat': 'dex'}
        delta = {'world_entities': [{'kind': 'item', 'name': 'Кедровый клинок',
                                     'mechanics': mechanics, 'evidence': story}]}
        response = types.SimpleNamespace(choices=[types.SimpleNamespace(
            finish_reason='stop', message=types.SimpleNamespace(content=json.dumps(delta)))])
        with patch.object(self.m, '_measured_completion', return_value=response) as extract:
            saved = self.commit()
            self.assertEqual(saved, self.commit())
        extract.assert_called_once()
        item, = saved['state']['inventory']
        project, = saved['state']['constructor']
        self.assertEqual((item['recipe_id'], item['mechanics']), (project['id'], mechanics))

    def test_two_mirons_keep_their_own_meeting_places_and_ids_after_reload(self):
        workshop = 'Мастерская Кедровой Гавани'
        gates = 'Ворота Кедровой Гавани'
        story = ('У ворот тебя встречает страж Мирон; он остаётся на посту и указывает дорогу к мастерской. '
                 'Там ты знакомишься с другим Мироном — оружейником, который занят работой у наковальни.')
        delta = {'scene': {'location': workshop}, 'world_entities': [
            {'name': 'Мирон', 'kind': 'npc', 'role': 'страж', 'location': workshop,
             'evidence': 'У ворот тебя встречает страж Мирон'},
        ]}
        self.stream(story + START + json.dumps(delta, ensure_ascii=False) + END)
        first = self.commit()
        world = {entry['role']: entry for entry in first['state']['world_entities']}
        self.assertEqual({role: entry['location'] for role, entry in world.items()},
                         {'страж': gates, 'оружейник': workshop})
        self.assertEqual(len({entry['id'] for entry in world.values()}), 2)
        fresh_story = 'У ворот Кедровой Гавани на посту стоит страж Мирон. Оружейник остаётся в мастерской.'
        self.data.update(revision=1, request_id=uuid4().hex, action={'content': 'Иду к воротам.'})
        self.stream(fresh_story + START + json.dumps({'scene': {'location': gates}}, ensure_ascii=False) + END)
        self.commit()
        saved = self.m.PARTIES.get(self.owner, self.party)
        self.assertCountEqual([(e['id'], e['role'], e['location']) for e in saved['state']['world_entities']],
                              [(e['id'], e['role'], e['location']) for e in world.values()])

    def test_live_miron_replies_keep_two_records_across_scenes(self):
        workshop, gates = 'Мастерская Кедровой Гавани', 'Ворота Кедровой Гавани'
        opening = ('У ворот тебя останавливает страж по имени Мирон — синий плащ застёгнут у горла.\n'
                   'В Мастерской Кедровой Гавани другой Мирон — оружейник в потемневшем кожаном фартуке.')
        exchanges = [
            (workshop, opening),
            (workshop, 'В мастерской Мирон-оружейник сверяет отметку с записью о линзе, которую приняла Нора.'),
            (gates, 'У ворот Мирон-страж отвечает, что ночью видел незнакомца у северной дороги.'),
            (gates, 'У караульного поста стоит Мирон — тот самый страж, который проверял твои бумаги.'),
        ]
        original = None
        for revision, (location, story) in enumerate(exchanges):
            self.data.update(revision=revision, request_id=uuid4().hex)
            self.stream(story + START + json.dumps({'scene': {'location': location}}, ensure_ascii=False) + END)
            saved = self.commit()
            people = saved['state']['world_entities']
            self.assertCountEqual([(npc['name'], npc['role'], npc['location']) for npc in people],
                                  [('Мирон', 'страж', gates), ('Мирон', 'оружейник', workshop)])
            ids = {npc['role']: npc['id'] for npc in people}
            self.assertNotEqual(ids['страж'], ids['оружейник'])
            if original is None:
                original = ids
            self.assertEqual(ids, original)
        self.assertEqual(saved, self.m.PARTIES.get(self.owner, self.party))

    def test_live_weapon_handoff_without_quotes_saves_validated_project(self):
        story = ('Мирон снимает Кедровый клинок с верстака и вкладывает его тебе в ладонь. '
                 'Рукоять гладкая, тёплая от мастерской.\n\n'
                 'Правила клинка подтверждены: простое лёгкое оружие ближнего боя; '
                 'при атаке использует Ловкость и наносит 1d6 физического урона. '
                 'Цена — 0: Мирон вручает его как подарок.')
        self.stream(story + START + '{}' + END)
        with patch.object(self.m, '_measured_completion', side_effect=AssertionError('Unexpected extraction')):
            self.commit()
        state = self.m.PARTIES.get(self.owner, self.party)['state']
        project, = state['constructor']
        item, = state['inventory']
        self.assertEqual(item['recipe_id'], project['id'])
        self.assertEqual(item['mechanics'], {'slot': 'weapon', 'dice': '1d6', 'element': 'physical', 'stat': 'dex'})
        self.assertIn('"dice": "1d6"', self.m._engine_command(state, '/design Кедровый клинок', None)[1])
        equipped, _ = self.m._engine_command(state, '/equip Кедровый клинок', None)
        self.assertEqual(equipped['weapon_in_hand'], 'Кедровый клинок')
        before = self.m.DEFAULT_STATE | {'name': 'Вера', 'race': 'человек', 'class': 'следопыт'}
        hypothetical = 'Если Мирон снимает Кедровый клинок с верстака и вкладывает его тебе в ладонь. ' + story.split('\n\n')[1]
        result = self.m.extract_state([{'role': 'assistant', 'content': hypothetical}], before, inline_delta={})
        self.assertEqual(result['inventory'], [])
        self.assertEqual(result['constructor'], [])

    def test_live_lesson_saves_thunder_spell_and_uses_wisdom_in_combat(self):
        story = ('Ты выполняешь упражнение. Мирон кивает и подтверждает, что приём освоен.\n\n'
                 '— Точные правила: «Отзвук цапли» — магическая атака действием по одной цели. '
                 'Проверка атаки использует Мудрость; при попадании цель получает 1d6 громового урона. '
                 'Стоимость — 1 мана.\n\nМирон не просит повторять упражнение: приём изучен, но ману ты не тратила.')
        self.stream(story + START + '{}' + END)
        self.commit()
        state = self.m.PARTIES.get(self.owner, self.party)['state']
        ability, = state['abilities']
        project, = state['constructor']
        self.assertEqual(ability['recipe_id'], project['id'])
        self.assertEqual({key: ability['mechanics'][key] for key in ('dice', 'stat', 'element', 'cost')},
                         {'dice': '1d6', 'stat': 'wis', 'element': 'thunder', 'cost': 1})
        self.assertIn('Отзвук цапли', self.m._engine_command(state, '/design Отзвук цапли', None)[1])
        with self.assertRaisesRegex(ValueError, 'уже знает'):
            self.m._engine_command(state, '/learn Отзвук цапли', None)
        state.update(stats={'int': 10, 'wis': 16}, mana=6, max_mana=6, scene={'location': 'Двор'},
                     world_entities=[{'id': 'ash', 'name': 'Ясень', 'kind': 'creature', 'location': 'Двор',
                                      'hp': 8, 'max_hp': 8, 'threat': 'minor'}])
        rolls = iter((20, 2, 7, 4, 1))
        fought, reply = self.m.combat_turn(state, '/cast', 'Отзвук цапли | Ясень', roller=lambda *_: next(rolls))
        self.assertIn('7+5', reply)
        self.assertIn('4, гром', reply)
        self.assertEqual((fought['mana'], fought['world_entities'][0]['hp']), (5, 4))
        unfinished = story.replace('приём освоен', 'приём не освоен').replace('приём изучен', 'приём не изучен')
        self.assertEqual(self.m.learned_spell_from_story(unfinished, 'Вера'), [])

    def test_state_extraction_retries_lighthouse_and_preserves_unsaved_draft(self):
        quest = {'id': 'lens', 'name': 'Линза маяка', 'giver': 'Мирон-оружейник',
                 'location': 'Мастерская Кедровой Гавани', 'status': 'active',
                 'stages': [{'text': 'Передать линзу Норе', 'done': False},
                            {'text': 'Предъявить отметку оружейнику', 'done': False}],
                 'rewards': {'gold': 7, 'xp': 40}}
        self.party = uuid4().hex
        self.m.PARTIES.create(self.owner, self.party, self.m.DEFAULT_STATE | {
            'name': 'Вера', 'race': 'человек', 'class': 'следопыт', 'gold': 2, 'quests': [quest],
        }, [])
        self.data.update(party_id=self.party, action={'content': 'Передаю линзу Норе на маяке.'})
        story = ('Нора принимает линзу и вручает тебе отметку с белой цаплей. '
                 'Осталось предъявить отметку оружейнику в мастерской.')
        self.stream(story)

        def response(content, finish='stop'):
            return types.SimpleNamespace(choices=[types.SimpleNamespace(
                finish_reason=finish, message=types.SimpleNamespace(content=content))])

        with patch.object(self.m, '_measured_completion',
                          side_effect=[response('', 'length'), response('неполная сверка')]):
            with self.assertRaises(self.m.StateUnavailable):
                self.commit()
        draft = self.m.PARTIES.get(self.owner, self.party)
        self.assertEqual((draft['revision'], draft['messages'], draft['state']['gold']), (0, [], 2))
        self.assertIsNotNone(draft['draft'])
        delta = {'quests': [{**quest, 'stages': [{**quest['stages'][0], 'done': True}, quest['stages'][1]]}],
                 'inventory': [{'name': 'Отметка с белой цаплей', 'quantity': 1}],
                 'resource_events': {'inventory': 'Нора принимает линзу и вручает тебе отметку с белой цаплей.'}}
        with patch.object(self.m, '_measured_completion', side_effect=[
            response('', 'length'), response(json.dumps(delta, ensure_ascii=False)),
        ]) as extract:
            saved = self.commit()
            self.assertEqual(saved, self.commit())
        self.assertGreater(extract.call_args_list[1].kwargs['max_tokens'], extract.call_args_list[0].kwargs['max_tokens'])
        self.assertIn('один полный компактный JSON', extract.call_args.kwargs['messages'][-1]['content'])
        state = saved['state']
        self.assertEqual((saved['revision'], state['gold'], state['xp']), (1, 2, 0))
        self.assertEqual(state['inventory'][0]['name'], 'Отметка с белой цаплей')
        self.assertEqual([stage['done'] for stage in state['quests'][0]['stages']], [True, False])
        context = self.m._quest_context([{**quest, 'status': 'completed'}], [])
        self.assertEqual((context[0]['giver'], context[0]['location']), (quest['giver'], quest['location']))

    def test_unicode_metadata_is_hidden_and_committed(self):
        story = 'Лина возвращается в таверну.'
        wire = story + '⟦STATE_DELTA⟧{"scene":{"location":"Таверна"}}⟦/STATE_DELTA⟧'
        self.assertEqual(self.stream(wire), story + self.m.TURN_COMPLETE)
        with patch.object(self.m, '_measured_completion', side_effect=AssertionError('Unexpected fallback')):
            saved = self.commit()
        self.assertEqual(saved['state']['scene']['location'], 'Таверна')
        self.assertEqual(saved['messages'][-1]['content'], story)

    def test_quest_coins_are_committed_once_alongside_xp(self):
        quest = {
            'id': 'parcel', 'name': 'Возвращение', 'status': 'active',
            'stages': [{'text': 'Вернуть посылку', 'done': False}],
            'rewards': {'xp': 50, 'gold': 6, 'silver': 2, 'copper': 3},
        }
        state = self.m.PARTIES.get(self.owner, self.party)['state']
        state = {**state, 'gold': 4, 'silver': 3, 'copper': 2, 'quests': [quest]}
        self.party = uuid4().hex
        self.m.PARTIES.create(self.owner, self.party, state, [])
        self.data.update(party_id=self.party, action={'content': 'Возвращаю посылку.'})
        story = 'Посылка доставлена. Ты получаешь шесть золотых.'
        completed = {'id': 'parcel', 'name': 'Возвращение', 'status': 'completed',
                     'stages': [{'text': 'Вернуть посылку', 'done': True}]}
        delta = {'quests': [completed], 'gold': 10, 'resource_events': {'wallet': story}}
        wire = story + START + json.dumps(delta, ensure_ascii=False) + END
        self.assertEqual(self.stream(wire), story + self.m.TURN_COMPLETE)
        with patch.object(self.m, '_measured_completion', side_effect=AssertionError('Unexpected fallback')):
            saved = self.commit()
            self.assertEqual(saved, self.commit())
        self.assertIn('Золото 4 → 10', saved['messages'][-1]['resolution'])
        reloaded = self.m.PARTIES.get(self.owner, self.party)
        history = [*reloaded['messages'], {'role': 'user', 'content': 'Проверяю квест.'},
                   {'role': 'assistant', 'content': 'Посылка уже доставлена.'}]
        next_state = self.m.extract_state(history, reloaded['state'], inline_delta={'quests': [completed]})
        for updated in (reloaded['state'], next_state):
            self.assertEqual(updated['xp'], 50)
            self.assertEqual((updated['gold'], updated['silver'], updated['copper']), (10, 5, 5))

    def test_quest_coins_wait_for_completion_and_separate_payment_condition(self):
        state = self.m.PARTIES.get(self.owner, self.party)['state']
        ordinary = {'id': 'open', 'name': 'Доклад', 'status': 'active', 'rewards': {'gold': 9},
                    'stages': [{'text': 'Сдать доклад', 'done': False}]}
        conditional = {'id': 'sealed', 'name': 'Печать', 'status': 'active',
                       'reward_condition': 'Печать не вскрыта', 'rewards': {'silver': 7}}
        state = {**state, 'quests': [ordinary, conditional]}
        history = [{'role': 'user', 'content': 'Показываю письмо.'},
                   {'role': 'assistant', 'content': 'Письмо принято. Печать ещё не проверили.'}]
        closed = [{**quest, 'status': 'completed'} for quest in state['quests']]
        pending = self.m.extract_state(history, state, inline_delta={'quests': closed})
        self.assertEqual({q['id']: q['status'] for q in pending['quests']},
                         {'open': 'active', 'sealed': 'completed'})
        self.assertEqual((pending['gold'], pending['silver']), (0, 0))
        evidence = 'Проверяющий подтверждает, что печать не вскрыта.'
        history += [{'role': 'user', 'content': 'Жду проверки.'},
                    {'role': 'assistant', 'content': evidence}]
        event = {'id': 'sealed', 'outcome': 'met', 'evidence': evidence}
        paid = self.m.extract_state(history, pending, inline_delta={'reward_outcomes': [event, event]})
        self.assertEqual((paid['gold'], paid['silver']), (0, 7))

    def test_explicit_coin_terms_can_fill_missing_quest_reward(self):
        quest = {'id': 'parcel', 'name': 'Возвращение', 'status': 'active',
                 'rewards': {'xp': 50, 'gold': 2}}
        state = {**self.m.PARTIES.get(self.owner, self.party)['state'], 'quests': [quest]}
        evidence = 'Награда за возвращение посылки — 6 серебряных.'
        history = [{'role': 'user', 'content': 'Сдаю посылку.'},
                   {'role': 'assistant', 'content': evidence}]
        patch = {**quest, 'status': 'completed',
                 'rewards': {'xp': 500, 'gold': 20, 'silver': 6, 'copper': 99},
                 'reward_terms_evidence': evidence}
        updated = self.m.extract_state(history, state, inline_delta={'quests': [patch]})
        self.assertEqual(updated['quests'][0]['rewards'], {'xp': 50, 'gold': 2, 'silver': 6})
        self.assertNotIn('reward_terms_evidence', updated['quests'][0])
        self.assertEqual((updated['xp'], updated['gold'], updated['silver'], updated['copper']),
                         (50, 2, 6, 0))
        unsupported = self.m.extract_state(history, state, inline_delta={
            'quests': [{**patch, 'reward_terms_evidence': 'Мастер обещал много монет.'}],
        })
        self.assertEqual(unsupported['quests'][0]['rewards'], quest['rewards'])
        self.assertEqual(unsupported['silver'], 0)

    def test_metadata_is_hidden_with_inline_state_disabled(self):
        story = 'Лина позволяет Забытому взять её за руку.'
        delta = {'facts': [{'id': 'npc-action:lina-accepts-hand', 'subject': 'Лина',
                           'detail': story, 'source': 'Видимое повествование',
                           'holder': '', 'status': 'confirmed'}]}
        wire = story + '⟦STATE_DELTA⟧' + json.dumps(delta, ensure_ascii=False) + '⟦/STATE_DELTA⟧'
        response = types.SimpleNamespace(choices=[types.SimpleNamespace(
            finish_reason='stop', message=types.SimpleNamespace(content=json.dumps(delta)))])
        with patch.object(self.m, 'INLINE_STATE_ENABLED', False):
            self.assertEqual(self.stream(wire), story + self.m.TURN_COMPLETE)
            with patch.object(self.m, '_measured_completion', return_value=response) as call:
                saved = self.commit()
            self.assertEqual(call.call_count, 1)
        self.assertEqual(saved['messages'][-1]['content'], story)
        self.assertEqual(saved['state']['facts'][0]['id'], 'npc-action:lina-accepts-hand')

    def test_legacy_npc_action_requires_confirmation_in_visible_story(self):
        detail = 'Лина позволяет Забытом взять её за руку; напряжение в её пальцах понемногу слабеет.'
        fact = {'id': 'npc-action:lina-accepts-hand', 'subject': 'Лина', 'detail': detail,
                'source': 'Видимое повествование', 'holder': '', 'status': 'confirmed'}
        state = self.m.PARTIES.get(self.owner, self.party)['state']
        for reply, expected in ((detail, [fact['id']]), ('Лина отдёргивает руку.', [])):
            with self.subTest(reply=reply):
                history = [{'role': 'user', 'content': 'Беру Лину за руку.'},
                           {'role': 'assistant', 'content': reply}]
                updated = self.m.extract_state(history, state, inline_delta={'facts': [fact]})
                self.assertEqual([record['id'] for record in updated['facts']], expected)

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

    def test_alpha513_escort_keeps_npc_location_when_name_is_claimed_elsewhere(self):
        hero = {**self.m.DEFAULT_STATE, 'name': 'Путник', 'race': 'человек', 'class': 'следопыт'}
        old, outside, inn, mill = 'Ниша водосброса', 'Переулок за трактиром', 'Таверна', 'Лаз у мельницы'
        thomas = {'id': 'original', 'kind': 'npc', 'name': 'Томас', 'location': old,
                  'hp': 6, 'max_hp': 8, 'aliases': ['Подросток из водосброса']}
        state = {**hero, 'scene': {'location': old}, 'world_entities': [thomas]}
        moves = (
            (outside, 'Вы возвращаетесь через водосброс. Томас держится рядом и молча слушает.'),
            (inn, 'Вы с Томасом направляетесь в таверну. Внутри пахнет прелым элем. Томас обнимает сестру.'),
            (mill, 'Томас остаётся рядом с сестрой. Ты добираешься до лаза у мельницы.'),
        )
        for location, story in moves:
            state = self.m.extract_state([{'role': 'user', 'content': 'Иду дальше.'},
                                          {'role': 'assistant', 'content': story}], state,
                                         inline_delta={'scene': {'location': location}})
        saved, = state['world_entities']
        self.assertEqual((saved['id'], saved['location'], saved['hp'], saved['aliases']),
                         ('original', inn, 6, thomas['aliases']))

        evidence = 'Подросток не поднимает нож.'
        story = evidence + '\n\n— Томас, — отвечает он. Его слова пока ничем не подтверждены.'
        for identity in ({'id': 'original'}, {'new_entity': True}, {}):
            with self.subTest(identity=identity):
                updated = self.m.extract_state([{'role': 'user', 'content': 'Кто ты?'},
                                                 {'role': 'assistant', 'content': story}], state,
                                                inline_delta={'world_entities': [{
                                                    'kind': 'npc', 'name': 'Томас', 'location': mill,
                                                    'evidence': evidence, **identity}]})
                self.assertEqual(updated['world_entities'], state['world_entities'])
        real_homonym = 'Другой человек по имени Томас подходит к мельнице. Это мельник из соседней деревни.'
        introduced = self.m.extract_state([{'role': 'user', 'content': 'Кто там?'},
                                           {'role': 'assistant', 'content': real_homonym}], state,
                                          inline_delta={'world_entities': [{
                                              'kind': 'npc', 'name': 'Томас', 'location': mill,
                                              'evidence': real_homonym, 'new_entity': True}]})
        self.assertEqual(len({npc['id'] for npc in introduced['world_entities']}), 2)

    def test_alpha513_witnessed_reunion_reaches_model_in_crowded_scene(self):
        reunion = {'id': 'npc-action:reunion', 'kind': 'npc_action', 'subject': 'Томас',
                   'detail': 'Увидев брата, она бросается к нему; Томас обнимает её.',
                   'status': 'confirmed', 'location': 'Таверна'}
        noise = [{'id': 'npc-action:mill-' + str(i), 'kind': 'npc_action', 'subject': 'Мельник',
                  'status': 'confirmed', 'detail': 'Мельник осматривает мельницу и боковой лаз.'}
                 for i in range(35)]
        context = self.m.facts_context([reunion, {**reunion, 'id': 'same-witness'}] + noise,
                                       'Осматриваю боковой лаз у мельницы.')
        self.assertEqual(sum(fact['detail'] == reunion['detail'] for fact in context), 1)
        self.assertLessEqual(len(context), 12)

    def test_alpha512_mechanic_and_homonymous_npcs_are_recorded_on_opening(self):
        state = {**self.m.DEFAULT_STATE, 'name': 'Нерис', 'race': 'полуэльф', 'class': 'следопыт',
                 'scene': {'location': 'Двор станции'}}
        story = ('Возле лестницы механик Ильва проверяет связку инструментов. '
                 'Торговец Тарен раскладывает товар у прилавка; страж Тарен несёт караул.')
        history = [{'role': 'user', 'content': 'Осматриваюсь во дворе.'},
                   {'role': 'assistant', 'content': story}]
        updated = self.m.extract_state(history, state, inline_delta={})
        people = updated['world_entities']
        self.assertEqual({(npc['name'], npc['role']) for npc in people},
                         {('Ильва', 'механик'), ('Тарен', 'страж'), ('Тарен', 'торговец')})
        self.assertEqual(len({npc['id'] for npc in people}), 3)

    def test_alpha512_confirmed_loan_is_added_once(self):
        state = {**self.m.DEFAULT_STATE, 'name': 'Нерис', 'race': 'полуэльф', 'class': 'следопыт',
                 'inventory': [{'name': 'Штифты', 'quantity': 1}]}
        action = 'С штифтом возвращаюсь к Ильве. Прошу одолжить её разводной ключ и объясняю ситуацию.'
        reply = 'Ильва смотрит на купленный штифт и протягивает тебе разводной ключ: — Возвращай.'
        history = [{'role': 'user', 'content': action}, {'role': 'assistant', 'content': reply}]
        borrowed = self.m.extract_state(history, state, inline_delta={})
        self.assertEqual([item['name'] for item in borrowed['inventory']], ['Штифты', 'разводной ключ'])
        self.assertEqual(self.m.extract_state(history, borrowed, inline_delta={})['inventory'], borrowed['inventory'])
        structured = self.m.extract_state(history, state, inline_delta={
            'inventory': [{'name': 'Разводной ключ', 'quantity': 1}], 'resource_events': {'inventory': reply}})
        self.assertEqual(sum(item['name'].casefold() == 'разводной ключ' for item in structured['inventory']), 1)
        history[-1]['content'] = 'Если попросишь, Ильва протягивает тебе разводной ключ.'
        self.assertEqual(self.m.extract_state(history, state, inline_delta={})['inventory'], state['inventory'])

    def test_alpha512_explicit_prose_skill_check_creates_the_card(self):
        state = {**self.m.DEFAULT_STATE, 'name': 'Нерис', 'race': 'полуэльф', 'class': 'следопыт'}
        reply = ('Чтобы осмотреть место, проверь **Внимательность, DC 13, модификатор +3**. '
                 'Если заметишь что-то за ограждением, опишу находку отдельно.')
        history = [{'role': 'user', 'content': 'Осматриваю ограждение.'},
                   {'role': 'assistant', 'content': reply}]
        updated = self.m.extract_state(history, state, inline_delta={})
        check = updated['pending_check']
        self.assertEqual((check['kind'], check['dc'], check['expr']), ('check', 13, '1d20+3'))
        history[-1]['content'] = 'Если решишь осмотреться, проверь Внимательность, DC 13, модификатор +3.'
        self.assertIsNone(self.m.extract_state(history, state, inline_delta={})['pending_check'])

    def test_alpha512_two_narrated_practice_shots_spend_ammo_only_on_resolution(self):
        from campaign import roll_result
        from engine.equipment import ammo_available
        state = {**self.m.DEFAULT_STATE, 'name': 'Нерис', 'race': 'полуэльф', 'class': 'следопыт',
                 'scene': {'location': 'Полигон'}, 'stats': {'dex': 17},
                 'inventory': ['короткий лук', {'name': 'стрелы', 'quantity': 20}]}
        samples = (
            ('Стреляю одной стрелой из короткого лука в Левый манекен. Стрелу не подбираю.',
             'Ты выпускаешь стрелу в Левый манекен у стойки. Брось атаку коротким луком, '
             'против защиты учебного манекена. Команда: /roll 1d20+5', 10, 8, True),
            ('Выстрел из короткого лука по правому манекену за ограждением. После выстрела стрелу не подбираю.',
             'Ты выпускаешь стрелу. Атака коротким луком по Правому манекену за ограждением, '
             'с помехой из-за укрытия в три четверти: /roll 2d20kl1+5.', 15, 7, False),
        )
        history = []
        for count, (action, announcement, dc, die, success) in zip((20, 19), samples):
            history.extend([{'role': 'user', 'content': action}, {'role': 'assistant', 'content': announcement}])
            prepared = self.m.extract_state(history, state, inline_delta={})
            check = prepared['pending_check']
            self.assertEqual((check['dc'], check['expr'], check['ammo']), (dc, '1d20+5', 'arrow'))
            self.assertEqual(ammo_available(prepared, 'arrow'), count)
            roll = roll_result(check, {'expr': check['expr'], 'check_id': check['id'], 'rolls': [die]})
            self.assertEqual(roll['success'], success)
            history.extend([{'role': 'user', 'content': '/roll 1d20+5'},
                            {'role': 'assistant', 'content': 'Стрела попадает.' if success else 'Промах.'}])
            state = self.m.extract_state(history, prepared, result=roll, inline_delta={})
            self.assertEqual(ammo_available(state, 'arrow'), count - 1)
            self.assertEqual(self.m.extract_state(history, state, result=roll, inline_delta={})['inventory'],
                             state['inventory'])

    def test_alpha512_installed_pin_is_spent_once_under_its_inventory_name(self):
        from campaign import roll_result
        state = {**self.m.DEFAULT_STATE, 'name': 'Нерис', 'race': 'полуэльф', 'class': 'следопыт',
                 'inventory': [{'name': 'Штифты', 'quantity': 2}, 'разводной ключ']}
        action = 'Выравниваю гнездо под штифт.'
        announce = 'Проверь Ловкость рук, DC 10. Команда: /roll 1d20+2. Если удастся, можно будет вставить целый штифт.'
        history = [{'role': 'user', 'content': action}, {'role': 'assistant', 'content': announce}]
        prepared = self.m.extract_state(history, state, inline_delta={})
        check = prepared['pending_check']
        roll = roll_result(check, {'expr': check['expr'], 'check_id': check['id'], 'rolls': [10]})
        reply = 'Гнездо выровнено. Стопорный **штифт вставляется в гнездо**. Колокол готов к проверке.'
        history.extend([{'role': 'user', 'content': '/roll 1d20+2'}, {'role': 'assistant', 'content': reply}])
        updated = self.m.extract_state(history, prepared, result=roll, inline_delta={
            'inventory_remove': [{'name': 'стопорный штифт', 'quantity': 1}],
            'resource_events': {'inventory': reply}})
        self.assertEqual(updated['inventory'], [{'name': 'Штифты', 'quantity': 1}, 'разводной ключ'])
        self.assertEqual(self.m.extract_state(history, updated, result=roll, inline_delta={})['inventory'],
                         updated['inventory'])
        recap = [{'role': 'user', 'content': 'Вставляю штифт, если ещё не установлен.'},
                 {'role': 'assistant', 'content': 'Штифт уже установлен в гнезде. Подан сигнал.'}]
        self.assertEqual(self.m.extract_state(recap, updated, inline_delta={})['inventory'], updated['inventory'])

    def test_alpha511_health_recap_only_corrects_hero_hp(self):
        story = ('У гоблина HP: 3/8. У его союзника текущее здоровье: 2/8. '
                 'Текущие HP по листу: **17 из 24**. Текущее здоровье героя: 17/24.')
        updated = self.m._synchronize_recap(story, {'hp': 24, 'max_hp': 24}, None)
        self.assertEqual(updated, ('У гоблина HP: 3/8. У его союзника текущее здоровье: 2/8. '
                                   'Текущие HP по листу: **24/24**. Текущее здоровье героя: 24/24.'))

    def test_alpha511_reported_zero_stock_blocks_buy_and_survives_repeat_quote(self):
        from engine.economy import current_offers, merge_offers
        state = {**self.m.DEFAULT_STATE, 'name': 'Руна', 'race': 'человек', 'class': 'лучник',
                 'scene': {'location': 'Двор'}, 'silver': 20, 'inventory': []}
        price = 'Торговец сообщает: дорожный паёк — 5 серебряных'
        state['shop_offers'] = merge_offers(state, [], price + '; в продаже осталось два.')
        offer_id = current_offers(state)[0]['offer_id']
        for stock in ('в продаже осталось 0', 'в продаже осталось **ноль**',
                      'в продаже не осталось ни одного'):
            with self.subTest(stock=stock):
                story = price + '; ' + stock + '.'
                closed = {**state, 'shop_offers': merge_offers(state, [], story)}
                self.assertEqual(current_offers(closed), [])
                saved, = current_offers(closed, include_sold_out=True)
                self.assertEqual((saved['offer_id'], saved['stock']), (offer_id, 0))
                closed['shop_offers'] = merge_offers(closed, [], price + '.')
                with self.assertRaises(ValueError):
                    self.m._engine_command(closed, '/buy ' + offer_id, None)
                self.assertEqual((closed['silver'], closed['inventory']), (20, []))
                closed['shop_offers'] = merge_offers(closed, [], price + '; в продаже осталось три.')
                reopened, = current_offers(closed)
                self.assertEqual((reopened['offer_id'], reopened['stock']), (offer_id, 3))
        unsupported = {'item': 'дорожный паёк', 'currency': 'silver', 'price': 5, 'stock': 0,
                       'evidence': price}
        unchanged = {**state, 'shop_offers': merge_offers(state, [unsupported], price)}
        self.assertEqual(current_offers(unchanged)[0]['stock'], 2)

    def test_letter_roles_in_parentheses_do_not_hide_namesakes(self):
        letter = {'id': 'seal', 'name': 'Запечатанное письмо', 'quantity': 1}
        candidates = [
            {'id': 'guard', 'name': 'Орен (страж)', 'kind': 'npc', 'location': 'Двор'},
            {'id': 'merchant', 'name': 'Орен', 'kind': 'npc', 'role': 'торговец', 'location': 'Двор'},
        ]
        ambiguous = 'Передаю запечатанное письмо Орену.'
        explicit = 'Передаю запечатанное письмо стражу Орену.'
        merchant = 'Торговец Орен принимает запечатанное письмо.'
        guard = 'Орен (страж) принимает запечатанное письмо.'
        self.assertIsNone(self.m.narrated_letter_transfer([letter], ambiguous, merchant, candidates, 'Двор'))
        self.assertIsNone(self.m.narrated_letter_transfer([letter], explicit, merchant, candidates, 'Двор'))
        transfer = self.m.narrated_letter_transfer([letter], explicit, guard, candidates, 'Двор')
        self.assertEqual(transfer['holder_id'], 'guard')

    def test_rejected_letter_does_not_complete_quest_or_pay(self):
        letter = {'id': 'seal', 'name': 'Запечатанное письмо', 'quantity': 1}
        quest = {'id': 'delivery', 'name': 'Письмо для стража', 'status': 'active',
                 'stages': [{'text': 'Вручить письмо стражу', 'done': False}],
                 'rewards': {'xp': 50, 'silver': 6}}
        state = {**self.m.DEFAULT_STATE, 'name': 'Дана', 'race': 'человек', 'class': 'следопыт',
                 'scene': {'location': 'Двор'}, 'silver': 4, 'inventory': [letter], 'quests': [quest],
                 'world_entities': [
                     {'id': 'guard', 'name': 'Орен (страж)', 'kind': 'npc', 'location': 'Двор'},
                     {'id': 'merchant', 'name': 'Орен', 'kind': 'npc', 'role': 'торговец', 'location': 'Двор'},
                 ]}
        wrong = 'Торговец Орен принимает запечатанное письмо.\n+50 XP — квест «Письмо для стража».'
        history = [{'role': 'user', 'content': 'Передаю запечатанное письмо Орену.'},
                   {'role': 'assistant', 'content': wrong}]
        completed = {**quest, 'status': 'completed', 'stages': [{'text': 'Вручить письмо стражу', 'done': True}]}
        rejected = self.m.extract_state(history, state, inline_delta={
            'quests': [completed], 'silver': 10, 'resource_events': {'wallet': wrong.splitlines()[0]},
        })
        self.assertEqual(rejected['inventory'], [letter])
        self.assertEqual(rejected['quests'][0]['status'], 'active')
        self.assertEqual((rejected['xp'], rejected['silver']), (0, 4))
        history += [{'role': 'user', 'content': 'Передаю запечатанное письмо стражу Орену.'},
                    {'role': 'assistant', 'content': 'Орен (страж) принимает запечатанное письмо.'}]
        delivered = self.m.extract_state(history, rejected, inline_delta={'quests': [completed]})
        self.assertEqual(delivered['inventory'], [])
        self.assertEqual(delivered['quests'][0]['status'], 'completed')
        self.assertEqual((delivered['xp'], delivered['silver']), (50, 10))
        self.assertEqual([f['holder_id'] for f in delivered['facts'] if f.get('holder')], ['guard'])
        self.assertEqual(self.m.extract_state(history, delivered, inline_delta={}), delivered)

    def test_alpha511_letter_requires_same_npc_in_action_and_reply(self):
        letter = {'id': 'seal', 'name': 'Запечатанное письмо', 'quantity': 1}
        state = {**self.m.DEFAULT_STATE, 'name': 'Ива', 'race': 'человек', 'class': 'маг',
                 'scene': {'location': 'Архив'}, 'inventory': [letter],
                 'world_entities': [
                     {'id': 'guard', 'name': 'Орен', 'kind': 'npc', 'role': 'страж', 'location': 'Архив'},
                     {'id': 'merchant', 'name': 'Орен', 'kind': 'npc', 'role': 'торговец', 'location': 'Архив'},
                 ]}
        action = 'Передаю запечатанное письмо стражу Орену.'
        wrong = 'Торговец Орен принимает запечатанное письмо.'
        right = 'Страж Орен принимает запечатанное письмо.'
        for request, reply in ((action, wrong), ('Передаю письмо Орену.', right),
                               (action, right.rstrip('.') + ', если ты согласишься.')):
            with self.subTest(request=request, reply=reply):
                history = [{'role': 'user', 'content': request}, {'role': 'assistant', 'content': reply}]
                updated = self.m.extract_state(history, state, inline_delta={
                    'item_transfers': [{'id': 'seal', 'quantity': 1, 'holder': 'Торговец Орен',
                                        'evidence': reply}],
                    'inventory_remove': [{'id': 'seal', 'quantity': 1}],
                    'resource_events': {'inventory': reply},
                    'facts': [{'id': 'letter', 'subject': 'Запечатанное письмо', 'detail': 'Передано.',
                               'holder': 'Торговец Орен', 'status': 'confirmed'}],
                })
                self.assertEqual(updated['inventory'], [letter])
                self.assertFalse(any(fact.get('holder') for fact in updated['facts']))
                self.assertTrue(any('Уточните получателя' in log for log in updated['effect_log']))

        party_id, request_id = uuid4().hex, uuid4().hex
        narrated = [{'role': 'user', 'content': action},
                    {'role': 'assistant', 'content': 'Ты вручаешь **запечатанное письмо** стражу Орену.'}]
        delivered = self.m.extract_state(narrated, state, inline_delta={})
        self.assertEqual(delivered['inventory'], [])
        self.assertEqual([(fact['holder'], fact['holder_id']) for fact in delivered['facts'] if fact.get('holder')],
                         [('Страж Орен', 'guard')])
        self.m.PARTIES.create(self.owner, party_id, state, [])
        self.m.PARTIES.stage(self.owner, party_id, 0, request_id, {'role': 'user', 'content': action},
                             {'role': 'assistant', 'content': right, '_state_delta': {}})
        self.data.update(party_id=party_id, request_id=request_id)
        committed = self.commit()
        self.assertEqual(committed, self.commit())
        self.assertEqual(committed['revision'], 1)
        self.assertEqual(committed['state']['inventory'], [])
        fact, = [fact for fact in committed['state']['facts'] if fact.get('holder')]
        self.assertEqual((fact['holder'], fact['holder_id']), ('Страж Орен', 'guard'))
        self.assertEqual(self.m.PARTIES.get(self.owner, party_id)['state'], committed['state'])

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
