import json
import unittest
from engine.turn_protocol import DeltaStream, START, END, validate_delta
from engine.world import merge_entities, world_context, bind_damage_target, npc_damage, apply_npc_damage
from campaign import roll_result


class ProtocolTest(unittest.TestCase):
    def test_every_chunk_boundary(self):
        story = 'Герой видит лес.\n'
        payload = {'scene': {'location':'лес'}}
        wire = story + START + json.dumps(payload, ensure_ascii=False) + END
        for size in range(1,len(wire)+1):
            p=DeltaStream()
            visible=''.join(p.feed(wire[i:i+size]) for i in range(0,len(wire),size))
            self.assertEqual(visible,story)
            self.assertEqual(p.finish(),payload)

    def test_invalid_truncated_and_null_fall_back(self):
        for suffix in ('{broken'+END, '{"scene":{}}', 'null'+END, '{}'+END+'more', '{"world_entities":42}'+END):
            p=DeltaStream()
            self.assertEqual(p.feed('Текст'+START+suffix),'Текст')
            self.assertIsNone(p.finish())

    def test_missing_block_and_empty_delta(self):
        p=DeltaStream(); self.assertEqual(p.feed('Обычный текст.'),'Обычный текст.'); self.assertIsNone(p.finish())
        p=DeltaStream(); p.feed('Текст'+START+'{}'+END); self.assertEqual(p.finish(),{})

    def test_protected_fields_and_bad_types(self):
        self.assertEqual(validate_delta({'xp':1000,'level':99,'hp_delta':-3}),{'hp_delta':-3})
        for v in ({'hp_delta':True},{'scene':[]},{'bogus':1},{'facts':[{'bad':float('nan')}]},{'gold':10**20}):
            self.assertIsNone(validate_delta(v))

    def test_overlong_metadata_not_visible(self):
        p=DeltaStream(); self.assertEqual(p.feed('Текст'+START+'x'*25000+END),'Текст'); self.assertIsNone(p.finish())


class DynamicWorldTest(unittest.TestCase):
    def test_named_remote_npc_survives_a_crowded_scene_after_many_turns(self):
        mara = {'id': 'mara', 'kind': 'npc', 'name': 'Мара', 'location': 'Серый Брод',
                'description': 'Хозяйка трактира, отдала герою журнал.'}
        state = {'world_entities': [mara], 'scene': {'location': 'Башня'}}
        for index in range(40):
            state['world_entities'].append({'id': str(index), 'kind': 'npc', 'name': f'Стражник {index}',
                                            'location': 'Башня', 'description': 'Уже знакомый часовой.'})
        context = world_context(state, 'Расспрашиваю о Маре.')
        self.assertEqual(context[0]['id'], 'mara')
        self.assertIn('журнал', context[0]['description'])
        self.assertLessEqual(len(context), 12)

    def test_existing_npc_gains_combat_stats_without_losing_identity(self):
        original = {'id': 'mara', 'kind': 'npc', 'name': 'Мара', 'attitude': -10,
                    'description': 'Хозяйка трактира.'}
        evidence = 'Мара обнажает кинжал.'
        proposal = {'id': 'mara', 'threat': 'dangerous', 'evidence': evidence}
        state = {'world_entities': [original]}
        updated = merge_entities(state, [proposal], evidence)[0]
        self.assertEqual({field: updated[field] for field in original}, original)
        self.assertTrue(31 <= updated['hp'] <= 65)
        updated['hp'] -= 5
        replay = merge_entities({'world_entities': [updated]}, [proposal], evidence)[0]
        self.assertEqual(replay['hp'], updated['hp'])

    def setUp(self):
        self.reply='Из тени вышел Пожиратель снов.'
        self.patch={'kind':'creature','name':'Пожиратель снов','threat':'dangerous','description':'Шесть глаз', 'location':'Лес','evidence':self.reply,'hp':99999}
        self.entities=merge_entities({},[self.patch],self.reply)
        self.entity=self.entities[0]
        self.state={'world_entities':self.entities,'scene':{'location':'Лес','participants':['Пожиратель снов']}}

    def test_new_creature_server_stats_and_reuse(self):
        self.assertTrue(31<=self.entity['hp']<=65)
        self.assertEqual(self.entity['hp'],self.entity['max_hp'])
        patch={**self.patch,'description':'Семь глаз','threat':'legendary'}
        result=merge_entities(self.state,[patch],self.reply)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['id'],self.entity['id'])
        self.assertEqual(result[0]['hp'],self.entity['hp'])
        self.assertEqual(self.entity['description'],'Шесть глаз')

    def test_unknown_id_identity_and_evidence_rejected(self):
        for patch in ({**self.patch,'id':'invented'}, {**self.patch,'id':self.entity['id'],'name':'Другая тварь'}, {**self.patch,'evidence':'Нет в тексте'}):
            self.assertEqual(merge_entities(self.state,[patch],self.reply),self.entities)

    def test_arbitrary_content_not_catalogue(self):
        for kind in ('npc','item','location','faction','ability','other'):
            proposal={'kind':kind,'name':'Неповторимый '+kind,'evidence':'Возник необычный объект.'}
            result=merge_entities({},[proposal],'Возник необычный объект.')
            self.assertEqual(result[0]['name'],proposal['name'])
            self.assertNotIn('hp',result[0])

    def test_context_bounded_and_relevant(self):
        entries=[{'id':str(i),'name':'Прочий '+str(i),'kind':'npc','description':'а'*10000} for i in range(50)]
        result=world_context({**self.state,'world_entities':self.entities+entries},'Пожиратель снов')
        self.assertEqual(result[0]['id'],self.entity['id'])
        self.assertLessEqual(len(result),12)
        self.assertLessEqual(sum(len(json.dumps(e,ensure_ascii=False)) for e in result),3500)

    def test_signed_roll_applies_damage_only_to_bound_target(self):
        check={'kind':'damage','target':self.entity['name'],'effect':'','id':'c','expr':'2d6'}
        bind_damage_target(self.state,check,{'world_target_id':self.entity['id']})
        roll=roll_result(check,{'expr':'2d6','rolls':[4,5],'check_id':'c'})
        outcome=npc_damage(self.state,roll)
        self.assertEqual(outcome['hp_after'],self.entity['hp']-9)
        result=apply_npc_damage(merge_entities(self.state,[],''),outcome)
        self.assertEqual(result[0]['hp'],self.entity['hp']-9)
        self.assertEqual(npc_damage(self.state,{'total':9,'check':{'kind':'damage','effect':'hero_damage','world_target_id':self.entity['id']}}),{})

    def test_death_preserved_and_new_target_resolution(self):
        check={'kind':'damage','target':self.entity['name'],'effect':''}
        bind_damage_target(self.state,check,{'world_target':True})
        self.assertEqual(check['world_target_id'],self.entity['id'])
        outcome=npc_damage(self.state,{'check':check,'total':1000})
        dead=apply_npc_damage(merge_entities(self.state,[],''),outcome)
        updated=merge_entities({'world_entities':dead},[{**self.patch,'status':'жив'}],self.reply)
        self.assertEqual(updated[0]['hp'],0)
        self.assertEqual(updated[0]['status'],'побеждён (0 HP)')

    def test_legacy_saves_without_registry(self):
        self.assertEqual(world_context({'name':'Герой'},'лес'),[])
        self.assertEqual(merge_entities({},None,''),[])
