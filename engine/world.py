"""Persistent generated world entities. No predefined bestiary or item catalogue."""
import re
import json
from copy import deepcopy
from uuid import uuid4

KINDS = {'npc', 'creature', 'item', 'location', 'faction', 'ability', 'other'}
# Relative power within the world's fiction, not scaled to the player's level.
THREAT_HP = {'minor': (4,12), 'normal': (13,30), 'dangerous': (31,65), 'elite': (66,130), 'legendary': (131,300)}
TEXT_FIELDS = {'description': 600, 'location': 180, 'status': 120, 'traits': 400}


def text(v, n):
    return v.strip()[:n] if isinstance(v, str) else ''


def key(v):
    return ' '.join(text(v, 160).casefold().replace('ё','е').split())


def records(state):
    value = state.get('world_entities')
    return [e for e in value if isinstance(e,dict) and isinstance(e.get('id'),str) and isinstance(e.get('name'),str)] if isinstance(value,list) else []


def merge_entities(state, proposals, reply):
    """Apply small evidence-grounded patches; identity and combat HP belong to the server."""
    result = deepcopy(records(state))
    by_id = {e['id']: e for e in result}
    if not isinstance(proposals,list):
        return result
    for patch in proposals[:12]:
        if not isinstance(patch,dict): continue
        evidence = text(patch.get('evidence'),600)
        if len(evidence)<5 or evidence not in reply: continue
        entity_id = text(patch.get('id'),80)
        name = text(patch.get('name'),160)
        kind = patch.get('kind')
        if entity_id:
            entity = by_id.get(entity_id)
            if entity is None: continue
            # An ID cannot be used to change the identity of another entity.
            if (name and key(name)!=key(entity['name'])) or (kind and kind!=entity.get('kind')): continue
        else:
            if not name or not isinstance(kind,str) or kind not in KINDS: continue
            matches = [e for e in result if e.get('kind')==kind and key(e['name'])==key(name)]
            if len(matches)>1: continue
            entity = matches[0] if matches else None
            if entity is None:
                entity = {'id':uuid4().hex,'name':name,'kind':kind}
                threat = patch.get('threat')
                if kind in {'npc','creature'} and isinstance(threat,str) and threat in THREAT_HP:
                    low, high = THREAT_HP[threat]
                    hp = low + int(entity['id'][:8], 16) % (high-low+1)
                    entity.update(threat=threat,hp=hp,max_hp=hp)
                result.append(entity)
                by_id[entity['id']]=entity
        # Death from the rules engine cannot be silently undone by narration.
        for field, limit in TEXT_FIELDS.items():
            value = text(patch.get(field),limit)
            if value and not (field=='status' and entity.get('hp')==0):
                entity[field] = value
        entity['evidence']=evidence
    return result


def world_context(state, query, limit=12):
    """Small relevant subset, independent of total saved world size."""
    scene = state.get('scene') or {}
    scene = scene if isinstance(scene,dict) else {}
    location = key(scene.get('location'))
    people = scene.get('participants') or []
    people = {key(p) for p in people} if isinstance(people,list) else set()
    words = {w[:5] for w in re.findall(r'\w{3,}',key(query))}
    def score(e):
        name = key(e['name'])
        local = bool(location and (key(e.get('location'))==location or name==location))
        overlap = words & {w[:5] for w in re.findall(r'\w{3,}', name+' '+text(e.get('description'),600).casefold())}
        return 10*int(name in people)+6*int(local)+3*int(bool(name and name in key(query)))+len(overlap)
    ranked = sorted(reversed(records(state)),key=score,reverse=True)
    budget = 3500
    selected = []
    for e in ranked[:limit]:
        entry = {k: text(e.get(k), n) for k,n in {'id':80,'name':160,'kind':30,'description':260,
                 'location':180,'traits':160,'status':120,'threat':30,'form':120,'environment':160}.items() if isinstance(e.get(k),str)}
        entry.update({k:e[k] for k in ('hp','max_hp','mana','max_mana','attitude') if type(e.get(k)) is int})
        size = len(json.dumps(entry,ensure_ascii=False))
        if size <= budget:
            selected.append(entry)
            budget -= size
    return selected


def npc_damage(state, result):
    """Resolve signed damage only against an explicitly bound existing creature/NPC."""
    check = (result or {}).get('check') or {}
    if check.get('kind')!='damage' or check.get('effect') or not check.get('world_target_id'):
        return {}
    total = result.get('total')
    if type(total) is not int: return {}
    target = next((e for e in records(state) if e['id']==check['world_target_id']),None)
    if not target or type(target.get('hp')) is not int or type(target.get('max_hp')) is not int: return {}
    return {'id':target['id'],'name':target['name'],'hp_before':target['hp'],
            'hp_after':max(0,target['hp']-max(0,total)),'max_hp':target['max_hp']}


def bind_damage_target(state, check, proposal):
    if not isinstance(check,dict) or check.get('kind')!='damage' or check.get('effect') or not isinstance(proposal,dict): return
    entity_id = proposal.get('world_target_id')
    target = next((e for e in records(state) if e['id']==entity_id),None)
    if not entity_id and proposal.get('world_target') is True:
        matches = [e for e in records(state) if key(e['name'])==key(check.get('target'))
                   and e.get('kind') in {'npc','creature'} and type(e.get('hp')) is int]
        target = matches[0] if len(matches)==1 else None
    if (target and target.get('kind') in {'npc','creature'} and type(target.get('hp')) is int
            and key(check.get('target'))==key(target['name'])):
        check['world_target_id']=target['id']


def apply_npc_damage(entities, outcome):
    for e in entities:
        if outcome and e['id']==outcome['id']:
            e['hp']=outcome['hp_after']
            if e['hp']==0: e['status']='побеждён (0 HP)'
    return entities


WORLD_PROMPT = '''
ДИНАМИЧЕСКИЙ МИР. Не ограничивай фантазию каталогом. Для новых значимых существ, NPC, предметов, локаций, фракций и способностей верни world_entities: [{kind,name,description,location,traits,status,evidence}]. kind: npc, creature, item, location, faction, ability или other. Для боевого существа можешь при создании указать threat: minor, normal, dangerous, elite, legendary (относительная сила в мире, не подстраивай под героя). HP вычисляет сервер в пределах диапазона силы, не возвращай hp/max_hp. Для двух разных одноимённых существ используй различимые устойчивые имена. Для обновления существующей записи используй точный id из world_entities контекста, не меняй имя и вид. evidence — дословная фраза нового видимого повествования, не фантазия игрока и не пересказ. Не сохраняй неподтверждённые догадки как события. Не воскрешай побеждённых существ без особого сюжетного разрешения. Для новой проверки урона по зарегистрированному существу добавь в pending_check точный world_target_id и target (имя этого существа). Если существо впервые создаётся этим же ходом, вместо ID передай world_target: true и точное имя target. npc_rule_effect — уже вычисленный итог броска по существу, не пересчитывай его. Не объявляй окончательное убийство до разрешения броска. Новые необычные свойства допустимы как сюжетные факты, но не создают автоматическую игровую механику сами по себе.
'''
