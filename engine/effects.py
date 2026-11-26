"""Bounded, declarative effects for generated worlds. Never execute model code.
Amounts are proposals from the GM; Python checks targets, limits and arithmetic.
Durations use the existing campaign clock, not real time.
"""
from copy import deepcopy
import re
from uuid import uuid4
from engine.world import records, merge_entities, key, text, KINDS

STATS = {'str','dex','con','int','wis','cha'}
CONTROLS = {'sleep','paralysis','hypnosis','fear','restrained','stun','silence','other'}
INCAPACITATED = {'sleep': 'сон', 'paralysis': 'паралич', 'stun': 'оглушение'}
PASSIVE_OBSERVATION = re.compile(
    r'(?:осматриваюсь|осмотреться|слушаю|прислушиваюсь|наблюдаю|смотрю)[.!?]?'
    r'|я внимательно осматриваюсь вокруг\. что я вижу\?'
)
MAX_ACTIVE = 40
MAX_EFFECTS = 12
MAX_DURATION = 43200  # 30 game days
LABELS = {'damage':'Урон','heal':'Лечение','modifier':'Модификатор','move':'Перемещение',
          'control':'Контроль','create':'Создание','transform':'Изменение','drain':'Поглощение',
          'environment':'Окружение','schedule':'Отложенный эффект','relation':'Отношение','dispel':'Снятие эффекта'}

class InvalidEffect(ValueError):
    pass

def need(test, message):
    if not test: raise InvalidEffect(message)

def integer(v, low, high):
    need(type(v) is int and low <= v <= high, 'Недопустимое числовое значение')
    return v

def resolve_ref(state, ref):
    if isinstance(ref,str): return ref
    need(isinstance(ref,dict),'Нужен точный ID цели')
    kind=ref.get('kind'); name=key(ref.get('name'))
    need(isinstance(kind,str) and kind in KINDS and name,'Нужны вид и точное имя объекта')
    matches=[e for e in records(state) if e.get('kind')==kind and key(e['name'])==name]
    need(len(matches)==1,'Имя цели не найдено или неоднозначно')
    return matches[0]['id']


def target(state, ident):
    need(isinstance(ident,str), 'Нужен точный ID цели')
    if ident == 'hero': return state
    obj = next((e for e in records(state) if e['id']==ident),None)
    need(obj is not None, 'Цель не найдена')
    return obj

def label(obj):
    return text(obj.get('name'),160) or 'Герой'

def pool(obj, resource):
    need(resource in ('hp','mana'), 'Неизвестный ресурс')
    if resource == 'mana' and 'mana' not in obj and 'max_mana' not in obj:
        # Optional resource: introduced lazily, not an automatic change to old saves.
        need(type(obj.get('max_hp')) is int and obj['max_hp']>0, 'У цели нет ресурса')
        obj['max_mana'] = max(1,min(300,obj['max_hp']))
        obj['mana'] = obj['max_mana']
    maximum=obj.get('max_'+resource); value=obj.get(resource)
    need(type(maximum) is int and type(value) is int and maximum>0 and 0<=value<=maximum, 'У цели нет ресурса')
    return value, maximum

def change_pool(obj, resource, delta):
    before, maximum=pool(obj,resource)
    need(not (resource=='hp' and before==0 and delta>0), 'Обычное лечение не воскрешает')
    after=max(0,min(maximum,before+delta)); obj[resource]=after
    if resource=='hp' and after==0 and obj.get('kind') in ('npc','creature'):
        obj['status']='побеждён (0 HP)'
    return after-before

def active(state):
    return [e for e in state.get('active_effects',[]) if isinstance(e,dict) and isinstance(e.get('id'),str)] if isinstance(state.get('active_effects'),list) else []

def effect_context(state):
    minute=state.get('world_minute')
    entries=active(state)
    return [{k:e[k] for k in ('id','type','target','name','stat','amount','control','expires','next_tick','interval','remaining','operation') if k in e}
            for e in entries[:MAX_ACTIVE] if not (type(minute) is int and type(e.get('expires')) is int and e['expires']<=minute)]

def check_bonus(state, stat):
    if stat not in STATS: return 0
    # Explicitly classified checks only. Modifier is a bonus to a check, not a base stat rewrite.
    total=sum(e.get('amount',0) for e in effect_context(state) if e.get('target')=='hero' and e.get('type')=='modifier' and e.get('stat')==stat and type(e.get('amount')) is int)
    return max(-10,min(10,total))

def hero_incapacitated(state):
    for effect in effect_context(state):
        if effect.get('type') == 'control' and effect.get('target') == 'hero':
            control = effect.get('control')
            if isinstance(control, str) and control in INCAPACITATED:
                return control
    return None

def control_restriction(state, action, *, waiting=False):
    control = hero_incapacitated(state)
    if not control or waiting:
        return None
    if control == 'paralysis' and PASSIVE_OBSERVATION.fullmatch(action.strip().casefold()):
        return None
    return (f'Герой пока не может действовать ({INCAPACITATED[control]}). '
            'Можно подождать с помощью /wait и дать миру возможность отреагировать.')

def _apply(state, p, minute, evidence, blocked_hp):
    typ=p.get('type'); need(isinstance(typ,str) and typ in LABELS,'Неизвестный тип эффекта')
    ident=p.get('target'); name=text(p.get('name'),100) or LABELS[typ]
    if typ=='create':
        patch=p.get('entity'); need(isinstance(patch,dict),'Нет описания объекта')
        kind=patch.get('kind'); nm=text(patch.get('name'),160)
        need(isinstance(kind,str) and kind in KINDS and nm,'Неизвестный вид или имя объекта')
        need(not patch.get('id'),'Создание не изменяет существующий ID')
        matches=[e for e in records(state) if e.get('kind')==kind and key(e['name'])==key(nm)]
        need(len(matches)<=1,'Неоднозначное имя')
        if not matches:
            state['world_entities']=merge_entities(state,[{**patch,'evidence':evidence}],evidence)
            need(any(e.get('kind')==kind and key(e['name'])==key(nm) for e in records(state)),'Объект не создан')
        return f'Создано: {nm}'
    if typ=='dispel':
        eid=p.get('effect_id'); old=active(state); found=next((e for e in old if e['id']==eid),None)
        need(found is not None,'Эффект не найден'); state['active_effects']=[e for e in old if e['id']!=eid]
        return f'Снят эффект: {found.get("name","эффект")}'
    obj=target(state,ident)
    if typ in ('damage','heal'):
        need(ident not in blocked_hp,'HP уже рассчитано подписанным броском')
        amount=integer(p.get('amount'),1,300)
        delta=change_pool(obj,'hp',amount if typ=='heal' else -amount)
        return f'{label(obj)}: {delta:+} HP'
    if typ=='drain':
        resource=p.get('resource'); need(isinstance(resource,str) and resource in ('hp','mana'),'Неизвестный ресурс')
        recipient=p.get('recipient'); need(ident!=recipient,'Нельзя поглощать свой ресурс')
        dest=target(state,recipient)
        if resource=='hp': need(ident not in blocked_hp and recipient not in blocked_hp,'HP уже рассчитано подписанным броском')
        amount=integer(p.get('amount'),1,300)
        source_value,_=pool(obj,resource); dest_value,dest_max=pool(dest,resource)
        amount=min(amount,source_value,dest_max-dest_value)
        if amount:
            change_pool(obj,resource,-amount); change_pool(dest,resource,amount)
        return f'{label(obj)} → {label(dest)}: {amount} {"HP" if resource=="hp" else "маны"}'
    if typ=='move':
        dest=target(state,p.get('destination')); need(dest.get('kind')=='location','Назначение должно быть локацией')
        need(obj is state or obj.get('kind') in ('npc','creature','item'),'Объект нельзя переместить')
        if obj is state:
            from campaign import merge_scene, clock_label
            state['scene']=merge_scene(state.get('scene'),{'location':dest['name']})
            if type(minute) is int: state['scene']['time']=clock_label(minute)
            state['position']='стоит'; state['cover']='нет'
        else: obj['location']=dest['name']; obj['location_id']=dest['id']
        return f'{label(obj)} → {dest["name"]}'
    if typ in ('transform','environment'):
        need(obj is not state or typ=='transform','Окружение должно быть локацией')
        need(typ!='environment' or obj.get('kind')=='location','Окружение должно быть локацией')
        fields=('description','traits','form') if typ=='transform' else ('environment','description','traits')
        updates={k:text(p.get(k),600) for k in fields if text(p.get(k),600)}
        need(updates,'Нет описания изменения')
        if obj is state:
            obj['hero_form']={**(obj.get('hero_form') if isinstance(obj.get('hero_form'),dict) else {}),**updates}
        else: obj.update(updates)
        return f'{LABELS[typ]}: {label(obj)}'
    if typ=='relation':
        need(obj.get('kind') in ('npc','creature','faction'),'Отношение доступно персонажу или фракции')
        amount=integer(p.get('amount'),-30,30); before=obj.get('attitude',0)
        before=before if type(before) is int else 0
        obj['attitude']=max(-100,min(100,before+amount))
        return f'{label(obj)}: отношение {before} → {obj["attitude"]}'
    need(type(minute) is int and minute>=0,'Игровые часы ещё не установлены')
    entries=active(state); need(len(entries)<MAX_ACTIVE,'Слишком много активных эффектов')
    entry={'id':uuid4().hex,'type':typ,'target':ident,'name':name}
    if typ=='modifier':
        stat=p.get('stat'); need(isinstance(stat,str) and stat in STATS,'Неизвестная характеристика')
        entry.update(stat=stat,amount=integer(p.get('amount'),-5,5),expires=minute+integer(p.get('duration'),1,MAX_DURATION))
    elif typ=='control':
        control=p.get('control'); need(isinstance(control,str) and control in CONTROLS,'Неизвестный контроль')
        entry.update(control=control,expires=minute+integer(p.get('duration'),1,MAX_DURATION))
    elif typ=='schedule':
        operation=p.get('operation'); need(operation in ('damage','heal'),'Отложенный эффект: только урон или лечение')
        pool(obj,'hp'); delay=integer(p.get('delay'),1,MAX_DURATION)
        interval=integer(p.get('interval',delay),1,MAX_DURATION); count=integer(p.get('count',1),1,20)
        need(delay+(count-1)*interval<=MAX_DURATION,'Слишком долгая серия')
        entry.update(operation=operation,amount=integer(p.get('amount'),1,100),next_tick=minute+delay,interval=interval,remaining=count)
    else: raise InvalidEffect('Неподдерживаемый эффект')
    # Reapplying the same named timed effect replaces it, rather than stacking indefinitely.
    entries=[e for e in entries if (e.get('target'),e.get('type'),key(e.get('name'))) != (ident,typ,key(name))]
    state['active_effects']=entries+[entry]
    return f'{name}: {label(obj)}'

def advance_effects(state, minute):
    """Resolve all due ticks, at most 20 per entry; remove expired effects."""
    if type(minute) is not int:
        state['active_effects']=active(state)
        return []
    reports=[]; keep=[]
    for e in active(state):
        if type(e.get('expires')) is int and e['expires']<=minute:
            reports.append(f'Истёк эффект: {e.get("name","эффект")}'); continue
        if e.get('type')=='schedule':
            try:
                remaining=integer(e.get('remaining'),1,20); due=integer(e.get('next_tick'),0,10**12)
                interval=integer(e.get('interval'),1,MAX_DURATION); amount=integer(e.get('amount'),1,100)
                operation=e.get('operation'); need(operation in ('damage','heal'),'Неизвестный эффект')
                obj=target(state,e.get('target')); total=0; ticks=0
                while remaining and due<=minute:
                    delta=change_pool(obj,'hp',amount if operation=='heal' else -amount)
                    total+=delta; ticks+=1; remaining-=1; due+=interval
                if ticks: reports.append(f'{e.get("name","Эффект")}: {label(obj)} {total:+} HP ({ticks} сраб.)')
                if not remaining: continue
                e.update(remaining=remaining,next_tick=due)
            except InvalidEffect:
                reports.append(f'Прекращён эффект: {e.get("name","эффект")}'); continue
        keep.append(e)
    state['active_effects']=keep
    return reports

def resolve_effects(state, proposals, reply, minute, blocked_hp=(), hero_before=None):
    """Atomic per proposal, reject without partial writes. Existing clock ticks once per commit."""
    state=deepcopy(state); reports=advance_effects(state,minute); rejected=[]; blocked=set(blocked_hp)
    # Old-style HP delta is a fallback; explicit hero HP effects replace it, not add to it.
    # Preserve independent scheduled ticks when switching to the authoritative base.
    tick_delta=state.get('hp',0)-hero_before[1] if hero_before is not None else 0
    hero_replaced=False
    applied_hp_events = set()
    for i,p in enumerate(proposals[:MAX_EFFECTS] if isinstance(proposals,list) else []):
        try:
            need(isinstance(p,dict),'Нужен объект эффекта')
            evidence=text(p.get('evidence'),600); need(len(evidence)>=5 and evidence in reply,'Нет подтверждения в повествовании')
            trial=deepcopy(state)
            hp_targets=set()
            typ=p.get('type')
            p=dict(p)
            for field in ('target','recipient','destination'):
                if field in p: p[field]=resolve_ref(trial,p[field])
            if typ in ('damage','heal') and isinstance(p.get('target'),str): hp_targets.add(p['target'])
            if typ=='drain' and p.get('resource')=='hp': hp_targets.update(v for v in (p.get('target'),p.get('recipient')) if isinstance(v,str))
            # Separate hits need separate snippets of narration, even with equal damage.
            hp_event = (typ, tuple(sorted(hp_targets)), ' '.join(evidence.casefold().split()))
            if hp_targets:
                need(hp_event not in applied_hp_events, 'Повтор события изменения HP')
            if 'hero' in hp_targets and hero_before is not None and not hero_replaced and 'hero' not in blocked:
                trial['hp']=max(0,min(trial['max_hp'],hero_before[0]+tick_delta))
            report=_apply(trial,p,minute,evidence,blocked)
            state=trial; reports.append(report)
            if hp_targets:
                applied_hp_events.add(hp_event)
            if 'hero' in hp_targets: hero_replaced=True
        except (InvalidEffect,TypeError,KeyError,ValueError) as exc:
            rejected.append(f'Эффект {i+1} не применён: {str(exc) if isinstance(exc,InvalidEffect) else "некорректные данные"}')
    state['effect_log']=(reports+rejected)[:60]
    return state

EFFECT_PROMPT = '''
УНИВЕРСАЛЬНЫЕ ЭФФЕКТЫ: для новых уже совершённых действий можно вернуть effects — массив до 12 объектов. Каждый имеет type, evidence (дословная фраза видимого нового ответа), name (название эффекта). target: "hero" или точный ID существующего объекта из world_entities; для объекта, созданного в этом ходе, допустима ссылка {kind,name}, если имя однозначно. Числа — предложения, сервер проверяет и рассчитывает результат. Не используй эффекты для намерений, ожидания броска, предположений или старых событий. Не повторяй урон/лечение, уже рассчитанные roll_result / rule_effect / npc_rule_effect, или тики active_effects. Каждый самостоятельный удар подкрепляй своей дословной фразой. Если по цели уже есть итог серверного броска урона или лечения, разреши её отдельные удары в следующих ходах. Не называй в тексте предполагаемые итоги HP, числовые бонусы или длительность новых эффектов: их покажет сервер. если effects меняет HP героя, не возвращай hp_delta для этого же действия. Поддерживаются:
damage/heal: target, amount (1..300); обычное лечение не воскрешает. При неопределённом исходе назначь обычный бросок вместо эффекта.
modifier: target, stat (str/dex/con/int/wis/cha), amount (-5..5, бонус проверки), duration (игровые минуты); control: target, control (sleep/paralysis/hypnosis/fear/restrained/stun/silence/other), duration. Для новых проверок героя указывай pending_check.stat, если известна характеристика; не включай бонус modifier в формулу, сервер прибавляет его. Сон, паралич и оглушение ограничивают действия героя: при ожидании описывай инициативу NPC и окружения, не приписывай герою добровольных действий.
move: target, destination (ID открытой локации); create: entity {kind,name,description,location,traits,threat} — произвольные NPC, существа, предметы и локации; уникальное имя; transform: target, form/description/traits (форма героя сохраняется отдельно от расы и характеристик); environment: target (локация), environment/description/traits; drain: target, recipient (ID или hero), resource hp или mana, amount; relation: target (NPC/существо/фракция), amount (-30..30, отношение к герою); schedule: target, operation damage/heal, amount (1..100), delay, interval, count (1..20), всё время в игровых минутах; dispel: effect_id из active_effects.
Можно комбинировать разные типы. Сервер ограничивает числа, длительность до 30 игровых дней, сохраняет эффекты и выполняет тики по игровым часам. Для перемещения героя обнови scene; для незарегистрированных целей сначала создай world_entities или эффект create, затем используй {kind,name} либо известный ID. Если механизм не поддерживается (например, путешествие во времени, новая физика), сохрани как сюжетный факт через facts, не имитируй выполненную числовую операцию. Не возвращай active_effects, effect_log, mana, max_mana или hero_form напрямую. Не обещай конкретный итог, если его ещё должен подтвердить сервер.
'''
