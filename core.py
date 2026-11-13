"""Logic dùng chung cho GUI (app.py) và CLI (music_sync.py)."""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import time
import unicodedata
from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import yaml
from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
DB_PATH = ROOT / "state.sqlite"
SESSION_PATH = ROOT / "music_sync"
AUDIO_EXTS = {".mp3", ".flac", ".wav", ".aiff", ".aif", ".m4a", ".aac", ".ogg", ".opus", ".alac", ".wma"}
LOSSLESS_EXTS = {".flac", ".wav", ".aiff", ".aif", ".alac"}
MIME = {".mp3": "audio/mpeg", ".flac": "audio/flac", ".wav": "audio/wav", ".m4a": "audio/mp4", ".aac": "audio/aac",
        ".ogg": "audio/ogg", ".opus": "audio/ogg", ".aiff": "audio/aiff", ".aif": "audio/aiff", ".alac": "audio/mp4",
        ".wma": "audio/x-ms-wma"}
BROWSER_UNPLAYABLE = {".aiff", ".aif", ".wma", ".alac"}
PREVIEW_CHUNK = 512 * 1024      # = kích thước request tối đa của Telegram
GENERAL_TOPIC_ID = 1

DEFAULT_CONFIG = {
    "api_id": None,
    "api_hash": "",
    "music_dirs": ["~/Music"],
    "download_dir": "~/Music/_telegram_new",
    "have_threshold": 90,
    "maybe_threshold": 70,
    "read_tags": True,
    "parallel_downloads": 3,
    "parallel_scans": 3,
    "groups": [],
    "skip_topics": ["General"],
    "artists": {},
}

Progress = Callable[[str, float | None], None]  # (message, fraction 0..1 | None)


def _noop(msg: str, frac: float | None = None) -> None:
    pass


# ============================================================================ config
def load_raw_config(path: Path = CONFIG_PATH) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    raw = raw or {}
    cfg = {**DEFAULT_CONFIG, **raw}
    if "match_threshold" in raw and "have_threshold" not in raw:  # tương thích config cũ
        cfg["have_threshold"] = raw["match_threshold"]
    return cfg


def save_raw_config(cfg: dict, path: Path = CONFIG_PATH) -> None:
    keep = {k: cfg.get(k, v) for k, v in DEFAULT_CONFIG.items()}
    path.write_text(yaml.safe_dump(keep, allow_unicode=True, sort_keys=False), encoding="utf-8")


def credentials_ok(cfg: dict) -> bool:
    try:
        api_id = int(cfg.get("api_id") or 0)
    except (TypeError, ValueError):
        return False
    h = str(cfg.get("api_hash") or "")
    return api_id > 0 and api_id != 1234567 and len(h) == 32 and h != "0123456789abcdef0123456789abcdef"


def expand(p: str) -> Path:
    return Path(os.path.expanduser(str(p)))


def music_dirs(cfg: dict) -> list[Path]:
    return [expand(p) for p in cfg.get("music_dirs") or []]


def download_dir(cfg: dict) -> Path:
    return expand(cfg.get("download_dir") or DEFAULT_CONFIG["download_dir"])


def alias_map(cfg: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for canonical, aliases in (cfg.get("artists") or {}).items():
        out[norm_name(canonical)] = canonical
        for a in aliases or []:
            out[norm_name(str(a))] = canonical
    return out


def resolve_artist(amap: dict[str, str], source_title: str) -> str:
    return amap.get(norm_name(source_title), source_title.strip())


def is_skipped(cfg: dict, title: str) -> bool:
    return norm_name(title) in {norm_name(s) for s in cfg.get("skip_topics") or []}


_TOPIC_NOISE = {"official", "music", "musics", "tracks", "track", "channel", "dj", "producer", "fan", "fans",
                "releases", "release", "discography", "collection", "archive", "hq", "flac", "mp3", "wav",
                "vn", "ua", "uk", "us", "de", "nl", "page", "group", "songs", "remixes", "edits", "new", "the",
                "all", "full", "best", "of", "tm", "pack", "packs", "set", "sets"}


def topic_core(title: str) -> str:
    toks = [w for w in norm_name(title).split() if w not in _TOPIC_NOISE and not w.isdigit()]
    return " ".join(toks) or norm_name(title)


def display_name(title: str) -> str:
    t = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", title)          # bỏ (UA) [Official]
    t = re.sub(r"[^\w\s&.'\-]", " ", t)                       # bỏ emoji, ký hiệu
    return _SPACES.sub(" ", t).strip(" -._") or title.strip()


def suggest_artists(titles: Iterable[str]) -> dict[str, str]:
    """Gom các topic khác tên nhưng cùng nghệ sĩ: {tên topic: tên nghệ sĩ gợi ý} (chỉ cụm ≥ 2 topic)."""
    reps: list[str] = []
    clusters: list[list[str]] = []
    for t in dict.fromkeys(titles):
        c = topic_core(t)
        cc = c.replace(" ", "")
        for i, r in enumerate(reps):
            rr = r.replace(" ", "")
            if c == r or fuzz.ratio(cc, rr) >= 90 or (min(len(cc), len(rr)) >= 5 and (cc.startswith(rr) or rr.startswith(cc))):
                clusters[i].append(t)
                break
        else:
            reps.append(c)
            clusters.append([t])
    out: dict[str, str] = {}
    for cl in clusters:
        if len(cl) < 2:
            continue
        name = min((display_name(t) for t in cl), key=lambda s: (len(s), s))
        for t in cl:
            out[t] = name
    return out


# ============================================================================ normalize
def strip_accents(s: str) -> str:
    s = s.replace("đ", "d").replace("Đ", "D")
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


_NONWORD = re.compile(r"[^\w]+")
_SPACES = re.compile(r"\s+")


def norm_name(s: str) -> str:
    return _SPACES.sub(" ", _NONWORD.sub(" ", strip_accents(s).lower())).strip()


_AUDIO_EXT_RE = re.compile(r"\.(mp3|flac|wav|aiff?|m4a|aac|ogg|opus|alac|wma)$", re.I)
_JUNK = re.compile("|".join([
    r"\[[^\]]*\]",                                   # [320kbps] [Label] [www.site.com]
    r"\{[^}]*\}",
    r"https?://\S+|www\.\S+|t\.me/\S+",
    r"@\w+",
    r"#\w+",
    r"\b\d{2,4}\s*kbps\b",
    r"\b(?:mp3|flac|wav|aiff|m4a|hq|hd|free download|free dl|out now|premiere|master(?:ed)?|final)\b",
    r"\boriginal(?: mix| version)?\b",             # "Song" == "Song (Original Mix)"
]))
_TRACKNO = re.compile(r"^\s*(?:[a-d]?\d{1,3})\s*[.\-)_]\s+")
# Viết tắt / cách viết khác nhau -> 1 từ chuẩn. Thứ tự quan trọng (cụm dài trước).
_SYNONYMS = [(re.compile(p), rep) for p, rep in [
    (r"\bre ?mix(?:ed)?\b|\brmx\b|\brmix\b", " remix "),
    (r"\bext(?:ended)?(?: mix| version| edit)?\b", " extended "),
    (r"\bradio(?: edit| mix| version| cut)?\b", " radio "),
    (r"\bclub(?: mix| edit| version)?\b", " club "),
    (r"\bdub(?: mix)?\b", " dub "),
    (r"\bvip(?: mix)?\b", " vip "),
    (r"\bboot(?:leg)?\b", " bootleg "),
    (r"\bre ?work(?:ed)?\b", " rework "),
    (r"\bre ?edit\b", " edit "),
    (r"\binstr(?:umental)?\b|\binst\b", " instrumental "),
    (r"\ba ?c+ap+el+a\b", " acapella "),
    (r"\bmash ?up\b", " mashup "),
    (r"\bfeat(?:uring)?\b|\bft\b|\bvs\b|\bversus\b|\bwith\b|\band\b|\bx\b", " "),
    (r"\bmix\b", " "),                              # "(Peggy Gou Mix)" ~ "(Peggy Gou)"
]]
VERSION_WORDS = {"remix", "extended", "radio", "club", "dub", "vip", "bootleg", "rework", "edit",
                 "instrumental", "acapella", "mashup", "live", "flip"}
_PARENS = re.compile(r"\(([^)]*)\)")
_FEAT_START = re.compile(r"^\s*(?:feat|ft|featuring|with)\b", re.I)
_FEAT_TAIL = re.compile(r"\b(?:feat|ft|featuring)\b.*$", re.I)


def norm_track(s: str) -> str:
    """Chuẩn hoá tên bài để so khớp (giữ lại thông tin phiên bản: remix/extended/radio...)."""
    s = strip_accents(s).lower()
    s = _AUDIO_EXT_RE.sub("", s)
    s = s.replace("_", " ").replace("&", " ").replace("+", " ")
    s = _JUNK.sub(" ", s)
    s = _TRACKNO.sub("", s)
    s = _NONWORD.sub(" ", s)                         # bỏ () - . , ' ...
    for p, rep in _SYNONYMS:
        s = p.sub(rep, s)
    return _SPACES.sub(" ", s).strip()


def split_artist_title(stem: str) -> tuple[str | None, str]:
    parts = re.split(r"\s+[-–—]\s+", stem, maxsplit=1)
    return (parts[0], parts[1]) if len(parts) == 2 else (None, stem)


def _without_versions(s: str) -> str:
    return " ".join(w for w in s.split() if w not in VERSION_WORDS)


@dataclass(slots=True)
class TrackKey:
    strings: list[str]            # chuỗi chuẩn hoá để so (tên đầy đủ, chỉ tên bài, theo tag...)
    versions: frozenset[str]      # remix / extended / radio ...
    duration: int = 0             # giây, 0 = không rõ
    core: str = ""                # tên bài chính: bỏ nghệ sĩ, bỏ (…), bỏ từ phiên bản
    mix: str = ""                 # nội dung trong ngoặc (tên remixer…), bỏ từ phiên bản
    artists: tuple[str, ...] = () # các khả năng tên nghệ sĩ (tên file, tag, kênh)
    canon: str = ""               # "core mix": tên bài không kèm nghệ sĩ/feat/từ phiên bản

    @property
    def main(self) -> str:
        return self.strings[0] if self.strings else ""


def make_key(file_name: str | None, performer: str | None, title: str | None, duration: int = 0,
             channel_artist: str | None = None) -> TrackKey:
    strings: list[str] = []
    artists: list[str] = []
    title_raw = title
    if file_name:
        stem = _AUDIO_EXT_RE.sub("", file_name).replace("_", " ")
        strings.append(norm_track(stem))
        art, t = split_artist_title(stem)
        strings.append(norm_track(t))
        title_raw = title_raw or t
        if art:
            artists.append(norm_track(_FEAT_TAIL.sub("", art)))
    if title:
        strings.append(norm_track(f"{performer or ''} {title}"))
        strings.append(norm_track(title))
    if performer:
        artists.append(norm_track(performer))
    if channel_artist:
        artists.append(norm_track(channel_artist))
    strings = [s for s in dict.fromkeys(strings) if s]

    title_raw = _JUNK.sub(" ", strip_accents(title_raw or "").lower())
    mix_parts = [m for m in _PARENS.findall(title_raw) if not _FEAT_START.match(m)]
    main_raw = _FEAT_TAIL.sub("", _PARENS.sub(" ", title_raw))
    core = _without_versions(norm_track(main_raw))
    mix = _without_versions(norm_track(" ".join(mix_parts)))
    versions = frozenset(w for s in strings[:1] for w in s.split() if w in VERSION_WORDS)
    if title and not versions:
        versions = frozenset(w for w in norm_track(title).split() if w in VERSION_WORDS)
    core = core or _without_versions(strings[1] if len(strings) > 1 else strings[0] if strings else "")
    return TrackKey(strings, versions, int(duration or 0), core, mix,
                    tuple(a for a in dict.fromkeys(artists) if a), f"{core} {mix}".strip())


# ============================================================================ db
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE IF NOT EXISTS tracks(
            chat_id INTEGER, msg_id INTEGER, chat_title TEXT, topic_id INTEGER, source TEXT,
            file_name TEXT, performer TEXT, title TEXT, size INTEGER, duration INTEGER, date TEXT,
            status TEXT DEFAULT 'new', local_path TEXT,
            PRIMARY KEY(chat_id, msg_id));
        CREATE INDEX IF NOT EXISTS tracks_status ON tracks(status);
        CREATE TABLE IF NOT EXISTS progress(
            chat_id INTEGER, topic_id INTEGER, last_msg_id INTEGER,
            PRIMARY KEY(chat_id, topic_id));
        CREATE TABLE IF NOT EXISTS local_files(
            path TEXT PRIMARY KEY, mtime REAL, size INTEGER,
            artist TEXT, title TEXT, duration INTEGER);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        """
    )
    return con


def meta_get(con: sqlite3.Connection, k: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def meta_set(con: sqlite3.Connection, k: str, v: str) -> None:
    con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, v))
    con.commit()


def set_status(con: sqlite3.Connection, chat_id: int, msg_id: int, status: str, local_path: str | None = None) -> None:
    con.execute("UPDATE tracks SET status=?, local_path=COALESCE(?, local_path) WHERE chat_id=? AND msg_id=?",
                (status, local_path, chat_id, msg_id))
    con.commit()


def counts(con: sqlite3.Connection) -> dict[str, int]:
    out = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) n FROM tracks GROUP BY status")}
    out["local"] = con.execute("SELECT COUNT(*) FROM local_files").fetchone()[0]
    return out


# ============================================================================ local library
@dataclass(slots=True)
class LocalTrack:
    path: str
    key: TrackKey


_LOCAL_KEY_CACHE: dict[str, tuple[float, int, LocalTrack]] = {}


def _walk(root: Path) -> Iterable[os.DirEntry]:
    stack = [str(root)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if not e.name.startswith("."):
                                stack.append(e.path)
                        elif os.path.splitext(e.name)[1].lower() in AUDIO_EXTS:
                            yield e
                    except OSError:
                        continue
        except OSError:
            continue


def _read_tags(path: str) -> tuple[str | None, str | None, int]:
    try:
        from mutagen import File as MFile

        m = MFile(path, easy=True)
        if m is None:
            return None, None, 0
        artist = title = None
        if m.tags:
            artist = (m.tags.get("artist") or [None])[0]
            title = (m.tags.get("title") or [None])[0]
        return artist, title, int(getattr(m.info, "length", 0) or 0)
    except Exception:  # noqa: BLE001
        return None, None, 0


def refresh_local(cfg: dict, con: sqlite3.Connection, progress: Progress = _noop) -> list[LocalTrack]:
    """Quét thư viện offline. Chỉ đọc tag file mới/đổi (cache sqlite), đọc song song nhiều luồng."""
    t0 = time.time()
    cached = {r["path"]: r for r in con.execute("SELECT * FROM local_files")}
    entries: dict[str, tuple[float, int]] = {}
    for d in [*music_dirs(cfg), download_dir(cfg)]:
        if d.exists():
            progress(f"Đang liệt kê {d} …", None)
            for e in _walk(d):
                try:
                    st = e.stat()
                except OSError:
                    continue
                entries[e.path] = (st.st_mtime, st.st_size)

    changed = [p for p, (mt, sz) in entries.items()
               if not (p in cached and cached[p]["mtime"] == mt and cached[p]["size"] == sz)]
    if changed:
        read_tags = bool(cfg.get("read_tags", True))
        progress(f"Đọc thông tin {len(changed)} file mới/thay đổi …", 0)
        rows = []
        if read_tags:
            with ThreadPoolExecutor(max_workers=min(16, (os.cpu_count() or 4) * 2)) as ex:
                for i, (p, tags) in enumerate(zip(changed, ex.map(_read_tags, changed, chunksize=32))):
                    rows.append((p, *entries[p], *tags))
                    if i % 500 == 0:
                        progress(f"Đã đọc {i}/{len(changed)} file", i / len(changed))
        else:
            rows = [(p, *entries[p], None, None, 0) for p in changed]
        con.executemany("INSERT OR REPLACE INTO local_files VALUES(?,?,?,?,?,?)", rows)
        for r in rows:
            cached[r[0]] = dict(zip(("path", "mtime", "size", "artist", "title", "duration"), r))
    gone = [p for p in cached if p not in entries]
    if gone:
        con.executemany("DELETE FROM local_files WHERE path=?", [(g,) for g in gone])
    con.commit()

    out: list[LocalTrack] = []
    for p, (mt, sz) in entries.items():
        hit = _LOCAL_KEY_CACHE.get(p)
        if hit and hit[0] == mt and hit[1] == sz:
            out.append(hit[2])
            continue
        r = cached[p]
        lt = LocalTrack(p, make_key(os.path.basename(p), r["artist"], r["title"], r["duration"] or 0))
        _LOCAL_KEY_CACHE[p] = (mt, sz, lt)
        out.append(lt)
    progress(f"Thư viện offline: {len(out)} file ({time.time() - t0:.1f}s)", 1.0)
    return out


# ============================================================================ matching
@dataclass(slots=True)
class Match:
    status: str                  # "have" | "maybe" | "new"
    score: float = 0.0
    local_path: str | None = None
    reason: str = ""


def _tokens(s: str) -> set[str]:
    out = set()
    for w in s.split():
        if len(w) < 2 or w in VERSION_WORDS:
            continue
        out.add(w)
        if len(w) >= 5:
            out.add("§" + w[:4])   # tiền tố: bắt lỗi chính tả ở cuối từ
    return out


class Matcher:
    """So khớp nhanh: chỉ mục từ khoá -> vài chục ứng viên -> chấm điểm chi tiết."""

    CORE_MIN = 72       # tên bài chính phải giống ít nhất mức này
    MAX_CANDIDATES = 40

    def __init__(self, local: list[LocalTrack], have: int = 90, maybe: int = 70):
        self.have, self.maybe = have, maybe
        self.local = local
        self.exact: dict[str, list[int]] = defaultdict(list)
        self.index: dict[str, list[int]] = defaultdict(list)
        for i, lt in enumerate(local):
            toks: set[str] = set()
            for s in (*lt.key.strings, "§" + lt.key.canon):
                self.exact[s].append(i)
                toks |= _tokens(s)
            for t in toks:
                self.index[t].append(i)
        self.common = max(200, len(local) // 20)  # từ xuất hiện quá nhiều -> ít giá trị phân biệt

    def _candidates(self, tk: TrackKey) -> tuple[list[int], set[int]]:
        exact_hits: set[int] = set()
        for s in (*tk.strings, "§" + tk.canon):
            exact_hits.update(self.exact.get(s, ()))
        weights: dict[int, float] = defaultdict(float)
        toks = set().union(*(_tokens(s) for s in tk.strings)) if tk.strings else set()
        core_toks = _tokens(tk.core)
        for t in toks:
            post = self.index.get(t)
            if not post or (len(post) > self.common and t not in core_toks):
                continue
            w = (2.0 if t in core_toks else 1.0) / len(post)
            for i in post:
                weights[i] += w
        best = sorted(weights, key=weights.__getitem__, reverse=True)[: self.MAX_CANDIDATES]
        return list(dict.fromkeys([*exact_hits, *best])), exact_hits

    def _judge(self, tk: TrackKey, lt: LocalTrack, exact: bool) -> Match | None:
        lk = lt.key
        same_main = bool(tk.strings and lk.strings and tk.strings[0] == lk.strings[0])
        core_sim = fuzz.token_sort_ratio(tk.core, lk.core) if tk.core and lk.core else 0.0
        if not exact and core_sim < self.CORE_MIN:
            return None
        if exact:
            score = 100.0
        else:
            score = max(fuzz.token_sort_ratio(a, b) for a in tk.strings for b in lk.strings)
            if tk.canon and lk.canon:  # so riêng tên bài (phiên bản/nghệ sĩ xét ở dưới)
                score = max(score, fuzz.token_sort_ratio(tk.canon, lk.canon))
        reasons: list[str] = []
        if score >= self.have:
            status = "have"
        elif score >= self.maybe:
            status = "maybe"
        elif core_sim >= 90 and fuzz.token_set_ratio(tk.main, lk.main) >= self.have:
            status = "maybe"
            reasons.append("tên gần giống (thừa/thiếu vài từ)")
        elif core_sim >= 90 and (tk.mix or lk.mix):
            status = "maybe"            # cùng bài gốc, khác bản remix -> lý do thêm ở dưới
        else:
            return None
        mix_conflict = bool(tk.mix and lk.mix and fuzz.token_sort_ratio(tk.mix, lk.mix) < 70)

        # Nghệ sĩ
        artist_ok = None
        if tk.artists and lk.artists:
            a_sim = max(fuzz.token_set_ratio(a, b) for a in tk.artists for b in lk.artists)
            artist_ok = a_sim >= 60
            if not artist_ok:
                if (core_sim < 90 or mix_conflict) and not same_main:
                    return None
                status = "maybe"
                reasons.append("khác nghệ sĩ")
        # Phiên bản (extended/radio/remix…)
        if tk.versions != lk.versions:
            status = "maybe"
            reasons.append("khác phiên bản: " + ", ".join(sorted(tk.versions ^ lk.versions)))
        # Remixer / ghi chú trong ngoặc
        if mix_conflict:
            status = "maybe"
            reasons.append(f"khác bản mix: ({tk.mix}) ≠ ({lk.mix})")
        # Tên bài quá ngắn mà không xác nhận được nghệ sĩ
        if status == "have" and artist_ok is None and len(tk.core.split()) <= 2 and not same_main:
            status = "maybe"
            reasons.append("tên bài ngắn, chưa rõ nghệ sĩ")
        # Thời lượng
        d1, d2 = tk.duration, lk.duration
        if d1 and d2:
            diff = abs(d1 - d2)
            soft = all(r.startswith(("tên bài ngắn", "tên gần giống")) for r in reasons)
            if status == "maybe" and diff <= 2 and soft and reasons:
                status = "have"
                reasons.append(f"cùng thời lượng {d1 // 60}:{d1 % 60:02d}")
            elif status == "have" and diff > 15:
                status = "maybe"
                reasons.append(f"lệch thời lượng {diff}s")
        return Match(status, round(score, 1), lt.path, "; ".join(reasons))

    def match(self, tk: TrackKey) -> Match:
        if not self.local or not tk.strings:
            return Match("new")
        cands, exact_hits = self._candidates(tk)
        best: Match | None = None
        rank = {"have": 2, "maybe": 1}
        for i in cands:
            m = self._judge(tk, self.local[i], i in exact_hits)
            if m and (best is None or (rank[m.status], m.score) > (rank[best.status], best.score)):
                best = m
                if m.status == "have" and m.score >= 100:
                    break
        return best or Match("new")


@dataclass
class Result:
    row: sqlite3.Row
    artist: str
    key: TrackKey
    match: Match = field(default_factory=lambda: Match("new"))
    copies: int = 1               # số nguồn (group/topic) có cùng bài
    _dict: dict | None = None

    @property
    def id(self) -> str:
        return f"{self.row['chat_id']}:{self.row['msg_id']}"

    @property
    def quality(self) -> tuple:
        ext = Path(self.row["file_name"]).suffix.lower()
        return (ext in LOSSLESS_EXTS, self.row["size"] or 0)

    def as_dict(self) -> dict:
        if self._dict is None:
            r = self.row
            dur = r["duration"] or 0
            self._dict = {
                "id": self.id,
                "chat_id": r["chat_id"], "msg_id": r["msg_id"],
                "status": self.match.status,
                "artist": self.artist,
                "file_name": r["file_name"],
                "ext": Path(r["file_name"]).suffix.lower().lstrip("."),
                "lossless": Path(r["file_name"]).suffix.lower() in LOSSLESS_EXTS,
                "size_mb": round((r["size"] or 0) / 1e6, 1),
                "duration": f"{dur // 60}:{dur % 60:02d}" if dur else "",
                "group": r["chat_title"], "topic": r["source"],
                "copies": self.copies,
                "local": Path(self.match.local_path).name if self.match.local_path else "",
                "local_path": self.match.local_path or "",
                "score": self.match.score,
                "reason": self.match.reason,
                "date": (r["date"] or "")[:10],
            }
        return self._dict

    def set_status(self, status: str) -> None:
        self.match.status = status
        if self._dict is not None:
            self._dict["status"] = status


def compute(cfg: dict, con: sqlite3.Connection, progress: Progress = _noop) -> list[Result]:
    t0 = time.time()
    local = refresh_local(cfg, con, progress)
    amap = alias_map(cfg)
    skip = {norm_name(s) for s in cfg.get("skip_topics") or []}
    rows = con.execute("SELECT * FROM tracks WHERE status='new'").fetchall()
    progress(f"Gộp trùng {len(rows)} file Telegram …", None)

    # Gộp bài trùng giữa các group/kênh, giữ bản chất lượng tốt nhất.
    holder_of: dict[str, list] = {}
    holders: list[list] = []
    for r in rows:
        src = r["source"] or ""
        if norm_name(src) in skip:
            continue
        artist = resolve_artist(amap, src)
        res = Result(r, artist, make_key(r["file_name"], r["performer"], r["title"], r["duration"] or 0, artist))
        if not res.key.strings:
            continue
        h = holder_of.get(res.key.main)
        if h is None:
            h = [res, 0]
            holders.append(h)
            holder_of[res.key.main] = h
        elif res.quality > h[0].quality:
            h[0] = res
        h[1] += 1

    matcher = Matcher(local, int(cfg.get("have_threshold", 90)), int(cfg.get("maybe_threshold", 70)))
    results = []
    n = max(len(holders), 1)
    for i, (res, copies) in enumerate(holders):
        res.copies = copies
        res.match = matcher.match(res.key)
        results.append(res)
        if i % 250 == 0:
            progress(f"Đối chiếu {i}/{len(holders)}", i / n)
    order = {"new": 0, "maybe": 1, "have": 2}
    results.sort(key=lambda x: (order[x.match.status], x.artist.lower(), x.row["file_name"].lower()))
    c = {s: sum(1 for x in results if x.match.status == s) for s in order}
    meta_set(con, "last_compare", time.strftime("%Y-%m-%d %H:%M"))
    progress(f"Xong trong {time.time() - t0:.1f}s: {c['new']} mới, {c['maybe']} có thể đã có, {c['have']} đã có", 1.0)
    return results


def ignore_results(con: sqlite3.Connection, chosen: list[Result]) -> None:
    """Bỏ qua các bài đã chọn, kể cả bản trùng ở group khác (để lần sau không hiện lại)."""
    keys = {r.key.main for r in chosen}
    rows = con.execute("SELECT chat_id, msg_id, file_name, performer, title FROM tracks WHERE status='new'").fetchall()
    upd = [(r["chat_id"], r["msg_id"]) for r in rows
           if make_key(r["file_name"], r["performer"], r["title"]).main in keys]
    con.executemany("UPDATE tracks SET status='ignored' WHERE chat_id=? AND msg_id=?", upd)
    con.commit()


# ============================================================================ telegram
def audio_info(msg) -> dict | None:
    f = msg.file
    if f is None or msg.voice:
        return None
    name = f.name
    is_audio = bool(msg.audio) or (f.mime_type or "").startswith("audio/") or (
        bool(name) and Path(name).suffix.lower() in AUDIO_EXTS)
    if not is_audio:
        return None
    performer, title = getattr(f, "performer", None), getattr(f, "title", None)
    if not name:
        base = " - ".join(x for x in (performer, title) if x) or f"track_{msg.id}"
        name = base + ((f.ext or "").lower() or ".mp3")
    return dict(file_name=name, performer=performer, title=title, size=f.size or 0,
                duration=int(getattr(f, "duration", 0) or 0))


def safe(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", s).strip(" .") or "_"


class Telegram:
    """Bọc Telethon client; tạo lười trong event loop đang chạy."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.client = None
        self._phone: str | None = None
        self._phone_hash: str | None = None
        self.me = None
        self._entities: dict = {}
        self._msgs: dict[tuple[int, int], object] = {}
        self._chunks: OrderedDict[tuple[int, int, int], bytes] = OrderedDict()
        self._chunk_locks: dict[tuple[int, int, int], asyncio.Lock] = {}

    async def connect(self):
        from telethon import TelegramClient

        if not credentials_ok(self.cfg):
            raise RuntimeError("Chưa nhập api_id/api_hash hợp lệ (lấy ở https://my.telegram.org).")
        if self.client is None:
            self.client = TelegramClient(str(SESSION_PATH), int(self.cfg["api_id"]), str(self.cfg["api_hash"]))
            self.client.flood_sleep_threshold = 300
        if not self.client.is_connected():
            await self.client.connect()
        if self.me is None and await self.client.is_user_authorized():
            self.me = await self.client.get_me()
        return self.client

    async def disconnect(self):
        if self.client is not None:
            await self.client.disconnect()
            self.client = None
            self.me = None

    async def authorized(self) -> bool:
        await self.connect()
        return self.me is not None

    async def send_code(self, phone: str) -> None:
        c = await self.connect()
        sent = await c.send_code_request(phone)
        self._phone, self._phone_hash = phone, sent.phone_code_hash

    async def sign_in(self, code: str | None = None, password: str | None = None) -> str:
        """Trả về 'ok' hoặc 'need_password'."""
        from telethon.errors import SessionPasswordNeededError

        c = await self.connect()
        try:
            if password:
                await c.sign_in(password=password)
            else:
                await c.sign_in(phone=self._phone, code=code, phone_code_hash=self._phone_hash)
        except SessionPasswordNeededError:
            return "need_password"
        self.me = await c.get_me()
        return "ok"

    async def log_out(self) -> None:
        c = await self.connect()
        await c.log_out()
        self.client = None
        self.me = None

    # ------------------------------------------------------------ nghe thử (stream)
    async def get_message(self, chat_id: int, msg_id: int):
        key = (chat_id, msg_id)
        if key not in self._msgs:
            c = await self.connect()
            m = await c.get_messages(await self._entity(chat_id), ids=msg_id)
            if m is None or m.file is None:
                raise FileNotFoundError("Tin nhắn đã bị xoá")
            self._msgs[key] = m
        return self._msgs[key]

    async def read_chunk(self, chat_id: int, msg_id: int, idx: int) -> bytes:
        """Đoạn thứ idx (512 KB) của file; cache LRU ~200 MB để tua lại không phải tải lại."""
        key = (chat_id, msg_id, idx)
        hit = self._chunks.get(key)
        if hit is not None:
            self._chunks.move_to_end(key)
            return hit
        lock = self._chunk_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._chunks:
                return self._chunks[key]
            msg = await self.get_message(chat_id, msg_id)
            parts = [p async for p in self.client.iter_download(
                msg.media, offset=idx * PREVIEW_CHUNK, request_size=PREVIEW_CHUNK, limit=1, file_size=msg.file.size)]
            data = b"".join(parts)
            self._chunks[key] = data
            while len(self._chunks) > 400:
                self._chunks.popitem(last=False)
        self._chunk_locks.pop(key, None)
        return data

    async def dialogs(self) -> list[dict]:
        c = await self.connect()
        out = []
        async for d in c.iter_dialogs():
            if d.is_group or d.is_channel:
                forum = bool(getattr(d.entity, "forum", False))
                kind = "forum" if forum else ("channel" if d.is_channel and not d.is_group else "group")
                self._entities[d.id] = d.entity
                out.append({"id": d.id, "name": d.name, "kind": kind,
                            "members": getattr(d.entity, "participants_count", None) or 0})
        return out

    async def _entity(self, g):
        if g in self._entities:
            return self._entities[g]
        c = await self.connect()
        try:
            ent = await c.get_entity(g)
        except Exception:  # noqa: BLE001
            ent = None
            async for d in c.iter_dialogs():
                self._entities[d.id] = d.entity
                if d.id == g or d.name == g:
                    ent = d.entity
            if ent is None:
                raise
        self._entities[g] = ent
        return ent

    async def sources(self, entity) -> list[tuple[int, str]]:
        from telethon.tl.functions.messages import GetForumTopicsRequest

        c = await self.connect()
        if not getattr(entity, "forum", False):
            return [(0, entity.title)]
        topics, offset_date, offset_id, offset_topic = [], None, 0, 0
        while True:
            res = await c(GetForumTopicsRequest(entity, offset_date, offset_id, offset_topic, limit=100))
            batch = [t for t in res.topics if hasattr(t, "title")]
            if not batch:
                break
            topics.extend((t.id, t.title) for t in batch)
            if len(topics) >= res.count:
                break
            last = batch[-1]
            offset_topic, offset_id = last.id, last.top_message
            offset_date = next((m.date for m in res.messages if m.id == last.top_message), None)
        return topics

    async def group_topics(self, progress: Progress = _noop) -> list[dict]:
        async def one(g):
            try:
                ent = await self._entity(g)
            except Exception as e:  # noqa: BLE001
                progress(f"Không mở được group {g!r}: {e}", None)
                return []
            return [{"group_id": g, "group": ent.title, "topic_id": tid, "topic": title}
                    for tid, title in await self.sources(ent)]

        parts = await asyncio.gather(*(one(g) for g in self.cfg.get("groups") or []))
        return [x for p in parts for x in p]

    async def scan(self, con: sqlite3.Connection, progress: Progress = _noop) -> int:
        """Quét song song nhiều topic; chỉ đọc tin nhắn mới hơn lần trước."""
        from telethon.utils import get_peer_id

        jobs = []
        for g in self.cfg.get("groups") or []:
            try:
                ent = await self._entity(g)
            except Exception as e:  # noqa: BLE001
                progress(f"Không mở được group {g!r}: {e}", None)
                continue
            for tid, title in await self.sources(ent):
                if is_skipped(self.cfg, title) or (tid == GENERAL_TOPIC_ID and getattr(ent, "forum", False)):
                    continue
                jobs.append((ent, get_peer_id(ent), tid, title))
        progress(f"Quét {len(jobs)} kênh/topic …", 0)
        sem = asyncio.Semaphore(max(1, int(self.cfg.get("parallel_scans", 3))))
        done = 0
        total = 0

        async def one(ent, chat_id, tid, title):
            nonlocal done, total
            async with sem:
                row = con.execute("SELECT last_msg_id FROM progress WHERE chat_id=? AND topic_id=?",
                                  (chat_id, tid)).fetchone()
                min_id = row["last_msg_id"] if row else 0
                kwargs = {"reply_to": tid} if tid else {}
                batch, max_id = [], min_id
                # wait_time=0: mặc định Telethon ngủ 1s/100 tin khi quét toàn bộ lịch sử
                async for msg in self.client.iter_messages(ent, min_id=min_id, wait_time=0, **kwargs):
                    max_id = max(max_id, msg.id)
                    info = audio_info(msg)
                    if info:
                        batch.append((chat_id, msg.id, ent.title, tid, title, info["file_name"], info["performer"],
                                      info["title"], info["size"], info["duration"], msg.date.isoformat()))
                before = con.total_changes
                con.executemany(
                    "INSERT OR IGNORE INTO tracks(chat_id,msg_id,chat_title,topic_id,source,file_name,"
                    "performer,title,size,duration,date) VALUES(?,?,?,?,?,?,?,?,?,?,?)", batch)
                added = con.total_changes - before
                con.execute("INSERT OR REPLACE INTO progress VALUES(?,?,?)", (chat_id, tid, max_id))
                con.commit()
                done += 1
                total += added
                progress(f"{ent.title} / {title}: +{added} file" if added else f"{ent.title} / {title}: không có gì mới",
                         done / len(jobs))

        await asyncio.gather(*(one(*j) for j in jobs))
        meta_set(con, "last_scan", time.strftime("%Y-%m-%d %H:%M"))
        progress(f"Quét xong: {total} file nhạc mới", 1.0)
        return total

    async def download(self, con: sqlite3.Connection, items: list[dict], progress: Progress = _noop,
                       cancel: asyncio.Event | None = None,
                       on_item: Callable[[dict, str], None] | None = None) -> int:
        """Tải song song N bài một lúc. on_item(item, 'downloading'|'done'|'error'|'gone')."""
        c = await self.connect()
        n_par = max(1, int(self.cfg.get("parallel_downloads", 3)))
        total_bytes = sum(int(it.get("size_mb", 0) * 1e6) for it in items) or 1
        got: dict[str, int] = {}
        done = 0
        active: dict[str, str] = {}

        # Lấy message theo lô (100 id / request) thay vì từng cái
        msgs: dict[tuple[int, int], object] = {}
        by_chat: dict[int, list[int]] = defaultdict(list)
        for it in items:
            by_chat[it["chat_id"]].append(it["msg_id"])
        for chat_id, ids in by_chat.items():
            try:
                ent = await self._entity(chat_id)
            except Exception:  # noqa: BLE001
                await c.get_dialogs()
                ent = await c.get_input_entity(chat_id)
            for k in range(0, len(ids), 100):
                for m in await c.get_messages(ent, ids=ids[k:k + 100]):
                    if m is not None:
                        msgs[(chat_id, m.id)] = m

        def report():
            frac = sum(got.values()) / total_bytes
            names = ", ".join(list(active.values())[:2])
            progress(f"Đang tải {len(active)} bài · xong {done}/{len(items)} · {names}", min(frac, 1.0))

        sem = asyncio.Semaphore(n_par)

        async def one(it):
            nonlocal done
            if cancel is not None and cancel.is_set():
                return
            async with sem:
                if cancel is not None and cancel.is_set():
                    return
                dest_dir = download_dir(self.cfg) / safe(it["artist"])
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / safe(it["file_name"])
                if dest.exists():
                    set_status(con, it["chat_id"], it["msg_id"], "downloaded", str(dest))
                    done += 1
                    on_item and on_item(it, "done")
                    return
                msg = msgs.get((it["chat_id"], it["msg_id"]))
                if msg is None or msg.file is None:
                    set_status(con, it["chat_id"], it["msg_id"], "gone")
                    on_item and on_item(it, "gone")
                    progress(f"{it['file_name']}: tin nhắn đã bị xoá", None)
                    return
                active[it["id"]] = it["file_name"]
                on_item and on_item(it, "downloading")
                tmp = dest.with_suffix(dest.suffix + ".part")

                def cb(cur, tot, _id=it["id"]):
                    got[_id] = cur
                    report()

                try:
                    await c.download_media(msg, file=str(tmp), progress_callback=cb)
                    tmp.rename(dest)
                    set_status(con, it["chat_id"], it["msg_id"], "downloaded", str(dest))
                    done += 1
                    on_item and on_item(it, "done")
                except Exception as e:  # noqa: BLE001
                    tmp.unlink(missing_ok=True)
                    on_item and on_item(it, "error")
                    progress(f"{it['file_name']}: lỗi {e}", None)
                finally:
                    active.pop(it["id"], None)
                    report()

        await asyncio.gather(*(one(it) for it in items))
        progress(f"Đã tải {done}/{len(items)} bài vào {download_dir(self.cfg)}", 1.0)
        return done
