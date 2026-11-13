#!/usr/bin/env python3
"""Самопроверка nowplay.py без сети: python3 selftest.py. Сеть и Telegram подменены, состояние — во временном файле."""
import hashlib, json, os, stat, tempfile, urllib.error
from urllib.parse import parse_qs, urlsplit

tmp = tempfile.mkdtemp()
os.environ["NOWPLAY_STATE"], os.environ["NOWPLAY_ENV"] = f"{tmp}/state.json", f"{tmp}/.env"
for k in ("TG_TOKEN", "OWNER_ID", "SPOTIFY_CLIENT_ID", "SPOTIFY_REFRESH", "LASTFM_USER", "LASTFM_API_KEY", "APPLE_DEV_TOKEN", "APPLE_USER_TOKEN", "YANDEX_MUSIC_TOKEN", "VK_COOKIES"):
    os.environ.pop(k, None)
with open(f"{tmp}/.env", "w") as f:
    f.write("# комментарий\nTG_TOKEN='t'\nOWNER_ID=1\nLASTFM_USER=\n")

import nowplay as n
from nowplay import C, E

n.load()
assert E == {"TG_TOKEN": "t", "OWNER_ID": "1"}, E  # кавычки сняты, пустое и комментарии пропущены

calls, vk_logins, tg_calls, sent = [], [], [], []
playing = [True]
spotify = [None]  # ответ currently-playing
sp_fail = [False]


def fake_http(url, data=None, headers=None, **k):
    calls.append((url, data))
    if "audioscrobbler" in url:
        if "getinfo" in url:
            if "api_key=bad" in url:
                raise urllib.error.HTTPError(url, 403, "", {}, None)
            return b'{"user": {"name": "me"}}'
        t = {"artist": {"#text": "Земфира"}, "name": "Хочешь?"}
        if playing[0]:
            t["@attr"] = {"nowplaying": "true"}
        return json.dumps({"recenttracks": {"track": [t]}}).encode()
    if "yandex" in url and "/search?" in url:
        return json.dumps({"result": {"tracks": {"results": [{"id": 6, "title": "Другая", "artists": [{"name": "Земфира"}]},
                                                             {"id": 7, "title": "Хочешь?", "artists": [{"name": "Земфира"}], "durationMs": 181000, "coverUri": "c.ya/get/%%"}]}}}).encode()
    if url == "https://c.ya/get/300x300":
        return b"JPEG"
    if url.endswith("/tracks/7/download-info"):
        return json.dumps({"result": [{"codec": "aac", "bitrateInKbps": 999, "downloadInfoUrl": "https://s/a?x=1"}, {"codec": "mp3", "bitrateInKbps": 192, "downloadInfoUrl": "https://s/lo?x=1"},
                                      {"codec": "mp3", "bitrateInKbps": 320, "downloadInfoUrl": "https://s/hi?x=1"}]}).encode()
    if url == "https://s/hi?x=1&format=json":
        return b'{"host": "h", "path": "/p/1", "ts": "t", "s": "s"}'
    if url == "https://h/get-mp3/" + hashlib.md5("XGRlBW9FXlekgbPrRHuSiAp/1s".encode()).hexdigest() + "/t/p/1":
        return b"FULL"
    if "login.vk.ru" in url:
        assert data == b"version=1&app_id=6287487&access_token="
        vk_logins.append(headers["Cookie"])
        return b'{"type": "okay", "data": {"access_token": "vt"}}' if "remixsid=2" in headers["Cookie"] else b'{"type": "error", "error_info": "unauthorized"}'
    if "api.vk.ru/method/users.get" in url:
        return b'{"response": [{"id": 5}]}'
    if "api.vk.ru/method/audio.search" in url:
        assert b"access_token=vt" in data
        return json.dumps({"response": {"items": [
            {"artist": "Земфира", "title": "Только в VK (Speed Up)", "url": "https://v/fast.mp3", "duration": 100},
            {"artist": "Кто-то", "title": "Только в VK", "url": "https://v/other.mp3", "duration": 200},
            {"artist": "Земфира", "title": "Только в VK", "url": "https://v/hls/index.m3u8", "duration": 200},
            {"artist": "Земфира", "title": "Только в VK", "url": "https://v/ok.mp3?x=1", "duration": 200, "album": {"thumb": {"photo_300": "https://v/c.jpg"}}}]}}).encode()
    if url == "https://v/ok.mp3?x=1":
        return b"VKMP3"
    if url == "https://v/fast.mp3":
        return b"FAST"
    if url == "https://v/c.jpg":
        return b"VKJPEG"
    if "account/status" in url:
        return b'{"result": {"account": {"login": "me"}, "plus": {"hasPlus": true}}}'
    if "itunes.apple.com" in url:
        return json.dumps({"results": [{"trackName": "Только в iTunes", "artistName": "Земфира", "trackViewUrl": "https://music.apple.com/t?a=1&b=2"}]}).encode()
    if "amp-api.music.apple.com" in url:
        if headers["Media-User-Token"] != "u" or headers["Authorization"] != "Bearer d":
            raise urllib.error.HTTPError(url, 401, "", {}, None)
        return json.dumps({"data": [{"id": "42", "attributes": {"name": "1998", "artistName": "Chet Faker", "url": "https://music.apple.com/x", "durationInMillis": 240000}}]}).encode()
    if "accounts.spotify.com/api/token" in url:
        if sp_fail[0]:
            raise urllib.error.HTTPError(url, 400, "", {}, None)
        f = parse_qs(data.decode())
        assert f["client_id"] == ["cid"] and "client_secret" not in f  # PKCE: секрета приложения нет
        if f["grant_type"] == ["authorization_code"]:
            assert f["code"] == ["CODE"] and f["redirect_uri"] == [n.SP_REDIRECT] and f["code_verifier"] == [pkce[0]]
        return b'{"access_token": "at", "refresh_token": "rt2", "expires_in": 3600}'
    if "api.spotify.com" in url:
        assert headers["Authorization"] == "Bearer at"
        return json.dumps(spotify[0]).encode() if spotify[0] else b""
    if url == "https://i.scdn/300":
        return b"SPJPEG"
    if url == "https://tg/file/bott/docs/c.json":
        return b'[{"name": "remixsid", "value": "2", "domain": ".vk.ru"}]'
    if url.endswith("/sendAudio"):
        return b'{"ok": true, "result": {"message_id": 9, "chat": {"id": 1}, "audio": {"file_id": "FID"}}}'
    raise AssertionError(url)


def fake_tg(method, **k):
    tg_calls.append((method, k))
    return {"file_path": "docs/c.json"} if method == "getFile" else {}


# связь с Telegram оборвалась дважды — третья попытка проходит; отказ Telegram (HTTP 400) не повторяем
flaky, real_sleep = [], n.time.sleep
def flaky_http(url, data=None, headers=None, **k):
    flaky.append(url)
    if "getMe" in url and len(flaky) < 3:
        raise urllib.error.URLError(ConnectionResetError(104, "reset"))
    if "bad" in url:
        raise urllib.error.HTTPError(url, 400, "", {}, None)
    return b'{"ok": true, "result": {"username": "b"}}'
n.http, n.time.sleep = flaky_http, (lambda s: None)
assert n.tg("getMe") == {"username": "b"} and len(flaky) == 3
try:
    n.tg("bad")
    raise AssertionError("HTTPError должен дойти до вызывающего")
except urllib.error.HTTPError:
    assert len(flaky) == 4
n.time.sleep = real_sleep

n.http, n.tg, n.send, n.TG_API = fake_http, fake_tg, (lambda t, **k: sent.append(t)), "https://tg"
msg = lambda text, **k: {"text": text, "chat": {"id": 1, "type": "private"}, "from": {"id": 1}, "message_id": 2, **k}

# ничего не подключено: /np говорит, что подключить; чужим бот не отвечает; любое другое сообщение — справка
assert n.now_playing() is None and "/spotify" in n.handle(msg("/np"))
n.update({"message": msg("/np", **{"from": {"id": 2}})})
n.update({"message": msg("/np", chat={"id": -5, "type": "group"})})
assert not sent
n.update({"message": msg("/start")})
assert "▫️ Spotify" in sent[-1] and "Telegram для бизнеса" in sent[-1] and "/setinline" in sent[-1]
# @бот в чате, пока ничего не подключено или не играет: пустой список и кнопка-пояснение; чужому — просто пусто
iq = lambda uid=1: n.update({"inline_query": {"id": "q", "from": {"id": uid}, "query": ""}})
iq()
assert tg_calls[-1] == ("answerInlineQuery", {"inline_query_id": "q", "results": [], "cache_time": 0, "is_personal": True, "button": {"text": "Ничего не подключено", "start_parameter": "np"}})
iq(2)
assert tg_calls[-1][1]["results"] == [] and "button" not in tg_calls[-1][1]

# Last.fm: имя и ключ проверяем запросом; неверные не сохраняем
assert "не подошли" in n.handle(msg("/lastfm me bad")) and "LASTFM_USER" not in E
assert n.handle(msg("/lastfm me k")) == "✅ Last.fm: подключено." and (E["LASTFM_USER"], E["LASTFM_API_KEY"]) == ("me", "k")
assert stat.S_IMODE(os.stat(n.STATE).st_mode) == 0o600 and json.load(open(n.STATE)) == {"LASTFM_USER": "me", "LASTFM_API_KEY": "k"}
assert n.now_playing() == ("🎧 <code>Земфира — Хочешь?</code>\n\n<a href=\"https://open.spotify.com/search/%D0%97%D0%B5%D0%BC%D1%84%D0%B8%D1%80%D0%B0%20%D0%A5%D0%BE%D1%87%D0%B5%D1%88%D1%8C%3F\">Spotify</a>", True)

# переписка с другом: команду стираем; mp3 нет (каталоги не подключены) — другу текст со ссылками, владельцу отбивка
mb = {"text": "/np", "from": {"id": 1}, "chat": {"id": 55}, "message_id": 3, "business_connection_id": "bc"}
n.update({"business_message": mb})
assert tg_calls[-2] == ("deleteBusinessMessages", {"business_connection_id": "bc", "message_ids": [3]})
assert tg_calls[-1][0] == "sendMessage" and tg_calls[-1][1]["chat_id"] == 55 and tg_calls[-1][1]["business_connection_id"] == "bc"
assert "Земфира" in tg_calls[-1][1]["text"] and "music.yandex.ru/track/7" in tg_calls[-1][1]["text"]  # поиск Яндекса открыт и без токена
assert "/yamusic" in sent[-1] and "ушли ссылки" in sent[-1]
# не играет — другу ничего, владельцу «последним было»
playing[0], k0 = False, len(tg_calls)
n.handle_business(mb)
assert [c[0] for c in tg_calls[k0:]] == ["deleteBusinessMessages"] and "Последним было" in sent[-1] and "ничего не отправлено" in sent[-1]
# чужое сообщение в переписке, /np самому себе и не-команда — не трогаем
k0, s0 = len(tg_calls), len(sent)
n.handle_business({**mb, "from": {"id": 55}})
n.handle_business({**mb, "chat": {"id": 1}})
n.handle_business({**mb, "text": "привет /np"})
assert len(tg_calls) == k0 and len(sent) == s0
playing[0] = True

# Яндекс Музыка: токен из адреса (и без команды), сообщение с токеном стираем
assert n._same("Хочешь?", "хочешь") and not n._same("", "x") and not n._same("Creep", "Karma Police")
assert n.handle(msg("https://music.yandex.ru/#access_token=tok10&token_type=bearer")).startswith("✅ Яндекс Музыка: <b>me</b>") and E["YANDEX_MUSIC_TOKEN"] == "tok10"
assert tg_calls[-1] == ("deleteMessage", {"chat_id": 1, "message_id": 2})
assert n.handle(msg("/yamusic https://music.yandex.ru/#access_token=tok11&token_type=bearer")).startswith("✅ Яндекс Музыка: <b>me</b>")
assert E["YANDEX_MUSIC_TOKEN"] == "tok11" and "access_token" in n.handle(msg("/yamusic"))
# трек целиком: лучший mp3, подпись ссылки; в sendAudio — файл, обложка, подпись со ссылками, business_connection_id
n.now_playing()
assert n.np_audio() == {"audio": b"FULL", "title": "Хочешь?", "performer": "Земфира", "duration": 181} and C.link == "https://song.link/ya/7"
n.now_playing()
assert n.np_send(55, "<b>подпись</b>", "bc") and calls[-1][0] == "https://tg/bott/sendAudio"
assert all(x in calls[-1][1] for x in (b"FULL", b'name="business_connection_id"\r\n\r\nbc', "Земфира - Хочешь?.mp3".encode(), b'song.link/ya/7">song.link</a>',
                                       b'name="thumbnail"; filename="cover.jpg"\r\nContent-Type: image/jpeg\r\n\r\nJPEG\r\n'))
# целиком в переписку с другом: один sendAudio, без текста и без отбивки
k0, s0 = len(tg_calls), len(sent)
n.handle_business(mb)
assert [c[0] for c in tg_calls[k0:]] == ["deleteBusinessMessages"] and len(sent) == s0 and calls[-1][0].endswith("/sendAudio")
# @бот: mp3 грузим в личку владельца, берём file_id, сообщение стираем; повторный запрос того же трека ничего не качает
k0, c0 = len(tg_calls), len(calls)
iq()
assert [c[0] for c in tg_calls[k0:]] == ["deleteMessage", "answerInlineQuery"] and tg_calls[k0][1] == {"chat_id": 1, "message_id": 9}
r = tg_calls[-1][1]["results"]
assert len(r) == 1 and r[0]["type"] == "audio" and r[0]["audio_file_id"] == "FID" and r[0]["parse_mode"] == "HTML"
assert r[0]["caption"].startswith("🎧 <code>Земфира — Хочешь?</code>") and "song.link/ya/7" in r[0]["caption"] and b'name="chat_id"\r\n\r\n1\r\n' in calls[-1][1]
c0 = len(calls)
iq()
assert tg_calls[-1][1]["results"] == r and not any("sendAudio" in u or "get-mp3" in u for u, _ in calls[c0:])
iq(2)
assert tg_calls[-1][1]["results"] == []
playing[0] = False
iq()
assert tg_calls[-1][1]["results"] == [] and tg_calls[-1][1]["button"]["text"].startswith("Сейчас ничего не играет")
playing[0] = True
C.track = ("Земфира", "Нет такой")
assert n.np_audio() is None and not n.np_send(55, "x") and "Яндекс Музыка)" in n.np_miss()

# VK: куки в трёх видах; частями — по хосту за раз; без кук vk.ru VK не пускает, но куки сохраняем
assert n.vk_cookies('[{"name": "p", "value": "1"}]') == n.vk_cookies('{"p": "1"}') == n.vk_cookies("Cookie: p=1") == {"p": "1"}
assert n.vk_cookies("a=b=c; d=e") == {"a": "b=c", "d": "e"}
assert "Не разобрал" in n.handle(msg("/vk что-то")) and "VK_COOKIES" not in E
r = n.handle(msg('/vk [{"name": "p", "value": "1", "domain": "login.vk.ru"}]'))
assert "unauthorized" in r and "обоих хостов" in r and E["VK_COOKIES"] == "p=1"
assert n.handle(msg("/vk", document={"file_id": "f", "file_size": 60})).startswith("✅ VK подключён") and E["VK_COOKIES"] == "p=1; remixsid=2"  # файл .json с подписью /vk
assert vk_logins == ["p=1", "p=1; remixsid=2"]
# второй источник: точное название раньше «(Speed Up)», чужой исполнитель и поток m3u8 не берём; токен по кукам — один раз на 10 минут
C.track, C.cover, C.dur, C.vk_tok = ("Земфира", "Только в VK"), None, 0, None
assert n.np_audio() == {"audio": b"VKMP3", "title": "Только в VK", "performer": "Земфира", "duration": 200} and C.cover == "https://v/c.jpg"
assert n.np_audio()["audio"] == b"VKMP3" and len(vk_logins) == 3 and "Яндекс Музыка и VK" in n.np_miss()
C.dur = 100  # длина известна: запись на 200 с — не та, «(Speed Up)» на 100 с подходит по длине
assert n.np_audio()["audio"] == b"FAST"
C.dur = 300
assert n.np_audio() is None
C.dur = 203  # ±7 с
assert n.np_audio()["audio"] == b"VKMP3"
C.dur = 0
assert n.handle(msg("/vk off")) == "VK отключён." and "VK_COOKIES" not in E and C.vk_tok is None

# без mp3 — ссылки: Apple Music и YouTube встают перед song.link; чужой трек и уже яблочную ссылку не трогаем
C.track, C.ya_hit = ("Земфира", "Нет такой"), None
sl = "T\n<a href=\"u\">Spotify</a> · <a href=\"https://song.link/s/x\">song.link</a>"
yt = "<a href=\"https://music.youtube.com/search?q=%D0%97%D0%B5%D0%BC%D1%84%D0%B8%D1%80%D0%B0%20{}\">YouTube</a>"
assert n.np_text(sl) == sl.replace(" · <a href=\"https://song", " · " + yt.format("%D0%9D%D0%B5%D1%82%20%D1%82%D0%B0%D0%BA%D0%BE%D0%B9") + " · <a href=\"https://song")
C.track = ("Земфира", "Только в iTunes")
ap, y = "<a href=\"https://music.apple.com/t?a=1&amp;b=2\">Apple Music</a>", yt.format("%D0%A2%D0%BE%D0%BB%D1%8C%D0%BA%D0%BE%20%D0%B2%20iTunes")
assert n.np_text(sl) == sl.replace(" · <a href=\"https://song", f" · {ap} · {y} · <a href=\"https://song") and n.np_text("T") == f"T · {ap} · {y}"
assert n.np_text("https://music.apple.com/z") == f"https://music.apple.com/z · {y}"
C.track = ("Земфира", "Хочешь?")
assert "<a href=\"https://music.yandex.ru/track/7\">Яндекс Музыка</a> · <a href=\"https://music.youtube.com" in n.np_text("T")
assert n.handle(msg("/yamusic off")) == "Яндекс Музыка отключена." and "YANDEX_MUSIC_TOKEN" not in E
assert n.np_audio() is None and "/yamusic" in n.np_miss()
# @бот без mp3: строка-статья, по нажатию уйдёт текст со ссылками
C.sent = None
iq()
r = tg_calls[-1][1]["results"][0]
assert r["type"] == "article" and r["title"] == "Земфира — Хочешь?" and "music.yandex.ru/track/7" in r["input_message_content"]["message_text"]

# Apple Music: токены проверяем запросом; Last.fm молчит → последний трек Apple, с длиной и обложкой-ссылкой
assert "не подошли" in n.handle(msg("/apple x y")) and "APPLE_DEV_TOKEN" not in E
assert n.handle(msg("/apple d u")) == "✅ Apple Music: подключено."
playing[0] = False
assert n.now_playing() == ("🎧 <code>Chet Faker — 1998</code>\n\n<a href=\"https://music.apple.com/x\">Apple Music</a> · <a href=\"https://song.link/i/42\">song.link</a>", True)
assert C.track == ("Chet Faker", "1998") and C.dur == 240
playing[0] = True
assert "Земфира" in n.now_playing()[0]  # Last.fm играет — он важнее Apple

# Spotify: своё приложение, вход PKCE; адрес возврата человек присылает боту сам
assert "developer.spotify.com" in n.handle(msg("/spotify"))
r = n.handle(msg("/spotify cid"))
q = parse_qs(urlsplit(r.split('"')[1].replace("&amp;", "&")).query)
pkce = E["SPOTIFY_PKCE"].split(" ")
assert q["client_id"] == ["cid"] and q["redirect_uri"] == [n.SP_REDIRECT] and q["code_challenge_method"] == ["S256"] and q["state"] == [pkce[1]]
assert q["code_challenge"] == [__import__("base64").urlsafe_b64encode(hashlib.sha256(pkce[0].encode()).digest()).rstrip(b"=").decode()]
assert "не от последней ссылки" in n.handle(msg(n.SP_REDIRECT + "?code=CODE&state=чужой")) and "SPOTIFY_REFRESH" not in E
assert "не дал код" in n.handle(msg(f"{n.SP_REDIRECT}?error=access_denied&state={pkce[1]}"))
sp_fail[0] = True
assert "код не принял (400)" in n.handle(msg(f"{n.SP_REDIRECT}?code=CODE&state={pkce[1]}")) and "SPOTIFY_REFRESH" not in E
sp_fail[0] = False
assert n.handle(msg(f"{n.SP_REDIRECT}?code=CODE&state={pkce[1]}")).startswith("✅ Spotify") and E["SPOTIFY_REFRESH"] == "rt2" and "SPOTIFY_PKCE" not in E
# Spotify важнее Last.fm и Apple: исполнители через запятую, длина, обложка до 320 px, song.link по id
spotify[0] = {"is_playing": True, "item": {"id": "S1", "name": "Creep", "duration_ms": 238000, "artists": [{"name": "Radiohead"}, {"name": "X"}],
                                           "external_urls": {"spotify": "https://open.spotify.com/track/S1"},
                                           "album": {"images": [{"url": "https://i.scdn/640", "width": 640}, {"url": "https://i.scdn/300", "width": 300}]}}}
assert n.now_playing() == ("🎧 <code>Radiohead, X — Creep</code>\n\n<a href=\"https://open.spotify.com/track/S1\">Spotify</a> · <a href=\"https://song.link/s/S1\">song.link</a>", True)
assert (C.track, C.dur, C.cover) == (("Radiohead, X", "Creep"), 238, "https://i.scdn/300")
spotify[0] = None  # 204: в Spotify тишина — идём к Last.fm
assert "Земфира" in n.now_playing()[0]
# токен обновляется по refresh; новый refresh сохраняется
C.sp_tok, E["SPOTIFY_REFRESH"] = (None, 0), "old"
assert n.sp_access() == "at" and E["SPOTIFY_REFRESH"] == "rt2"
assert "✅ Spotify" in n.help_text() and "▫️ VK" in n.help_text()

# состояние переживает перезапуск; права 600
n.load()
assert E["SPOTIFY_REFRESH"] == "rt2" and E["APPLE_USER_TOKEN"] == "u" and E["TG_TOKEN"] == "t" and stat.S_IMODE(os.stat(n.STATE).st_mode) == 0o600
print("ok")
