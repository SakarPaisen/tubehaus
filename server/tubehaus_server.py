#!/usr/bin/env python3
"""TubeHaus server - the API behind the TubeHaus web app.

It runs next to an Invidious instance and adds what Invidious does not have:
  * one shared library (subscriptions, history, resume points, playlists, likes)
    so every device sees the same state;
  * YouTube live streams as HLS, with segments pulled ahead of the player;
  * live captions transcribed on the fly with whisper.cpp;
  * machine-translated subtitles for videos that have none in your language;
  * "send to TV": a phone starts playback on another open TubeHaus screen;
  * high-quality offline save (separate video + audio merged with ffmpeg);
  * a cache in front of Invidious' /api/v1/videos so clicks answer instantly.

Configuration comes from environment variables - see .env.example.
Python standard library only; ffmpeg, yt-dlp and whisper.cpp are optional.
"""
import base64, json, os, re, subprocess, threading, time, urllib.request, urllib.parse, urllib.error
import datetime, secrets
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def env(key, default=""):
    return os.environ.get(key, default)


BIND = (env("HT_BIND", "127.0.0.1"), int(env("HT_PORT", "3010")))
STATE = env("HT_STATE", "./state")                       # library, subtitle cache
INV = env("HT_INVIDIOUS", "http://127.0.0.1:3000").rstrip("/")
COMPANION = env("HT_COMPANION", "http://127.0.0.1:8282").rstrip("/")
PUBLIC = env("HT_PUBLIC_URL", "http://localhost:8080").rstrip("/")   # where browsers open TubeHaus
# YouTube refuses most cloud/VPS addresses ("Sign in to confirm you're not a bot").
# Sending YouTube traffic out through Cloudflare WARP (warp-cli in proxy mode,
# which listens on 127.0.0.1:40000 and speaks HTTP CONNECT) is what makes this work.
YT_PROXY = env("HT_YT_PROXY", "127.0.0.1:40000")        # "" = connect to YouTube directly
WARP_ROTATE = env("HT_WARP_ROTATE", "1") == "1"         # get a new WARP address when YouTube blocks us
YTDLP = env("HT_YTDLP", "yt-dlp")
LT = env("HT_LIBRETRANSLATE", "")                        # optional LibreTranslate URL (translation fallback)
SUB_LANG = env("HT_SUB_LANG", "en")                      # default target of translated subtitles (the app asks for its own language)
WHISPER_BIN = env("HT_WHISPER_BIN", "")                  # whisper.cpp "whisper-cli"; empty = no live captions
WHISPER_MODEL = env("HT_WHISPER_MODEL", "")              # e.g. ggml-base-q5_1.bin
WHISPER_LANG = env("HT_WHISPER_LANG", "auto")
YTDL_DIR = env("HT_DOWNLOAD_DIR", "/var/tmp/tubehaus-dl")

lock = threading.Lock()
LIVE_CACHE = {}
# every request to YouTube itself goes through YT_PROXY when one is set
YT = urllib.request.build_opener(urllib.request.ProxyHandler(
    {"http": "http://" + YT_PROXY, "https": "http://" + YT_PROXY} if YT_PROXY else {}))
LIVE_ORIGIN = PUBLIC
LIVE_BASE = LIVE_ORIGIN + "/api/p/live/s/"   # + base64url(segment url) + ".ts"; proxies re-decode %-escapes

# ---------- /api/v1/videos cache ----------
# Invidious re-fetches any videos row older than ten minutes, and that refetch is
# a YouTube round trip: measured 0.9-2.1 s on a click, 0.003 s warm.
# The signed googlevideo URLs in the answer stay valid for six hours, so a recent
# copy is just as playable. Serve it straight back, refresh behind the user once
# it ages, and warm the recommendations the user is most likely to click next.
VID_FRESH = 900        # younger than this: hand it over, do nothing else
VID_STALE = 7200       # older than this: fetch synchronously (URLs would be near expiry)
VID_KEEP = 400         # entries
VID_PREFETCH = 4       # recommended videos warmed after a cold click
vid_cache = {}         # path -> (ts, code, body)
vid_lk = threading.Lock()
vid_busy = set()       # paths currently being refreshed


def vid_store(key, code, body):
    """Cache only plain, finished videos - live and premieres must stay fresh."""
    try:
        d = json.loads(body)
    except Exception:
        return None
    if code != 200 or not isinstance(d, dict) or not d.get("videoId"):
        return None
    if d.get("liveNow") or d.get("premiereTimestamp"):
        return d
    with vid_lk:
        vid_cache[key] = (time.time(), code, body)
        if len(vid_cache) > VID_KEEP:
            for k, _ in sorted(vid_cache.items(), key=lambda kv: kv[1][0])[:len(vid_cache) - VID_KEEP]:
                vid_cache.pop(k, None)
    return d


def vid_fetch(key):
    """key is the request path; returns (code, body) with hlsUrl patched in."""
    t0 = time.time()
    try:
        r = urllib.request.urlopen(INV + key, timeout=60)
        code, body = r.status, r.read()
    except urllib.error.HTTPError as e:
        code, body = e.code, e.read()
    if code >= 400:
        log("videos", "%d %s" % (code, key.split("?")[0][:60]), t0)
        return code, body
    try:
        d = json.loads(body)
    except Exception:
        d = None
    if isinstance(d, dict) and d.get("videoId"):
        if d.get("liveNow") and not d.get("hlsUrl"):
            d["hlsUrl"] = "%s/api/p/live/%s.m3u8" % (LIVE_ORIGIN, d["videoId"])
            body = json.dumps(d, ensure_ascii=False).encode()
            log("videos", "hlsUrl injected %s %d" % (d["videoId"], code), t0, every=0)
    vid_store(key, code, body)
    return code, body


def vid_bg(key, prefetch_from=None):
    """Refresh one path (optional), then warm the videos it recommends."""
    if key:
        try:
            vid_fetch(key)
        except Exception as ex:
            log("videos", "refresh FAIL %s %s" % (key.split("/")[-1][:20], repr(ex)[:80]))
        finally:
            with vid_lk:
                vid_busy.discard(key)
    if not prefetch_from:
        return
    try:
        ids = [v.get("videoId") for v in (prefetch_from.get("recommendedVideos") or [])]
        warmed = 0
        for vid in ids:
            if warmed >= VID_PREFETCH:
                break
            if not vid or not re.fullmatch(r"[\w-]{11}", vid):
                continue
            k = "/api/v1/videos/" + vid
            with vid_lk:
                hot = k in vid_cache
            if hot:
                continue
            time.sleep(1.0)      # a human clicking, not a crawler
            vid_fetch(k)
            warmed += 1
        if warmed:
            log("videos", "prefetched %d recommended" % warmed, every=300)
    except Exception as ex:
        log("videos", "prefetch FAIL %s" % repr(ex)[:80])

# ---------- journal logging ----------
# Failures here used to be silent, so a dead proxy or an expired live URL
# looked like "the video just doesn't play". One compact line per event, and
# rate-limited per tag because a retrying player can hammer the same path.
LOG_EVERY = 60
_log_at, _log_lk = {}, threading.Lock()


def log(tag, msg, t0=None, every=LOG_EVERY):
    """every=0 logs every time (rare events); otherwise at most once per `every` s per tag."""
    now = time.time()
    if every:
        with _log_lk:
            if now - _log_at.get(tag, 0) < every:
                return
            _log_at[tag] = now
            if len(_log_at) > 500:
                for k in [k for k, v in _log_at.items() if now - v > 3600]:
                    del _log_at[k]
    print("[%s] %s%s" % (tag, msg, "" if t0 is None else " %dms" % ((now - t0) * 1000)), flush=True)


# ---------- machine-translated subtitles ----------
SUBS = f"{STATE}/subs"
SUB_MAX = 1200                        # cues; longer videos get truncated, not dropped


def _inv(path, timeout=60):
    return urllib.request.urlopen(INV + path, timeout=timeout).read()


def _vtt_cues(text):
    """[(timing line, [text lines])] in file order."""
    out, cur = [], None
    for line in text.replace("\r\n", "\n").split("\n"):
        if "-->" in line:
            cur = (line, [])
            out.append(cur)
        elif cur is not None:
            if line.strip() == "":
                cur = None
            else:
                cur[1].append(line)
    return out


def _translate(lines, tl=SUB_LANG):
    """Google first - a small local model turns "All right" into nonsense - and fall
    back to LibreTranslate (if configured) when Google refuses. Google's dict-chrome endpoint
    takes repeated q= parameters and answers one string per line, in order.
    (translate.googleapis.com/gtx often answers 429 from cloud addresses; this
    one does not.)"""
    UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"}
    GOOGLE = "https://clients5.google.com/translate_a/t?client=dict-chrome-ex&sl=en&tl=" + tl

    def google(chunk):
        url = GOOGLE + "".join("&q=" + urllib.parse.quote(x) for x in chunk)
        if len(url) > 7000:
            raise ValueError("too long")
        r = json.loads(urllib.request.urlopen(
            urllib.request.Request(url, headers=UA), timeout=60).read())
        out = [x if isinstance(x, str) else (x[0] if x else "") for x in r]
        if len(out) != len(chunk):
            raise ValueError("line count %d != %d" % (len(out), len(chunk)))
        return out

    def libre(chunk):
        if not LT:
            return chunk
        body = json.dumps({"q": chunk, "source": "en", "target": tl,
                           "format": "text"}).encode()
        req = urllib.request.Request(LT + "/translate", data=body,
                                     headers={"Content-Type": "application/json"})
        t = json.loads(urllib.request.urlopen(req, timeout=120).read()).get("translatedText")
        return t if isinstance(t, list) and len(t) == len(chunk) else chunk

    done, engine = [], "google"
    for i in range(0, len(lines), 20):
        chunk = lines[i:i + 20]
        try:
            done += google(chunk)
        except Exception as ex:
            log("sub", "google FAIL %s (falling back)" % repr(ex)[:70])
            engine = "libretranslate"
            try:
                done += libre(chunk)
            except Exception as ex2:
                log("sub", "libre FAIL %s" % repr(ex2)[:70])
                done += chunk
        time.sleep(0.2)
    log("sub", "translated %d lines via %s" % (len(lines), engine), every=0)
    return done


def sub_vtt(vid, tl=SUB_LANG):
    """Translated VTT for one video id, cached on disk."""
    # both end up in a file name: only a video id and a language code may get through
    if not re.fullmatch(r"[\w-]{11}", vid) or not re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2,4})?", tl):
        raise ValueError("bad video id or language")
    os.makedirs(SUBS, exist_ok=True)
    cache = os.path.join(SUBS, "%s.%s.vtt" % (vid, tl))
    if os.path.dirname(os.path.abspath(cache)) != os.path.abspath(SUBS):
        raise ValueError("bad cache path")
    if os.path.exists(cache):
        return open(cache, "rb").read()
    t0 = time.time()
    lst = json.loads(_inv("/api/v1/captions/" + vid))
    tracks = lst.get("captions") or []
    # prefer English, else whatever the video has
    pick = next((c for c in tracks if (c.get("languageCode") or "").startswith("en")), None) \
        or (tracks[0] if tracks else None)
    if not pick:
        raise ValueError("This video has no captions to translate")
    raw = _inv(pick["url"]).decode("utf-8", "replace")
    cues = _vtt_cues(raw)[:SUB_MAX]
    if not cues:
        raise ValueError("Couldn't read the captions")
    flat, idx = [], []
    for ci, (_, lines) in enumerate(cues):
        for li, ln in enumerate(lines):
            if ln.strip():
                idx.append((ci, li))
                flat.append(re.sub(r"<[^>]+>", "", ln).strip())
    ko = _translate(flat, tl)
    for (ci, li), text in zip(idx, ko):
        cues[ci][1][li] = text
    out = ["WEBVTT", ""]
    for timing, lines in cues:
        out += [timing] + lines + [""]
    data = "\n".join(out).encode("utf-8")
    tmp = cache + ".part"
    open(tmp, "wb").write(data)
    os.replace(tmp, cache)
    log("sub", "%s %s->%s %d cues" % (vid, pick.get("languageCode", "?"), tl, len(cues)), t0, every=0)
    return data


def jload(p, d):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return d


def jsave(p, v):
    # titles are any language: always UTF-8, whatever the system locale says
    t = p + ".tmp"
    with open(t, "w", encoding="utf-8") as f:
        json.dump(v, f, ensure_ascii=False)
    os.replace(t, p)


def get(url, t=15):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=t).read()


def resolve_master(vid):
    # mweb + a BotGuard PO token (bgutil container) gives HLS URLs that keep
    # working; android_vr needs no token but its segments 403 after ~20 s.
    t0 = time.time()
    # (install the bgutil-ytdlp-pot-provider plugin next to yt-dlp for the PO token)
    out = subprocess.run([YTDLP] + (["--proxy", "http://" + YT_PROXY] if YT_PROXY else []) + ["--extractor-args",
                          "youtube:player_client=mweb", "-j", "https://www.youtube.com/watch?v=" + vid],
                         capture_output=True, text=True, timeout=150,
                         env={**os.environ, "PATH": os.environ.get("PATH", "") + ":/usr/local/bin"})
    try:
        info = json.loads(out.stdout)
    except Exception:
        err = (out.stderr.strip().splitlines() or ["?"])[-1][:200]
        log("live", f"resolve {vid} FAIL yt-dlp {err[:120]}", t0, every=0)
        raise RuntimeError("Couldn't get the live stream: " + err)
    variants = {}
    for f in info.get("formats", []):
        if "m3u8" not in (f.get("protocol") or "") or f.get("vcodec") in (None, "none") or not f.get("height"):
            continue
        inf = (f'#EXT-X-STREAM-INF:BANDWIDTH={int((f.get("tbr") or 1000) * 1000)},'
               f'RESOLUTION={f.get("width")}x{f["height"]},FRAME-RATE={f.get("fps") or 30},'
               f'CODECS="{f.get("vcodec")},{f.get("acodec") or "mp4a.40.2"}"')
        variants[str(f["format_id"])] = (inf, f["url"])
    if not variants:
        log("live", f"resolve {vid} FAIL no-hls-variants", t0, every=0)
        raise RuntimeError("Not a live stream, or the stream has ended")
    log("live", f"resolve {vid} ok variants={len(variants)}", t0, every=0)
    return {"at": time.time(), "variants": variants, "used": time.time()}


LIVE_WINDOW = 40          # seconds of live history offered to the player


def live_window(text):
    """YouTube's mweb live playlist lists only the newest 3 one-second segments, so the
    player can never sit more than ~3 s behind the edge and one slow segment stalls it.
    Older segments stay downloadable by changing /sq/N/ in the URL, so rebuild the
    playlist with LIVE_WINDOW segments ending at the newest one."""
    segs = [l for l in text.splitlines() if l.startswith("https://")]
    m = re.search(r"/sq/(\d+)/", segs[-1]) if segs else None
    if not m:
        return "\n".join(LIVE_BASE + base64.urlsafe_b64encode(l.encode()).decode().rstrip("=") + ".ts"
                         if l.startswith("https://") else l for l in text.splitlines()) + "\n"
    durs = re.findall(r"#EXTINF:([\d.]+)", text)
    dur = durs[-1] if durs else "1.0"
    last, tmpl = int(m.group(1)), segs[-1]
    first = max(0, last - LIVE_WINDOW + 1)
    out = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:%d" % max(1, round(float(dur))),
           "#EXT-X-MEDIA-SEQUENCE:%d" % first]
    for sq in range(first, last + 1):
        u = tmpl.replace("/sq/%d/" % last, "/sq/%d/" % sq, 1)
        out += ["#EXTINF:%s," % dur, LIVE_BASE + base64.urlsafe_b64encode(u.encode()).decode().rstrip("=") + ".ts"]
    return "\n".join(out) + "\n"


def live_segment_retry(u, code):
    """Re-fetch segment sq of (vid, itag) through the current playlist URL template."""
    m = re.search(r"/id/([\w-]{11})[^/]*/itag/(\d+)/.*?/sq/(\d+)/", u)
    if not m:
        return None
    vid, itag, sq = m.group(1), m.group(2), int(m.group(3))
    for attempt in range(3):
        try:
            e = live_entry(vid, force=(code == 403 and attempt == 0))
            text = YT.open(e["variants"][itag][1], timeout=15).read().decode()
            segs = [l for l in text.splitlines() if l.startswith("https://")]
            last = int(re.search(r"/sq/(\d+)/", segs[-1]).group(1))
            if sq > last:                      # not produced yet: wait for it
                time.sleep(min(3, sq - last))
                continue
            u2 = segs[-1].replace("/sq/%d/" % last, "/sq/%d/" % sq, 1)
            r = YT.open(urllib.request.Request(u2), timeout=30)
            log("live", f"segment {code} recovered {vid}/{itag}/sq{sq} try{attempt + 1}", every=0)
            return r
        except Exception as ex:
            code = getattr(ex, "code", code)
            time.sleep(1)
    log("live", f"segment {code} NOT recovered {vid}/{itag}/sq{sq}", every=0)
    return None


def live_entry(vid, force=False):
    # URLs are re-resolved every 10 min while watched, and at once on a 403.
    e = LIVE_CACHE.get(vid)
    if force or not e or time.time() - e["at"] > 1800:
        e2 = resolve_master(vid)
        e2["used"] = time.time()
        LIVE_CACHE[vid] = e = e2
        if not any(t.name == "live-" + vid for t in threading.enumerate()):
            threading.Thread(target=live_refresher, args=(vid,), name="live-" + vid, daemon=True).start()
    e["used"] = time.time()
    return e


def live_refresher(vid):
    while True:
        time.sleep(600)
        e = LIVE_CACHE.get(vid)
        if not e or time.time() - e["used"] > 120:
            LIVE_CACHE.pop(vid, None)
            return
        try:
            n = resolve_master(vid)
            n["used"] = e["used"]
            LIVE_CACHE[vid] = n
        except Exception as ex:
            log("live", f"re-resolve {vid} FAIL {repr(ex)[:120]}", every=0)


# ---------- live pull-ahead: segments are fetched before the player asks ----------
# Measured: a 1-second live segment took 1.5 s through a proxy when every fetch
# opened a new TCP + CONNECT + TLS session, 0.45 s on a kept-alive one. So:
#  - GV_POOL keeps HTTPS connections to googlevideo open (through YT_PROXY if set);
#  - a LivePull per watched (video, itag) polls the playlist every second and pulls each
#    new segment into memory with a few parallel workers. The player sits ~20 s behind
#    the edge (the player's liveSegmentsDelay), so slow or failed segments are retried
#    here long before it needs them, and it is served from RAM at LAN speed.
import http.client
import queue

PULL_KEEP = 60        # segments kept per variant (~1 min, ~40 MB at 1080p)
PULL_WORKERS = 4
PULL_IDLE = 30        # stop pulling a variant this long after the player last asked


class GVPool:
    """Keep-alive HTTPS connections to googlevideo hosts, through YT_PROXY when set."""

    def __init__(self, per_host=8):
        self.idle, self.lock, self.per_host = {}, threading.Lock(), per_host

    def get(self, url, timeout=20, hops=3):
        sp = urllib.parse.urlsplit(url)
        host, path = sp.hostname, sp.path + ("?" + sp.query if sp.query else "")
        for attempt in range(2):
            with self.lock:
                lst = self.idle.get(host)
                c = lst.pop() if lst else None
            fresh = c is None
            if fresh:
                if YT_PROXY:
                    ph, pp = YT_PROXY.rsplit(":", 1)
                    c = http.client.HTTPSConnection(ph, int(pp), timeout=timeout)
                    c.set_tunnel(host, 443)
                else:
                    c = http.client.HTTPSConnection(host, 443, timeout=timeout)
            elif c.sock:
                c.sock.settimeout(timeout)
            try:
                c.request("GET", path)
                r = c.getresponse()
                body = r.read()
            except Exception:
                c.close()
                if fresh or attempt:
                    raise
                continue                      # the pooled connection had gone stale
            if r.will_close:
                c.close()
            else:
                with self.lock:
                    lst = self.idle.setdefault(host, [])
                    if len(lst) < self.per_host:
                        lst.append(c)
                    else:
                        c.close()
            if r.status in (301, 302, 303, 307, 308) and hops and r.getheader("Location"):
                return self.get(urllib.parse.urljoin(url, r.getheader("Location")), timeout, hops - 1)
            return r.status, r.getheader("Content-Type"), body


GV_POOL = GVPool()
LIVE_PULL, LIVE_PULL_LOCK = {}, threading.Lock()


def _sq(url):
    m = re.search(r"/sq/(\d+)/", url)
    return int(m.group(1)) if m else None


def ts_first_pts(ts):
    """First video PTS (90 kHz) in an MPEG-TS segment, or None."""
    for i in range(0, min(len(ts), 188 * 4000) - 187, 188):
        pk = ts[i:i + 188]
        if pk[0] != 0x47 or not (pk[1] & 0x40):
            continue
        afc, off = (pk[3] >> 4) & 3, 4
        if afc == 2:
            continue
        if afc == 3:
            off += 1 + pk[4]
        pes = pk[off:]
        if len(pes) < 14 or pes[:3] != b"\x00\x00\x01" or not 0xE0 <= pes[3] <= 0xEF or not pes[7] & 0x80:
            continue
        b = pes[9:14]
        return ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1
    return None


class LivePull:
    def __init__(self, vid, itag):
        self.vid, self.itag = vid, itag
        self.cond = threading.Condition()
        self.segs = {}            # sq -> (content type, bytes)
        self.inflight = set()     # queued or being fetched
        self.fetching = set()     # being fetched right now
        self.urgent = set()       # re-queued at the front because the player is waiting
        self.tmpl, self.last, self.queued = None, -1, -1
        self.text, self.text_at = None, 0.0
        self.used = time.time()
        self.q = queue.PriorityQueue()
        # per segment: exact duration, discontinuity before it, discontinuity sequence -
        # from YouTube's own playlist while the segment is listed there, else worked
        # out from the video timestamps of the segments we pulled
        self.meta, self.pts = {}, {}
        self.stats = {"pulled": 0, "hit": 0, "miss": 0, "retry": 0, "fail": 0}
        threading.Thread(target=self.watch, name=f"pull-{vid}-{itag}", daemon=True).start()
        for i in range(PULL_WORKERS):
            threading.Thread(target=self.worker, name=f"pullw-{vid}-{itag}-{i}", daemon=True).start()

    # -- the playlist edge
    def watch(self):
        fails = 0
        while time.time() - self.used < PULL_IDLE:
            t0 = time.time()
            try:
                e = live_entry(self.vid)
                st, _, body = GV_POOL.get(e["variants"][self.itag][1], timeout=15)
                if st == 403:
                    live_entry(self.vid, force=True)
                    raise RuntimeError("playlist 403")
                if st != 200:
                    raise RuntimeError(f"playlist {st}")
                text = body.decode()
                segs = [l for l in text.splitlines() if l.startswith("https://")]
                last = _sq(segs[-1]) if segs else None
                if last is None:
                    raise RuntimeError("no segments")
                mm = re.search(r"#EXT-X-DISCONTINUITY-SEQUENCE:(\d+)", text)
                d, dur, disc = int(mm.group(1)) if mm else 0, None, False
                found = {}
                for line in text.splitlines():
                    if line == "#EXT-X-DISCONTINUITY":
                        disc, d = True, d + 1
                    elif line.startswith("#EXTINF:"):
                        dur = float(line[8:].split(",")[0])
                    elif line.startswith("https://"):
                        k = _sq(line)
                        if k is not None and dur:
                            found[k] = {"dur": dur, "disc": disc, "dseq": d}
                        dur, disc = None, False
                with self.cond:
                    self.meta.update(found)
                    self.text, self.text_at, self.tmpl, self.last = text, time.time(), segs[-1], last
                    start = max(self.queued + 1, last - 25)       # first poll: backfill what the player will want
                    for sq in range(start, last + 1):
                        if sq not in self.segs and sq not in self.inflight:
                            self.inflight.add(sq)
                            self.q.put((1, sq))
                    self.queued = max(self.queued, last)
                    for old in [k for k in self.segs if k < last - PULL_KEEP]:
                        del self.segs[old]
                    for dct in (self.meta, self.pts):
                        for old in [k for k in dct if k < last - PULL_KEEP - 5]:
                            del dct[old]
                    self.cond.notify_all()
                fails = 0
            except Exception as ex:
                fails += 1
                log("live", f"pull {self.vid}/{self.itag} playlist FAIL {repr(ex)[:100]}", every=30)
                if fails >= 3:
                    try:
                        live_entry(self.vid, force=True)
                    except Exception:
                        pass
                    fails = 0
            time.sleep(max(0.2, 1.0 - (time.time() - t0)))
        with LIVE_PULL_LOCK:
            if LIVE_PULL.get((self.vid, self.itag)) is self:
                del LIVE_PULL[(self.vid, self.itag)]
        for _ in range(PULL_WORKERS):
            self.q.put((9, -1))
        log("live", f"pull {self.vid}/{self.itag} stopped {self.stats}", every=0)

    # -- segment fetchers
    def worker(self):
        while True:
            _, sq = self.q.get()
            if sq < 0:
                return
            with self.cond:
                if sq in self.segs or sq in self.fetching:
                    continue                                # duplicate of an urgent re-queue
                self.fetching.add(sq)
            got = None
            for attempt in range(5):
                with self.cond:
                    tmpl, last = self.tmpl, self.last
                if sq < last - PULL_KEEP:
                    break                                   # fell out of the window
                if sq > last:
                    time.sleep(0.5)                          # not produced yet
                    continue
                try:
                    st, ctype, body = GV_POOL.get(tmpl.replace("/sq/%d/" % last, "/sq/%d/" % sq, 1), timeout=20)
                    if st == 200 and body:
                        got = (ctype or "video/mp2t", body)
                        break
                    if st == 403:
                        live_entry(self.vid, force=True)
                except Exception:
                    pass
                self.stats["retry"] += 1
                time.sleep(0.3 * (attempt + 1))
            with self.cond:
                self.inflight.discard(sq)
                self.fetching.discard(sq)
                self.urgent.discard(sq)
                if got:
                    self.segs[sq] = got
                    self.stats["pulled"] += 1
                    pts = ts_first_pts(got[1])
                    if pts is not None:
                        self.pts[sq] = pts
                else:
                    self.stats["fail"] += 1
                self.cond.notify_all()

    # -- what the HTTP handler calls
    def playlist(self):
        self.used = time.time()
        with self.cond:
            if self.text is None:
                self.cond.wait(8)
            return self.text if self.text and time.time() - self.text_at < 3 else None

    def window(self):
        """Our LIVE_WINDOW-segment playlist with YouTube's real durations and
        discontinuities, so the player's timeline matches the video and it never
        jumps a 'gap' that is really a timestamp reset."""
        with self.cond:
            if self.tmpl is None or not self.meta:
                return None
            last, tmpl = self.last, self.tmpl
            first = max(0, last - LIVE_WINDOW + 1, last - PULL_KEEP)
            known = [m["dur"] for m in self.meta.values()]
            typical = max(set(known), key=known.count)
            durs, discs = {}, {}
            for sq in range(first, last + 1):
                m = self.meta.get(sq)
                if m:
                    durs[sq], discs[sq] = m["dur"], m["disc"]
                    continue
                a, b, prev = self.pts.get(sq), self.pts.get(sq + 1), self.pts.get(sq - 1)
                dt = ((b - a) % (1 << 33)) / 90000 if a is not None and b is not None else None
                durs[sq] = dt if dt and 0.3 * typical < dt < 1.7 * typical else typical
                jump = ((a - prev) % (1 << 33)) / 90000 if a is not None and prev is not None else None
                discs[sq] = bool(jump is not None and abs(jump - durs.get(sq - 1, typical)) > 1.0)
            # discontinuity sequence of the first segment, counted back from a known one
            anchor = next((sq for sq in range(first, last + 1) if sq in self.meta), None)
            dseq = 0
            if anchor is not None:
                dseq = self.meta[anchor]["dseq"] - sum(1 for sq in range(first + 1, anchor + 1) if discs[sq])
            out = ["#EXTM3U", "#EXT-X-VERSION:3",
                   "#EXT-X-TARGETDURATION:%d" % max(1, round(max(durs.values()) + 0.4)),
                   "#EXT-X-MEDIA-SEQUENCE:%d" % first, "#EXT-X-DISCONTINUITY-SEQUENCE:%d" % max(0, dseq)]
            for sq in range(first, last + 1):
                if discs[sq] and sq != first:
                    out.append("#EXT-X-DISCONTINUITY")
                u = tmpl.replace("/sq/%d/" % last, "/sq/%d/" % sq, 1)
                out += ["#EXTINF:%.6f," % durs[sq], LIVE_BASE + base64.urlsafe_b64encode(u.encode()).decode().rstrip("=") + ".ts"]
            return "\n".join(out) + "\n"

    def segment(self, sq, timeout=15):
        self.used = time.time()
        deadline = time.time() + timeout
        with self.cond:
            while True:
                if sq in self.segs:
                    self.stats["hit"] += 1
                    return self.segs[sq]
                if self.last >= 0 and (sq < self.last - PULL_KEEP or sq > self.last + 5):
                    break
                if 0 <= sq <= self.last and sq not in self.fetching and sq not in self.urgent:
                    self.inflight.add(sq)
                    self.urgent.add(sq)
                    self.q.put((0, sq))                     # the player is waiting: jump the queue
                left = deadline - time.time()
                if left <= 0:
                    break
                self.cond.wait(left)
        self.stats["miss"] += 1
        return None


def live_pull(vid, itag, create=True):
    with LIVE_PULL_LOCK:
        p = LIVE_PULL.get((vid, itag))
        if p is None and create:
            p = LIVE_PULL[(vid, itag)] = LivePull(vid, itag)
            log("live", f"pull {vid}/{itag} start", every=0)
        return p


# ---------- self-healing egress: new WARP address when YouTube starts refusing ----------
# Sooner or later every uncached video fails with "Sign in to confirm you're not a bot /
# This helps protect our community" - YouTube has flagged the WARP exit address.
# A warp-cli disconnect/connect hands out a different one and playback comes straight
# back, so do that automatically and replay the request.
ROTATE_GAP = 45            # seconds between two rotations
ROTATE_BURST = (6, 1800)   # at most 6 rotations per 30 min, then pause and log
rot_lock, rot_hist = threading.Lock(), []


def warp_ip():
    try:
        t = YT.open("https://www.cloudflare.com/cdn-cgi/trace", timeout=8).read().decode()
        return re.search(r"^ip=(.+)$", t, re.M).group(1)
    except Exception:
        return "?"


def yt_blocked(d=None, body=b""):
    """True when YouTube refused us as a bot rather than refusing the video."""
    if d is not None:
        ps = d.get("playabilityStatus") or {}
        text = json.dumps(ps, ensure_ascii=False).lower()
        return ps.get("status") == "LOGIN_REQUIRED" and ("bot" in text or "protect our community" in text or not ps.get("reason")) \
            or "protect our community" in text
    return b"protect our community" in body or b"confirm you\xe2\x80\x99re not a bot" in body or b"not a bot" in body


def warp_rotate(why, started):
    """Get a new WARP exit address. Requests that were already waiting when another
    thread rotated just retry instead of rotating again. Returns True if the caller
    should retry."""
    if not (WARP_ROTATE and YT_PROXY):
        return False
    with rot_lock:
        now = time.time()
        if rot_hist and rot_hist[-1] > started:
            return True                              # someone rotated while we waited
        if rot_hist and now - rot_hist[-1] < ROTATE_GAP:
            return False
        recent = [t for t in rot_hist if now - t < ROTATE_BURST[1]]
        if len(recent) >= ROTATE_BURST[0]:
            log("warp", f"rotate skipped: {len(recent)} in 30 min ({why})", every=600)
            return False
        old = warp_ip()
        subprocess.run(["warp-cli", "--accept-tos", "disconnect"], capture_output=True, timeout=20)
        time.sleep(1)
        subprocess.run(["warp-cli", "--accept-tos", "connect"], capture_output=True, timeout=20)
        # wait until traffic flows again (in a container the status line can stay at
        # "Validating proxy configuration" while the proxy already works)
        new = "?"
        for _ in range(40):
            time.sleep(1)
            new = warp_ip()
            if new != "?":
                break
        rot_hist.append(time.time())
        del rot_hist[:-20]
        log("warp", f"rotated {old} -> {new} ({why}) in {time.time() - now:.1f}s", every=0)
        return True


# ---------- library: subscriptions / history / resume points / playlists ----------
# Every device shares this one file (put TubeHaus behind your own login - it has no
# accounts of its own). The subscription feed is gathered from YouTube RSS every 15 min.
YTLIB = f"{STATE}/ytlib.json"
yt_lock = threading.Lock()
yt_feed = {"items": {}, "updated": 0, "running": False}   # cid -> [entries]


def _yt_load():
    d = jload(YTLIB, {})
    for k, v in (("subs", {}), ("hist", []), ("pos", {}), ("wl", []), ("likes", []), ("dislikes", []), ("playlists", {})):
        d.setdefault(k, v)
    return d


def _yt_meta(v):
    v = v or {}
    out = {"id": str(v.get("id", ""))[:11]}
    for k in ("title", "author", "authorId"):
        out[k] = str(v.get(k) or "")[:300]
    for k in ("len", "published", "views"):
        try:
            out[k] = int(v.get(k) or 0)
        except Exception:
            out[k] = 0
    if v.get("live"):
        out["live"] = True
    return out if re.fullmatch(r"[\w-]{11}", out["id"]) else None


def _pl_summary(pl):
    items = pl.get("items", [])
    return {"title": pl.get("title", ""), "count": len(items), "first": items[0]["id"] if items else None,
            "updated": pl.get("updated", 0), "ids": [x["id"] for x in items]}


def ytlib_get():
    with yt_lock:
        d = _yt_load()
    return {"subs": d["subs"], "wl": d["wl"], "likes": d["likes"], "dislikes": d["dislikes"],
            "playlists": {k: _pl_summary(v) for k, v in d["playlists"].items()}}


def ytlib_post(path, b):
    now = int(time.time())
    with yt_lock:
        d = _yt_load()
        res = {"ok": True}
        if path.endswith("/yt/pos"):
            vid = str(b.get("id", ""))
            if not re.fullmatch(r"[\w-]{11}", vid):
                return 400, {"error": "bad id"}
            t, ln = int(b.get("t") or 0), int(b.get("len") or 0)
            d["pos"][vid] = [t, ln, now]
            if len(d["pos"]) > 5000:
                for k in sorted(d["pos"], key=lambda k: d["pos"][k][2])[:len(d["pos"]) - 5000]:
                    del d["pos"][k]
            meta = _yt_meta(b.get("meta")) if b.get("meta") else None
            old = next((h for h in d["hist"] if h["id"] == vid), None)
            if meta or old:
                h = dict(old or {}, **(meta or {}))
                h["at"], h["id"] = now, vid
                d["hist"] = [h] + [x for x in d["hist"] if x["id"] != vid]
                d["hist"] = d["hist"][:3000]
        elif path.endswith("/yt/sub"):
            cid = str(b.get("id", ""))
            if not re.fullmatch(r"UC[\w-]{22}", cid):
                return 400, {"error": "bad channel"}
            if b.get("on"):
                d["subs"][cid] = {"name": str(b.get("name") or cid)[:200], "thumb": str(b.get("thumb") or "")[:500], "at": now}
                threading.Thread(target=yt_feed_one, args=(cid,), daemon=True).start()
            else:
                d["subs"].pop(cid, None)
                yt_feed["items"].pop(cid, None)
        elif path.endswith("/yt/subs/import"):
            added = 0
            for x in (b.get("list") or [])[:2000]:
                cid = str(x.get("id", ""))
                if re.fullmatch(r"UC[\w-]{22}", cid) and cid not in d["subs"]:
                    d["subs"][cid] = {"name": str(x.get("name") or cid)[:200], "thumb": str(x.get("thumb") or "")[:500], "at": now}
                    added += 1
            res["added"] = added
            if added:
                threading.Thread(target=yt_feed_refresh, daemon=True).start()
        elif path.endswith("/yt/list"):
            lst, on, meta = str(b.get("list", "")), bool(b.get("on")), _yt_meta(b.get("video"))
            if not meta:
                return 400, {"error": "bad video"}
            if lst in ("wl", "likes"):
                d[lst] = [x for x in d[lst] if x["id"] != meta["id"]]
                if on:
                    d[lst].insert(0, meta)
                d[lst] = d[lst][:5000]
            elif lst == "dislikes":
                d["dislikes"] = [x for x in d["dislikes"] if x != meta["id"]] + ([meta["id"]] if on else [])
            elif lst in d["playlists"]:
                pl = d["playlists"][lst]
                pl["items"] = [x for x in pl["items"] if x["id"] != meta["id"]]
                if on:
                    pl["items"].append(meta)
                pl["updated"] = now
                res["playlist"] = _pl_summary(pl)
            else:
                return 404, {"error": "Playlist not found"}
        elif path.endswith("/yt/pl/new"):
            title = str(b.get("title") or "").strip()[:150]
            if not title:
                return 400, {"error": "Please enter a name"}
            pid = "HP" + secrets.token_hex(8)
            meta = _yt_meta(b.get("video")) if b.get("video") else None
            d["playlists"][pid] = {"title": title, "items": [meta] if meta else [], "updated": now}
            res.update(id=pid, playlist=_pl_summary(d["playlists"][pid]))
        elif path.endswith("/yt/pl/del"):
            d["playlists"].pop(str(b.get("id")), None)
        elif path.endswith("/yt/pl/rename"):
            pl = d["playlists"].get(str(b.get("id")))
            if not pl:
                return 404, {"error": "Playlist not found"}
            pl["title"] = str(b.get("title") or pl["title"]).strip()[:150]
            pl["updated"] = now
            res["playlist"] = _pl_summary(pl)
        elif path.endswith("/yt/hist/del"):
            if b.get("all"):
                d["hist"], d["pos"] = [], {}
            else:
                vid = str(b.get("id", ""))
                d["hist"] = [x for x in d["hist"] if x["id"] != vid]
                d["pos"].pop(vid, None)
        else:
            return 404, {"error": "not found"}
        jsave(YTLIB, d)
    return 200, res


def ytlib_query(path, qs):
    with yt_lock:
        d = _yt_load()
    if path.endswith("/yt/pos"):
        ids = [i for i in qs.get("ids", "").split(",") if re.fullmatch(r"[\w-]{11}", i)][:200]
        return {i: d["pos"][i][:2] for i in ids if i in d["pos"]}
    if path.endswith("/yt/hist"):
        n, o, q = min(int(qs.get("n") or 40), 200), int(qs.get("o") or 0), (qs.get("q") or "").strip().lower()
        h = [x for x in d["hist"] if not q or q in (x.get("title", "") + " " + x.get("author", "")).lower()]
        return {"items": h[o:o + n], "total": len(h)}
    if path.endswith("/yt/pl"):
        pl = d["playlists"].get(qs.get("id", ""))
        return pl or {"error": "Playlist not found"}
    if path.endswith("/yt/feed"):
        subs = d["subs"]
        if time.time() - yt_feed["updated"] > 900 and not yt_feed["running"]:
            threading.Thread(target=yt_feed_refresh, daemon=True).start()
        items = [e for cid, es in list(yt_feed["items"].items()) if cid in subs for e in es]
        items.sort(key=lambda e: e["published"], reverse=True)
        return {"items": items[:400], "updated": yt_feed["updated"],
                "updating": yt_feed["running"] or bool(subs and not yt_feed["items"])}
    return {"error": "not found"}


def yt_feed_one(cid):
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
          "m": "http://search.yahoo.com/mrss/"}
    try:
        root = ET.fromstring(get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}", 15))
    except Exception as ex:
        log("ytfeed", f"{cid} FAIL {repr(ex)[:100]}")
        return
    out = []
    for e in root.findall("a:entry", ns)[:15]:
        vid = e.findtext("yt:videoId", "", ns)
        if not vid:
            continue
        link = e.find("a:link", ns)
        href = link.get("href", "") if link is not None else ""
        try:
            pub = int(datetime.datetime.fromisoformat(e.findtext("a:published", "", ns).replace("Z", "+00:00")).timestamp())
        except Exception:
            pub = 0
        st = e.find("m:group/m:community/m:statistics", ns)
        out.append({"id": vid, "title": e.findtext("a:title", "", ns), "author": e.findtext("a:author/a:name", "", ns),
                    "authorId": cid, "published": pub, "views": int(st.get("views", 0)) if st is not None else None,
                    "short": "/shorts/" in href})
    yt_feed["items"][cid] = out


def yt_fill_thumbs():
    """Imported subscriptions come without channel pictures; fill them in from Invidious."""
    with yt_lock:
        missing = [c for c, v in _yt_load()["subs"].items() if not v.get("thumb")]
    got = {}
    for cid in missing[:50]:
        try:
            d = json.loads(urllib.request.urlopen(f"{INV}/api/v1/channels/{cid}?fields=authorThumbnails", timeout=20).read())
            th = sorted(d.get("authorThumbnails") or [], key=lambda x: x.get("width", 0))
            th = next((x for x in th if x.get("width", 0) >= 88), th[-1] if th else None)
            if th:
                got[cid] = re.sub(r"^https?://(yt3\.googleusercontent\.com|yt3\.ggpht\.com)/", "/ggpht/", re.sub(r"^https?://[^/]+(?=/ggpht/)", "", th["url"]))
        except Exception as ex:
            log("ytfeed", f"thumb {cid} FAIL {repr(ex)[:80]}")
    if got:
        with yt_lock:
            d = _yt_load()
            for cid, u in got.items():
                if cid in d["subs"]:
                    d["subs"][cid]["thumb"] = u
            jsave(YTLIB, d)


def yt_feed_refresh():
    if yt_feed["running"]:
        return
    yt_feed["running"] = True
    t0 = time.time()
    try:
        with yt_lock:
            subs = list(_yt_load()["subs"])
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(6) as ex:
            list(ex.map(yt_feed_one, subs))
        yt_feed["updated"] = time.time()
        log("ytfeed", f"refreshed {len(subs)} channels", t0, every=0)
        yt_fill_thumbs()
    finally:
        yt_feed["running"] = False


def yt_feed_loop():
    while True:
        try:
            yt_feed_refresh()
        except Exception as e:
            log("ytfeed", f"loop FAIL {repr(e)[:160]}")
        time.sleep(900)


# ---------- high-quality offline save: YouTube serves 720p+ as separate video and
# audio, so fetch both through the companion and merge them with ffmpeg ----------
ytdl_jobs = {}
ytdl_lock = threading.Lock()


def _ytdl_part(vid, itag, path, prog):
    r = urllib.request.urlopen(f"{COMPANION}/companion/latest_version?id={vid}&itag={itag}&local=true", timeout=30)
    url = r.geturl()
    r.close()
    m = re.search(r"clen=(\d+)", url)
    if not m:
        raise IOError(f"itag {itag}: size unknown")
    size = int(m.group(1))
    prog[itag] = [0, size]
    with open(path, "wb") as f:
        f.truncate(size)
    step = 8_000_000

    def get(start):
        end = min(start + step, size) - 1
        for attempt in range(5):
            try:
                data = urllib.request.urlopen(f"{url}&range={start}-{end}", timeout=90).read()
                if len(data) != end - start + 1:
                    raise IOError("short read")
                with open(path, "r+b") as f:
                    f.seek(start)
                    f.write(data)
                with ytdl_lock:
                    prog[itag][0] += len(data)
                return
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(1 + attempt * 2)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(get, range(0, size, step)))


def _ytdl_run(jid, vid, vitag, aitag, ext, name):
    j = ytdl_jobs[jid]
    d = f"{YTDL_DIR}/{jid}"
    os.makedirs(d, exist_ok=True)
    t0 = time.time()
    try:
        from concurrent.futures import ThreadPoolExecutor
        vp, ap = f"{d}/v.part", f"{d}/a.part"
        with ThreadPoolExecutor(2) as ex:
            for f in [ex.submit(_ytdl_part, vid, vitag, vp, j["prog"]), ex.submit(_ytdl_part, vid, aitag, ap, j["prog"])]:
                f.result()
        j["status"] = "merge"
        out = f"{d}/out.{ext}"
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", vp, "-i", ap, "-map", "0:v:0", "-map", "1:a:0", "-c", "copy"]
        if ext == "mp4":
            cmd += ["-movflags", "+faststart"]
        r = subprocess.run(cmd + [out], capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            raise IOError("Merging failed: " + r.stderr.strip()[-200:])
        for x in (vp, ap):
            os.remove(x)
        j.update(status="done", path=out, size=os.path.getsize(out), done_at=time.time())
        log("ytdl", f"{vid} {vitag}+{aitag} done {j['size']}B", t0, every=0)
    except Exception as ex:
        j.update(status="error", err=str(ex)[:200], done_at=time.time())
        log("ytdl", f"{vid} {vitag}+{aitag} FAIL {repr(ex)[:160]}", t0, every=0)
        subprocess.run(["rm", "-rf", d])


def ytdl_start(b):
    vid, vitag, aitag = str(b.get("id", "")), str(b.get("v", "")), str(b.get("a", ""))
    ext = "webm" if b.get("ext") == "webm" else "mp4"
    if not re.fullmatch(r"[\w-]{11}", vid) or not vitag.isdigit() or not aitag.isdigit():
        return 400, {"error": "Bad request"}
    name = re.sub(r'[\\/:*?"<>|\r\n]+', " ", str(b.get("name") or vid)).strip()[:120] or vid
    ytdl_gc()
    with ytdl_lock:
        busy = [x for x in ytdl_jobs.values() if x["status"] in ("download", "merge")]
        same = next((k for k, x in ytdl_jobs.items() if x["key"] == (vid, vitag, aitag) and x["status"] != "error"), None)
        if same:
            return 200, {"job": same}
        if len(busy) >= 2:
            return 429, {"error": "Two other saves are in progress. Try again when they finish"}
        os.makedirs(YTDL_DIR, exist_ok=True)
        st = os.statvfs(YTDL_DIR)
        if st.f_bavail * st.f_frsize < 10e9:
            return 507, {"error": "Not enough disk space on the server"}
        jid = secrets.token_hex(8)
        ytdl_jobs[jid] = {"key": (vid, vitag, aitag), "status": "download", "prog": {}, "name": f"{name}.{ext}", "t": time.time()}
    threading.Thread(target=_ytdl_run, args=(jid, vid, vitag, aitag, ext, name), name="ytdl-" + jid, daemon=True).start()
    return 200, {"job": jid}


def ytdl_status(jid):
    j = ytdl_jobs.get(jid)
    if not j:
        return {"status": "error", "err": "No such job (the server may have restarted)"}
    got = sum(x[0] for x in j["prog"].values())
    tot = sum(x[1] for x in j["prog"].values()) or 1
    pct = 100 if j["status"] == "done" else round(got / tot * 95, 1) if j["status"] == "download" else 97
    return {"status": j["status"], "pct": pct, "name": j["name"], "size": j.get("size") or tot, "err": j.get("err")}


def ytdl_gc():
    # Delete files two hours after they finished. A restart empties the job list, so
    # judge by each folder's last change instead (folders in use keep changing).
    try:
        names = os.listdir(YTDL_DIR)
    except FileNotFoundError:
        return
    for k in names:
        d = f"{YTDL_DIR}/{k}"
        j = ytdl_jobs.get(k)
        if j and j["status"] in ("download", "merge"):
            continue
        try:
            newest = max([os.path.getmtime(d)] + [os.path.getmtime(f"{d}/{f}") for f in os.listdir(d)])
        except OSError:
            continue
        last = max(newest, (j or {}).get("done_at") or 0)
        if time.time() - last > 7200:
            subprocess.run(["rm", "-rf", d])
            ytdl_jobs.pop(k, None)


def ytdl_gc_loop():
    while True:
        try:
            ytdl_gc()
        except Exception as e:
            log("ytdl", f"gc FAIL {repr(e)[:120]}")
        time.sleep(1800)


# ---------- "send to TV": open screens long-poll for commands, a phone posts them ----------
cast_cond = threading.Condition()
cast_devs = {}     # device id -> {"name", "tv", "seen"}
cast_q = []        # [{"seq", "to", "cmd", "from", "at"}]
cast_state = {"seq": 0}
CAST_TYPES = ("play", "toggle", "seek", "stop")


def _cast_id(v):
    v = str(v or "")
    return v if re.fullmatch(r"[\w-]{8,64}", v) else None


def cast_devices():
    now = time.time()
    with cast_cond:
        return {"devices": [{"id": k, "name": d["name"], "tv": d["tv"]}
                            for k, d in cast_devs.items() if now - d["seen"] < 75]}


def cast_send(b):
    to = _cast_id(b.get("to"))
    cmd = b.get("cmd") if isinstance(b.get("cmd"), dict) else {}
    typ = cmd.get("type")
    if not to or typ not in CAST_TYPES:
        return 400, {"error": "Bad request"}
    clean = {"type": typ}
    if typ == "play":
        if not re.fullmatch(r"[\w-]{11}", str(cmd.get("id", ""))):
            return 400, {"error": "Invalid video ID"}
        clean["id"] = cmd["id"]
        clean["t"] = max(0, int(cmd.get("t") or 0))
        if cmd.get("list") and re.fullmatch(r"[\w-]{2,64}", str(cmd["list"])):
            clean["list"] = cmd["list"]
    if typ == "seek":
        clean["d"] = max(-3600, min(3600, int(cmd.get("d") or 0)))
    with cast_cond:
        if to not in cast_devs or time.time() - cast_devs[to]["seen"] > 75:
            return 404, {"error": "That device is off"}
        cast_state["seq"] += 1
        cast_q.append({"seq": cast_state["seq"], "to": to, "cmd": clean,
                       "from": str(b.get("from") or "")[:40], "at": time.time()})
        del cast_q[:-50]
        cast_cond.notify_all()
    log("cast", f"{typ} -> {cast_devs[to]['name']}", every=0)
    return 200, {"ok": True}


def cast_wait(qs):
    """Long poll (up to 25 s) from a receiving screen; while it waits it is listed as online."""
    did = _cast_id(qs.get("id"))
    if not did:
        return {"error": "bad id"}
    name = str(qs.get("name") or "Device")[:40]
    tv = qs.get("tv") == "1"
    try:
        since = int(qs.get("since", "-1"))
    except ValueError:
        since = -1
    deadline = time.time() + 25
    with cast_cond:
        cast_devs[did] = {"name": name, "tv": tv, "seen": time.time()}
        if since < 0:   # first connection: do not replay old commands
            return {"seq": cast_state["seq"], "cmds": []}
        while True:
            mine = [c for c in cast_q if c["seq"] > since and c["to"] == did and time.time() - c["at"] < 60]
            left = deadline - time.time()
            if mine or left <= 0:
                cast_devs[did]["seen"] = time.time()
                return {"seq": cast_state["seq"], "cmds": [{"cmd": c["cmd"], "from": c["from"]} for c in mine]}
            cast_cond.wait(left)
            cast_devs[did]["seen"] = time.time()


# ---------- live captions: YouTube gives none for live streams, so transcribe them ----------
# whisper.cpp + a small ggml model (base-q5_1 keeps up with real time on 2 ARM cores).
# Audio of the lightest variant is cut into ~10 s chunks (two segments); the text is
# stored per segment number (sq), relative to that segment's start. The player knows
# which sq it is playing and asks /api/p/live/<id>/cc.json for that range.
# A captioner stops 2 minutes after nobody asks for it.
LIVE_CC, LIVE_CC_LOCK = {}, threading.Lock()
CC_TAG = re.compile(r"[\[(（【♪*].*[\])）】♪*]")   # sound tags like [Music] (applause)
# phrases whisper invents over music or silence (mostly Korean broadcast sign-offs)
CC_JUNK = re.compile(r"^(감사합니다|시청해 ?주셔서 감사합니다|구독과 좋아요|자막 제공|"
                     r"Thanks? (you )?for watching|Subscribe|[.\-…♪\s]*)[.!]?$", re.I)


def _cc_time(t):
    h, m, rest = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


class LiveCaptioner:
    def __init__(self, vid):
        self.vid = vid
        self.cues = {}          # sq -> [[start, end, text]] (seconds from the segment start)
        self.used = time.time()
        self.status = "starting"
        self.lag = None
        self.want = None        # the segment the viewer is watching: transcribe from there
        threading.Thread(target=self.run, name="cc-" + vid, daemon=True).start()

    def _itag(self):
        e = live_entry(self.vid)

        def bw(item):
            m = re.search(r"BANDWIDTH=(\d+)", item[1][0])
            return int(m.group(1)) if m else 1 << 40
        return min(e["variants"].items(), key=bw)[0]

    def run(self):
        # Transcription runs at about real-time speed, so starting where the viewer is
        # would always be one step late. Start 3 segments (~15 s) ahead of the viewer
        # and jump ahead again whenever it falls behind.
        itag, nxt = None, None
        fail_at, fails = None, 0
        while time.time() - self.used < 120:
            try:
                if itag is None:
                    itag = self._itag()
                pl = live_pull(self.vid, itag)
                pl.used = time.time()                      # keep the segments coming meanwhile
                want = self.want
                with pl.cond:
                    last = pl.last
                    if want is None:
                        want = last - 20
                    if nxt is None or nxt < want or nxt > want + 30:
                        nxt = min(want + 3, last - 1)
                    chunk = [k for k in (nxt, nxt + 1) if k in pl.segs]
                    data = {k: pl.segs[k][1] for k in chunk}
                    meta = {k: pl.meta.get(k, {}).get("dur") for k in chunk}
                if not chunk or chunk[0] != nxt:
                    if nxt < last - 3:
                        nxt += 1                            # a segment we never got: skip it
                    else:
                        time.sleep(1)
                    continue
                if len(chunk) < 2 and nxt + 1 >= last - 3:
                    time.sleep(1)                           # next segment still coming: wait to make 10 s
                    continue
                try:
                    self.transcribe(chunk, [data[k] for k in chunk], [meta.get(k) or 5.0 for k in chunk])
                    fails = 0
                except Exception as ex:
                    fails = fails + 1 if fail_at == nxt else 1
                    fail_at = nxt
                    if fails < 3:
                        raise                               # retried below, same chunk
                    log("livecc", f"{self.vid} sq {chunk[0]}-{chunk[-1]} skipped after {fails} failures: {repr(ex)[:120]}", every=0)
                    fails = 0
                nxt = chunk[-1] + 1
                self.lag = last - chunk[-1]
                self.status = "ok"
                for old in [k for k in self.cues if k < last - 80]:
                    del self.cues[old]
            except Exception as ex:
                self.status = "error"
                log("livecc", f"{self.vid} FAIL {repr(ex)[:160]}", every=30)
                time.sleep(3)
        with LIVE_CC_LOCK:
            if LIVE_CC.get(self.vid) is self:
                del LIVE_CC[self.vid]
        log("livecc", f"{self.vid} stopped", every=0)

    def transcribe(self, sqs, blobs, durs):
        t0 = time.time()
        d = f"/var/tmp/tubehaus-cc-{self.vid}"
        os.makedirs(d, exist_ok=True)
        wav = f"{d}/chunk.wav"
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "mpegts", "-i", "pipe:0", "-vn",
                            "-ar", "16000", "-ac", "1", wav], input=b"".join(blobs), capture_output=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError("ffmpeg " + r.stderr.decode()[-120:])
        # one thread and a 768-frame audio window (~15 s instead of 30 s): same speed
        # on a 10 s chunk, never more than one core, and no 100 % CPU bursts
        r = subprocess.run(["nice", "-n", "10", WHISPER_BIN, "-m", WHISPER_MODEL, "-f", wav, "-l", WHISPER_LANG,
                            "-t", "1", "-ac", "768", "-np", "-sns"], capture_output=True, timeout=60)
        if r.returncode != 0:     # e.g. a bad model path or a crash: no text is not "silence"
            raise RuntimeError("whisper exit %d: %s" % (r.returncode, r.stderr.decode("utf-8", "replace").strip()[-160:]))
        starts, acc = [], 0.0
        for dd in durs:
            starts.append(acc)
            acc += dd
        n = 0
        for line in r.stdout.decode("utf-8", "replace").splitlines():
            m = re.match(r"\[(\d+:\d+:[\d.]+) --> (\d+:\d+:[\d.]+)\]\s*(.*)$", line.strip())
            if not m:
                continue
            a, b, text = _cc_time(m.group(1)), _cc_time(m.group(2)), m.group(3).strip()
            text = re.sub(r"^[-–]\s*", "", text).strip()
            if not text or CC_JUNK.match(text) or CC_TAG.fullmatch(text):
                continue
            i = max(i for i, st in enumerate(starts) if st <= a + 0.01)   # the segment the line starts in
            self.cues.setdefault(sqs[i], []).append([round(a - starts[i], 2), round(b - starts[i], 2), text[:200]])
            n += 1
        log("livecc", f"{self.vid} sq {sqs[0]}-{sqs[-1]} {n} cues", t0, every=60)

    def get(self, lo, hi):
        self.used = time.time()
        self.want = lo
        return {str(k): v for k, v in self.cues.items() if lo <= k <= hi}


def live_cc(vid):
    with LIVE_CC_LOCK:
        c = LIVE_CC.get(vid)
        if c is None:
            c = LIVE_CC[vid] = LiveCaptioner(vid)
        return c


# ---------- self-check: `python3 tubehaus_server.py --doctor` / GET /api/p/health ----------
def doctor():
    """Check every piece TubeHaus depends on and print one line each. Returns the
    number of failed required checks (the exit code), so it can run in scripts."""
    import shutil
    fails = 0

    def line(ok, name, detail, required=True):
        nonlocal fails
        mark = "ok  " if ok else ("FAIL" if required else "warn")
        if not ok and required:
            fails += 1
        print(f"[{mark}] {name:<22} {detail}")

    def get(url, opener=None, timeout=10):
        return (opener or urllib.request.build_opener()).open(url, timeout=timeout).read()

    # 1. WARP (or no proxy)
    if YT_PROXY:
        try:
            t = get("https://www.cloudflare.com/cdn-cgi/trace", YT).decode()
            warp = re.search(r"^warp=(\w+)$", t, re.M)
            ip = re.search(r"^ip=(.+)$", t, re.M)
            line(warp and warp.group(1) in ("on", "plus"), "WARP proxy",
                 f"{YT_PROXY} -> warp={warp.group(1) if warp else '?'} ip={ip.group(1) if ip else '?'}")
        except Exception as ex:
            line(False, "WARP proxy", f"{YT_PROXY} unreachable ({type(ex).__name__}: {str(ex)[:80]})")
    else:
        line(True, "WARP proxy", "disabled (HT_YT_PROXY empty): YouTube is reached directly", required=False)

    # 2. YouTube itself, through the same path
    try:
        body = get("https://www.youtube.com/generate_204", YT)
        line(True, "YouTube reachable", "via WARP" if YT_PROXY else "direct")
    except urllib.error.HTTPError as ex:
        line(ex.code == 204, "YouTube reachable", f"HTTP {ex.code}")
    except Exception as ex:
        line(False, "YouTube reachable", f"{type(ex).__name__}: {str(ex)[:80]}")

    # 3. Invidious
    try:
        st = json.loads(get(INV + "/api/v1/stats"))
        line(True, "Invidious", f"{INV} (version {st.get('software', {}).get('version', '?')})")
    except Exception as ex:
        line(False, "Invidious", f"{INV} ({type(ex).__name__}: {str(ex)[:80]})")

    # 4. invidious-companion: does a real video resolve?
    try:
        d = json.loads(get(INV + "/api/v1/videos/jNQXAC9IVRw?local=true", timeout=30))
        n = len(d.get("adaptiveFormats") or [])
        line(n > 0, "Video lookup", f"'{d.get('title', '?')}' -> {n} streams" if n else
             f"no streams: {str(d.get('error') or d)[:100]}")
    except urllib.error.HTTPError as ex:
        line(False, "Video lookup", f"HTTP {ex.code}: {ex.read()[:120]!r}")
    except Exception as ex:
        line(False, "Video lookup", f"{type(ex).__name__}: {str(ex)[:80]}")

    # 5. optional tools
    yt = shutil.which(YTDLP) or (YTDLP if os.path.exists(YTDLP) else "")
    line(bool(yt), "yt-dlp (live streams)", yt or f"not found ({YTDLP})", required=False)
    ff = shutil.which("ffmpeg")
    line(bool(ff), "ffmpeg (HD downloads)", ff or "not found", required=False)
    wok = bool(WHISPER_BIN and WHISPER_MODEL and os.path.exists(WHISPER_BIN) and os.path.exists(WHISPER_MODEL))
    line(wok, "whisper (live captions)", f"{WHISPER_BIN} + {os.path.basename(WHISPER_MODEL)}" if wok else
         "not configured (HT_WHISPER_BIN / HT_WHISPER_MODEL)", required=False)

    # 6. state directory
    try:
        os.makedirs(STATE, exist_ok=True)
        probe = os.path.join(STATE, ".doctor")
        open(probe, "w").close()
        os.remove(probe)
        line(True, "State directory", os.path.abspath(STATE))
    except Exception as ex:
        line(False, "State directory", f"{STATE} not writable ({ex})")

    print("\nAll required checks passed." if not fails else f"\n{fails} required check(s) failed.")
    return fails


def health():
    now = time.time()
    return {"ok": True, "warp_proxy": bool(YT_PROXY), "warp_rotate": WARP_ROTATE,
            "warp_rotations_30min": len([t for t in rot_hist if now - t < 1800]),
            "live_captions": bool(WHISPER_BIN and WHISPER_MODEL)}


class H(BaseHTTPRequestHandler):
    # Invidious re-dialled TCP for every companion call under HTTP/1.0; 1.1 keeps
    # the socket. Every reply below either sends a real Content-Length or sets
    # close_connection, so the framing stays honest. Bodies are unchanged.
    protocol_version = "HTTP/1.1"

    def handle_one_request(self):
        # Handler exceptions used to escape into a socketserver traceback nobody reads.
        t0 = time.time()
        try:
            return BaseHTTPRequestHandler.handle_one_request(self)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as ex:
            self.close_connection = True
            log("http", "%s %s unhandled %s: %s" % (getattr(self, "command", "?"),
                str(getattr(self, "path", "?"))[:60], type(ex).__name__, str(ex)[:120]), t0, every=0)

    def send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()


    def qs(self):
        return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}

    def do_GET(self):
        p = self.path.split("?")[0].rstrip("/")
        if "/yt/cast/" in p:
            if p.endswith("/cast/devices"):
                return self.send(200, cast_devices())
            if p.endswith("/cast/wait"):
                d = cast_wait(self.qs())
                return self.send(400 if d.get("error") else 200, d)
        if p.endswith("/yt/dl") or p.endswith("/yt/dl/file"):
            jid = self.qs().get("job", "")
            if p.endswith("/yt/dl"):
                return self.send(200, ytdl_status(jid))
            j = ytdl_jobs.get(jid)
            if not j or j["status"] != "done":
                return self.send(404, {"error": "Not ready yet"})
            self.send_response(200)
            self.send_header("Content-Type", "video/webm" if j["path"].endswith(".webm") else "video/mp4")
            self.send_header("Content-Length", str(j["size"]))
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(j["name"]))
            self.end_headers()
            try:
                with open(j["path"], "rb") as f:
                    while chunk := f.read(1 << 20):
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            j["done_at"] = time.time()
            return
        if "/yt/" in p and "/api/p/" in self.path:
            if p.endswith("/yt/lib"):
                return self.send(200, ytlib_get())
            d = ytlib_query(p, self.qs())
            return self.send(404 if d.get("error") else 200, d)
        if "/live/" in p:
            if p.endswith("/cc.json") and not (WHISPER_BIN and WHISPER_MODEL):
                return self.send(200, {"status": "off", "lag": None, "cues": {}})
            return self.live(p)
        if "/sub/" in p:
            vid = p.rsplit("/", 1)[-1].replace(".vtt", "")
            if not re.fullmatch(r"[\w-]{11}", vid):
                return self.send(400, {"error": "Invalid video ID"})
            try:
                tl = self.qs().get("tl") or SUB_LANG     # the viewer's interface language
                if not re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2,4})?", tl):
                    return self.send(400, {"error": "bad language"})
                data = sub_vtt(vid, tl)
            except Exception as ex:
                log("sub", "%s FAIL %s" % (vid, repr(ex)[:100]))
                return self.send(502, {"error": str(ex)})
            self.send_response(200)
            self.send_header("Content-Type", "text/vtt; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if p.endswith("/api/p/health"):
            return self.send(200, health())
        if p.endswith("/plog"):
            # the player is the only place that sees a playback failure; without this
            # beacon a video that dies mid-playback leaves no trace on the server
            log("player", " ".join("%s=%s" % (k, v[:120]) for k, v in sorted(self.qs().items())) or "?", every=0)
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if p.startswith("/companion"):
            return self.companion_proxy()
        if "/api/v1/videos/" in p:
            return self.videos_api()
        if p.endswith("/latest_version"):
            return self.download()
        self.send(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0].rstrip("/")
        body_bytes = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        if p.startswith("/companion"):
            return self.companion_proxy(body_bytes)
        try:
            body = json.loads(body_bytes or b"{}")
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        if p.endswith("/yt/cast/send"):
            return self.send(*cast_send(body))
        if p.endswith("/yt/dl"):
            return self.send(*ytdl_start(body))
        if "/yt/" in p and "/api/p/" in self.path:
            return self.send(*ytlib_post(p, body))
        self.send(404, {"error": "not found"})


    def _pipe_resume(self, req, resp, code, hdrs, t0):
        """Copy an upstream body to the client, resuming with Range when the
        connection is cut short. Nothing is buffered, so length does not matter."""
        want = hdrs.get("Content-Length")
        want = int(want) if want and want.isdigit() else None
        self.send_response(code)
        for h in ("Content-Type", "Content-Range", "Accept-Ranges", "Content-Disposition"):
            if hdrs.get(h):
                self.send_header(h, hdrs[h])
        if want is not None:
            self.send_header("Content-Length", str(want))
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        sent, resumes = 0, 0
        while True:
            try:
                chunk = resp.read(1 << 20)
            except Exception as ex:
                log("companion", "stream cut %s at %dB (%s)" % (
                    self.path.split("?")[0][:40], sent, repr(ex)[:50]), t0)
                chunk = b""
            if chunk:
                try:
                    self.wfile.write(chunk)
                except Exception:
                    return          # client went away mid-download
                sent += len(chunk)
                continue
            if want is None or sent >= want or resumes >= 8:
                break
            resumes += 1
            r2 = urllib.request.Request(req.full_url, method="GET")
            for k, v in req.header_items():
                if k.lower() != "range":
                    r2.add_header(k, v)
            r2.add_header("Range", "bytes=%d-" % sent)
            try:
                resp = urllib.request.urlopen(r2, timeout=120)
            except Exception as ex:
                log("companion", "resume FAIL %dB %s" % (sent, repr(ex)[:60]), t0)
                break
        if resumes:
            log("companion", "streamed %dB with %d resume(s) %s" % (
                sent, resumes, self.path.split("?")[0][:40]), t0, every=0)
        return

    def companion_proxy(self, data=None):
        # Point Invidious' companion private_url here. Everything is passed to the real
        # companion; for live streams the player response gets an
        # hlsManifestUrl (companion's TV client never returns one), which is
        # exactly what Invidious' player.ecr needs to play a livestream.
        req = urllib.request.Request(COMPANION + self.path, data=data,
                                     method="POST" if data is not None else "GET")
        for h in ("Authorization", "Content-Type", "Range"):
            if self.headers.get(h):
                req.add_header(h, self.headers[h])
        t0 = time.time()
        for attempt in (0, 1):
            r = None
            try:
                r = urllib.request.urlopen(req, timeout=120)
                code, hdrs = r.status, r.headers
            except urllib.error.HTTPError as e:
                code, hdrs, body = e.code, e.headers, e.read()
                if attempt == 0 and yt_blocked(body=body) and warp_rotate(f"companion {code}", t0):
                    continue
                break
            if "/youtubei/v1/player" in self.path and "json" in (hdrs.get("Content-Type") or ""):
                body = r.read()
                try:
                    blocked = yt_blocked(json.loads(body))
                except Exception:
                    blocked = False
                if attempt == 0 and blocked and warp_rotate("player LOGIN_REQUIRED", t0):
                    continue
                if blocked:
                    log("warp", "still blocked after rotation " + self.path.split("?")[0][:50], every=60)
                r = None
            break
        if r is not None and code < 400:
            # Anything that is not the small player JSON can be a media stream,
            # and reading one of those in a single call dies about eighty
            # seconds in: YouTube throttles the connection and the read comes
            # back short (IncompleteRead), leaving the client with nothing.
            # Stream it through instead, and pick up where it stopped.
            if "/youtubei/" not in self.path and "json" not in (hdrs.get("Content-Type") or ""):
                return self._pipe_resume(req, r, code, hdrs, t0)
            body = r.read()
        r = None
        if code >= 400:
            log("companion", f"{code} {self.path.split('?')[0][:60]} {len(body)}B", t0)
        if "/youtubei/v1/player" in self.path and code == 200:
            try:
                d = json.loads(body)
                vd, sd = d.get("videoDetails") or {}, d.setdefault("streamingData", {})
                if (vd.get("isLive") or vd.get("isLiveContent") and d.get("playabilityStatus", {}).get("liveStreamability")) \
                        and not sd.get("hlsManifestUrl"):
                    sd["hlsManifestUrl"] = f"{LIVE_ORIGIN}/api/p/live/{vd['videoId']}.m3u8"
                    body = json.dumps(d).encode()
                # Videos YouTube ships without storyboards (old uploads, some
                # shorts) make Invidious' /api/v1/videos die mid-serialisation and
                # answer "200 OK" with an empty body, so the player shows nothing.
                # The key merely has to exist - an empty object is enough.
                if "storyboards" not in d and vd.get("videoId"):
                    d["storyboards"] = {}
                    body = json.dumps(d).encode()
                    log("companion", "storyboards stub %s" % vd["videoId"][:11], every=300)
            except Exception as ex:
                log("companion", f"live-patch FAIL {repr(ex)[:120]}")
        self.send_response(code)
        self.send_header("Content-Type", hdrs.get("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def videos_api(self):
        # Companion gives no HLS for live streams, so Invidious returns hlsUrl=null
        # and players show nothing. Pass the call through and point hlsUrl at our
        # own live proxy instead - and answer from the cache above when we can,
        # because otherwise every click waits on a YouTube round trip.
        t0 = time.time()
        key = self.path
        with vid_lk:
            hit = vid_cache.get(key)
        if hit and time.time() - hit[0] < VID_STALE:
            age = time.time() - hit[0]
            code, body = hit[1], hit[2]
            if age > VID_FRESH:
                with vid_lk:
                    stale = key not in vid_busy
                    if stale:
                        vid_busy.add(key)
                if stale:
                    threading.Thread(target=vid_bg, args=(key,), name="vid-" + key[-11:], daemon=True).start()
            log("videos", "cache %ds %s" % (age, key.split("/")[-1][:20]), t0, every=300)
        else:
            code, body = vid_fetch(key)
            d = None
            try:
                if code == 200:
                    d = json.loads(body)
            except Exception as ex:
                log("videos", "parse FAIL %d %s" % (code, repr(ex)[:100]), t0)
            if isinstance(d, dict) and d.get("recommendedVideos"):
                threading.Thread(target=vid_bg, args=(None, d), name="vid-warm", daemon=True).start()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        if "/live/" in self.path:
            self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def do_HEAD(self):
        # Shaka guesses the manifest type from a HEAD's Content-Type
        p = self.path.split("?")[0]
        self.send_response(200)
        self.send_header("Connection", "close")   # nothing to frame a HEAD reply with
        if "/live/" in p:
            self.send_header("Content-Type", "video/mp2t" if "/live/s/" in p else "application/vnd.apple.mpegurl")
        self.end_headers()

    def download(self):
        # Browsers fetch a download in one un-ranged request, which YouTube
        # throttles to a crawl for adaptive formats; pull it in 10 MB ranges instead.
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
        vid, itag = qs.get("id", ""), qs.get("itag", "")
        if not re.fullmatch(r"[\w-]{11}", vid) or not itag.isdigit():
            return self.send(400, {"error": "bad id/itag"})
        t0 = time.time()
        try:
            r = urllib.request.urlopen(f"{COMPANION}/companion/latest_version?id={vid}&itag={itag}&local=true", timeout=30)
            url = r.geturl(); r.close()
            m = re.search(r"clen=(\d+)", url)
            size = int(m.group(1)) if m else None
        except Exception as ex:
            log("download", f"latest_version {vid}/{itag} FAIL 502 {repr(ex)[:120]}", t0, every=0)
            return self.send(502, {"error": f"Couldn't get the video URL: {ex}"})
        log("download", f"latest_version {vid}/{itag} ok clen={size}", t0, every=0)
        name = qs.get("title") or f"{vid}.{itag}"
        if size is None:
            # muxed formats (e.g. itag 18) carry no clen but are ratebypass: pass the single stream through
            try:
                up = urllib.request.urlopen(url, timeout=60)
            except Exception as ex:
                log("download", f"open {vid}/{itag} FAIL 502 {repr(ex)[:120]}", t0, every=0)
                return self.send(502, {"error": f"Download failed: {ex}"})
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            if up.headers.get("Content-Length"):
                self.send_header("Content-Length", up.headers["Content-Length"])
            else:
                self.send_header("Connection", "close")   # unknown length frames by close
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
            self.end_headers()
            try:
                while chunk := up.read(1 << 16):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except Exception as ex:
                self.close_connection = True
                log("download", f"passthrough {vid}/{itag} FAIL {repr(ex)[:120]}", t0, every=0)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
        self.end_headers()
        pos, step = 0, 10_000_000
        try:
            while pos < size:
                end = min(pos + step, size) - 1
                for attempt in range(3):
                    try:
                        chunk = urllib.request.urlopen(f"{url}&range={pos}-{end}", timeout=60).read()
                        break
                    except (BrokenPipeError, ConnectionResetError):
                        raise
                    except Exception:
                        if attempt == 2:
                            raise
                self.wfile.write(chunk)
                pos = end + 1
            log("download", f"sent {vid}/{itag} {size}B", t0, every=0)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as ex:
            self.close_connection = True
            log("download", f"stream {vid}/{itag} FAIL at {pos}/{size} {repr(ex)[:120]}", t0, every=0)
        if pos < size:
            self.close_connection = True   # short body: the socket must not be reused

    def live(self, p):
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        t0 = time.time()
        try:
            if "/live/s/" in p:                        # media segment
                b = p.rsplit("/", 1)[1].removesuffix(".ts")
                u = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode()
                if not re.match(r"^https://[\w.-]+\.googlevideo\.com/videoplayback/", u):
                    return self.send(400, {"error": "bad url"})
                mk = re.search(r"/id/([\w-]{11})[^/]*/itag/(\d+)/.*?/sq/(\d+)/", u)
                pl = live_pull(mk.group(1), mk.group(2), create=False) if mk else None
                got = pl.segment(int(mk.group(3))) if pl else None
                if got:
                    self.send_response(200)
                    self.send_header("Content-Type", got[0])
                    self.send_header("Content-Length", str(len(got[1])))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(got[1])
                    return
                try:
                    r = YT.open(urllib.request.Request(u), timeout=30)
                except urllib.error.HTTPError as he:
                    m2 = re.search(r"/id/([\w-]{11})", u)
                    if he.code == 403 and m2 and m2.group(1) in LIVE_CACHE:
                        LIVE_CACHE[m2.group(1)]["at"] = 0
                        log("live", f"segment 403 {m2.group(1)} -> forcing re-resolve", t0, every=0)
                    else:
                        log("live", f"segment {he.code}", t0)
                    # rebuild the same segment from a fresh playlist instead of failing the player
                    r = live_segment_retry(u, he.code)
                    if r is None:
                        raise
                self.send_response(200)
                self.send_header("Content-Type", r.headers.get("Content-Type") or "video/mp2t")
                if r.headers.get("Content-Length"):
                    self.send_header("Content-Length", r.headers["Content-Length"])
                else:
                    self.send_header("Connection", "close")   # unknown length frames by close
                self.end_headers()
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                return
            mc = re.search(r"/live/([\w-]{11})/cc\.json$", p)
            if mc:                                     # live captions
                q = {k: v[0] for k, v in qs.items()}
                try:
                    lo, hi = int(q.get("from", "0")), int(q.get("to", "0"))
                except ValueError:
                    return self.send(400, {"error": "bad range"})
                c = live_cc(mc.group(1))
                return self.send(200, {"status": c.status, "lag": c.lag, "cues": c.get(lo, min(hi, lo + 40))})
            m = re.search(r"/live/([\w-]{11})(?:\.m3u8)?(?:/(\d+)(?:\.m3u8)?)?$", p)
            if not m:
                return self.send(400, {"error": "video id"})
            vid, itag = m.group(1), m.group(2)
            e = live_entry(vid)
            if itag is None:                           # master playlist
                lines = ["#EXTM3U", "#EXT-X-INDEPENDENT-SEGMENTS"]
                for t, (inf, _) in sorted(e["variants"].items(), key=lambda kv: int(kv[0])):
                    inf = re.sub(r',SUBTITLES="[^"]*"', "", inf)
                    lines += [inf, f"{LIVE_ORIGIN}/api/p/live/{vid}/{t}.m3u8"]
                body = ("\n".join(lines) + "\n").encode()
            else:                                      # media playlist
                pl = live_pull(vid, itag) if itag in e["variants"] else None
                text = pl.playlist() if pl else None
                win = pl.window() if text else None
                if win:
                    body = win.encode()
                elif text:
                    body = live_window(text).encode()
                else:
                  try:
                    text = YT.open(e["variants"][itag][1], timeout=20).read().decode()
                  except Exception as ex:
                    log("live", f"playlist {vid}/{itag} stale ({repr(ex)[:80]}) -> re-resolve", t0, every=0)
                    e = live_entry(vid, force=True)
                    text = YT.open(e["variants"][itag][1], timeout=20).read().decode()
                  body = live_window(text).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as ex:
            log("live", f"502 {p[:60]} {type(ex).__name__}: {str(ex)[:120]}", t0, every=0)
            self.close_connection = True   # headers may already be out
            try:
                self.send(502, {"error": str(ex)[:300]})
            except Exception:
                pass

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    import sys
    if "--doctor" in sys.argv:
        sys.exit(1 if doctor() else 0)
    os.makedirs(STATE, exist_ok=True)
    threading.Thread(target=yt_feed_loop, daemon=True).start()
    threading.Thread(target=ytdl_gc_loop, daemon=True).start()
    log("start", "TubeHaus server on %s:%d, Invidious %s" % (BIND[0], BIND[1], INV), every=0)
    ThreadingHTTPServer(BIND, H).serve_forever()
