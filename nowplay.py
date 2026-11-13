#!/usr/bin/env python3
"""nowplay-bot — Telegram-бот «сейчас играет» на одного владельца.

Набираешь @имя_бота в любом чате (или /np в переписке с другом) — от твоего имени уходит mp3 играющего трека с обложкой и ссылками.
Что играет: Spotify → Last.fm → Apple Music. mp3 целиком: Яндекс Музыка → VK Музыка.
Только стандартная библиотека Python 3.9+. Настройки — .env рядом с файлом, состояние — state.json (права 600).

  python3 nowplay.py        запуск (опрос getUpdates); в Docker — docker compose up -d --build
  python3 selftest.py       самопроверка без сети
"""
import base64, hashlib, json, os, re, secrets, sys, time, traceback
import urllib.error, urllib.request
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlencode, urlsplit

DIR = os.path.dirname(os.path.abspath(__file__))
STATE = os.environ.get("NOWPLAY_STATE") or f"{DIR}/state.json"
KEYS = ("TG_TOKEN", "OWNER_ID", "SPOTIFY_CLIENT_ID", "SPOTIFY_REFRESH", "LASTFM_USER", "LASTFM_API_KEY",
        "APPLE_DEV_TOKEN", "APPLE_USER_TOKEN", "YANDEX_MUSIC_TOKEN", "VK_COOKIES")
E = {}  # настройки: .env < окружение < state.json
S = {}  # то, что бот сохранил сам (токены, куки) — содержимое state.json
C = SimpleNamespace(track=None, dur=0, link=None, cover=None, ya_hit=None, sp_tok=(None, 0), vk_tok=None, caption="", sent=None)  # играющий трек и кеш токенов; до перезапуска
TG_API = os.environ.get("TG_API", "https://api.telegram.org")
NP_CMDS = ("/np", "/nowplaying", "/играет")
OFF = ("off", "выкл")


def load():
    E.clear(), S.clear()
    try:
        with open(os.environ.get("NOWPLAY_ENV") or f"{DIR}/.env", encoding="utf-8") as f:
            for line in f:
                k, eq, v = line.strip().partition("=")
                v = v.strip().strip("'\"")
                if eq and v and not k.startswith("#"):
                    E[k.strip()] = v
    except FileNotFoundError:
        pass
    E.update({k: v for k, v in os.environ.items() if k in KEYS and v})
    try:
        with open(STATE, encoding="utf-8") as f:
            S.update(json.load(f))
    except FileNotFoundError:
        pass
    E.update(S)


def set_cfg(k, v):
    """Настройку — в state.json (права 600) и сразу в E; None — убрать."""
    if v is None:
        S.pop(k, None), E.pop(k, None)
    else:
        S[k] = E[k] = str(v)
    tmp = STATE + ".tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(S, f, ensure_ascii=False)
    os.replace(tmp, STATE)


def http(url, data=None, headers=None, timeout=60):
    """data: dict → JSON, bytes → как есть."""
    hdrs = {"User-Agent": "nowplay-bot/1", **(headers or {})}
    if isinstance(data, dict):
        data, hdrs = json.dumps(data).encode(), {"Content-Type": "application/json", **hdrs}
    with urllib.request.urlopen(urllib.request.Request(url, data, hdrs), timeout=timeout) as r:
        return r.read()


FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def tg(method, timeout=70, **kw):
    for attempt in range(3):  # связь с Telegram рвётся (reset): без повтора ответ владельцу терялся молча
        try:
            return json.loads(http(f"{TG_API}/bot{E['TG_TOKEN']}/{method}", kw, timeout=timeout))["result"]
        except urllib.error.HTTPError:
            raise  # Telegram ответил отказом — повтор не поможет
        except OSError:
            if attempt == 2:
                raise
            time.sleep(1 + attempt)


def send(text, **extra):
    return tg("sendMessage", chat_id=int(E["OWNER_ID"]), text=text, parse_mode="HTML", link_preview_options={"is_disabled": True}, **extra)


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def unsend(m):
    """Сообщение с токеном или куками из чата стираем."""
    try:
        tg("deleteMessage", chat_id=m["chat"]["id"], message_id=m["message_id"])
    except Exception:
        pass


# ---------- Spotify: вход PKCE без секрета и без публичного адреса ----------

SP_REDIRECT = "http://127.0.0.1:8888/callback"  # никто не слушает: человек копирует адрес из строки браузера и присылает боту


def sp_auth_url():
    verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(16)
    set_cfg("SPOTIFY_PKCE", f"{verifier} {state}")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return "https://accounts.spotify.com/authorize?" + urlencode({
        "client_id": E["SPOTIFY_CLIENT_ID"], "response_type": "code", "redirect_uri": SP_REDIRECT, "scope": "user-read-currently-playing",
        "state": state, "code_challenge_method": "S256", "code_challenge": challenge})


def sp_token(form):
    return json.loads(http("https://accounts.spotify.com/api/token", urlencode({"client_id": E["SPOTIFY_CLIENT_ID"], **form}).encode(), FORM, timeout=30))


def sp_access():
    tok, until = C.sp_tok
    if until > time.time() + 60:
        return tok
    try:
        r = sp_token({"grant_type": "refresh_token", "refresh_token": E["SPOTIFY_REFRESH"]})
    except urllib.error.HTTPError as e:
        if e.code == 400 and b"invalid_grant" in e.read():  # доступ отозвали в Spotify
            set_cfg("SPOTIFY_REFRESH", None)
            send("🎧 Spotify отключился — войди заново: /spotify")
        raise
    if r.get("refresh_token"):  # PKCE обычно выдаёт новый, старый тогда перестаёт работать
        set_cfg("SPOTIFY_REFRESH", r["refresh_token"])
    C.sp_tok = (r["access_token"], time.time() + int(r.get("expires_in", 3600)))
    return r["access_token"]


def sp_connect(url):
    """Адрес, на который Spotify перенаправил после входа (127.0.0.1…?code=…&state=…) → токен."""
    q = parse_qs(urlsplit(url).query)
    verifier, _, state = (E.get("SPOTIFY_PKCE") or "").partition(" ")
    if not verifier or q.get("state") != [state]:
        return "Этот адрес не от последней ссылки входа. Начни заново: /spotify"
    if "code" not in q:
        return f"Spotify не дал код: {esc((q.get('error') or ['?'])[0])}. Начни заново: /spotify"
    try:
        r = sp_token({"grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": SP_REDIRECT, "code_verifier": verifier})
    except urllib.error.HTTPError as e:
        return f"Spotify код не принял ({e.code}) — он живёт пару минут и годится один раз. Начни заново: /spotify"
    set_cfg("SPOTIFY_REFRESH", r["refresh_token"])
    set_cfg("SPOTIFY_PKCE", None)
    C.sp_tok = (r["access_token"], time.time() + int(r.get("expires_in", 3600)))
    return "✅ Spotify подключён. Пиши /np — здесь или в переписке с другом."


def sp_cmd(arg):
    if arg.lower() in OFF:
        set_cfg("SPOTIFY_REFRESH", None)
        return "Spotify отключён."
    if arg:
        set_cfg("SPOTIFY_CLIENT_ID", arg.split()[0])
    if not E.get("SPOTIFY_CLIENT_ID"):
        return ("Создай своё приложение на developer.spotify.com/dashboard, в Redirect URIs впиши "
                f"<code>{SP_REDIRECT}</code>, затем пришли: <code>/spotify Client_ID</code>")
    return (f"<a href=\"{esc(sp_auth_url())}\">Войти через Spotify</a>. После входа браузер откроет страницу, которая не загрузится, — "
            "так и должно быть. Скопируй её адрес (начинается с http://127.0.0.1:8888/callback) и пришли сюда.")


# ---------- что сейчас играет ----------

def now_playing():
    """Spotify → Last.fm (скробблер: туда пишут Яндекс Музыка, Apple Music и др.) → Apple Music (последний начатый трек).
    → (текст, играет ли сейчас) или None, если ничего не подключено."""
    C.dur = 0  # длина играющего трека, с: по ней отличаем нужную запись от ремиксов и обрезков
    C.track = C.link = C.cover = C.ya_hit = None  # track — (исполнитель, название): по нему np_audio ищет mp3
    if E.get("SPOTIFY_REFRESH"):
        b = http("https://api.spotify.com/v1/me/player/currently-playing", headers={"Authorization": "Bearer " + sp_access()}, timeout=15)
        it = json.loads(b) if b else {}  # 204 — ничего не играет
        if it.get("is_playing") and it.get("item"):
            t = it["item"]
            who = ", ".join(a["name"] for a in t.get("artists") or []) or (t.get("show") or {}).get("name") or ""
            url = (t.get("external_urls") or {}).get("spotify") or ""
            C.track, C.dur = (who, t.get("name") or ""), int(t.get("duration_ms") or 0) // 1000
            C.link = f"https://song.link/s/{t['id']}" if t.get("id") and t.get("type", "track") == "track" else None
            C.cover = next((i["url"] for i in (t.get("album") or {}).get("images") or [] if (i.get("width") or 999) <= 320), None)
            return np_title(who, t.get("name")) + (f"\n\n<a href=\"{url}\">Spotify</a>" if url else "") + np_link(), True
    if not (E.get("LASTFM_API_KEY") and E.get("LASTFM_USER")):
        return apple_now() or (("Сейчас в Spotify ничего не играет.", False) if E.get("SPOTIFY_REFRESH") else None)
    tr = (lastfm("user.getrecenttracks", limit=1).get("recenttracks") or {}).get("track") or [None]
    tr = tr[0] if isinstance(tr, list) else tr
    if not tr:
        return "Last.fm пока не видел ни одного трека — проверь, что музыка туда пишется.", False
    artist, name = (tr.get("artist") or {}).get("#text") or "", tr.get("name") or ""
    line = f"{np_title(artist, name)}\n\n<a href=\"https://open.spotify.com/search/{quote(f'{artist} {name}')}\">Spotify</a>"
    if (tr.get("@attr") or {}).get("nowplaying") == "true":
        C.track = (artist, name)
        return line, True
    return apple_now() or (f"Сейчас ничего не играет. Последним было: {line}", False)


def lastfm(method, **kw):
    return json.loads(http("https://ws.audioscrobbler.com/2.0/?" + urlencode({"method": method, "user": E["LASTFM_USER"], "api_key": E["LASTFM_API_KEY"], "format": "json", **kw}), timeout=15))


def np_title(who, name):
    """Название моноширинным: в Telegram копируется одним нажатием."""
    return f"🎧 <code>{esc(who or '')} — {esc(name or '')}</code>"


def np_link():
    return f" · <a href=\"{C.link}\">song.link</a>" if C.link else ""


def apple_recent():
    return json.loads(http("https://amp-api.music.apple.com/v1/me/recent/played/tracks?limit=1&types=songs", timeout=15, headers={
        "Authorization": "Bearer " + E["APPLE_DEV_TOKEN"], "Media-User-Token": E["APPLE_USER_TOKEN"], "Origin": "https://music.apple.com"}))


def apple_now():
    """Apple Music «сейчас играет» не отдаёт — берём последний начатый трек.
    shortcut: времени у трека нет, поэтому считаем, что он и играет; Apple спрашиваем последним."""
    if not (E.get("APPLE_DEV_TOKEN") and E.get("APPLE_USER_TOKEN")):
        return None
    try:
        x = (apple_recent().get("data") or [{}])[0]
        a = x.get("attributes") or {}
    except Exception:
        traceback.print_exc()
        return None
    if not a.get("name"):
        return None
    C.track, C.dur = (a.get("artistName") or "", a["name"]), int(a.get("durationInMillis") or 0) // 1000
    C.link = f"https://song.link/i/{x['id']}" if str(x.get("id") or "").isdigit() else None
    C.cover = ((a.get("artwork") or {}).get("url") or "").replace("{w}", "300").replace("{h}", "300") or None
    return np_title(a.get("artistName"), a["name"]) + (f"\n\n<a href=\"{a['url']}\">Apple Music</a>" if a.get("url") else "") + np_link(), True


# ---------- mp3 целиком: Яндекс Музыка, затем VK Музыка (оба способа неофициальные) ----------

def _same(a, b):
    a, b = (re.sub(r"\W+", "", x.casefold()) for x in (a, b))
    return bool(a and b) and (a in b or b in a)


YA_MUSIC_AUTH = "https://oauth.yandex.ru/authorize?response_type=token&client_id=23cabbbdc6cd418abb4b39c32c41195d"  # приложение Яндекс Музыки


def ya_music(path, tok=None):
    return json.loads(http("https://api.music.yandex.net" + path, timeout=15, headers={
        **({"Authorization": "OAuth " + (tok or E["YANDEX_MUSIC_TOKEN"])} if tok or E.get("YANDEX_MUSIC_TOKEN") else {}),  # поиск открыт и без токена
        "X-Yandex-Music-Client": "YandexMusicAndroid/24023621"}))["result"]


def ya_find(artist, name):
    """Трек в каталоге Яндекса по исполнителю, названию и длине → как отдаёт API, или None. Ответ помним на время одного /np."""
    if (C.ya_hit or (None,))[0] != (artist, name):
        found = (ya_music("/search?" + urlencode({"text": f"{artist} {name}", "type": "track", "page": 0})).get("tracks") or {}).get("results") or []
        C.ya_hit = ((artist, name), next((t for t in found[:5] if _same(t.get("title") or "", name) and any(_same(a.get("name") or "", artist) for a in t.get("artists") or [])
                                          and (not C.dur or abs((t.get("durationMs") or 0) // 1000 - C.dur) <= 7)), None))
    return C.ya_hit[1]


def np_audio():
    """mp3 к /np: трек целиком из Яндекс Музыки по токену, иначе из VK Музыки по кукам. Отрывки на 30 секунд не шлём.
    → поля sendAudio (audio — байты) или None: трек не нашёлся."""
    artist, name = C.track or ("", "")
    if not name:
        return None
    # shortcut: сверка по вхождению — «Zemfira» против «Земфира» не сойдётся и уйдёт текст; транслит — когда начнёт мешать
    if E.get("YANDEX_MUSIC_TOKEN"):
        try:
            t = ya_find(artist, name)
            if t:
                info = max((i for i in ya_music(f"/tracks/{t['id']}/download-info") if i.get("codec") == "mp3"), key=lambda i: i.get("bitrateInKbps") or 0)
                d = json.loads(http(info["downloadInfoUrl"] + "&format=json", timeout=15))
                sign = hashlib.md5(("XGRlBW9FXlekgbPrRHuSiA" + d["path"][1:] + d["s"]).encode()).hexdigest()  # подпись ссылки, как в клиентах Яндекса
                data = http(f"https://{d['host']}/get-mp3/{sign}/{d['ts']}{d['path']}", timeout=120)
                if len(data) < 49_000_000:  # потолок файла у ботов Telegram — 50 МБ
                    C.link = C.link or f"https://song.link/ya/{t['id']}"
                    C.cover = C.cover or ("https://" + t["coverUri"].replace("%%", "300x300") if t.get("coverUri") else None)
                    return {"audio": data, "title": name, "performer": artist, "duration": (t.get("durationMs") or 0) // 1000}
        except Exception:
            traceback.print_exc()
    if E.get("VK_COOKIES"):  # чего нет в Яндексе (западный каталог) — из VK Музыки
        try:
            # длина известна (Spotify, Apple) — берём запись той же длины ±7 с, а не ремикс или обрезок; нет (Last.fm) — точное название раньше «(Speed Up)»
            hits = sorted((t for t in vk_api("audio.search", q=f"{artist} {name}", count=15)["items"]
                           if ".mp3" in (t.get("url") or "") and _same(t.get("title") or "", name) and _same(t.get("artist") or "", artist)
                           and (not C.dur or abs(int(t.get("duration") or 0) - C.dur) <= 7)), key=lambda t: len(t["title"]) != len(name))
            for t in hits[:1]:
                data = http(t["url"], headers={"User-Agent": VK_UA}, timeout=120)
                if len(data) < 49_000_000:
                    C.cover = C.cover or ((t.get("album") or {}).get("thumb") or {}).get("photo_300")
                    return {"audio": data, "title": name, "performer": artist, "duration": int(t.get("duration") or 0)}
        except Exception:
            traceback.print_exc()
    return None


VK_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"


def vk_cookies(text):
    """Выгрузка расширения (JSON-список {name, value}), JSON-словарь или строка заголовка Cookie → словарь."""
    text = re.sub(r"(?i)^cookie:\s*", "", text.strip())
    try:
        d = json.loads(text)
        return {c["name"]: c["value"] for c in d} if isinstance(d, list) else dict(d)
    except ValueError:
        return dict(p.strip().split("=", 1) for p in text.split(";") if "=" in p)


def vk_api(method, **params):
    """API VK от имени сайта: куки сессии (login.vk.ru + vk.ru) → токен на 15 минут → метод.
    Токен привязан к адресу, с которого получен: бот должен ходить в VK с одного и того же адреса."""
    tok, till = C.vk_tok or ("", 0)
    if time.time() > till:
        r = json.loads(http("https://login.vk.ru/?act=web_token", b"version=1&app_id=6287487&access_token=", timeout=15, headers={
            **FORM, "Cookie": E["VK_COOKIES"], "User-Agent": VK_UA, "Origin": "https://vk.ru", "Referer": "https://vk.ru/"}))
        if r.get("type") != "okay":
            raise OSError(f"вход по кукам не прошёл ({r.get('error_info') or r.get('type')})")
        tok = r["data"]["access_token"]
        C.vk_tok = (tok, time.time() + 600)
    r = json.loads(http(f"https://api.vk.ru/method/{method}?v=5.245&client_id=6287487", urlencode({**params, "access_token": tok}).encode(), timeout=15,
                        headers={**FORM, "User-Agent": VK_UA}))
    if "error" in r:
        C.vk_tok = None
        raise OSError(f"VK {method}: {r['error'].get('error_code')} {r['error'].get('error_msg')}")
    return r["response"]


def np_send(chat_id, text, bc=None):
    """/np с mp3: аудио с обложкой и подписью → отправленное сообщение. False — трек не нашли или Telegram не принял файл."""
    try:
        a = np_audio()
        if not a:
            return False
        data = a.pop("audio")
        text += f" · <a href=\"{C.link}\">song.link</a>" if C.link and "song.link" not in text else ""
        C.caption = np_text(text)
        fields = {"chat_id": chat_id, "caption": C.caption, "parse_mode": "HTML", **a, **({"business_connection_id": bc} if bc else {})}
        fname = re.sub(r'["\\/\r\n]', "", f"{a['performer']} - {a['title']}")[:80] or "track"
        files = [("audio", f"{fname}.mp3", "audio/mpeg", data)]
        try:  # обложка; не скачалась — шлём без неё
            cover = http(C.cover, timeout=10) if C.cover else b""
            if 0 < len(cover) < 200_000:  # потолок Telegram для thumbnail — 200 КБ
                files.append(("thumbnail", "cover.jpg", "image/jpeg", cover))
        except Exception:
            traceback.print_exc()
        b = f"----nowplay{time.time_ns()}"
        body = "".join(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n" for k, v in fields.items()).encode()
        for k, name, type_, blob in files:
            body += f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{name}\"\r\nContent-Type: {type_}\r\n\r\n".encode() + blob + b"\r\n"
        body += f"--{b}--\r\n".encode()
        return json.loads(http(f"{TG_API}/bot{E['TG_TOKEN']}/sendAudio", body, {"Content-Type": f"multipart/form-data; boundary={b}"}, timeout=180))["result"] or True
    except Exception:
        traceback.print_exc()
        return False


def np_miss():
    on = [n for k, n in (("YANDEX_MUSIC_TOKEN", "Яндекс Музыка"), ("VK_COOKIES", "VK")) if E.get(k)]
    return (f"mp3 не нашлось: трека нет там, где я ищу ({' и '.join(on)}), или Telegram не принял файл." if on
            else "mp3 не приложен: не подключены ни Яндекс Музыка (/yamusic), ни VK (/vk).")


def np_text(text):
    """Ссылки на трек в других сервисах — перед song.link: Apple Music (iTunes Search), Яндекс Музыка (поиск), YouTube Music.
    Друг открывает там, где слушает сам; без mp3 это всё, что он получит."""
    artist, name = C.track or ("", "")
    if not name:
        return text
    more = []
    if "music.apple.com" not in text:
        try:
            found = json.loads(http("https://itunes.apple.com/search?" + urlencode({"term": f"{artist} {name}", "entity": "song", "limit": 5}), timeout=15)).get("results") or []
            more.append(("Apple Music", next((t["trackViewUrl"] for t in found if t.get("trackViewUrl") and _same(t.get("trackName") or "", name)
                                              and _same(t.get("artistName") or "", artist)), None)))
        except Exception:
            traceback.print_exc()
    try:
        t = ya_find(artist, name)
        more.append(("Яндекс Музыка", t and f"https://music.yandex.ru/track/{t['id']}"))
    except Exception:
        traceback.print_exc()
    more.append(("YouTube", "https://music.youtube.com/search?q=" + quote(f"{artist} {name}")))
    links = " · ".join(f"<a href=\"{esc(u)}\">{n}</a>" for n, u in more if u)
    return text.replace(" · <a href=\"https://song.link", f" · {links} · <a href=\"https://song.link", 1) if "song.link" in text else f"{text} · {links}"


# ---------- команды владельца в личке с ботом ----------

def ya_cmd(m, arg):
    if arg.lower() in OFF:
        set_cfg("YANDEX_MUSIC_TOKEN", None)
        return "Яндекс Музыка отключена."
    if not arg:
        return (("✅ Яндекс Музыка подключена. Отключить — /yamusic off.\n\n" if E.get("YANDEX_MUSIC_TOKEN") else "")
                + f"Открой <a href=\"{YA_MUSIC_AUTH}\">вход Яндекса</a>, войди и сразу скопируй адрес страницы — в нём будет access_token=…; "
                "пришли его: <code>/yamusic адрес</code>. Сообщение с токеном я сотру.")
    unsend(m)
    tok = (re.search(r"access_token=([^&\s]+)", arg) or re.search(r"(\S+)", arg)).group(1)
    try:
        acc = ya_music("/account/status", tok)
    except Exception:
        return "Яндекс этот токен не принял. Пришли адрес целиком — с access_token=… внутри."
    set_cfg("YANDEX_MUSIC_TOKEN", tok)
    return (f"✅ Яндекс Музыка: <b>{esc((acc.get('account') or {}).get('login') or '')}</b>. К /np прикладываю трек целиком."
            + ("" if (acc.get("plus") or {}).get("hasPlus") else "\nПодписки Плюс у аккаунта не вижу — Яндекс отдаст только отрывок."))


def vk_cmd(m, arg):
    if arg.lower() in OFF:
        set_cfg("VK_COOKIES", None)
        C.vk_tok = None
        return "VK отключён."
    if not arg:
        return (("✅ VK подключён. Отключить — /vk off.\n\n" if E.get("VK_COOKIES") else "")
                + "Войди в vk.ru в браузере, открой <code>https://login.vk.ru/?act=web_token</code> и выгрузи куки этой страницы "
                "(расширение вроде Cookie-Editor → Export → JSON). Пришли их: <code>/vk …</code> или файлом .json с подписью /vk. Сообщение я сотру.")
    unsend(m)
    try:
        new = vk_cookies(arg)
    except Exception:
        new = {}
    if not new:
        return "Не разобрал куки: нужен JSON из расширения или строка вида name=value; name2=value2."
    # присылать можно частями — по хосту за раз: новые куки добавляются к прежним
    set_cfg("VK_COOKIES", "; ".join(f"{k}={v}" for k, v in {**vk_cookies(E.get("VK_COOKIES") or ""), **new}.items()))
    C.vk_tok = None
    try:
        vk_api("users.get")
    except Exception as e:
        return (f"Куки сохранил ({len(new)} шт.), но VK пока не пускает: {esc(e)}. Нужны куки обоих хостов — login.vk.ru и vk.ru; "
                "дошли недостающие ещё одним /vk. Токен VK привязан к адресу: бот должен выходить в сеть с одного адреса.")
    return "✅ VK подключён: чего нет в Яндексе, ищу там."


def pair_cmd(m, arg, keys, check, name, howto):
    """Сервис с двумя значениями (Last.fm, Apple Music): проверяем запросом и только потом сохраняем."""
    if arg.lower() in OFF:
        for k in keys:
            set_cfg(k, None)
        return f"{name}: отключено."
    p = arg.split()
    if len(p) != 2:
        return howto
    unsend(m)
    old = [E.get(k) for k in keys]
    E.update(zip(keys, p))
    try:
        check()
    except Exception:
        for k, v in zip(keys, old):
            E.pop(k, None) if v is None else E.update({k: v})
        return f"{name}: эти значения не подошли. {howto}"
    for k, v in zip(keys, p):
        set_cfg(k, v)
    return f"✅ {name}: подключено."


def help_text():
    on = lambda *keys: "✅" if all(E.get(k) for k in keys) else "▫️"
    return ("<b>Сейчас играет</b>\nНабери имя бота через @ в любом чате и выбери трек (inline-режим включается у @BotFather: /setinline).\n"
            "Или /np — здесь либо в переписке с другом (для переписки нужен Telegram Business: Настройки → Telegram для бизнеса → Чат-боты).\n\n"
            "<b>Что играет</b>\n"
            f"{on('SPOTIFY_REFRESH')} Spotify — /spotify\n"
            f"{on('LASTFM_USER', 'LASTFM_API_KEY')} Last.fm — /lastfm\n"
            f"{on('APPLE_DEV_TOKEN', 'APPLE_USER_TOKEN')} Apple Music — /apple\n\n"
            "<b>Откуда mp3</b>\n"
            f"{on('YANDEX_MUSIC_TOKEN')} Яндекс Музыка — /yamusic\n"
            f"{on('VK_COOKIES')} VK Музыка — /vk\n\n"
            "Отключить любой сервис: команда и <code>off</code>.")


NOTHING = "Не подключено ничего, откуда узнать, что играет: /spotify, /lastfm или /apple."


def handle(m):
    """Сообщение владельца в личке с ботом → текст ответа или None."""
    text = (m.get("text") or m.get("caption") or "").strip()
    doc = m.get("document")
    if doc and text.split(" ")[0] == "/vk" and (doc.get("file_size") or 0) < 1_000_000:  # куки файлом
        text = "/vk " + http(f"{TG_API}/file/bot{E['TG_TOKEN']}/{tg('getFile', file_id=doc['file_id'])['file_path']}", timeout=30).decode()
    cmd, _, arg = text.partition(" ")
    cmd, arg = cmd.split("@")[0].lower(), arg.strip()
    if text.startswith(SP_REDIRECT):
        return sp_connect(text)
    if cmd == "/yamusic" or "music.yandex.ru/#access_token=" in text:  # ссылку с токеном присылают и без команды
        return ya_cmd(m, arg if cmd == "/yamusic" else text)
    if cmd == "/spotify":
        return sp_cmd(arg)
    if cmd == "/vk":
        return vk_cmd(m, arg)
    if cmd == "/lastfm":
        return pair_cmd(m, arg, ("LASTFM_USER", "LASTFM_API_KEY"), lambda: lastfm("user.getinfo"), "Last.fm",
                        "Пришли имя и ключ API (last.fm/api/account/create): <code>/lastfm имя ключ</code>")
    if cmd == "/apple":
        return pair_cmd(m, arg, ("APPLE_DEV_TOKEN", "APPLE_USER_TOKEN"), apple_recent, "Apple Music",
                        "Открой music.apple.com, войди, в консоли браузера выполни <code>MusicKit.getInstance().developerToken</code> и "
                        "<code>MusicKit.getInstance().musicUserToken</code>; пришли оба: <code>/apple developerToken musicUserToken</code>. Сообщение я сотру.")
    if cmd in NP_CMDS:
        np_ = now_playing()
        if not np_ or not np_[1]:
            return np_[0] if np_ else NOTHING
        return None if np_send(int(E["OWNER_ID"]), np_[0]) else f"{np_text(np_[0])}\n\n{np_miss()}"
    return help_text()


def handle_business(m):
    """Владелец пишет /np в личке с другом (Telegram Business): команду стираем, другу от имени владельца — трек.
    Чужие сообщения и всё остальное не трогаем."""
    owner, chat, bc = int(E["OWNER_ID"]), m.get("chat") or {}, m.get("business_connection_id")
    if (m.get("text") or "").strip().lower() not in NP_CMDS or (m.get("from") or {}).get("id") != owner or chat.get("id") == owner:
        return
    try:
        tg("deleteBusinessMessages", business_connection_id=bc, message_ids=[m["message_id"]])
    except Exception:
        pass
    try:
        np_ = now_playing()
    except Exception as e:
        traceback.print_exc()
        np_ = (f"Не узнал, что играет ({esc(type(e).__name__)}).", False)
    if np_ and np_[1]:  # в переписку — только когда правда играет; иначе — тихо владельцу
        if not np_send(chat["id"], np_[0], bc):  # без mp3 — ссылки другу и отбивка владельцу
            tg("sendMessage", chat_id=chat["id"], text=np_text(np_[0]), parse_mode="HTML", business_connection_id=bc, link_preview_options={"is_disabled": True})
            send(f"{np_miss()} В переписку ушли ссылки.")
    else:
        send((np_[0] if np_ else NOTHING) + "\nВ переписку ничего не отправлено.")


def handle_inline(q):
    """Владелец набрал @бот в любом чате: одна строка на выбор — играющий трек; нажал — трек уходит в чат от его имени.
    Telegram шлёт inline только уже загруженные файлы: грузим mp3 в личку владельца, берём file_id, сообщение стираем."""
    owner = int(E["OWNER_ID"])
    answer = lambda results, **k: tg("answerInlineQuery", inline_query_id=q["id"], results=results, cache_time=0, is_personal=True, **k)
    if (q.get("from") or {}).get("id") != owner:
        return answer([])  # бот личный
    try:
        np_ = now_playing()
    except Exception as e:
        traceback.print_exc()
        np_ = (f"Не узнал, что играет ({type(e).__name__})", False)
    if not (np_ and np_[1]):
        return answer([], button={"text": re.sub(r"<[^>]+>|\n.*", "", np_[0])[:60] if np_ else "Ничего не подключено", "start_parameter": "np"})
    key = (C.track, C.dur)
    if (C.sent or (None,))[0] != key:  # тот же трек второй раз не качаем: Telegram спрашивает на каждую букву
        m = np_send(owner, np_[0])
        fid = isinstance(m, dict) and (m.get("audio") or {}).get("file_id")
        if fid:
            unsend(m)
        C.sent = (key, fid, C.caption if fid else np_text(np_[0]))
    _, fid, caption = C.sent
    title = " — ".join(C.track)
    answer([{"type": "audio", "id": "1", "audio_file_id": fid, "caption": caption, "parse_mode": "HTML"} if fid else
            {"type": "article", "id": "1", "title": title, "description": "mp3 не нашлось — уйдут ссылки",
             "input_message_content": {"message_text": caption, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}}])


def update(u):
    if "inline_query" in u:
        return handle_inline(u["inline_query"])
    if "business_message" in u:
        return handle_business(u["business_message"])
    m = u.get("message") or {}
    if (m.get("from") or {}).get("id") != int(E["OWNER_ID"]) or (m.get("chat") or {}).get("type") != "private":
        return  # бот личный: чужим не отвечаем
    r = handle(m)
    if r:
        send(r)


def main():
    load()
    if not (E.get("TG_TOKEN") and E.get("OWNER_ID", "").isdigit()):
        sys.exit("Нужны TG_TOKEN и OWNER_ID в .env — см. README.")
    print("nowplay-bot: @" + tg("getMe")["username"], flush=True)
    offset = 0
    while True:
        try:
            ups = tg("getUpdates", offset=offset, timeout=50, allowed_updates=["message", "business_message", "inline_query"])
        except Exception as e:
            print("getUpdates:", type(e).__name__, getattr(e, "code", ""), flush=True)
            time.sleep(5)
            continue
        for u in ups:
            offset = u["update_id"] + 1
            try:
                update(u)
            except Exception:
                traceback.print_exc()


if __name__ == "__main__":
    main()
