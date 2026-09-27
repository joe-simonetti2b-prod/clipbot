"""
Tests de bout en bout hors-ligne de la post-production et du service :
vidéo simulée -> clip 9:16 sous-titré, flux de validation, pages web,
démarrage/arrêt propre du service complet.
"""
import asyncio
import json
import re
import os
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from clipbot import pipeline as pipeline_mod
from clipbot.config import Settings
from clipbot.discovery import parse_channel
from clipbot.layout import Layout
from clipbot.models import Platform
from clipbot.render import render
from clipbot.storage import Store
from clipbot.subtitles import build_ass, chunk_words
from clipbot.tiktok import TikTokClient
from clipbot.transcribe import Transcript, Word, parse_groq
from clipbot.web import build_app

WORDS = [Word(0.2, 0.5, "Non"), Word(0.5, 0.9, "mais"), Word(0.9, 1.3, "regarde"),
         Word(1.5, 1.9, "ça!"), Word(2.6, 3.0, "C'est"), Word(3.0, 3.6, "incroyable")]


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
         "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@pytest.fixture(scope="module")
def source_video(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("src") / "raw.ts"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000",
         "-t", "16", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-f", "mpegts", str(p)],
        check=True)
    return p


def test_parse_channel():
    assert parse_channel("Kamet0") == (Platform.TWITCH, "kamet0")
    assert parse_channel("kick:foo") == (Platform.KICK, "foo")
    assert parse_channel("https://www.twitch.tv/bar/") == (Platform.TWITCH, "bar")


def test_subtitles_chunking_and_hook():
    chunks = chunk_words(WORDS)
    assert [len(c) for c in chunks] == [3, 1, 2]  # coupe sur « ça! » et sur la pause
    ass = build_ass(WORDS, "Il ne s'attendait pas à ça", "blur", 8)
    assert "Style: Hook" in ass and "INCROYABLE" in ass and ass.count("Dialogue: 0") == 6


@pytest.mark.parametrize("layout", [
    Layout("blur"), Layout("face", cx=0.3), Layout("split", cam=(0.7, 0.65, 0.3, 0.3)),
])
def test_render_layouts(tmp_path, source_video, layout):
    out = tmp_path / f"{layout.kind}.mp4"
    ass = build_ass(WORDS, "Test du hook", layout.kind, 6)
    assert asyncio.run(render(source_video, 2.0, 6.0, layout, ass, out))
    info = probe(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1080, 1920)
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert float(info["format"]["duration"]) == pytest.approx(6, abs=0.2)


class FakeTelegram:
    def __init__(self):
        self.clips, self.messages = [], []
        self.on_approve = self.on_reject = None

    async def send_clip(self, cid, path, caption, header):
        self.clips.append((cid, path, caption, header))
        return 42

    async def send(self, text, chat_id=None):
        self.messages.append(text)


def test_full_pipeline_to_validation(tmp_path, source_video, monkeypatch):
    async def fake_transcribe(*a, **k):
        return Transcript(WORDS, "fr")
    monkeypatch.setattr(pipeline_mod, "transcribe", fake_transcribe)

    s = Settings()
    s.capture.work_dir = tmp_path
    s.processing.anthropic_api_key = ""
    s.processing.channel_tags = {"demo": "@demo #democlips"}
    store = Store(tmp_path / "t.db")
    raw = tmp_path / "raw.ts"
    raw.write_bytes(source_video.read_bytes())
    cid = store.add_clip(platform="twitch", channel="demo", stream_title="Finale de folie",
                         category="Just Chatting", reason="rire", score=6.2,
                         raw_path=str(raw), trim_offset=3.0, trim_duration=8.0)

    async def scenario():
        async with aiohttp.ClientSession() as session:
            tg = FakeTelegram()
            tt = TikTokClient("", "", "inbox", "", store, session)
            p = pipeline_mod.Pipeline(s, store, session, tg, tt)
            await p._process(dict(store.clip(cid)))
            row = store.clip(cid)
            assert row["status"] == "ready" and Path(row["final_path"]).exists()
            assert not raw.exists()  # le brut est supprimé après montage
            assert tg.clips and "🎥 demo en live sur Twitch" in tg.clips[0][2]
            assert "@demo #democlips" in tg.clips[0][2]  # mentions de campagne
            assert float(probe(Path(row["final_path"]))["format"]["duration"]) == pytest.approx(8 + 1.8, abs=0.2)  # + outro
            # Validation depuis Telegram, puis un double appui est ignoré
            assert "Validé" in await p.approve(cid)
            assert await p.approve(cid) == "Déjà traité"
            assert store.clip(cid)["status"] == "approved"
    asyncio.run(scenario())


def test_render_light_profile(tmp_path, source_video):
    out = tmp_path / "light.mp4"
    ass = build_ass(WORDS, "Profil gratuit", "blur", 5)
    assert asyncio.run(render(source_video, 1.0, 5.0, Layout("blur"), ass, out,
                              height=1280, preset="ultrafast"))
    v = next(s for s in probe(out)["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (720, 1280)


def test_groq_response_parsing():
    tr = parse_groq({"language": "French", "words": [
        {"word": " Salut", "start": 0.0, "end": 0.4}, {"word": "", "start": 0.4, "end": 0.5},
        {"word": "tout", "start": 0.5, "end": 0.8}]})
    assert tr.language == "fr" and tr.text == "Salut tout" and tr.words[1].start == 0.5


def test_tiktok_chunking():
    tt = TikTokClient("k", "s", "inbox", "https://x", None, None)
    src, chunk = tt._source(30 * 1024 * 1024)
    assert src["total_chunk_count"] == 1 and chunk == src["video_size"]
    src, chunk = tt._source(100 * 1024 * 1024)
    assert chunk == 10 * 1024 * 1024 and src["total_chunk_count"] == 10


def test_web_routes(tmp_path):
    s = Settings()
    s.publish.telegram_pair_code = "secret"
    fake = SimpleNamespace(s=s, orch=SimpleNamespace(paused=False, watchers={}),
                           tiktok=SimpleNamespace(configured=False), tg=FakeTelegram())

    async def scenario():
        async with TestClient(TestServer(build_app(fake))) as c:
            assert (await c.get("/health")).status == 200
            assert "confidentialité" in await (await c.get("/privacy")).text()
            assert (await c.get("/tiktok/login?k=faux")).status == 403
            assert (await c.get("/tiktok/login?k=secret")).status == 503  # pas de clés TikTok
    asyncio.run(scenario())


def test_service_starts_and_stops_cleanly(tmp_path, monkeypatch):
    from clipbot.app import App
    monkeypatch.setenv("PORT", "18765")
    monkeypatch.setenv("CLIPBOT_WORKDIR", str(tmp_path))
    for k in ("TELEGRAM_BOT_TOKEN", "TWITCH_CLIENT_ID", "ALLOWED_CHANNELS"):
        monkeypatch.delenv(k, raising=False)

    async def scenario():
        app = App(Settings())
        task = asyncio.create_task(app.run())
        await asyncio.sleep(1.5)
        async with aiohttp.ClientSession() as s:
            async with s.get("http://127.0.0.1:18765/health") as r:
                assert r.status == 200
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, 10)
    asyncio.run(scenario())


def test_pairing_cannot_be_hijacked(tmp_path):
    from clipbot.telegram import TelegramBot
    store = Store(tmp_path / "p.db")
    sent = []

    async def scenario():
        bot = TelegramBot("", "1234", store, None)
        async def fake_send(text, chat_id=None):
            sent.append((chat_id, text))
        bot.send = fake_send
        msg = lambda cid, t: {"message": {"chat": {"id": cid}, "text": t}}
        await bot._handle(msg(111, "/start 1234"))        # propriétaire légitime
        await bot._handle(msg(999, "/start 1234"))        # intrus avec le bon code
        assert bot.owner == 111
        env_bot = TelegramBot("", "1234", store, None, owner_id="222")
        assert env_bot.owner == 222                      # la variable d'environnement prime
    asyncio.run(scenario())
    assert any("Refusé" in t for _, t in sent)


def test_channel_tags_parsing_and_credit(monkeypatch):
    monkeypatch.setenv("CHANNEL_TAGS", "xqc=@xqc #xqc; twitch:Kamet0=@kamet0 ;bad")
    s = Settings()
    assert s.processing.channel_tags == {"xqc": "@xqc #xqc", "kamet0": "@kamet0"}
    ass = build_ass(WORDS, "Hook", "blur", 5, credit="twitch.tv/xqc")
    assert "Credit,,0,0,0,,twitch.tv/xqc" in ass


# ---------------------------------------------------------------- v2 : rentabilité
def test_opencv_face_detector_available():
    import cv2
    c = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    assert not c.empty()


def test_translation_spreads_words_over_sentence():
    from clipbot import translate as tr_mod
    words = [Word(0.0, 0.3, "Bro"), Word(0.3, 0.6, "what"), Word(0.6, 1.0, "happened?"),
             Word(2.0, 2.4, "No"), Word(2.4, 3.0, "way")]
    assert [len(s) for s in tr_mod.sentences(words)] == [3, 2]

    async def fake_ask(session, llm, prompt, max_tokens=800):
        return {"lines": ["Frérot il s'est passé quoi ?", "Pas possible"]}

    async def scenario():
        orig = tr_mod.ask_json
        tr_mod.ask_json = fake_ask
        try:
            llm = tr_mod.LLM("groq", "k", "m")
            out = await tr_mod.translate_words(None, llm, words, "en", "fr")
        finally:
            tr_mod.ask_json = orig
        assert " ".join(w.text for w in out) == "Frérot il s'est passé quoi ? Pas possible"
        assert out[0].start == 0.0 and out[6].text == "Pas" and out[6].start == pytest.approx(2.0)
        assert out[5].end <= 2.0 + 1e-6  # la 1re phrase ne déborde pas sur la 2e
        assert all(a.end <= b.start + 1e-6 for a, b in zip(out, out[1:]))
        # même langue -> pas d'appel, pas de traduction
        assert await tr_mod.translate_words(None, llm, words, "fr", "fr") is None
    asyncio.run(scenario())


def test_backlog_keeps_only_best_clips(tmp_path):
    s = Settings()
    s.capture.work_dir = tmp_path
    s.processing.max_backlog = 2
    store = Store(tmp_path / "b.db")
    raws = []
    for score in (3.0, 9.0, 5.0, 12.0):
        f = tmp_path / f"r{score}.ts"
        f.write_bytes(b"x")
        raws.append(f)
        store.add_clip(platform="twitch", channel="x", reason="rire", score=score, raw_path=str(f))
    p = pipeline_mod.Pipeline(s, store, None, FakeTelegram(), TikTokClient("", "", "inbox", "", store, None))
    p.trim_backlog()
    kept = sorted(r["score"] for r in store.db.execute("SELECT score FROM clips WHERE status='extracted'"))
    assert kept == [9.0, 12.0]
    assert not raws[0].exists() and not raws[2].exists() and raws[3].exists()


def test_low_ai_score_is_never_rendered(tmp_path, source_video, monkeypatch):
    async def fake_transcribe(*a, **k):
        return Transcript(WORDS, "fr")

    async def fake_copy(*a, **k):
        from clipbot.copywriter import Copy
        return Copy("hook", "caption", 2)

    async def boom(*a, **k):
        raise AssertionError("le montage ne doit pas être lancé")
    monkeypatch.setattr(pipeline_mod, "transcribe", fake_transcribe)
    monkeypatch.setattr(pipeline_mod, "write_copy", fake_copy)
    monkeypatch.setattr(pipeline_mod, "render", boom)
    s = Settings()
    s.capture.work_dir = tmp_path
    store = Store(tmp_path / "d.db")
    raw = tmp_path / "raw.ts"
    raw.write_bytes(source_video.read_bytes())
    cid = store.add_clip(platform="twitch", channel="x", reason="burst", score=4,
                         raw_path=str(raw), trim_offset=0, trim_duration=5)

    async def scenario():
        p = pipeline_mod.Pipeline(s, store, None, FakeTelegram(), TikTokClient("", "", "inbox", "", store, None))
        await p._process(dict(store.clip(cid)))
    asyncio.run(scenario())
    assert store.clip(cid)["status"] == "discarded" and not raw.exists()


def test_youtube_metadata_and_resumable_upload(tmp_path, monkeypatch):
    import time as _t
    from aiohttp import web
    from clipbot import youtube as yt_mod

    meta = yt_mod.build_metadata("Quelle action\n\n#xqc #clip #fyp", "Il ne s'y attendait pas", "public")
    assert meta["snippet"]["title"].endswith("#Shorts") and meta["snippet"]["tags"] == ["xqc", "clip", "fyp"]

    got = {}

    async def init(req):
        got["meta"] = await req.json()
        got["len"] = req.headers["X-Upload-Content-Length"]
        return web.Response(headers={"Location": str(req.url.with_path("/put"))})

    async def put(req):
        got["body"] = await req.read()
        return web.json_response({"id": "abc123"})

    app = web.Application()
    app.add_routes([web.post("/upload", init), web.put("/put", put)])
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0123456789")
    store = Store(tmp_path / "y.db")
    store.set("youtube_tokens", {"access_token": "t", "refresh_token": "r", "expires_at": _t.time() + 3600})

    async def scenario():
        async with TestServer(app) as srv, aiohttp.ClientSession() as session:
            monkeypatch.setattr(yt_mod, "UPLOAD_URL", str(srv.make_url("/upload")))
            c = yt_mod.YouTubeClient("id", "secret", "public", "https://x", store, session)
            assert c.configured and c.connected
            assert await c.publish(video, "légende #clip", "Hook") == "abc123"
    asyncio.run(scenario())
    assert got["body"] == b"0123456789" and got["len"] == "10"
    assert got["meta"]["status"]["privacyStatus"] == "public"


def test_groq_model_fallback(monkeypatch):
    """Clé sans accès au modèle demandé : le bot bascule seul sur un modèle disponible."""
    from aiohttp import web
    from clipbot import llm as llm_mod
    calls = []

    async def models(req):
        return web.json_response({"data": [{"id": "whisper-large-v3-turbo"},
                                           {"id": "openai/gpt-oss-20b"}, {"id": "llama-3.1-8b-instant"}]})

    async def chat(req):
        body = await req.json()
        calls.append(body["model"])
        if body["model"] == "llama-3.3-70b-versatile":
            return web.json_response({"error": {"code": "model_not_found"}}, status=404)
        return web.json_response({"choices": [{"message": {"content": '{"score": 7}'}}]})

    app = web.Application()
    app.add_routes([web.get("/models", models), web.post("/chat", chat)])

    async def scenario():
        async with TestServer(app) as srv, aiohttp.ClientSession() as session:
            monkeypatch.setattr(llm_mod, "GROQ_MODELS", str(srv.make_url("/models")))
            monkeypatch.setattr(llm_mod, "GROQ_CHAT", str(srv.make_url("/chat")))
            llm_mod._groq_model_cache.clear()
            llm_mod._groq_bad_models.clear()
            llm = llm_mod.LLM("groq", "cle-test", "llama-3.3-70b-versatile")
            assert await llm_mod.ask_json(session, llm, "note") == {"score": 7}
            assert await llm_mod.ask_json(session, llm, "note") == {"score": 7}
    asyncio.run(scenario())
    assert calls == ["openai/gpt-oss-20b", "openai/gpt-oss-20b"]


def test_keyframe_sampling_is_fast(source_video):
    import time as _t
    from clipbot.layout import keyframes_gray, MAX_SAMPLES
    t = _t.time()
    frames = keyframes_gray(source_video, 2.0, 12.0)
    assert 1 <= len(frames) <= MAX_SAMPLES and frames[0].shape == (360, 640)
    assert _t.time() - t < 5


def test_translation_keeps_original_for_missing_lines():
    from clipbot import translate as tr_mod
    words = [Word(0.0, 0.4, "Bro"), Word(0.4, 0.9, "what?"), Word(2.0, 2.4, "No"),
             Word(2.4, 3.0, "way."), Word(4.0, 4.5, "Chat"), Word(4.5, 5.0, "look")]

    async def partial(session, llm, prompt, max_tokens=800):
        return {"t": {"1": "Frérot quoi ?", "3": "Chat regarde"}}   # la 2e réplique manque

    async def scenario():
        orig = tr_mod.ask_json
        tr_mod.ask_json = partial
        try:
            out = await tr_mod.translate_words(None, tr_mod.LLM("groq", "k", "m"), words, "en", "fr")
        finally:
            tr_mod.ask_json = orig
        assert " ".join(w.text for w in out) == "Frérot quoi ? No way. Chat regarde"
    asyncio.run(scenario())


def test_tiktok_credentials_check(tmp_path):
    from aiohttp import web
    from clipbot import tiktok as tt_mod

    async def token(req):
        form = await req.post()
        if form["client_secret"] == "bon":
            return web.json_response({"access_token": "x", "expires_in": 7200})
        return web.json_response({"error": "invalid_client",
                                  "error_description": "Client key or secret is incorrect."})

    app = web.Application()
    app.add_routes([web.post("/token", token)])

    async def scenario():
        async with TestServer(app) as srv, aiohttp.ClientSession() as session:
            orig = tt_mod.TOKEN_URL
            tt_mod.TOKEN_URL = str(srv.make_url("/token"))
            try:
                store = Store(tmp_path / "t.db")
                good = tt_mod.TikTokClient("k", "bon", "inbox", "https://x", store, session)
                bad = tt_mod.TikTokClient("k", "faux", "inbox", "https://x", store, session)
                assert (await good.check_credentials())[0] is True
                ok, why = await bad.check_credentials()
                assert ok is False and "incorrect" in why
            finally:
                tt_mod.TOKEN_URL = orig
    asyncio.run(scenario())


def test_tiktok_login_requests_only_mode_scopes(tmp_path):
    import urllib.parse as up
    store = Store(tmp_path / "s.db")
    inbox = TikTokClient("sbkey", "s", "inbox", "https://x", store, None)
    q = up.parse_qs(up.urlparse(inbox.login_url()).query)
    assert q["scope"] == ["user.info.basic,video.upload"]
    assert q["redirect_uri"] == ["https://x/tiktok/callback"]
    direct = TikTokClient("k", "s", "direct", "https://x", store, None)
    assert "video.publish" in up.parse_qs(up.urlparse(direct.login_url()).query)["scope"][0]


def test_failed_publish_returns_clip_to_telegram(tmp_path):
    s = Settings()
    s.capture.work_dir = tmp_path
    store = Store(tmp_path / "f.db")
    video = tmp_path / "c.mp4"
    video.write_bytes(b"x")
    cid = store.add_clip(platform="twitch", channel="x", reason="rire", score=6,
                         final_path=str(video), caption="légende", status="publishing")

    class BrokenTikTok:
        mode, configured, connected = "inbox", True, True
        async def publish(self, *a, **k):
            from clipbot.tiktok import TikTokError
            raise TikTokError("trop de brouillons en attente")

    tg = FakeTelegram()
    p = pipeline_mod.Pipeline(s, store, None, tg, BrokenTikTok())
    asyncio.run(p._publish_everywhere(dict(store.clip(cid)), ["tiktok"]))
    row = store.clip(cid)
    assert row["status"] == "ready" and "brouillons" in row["error"]
    assert tg.clips and tg.clips[0][0] == cid     # renvoyé avec ses boutons


def test_telegram_backup_roundtrip(tmp_path):
    from clipbot.backup import TelegramBackup, seal, unseal, HEADER
    assert unseal(seal({"a": 1}, "tok"), "tok") == {"a": 1}
    assert unseal(seal({"a": 1}, "tok"), "autre") is None       # illisible sans le bon jeton

    class FakeBot:
        enabled, owner = True, 42
        def __init__(self):
            self.pinned, self.calls = None, []
        async def _call(self, method, **kw):
            self.calls.append(method)
            body = kw.get("json", {})
            if method == "sendMessage":
                self.pinned = {"message_id": 7, "text": body["text"]}
                return {"message_id": 7}
            if method == "editMessageText":
                self.pinned["text"] = body["text"]
                return {}
            if method == "getChat":
                return {"pinned_message": self.pinned} if self.pinned else {}
            return {}

    bot = FakeBot()
    first = Store(tmp_path / "a.db")
    b1 = TelegramBackup(bot, first, "jeton")
    first.set("tiktok_tokens", {"refresh_token": "r"})
    first.set("auto_publish", True)
    asyncio.run(b1._write())
    assert bot.pinned["text"].startswith(HEADER) and "refresh" not in bot.pinned["text"]

    fresh = Store(tmp_path / "b.db")                            # redémarrage : disque vide
    b2 = TelegramBackup(bot, fresh, "jeton")
    assert asyncio.run(b2.restore()) == 2
    assert fresh.get("tiktok_tokens") == {"refresh_token": "r"} and fresh.get("auto_publish") is True
    fresh.set("paused", True)
    asyncio.run(b2._write())
    assert bot.calls.count("sendMessage") == 1 and "editMessageText" in bot.calls


# ------------------------------------------------------ v3 : Twitch + Kick, 2 lives
def _cand(ch, viewers, platform=Platform.TWITCH, sid=None):
    from clipbot.models import StreamCandidate
    return StreamCandidate(platform, ch, sid or f"{ch}-1", f"https://x/{ch}", "", "", viewers,
                           chat_ref=ch)


def test_rotation_fills_then_switches_progressively():
    from clipbot.watcher import plan_rotation
    live = [_cand("a", 50000), _cand("b", 20000), _cand("c", 9000)]
    stop, start = plan_rotation({}, live, 2, 1.3, 600, now=0)
    assert stop == [] and [c.channel for c in start] == ["a", "b"]

    watched = {"twitch:a-1": (live[0], 0), "twitch:b-1": (live[1], 0)}
    # un nouveau live plus gros arrive, mais "b" n'est suivi que depuis 5 min -> on attend
    live2 = [_cand("d", 90000)] + live
    assert plan_rotation(watched, live2, 2, 1.3, 600, now=300) == ([], [])
    # après 10 min : on remplace UN seul live, le moins regardé
    stop, start = plan_rotation(watched, live2, 2, 1.3, 600, now=700)
    assert stop == ["twitch:b-1"] and [c.channel for c in start] == ["d"]
    # écart trop faible (< 1,3x) : pas de bascule
    live3 = [_cand("a", 50000), _cand("b", 20000), _cand("e", 24000)]
    assert plan_rotation(watched, live3, 2, 1.3, 600, now=5000) == ([], [])


def test_rotation_drops_ended_and_avoids_same_creator_twice():
    from clipbot.watcher import plan_rotation
    a = _cand("a", 50000)
    watched = {"twitch:a-1": (a, 0), "twitch:b-1": (_cand("b", 1000), 0)}
    live = [a, _cand("a", 40000, Platform.KICK, "a-k"), _cand("c", 3000)]
    stop, start = plan_rotation(watched, live, 2, 1.3, 600, now=10)
    assert stop == ["twitch:b-1"] and [c.channel for c in start] == ["c"]


def test_twitch_public_gql_parsing(monkeypatch):
    from aiohttp import web
    from clipbot import public

    async def gql(req):
        body = await req.json()
        assert req.headers["Client-ID"] == public.TWITCH_WEB_CLIENT_ID
        assert '"kamet0"' in body["query"]
        return web.json_response({"data": {"users": [
            {"login": "kamet0", "followers": {"totalCount": 5},
             "stream": {"id": "42", "title": "KC", "viewersCount": 31000, "type": "live",
                        "game": {"name": "Just Chatting"}}},
            {"login": "zerator", "followers": {"totalCount": 5}, "stream": None},
            None]}})

    app = web.Application()
    app.add_routes([web.post("/gql", gql)])

    async def scenario():
        async with TestServer(app) as srv, aiohttp.ClientSession() as session:
            monkeypatch.setattr(public, "TWITCH_GQL", str(srv.make_url("/gql")))
            users = await public.TwitchPublic(session).users(["Kamet0", "zerator", "nexistepas"])
            assert users["nexistepas"] is None and users["zerator"]["stream"] is None
            c = public.TwitchPublic.to_candidate(users["kamet0"])
            assert (c.channel, c.viewers, c.stream_id, c.category) == ("kamet0", 31000, "42", "Just Chatting")
    asyncio.run(scenario())


def test_kick_channel_parsing_and_variant_choice():
    from clipbot.public import kick_to_candidate, pick_variant
    data = {"slug": "Westcol", "chatroom": {"id": 999},
            "playback_url": "https://ivs.example/master.m3u8",
            "livestream": {"id": 7, "is_live": True, "viewer_count": 45000,
                           "session_title": "LIVE", "categories": [{"name": "Just Chatting"}]}}
    c = kick_to_candidate(data)
    assert (c.platform, c.channel, c.viewers, c.chat_ref) == (Platform.KICK, "westcol", 45000, "999")
    assert c.hls_url.endswith("master.m3u8") and kick_to_candidate({"slug": "x", "livestream": None}) is None
    master = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080\n1080p/index.m3u8\n"
              "#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720\n720p/index.m3u8\n"
              "#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=852x480\n480p/index.m3u8\n")
    assert pick_variant(master, "https://ivs.example/master.m3u8") == "https://ivs.example/720p/index.m3u8"


def test_kick_chat_event_parsing():
    import json as _j
    from clipbot.chat import parse_kick_event
    raw = _j.dumps({"event": "App\\Events\\ChatMessageEvent", "channel": "chatrooms.9.v2",
                    "data": _j.dumps({"id": "m1", "content": "LMAO [emote:37226:KEKW] [emote:1:KEKW]",
                                      "sender": {"username": "Bob"}})})
    event, msg, mid = parse_kick_event(raw)
    assert msg.text == "LMAO KEKW KEKW" and msg.author == "Bob" and mid == "m1"
    assert parse_kick_event('{"event":"pusher:ping","data":{}}')[1] is None


def test_channel_resolution_both_platforms_and_missing():
    from clipbot.public import resolve

    class FakeTwitch:
        async def users(self, logins):
            return {l: ({"login": l} if l in ("kamet0", "amouranth") else None) for l in logins}

    class FakeKick:
        async def channel(self, slug):
            return {"slug": slug} if slug in ("westcol", "amouranth") else None

    specs = [(None, "kamet0"), (None, "westcol"), (None, "amouranth"), (None, "adadinross"),
             ("kick", "kamet0")]
    r = asyncio.run(resolve(specs, FakeTwitch(), FakeKick()))
    assert (Platform.TWITCH, "kamet0") in r.targets and (Platform.KICK, "westcol") in r.targets
    assert (Platform.TWITCH, "amouranth") in r.targets and (Platform.KICK, "amouranth") in r.targets
    assert r.missing == ["adadinross", "kamet0"]     # kamet0 n'existe pas sur Kick (forcé)
    assert "Introuvables" in r.summary()


def test_backlog_keeps_best_clip_of_each_creator(tmp_path):
    s = Settings()
    s.capture.work_dir = tmp_path
    s.processing.max_backlog = 2
    store = Store(tmp_path / "k.db")
    for ch, score in (("a", 20), ("a", 18), ("a", 15), ("b", 5)):
        store.add_clip(platform="twitch", channel=ch, reason="rire", score=score)
    p = pipeline_mod.Pipeline(s, store, None, FakeTelegram(), TikTokClient("", "", "inbox", "", store, None))
    p.trim_backlog()
    kept = sorted((r["channel"], r["score"]) for r in
                  store.db.execute("SELECT channel, score FROM clips WHERE status='extracted'"))
    assert kept == [("a", 20.0), ("b", 5.0)]
    # alternance : après un clip de "a", le suivant vient de "b"
    assert store.next_clip("extracted", avoid_channel="a")["channel"] == "b"


def test_direct_hls_capture_without_ytdlp(tmp_path):
    """Capture Kick : FFmpeg lit directement le flux HLS (aucun processus yt-dlp)."""
    from aiohttp import web
    from clipbot.config import CaptureConfig
    from clipbot.models import StreamCandidate
    from clipbot.recorder import StreamRecorder
    hls = tmp_path / "hls"
    hls.mkdir()
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
                    "-f", "lavfi", "-i", "sine", "-t", "12", "-c:v", "libx264", "-preset", "ultrafast",
                    "-g", "30", "-c:a", "aac", "-f", "hls", "-hls_time", "2", "-hls_list_size", "0",
                    str(hls / "index.m3u8")], check=True)
    app = web.Application()
    app.router.add_static("/", hls)

    async def scenario():
        async with TestServer(app) as srv:
            c = StreamCandidate(Platform.KICK, "demo", "k1", "https://kick.com/demo", "", "", 1,
                                hls_url=str(srv.make_url("/index.m3u8")))
            rec = StreamRecorder(c, CaptureConfig(work_dir=tmp_path / "w", segment_s=2))
            task = asyncio.create_task(rec.run())
            segs = []
            for _ in range(40):
                await asyncio.sleep(0.25)
                segs = list(rec.buffer_dir.glob("seg_*.ts"))
                if len(segs) >= 2:
                    break
            rec._stopping = True
            await rec._kill()
            await asyncio.wait_for(task, 10)
            return rec, segs
    rec, segs = asyncio.run(scenario())
    assert rec.mode == "direct" and len(segs) >= 2


def test_resolution_drops_homonym_accounts():
    from clipbot.public import resolve

    class T:
        async def users(self, logins):
            data = {"squeezie": 9_000_000, "westcol": 400_000, "xqc": 12_000_000}
            return {l: ({"login": l, "followers": {"totalCount": data[l]}} if l in data else None)
                    for l in logins}

    class K:
        async def channel(self, slug):
            data = {"squeezie": 37, "westcol": 5_000_000, "xqc": 900_000}
            return {"slug": slug, "followers_count": data[slug]} if slug in data else None

    r = asyncio.run(resolve([(None, "squeezie"), (None, "westcol"), (None, "xqc")], T(), K()))
    assert (Platform.KICK, "squeezie") not in r.targets and (Platform.TWITCH, "squeezie") in r.targets
    assert (Platform.KICK, "westcol") in r.targets and (Platform.TWITCH, "westcol") in r.targets
    assert (Platform.KICK, "xqc") in r.targets          # 900k abonnés : vrai compte, gardé
    assert any("squeezie" in h for h in r.homonyms)


def test_french_focus_outranks_bigger_us_stream():
    from clipbot.watcher import plan_rotation
    fr = _cand("kamet0", 20000)
    fr.boost = 3.0                                   # 20 000 × 3 = 60 000
    us1, us2 = _cand("kaicenat", 50000), _cand("xqc", 40000)
    stop, start = plan_rotation({}, sorted([us1, us2, fr], key=lambda c: c.weight, reverse=True),
                                2, 1.3, 600, now=0)
    assert [c.channel for c in start] == ["kamet0", "kaicenat"]


def test_frozen_capture_is_restarted_in_ytdlp_mode(tmp_path):
    """Capture qui tourne sans rien écrire (cas nico_la) : relancée, et bascule en yt-dlp."""
    from clipbot.config import CaptureConfig
    from clipbot.models import StreamCandidate
    from clipbot.recorder import StreamRecorder

    class Frozen(StreamRecorder):
        STALL_S = 1.5
        STARTUP_GRACE_S = 1.5
        WATCH_EVERY_S = 0.3
        spawns = []

        async def _spawn(self):
            Frozen.spawns.append(self.mode)
            p = await asyncio.create_subprocess_exec("sleep", "30", stderr=asyncio.subprocess.PIPE)
            self._procs = [p, p]

    async def scenario():
        c = StreamCandidate(Platform.TWITCH, "nico_la", "1", "https://www.twitch.tv/nico_la", "", "", 1)
        rec = Frozen(c, CaptureConfig(work_dir=tmp_path, segment_s=2))
        task = asyncio.create_task(rec.run())
        await asyncio.sleep(9)
        rec._stopping = True
        await rec._kill()
        await asyncio.wait_for(task, 10)
        return rec
    rec = asyncio.run(scenario())
    # figée deux fois -> bascule yt-dlp (1re fois : seconde chance, pub mal annoncée)
    assert rec.mode == "pipe" and Frozen.spawns[:2] == ["streamlink", "streamlink"]


def test_noisy_capture_process_does_not_block(tmp_path):
    """Un processus qui écrit beaucoup sur sa sortie d'erreur ne doit pas se bloquer."""
    from clipbot.config import CaptureConfig
    from clipbot.models import StreamCandidate
    from clipbot.recorder import StreamRecorder

    async def scenario():
        c = StreamCandidate(Platform.TWITCH, "x", "1", "https://x", "", "", 1)
        rec = StreamRecorder(c, CaptureConfig(work_dir=tmp_path, segment_s=2))
        # 500 Ko de messages d'erreur : bloquerait sans vidange (tampon ~64 Ko)
        p = await asyncio.create_subprocess_exec(
            "python3", "-c", "import sys\nfor i in range(10000): sys.stderr.write('x'*50+'\\n')",
            stderr=asyncio.subprocess.PIPE)
        rec._drain(p, "bruyant")
        await asyncio.wait_for(p.wait(), 10)
        return p.returncode, list(rec._err_tail)
    code, tail = asyncio.run(scenario())
    assert code == 0 and tail and tail[-1].startswith("bruyant:")


def test_single_growing_file_counts_as_frozen(tmp_path):
    """Un seul fichier qui grossit sans nouveau découpage = capture figée."""
    import time as _t
    from datetime import datetime
    from clipbot.config import CaptureConfig
    from clipbot.models import StreamCandidate
    from clipbot.recorder import SEG_FMT, StreamRecorder
    rec = StreamRecorder(StreamCandidate(Platform.TWITCH, "x", "1", "u", "", "", 1),
                         CaptureConfig(work_dir=tmp_path))
    assert rec.mode == "streamlink"
    old = datetime.fromtimestamp(_t.time() - 120).strftime(SEG_FMT)
    f = rec.buffer_dir / f"seg_{old}.ts"
    f.write_bytes(b"x")                     # écrit à l'instant, mais créé il y a 2 min
    assert rec._last_write_age(started=_t.time() - 300) > 100


def test_tiktok_pending_limit_keeps_clip_queued(tmp_path):
    s = Settings()
    s.capture.work_dir = tmp_path
    store = Store(tmp_path / "q.db")
    video = tmp_path / "c.mp4"
    video.write_bytes(b"x")
    ids = [store.add_clip(platform="twitch", channel="x", reason="rire", score=6, ai_score=a,
                          final_path=str(video), caption="c", status="publishing") for a in (5, 8)]

    class FullTikTok:
        mode, configured, connected = "inbox", True, True
        async def publish(self, *a, **k):
            from clipbot.tiktok import TikTokError
            raise TikTokError("/post/publish/inbox/video/init/ : spam_risk_too_many_pending_share")

    tg = FakeTelegram()
    p = pipeline_mod.Pipeline(s, store, None, tg, FullTikTok())
    for cid in ids:
        asyncio.run(p._publish_everywhere(dict(store.clip(cid)), ["tiktok"]))
    assert all(store.clip(c)["status"] == "approved" for c in ids)   # en file, pas perdus
    assert p.tiktok_full_until > 0 and len(tg.messages) == 1 and not tg.clips
    assert store.next_clip("approved")["ai_score"] == 8              # le meilleur passe d'abord


def test_streamlink_ad_break_not_a_stall(tmp_path):
    import time as _t
    from clipbot.config import CaptureConfig
    from clipbot.models import Platform, StreamCandidate
    from clipbot.recorder import StreamRecorder
    c = StreamCandidate(platform=Platform.TWITCH, channel="x", stream_id="1",
                        url="https://www.twitch.tv/x", title="", category="", viewers=1)
    r = StreamRecorder(c, CaptureConfig(work_dir=tmp_path))
    assert r.mode == "streamlink"
    now = _t.time()
    # Démarrage : 60 s sans morceau = pub d'avant-live tolérée
    assert not r._is_stalled(now - 60)
    assert r._is_stalled(now - 200)
    # Pub annoncée par Streamlink : tolérée pendant sa durée
    r._note_ad("[plugins.twitch][info] Detected advertisement break of 90 seconds")
    assert not r._is_stalled(now - 200)


# ---------------------------------------------------------------- v3 : choix manuel, montage
def test_rotation_pinned_channels_take_and_keep_their_slot():
    from clipbot.watcher import plan_rotation
    a, b, small = _cand("a", 50000), _cand("b", 20000), _cand("small", 800)
    watched = {"twitch:a-1": (a, 0), "twitch:b-1": (b, 0)}
    live = [a, b, small]
    # Choix manuel d'un petit live : il remplace le plus faible non choisi, immédiatement
    stop, start = plan_rotation(watched, live, 2, 1.3, 600, now=10, pinned={"small"})
    assert stop == ["twitch:b-1"] and [c.channel for c in start] == ["small"]
    # Une fois suivi, un choix manuel n'est jamais remplacé par la rotation
    watched = {"twitch:a-1": (a, 0), "twitch:small-1": (small, 0)}
    huge = _cand("huge", 900000)
    stop, start = plan_rotation(watched, [huge, a, small], 2, 1.3, 600, now=5000, pinned={"small"})
    assert stop == ["twitch:a-1"] and [c.channel for c in start] == ["huge"]
    # Deux choix manuels : l'algorithme n'a plus de place
    stop, start = plan_rotation(watched, [huge, a, small], 2, 1.3, 600, now=5000,
                                pinned={"small", "a"})
    assert (stop, start) == ([], [])
    # Choix manuel hors ligne : la place reste à l'algorithme
    stop, start = plan_rotation({}, [huge, a], 2, 1.3, 600, now=0, pinned={"offline"})
    assert [c.channel for c in start] == ["huge", "a"]


def test_clip_links_and_ids():
    from clipbot.harvest import accept_owner, clip_id
    from clipbot.watcher import find_clip_links
    links = find_clip_links("mdr https://clips.twitch.tv/FunnyCat-AbC123 et "
                            "https://www.twitch.tv/nico_la/clip/Slug-XyZ789 "
                            "https://kick.com/adinross?clip=clip_01JABC pas ça https://twitch.tv/nico_la")
    assert len(links) == 3
    assert clip_id(links[0]) == "funnycat-abc123" and clip_id(links[2]) == "clip_01jabc"
    assert accept_owner({"uploader": "Nico_la"}, {"nico_la"})
    assert not accept_owner({"uploader": "random"}, {"nico_la"})
    assert accept_owner({}, {"nico_la"})


def test_subtitles_pacing_and_overlays():
    from clipbot.subtitles import SUB_LEAD, chunk_times
    chunks = chunk_words(WORDS)
    times = chunk_times(chunks, 8)
    # jamais deux bouts à l'écran en même temps, légère avance sur la voix
    assert all(times[i][1] <= times[i + 1][0] + 1e-9 for i in range(len(times) - 1))
    assert times[0][0] == pytest.approx(0.2 - SUB_LEAD)
    assert all(e - s >= 0.45 - 1e-9 for s, e in times[:-1] if True)
    ass = build_ass(WORDS, "Hook", "face", 8, keywords=["incroyable"], cover="IL CRAQUE",
                    creator="nico_la", watermark="@clipclaptrap", outro_s=1.8, peak_at=3.0)
    assert "IL CRAQUE" in ass and "NICO_LA" in ass and "@clipclaptrap" in ass
    assert "S'ABONNER" in ass and "PARTAGER" in ass
    assert "\\c&H0066FF33&}INCROYABLE" in ass or "INCROYABLE" in ass
    # sous-titres traduits : bouts de phrase entiers, pas de karaoké mot à mot
    tr = build_ass(WORDS, "", "blur", 8, karaoke=False)
    assert "\\c&H0000FFFF&" not in tr and tr.count("Dialogue: 0") < len(WORDS)


def test_render_outro_and_av_sync(tmp_path):
    """Son qui démarre 0,5 s après l'image dans le live : le bip et le flash restent calés."""
    src = tmp_path / "late.ts"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "color=black:s=640x360:r=30:d=9,drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:"
         "enable='between(t,4.5,4.7)'",
         "-itsoffset", "1.5", "-f", "lavfi", "-i",
         "aevalsrc='if(between(t,3,3.2),sin(2*PI*1000*t),0)':s=48000:d=8",
         "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-c:a", "aac", "-f", "mpegts",
         str(src)], check=True)
    out = tmp_path / "o.mp4"
    assert asyncio.run(render(src, 1.0, 6.0, Layout("blur"), build_ass([], "", "blur", 6),
                              out, height=640, preset="ultrafast", threads=1, outro_s=1.5))
    assert float(probe(out)["format"]["duration"]) == pytest.approx(7.5, abs=0.15)
    sil = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(out), "-af",
                          "silencedetect=n=-30dB:d=0.1", "-vn", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    beep = float(re.search(r"silence_end: ([\d.]+)", sil).group(1))
    assert beep == pytest.approx(3.5, abs=0.1)
