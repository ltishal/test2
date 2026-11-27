"""Regression cases found in the Alpha 3 audit, no paid model calls."""
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from concurrent.futures import ThreadPoolExecutor
from engine.clock import advance_action
from engine.combat import combat_turn, hero_defense, safe_ability, find_combatant, profile
from engine.equipment import equipment_spec, item_spec
from engine.effects import resolve_effects, check_bonus
from engine.world import merge_entities, combat_ready, life_state
from engine.economy import merge_offers, current_offers, settle_offer, trade_evidence
from engine.api_budget import ApiBudget, BudgetExceeded, estimated_units


class Alpha31Test(unittest.TestCase):
    def setUp(self):
        self.hero={'name':'Лея','hp':50,'max_hp':50,'race':'эльф','class':'маг','level':3,'xp':0,
                   'world_minute':600,'scene':{'location':'Двор'},'stats':{'str':10,'dex':14,'int':16},
                   'gold':10,'silver':10,'copper':0,'inventory':[],
                   'world_entities':[{'id':'guard','name':'Орен','kind':'npc','location':'Двор',
                                      'hp':80,'max_hp':80,'threat':'normal'}]}

    def test_ten_rounds_advance_minute_and_tick_once(self):
        state=deepcopy(self.hero)
        state['active_effects']=[{'id':'poison','name':'Яд','type':'schedule','target':'hero',
                                  'operation':'damage','amount':3,'interval':1,'next_tick':601,'remaining':2}]
        for _ in range(10):
            state,_=combat_turn(state,'/attack','guard',roller=lambda low,high:low)
        self.assertEqual((state['world_minute'],state['world_second'],state['hp']),(601,0,47))
        self.assertEqual(state['active_effects'][0]['remaining'],1)
        self.assertEqual(self.hero['world_minute'],600)

    def test_partial_seconds_preserved_across_wait_and_round(self):
        state=deepcopy(self.hero); state['world_second']=54
        advance_action(state,60)
        self.assertEqual((state['world_minute'],state['world_second']),(601,54))
        advance_action(state,6)
        self.assertEqual((state['world_minute'],state['world_second']),(602,0))

    def test_expired_control_and_modifier_on_clock(self):
        state=deepcopy(self.hero); state['world_second']=54
        state['active_effects']=[{'id':'m','type':'modifier','target':'hero','stat':'str','amount':-5,'expires':601}]
        self.assertEqual(check_bonus(state,'str'),-5)
        advance_action(state,6)
        self.assertEqual(check_bonus(state,'str'),0)
        self.assertEqual(state['active_effects'],[])

    def test_modifier_changes_hero_hit(self):
        base=deepcopy(self.hero)
        def play(state):
            # Hero initiative 20+2, enemy 1; hero attack 10+2 vs AC12; damage 4; enemy rolls 1.
            values=iter([20,1,10,4,1])
            return combat_turn(state,'/attack','guard',roller=lambda lo,hi:next(values))
        good,_=play(base)
        self.assertEqual(good['world_entities'][0]['hp'],76)
        base['active_effects']=[{'id':'m','type':'modifier','target':'hero','stat':'str','amount':-5,'expires':610}]
        bad,log=play(base)
        self.assertEqual(bad['world_entities'][0]['hp'],80)
        self.assertIn('Промах',log)

    def test_npc_modifier_and_control(self):
        state=deepcopy(self.hero)
        state['active_effects']=[{'id':'m','type':'modifier','target':'guard','stat':'str','amount':-5,'expires':610}]
        self.assertEqual(check_bonus(state,'str','guard'),-5)
        # Hero misses; enemy 9+3-5=7 vs AC12.
        values=iter([20,1,1,9])
        updated,_=combat_turn(state,'/attack','guard',roller=lambda lo,hi:next(values))
        self.assertEqual(updated['hp'],50)
        state['active_effects']=[{'id':'c','type':'control','target':'hero','control':'paralysis','expires':610}]
        with self.assertRaisesRegex(ValueError,'не может'): combat_turn(state,'/attack','guard')

    def test_custom_equipment_registry_and_combat(self):
        story='Кузнец отдаёт Ледяную иглу и Плащ ветра.'
        state=deepcopy(self.hero)
        state['world_entities']=merge_entities(state,[
            {'name':'Ледяная игла','kind':'item','mechanics':{'slot':'weapon','dice':'2d6+1','element':'cold','stat':'dex'},'evidence':story},
            {'name':'Плащ ветра','kind':'item','mechanics':{'slot':'armor','defense':15,'dex_cap':2},'evidence':story}],story)
        state.update(inventory=['Ледяная игла','Плащ ветра'],weapon_in_hand='Ледяная игла',armor_equipped='Плащ ветра')
        self.assertEqual(hero_defense(state),17)
        self.assertEqual(item_spec(state,'Ледяная игла')['dice'],'2d6+1')
        state['world_entities'][0]['combat_profile']={'resistances':{'cold':'resistant'}}
        values=iter([20,1,12,6,6,1])
        after,_=combat_turn(state,'/attack','guard',roller=lambda lo,hi:next(values))
        self.assertEqual(after['world_entities'][0]['hp'],74)
        self.assertIsNone(equipment_spec({'slot':'armor','defense':999}))
        self.assertIsNone(equipment_spec({'slot':'weapon','dice':'99d99'}))

    def test_homonyms_new_id_and_move_existing(self):
        state=deepcopy(self.hero); story='В порту Орен чинит лодку.'
        state['world_entities']=merge_entities(state,[{'name':'Орен','kind':'npc','location':'Порт','evidence':story}],story)
        self.assertEqual(len(state['world_entities']),2)
        second=state['world_entities'][1]['id']; self.assertNotEqual(second,'guard')
        text='Орен из порта пришёл во двор.'
        state['world_entities']=merge_entities(state,[{'id':second,'name':'Орен','location':'Двор','evidence':text}],text)
        with self.assertRaises(ValueError): find_combatant(state,'Орен')
        self.assertEqual(find_combatant(state,second)['id'],second)
        self.assertEqual(len(state['world_entities']),2)

    def test_same_location_explicit_new_entity(self):
        story='Другой стражник по имени Орен входит во двор.'
        entities=merge_entities(self.hero,[{'kind':'npc','name':'Орен','location':'Двор','new_entity':True,'evidence':story}],story)
        self.assertEqual(len(entities),2)
        self.assertNotEqual(entities[0]['id'],entities[1]['id'])

    def test_death_synonyms_and_no_resurrection(self):
        for label in ('убит','погибший','уничтожен','мёртв','dead'):
            with self.subTest(label=label):
                e={'kind':'npc','name':'Орен','id':'guard','status':label}
                combat_ready(e,'normal'); self.assertNotIn('hp',e)
                text='Орен снова стоит.'
                after=merge_entities({'world_entities':[e]},[{'id':'guard','status':'alive','threat':'normal','evidence':text}],text)[0]
                self.assertIn(life_state(after),('dead','destroyed'))
                self.assertNotIn('hp',after)
        state=deepcopy(self.hero)
        state['world_entities'][0]['status']='убит'
        with self.assertRaises(ValueError): combat_turn(state,'/attack','guard')

    def test_ability_power_budget(self):
        absurd={'name':'Всё разрушить','dice':'6d20+12','hits':4,'automatic':True,'cost':0,'cooldown':0}
        self.assertIsNone(safe_ability(absurd,power=20))
        strong={'name':'Шторм','dice':'3d8','cost':4,'cooldown':2,'element':'lightning'}
        self.assertIsNone(safe_ability({**strong,'cost':0,'cooldown':0},power=1))
        self.assertIsNotNone(safe_ability(strong,power=5))
        self.assertEqual(profile({'threat':'minor'},{'abilities':[absurd]})['abilities'],[])

    def test_trade_evidence_seller_stock_and_quantity(self):
        state=deepcopy(self.hero)
        bad={'item':'Алмаз','currency':'copper','price':1,'evidence':'Идёт дождь.'}
        self.assertEqual(merge_offers(state,[bad],bad['evidence']),[])
        text='Орен продаёт Алмаз за три серебряных, в наличии 2 шт.'
        good={'item':'Алмаз','currency':'silver','price':3,'stock':2,'seller_id':'guard','evidence':text}
        self.assertEqual(merge_offers(state,[{**good,'seller_id':'unknown'}],text),[])
        state['shop_offers']=merge_offers(state,[good],text)
        offer,=current_offers(state)
        with self.assertRaises(ValueError): settle_offer(state,offer,3)
        settle_offer(state,offer,2)
        self.assertEqual(state['silver'],4)
        self.assertEqual(current_offers(state),[])
        self.assertEqual(state['inventory'][0]['quantity'],2)
        self.assertFalse(trade_evidence({**good,'price':1},text))

    def test_dead_or_remote_seller_unavailable(self):
        state=deepcopy(self.hero)
        text='Орен продаёт Паёк за три серебряных.'
        state['shop_offers']=merge_offers(state,[{'item':'Паёк','currency':'silver','price':3,'seller_id':'guard','evidence':text}],text)
        self.assertEqual(len(current_offers(state)),1)
        state['world_entities'][0]['location']='Порт'
        self.assertEqual(current_offers(state),[])
        state['world_entities'][0].update(location='Двор',status='убит')
        self.assertEqual(current_offers(state),[])

    def test_budget_persists_and_cookie_rotation_cannot_reset_global(self):
        with TemporaryDirectory() as path:
            file=Path(path)/'api.sqlite3'
            budget=ApiBudget(file,global_calls=2,owner_calls=1,global_units=100)
            budget.reserve('one',20,'2026-11-27')
            with self.assertRaises(BudgetExceeded): budget.reserve('one',20,'2026-11-27')
            budget=ApiBudget(file,global_calls=2,owner_calls=1,global_units=100)
            budget.reserve('two',20,'2026-11-27')
            with self.assertRaises(BudgetExceeded): budget.reserve('three',20,'2026-11-27')
            budget.reserve('one',20,'2026-11-28')

    def test_budget_atomic_concurrent_and_unit_cap(self):
        with TemporaryDirectory() as path:
            budget=ApiBudget(Path(path)/'api.sqlite3',global_calls=99,owner_calls=99,global_units=100)
            def reserve(i):
                try: budget.reserve(str(i),30,'2026-11-27'); return True
                except BudgetExceeded: return False
            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(sum(pool.map(reserve,range(12))),3)
            self.assertEqual(estimated_units({'messages':[{'content':'аб'}],'max_tokens':10}),14)

    def test_closed_location_cannot_be_entered_by_effect(self):
        state=deepcopy(self.hero); state['world_entities'].append({'id':'gate','name':'Врата','kind':'location','access':'closed'})
        story='Герой исчезает во вратах.'
        after=resolve_effects(state,[{'type':'move','target':'hero','destination':'gate','evidence':story}],story,600)
        self.assertEqual(after['scene']['location'],'Двор')
        self.assertIn('не применён',after['effect_log'][0])
