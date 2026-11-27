"""Server-owned coin arithmetic and location-scoped trade offers."""
import re
from copy import deepcopy
from uuid import uuid4

COINS = {"gold": ("зм", 100), "silver": ("см", 10), "copper": ("мм", 1)}
LIMIT = 1_000_000
WORDS = {"одну": 1, "одна": 1, "одной": 1, "один": 1,
         "две": 2, "два": 2, "три": 3, "четыре": 4, "пять": 5, "десять": 10, "сто": 100}
COUNT = r"(?:[1-9][0-9]{0,5}|" + "|".join(WORDS) + ")"
COIN_PHRASE = re.compile(
    rf"\b({COUNT})\s+(золот\w*|серебрян\w*|медн\w*|зм|см|мм)\b", re.I
)
EXCHANGE_INTENT = re.compile(r"размен|обменива|обменя|меняю", re.I)
PAID_COIN = re.compile(
    r"\b(?:отдаю|передаю|плачу|меняю|размениваю|обмениваю|разменяй(?:те)?)\b[^.;!?]{0,90}", re.I
)
EXCHANGE_DONE = re.compile(r"обмен (?:состоялся|заверш[её]н)|разменял[аи]?|"
                           r"\bпринима\w+\b.{1,100}\b(?:отсчитыва\w+|выда[её]т|да[её]т)\b", re.I | re.S)


def wallet(state):
    return {coin: value if type(value := state.get(coin)) is int and value >= 0 else 0
            for coin in COINS}


def wallet_text(state):
    return " · ".join(f"{amount} {COINS[coin][0]}" for coin, amount in wallet(state).items())


def amount(value):
    return type(value) is int and 0 < value <= LIMIT


def grounded(proposal, reply):
    evidence = proposal.get("evidence") if isinstance(proposal, dict) else None
    if not isinstance(evidence, str) or len(evidence.strip()) < 5:
        return False
    return proof_text(evidence) in proof_text(reply) and len(proof_text(evidence)) >= 5


def proof_text(value):
    return " ".join(re.sub(r"[*_`]", "", value).split()).strip(" «»“”\"")


def trade_key(name):
    return " ".join(name.casefold().replace("ё", "е").split()) if isinstance(name, str) else ""


def trade_evidence(proposal, reply, *, quantity=1):
    if not grounded(proposal, reply): return False
    evidence=proposal['evidence'].casefold().replace('ё','е')
    name=trade_key(proposal.get('item'))
    # Inflection-tolerant stem match for meaningful item words, no unrelated quote.
    if not name or not all(word[:max(3,len(word)-2)] in evidence for word in re.findall(r'\w+',name) if len(word)>=3): return False
    if not any(c==proposal.get('currency') and n==proposal.get('price') for c,n in coin_mentions(evidence)): return False
    if not re.search(r'сто[ия]|цен|прода|куп|отда|покуп|предлага|за\s|[—–:]', evidence): return False
    if re.search(r'\b(?:не прода|не куп|не отда|отказ|если|может)\w*',evidence): return False
    if quantity>1 and stated_stock(evidence) != quantity: return False
    return True


STOCK = re.compile(rf"\b({COUNT})\s*(?:шт\.?|штук\w*|единиц\w*|порци\w*|предмет\w*|пайк\w*)", re.I)
EXPLICIT_QUOTE = re.compile(
    rf"(?:^|[«\"\n]|[:.]\s+)([А-Яа-яЁё0-9][^«»\"\n:.,;—–]{{1,75}}?)\s+"
    rf"(?:стоит\s+|[—–]\s*)({COUNT})\s+(золот\w*|серебрян\w*|медн\w*|зм|см|мм)\b", re.I
)


def stated_stock(value):
    match = STOCK.search(value)
    if not match:
        return None
    count = match[1].casefold()
    return int(count) if count.isdigit() else WORDS[count]


def offers_from_story(reply):
    """Recover explicitly priced items from a sign or a merchant's quote."""
    if not re.search(r"торгов|продав|лавк|прилав|таблич|прайс", reply, re.I):
        return []
    quotes = []
    clean = proof_text(reply)
    for match in EXPLICIT_QUOTE.finditer(clean):
        name = match[1].strip()
        price = coin_mentions(match[0])
        if not price or len(name.split()) > 6 or re.search(r"\b(?:если|может|не|доступно|нужно)\b", name, re.I):
            continue
        tail = clean[match.end():match.end()+125]
        stock_match = re.match(
            r'^[»" ,;]*(?:[.!?]\s*)?(?:(?:торговец|продавец|он|она)\s+подтверждает,?\s+что\s+)?'
            r'(?:в наличии|у него есть|у не[её] есть|осталось)\s+([^.!?;]{1,48})', tail, re.I
        )
        stock = stated_stock(stock_match[1]) if stock_match else None
        evidence = clean[match.start():match.end() + (stock_match.end() if stock_match else 0)].strip(' «»":')
        currency, amount = price[0]
        quotes.append({"item": name, "currency": currency, "price": amount, "stock": stock or 1,
                       "stock_explicit": stock is not None, "evidence": evidence})
    return quotes[:6]


PURCHASE_CANCELLED = re.compile(
    r'\b(?:покупка\s+не\s+состоялась|сделка\s+(?:не\s+состоялась|отменена)|'
    r'ты\s+отказываешься|отказ(?:ался|алась|ываюсь)\s+от\s+покупки|'
    r'не\s+(?:покупаешь|покупаю|купил[аи]?))\b', re.I
)
PURCHASE_COMPLETE = re.compile(
    r'\b(?:покупк\w* состоял\w*|сделка состоял\w*|покупа(?:ешь|ет|ю)|'
    r'купил[аи]?|отда[лёт][аи]?|отдал[аи]?|переда(?:л[аи]?|[её]т)|'
    r'оплатил[аи]?|продал[аи]?|принима\w* монет\w*)\b', re.I
)


def confirmed_purchase(proposal, reply):
    if not isinstance(proposal, dict) or PURCHASE_CANCELLED.search(reply):
        return None
    if not PURCHASE_COMPLETE.search(reply):
        return None
    quantity = proposal.get('quantity', 1)
    quantity = quantity if type(quantity) is int else 1
    candidates = [proposal, *({**proposal, 'evidence': sentence}
                              for sentence in re.split(r'(?<=[.!?])\s+|\n+', reply))]
    return next((candidate for candidate in candidates
                 if trade_evidence(candidate, reply, quantity=quantity)), None)


def settle_offer(state, offer, quantity):
    if not amount(quantity) or quantity>99: raise ValueError('Количество от 1 до 99.')
    stock=offer.get('stock',1)
    if type(stock) is int and quantity>stock: raise ValueError('У продавца недостаточно товара.')
    result=apply_purchase(state,{**offer,'price':offer['price']*quantity,'quantity':quantity})
    if type(stock) is int:
        for saved in state.get('shop_offers',[]):
            if (saved.get('offer_id') and saved.get('offer_id')==offer.get('offer_id')) or (not offer.get('offer_id') and all(saved.get(k)==offer.get(k) for k in ('location','item','seller_id','price','currency'))):
                saved['stock']=stock-quantity
    return result

def apply_purchase(state, change):
    currency, price, quantity = (change.get(k) for k in ("currency", "price", "quantity"))
    name = change.get("item")
    if (not isinstance(currency, str) or currency not in COINS or not amount(price)
            or not amount(quantity) or quantity > 99 or not trade_key(name) or len(name) > 160):
        raise ValueError("Проверьте цену, валюту и количество покупки.")
    balance = wallet(state)
    if balance[currency] < price:
        raise ValueError(f"Покупка не состоялась. Нужно {price} {COINS[currency][0]}, "
                         f"в кошельке {balance[currency]} {COINS[currency][0]}.")
    inventory = deepcopy(state.get("inventory")) if isinstance(state.get("inventory"), list) else []
    for index, entry in enumerate(inventory):
        if trade_key(entry.get("name") if isinstance(entry, dict) else entry) == trade_key(name):
            item = entry if isinstance(entry, dict) else {"name": entry}
            count = item.get("quantity", 1)
            item["quantity"] = (count if type(count) is int and count > 0 else 1) + quantity
            inventory[index] = item
            break
    else:
        inventory.append({"name": name.strip(), "quantity": quantity})
    state.update(inventory=inventory, **{currency: balance[currency] - price})
    return f"Куплено: {name.strip()} ×{quantity} за {price} {COINS[currency][0]}."


def apply_exchange(state, change):
    source, target, paid = (change.get(k) for k in ("from", "to", "amount"))
    if (not isinstance(source, str) or not isinstance(target, str) or source not in COINS
            or target not in COINS or source == target or not amount(paid)):
        raise ValueError("Проверьте валюты и сумму размена.")
    received, remainder = divmod(paid * COINS[source][1], COINS[target][1])
    if remainder or not amount(received):
        raise ValueError("Эту сумму нельзя разменять без остатка.")
    balance = wallet(state)
    if balance[source] < paid:
        raise ValueError(f"Размен не состоялся. В кошельке {balance[source]} {COINS[source][0]}.")
    state.update({source: balance[source] - paid, target: balance[target] + received})
    return f"Размен выполнен: {paid} {COINS[source][0]} → {received} {COINS[target][0]}."


def coin_mentions(content):
    found = []
    for match in COIN_PHRASE.finditer(content.casefold().replace("*", "")):
        count, word = match.groups()
        coin = next(key for key, (label, _) in COINS.items()
                    if word == label or word.startswith({"gold": "золот", "silver": "серебрян", "copper": "медн"}[key]))
        found.append((coin, int(count) if count.isdigit() else WORDS[count]))
    return list(dict.fromkeys(found))


def exchange_from_story(action, reply):
    """Old prose-only turns still settle an explicit, confirmed two-coin exchange."""
    if not EXCHANGE_INTENT.search(action) or not EXCHANGE_DONE.search(reply):
        return None
    expressed = PAID_COIN.search(action)
    if not expressed or re.search(r"\bне\s*$", action[:expressed.start()], re.I):
        return None
    paid = coin_mentions(expressed[0])[:1]
    credited = coin_mentions(reply)
    pairs = [(source, quantity, target, count) for source, quantity in paid
             for target, count in credited if source != target and
             quantity * COINS[source][1] == count * COINS[target][1]]
    if len(pairs) != 1:
        return None
    source, quantity, target, _ = pairs[0]
    return {"from": source, "to": target, "amount": quantity}


def current_offers(state, *, include_sold_out=False):
    scene = state.get("scene")
    location = trade_key(scene.get("location")) if isinstance(scene, dict) else ""
    raw = state.get("shop_offers")
    if not location or not isinstance(raw, list):
        return []
    from engine.world import records, life_state
    local_sellers={e['id'] for e in records(state) if e.get('kind')=='npc' and life_state(e)=='alive' and trade_key(e.get('location'))==location}
    return [offer for offer in raw if isinstance(offer, dict)
            and trade_key(offer.get("location")) == location and trade_key(offer.get("item"))
            and isinstance(offer.get("currency"), str) and offer["currency"] in COINS
            and amount(offer.get("price"))
            and (amount(offer.get("stock",1)) or
                 include_sold_out and type(offer.get("stock")) is int and offer["stock"] == 0)
            and (not offer.get('seller_id') or (isinstance(offer.get('seller_id'),str) and offer['seller_id'] in local_sellers))][:20]


def merge_offers(state, proposed, reply):
    offers = [offer for offer in state.get("shop_offers", []) if isinstance(offer, dict)] if isinstance(state.get("shop_offers"), list) else []
    scene = state.get("scene")
    location = scene.get("location") if isinstance(scene, dict) else ""
    if not trade_key(location):
        return offers
    inferred = offers_from_story(reply)
    proposals = proposed[:10] if isinstance(proposed, list) else []
    accepted = set()
    for quote in [*proposals, *inferred]:
        if not trade_evidence(quote, reply):
            continue
        name, coin, price = (quote.get(k) for k in ("item", "currency", "price"))
        if (not trade_key(name) or len(name) > 160 or not isinstance(coin, str)
                or coin not in COINS or not amount(price)):
            continue
        seller_id=quote.get('seller_id')
        if seller_id is not None and not isinstance(seller_id,str): continue
        if seller_id:
            from engine.world import records, life_state
            seller=next((e for e in records(state) if e['id']==seller_id and e.get('kind')=='npc' and life_state(e)=='alive' and trade_key(e.get('location'))==trade_key(location)),None)
            if not seller or trade_key(seller['name']) not in trade_key(quote['evidence']): continue
        stock = quote.get('stock', 1)
        if not stated_stock(quote['evidence']):
            corroboration = [extra for extra in inferred if extra['stock_explicit']
                             and extra['price'] == price and extra['currency'] == coin
                             and trade_key(extra['item']) == trade_key(name)
                             and proof_text(quote['evidence']) in proof_text(extra['evidence'])]
            if len(corroboration) == 1:
                stock = corroboration[0]['stock']
                quote = {**quote, 'evidence': corroboration[0]['evidence'], 'stock_explicit': True}
        if type(stock) is not int or not 1<=stock<=99: continue
        if stock>1 and stated_stock(quote['evidence']) != stock: continue
        identity = (trade_key(location), trade_key(name),seller_id)
        if not seller_id and (identity[:2] in accepted or any(
                trade_key(old.get("location")) == identity[0] and trade_key(old.get("item")) == identity[1]
                and old.get("seller_id") for old in offers)):
            continue
        previous = next((old for old in offers if (trade_key(old.get("location")), trade_key(old.get("item")),
                                                   old.get("seller_id")) == identity), None)
        if previous and not quote.get('stock_explicit') and not re.search(
                r"\b(?:в наличии|есть|остал\w*|поступил\w*|запас)\b", quote['evidence'], re.I):
            stock = previous.get('stock', stock)
        offers = [old for old in offers if (trade_key(old.get("location")), trade_key(old.get("item")),old.get("seller_id")) != identity]
        offers.append({"location": location, "item": name.strip(), "currency": coin, "price": price,
                       "seller_id":seller_id,"stock":stock,"offer_id":(previous or {}).get('offer_id') or uuid4().hex})
        accepted.add(identity[:2])
    return offers[-100:]


TRADE_PROMPT = """
Торговля: подтверждённый размен передай как exchange:{from,to,amount,evidence}, где amount — сколько монет отдал герой, from/to — gold/silver/copper. Курс 1 зм = 10 см = 100 мм, полученную сумму считает сервер. Названную за единицу цену сохрани в shop_offers:[{item,currency,price,seller_id,stock,evidence}]. evidence содержит название товара, продавца и цену; stock — доступное количество. При покупке используй сохранённую цену, quantity и кошелёк; не объявляй оплату, если нужного номинала не хватает. Для сверки кошелька используй только лист. Успешную покупку передай через purchase и не дублируй кошелёк или инвентарь.
"""
