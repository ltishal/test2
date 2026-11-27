import unittest
from copy import deepcopy
from engine.effects import resolve_effects, effect_context, check_bonus
from engine.turn_protocol import validate_delta
from campaign import roll_result

class EffectsTest(unittest.TestCase):
    def setUp(self):
        self.story='Событие действительно произошло.'
        self.state={'name':'Лис','hp':20,'max_hp':30,'world_minute':100,'scene':{'location':'Лес'},'world_entities':[
            {'id':'wolf','kind':'creature','name':'Волк','hp':20,'max_hp':20},
            {'id':'gate','kind':'location','name':'Врата'}, {'id':'npc','kind':'npc','name':'Мара'}]}
    def p(self, typ, **kw):
        return {'type':typ,'evidence':self.story,**kw}
    def run_effects(self, *p, state=None, minute=100, **kw):
        return resolve_effects(self.state if state is None else state,list(p),self.story,minute,**kw)
    def test_damage_heal_and_no_mutation(self):
        old=deepcopy(self.state)
        s=self.run_effects(self.p('damage',target='wolf',amount=7),self.p('heal',target='hero',amount=99))
        self.assertEqual(s['hp'],30); self.assertEqual(s['world_entities'][0]['hp'],13); self.assertEqual(self.state,old)
    def test_death_is_not_reversed_by_heal(self):
        s=self.run_effects(self.p('damage',target='wolf',amount=99))
        self.assertEqual(s['world_entities'][0]['hp'],0)
        s=self.run_effects(self.p('heal',target='wolf',amount=10),state=s)
        self.assertEqual(s['world_entities'][0]['hp'],0); self.assertIn('не применён',s['effect_log'][0])
    def test_evidence_unknown_target_and_bool_rejected(self):
        s=self.run_effects(self.p('damage',target='wolf',amount=True),self.p('damage',target='unknown',amount=5),self.p('damage',target='wolf',amount=5,evidence='не подтверждено'))
        self.assertEqual(s['world_entities'],self.state['world_entities']); self.assertEqual(len(s['effect_log']),3)
    def test_signed_and_duplicate_hp_blocked(self):
        s=self.run_effects(self.p('damage',target='wolf',amount=8),self.p('damage',target='hero',amount=3),self.p('damage',target='hero',amount=3),blocked_hp={'wolf'})
        self.assertEqual(s['world_entities'][0]['hp'],20); self.assertEqual(s['hp'],17)
    def test_distinct_hits_accumulate_but_a_repeat_does_not(self):
        first = 'Стражник попадает копьём.'
        second = 'Камень ударяет в плечо.'
        effects = [
            self.p('damage', target='hero', amount=3, evidence=first),
            self.p('damage', target='hero', amount=4, evidence=second),
            self.p('damage', target='hero', amount=4, evidence=second),
        ]
        state = resolve_effects(self.state, effects, first + ' ' + second, 100)
        self.assertEqual(state['hp'], 13)
        self.assertIn('Повтор события', state['effect_log'][-1])
    def test_explicit_hp_replaces_legacy_delta(self):
        s=self.run_effects(self.p('damage',target='hero',amount=5),state={**self.state,'hp':15},hero_before=(20,15))
        self.assertEqual(s['hp'],15)
    def test_modifier_and_roll(self):
        s=self.run_effects(self.p('modifier',target='hero',stat='dex',amount=-3,duration=20))
        self.assertEqual(check_bonus(s,'dex'),-3)
        check={'id':'a','expr':'1d20+2','dc':15,'effect_bonus':check_bonus(s,'dex')}
        roll=roll_result(check,{'expr':'1d20+2','rolls':[15],'check_id':'a'})
        self.assertEqual(roll['total'],14); self.assertFalse(roll['success'])
        s=self.run_effects(state=s,minute=120); self.assertEqual(s['active_effects'],[])
    def test_schedule_ticks_and_expiration(self):
        s=self.run_effects(self.p('schedule',target='hero',name='Яд',operation='damage',amount=2,delay=5,interval=5,count=3))
        s=self.run_effects(state=s,minute=112); self.assertEqual(s['hp'],16); self.assertEqual(s['active_effects'][0]['remaining'],1)
        s=self.run_effects(state=s,minute=112); self.assertEqual(s['hp'],16)
        s=self.run_effects(state=s,minute=115); self.assertEqual(s['hp'],14); self.assertEqual(s['active_effects'],[])
    def test_control_and_dispel(self):
        s=self.run_effects(self.p('control',target='wolf',control='sleep',duration=10))
        self.assertEqual(effect_context(s)[0]['control'],'sleep')
        s=self.run_effects(self.p('dispel',effect_id=s['active_effects'][0]['id']),state=s); self.assertEqual(s['active_effects'],[])
    def test_create_move_transform_environment_relation(self):
        s=self.run_effects(self.p('create',entity={'kind':'location','name':'Зеркальный мир'}),self.p('move',target='hero',destination={'kind':'location','name':'Зеркальный мир'}),self.p('transform',target='hero',form='Тень'),self.p('transform',target='wolf',form='Водяной волк'),self.p('environment',target='gate',environment='Лёд'),self.p('relation',target='npc',amount=20))
        self.assertEqual(s['scene']['location'],'Зеркальный мир'); self.assertEqual(s['hero_form']['form'],'Тень'); self.assertEqual(s['world_entities'][0]['form'],'Водяной волк'); self.assertEqual(s['world_entities'][1]['environment'],'Лёд'); self.assertEqual(s['world_entities'][2]['attitude'],20)
    def test_drain_conserves_resource_and_invalid_is_atomic(self):
        s=self.run_effects(self.p('drain',target='wolf',recipient='hero',resource='hp',amount=99))
        self.assertEqual(s['hp'],30); self.assertEqual(s['world_entities'][0]['hp'],10)
        s=self.run_effects(self.p('drain',target='wolf',recipient='npc',resource='mana',amount=5))
        self.assertNotIn('mana',s['world_entities'][0])
    def test_mana_drain(self):
        s=self.run_effects(self.p('drain',target='wolf',recipient='hero',resource='mana',amount=5),state={**self.state,'mana':10,'max_mana':20})
        self.assertEqual(s['mana'],15); self.assertEqual(s['world_entities'][0]['mana'],15)
    def test_protocol_effects_and_protected_fields(self):
        self.assertEqual(validate_delta({'effects':[]}),{'effects':[]}); self.assertIsNone(validate_delta({'effects':{}})); self.assertIsNone(validate_delta({'active_effects':[]}))
