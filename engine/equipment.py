"""Validated, data-driven equipment; no executable model-supplied code."""
import re

from campaign import formula_key
from engine.world import key, records

ELEMENTS = {'physical','fire','cold','lightning','poison','arcane','radiant','shadow'}
WEAPONS = {'кинжал':'1d4','посох':'1d6','боевой посох':'1d6','короткий меч':'1d6', 'длинный меч':'1d8','двуручный меч':'2d6','длинный лук':'1d8', 'короткий лук':'1d6', 'лук':'1d6',
           'легкий арбалет':'1d8', 'тяжелый арбалет':'1d10', 'арбалет':'1d8'}
ARMOR = {'кожаная броня':11,'кожаный доспех':11,'клепаная кожа':12,'чешуйчатый доспех':14,'кольчуга':16,'латы':18}


def equipment_spec(raw):
    if not isinstance(raw, dict): return None
    slot = raw.get('slot')
    if slot == 'weapon':
        dice = raw.get('dice')
        parts = formula_key(dice) if isinstance(dice,str) else None
        if not parts or parts[0] > 4 or parts[1] > 12 or parts[2] or not -3 <= parts[4] <= 6:
            return None
        if parts[0]*parts[1]+parts[4] > 36: return None
        element = raw.get('element','physical')
        if not isinstance(element,str) or element not in ELEMENTS: return None
        stat = raw.get('stat','str')
        if stat not in ('str','dex','int','wis','cha'): return None
        ammo = raw.get('ammo')
        if ammo is not None and ammo not in ('arrow', 'bolt'): return None
        spec = {'slot':slot,'dice':dice,'element':element,'stat':stat}
        if ammo: spec['ammo'] = ammo
        return spec
    if slot == 'armor':
        defense = raw.get('defense'); dex_cap = raw.get('dex_cap',0)
        if type(defense) is not int or not 10 <= defense <= 20 or type(dex_cap) is not int or not 0 <= dex_cap <= 5:
            return None
        return {'slot':slot,'defense':defense,'dex_cap':dex_cap}
    return None


def weapon_spec(name):
    name = key(name)
    if name not in WEAPONS:
        return None
    spec = {'slot': 'weapon', 'dice': WEAPONS[name], 'element': 'physical', 'stat': 'str'}
    if name.endswith(('лук', 'арбалет')):
        spec.update(stat='dex', ammo='arrow' if name.endswith('лук') else 'bolt')
    return spec


def ammo_kind(item):
    fields = (item.get('name'), item.get('id')) if isinstance(item, dict) else (item,)
    for field in fields:
        name = key(field)
        if name in ('arrow', 'arrows') or re.search(r'\bстрел(?:а|ы|у)?\b', name):
            return 'arrow'
        if name in ('bolt', 'bolts') or re.search(r'\bболт(?:ы|ов)?\b', name):
            return 'bolt'
    return None


def ammo_count(item):
    count = item.get('quantity', 1) if isinstance(item, dict) else 1
    return count if type(count) is int and count > 0 else 0


def ammo_available(state, kind):
    return sum(ammo_count(item) for item in state.get('inventory', []) if ammo_kind(item) == kind)


def require_ammo(state, kind):
    if not ammo_available(state, kind):
        raise ValueError('Для выстрела нет боеприпасов.')


def consume_ammo(state, kind):
    require_ammo(state, kind)
    inventory = state['inventory']
    for index, item in enumerate(inventory):
        if ammo_kind(item) == kind and (count := ammo_count(item)):
            if count == 1:
                del inventory[index]
            else:
                inventory[index] = {**item, 'quantity': count - 1}
            break
    label = 'Стрелы' if kind == 'arrow' else 'Болты'
    return f'{label}: −1, осталось {ammo_available(state, kind)}.'


def item_spec(state, item):
    name = item.get('name') if isinstance(item,dict) else item
    builtin = weapon_spec(name)
    if isinstance(item,dict):
        spec = equipment_spec(item.get('mechanics'))
        if spec:
            if builtin and builtin.get('ammo') and spec['slot'] == 'weapon':
                spec.update(ammo=builtin['ammo'], stat='dex')
            return spec
    world_id = item.get('world_id') if isinstance(item,dict) else None
    matches = [e for e in records(state) if e.get('kind')=='item' and (e['id']==world_id if world_id else key(e['name'])==key(name))]
    if len(matches)==1:
        spec=equipment_spec(matches[0].get('mechanics'))
        if spec:
            if builtin and builtin.get('ammo') and spec['slot'] == 'weapon':
                spec.update(ammo=builtin['ammo'], stat='dex')
            return spec
    if builtin: return builtin
    if key(name) in ARMOR: return {'slot':'armor','defense':ARMOR[key(name)],'dex_cap':5 if ARMOR[key(name)]<=12 else 0}
    return None


def equipped(state, slot):
    name=state.get('weapon_in_hand' if slot=='weapon' else 'armor_equipped')
    if not name: return None
    matches=[item for item in state.get('inventory',[]) if key(item.get('name') if isinstance(item,dict) else item)==key(name)]
    ident=state.get(slot+'_equipped_id')
    if ident:
        exact=[item for item in matches if isinstance(item,dict) and item.get('id')==ident]
        if exact: matches=exact
    if len(matches)==1: spec=item_spec(state,matches[0])
    else:  # Legacy saves may contain only the built-in equipped name.
        spec=(weapon_spec(name) if slot=='weapon' else
              {'slot':'armor','defense':ARMOR[key(name)],'dex_cap':5 if ARMOR[key(name)]<=12 else 0} if slot=='armor' and key(name) in ARMOR else None)
    return spec if spec and spec['slot']==slot else None
