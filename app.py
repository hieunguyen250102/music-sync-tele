#!/usr/bin/env python3
"""Giao diện đồ hoạ: .venv/bin/python app.py  (mở http://127.0.0.1:8777)"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from urllib.parse import quote

from fastapi import Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from nicegui import app, run, ui

import core

# ============================================================================ state (1 người dùng, dùng chung)
S = SimpleNamespace(
    cfg=core.load_raw_config(),
    con=core.db(),
    tg=None,
    results=[],            # list[core.Result]
    by_id={},              # id -> Result
    ver=0,                 # tăng mỗi khi danh sách kết quả đổi
    dl_ver=0,              # tăng mỗi khi trạng thái tải của 1 dòng đổi
    busy=False, task="", cancel=None,
    msg="", frac=0.0, finished_at=0.0, ok=True,
    logs=[], log_seq=0,
    dialogs=None,          # cache danh sách group/kênh
    topics=[],             # dòng của bảng gộp nghệ sĩ
    login_stage="phone",   # phone -> code -> password
    dl_queue=[], dl_running=False,
)
NAV = [("home", "Tổng quan", "space_dashboard"), ("tracks", "Bài mới", "library_music"),
       ("sources", "Nguồn & nghệ sĩ", "hub"), ("settings", "Cài đặt", "tune")]


def tg() -> core.Telegram:
    if S.tg is None:
        S.tg = core.Telegram(S.cfg)
    return S.tg


def progress(msg: str, frac: float | None = None) -> None:
    S.msg = msg
    if frac is not None:
        S.frac = frac
    S.log_seq += 1
    S.logs.append((S.log_seq, time.strftime("%H:%M:%S ") + msg))
    del S.logs[:-1500]


def set_results(results: list) -> None:
    S.results = results
    S.by_id = {r.id: r for r in results}
    for r in results:
        r.as_dict().setdefault("dl", "")
    S.ver += 1


def stats() -> dict:
    c = {"new": 0, "maybe": 0, "have": 0, "new_gb": 0.0}
    for r in S.results:
        st = r.match.status
        c[st] = c.get(st, 0) + 1
        if st == "new":
            c["new_gb"] += (r.row["size"] or 0) / 1e9
    return c


def group_names() -> dict[str, str]:
    try:
        return json.loads(core.meta_get(S.con, "group_names", "{}") or "{}")
    except ValueError:
        return {}


async def reset_tg() -> None:
    if S.tg is not None:
        await S.tg.disconnect()
    S.tg = None


app.on_shutdown(reset_tg)


# ============================================================================ nghe thử: stream có hỗ trợ tua (HTTP Range)
def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    if not header or not header.startswith("bytes="):
        return 0, size - 1
    a, _, b = header[6:].split(",")[0].strip().partition("-")
    try:
        if a == "":
            start, end = max(0, size - int(b)), size - 1
        else:
            start, end = int(a), (int(b) if b else size - 1)
    except ValueError:
        return 0, size - 1
    if start >= size:
        return None
    return start, min(end, size - 1)


async def _prefetch(chat_id: int, msg_id: int, idx: int) -> None:
    try:
        await tg().read_chunk(chat_id, msg_id, idx)
    except Exception:  # noqa: BLE001
        pass


@app.get("/preview/tg/{chat_id}/{msg_id}")
async def preview_tg(request: Request, chat_id: int, msg_id: int):
    row = S.con.execute("SELECT file_name, size FROM tracks WHERE chat_id=? AND msg_id=?", (chat_id, msg_id)).fetchone()
    if row is None:
        return Response("Không có bài này", status_code=404)
    try:
        if not await tg().authorized():
            return Response("Chưa đăng nhập Telegram", status_code=401)
        msg = await tg().get_message(chat_id, msg_id)
    except Exception as e:  # noqa: BLE001
        return Response(str(e), status_code=404)
    size = int(msg.file.size or row["size"] or 0)
    rng = _parse_range(request.headers.get("range"), size)
    if rng is None:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    start, end = rng
    ch = core.PREVIEW_CHUNK
    last_idx = (size - 1) // ch

    async def body():
        pos = start
        while pos <= end:
            idx = pos // ch
            chunk = await tg().read_chunk(chat_id, msg_id, idx)
            if idx + 1 <= last_idx:
                asyncio.create_task(_prefetch(chat_id, msg_id, idx + 1))   # tải trước đoạn kế -> phát mượt
            lo, hi = pos - idx * ch, min(len(chunk), end - idx * ch + 1)
            if lo >= hi:
                break
            yield chunk[lo:hi]
            pos = idx * ch + hi

    mime = core.MIME.get(Path(row["file_name"]).suffix.lower(), "application/octet-stream")
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Cache-Control": "no-store"}
    partial = bool(request.headers.get("range"))
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(body(), status_code=206 if partial else 200, media_type=mime, headers=headers)


@app.get("/preview/local")
async def preview_local(path: str):
    # chỉ phát file nằm trong thư viện đã quét hoặc file app đã tải (không mở file tuỳ ý)
    known = S.con.execute("SELECT 1 FROM local_files WHERE path=? UNION SELECT 1 FROM tracks WHERE local_path=?",
                          (path, path)).fetchone()
    if not known or not os.path.isfile(path):
        return Response("Không tìm thấy file", status_code=404)
    return FileResponse(path, media_type=core.MIME.get(Path(path).suffix.lower(), "application/octet-stream"))

CSS = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#f5f6fa;--card:#fff;--muted:#6b7280;--border:#e6e8ef;--soft:#eef0ff}
body.body--dark{--bg:#0d0f14;--card:#161922;--muted:#9aa3b2;--border:#252a36;--soft:#1e2240}
body{background:var(--bg);font-family:Inter,system-ui,-apple-system,sans-serif;font-feature-settings:"cv11"}
.nicegui-content{padding:0}
.page{width:100%;max-width:1400px;margin:0 auto;padding:24px 28px 120px}
.nav-item .q-focus-helper{display:none}
.q-page-sticky{z-index:3000}
.card{background:var(--card)!important;border:1px solid var(--border);border-radius:16px;box-shadow:none!important}
.muted{color:var(--muted)}
.h-title{font-size:22px;font-weight:700;letter-spacing:-.01em}
.h-sub{font-size:13px;color:var(--muted)}
.sec-title{font-size:15px;font-weight:600}
.tile{cursor:pointer;transition:transform .15s ease, box-shadow .15s ease;padding:18px 20px}
.tile:hover{transform:translateY(-2px);box-shadow:0 8px 24px rgba(17,24,39,.08)!important}
.tile .num{font-size:28px;font-weight:700;line-height:1.1}
.tile .lbl{font-size:13px;color:var(--muted)}
.tile .ico{width:42px;height:42px;border-radius:12px;display:flex;align-items:center;justify-content:center}
.app-header{background:var(--card)!important;color:inherit!important;border-bottom:1px solid var(--border)}
.app-drawer{background:var(--card)!important;border-right:1px solid var(--border)}
.nav-item{border-radius:10px;margin:2px 10px;min-height:42px;color:inherit}
.nav-item.active{background:var(--soft);color:#5b5bd6;font-weight:600}
.nav-item.active .q-icon{color:#5b5bd6}
.q-table__card{background:var(--card)!important;box-shadow:none!important}
.q-table thead th{font-weight:600;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.03em}
.q-table tbody td{font-size:13px}
.q-table tbody tr:hover{background:var(--soft)}
.selbar{position:sticky;top:64px;z-index:5;background:#5b5bd6;color:#fff;border-radius:12px;padding:8px 14px}
.taskpanel{width:400px;max-width:calc(100vw - 32px)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.step-dot{width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:600;font-size:13px}
.empty{padding:56px 16px;text-align:center}
.folder-row{border:1px solid var(--border);border-radius:10px;padding:6px 6px 6px 12px}
.ms-bar{position:fixed;left:0;right:0;bottom:0;z-index:2500;background:var(--card);border-top:1px solid var(--border);
        box-shadow:0 -8px 28px rgba(17,24,39,.10);padding:10px 18px;display:none}
@media(min-width:900px){.ms-bar{left:236px}}
body.ms-playing .ms-bar{display:block}
body.ms-playing .taskpanel{margin-bottom:118px}
body.ms-playing .page{padding-bottom:230px}
.ms-audio{width:100%;height:38px}
.ms-cover{width:48px;height:48px;border-radius:12px;flex:none;display:flex;align-items:center;justify-content:center;
          color:#fff;background:linear-gradient(135deg,#5b5bd6,#a855f7)}
.ms-err{color:#ef4444;font-size:12px}
.ms-keys{font-size:11px;color:var(--muted)}
.ms-keys kbd{border:1px solid var(--border);border-bottom-width:2px;border-radius:4px;padding:0 4px;font-family:inherit;font-size:10px}
.q-table tbody tr.ms-row-playing td{background:var(--soft)}
</style>
<script>
window.msPlayer = {
  active: false,
  el() { return document.querySelector('audio.ms-audio'); },
  show(on) {
    document.body.classList.toggle('ms-playing', on); this.active = on;
    const a = this.el(); if (!on && a) { a.pause(); a.removeAttribute('src'); a.load(); }
  },
  load(src, keep) {
    const a = this.el(); if (!a) return;
    const t = keep ? a.currentTime : 0;
    document.querySelectorAll('.ms-err').forEach(x => x.textContent = '');
    a.src = src;
    if (t > 0) a.addEventListener('loadedmetadata', () => { a.currentTime = Math.min(t, Math.max(0, (a.duration || t) - 1)); }, {once: true});
    this.show(true);
    a.play().catch(() => {});
  },
  toggle() { const a = this.el(); if (a) { a.paused ? a.play() : a.pause(); } },
  seekPct(p) { const a = this.el(); if (a && isFinite(a.duration)) a.currentTime = a.duration * p; },
  skip(s) { const a = this.el(); if (a) a.currentTime = Math.max(0, a.currentTime + s); },
};
document.addEventListener('error', (e) => {
  if (e.target && e.target.classList && e.target.classList.contains('ms-audio') && e.target.getAttribute('src'))
    document.querySelectorAll('.ms-err').forEach(x => x.textContent = 'Không phát được – định dạng trình duyệt không hỗ trợ hoặc lỗi kết nối.');
}, true);
document.addEventListener('keydown', (e) => {
  if (!window.msPlayer.active || e.ctrlKey || e.metaKey || e.altKey) return;
  const t = e.target;
  if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
  const k = e.key.toLowerCase();
  const map = {n: 'next', j: 'next', arrowdown: 'next', p: 'prev', k: 'prev', arrowup: 'prev',
               d: 'download', x: 'ignore', c: 'compare', escape: 'close'};
  if (k === ' ') { e.preventDefault(); msPlayer.toggle(); }
  else if (k === 'arrowright') { e.preventDefault(); msPlayer.skip(15); }
  else if (k === 'arrowleft') { e.preventDefault(); msPlayer.skip(-15); }
  else if (k.length === 1 && k >= '1' && k <= '9') { msPlayer.seekPct(+k / 10); }
  else if (map[k]) { e.preventDefault(); emitEvent('ms_key', map[k]); }
});
</script>
"""

STATUS_SLOT = """
<q-td :props="props">
  <q-spinner-dots v-if="props.row.dl=='downloading'" color="primary" size="22px"><q-tooltip>Đang tải</q-tooltip></q-spinner-dots>
  <q-badge v-else-if="props.row.dl=='done'" rounded color="positive" label="Đã tải"/>
  <q-badge v-else-if="props.row.dl=='error'" rounded color="negative" label="Lỗi"/>
  <q-badge v-else-if="props.row.dl=='gone'" rounded color="grey" label="Đã xoá"/>
  <q-badge v-else-if="props.row.dl=='queued'" rounded outline color="primary" label="Chờ tải"/>
  <q-badge v-else rounded
     :color="props.value=='new' ? 'positive' : props.value=='maybe' ? 'orange' : 'grey-5'"
     :label="props.value=='new' ? 'Mới' : props.value=='maybe' ? 'Xem lại' : 'Đã có'"/>
</q-td>"""
TITLE_SLOT = """
<q-td :props="props" style="max-width:520px">
  <div class="text-weight-medium" style="white-space:normal;word-break:break-word"
       :class="props.row.playing ? 'text-primary' : ''">
    <q-icon v-if="props.row.playing" name="graphic_eq" color="primary" size="18px" class="q-mr-xs"/>{{ props.row.file_name }}</div>
  <div class="muted" style="font-size:12px">
    <b>{{ props.row.artist }}</b> · {{ props.row.group }} / {{ props.row.topic }}
    <q-badge v-if="props.row.copies > 1" outline color="primary" class="q-ml-xs" :label="'×' + props.row.copies">
      <q-tooltip>Có ở {{ props.row.copies }} nơi – đã chọn bản tốt nhất</q-tooltip></q-badge>
  </div>
</q-td>"""
FMT_SLOT = """
<q-td :props="props">
  <q-chip dense square :color="props.row.lossless ? 'teal-1' : 'grey-3'" :text-color="props.row.lossless ? 'teal-9' : 'grey-8'"
          class="text-weight-bold" style="font-size:11px" :label="(props.row.ext || '?').toUpperCase()"/>
</q-td>"""
LOCAL_SLOT = """
<q-td :props="props" style="max-width:360px">
  <template v-if="props.row.local">
    <div style="font-size:12px;white-space:normal;word-break:break-word"><q-icon name="folder" size="14px" class="q-mr-xs muted"/>{{ props.row.local }}
      <q-tooltip>{{ props.row.local_path }}</q-tooltip></div>
    <div style="font-size:12px" :class="props.row.status=='have' ? 'text-positive' : 'text-orange-9'">
      {{ props.row.reason || ('giống ' + props.row.score + '%') }}</div>
  </template>
  <span v-else class="muted">—</span>
</q-td>"""
ACTIONS_SLOT = """
<q-td :props="props" auto-width>
  <q-btn flat round dense :icon="props.row.playing ? 'graphic_eq' : 'play_circle'" color="accent"
         @click.stop="() => $parent.$emit('play', props.row)"><q-tooltip>Nghe thử</q-tooltip></q-btn>
  <q-btn flat round dense icon="download" color="primary"
         :disable="['downloading','done','queued'].includes(props.row.dl)"
         @click.stop="() => $parent.$emit('dl', props.row)"><q-tooltip>Tải bài này</q-tooltip></q-btn>
  <q-btn flat round dense icon="do_not_disturb_on" color="grey-6" :disable="props.row.dl=='done'"
         @click.stop="() => $parent.$emit('ignore', props.row)"><q-tooltip>Đã có / bỏ qua</q-tooltip></q-btn>
</q-td>"""


# ============================================================================ helpers UI
def card(classes: str = ""):
    return ui.card().classes(f"card {classes}")


def section_header(title: str, sub: str | None = None):
    with ui.column().classes("gap-0"):
        ui.label(title).classes("sec-title")
        if sub:
            ui.label(sub).classes("h-sub")


async def pick_folder(start: str | None) -> str | None:
    p0 = core.expand(start) if start and core.expand(start).exists() else Path.home()
    state = {"p": p0}
    with ui.dialog() as dlg, card("w-[600px] max-w-full q-pa-md"):
        ui.label("Chọn thư mục").classes("sec-title")
        cur = ui.label().classes("mono muted break-all")
        with ui.row().classes("gap-1"):
            for label, target in (("Home", Path.home()), ("/", Path("/")), ("/media", Path("/media")), ("/mnt", Path("/mnt"))):
                if target.exists():
                    ui.button(label, on_click=lambda t=target: show(t)).props("flat dense no-caps size=sm")
        lst = ui.list().props("dense separator").classes("w-full overflow-auto").style("height:340px")
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Huỷ", on_click=lambda: dlg.submit(None)).props("flat no-caps")
            ui.button("Chọn thư mục này", icon="check", on_click=lambda: dlg.submit(str(state["p"]))).props("unelevated no-caps")

    def show(p: Path):
        state["p"] = p
        cur.text = str(p)
        lst.clear()
        try:
            subs = sorted((d for d in p.iterdir() if d.is_dir() and not d.name.startswith(".")), key=lambda d: d.name.lower())
        except OSError:
            subs = []
        with lst:
            if p.parent != p:
                with ui.item(on_click=lambda: show(p.parent)):
                    with ui.item_section().props("avatar"):
                        ui.icon("arrow_upward")
                    ui.item_section("..")
            for d in subs[:800]:
                with ui.item(on_click=lambda d=d: show(d)):
                    with ui.item_section().props("avatar"):
                        ui.icon("folder", color="amber-7")
                    ui.item_section(d.name)
            if not subs:
                ui.item("(không có thư mục con)").classes("muted")

    show(p0)
    result = await dlg
    dlg.delete()
    return result


# ============================================================================ page
@ui.page("/")
async def index():
    ui.add_head_html(CSS)
    ui.colors(primary="#5b5bd6", secondary="#64748b", accent="#a855f7", positive="#10b981",
              negative="#ef4444", warning="#f59e0b", info="#0ea5e9")
    dark = ui.dark_mode(app.storage.general.get("dark"))
    seen = {"ver": -1, "dl_ver": -1, "log": S.log_seq}
    player = {"id": None, "src": "tg", "sync": False}

    # ---------------------------------------------------------------- header + nav
    with ui.header().classes("app-header items-center px-4 py-2 gap-3"):
        ui.button(icon="menu", on_click=lambda: drawer.toggle()).props("flat round dense").classes("lt-md")
        with ui.row().classes("items-center gap-2 no-wrap"):
            with ui.element("div").classes("step-dot").style("background:#5b5bd6;color:#fff;border-radius:10px"):
                ui.icon("graphic_eq", size="18px")
            ui.label("Music Sync").classes("text-weight-bold").style("font-size:17px")
            ui.label("Telegram → thư viện").classes("muted gt-xs").style("font-size:13px")
        ui.space()
        acct = ui.chip("Đang kiểm tra…", icon="hourglass_empty").props("outline")

        def toggle_dark():
            nxt = {None: True, True: False, False: None}[dark.value]
            dark.set_value(nxt)
            app.storage.general["dark"] = nxt
            dark_btn.props(f"icon={ {None: 'brightness_auto', True: 'dark_mode', False: 'light_mode'}[nxt] }")

        dark_btn = ui.button(on_click=toggle_dark).props(
            f"flat round icon={ {None: 'brightness_auto', True: 'dark_mode', False: 'light_mode'}[dark.value] }")
        dark_btn.tooltip("Giao diện sáng / tối / theo hệ thống")

    nav_items: dict[str, ui.item] = {}
    nav_badges: dict[str, ui.badge] = {}
    with ui.left_drawer(value=None, bordered=False).classes("app-drawer q-pt-md").props("width=236 breakpoint=900") as drawer:
        for key, label, icon in NAV:
            with ui.item(on_click=lambda k=key: go(k)).classes("nav-item") as it:
                with ui.item_section().props("avatar").style("min-width:36px"):
                    ui.icon(icon, size="20px")
                ui.item_section(label)
                if key == "tracks":
                    with ui.item_section().props("side"):
                        nav_badges[key] = ui.badge("", color="positive").props("rounded")
            nav_items[key] = it
        ui.space()
        last_lbl = ui.label("").classes("muted q-px-lg q-pb-md").style("font-size:12px;white-space:pre-line")

    def go(key: str):
        panels.set_value(key)
        for k, it in nav_items.items():
            it.classes(add="active") if k == key else it.classes(remove="active")

    async def refresh_account():
        if not core.credentials_ok(S.cfg):
            acct.text = "Chưa cấu hình Telegram"
            acct.props("icon=link_off color=warning text-color=white")
        else:
            try:
                ok = await tg().authorized()
            except Exception as e:  # noqa: BLE001
                ok = False
                progress(f"Lỗi kết nối Telegram: {e}")
            if ok:
                me = tg().me
                acct.text = f"{me.first_name or ''} {('@' + me.username) if me.username else ''}".strip()
                acct.props("icon=verified color=positive text-color=white")
            else:
                acct.text = "Chưa đăng nhập"
                acct.props("icon=login color=orange text-color=white")
        acct.update()
        account_card.refresh()
        dashboard.refresh()

    async def guard(label: str, fn):
        if S.busy:
            ui.notify("Đang có tác vụ khác chạy, đợi xong đã nhé.", type="warning")
            return None
        S.busy, S.task, S.frac, S.ok = True, label, 0.0, True
        S.cancel = None
        progress(f"▶ {label}")
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001
            S.ok = False
            progress(f"✖ {e}")
            ui.notify(str(e), type="negative", multi_line=True, close_button=True)
            return None
        finally:
            S.busy = False
            S.finished_at = time.time()

    # ---------------------------------------------------------------- actions
    async def do_compare():
        async def job():
            res = await run.io_bound(core.compute, S.cfg, S.con, progress)
            set_results(res)
        await guard("Đối chiếu với thư viện", job)

    async def need_login():
        if not core.credentials_ok(S.cfg) or not await tg().authorized():
            go("settings")
            raise RuntimeError("Cần đăng nhập Telegram trước (tab Cài đặt).")

    async def do_scan():
        async def job():
            await need_login()
            if not S.cfg.get("groups"):
                go("sources")
                raise RuntimeError("Chưa chọn group nào để quét (tab Nguồn & nghệ sĩ).")
            await tg().scan(S.con, progress)
        await guard("Quét Telegram", job)
        await do_compare()

    async def do_download(items: list[dict]):
        # dùng đúng dict trong bộ nhớ (hàng chọn từ trình duyệt chỉ là bản sao)
        items = [S.by_id[it["id"]].as_dict() for it in items if it.get("id") in S.by_id]
        items = [it for it in items if it.get("dl") not in ("done", "downloading", "queued")]
        if not items:
            ui.notify("Không có bài nào để tải.", type="warning")
            return
        if S.busy and not S.dl_running:
            ui.notify("Đang có tác vụ khác chạy, đợi xong đã nhé.", type="warning")
            return
        for it in items:
            it["dl"] = "queued"
        S.dl_queue.extend(items)
        S.dl_ver += 1
        if S.dl_running:                       # đang tải -> chỉ thêm vào hàng chờ
            ui.notify(f"Đã thêm {len(items)} bài vào hàng chờ tải.", type="info")
            return

        def on_item(it: dict, state: str):
            it["dl"] = state
            if state == "done" and it["id"] in S.by_id:
                S.by_id[it["id"]].match.status = "have"
            S.dl_ver += 1

        async def job():
            S.dl_running = True
            total = done = 0
            try:
                await need_login()
                S.cancel = asyncio.Event()
                while S.dl_queue and not S.cancel.is_set():
                    batch, S.dl_queue = S.dl_queue, []
                    total += len(batch)
                    S.task = f"Tải nhạc · {total} bài"
                    done += await tg().download(S.con, batch, progress, S.cancel, on_item)
            finally:
                S.dl_running = False
                for r in S.results:            # bài còn chờ (bị dừng/lỗi đăng nhập) -> trả lại trạng thái
                    if r.as_dict().get("dl") == "queued":
                        r.as_dict()["dl"] = ""
                S.dl_queue = []
                S.dl_ver += 1
            ui.notify(f"Đã tải {done}/{total} bài", type="positive" if done == total else "warning")
            S.ver += 1
        await guard("Tải nhạc", job)

    def do_ignore(rows: list[dict]):
        res = [S.by_id[r["id"]] for r in rows if r["id"] in S.by_id]
        if not res:
            return
        core.ignore_results(S.con, res)
        ids = {r.id for r in res}
        set_results([r for r in S.results if r.id not in ids])
        ui.notify(f"Đã bỏ qua {len(res)} bài – sẽ không hiện lại.", type="info")

    async def do_sync():
        await do_scan()

    # ================================================================ panels
    with ui.tab_panels(value="home", animated=False).classes("w-full bg-transparent").props("keep-alive") as panels:

        # ============================================================ HOME
        with ui.tab_panel("home").classes("q-pa-none"):
            with ui.element("div").classes("page"):
                @ui.refreshable
                def dashboard():
                    c = stats()
                    cnt = core.counts(S.con)
                    creds = core.credentials_ok(S.cfg)
                    logged = bool(S.tg and S.tg.me)
                    has_groups = bool(S.cfg.get("groups"))
                    dirs = core.music_dirs(S.cfg)
                    has_dirs = bool(dirs) and all(d.exists() for d in dirs)

                    with ui.row().classes("w-full items-end justify-between q-mb-lg"):
                        with ui.column().classes("gap-1"):
                            ui.label("Tổng quan").classes("h-title")
                            ui.label("Quét các group Telegram, so với thư viện trong máy và tải bài còn thiếu.").classes("h-sub")
                        with ui.row().classes("gap-2"):
                            ui.button("Đối chiếu lại", icon="compare_arrows", on_click=do_compare).props("outline no-caps")
                            ui.button("Đồng bộ ngay", icon="sync", on_click=do_sync).props("unelevated no-caps size=md")

                    steps = [
                        ("Nhập API Telegram", creds, "settings"),
                        ("Đăng nhập tài khoản", logged, "settings"),
                        ("Chọn thư mục nhạc trong máy", has_dirs, "settings"),
                        ("Chọn group cần theo dõi", has_groups, "sources"),
                    ]
                    if not all(s[1] for s in steps):
                        with card("w-full q-pa-lg q-mb-lg"):
                            section_header("Bắt đầu", "Hoàn thành các bước sau để đồng bộ lần đầu")
                            with ui.column().classes("w-full gap-2 q-mt-sm"):
                                first_todo = next(i for i, s in enumerate(steps) if not s[1])
                                for i, (label, done, target) in enumerate(steps):
                                    with ui.row().classes("items-center w-full no-wrap gap-3"):
                                        with ui.element("div").classes("step-dot").style(
                                                "background:#10b981;color:#fff" if done else
                                                ("background:#5b5bd6;color:#fff" if i == first_todo else "background:var(--soft);color:var(--muted)")):
                                            ui.icon("check", size="16px") if done else ui.label(str(i + 1))
                                        ui.label(label).classes("text-weight-medium" if i == first_todo else "muted" if done else "")
                                        ui.space()
                                        if not done and i == first_todo:
                                            ui.button("Làm ngay", icon="arrow_forward", on_click=lambda t=target: go(t)).props("flat no-caps dense")

                    tiles = [
                        ("Bài mới", c["new"], f"~{c['new_gb']:.2f} GB cần tải", "fiber_new", "#10b981", "new"),
                        ("Cần xem lại", c["maybe"], "có thể đã có trong máy", "rule", "#f59e0b", "maybe"),
                        ("Đã có", c["have"], "khớp với file trong máy", "task_alt", "#64748b", "have"),
                        ("Đã tải", cnt.get("downloaded", 0), "bài tải bằng app", "download_done", "#0ea5e9", None),
                        ("File trong máy", cnt.get("local", 0), f"{len(dirs)} thư mục", "folder_open", "#a855f7", None),
                        ("Group theo dõi", len(S.cfg.get("groups") or []), f"{len(S.cfg.get('artists') or {})} nghệ sĩ đã gộp", "hub", "#5b5bd6", "sources"),
                    ]
                    with ui.grid().classes("w-full gap-4 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-6"):
                        for label, num, sub, icon, color, target in tiles:
                            def click(t=target):
                                if t in ("new", "maybe", "have"):
                                    f_status.set_value(t)
                                    go("tracks")
                                elif t:
                                    go(t)
                            with card("tile").on("click", click):
                                with ui.row().classes("items-center justify-between w-full no-wrap"):
                                    with ui.column().classes("gap-0"):
                                        ui.label(f"{num:,}".replace(",", ".")).classes("num")
                                        ui.label(label).classes("text-weight-medium")
                                    with ui.element("div").classes("ico").style(f"background:{color}1f;color:{color}"):
                                        ui.icon(icon, size="22px")
                                ui.label(sub).classes("lbl")

                    # Nghệ sĩ có nhiều bài mới nhất
                    by_artist: dict[str, list] = {}
                    for r in S.results:
                        if r.match.status == "new":
                            by_artist.setdefault(r.artist, []).append(r)
                    if by_artist:
                        top = sorted(by_artist.items(), key=lambda kv: -len(kv[1]))[:12]
                        with card("w-full q-pa-lg q-mt-lg"):
                            section_header("Nghệ sĩ có nhiều bài mới", "Bấm để xem danh sách")
                            with ui.row().classes("gap-2 q-mt-sm"):
                                for name, lst in top:
                                    def open_artist(n=name):
                                        f_status.set_value("new")
                                        f_artist.set_value(n)
                                        go("tracks")
                                    ui.chip(f"{name} · {len(lst)}", icon="person", on_click=open_artist).props("outline clickable")
                    elif not S.results and core.counts(S.con).get("new", 0) == 0 and has_groups:
                        with card("w-full q-mt-lg"):
                            with ui.column().classes("empty items-center w-full"):
                                ui.icon("cloud_sync", size="48px").classes("muted")
                                ui.label("Chưa có dữ liệu – bấm “Đồng bộ ngay” để quét Telegram lần đầu.").classes("muted")

                dashboard()

        # ============================================================ TRACKS
        with ui.tab_panel("tracks").classes("q-pa-none"):
            with ui.element("div").classes("page"):
                with ui.row().classes("w-full items-end justify-between q-mb-md"):
                    with ui.column().classes("gap-1"):
                        ui.label("Bài mới").classes("h-title")
                        summary = ui.label("").classes("h-sub")
                    with ui.row().classes("gap-2"):
                        ui.button("Quét Telegram", icon="cloud_sync", on_click=do_scan).props("outline no-caps")
                        ui.button("Đối chiếu lại", icon="compare_arrows", on_click=do_compare).props("outline no-caps")
                        dl_all_btn = ui.button("Tải tất cả bài mới", icon="download",
                                               on_click=lambda: do_download([r.as_dict() for r in S.results if r.match.status == "new"])
                                               ).props("unelevated no-caps")

                with card("w-full q-pa-md"):
                    with ui.row().classes("w-full items-center gap-3"):
                        f_status = ui.toggle({"new": "Mới", "maybe": "Cần xem lại", "have": "Đã có", "all": "Tất cả"},
                                             value="new").props("no-caps rounded unelevated toggle-color=primary")
                        f_search = ui.input(placeholder="Tìm tên bài, file trong máy…").props(
                            "outlined dense clearable debounce=250").classes("w-72")
                        with f_search.add_slot("prepend"):
                            ui.icon("search")
                        f_artist = ui.select(["(Tất cả nghệ sĩ)"], value="(Tất cả nghệ sĩ)", with_input=True).props(
                            "outlined dense options-dense").classes("w-60")
                        f_lossless = ui.switch("Chỉ lossless")
                        shown_lbl = ui.label("").classes("muted q-ml-auto").style("font-size:13px")

                    with ui.row().classes("selbar w-full items-center q-mt-md") as selbar:
                        ui.icon("check_box")
                        sel_lbl = ui.label("")
                        ui.space()
                        ui.button("Tải các bài đã chọn", icon="download",
                                  on_click=lambda: do_download(list(table.selected))).props("unelevated no-caps color=white text-color=primary")
                        ui.button("Đã có / bỏ qua", icon="do_not_disturb_on",
                                  on_click=lambda: (do_ignore(list(table.selected)), clear_sel())).props("flat no-caps color=white")
                        ui.button(icon="close", on_click=lambda: clear_sel()).props("flat round dense color=white")
                    selbar.set_visibility(False)

                    columns = [
                        {"name": "status", "label": "", "field": "status", "align": "left", "sortable": True},
                        {"name": "title", "label": "Bài trên Telegram", "field": "file_name", "align": "left", "sortable": True},
                        {"name": "fmt", "label": "Định dạng", "field": "ext", "sortable": True},
                        {"name": "size_mb", "label": "MB", "field": "size_mb", "sortable": True},
                        {"name": "duration", "label": "Dài", "field": "duration"},
                        {"name": "local", "label": "Trong máy", "field": "local", "align": "left"},
                        {"name": "date", "label": "Ngày đăng", "field": "date", "sortable": True},
                        {"name": "actions", "label": "", "field": "id"},
                    ]
                    table = ui.table(rows=[], columns=columns, row_key="id", selection="multiple",
                                     pagination={"rowsPerPage": 50, "sortBy": None}).classes("w-full q-mt-sm").props(
                        "flat dense separator=horizontal rows-per-page-label='Dòng mỗi trang' "
                        "no-data-label='Không có bài nào' :rows-per-page-options='[25,50,100,200]'")
                    table.add_slot("body-cell-status", STATUS_SLOT)
                    table.add_slot("body-cell-title", TITLE_SLOT)
                    table.add_slot("body-cell-fmt", FMT_SLOT)
                    table.add_slot("body-cell-local", LOCAL_SLOT)
                    table.add_slot("body-cell-actions", ACTIONS_SLOT)
                    table.add_slot("no-data", """
                        <div class="full-width column items-center q-pa-xl muted">
                          <q-icon name="library_music" size="44px"/>
                          <div class="q-mt-sm">Không có bài nào ở mục này.</div>
                        </div>""")

        def clear_sel():
            table.selected = []
            table.update()
            on_select()


        def on_select(_=None):
            n = len(table.selected)
            selbar.set_visibility(n > 0)
            mb = sum(r.get("size_mb", 0) for r in table.selected)
            sel_lbl.text = f"Đã chọn {n} bài · {mb:,.0f} MB"

        table.on_select(on_select)
        table.on("dl", lambda e: do_download([S.by_id[e.args["id"]].as_dict()] if e.args["id"] in S.by_id else []))
        table.on("ignore", lambda e: do_ignore([e.args]))
        table.on("play", lambda e: play(e.args["id"]))

        def render_table():
            st = f_status.value
            art = f_artist.value
            q = (f_search.value or "").lower().strip()
            ll = f_lossless.value
            rows = []
            for r in S.results:
                d = r.as_dict()
                if st != "all" and d["status"] != st and not (d.get("dl") and st == "new"):
                    continue
                if art and art != "(Tất cả nghệ sĩ)" and d["artist"] != art:
                    continue
                if ll and not d["lossless"]:
                    continue
                if q and q not in d["file_name"].lower() and q not in d["local"].lower() and q not in d["artist"].lower():
                    continue
                rows.append({**d, "playing": d["id"] == player["id"]})   # bản sao: NiceGUI chỉ gửi khi khác bản trước
            table.update_rows(rows)
            on_select()
            c = stats()
            f_status.set_options({"new": f"Mới · {c['new']}", "maybe": f"Cần xem lại · {c['maybe']}",
                                  "have": f"Đã có · {c['have']}", "all": "Tất cả"}, value=st)
            shown_lbl.text = f"Hiện {len(rows):,} bài".replace(",", ".")
            summary.text = (f"{c['new']} bài mới (~{c['new_gb']:.2f} GB) · {c['maybe']} cần xem lại · {c['have']} đã có"
                            if S.results else "Chưa đối chiếu – bấm “Quét Telegram” hoặc “Đối chiếu lại”.")
            dl_all_btn.set_enabled(c["new"] > 0)

        for el in (f_status, f_artist, f_search, f_lossless):
            el.on_value_change(lambda _: render_table())

        # ============================================================ SOURCES
        with ui.tab_panel("sources").classes("q-pa-none"):
            with ui.element("div").classes("page"):
                ui.label("Nguồn & nghệ sĩ").classes("h-title")
                ui.label("Chọn group cần theo dõi và gộp các kênh/topic khác tên nhưng cùng một DJ.").classes("h-sub q-mb-lg")

                with card("w-full q-pa-lg"):
                    with ui.row().classes("w-full items-center justify-between"):
                        section_header("Group đang theo dõi", "Mỗi topic (hoặc mỗi channel) trong group được xem là một nghệ sĩ")
                        ui.button("Chọn group", icon="playlist_add_check", on_click=lambda: open_group_picker()).props("unelevated no-caps")

                    @ui.refreshable
                    def group_chips():
                        names = group_names()
                        gs = S.cfg.get("groups") or []
                        if not gs:
                            ui.label("Chưa chọn group nào.").classes("muted q-mt-sm")
                            return
                        with ui.row().classes("gap-2 q-mt-sm"):
                            for g in gs:
                                ui.chip(names.get(str(g), str(g)), icon="forum", removable=True,
                                        on_value_change=lambda e, g=g: remove_group(g) if not e.value else None).props("outline")
                    group_chips()

                with card("w-full q-pa-lg q-mt-lg"):
                    with ui.row().classes("w-full items-center justify-between"):
                        section_header("Gộp nghệ sĩ", "Topic nào cùng một DJ thì đặt cùng tên nghệ sĩ. Để trống = dùng tên topic.")
                        with ui.row().classes("gap-2"):
                            ui.button("Tải topic", icon="download", on_click=lambda: load_topics()).props("outline no-caps")
                            ui.button("Tự gợi ý gộp", icon="auto_awesome", on_click=lambda: auto_suggest()).props("outline no-caps")
                            ui.button("Lưu", icon="save", on_click=lambda: save_mapping()).props("unelevated no-caps")
                    with ui.row().classes("w-full items-center gap-3 q-mt-sm"):
                        t_search = ui.input(placeholder="Lọc topic / nghệ sĩ…").props("outlined dense clearable").classes("w-72")
                        with t_search.add_slot("prepend"):
                            ui.icon("search")
                        topics_info = ui.label("Bấm “Tải topic” để lấy danh sách topic của các group.").classes("muted").style("font-size:13px")
                    ui.html('<datalist id="artist-list"></datalist>', sanitize=False).classes("hidden")
                    t_table = ui.table(
                        rows=[], row_key="key", pagination={"rowsPerPage": 50},
                        columns=[
                            {"name": "topic", "label": "Topic / kênh", "field": "topic", "align": "left", "sortable": True},
                            {"name": "group", "label": "Group", "field": "group", "align": "left", "sortable": True},
                            {"name": "artist", "label": "Nghệ sĩ (tên chuẩn)", "field": "artist", "align": "left", "sortable": True},
                            {"name": "skip", "label": "Bỏ qua", "field": "skip"},
                        ]).classes("w-full q-mt-sm").props("flat dense separator=horizontal no-data-label='Chưa có topic'")
                    t_table.add_slot("body-cell-group", """
                        <q-td :props="props"><q-chip dense outline icon="forum" :label="props.value" style="font-size:12px"/></q-td>""")
                    t_table.add_slot("body-cell-artist", """
                        <q-td :props="props" style="min-width:260px">
                          <q-input v-model="props.row.artist" dense outlined debounce="300" :placeholder="props.row.topic"
                                   list="artist-list"
                                   @update:model-value="() => $parent.$emit('rename', props.row)">
                            <template v-slot:append><q-icon v-if="props.row.suggested && !props.row.artist" name="auto_awesome"
                              color="accent" class="cursor-pointer" size="18px"
                              @click="() => { props.row.artist = props.row.suggested; $parent.$emit('rename', props.row) }">
                              <q-tooltip>Gợi ý: {{ props.row.suggested }}</q-tooltip></q-icon></template>
                          </q-input>
                        </q-td>""")
                    t_table.add_slot("body-cell-skip", """
                        <q-td :props="props"><q-toggle v-model="props.row.skip" dense
                              @update:model-value="() => $parent.$emit('rename', props.row)"/></q-td>""")
                    t_table.bind_filter_from(t_search, "value")

        def remove_group(g):
            S.cfg["groups"] = [x for x in S.cfg.get("groups") or [] if x != g]
            core.save_raw_config(S.cfg)
            group_chips.refresh()
            dashboard.refresh()

        async def open_group_picker():
            async def load():
                await need_login()
                S.dialogs = await tg().dialogs()
            if S.dialogs is None:
                await guard("Tải danh sách group", load)
                if S.dialogs is None:
                    return
            chosen = {str(g) for g in S.cfg.get("groups") or []}
            checks: dict = {}
            with ui.dialog() as dlg, card("w-[640px] max-w-full q-pa-md"):
                ui.label("Chọn group / kênh nhạc").classes("sec-title")
                flt = ui.input(placeholder="Tìm theo tên…").props("outlined dense clearable autofocus").classes("w-full")
                with ui.scroll_area().style("height:420px").classes("w-full"):
                    rows_box = ui.column().classes("w-full gap-0")
                    with rows_box:
                        for d in sorted(S.dialogs, key=lambda d: (str(d["id"]) not in chosen, d["name"].lower())):
                            with ui.row().classes("w-full items-center no-wrap q-py-xs") as row:
                                cb = ui.checkbox(value=str(d["id"]) in chosen)
                                ui.label(d["name"]).classes("text-weight-medium ellipsis").style("flex:1")
                                ui.badge({"forum": "Forum (có topic)", "group": "Group", "channel": "Channel"}[d["kind"]],
                                         color={"forum": "primary", "group": "secondary", "channel": "info"}[d["kind"]]).props("outline")
                            checks[d["id"]] = (cb, row, d["name"].lower())

                def apply_filter():
                    q = (flt.value or "").lower()
                    for cb, row, name in checks.values():
                        row.set_visibility(q in name)
                flt.on_value_change(lambda _: apply_filter())
                with ui.row().classes("w-full justify-end gap-2 q-mt-sm"):
                    ui.button("Huỷ", on_click=lambda: dlg.submit(None)).props("flat no-caps")
                    ui.button("Lưu", icon="check", on_click=lambda: dlg.submit(
                        [gid for gid, (cb, _, _) in checks.items() if cb.value])).props("unelevated no-caps")
            res = await dlg
            dlg.delete()
            if res is None:
                return
            S.cfg["groups"] = res
            core.save_raw_config(S.cfg)
            names = {str(d["id"]): d["name"] for d in S.dialogs}
            core.meta_set(S.con, "group_names", json.dumps(names, ensure_ascii=False))
            group_chips.refresh()
            dashboard.refresh()
            ui.notify(f"Đang theo dõi {len(res)} group.", type="positive")
            if res:
                await load_topics()

        def refresh_topic_table():
            t_table.rows = S.topics
            t_table.update()
            artists = sorted({r["artist"] for r in S.topics if r["artist"]} | set((S.cfg.get("artists") or {}).keys()),
                             key=str.lower)
            opts = "".join(f'<option value="{a.replace(chr(34), "&quot;")}"></option>' for a in artists)
            ui.run_javascript(f"document.getElementById('artist-list').innerHTML = {json.dumps(opts)}")
            n_art = len({core.norm_name(r['artist'] or r['topic']) for r in S.topics if not r["skip"]})
            sug = sum(1 for r in S.topics if r["suggested"] and not r["artist"])
            topics_info.text = (f"{len(S.topics)} topic → {n_art} nghệ sĩ"
                                + (f" · {sug} gợi ý gộp chưa áp dụng (bấm ✨)" if sug else ""))

        async def load_topics():
            async def job():
                await need_login()
                rows = await tg().group_topics(progress)
                amap = core.alias_map(S.cfg)
                sug = core.suggest_artists([r["topic"] for r in rows])
                # cụm nào đã có topic được đặt tên trước đó -> dùng tên đó cho cả cụm
                for name in set(sug.values()):
                    members = [t for t, n in sug.items() if n == name]
                    existing = next((amap[core.norm_name(t)] for t in members if core.norm_name(t) in amap), None)
                    if existing:
                        for t in members:
                            sug[t] = existing
                S.topics = []
                for r in rows:
                    mapped = amap.get(core.norm_name(r["topic"]), "")
                    S.topics.append({
                        "key": f"{r['group_id']}:{r['topic_id']}", "topic": r["topic"], "group": r["group"],
                        "artist": mapped if core.norm_name(mapped) != core.norm_name(r["topic"]) else "",
                        "suggested": sug.get(r["topic"], ""),
                        "skip": core.is_skipped(S.cfg, r["topic"]),
                    })
                S.topics.sort(key=lambda r: (core.norm_name(r["artist"] or r["suggested"] or r["topic"]), r["group"]))
                refresh_topic_table()
                progress(f"Có {len(rows)} topic", 1)
            await guard("Tải topic", job)

        def on_rename(e):
            row = next((r for r in S.topics if r["key"] == e.args["key"]), None)
            if row:
                row["artist"] = (e.args.get("artist") or "").strip()
                row["skip"] = bool(e.args.get("skip"))
        t_table.on("rename", on_rename)

        def auto_suggest():
            if not S.topics:
                ui.notify("Bấm “Tải topic” trước.", type="warning")
                return
            n = 0
            for r in S.topics:
                if r["suggested"] and not r["artist"]:
                    r["artist"] = r["suggested"]
                    n += 1
            refresh_topic_table()
            ui.notify(f"Đã điền {n} gợi ý – kiểm tra lại rồi bấm Lưu." if n else "Không có gợi ý mới.",
                      type="info")

        def save_mapping():
            if not S.topics:
                ui.notify("Chưa có topic nào.", type="warning")
                return
            loaded = {core.norm_name(r["topic"]) for r in S.topics}
            artists: dict[str, list[str]] = {}
            for canon, aliases in (S.cfg.get("artists") or {}).items():
                keep = [a for a in aliases or [] if core.norm_name(str(a)) not in loaded]
                if keep:
                    artists[canon] = keep
            skips = [s for s in S.cfg.get("skip_topics") or [] if core.norm_name(s) not in loaded]
            canon_by_norm = {core.norm_name(k): k for k in artists}   # gộp "Artbat" / "ARTBAT"
            for r in S.topics:
                name = (r["artist"] or "").strip()
                if name:
                    name = canon_by_norm.setdefault(core.norm_name(name), name)
                    r["artist"] = name
                if r["skip"]:
                    skips.append(r["topic"])
                if name and core.norm_name(name) != core.norm_name(r["topic"]):
                    lst = artists.setdefault(name, [])
                    if r["topic"] not in lst:
                        lst.append(r["topic"])
            S.cfg["artists"], S.cfg["skip_topics"] = artists, list(dict.fromkeys(skips))
            core.save_raw_config(S.cfg)
            refresh_topic_table()
            dashboard.refresh()
            ui.notify(f"Đã lưu: {len(artists)} nghệ sĩ gộp, {len(S.cfg['skip_topics'])} topic bỏ qua.", type="positive")
            if S.results:
                asyncio.create_task(do_compare())

        # ============================================================ SETTINGS
        with ui.tab_panel("settings").classes("q-pa-none"):
            with ui.element("div").classes("page"):
                ui.label("Cài đặt").classes("h-title")
                ui.label("Các thay đổi được lưu tự động.").classes("h-sub q-mb-lg")
                with ui.grid().classes("w-full gap-5 grid-cols-1 lg:grid-cols-2"):
                    with card("q-pa-lg"):
                        @ui.refreshable
                        def account_card():
                            creds = core.credentials_ok(S.cfg)
                            logged = bool(S.tg and S.tg.me)
                            section_header("Tài khoản Telegram")
                            if logged:
                                me = S.tg.me
                                with ui.row().classes("items-center gap-3 q-mt-md"):
                                    initials = "".join(x[0] for x in f"{me.first_name or ''} {me.last_name or ''}".split()[:2]).upper() or "?"
                                    with ui.element("div").classes("step-dot").style(
                                            "width:44px;height:44px;background:#5b5bd6;color:#fff;font-size:16px"):
                                        ui.label(initials)
                                    with ui.column().classes("gap-0"):
                                        ui.label(f"{me.first_name or ''} {me.last_name or ''}".strip()).classes("text-weight-medium")
                                        ui.label(f"@{me.username}" if me.username else f"ID {me.id}").classes("muted")
                                    ui.space()
                                    ui.badge("Đã kết nối", color="positive").props("rounded")
                                with ui.row().classes("gap-2 q-mt-md"):
                                    ui.button("Đăng xuất", icon="logout", on_click=lambda: confirm_logout()).props("outline no-caps color=negative")
                                return
                            if not creds:
                                ui.markdown("Vào **[my.telegram.org](https://my.telegram.org)** → *API development tools* → "
                                            "tạo app bất kỳ → copy **api_id** và **api_hash**.").classes("muted q-mt-sm")
                            else:
                                ui.label("API đã lưu. Đăng nhập bằng số điện thoại Telegram của bạn.").classes("muted q-mt-sm")
                            with ui.expansion("API Telegram", icon="key", value=not creds).classes("w-full q-mt-sm").props("dense"):
                                api_id = ui.input("api_id", value=str(S.cfg.get("api_id") or "") if creds else "").props("outlined dense").classes("w-full")
                                api_hash = ui.input("api_hash", value=str(S.cfg.get("api_hash") or "") if creds else "",
                                                    password=True, password_toggle_button=True).props("outlined dense").classes("w-full q-mt-sm")

                                async def save_api():
                                    S.cfg["api_id"] = int(api_id.value) if (api_id.value or "").strip().isdigit() else None
                                    S.cfg["api_hash"] = (api_hash.value or "").strip()
                                    core.save_raw_config(S.cfg)
                                    await reset_tg()
                                    if not core.credentials_ok(S.cfg):
                                        ui.notify("api_id phải là số, api_hash dài 32 ký tự.", type="warning")
                                    else:
                                        ui.notify("Đã lưu API.", type="positive")
                                    await refresh_account()
                                ui.button("Lưu API", icon="save", on_click=save_api).props("unelevated no-caps").classes("q-mt-sm")
                            if not creds:
                                return
                            ui.separator().classes("q-my-md")
                            if S.login_stage == "phone":
                                phone = ui.input("Số điện thoại", placeholder="+84 912 345 678").props("outlined dense").classes("w-full")

                                async def send_code():
                                    async def job():
                                        await tg().send_code((phone.value or "").replace(" ", ""))
                                        S.login_stage = "code"
                                        account_card.refresh()
                                        ui.notify("Đã gửi mã – mở app Telegram để xem.", type="positive")
                                    await guard("Gửi mã đăng nhập", job)
                                ui.button("Gửi mã đăng nhập", icon="sms", on_click=send_code).props("unelevated no-caps").classes("q-mt-sm")
                            else:
                                need_pw = S.login_stage == "password"
                                code = ui.input("Mã đăng nhập (trong app Telegram)").props("outlined dense").classes("w-full")
                                code.set_visibility(not need_pw)
                                pw = ui.input("Mật khẩu 2 lớp", password=True, password_toggle_button=True).props(
                                    "outlined dense").classes("w-full")
                                pw.set_visibility(need_pw)

                                async def login():
                                    async def job():
                                        res = await tg().sign_in(code=(code.value or "").strip() or None,
                                                                 password=(pw.value or "") or None)
                                        if res == "need_password":
                                            S.login_stage = "password"
                                            account_card.refresh()
                                            ui.notify("Tài khoản có mật khẩu 2 lớp – nhập để tiếp tục.", type="info")
                                            return
                                        S.login_stage = "phone"
                                        ui.notify("Đăng nhập thành công!", type="positive")
                                        await refresh_account()
                                        go("sources" if not S.cfg.get("groups") else "home")
                                    await guard("Đăng nhập", job)
                                with ui.row().classes("gap-2 q-mt-sm"):
                                    ui.button("Đăng nhập", icon="login", on_click=login).props("unelevated no-caps")
                                    ui.button("Đổi số", on_click=lambda: (setattr(S, "login_stage", "phone"), account_card.refresh())).props("flat no-caps")
                        account_card()

                    with card("q-pa-lg"):
                        section_header("Thư viện nhạc", "Các thư mục dùng để kiểm tra bài đã có (quét cả thư mục con)")

                        @ui.refreshable
                        def folders():
                            with ui.column().classes("w-full gap-2 q-mt-md"):
                                for i, d in enumerate(S.cfg.get("music_dirs") or []):
                                    ok = core.expand(d).exists()
                                    with ui.row().classes("folder-row w-full items-center no-wrap"):
                                        ui.icon("folder" if ok else "folder_off", color="amber-7" if ok else "negative")
                                        ui.label(d).classes("mono ellipsis").style("flex:1")
                                        if not ok:
                                            ui.badge("không tồn tại", color="negative").props("outline")
                                        ui.button(icon="close", on_click=lambda i=i: remove_dir(i)).props("flat round dense size=sm")
                                ui.button("Thêm thư mục", icon="create_new_folder", on_click=lambda: add_dir()).props("outline no-caps")
                                ui.label("Thư mục lưu bài tải về").classes("text-weight-medium q-mt-md")
                                with ui.row().classes("folder-row w-full items-center no-wrap"):
                                    ui.icon("download", color="primary")
                                    ui.label(str(S.cfg.get("download_dir"))).classes("mono ellipsis").style("flex:1")
                                    ui.button("Đổi", on_click=lambda: change_dl()).props("flat no-caps dense")
                        folders()

                    with card("q-pa-lg"):
                        section_header("Độ nhạy so khớp", "Điểm giống nhau giữa tên bài trên Telegram và file trong máy (0–100)")
                        ui.label("“Đã có” khi điểm ≥").classes("q-mt-md")
                        s_have = ui.slider(min=80, max=100, step=1, value=S.cfg.get("have_threshold", 90)).props("label-always color=positive")
                        ui.label("“Cần xem lại” khi điểm ≥").classes("q-mt-sm")
                        s_maybe = ui.slider(min=50, max=90, step=1, value=S.cfg.get("maybe_threshold", 70)).props("label-always color=orange")
                        s_tags = ui.switch("Đọc tag (nghệ sĩ, tên bài, thời lượng) trong file nhạc",
                                           value=bool(S.cfg.get("read_tags", True))).classes("q-mt-sm")
                        ui.label("Bài khác phiên bản (Extended/Radio), khác bản remix, hay chỉ trùng tên ngắn luôn được "
                                 "đưa vào “Cần xem lại” để bạn tự quyết.").classes("h-sub q-mt-sm")

                    with card("q-pa-lg"):
                        section_header("Tốc độ", "Tăng số luồng giúp nhanh hơn; quá cao có thể bị Telegram giới hạn tạm thời")
                        ui.label("Số bài tải cùng lúc").classes("q-mt-md")
                        s_pdl = ui.slider(min=1, max=6, step=1, value=S.cfg.get("parallel_downloads", 3)).props("label-always markers")
                        ui.label("Số topic quét cùng lúc").classes("q-mt-sm")
                        s_psc = ui.slider(min=1, max=6, step=1, value=S.cfg.get("parallel_scans", 3)).props("label-always markers")

        def save_prefs(_=None):
            S.cfg.update(have_threshold=int(s_have.value), maybe_threshold=int(min(s_maybe.value, s_have.value - 1)),
                         read_tags=bool(s_tags.value), parallel_downloads=int(s_pdl.value),
                         parallel_scans=int(s_psc.value))
            core.save_raw_config(S.cfg)
        for el in (s_have, s_maybe, s_pdl, s_psc):
            el.on("change", save_prefs)       # chỉ lưu khi thả chuột
        s_tags.on_value_change(save_prefs)

        def remove_dir(i: int):
            dirs = list(S.cfg.get("music_dirs") or [])
            dirs.pop(i)
            S.cfg["music_dirs"] = dirs
            core.save_raw_config(S.cfg)
            folders.refresh()
            dashboard.refresh()

        async def add_dir():
            p = await pick_folder((S.cfg.get("music_dirs") or ["~"])[-1])
            if p and p not in (S.cfg.get("music_dirs") or []):
                S.cfg["music_dirs"] = [*(S.cfg.get("music_dirs") or []), p]
                core.save_raw_config(S.cfg)
                folders.refresh()
                dashboard.refresh()
                ui.notify("Đã thêm thư mục – bấm “Đối chiếu lại” để cập nhật.", type="positive")

        async def change_dl():
            p = await pick_folder(str(S.cfg.get("download_dir")))
            if p:
                S.cfg["download_dir"] = p
                core.save_raw_config(S.cfg)
                folders.refresh()

        async def confirm_logout():
            with ui.dialog() as dlg, card("q-pa-lg"):
                ui.label("Đăng xuất khỏi Telegram?").classes("sec-title")
                ui.label("Phiên đăng nhập của app sẽ bị huỷ. Lần sau cần đăng nhập lại bằng mã.").classes("muted")
                with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                    ui.button("Huỷ", on_click=lambda: dlg.submit(False)).props("flat no-caps")
                    ui.button("Đăng xuất", on_click=lambda: dlg.submit(True)).props("unelevated no-caps color=negative")
            ok = await dlg
            dlg.delete()
            if ok:
                await guard("Đăng xuất", lambda: tg().log_out())
                await refresh_account()

    # ---------------------------------------------------------------- task panel (nổi góc phải dưới)
    with ui.page_sticky(position="bottom-right", x_offset=20, y_offset=20):
        with card("taskpanel q-pa-md") as panel:
            with ui.row().classes("w-full items-center no-wrap gap-2"):
                t_spin = ui.spinner(size="20px")
                t_icon = ui.icon("check_circle", color="positive", size="20px")
                t_title = ui.label("").classes("text-weight-medium ellipsis").style("flex:1")
                t_stop = ui.button("Dừng", on_click=lambda: S.cancel and S.cancel.set()).props("flat dense no-caps color=negative")
                ui.button(icon="close", on_click=lambda: setattr(S, "finished_at", 1)).props("flat round dense size=sm")
            t_bar = ui.linear_progress(value=0, show_value=False).props("rounded size=6px").classes("q-mt-sm")
            t_msg = ui.label("").classes("muted ellipsis q-mt-xs").style("font-size:12px")
            with ui.expansion("Nhật ký").props("dense").classes("w-full").style("font-size:12px"):
                t_log = ui.log(max_lines=400).classes("mono w-full").style("height:180px")
    panel.set_visibility(False)

    # ---------------------------------------------------------------- thanh nghe thử (cố định ở đáy)
    with ui.element("div").classes("ms-bar"):
        with ui.row().classes("w-full items-center no-wrap gap-4"):
            with ui.element("div").classes("ms-cover"):
                ui.icon("music_note", size="24px")
            with ui.column().classes("gap-0").style("min-width:0;flex:1 1 260px;max-width:360px"):
                p_title = ui.label("").classes("text-weight-bold ellipsis").style("max-width:100%")
                p_sub = ui.label("").classes("muted ellipsis").style("font-size:12px;max-width:100%")
                p_reason = ui.label("").classes("text-orange-9 ellipsis").style("font-size:12px;max-width:100%")
                ui.html('<span class="ms-err"></span>', sanitize=False)
            with ui.column().classes("gap-1").style("flex:2 1 320px;min-width:240px"):
                ui.html('<audio class="ms-audio" controls preload="auto"></audio>', sanitize=False).classes("w-full")
                with ui.row().classes("items-center gap-1 no-wrap"):
                    ui.label("Nhảy tới").classes("muted").style("font-size:12px")
                    for pct in (25, 50, 75):
                        ui.button(f"{pct}%", on_click=lambda p=pct: ui.run_javascript(f"msPlayer.seekPct({p / 100})")
                                  ).props("flat dense no-caps size=sm")
                    ui.button("+30s", on_click=lambda: ui.run_javascript("msPlayer.skip(30)")).props("flat dense no-caps size=sm")
                    p_src = ui.toggle({"tg": "Bản Telegram", "local": "Bản trong máy"}, value="tg").props(
                        "dense no-caps rounded size=sm toggle-color=accent unelevated").classes("q-ml-sm")
            with ui.row().classes("items-center gap-1 no-wrap"):
                ui.button(icon="skip_previous", on_click=lambda: step(-1)).props("flat round").tooltip("Bài trước (P)")
                ui.button(icon="skip_next", on_click=lambda: step(1)).props("flat round").tooltip("Bài tiếp (N)")
                p_dl = ui.button("Tải", icon="download", on_click=lambda: p_download()).props("unelevated no-caps")
                p_dl.tooltip("Tải bài này và chuyển sang bài tiếp (D)")
                ui.button("Bỏ qua", icon="do_not_disturb_on", on_click=lambda: p_ignore()).props(
                    "outline no-caps color=grey-7").tooltip("Đánh dấu đã có / bỏ qua và chuyển bài (X)")
                ui.button(icon="close", on_click=lambda: close_player()).props("flat round").tooltip("Đóng (Esc)")
        ui.html('<div class="ms-keys q-mt-xs"><kbd>Space</kbd> phát/dừng · <kbd>←</kbd><kbd>→</kbd> tua 15s · '
                '<kbd>1</kbd>–<kbd>9</kbd> nhảy 10–90% · <kbd>N</kbd>/<kbd>P</kbd> bài tiếp/trước · '
                '<kbd>D</kbd> tải · <kbd>X</kbd> bỏ qua · <kbd>C</kbd> đổi bản Telegram/trong máy · <kbd>Esc</kbd> đóng</div>',
                sanitize=False)

    def mark_playing():
        table.update_rows([{**r, "playing": r["id"] == player["id"]} for r in table.rows], clear_selection=False)

    def play(row_id: str, src: str = "tg", keep: bool = False):
        r = S.by_id.get(row_id)
        if r is None:
            return
        d = r.as_dict()
        if src == "local" and not d["local_path"]:
            src = "tg"
        if src == "tg" and not (S.tg and S.tg.me):
            ui.notify("Cần đăng nhập Telegram để nghe thử.", type="warning")
            return
        url = (f"/preview/tg/{d['chat_id']}/{d['msg_id']}" if src == "tg"
               else "/preview/local?path=" + quote(d["local_path"]))
        player.update(id=row_id, src=src)
        ui.run_javascript(f"msPlayer.load({json.dumps(url)}, {'true' if keep else 'false'})")
        if src == "tg":
            p_title.text = d["file_name"]
            p_sub.text = f"{d['artist']} · Telegram: {d['group']} / {d['topic']} · {d['ext'].upper()} · {d['size_mb']} MB"
            ext = Path(d["file_name"]).suffix.lower()
        else:
            p_title.text = d["local"]
            p_sub.text = f"File trong máy · {d['local_path']}"
            ext = Path(d["local_path"]).suffix.lower()
        p_reason.text = ((("Trình duyệt thường không phát được " + ext.upper().lstrip(".") + ". ") if ext in core.BROWSER_UNPLAYABLE else "")
                         + (f"So với: {d['local']} – {d['reason'] or 'giống ' + str(d['score']) + '%'}" if d["local"] and src == "tg" else ""))
        player["sync"] = True
        p_src.set_visibility(bool(d["local_path"]))
        p_src.set_value(src)
        player["sync"] = False
        p_dl.set_enabled(d.get("dl") not in ("done", "downloading", "queued"))
        mark_playing()

    def on_src_change(e):
        if not player["sync"] and player["id"]:
            play(player["id"], e.value, keep=True)      # giữ nguyên vị trí đang nghe để so sánh A/B
    p_src.on_value_change(on_src_change)

    def neighbour(delta: int) -> str | None:
        ids = [r["id"] for r in table.rows]
        if not ids:
            return None
        i = ids.index(player["id"]) if player["id"] in ids else (-1 if delta > 0 else len(ids))
        j = i + delta
        return ids[j] if 0 <= j < len(ids) else None

    def step(delta: int):
        nxt = neighbour(delta)
        if nxt:
            play(nxt)
        else:
            ui.notify("Đã hết danh sách.", type="info")

    def close_player():
        player.update(id=None)
        ui.run_javascript("msPlayer.show(false)")
        mark_playing()

    async def p_download():
        cur, nxt = player["id"], neighbour(1)
        if not cur:
            return
        if nxt:
            play(nxt)                                    # nghe tiếp bài sau trong lúc tải
        await do_download([{"id": cur}])

    def p_ignore():
        cur, nxt = player["id"], neighbour(1)
        if not cur:
            return
        do_ignore([{"id": cur}])
        if nxt:
            play(nxt)
        else:
            close_player()

    async def on_key(e):
        k = e.args[0] if isinstance(e.args, list) else e.args
        if k == "next":
            step(1)
        elif k == "prev":
            step(-1)
        elif k == "download":
            await p_download()
        elif k == "ignore":
            p_ignore()
        elif k == "compare" and player["id"] and p_src.visible:
            play(player["id"], "local" if player["src"] == "tg" else "tg", keep=True)
        elif k == "close":
            close_player()
    ui.on("ms_key", on_key)

    # ---------------------------------------------------------------- tick: đồng bộ state -> UI
    def tick():
        for seq, line in S.logs:
            if seq > seen["log"]:
                t_log.push(line)
        seen["log"] = S.log_seq
        show = S.busy or (time.time() - S.finished_at < 6)
        panel.set_visibility(show)
        if show:
            t_title.text = S.task + ("" if S.busy else (" – xong" if S.ok else " – lỗi"))
            t_msg.text = S.msg
            t_bar.value = S.frac or 0
            t_spin.set_visibility(S.busy)
            t_icon.set_visibility(not S.busy)
            t_icon.props(f"name={'check_circle' if S.ok else 'error'} color={'positive' if S.ok else 'negative'}")
            t_stop.set_visibility(S.busy and S.cancel is not None)
        if seen["ver"] != S.ver:
            seen["ver"] = S.ver
            arts = sorted({r.artist for r in S.results}, key=str.lower)
            cur = f_artist.value if f_artist.value in arts else "(Tất cả nghệ sĩ)"
            f_artist.set_options(["(Tất cả nghệ sĩ)", *arts], value=cur)
            render_table()
            dashboard.refresh()
            c = stats()
            nav_badges["tracks"].text = str(c["new"]) if c["new"] else ""
            nav_badges["tracks"].set_visibility(bool(c["new"]))
            ls, lc = core.meta_get(S.con, "last_scan"), core.meta_get(S.con, "last_compare")
            last_lbl.text = (f"Quét Telegram: {ls or 'chưa'}\nĐối chiếu: {lc or 'chưa'}")
        if seen["dl_ver"] != S.dl_ver:
            seen["dl_ver"] = S.dl_ver
            table.update_rows([{**r, "dl": S.by_id[r["id"]].as_dict().get("dl", "")} if r["id"] in S.by_id else r
                               for r in table.rows], clear_selection=False)

    ui.timer(0.4, tick)
    go("home" if core.credentials_ok(S.cfg) else "settings")
    await refresh_account()
    if not S.results and core.counts(S.con).get("new", 0):
        ui.timer(0.3, do_compare, once=True)   # tự đối chiếu khi mở app (nhanh nhờ cache)


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(title="Music Sync", host="127.0.0.1", port=int(os.environ.get("PORT", 8777)), reload=False,
           show=not os.environ.get("NO_BROWSER"), favicon="🎧", language="vi", dark=None)
