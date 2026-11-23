"""
Генерация портрета персонажа: цепочка провайдеров с фолбэками.

Рассчитано на несколько одновременных игроков:
  • дисковый кэш  — общий для всех воркеров, портрет считается один раз за жизнь персонажа
  • дедупликация  — одновременные запросы одного персонажа ждут одну генерацию
  • семафор       — не более AVATAR_MAX_CONCURRENT обращений к провайдерам разом
  • лимит частоты — на IP, чтобы «перегенерировать» не превращалось в спам

Порядок задаётся AVATAR_PROVIDERS (по умолчанию: cloudflare,pollinations,static).
Доступные: local, hf, horde, pollinations, perchance, bing, cloudflare, static.

Промпт всегда английский: кириллицу модели генерации не знают и пытаются
нарисовать её буквами. Имя персонажа остаётся только в seed.
"""
import base64
import hashlib
import json
import os
import random
import re
import threading
import time
import urllib.parse
from pathlib import Path

import requests

# ─── Конфиг ────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent

SIZE = int(os.getenv("AVATAR_SIZE", "512"))
TIMEOUT = float(os.getenv("AVATAR_TIMEOUT", "60"))
HORDE_TIMEOUT = float(os.getenv("AVATAR_HORDE_TIMEOUT", "240"))

PROVIDERS = [p.strip() for p in os.getenv(
    "AVATAR_PROVIDERS", "cloudflare,pollinations,static"
).split(",") if p.strip()]

# Относительный путь считается от папки проекта, а не от текущего каталога —
# иначе gunicorn/systemd с другим WorkingDirectory потеряет кэш.
_raw_cache = os.getenv("AVATAR_CACHE_DIR") or "saves/avatars"
_cache_path = Path(_raw_cache)
CACHE_DIR = (_cache_path if _cache_path.is_absolute() else BASE_DIR / _cache_path).resolve()

MAX_CONCURRENT = max(1, int(os.getenv("AVATAR_MAX_CONCURRENT", "2")))
RATE_LIMIT = max(0, int(os.getenv("AVATAR_RATE_LIMIT", "20")))
RATE_WINDOW = float(os.getenv("AVATAR_RATE_WINDOW", "3600"))
MEM_CACHE_MAX = int(os.getenv("AVATAR_MEM_CACHE", "64"))

STYLE = ("detailed digital painting, painterly style, muted colours, "
         "dramatic rim lighting, dark atmospheric background")
NEGATIVE = ("text, watermark, signature, logo, frame, border, ui, multiple people, "
            "deformed hands, extra limbs, low quality, jpeg artifacts, blurry")

RACE_HINTS = {
    "человек": "human",
    "эльф": "elf with pointed ears and slender features",
    "полуэльф": "half-elf with subtly pointed ears",
    "дворф": "dwarf, stocky, with a braided beard",
    "полурослик": "halfling, small and nimble, with curly hair",
    "гном": "gnome with wild hair and goggles",
    "полуорк": "half-orc with small tusks and a heavy brow",
    "тифлинг": "tiefling with curved horns and a long tail",
    "драконорождённый": "dragonborn with a scaled hide and blunt snout",
    "гоблин": "goblin with a wide grin and large ears",
    "кобольд": "kobold, small and reptilian, with a snout and tail",
    "орк": "orc with tusks and war paint",
}

CLASS_HINTS = {
    "воин": "a battle-worn knight in battered plate armour",
    "варвар": "a barbarian in fur and hide, with tribal tattoos",
    "паладин": "a paladin in polished armour bearing a holy symbol",
    "плут": "a rogue in a hood, daggers at the belt",
    "следопыт": "a ranger in a weather-beaten cloak, a bow over the shoulder",
    "монах": "a monk in simple robes with wrapped hands",
    "маг": "a mage in layered robes, an arcane focus glowing",
    "жрец": "a cleric in chainmail with a temple sigil",
    "друид": "a druid in mossy robes with an antler charm",
    "бард": "a bard with a lute and a flamboyant hat",
    "колдун": "a warlock with a pact mark and a dark gaze",
    "чародей": "a sorcerer with faint arcane light in the palms",
}

CONDITION_HINTS = {
    "ранен": "bloodied, with torn clothes and fresh cuts",
    "изранен": "heavily wounded, wrapped in bandages",
    "отравлен": "sallow-skinned, with dark veins at the temples",
    "испуган": "wide-eyed and tense",
    "проклят": "with a faint unnatural glow in the eyes",
    "устал": "exhausted, with shadows under the eyes",
}


# ─── Промпт и seed ─────────────────────────────────────────────────────────
def _clean(text, limit=200):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit]


def _identity(state):
    return "|".join(_clean(state.get(k, ""), 60)
                    for k in ("name", "race", "class", "background"))


def _seed(state):
    """Детерминированный seed: один персонаж → один и тот же портрет.

    Имя входит именно сюда, а не в текст промпта.
    """
    digest = hashlib.sha256(_identity(state).encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _archetype(state):
    """Английское описание облика по классу, либо по главной характеристике."""
    cls = (state.get("class") or "").lower()
    for key, val in CLASS_HINTS.items():
        if key in cls:
            return val
    stats = state.get("stats") or {}
    table = [("str", "a battle-worn warrior"),
             ("dex", "a lithe rogue in a hood"),
             ("int", "a scholar-mage with an arcane focus"),
             ("wis", "a temple cleric"),
             ("cha", "a charismatic bard in fine clothes")]
    return max(table, key=lambda kv: stats.get(kv[0], 10))[1]


def build_prompt(state):
    """Промпт для FLUX: связная фраза на английском, без кириллицы.

    Кириллицу модели генерации не знают — они либо игнорируют её, либо
    пытаются нарисовать буквами. Поэтому имя в текст не попадает.
    Список тегов через запятую тоже не подходит: FLUX обучен на связных
    описаниях и на рубленый список реагирует хуже, чем SDXL.
    """
    race_raw = _clean(state.get("race"), 40).lower()
    race = RACE_HINTS.get(race_raw, "")
    cls = _archetype(state)

    subject_parts = [p for p in (race, cls) if p]
    for cond in (state.get("conditions") or [])[:2]:
        hint = CONDITION_HINTS.get(_clean(cond, 40).lower())
        if hint:
            subject_parts.append(hint)
    subject = ", ".join(subject_parts) or "a wandering adventurer"

    return (
        f"A fantasy RPG character portrait of {subject}. "
        f"Head and shoulders, centered composition, looking at the viewer. "
        f"Dark atmospheric background, dramatic rim lighting from one side. "
        f"Detailed digital painting, painterly style, muted colours. "
        f"Single character, no text."
    )[:900]


# ─── Утилиты ───────────────────────────────────────────────────────────────
def _b64_to_bytes(raw):
    raw = (raw or "").strip()
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    return base64.b64decode(raw)


def _sniff_mime(data):
    """Определяет тип картинки по сигнатуре файла."""
    if not data:
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:5] == b"<?xml" or data[:4] == b"<svg":
        return "image/svg+xml"
    return "image/jpeg"


def _mime_to_ext(mime):
    mime = (mime or "").lower()
    if "png" in mime:
        return "png"
    if "webp" in mime:
        return "webp"
    if "svg" in mime:
        return "svg"
    return "jpg"


# ─── Провайдеры ────────────────────────────────────────────────────────────
def gen_cloudflare(prompt, seed, size):
    """Cloudflare Workers AI — FLUX.1-schnell.

    Бесплатный тариф: ~10 000 нейронов в сутки, одна картинка 512×512
    стоит порядка 20–25 нейронов, то есть 300–500 портретов в день.

    Нужны CF_ACCOUNT_ID (Account ID со страницы Workers AI) и CF_API_TOKEN
    (токен с шаблоном «Workers AI»).
    """
    account = (os.getenv("CF_ACCOUNT_ID") or "").strip()
    token = (os.getenv("CF_API_TOKEN") or "").strip()
    if not account or not token:
        raise RuntimeError("CF_ACCOUNT_ID / CF_API_TOKEN не заданы")

    model = os.getenv("CF_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell")
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"

    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json={"prompt": prompt, "steps": 4, "seed": seed},
        timeout=TIMEOUT)

    if r.status_code == 401:
        raise RuntimeError("Cloudflare: неверный CF_API_TOKEN")
    if r.status_code == 403:
        raise RuntimeError("Cloudflare: токену не выдан доступ к Workers AI")
    if r.status_code == 429:
        raise RuntimeError("Cloudflare: суточная квота исчерпана")

    mime = r.headers.get("Content-Type", "")
    if r.status_code == 200 and mime.startswith("image/"):
        return r.content, mime            # сырая картинка

    # Иначе JSON вида {"result": {"image": "<base64>"}, "success": true}
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"Cloudflare {r.status_code}: {r.text[:180]}")

    if not data.get("success"):
        raise RuntimeError(f"Cloudflare: {str(data.get('errors'))[:180]}")

    b64 = (data.get("result") or {}).get("image")
    if not b64:
        raise RuntimeError("Cloudflare: в ответе нет изображения")
    raw = _b64_to_bytes(b64)
    return raw, _sniff_mime(raw)


def gen_local(prompt, seed, size):
    """Локальный Stable Diffusion: A1111 / Forge / SD.Next, иначе Fooocus-API."""
    base = (os.getenv("AVATAR_LOCAL_URL") or "").rstrip("/")
    if not base:
        raise RuntimeError("AVATAR_LOCAL_URL не задан")

    # 1) A1111-совместимый API
    try:
        payload = {
            "prompt": prompt, "negative_prompt": NEGATIVE,
            "width": size, "height": size,
            "steps": int(os.getenv("AVATAR_LOCAL_STEPS", "26")),
            "cfg_scale": float(os.getenv("AVATAR_LOCAL_CFG", "6.0")),
            "sampler_name": os.getenv("AVATAR_LOCAL_SAMPLER", "DPM++ 2M Karras"),
            "seed": seed, "batch_size": 1, "save_images": False,
        }
        r = requests.post(f"{base}/sdapi/v1/txt2img", json=payload, timeout=TIMEOUT)
        if r.status_code == 404:
            raise FileNotFoundError
        r.raise_for_status()
        images = (r.json() or {}).get("images") or []
        if not images:
            raise RuntimeError("локальный SD вернул пустой ответ")
        return _b64_to_bytes(images[0]), "image/png"
    except FileNotFoundError:
        pass  # не A1111 — пробуем Fooocus

    # 2) Fooocus-API
    r = requests.post(
        f"{base}/v1/generation/text-to-image",
        json={"prompt": prompt, "negative_prompt": NEGATIVE,
              "image_number": 1, "async_process": False, "require_base64": True,
              "performance_selection": "Speed",
              "style_selections": ["Fooocus V2", "Fooocus Enhance"]},
        timeout=TIMEOUT)
    r.raise_for_status()
    items = r.json()
    if not isinstance(items, list) or not items:
        raise RuntimeError("Fooocus вернул пустой ответ")
    b64 = items[0].get("base64")
    if not b64:
        url = items[0].get("url")
        if not url:
            raise RuntimeError("Fooocus: нет base64 и нет url")
        return requests.get(url, timeout=60).content, "image/png"
    return _b64_to_bytes(b64), "image/png"


def gen_hf(prompt, seed, size):
    """Hugging Face Inference (нужен бесплатный HF_TOKEN)."""
    token = os.getenv("HF_TOKEN")
    if not token:
        raise RuntimeError("HF_TOKEN не задан")
    model = os.getenv("HF_IMAGE_MODEL", "black-forest-labs/FLUX.1-schnell")
    url = f"https://router.huggingface.co/hf-inference/models/{model}"
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "image/png"},
        json={"inputs": prompt,
              "parameters": {"width": size, "height": size, "seed": seed}},
        timeout=TIMEOUT)
    if r.status_code == 503:
        raise RuntimeError("модель HF прогревается, попробуйте позже")
    if r.status_code == 401:
        raise RuntimeError("HF_TOKEN недействителен")
    r.raise_for_status()
    mime = r.headers.get("Content-Type", "image/png")
    if not mime.startswith("image/"):
        raise RuntimeError(f"HF ответил не картинкой: {r.text[:180]}")
    return r.content, mime


def gen_horde(prompt, seed, size):
    """Stable Horde — бесплатно и без ключа, но с очередью (может быть минуты)."""
    api = "https://stablehorde.net/api/v2"
    headers = {"apikey": os.getenv("HORDE_KEY", "0000000000"),
               "Client-Agent": "rpg-ai-master:1.0:local"}
    models = [m.strip() for m in os.getenv("HORDE_MODELS", "").split(",") if m.strip()]
    body = {
        "prompt": prompt,
        "params": {"width": size, "height": size, "steps": 22, "cfg_scale": 6.5,
                   "sampler_name": "k_euler_a", "karras": True, "n": 1,
                   "seed": str(seed)},
        "nsfw": False, "censor_nsfw": True, "trusted_workers": False,
        "slow_workers": True, "r2": True,
    }
    if models:
        body["models"] = models

    r = requests.post(f"{api}/generate/async", headers=headers, json=body, timeout=30)
    r.raise_for_status()
    job_id = (r.json() or {}).get("id")
    if not job_id:
        raise RuntimeError(f"Horde: {r.text[:180]}")

    deadline = time.time() + HORDE_TIMEOUT
    while time.time() < deadline:
        time.sleep(4)
        chk = requests.get(f"{api}/generate/check/{job_id}", timeout=20).json()
        if chk.get("faulted"):
            raise RuntimeError("Horde: задача упала на воркере")
        if chk.get("done"):
            break
    else:
        try:
            requests.delete(f"{api}/generate/status/{job_id}", headers=headers, timeout=20)
        except requests.RequestException:
            pass
        raise RuntimeError("Horde: таймаут очереди")

    status = requests.get(f"{api}/generate/status/{job_id}", timeout=30).json()
    gens = status.get("generations") or []
    if not gens:
        raise RuntimeError("Horde: пустой результат")
    img = gens[0].get("img") or ""
    if not img:
        raise RuntimeError("Horde: нет данных изображения")
    if img.startswith("http"):
        return requests.get(img, timeout=90).content, "image/webp"
    return _b64_to_bytes(img), "image/webp"


def gen_pollinations(prompt, seed, size):
    """Pollinations. Без private/safe — на анонимном уровне они ломают запрос.

    Модели перебираются по списку POLLINATIONS_MODEL (например, flux,turbo):
    если первая недоступна для текущего уровня доступа, берётся следующая.
    """
    token = (os.getenv("POLLINATIONS_TOKEN") or "").strip()
    models = [m.strip() for m in
              os.getenv("POLLINATIONS_MODEL", "flux,turbo").split(",") if m.strip()]

    last_error = None
    for model in models:
        params = {
            "width": size,
            "height": size,
            "seed": seed,
            "model": model,
            "nologo": "true",
        }
        if token:
            params["token"] = token

        url = ("https://image.pollinations.ai/prompt/"
               + urllib.parse.quote(prompt, safe="")
               + "?" + urllib.parse.urlencode(params))
        try:
            r = requests.get(
                url,
                timeout=min(TIMEOUT, 60),
                headers={"User-Agent": "rpg-ai-master/1.0",
                         "Referer": "https://github.com/"},
            )
            if r.status_code == 402:
                raise RuntimeError("402 — нужен POLLINATIONS_TOKEN")
            if r.status_code == 429:
                raise RuntimeError("429 — лимит запросов, попробуйте позже")
            r.raise_for_status()
            mime = r.headers.get("Content-Type", "")
            if not mime.startswith("image/"):
                raise RuntimeError(f"{r.status_code}: {r.text[:180]}")
            return r.content, mime
        except Exception as exc:
            last_error = exc

    raise last_error or RuntimeError("Pollinations недоступен")


def gen_perchance(prompt, seed, size):
    """
    Неофициальный адаптер Perchance.
    Требует PERCHANCE_USER_KEY (UUID из localStorage на perchance.org).
    Свежий adAccessCode выдаётся не всегда — возможна ошибка invalid_ad_access_code.
    """
    user_key = os.getenv("PERCHANCE_USER_KEY")
    if not user_key:
        raise RuntimeError("PERCHANCE_USER_KEY не задан")
    channel = os.getenv("PERCHANCE_CHANNEL", "ai-character-generator")
    sub = os.getenv("PERCHANCE_SUBCHANNEL", "")
    req_id = str(random.randint(10 ** 11, 10 ** 12 - 1))
    bust = str(int(time.time() * 1000))

    ad = requests.get(
        "https://image-generation.perchance.org/api/checkAdAccess",
        params={"userKey": user_key, "requestId": req_id, "channel": channel,
                "subChannel": sub, "__cacheBust": bust},
        timeout=30).json()
    code = ad.get("adAccessCode")
    if not code:
        raise RuntimeError(f"Perchance: нет adAccessCode ({str(ad)[:140]})")

    r = requests.get(
        "https://image-generation.perchance.org/api/generate",
        params={"prompt": prompt, "seed": str(seed),
                "resolution": f"{size}x{size}", "channel": channel,
                "subChannel": sub, "userKey": user_key, "adAccessCode": code,
                "requestId": req_id, "__cacheBust": bust},
        timeout=TIMEOUT)
    data = r.json()
    if data.get("status") != "success":
        raise RuntimeError(f"Perchance: {str(data)[:180]}")
    return requests.get(data["imageUrl"], timeout=60).content, "image/jpeg"


def gen_bing(prompt, seed, size):
    """
    Bing Image Creator через Playwright (единственный рабочий путь).
    Нужен BING_COOKIE_U — значение cookie _U с bing.com после входа в аккаунт.
    Неофициально и против ToS; лимит ~15 быстрых генераций.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("нужен пакет playwright + `playwright install chromium`") from exc

    cookie = os.getenv("BING_COOKIE_U")
    if not cookie:
        raise RuntimeError("BING_COOKIE_U не задан")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(viewport={"width": 1280, "height": 900},
                                      locale="en-US")
            ctx.add_cookies([{"name": "_U", "value": cookie,
                              "domain": ".bing.com", "path": "/"}])
            page = ctx.new_page()
            page.goto("https://www.bing.com/images/create",
                      wait_until="domcontentloaded", timeout=60000)
            page.fill("textarea", prompt)
            page.keyboard.press("Enter")
            page.wait_for_selector("img.mimg", timeout=120000)
            src = page.get_attribute("img.mimg", "src")
            if not src:
                raise RuntimeError("Bing: не найден src изображения")
            return requests.get(src, timeout=90).content, "image/jpeg"
        finally:
            browser.close()


def gen_static(state, size, extra_seed=None):
    """Запасной аватар DiceBear.

    extra_seed задаётся при перегенерации, чтобы «Сгенерировать заново»
    действительно меняло картинку — иначе seed берётся от имени и всегда
    даёт одно и то же лицо.
    """
    base = _identity(state) or "adventurer"
    if extra_seed is not None:
        base = f"{base}|{extra_seed}"
    style = os.getenv("AVATAR_STATIC_STYLE", "lorelei")

    url = (f"https://api.dicebear.com/9.x/{style}/svg"
           f"?seed={urllib.parse.quote(base)}"
           f"&size={size}"
           f"&backgroundType=gradientLinear"
           f"&backgroundColor=24201d,1a1614,2a2420")

    # У adventurer очки и хмурый рот включены по умолчанию — гасим.
    # DiceBear игнорирует неизвестные параметры, так что вреда не будет.
    if style == "adventurer":
        url += ("&glassesProbability=0"
                "&earringsProbability=0"
                "&featuresProbability=0")

    return url, "image/svg+xml"


PROVIDER_FUNCS = {
    "cloudflare": gen_cloudflare,
    "local": gen_local,
    "hf": gen_hf,
    "horde": gen_horde,
    "pollinations": gen_pollinations,
    "perchance": gen_perchance,
    "bing": gen_bing,
}


# ─── Слой для нескольких игроков ───────────────────────────────────────────
_SEM = threading.BoundedSemaphore(MAX_CONCURRENT)   # слоты на сетевые вызовы
_MEM = {}                                            # кэш в памяти процесса
_MEM_LOCK = threading.Lock()
_INFLIGHT = {}                                       # дедупликация
_INFLIGHT_LOCK = threading.Lock()
_RATE = {}                                           # лимит частоты на IP
_RATE_LOCK = threading.Lock()


def rate_limit_ok(ip):
    """True — запрос разрешён. Скользящее окно, чистится по ходу."""
    if RATE_LIMIT <= 0:
        return True
    now = time.time()
    with _RATE_LOCK:
        hits = [t for t in _RATE.get(ip, []) if now - t < RATE_WINDOW]
        if len(hits) >= RATE_LIMIT:
            _RATE[ip] = hits
            return False
        hits.append(now)
        _RATE[ip] = hits
        if len(_RATE) > 512:
            for k in [k for k, v in _RATE.items()
                      if not v or now - v[-1] > RATE_WINDOW]:
                _RATE.pop(k, None)
    return True


def _cache_key(state):
    raw = f"{_identity(state)}|{SIZE}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _disk_read(state):
    key = _cache_key(state)
    try:
        meta = json.loads((CACHE_DIR / f"{key}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    # Фолбэк не считается кэшем: иначе DiceBear залипнет навсегда,
    # и настоящий портрет уже никогда не запросится.
    if meta.get("provider") == "static":
        return None
    ext = meta.get("ext")
    if ext:
        if not (CACHE_DIR / f"{key}.{ext}").exists():
            return None
        meta["url"] = f"/api/avatar/file/{key}.{ext}"
    elif not meta.get("url"):
        return None
    meta["ok"] = True
    meta["cached"] = True
    return meta


def _atomic_write(path, data, binary=False):
    """Через временный файл: читатель никогда не увидит половину."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    if binary:
        tmp.write_bytes(data)
    else:
        tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def _disk_write(state, data, mime, seed, provider, prompt):
    key = _cache_key(state)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ext = _mime_to_ext(mime)
    _atomic_write(CACHE_DIR / f"{key}.{ext}", data, binary=True)
    _atomic_write(CACHE_DIR / f"{key}.json",
                  json.dumps({"provider": provider, "mime": mime, "ext": ext,
                              "seed": seed, "prompt": prompt}, ensure_ascii=False))
    return f"/api/avatar/file/{key}.{ext}"


def _remember(key, result):
    with _MEM_LOCK:
        if len(_MEM) >= MEM_CACHE_MAX:
            _MEM.pop(next(iter(_MEM)))
        _MEM[key] = result


class _Flight:
    """Одна генерация на всех, кто попросил то же самое одновременно."""
    __slots__ = ("event", "result")

    def __init__(self):
        self.event = threading.Event()
        self.result = None


def _coalesce(key):
    """Возвращает (flight, is_leader)."""
    with _INFLIGHT_LOCK:
        flight = _INFLIGHT.get(key)
        if flight is not None:
            return flight, False
        flight = _Flight()
        _INFLIGHT[key] = flight
        return flight, True


def _settle(key, flight, result):
    with _INFLIGHT_LOCK:
        _INFLIGHT.pop(key, None)
    flight.result = result
    flight.event.set()


def _render(state, key, force):
    """Реальный проход по цепочке провайдеров."""
    prompt = build_prompt(state)
    seed = random.randint(1, 2 ** 31 - 1) if force else _seed(state)
    errors = {}

    for name in PROVIDERS:
        try:
            if name == "static":
                url, _ = gen_static(state, SIZE,
                                    extra_seed=(seed if force else None))
                # На диск не пишем: фолбэк должен пересматриваться при
                # следующем запросе, а не закрепляться навсегда.
                result = {"ok": True, "provider": "static", "url": url,
                          "seed": seed, "prompt": prompt}
            else:
                func = PROVIDER_FUNCS.get(name)
                if func is None:
                    raise RuntimeError("неизвестный провайдер")
                with _SEM:          # слот только на время сетевого вызова
                    data, mime = func(prompt, seed, SIZE)
                if not data:
                    raise RuntimeError("пустой ответ")
                url = _disk_write(state, data, mime, seed, name, prompt)
                result = {"ok": True, "provider": name, "seed": seed,
                          "prompt": prompt, "mime": mime, "url": url}
            _remember(key, result)
            return result
        except Exception as exc:  # любой сбой = идём к следующему
            errors[name] = f"{type(exc).__name__}: {exc}"[:200]

    return {"ok": False, "provider": None, "seed": seed, "prompt": prompt,
            "errors": errors}


def generate_avatar(state, force=False):
    state = state if isinstance(state, dict) else {}
    key = f"{_identity(state)}|{SIZE}"

    if not force:
        with _MEM_LOCK:
            hit = _MEM.get(key)
        if hit:
            return hit

        hit = _disk_read(state)
        if hit:
            _remember(key, hit)
            return hit

        flight, leader = _coalesce(key)
        if not leader:
            if flight.event.wait(timeout=TIMEOUT + 20) and flight.result is not None:
                return flight.result
            return _render(state, key, force=False)   # лидер не успел — считаем сами

        try:
            result = _render(state, key, force=False)
        except BaseException:
            _settle(key, flight, {"ok": False, "provider": None,
                                  "errors": {"internal": "генерация прервана"}})
            raise
        _settle(key, flight, result)
        return result

    return _render(state, key, force=True)
