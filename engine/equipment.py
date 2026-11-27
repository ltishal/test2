"""Validated, data-driven equipment; no executable model-supplied code."""
from campaign import formula_key
from engine.world import key, records

ELEMENTS = {'physical','fire','cold','lightning','poison','arcane','radiant','shadow'}
WEAPONS = {'кинжал':'1d4','посох':'1d6','боевой посох':'1d6','короткий меч':'1d6', 'длинный меч':'1d8','двуручный меч':'2d6','длинный лук':'1d8'}
ARMOR = {'кожаная броня':11,'клепаная кожа':12,'чешуйчатый доспех':14,'кольчуга':16,'латы':18}


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
        return {'slot':slot,'dice':dice,'element':element,'stat':stat}
    if slot == 'armor':
        defense = raw.get('defense'); dex_cap = raw.get('dex_cap',0)
        if type(defense) is not int or not 10 <= defense <= 20 or type(dex_cap) is not int or not 0 <= dex_cap <= 5:
            return None
        return {'slot':slot,'defense':defense,'dex_cap':dex_cap}
    return None


def item_spec(state, item):
    name = item.get('name') if isinstance(item,dict) else item
    if isinstance(item,dict):
        spec = equipment_spec(item.get('mechanics'))
        if spec: return spec
    world_id = item.get('world_id') if isinstance(item,dict) else None
    matches = [e for e in records(state) if e.get('kind')=='item' and (e['id']==world_id if world_id else key(e['name'])==key(name))]
    if len(matches)==1:
        spec=equipment_spec(matches[0].get('mechanics'))
        if spec: return spec
    if key(name) in WEAPONS: return {'slot':'weapon','dice':WEAPONS[key(name)],'element':'physical','stat':'str'}
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
        spec=({'slot':'weapon','dice':WEAPONS[key(name)],'element':'physical','stat':'str'} if slot=='weapon' and key(name) in WEAPONS else
              {'slot':'armor','defense':ARMOR[key(name)],'dex_cap':5 if ARMOR[key(name)]<=12 else 0} if slot=='armor' and key(name) in ARMOR else None)
    return spec if spec and spec['slot']==slot else None
