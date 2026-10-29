#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大肥鱼看过什么 · 打分台（独立小服务，只监听 127.0.0.1）

    GET  /watch?t=<token>      在线版（能打分，实时保存）
    GET  /snapshot?t=<token>   自包含快照（封面内嵌 base64，用于 QQ 发文件）
    POST /rate                 打分：{"bvid":"BV...","stars":1-5,"note":"..."}
    GET  /cover/<bvid>.jpg     封面缓存
    GET  /health               健康检查

数据来源（只读）：/opt/astrbot/data/bili_whale/state.json
写出的东西：    /opt/astrbot/data/bili_whale/ratings.json
                /opt/astrbot/data/bili_whale/covers/
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA = Path(os.environ.get("WATCHUI_DATA", "/opt/astrbot/data/bili_whale"))
STATE = DATA / "state.json"
RATINGS = DATA / "ratings.json"
COVERS = DATA / "covers"
COVER_INDEX = COVERS / "index.json"
DIARY_FILE = Path(os.environ.get("WATCHUI_DIARY", "/opt/astrbot/data/whale_journal/entries.jsonl"))
CRED_FILE = Path(os.environ.get("WATCHUI_CRED", "/opt/bili-ai/credentials.json"))
TOKEN_FILE = Path(os.environ.get("WATCHUI_TOKEN_FILE", "/opt/bili-watch-ui/token.txt"))
PUBLIC_URL_FILE = Path(os.environ.get("WATCHUI_PUBLIC_URL_FILE", "/opt/bili-watch-ui/public_url.txt"))
PORT = int(os.environ.get("WATCHUI_PORT", "18900"))
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

try:
    from diary_view import load_entries as diary_entries, render_diary
except Exception:  # 单文件跑也不会崩
    try:
        from .diary_view import load_entries as diary_entries, render_diary
    except Exception:
        diary_entries = None
        render_diary = None

_lock = threading.Lock()
_cover_lock = threading.Lock()


# ---------------------------------------------------------------- 基础读写
def load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


_tok_cache: str = ""
_tok_lock = threading.Lock()


def public_url() -> str:
    """cloudflared 快通道的公网地址（手机直接开这个就行）。"""
    try:
        u = PUBLIC_URL_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    return u if u.startswith("http") else ""


def token() -> str:
    global _tok_cache
    if _tok_cache:
        return _tok_cache
    with _tok_lock:
        if _tok_cache:
            return _tok_cache
        try:
            t = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if t:
                _tok_cache = t
                return t
        except Exception:
            pass
        t = secrets.token_urlsafe(12)
        try:
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_FILE.write_text(t + "\n", encoding="utf-8")
        except Exception:
            pass
        _tok_cache = t
        return t


def sessdata() -> str:
    try:
        c = load_json(CRED_FILE, {})
        return str(c.get("sessdata") or c.get("SESSDATA") or "")
    except Exception:
        return ""



_FAV_ON = "★ 已收藏"
_FAV_OFF = "☆ 收藏"

WK = "一二三四五六日"


def _day_label(ts: int) -> str:
    if not ts:
        return "日期未知"
    lt = time.localtime(ts)
    return time.strftime("%m-%d", lt) + " 星期" + WK[lt.tm_wday]


# ---------------------------------------------------------------- 数据装配
def load_items(limit: int = 300) -> list[dict]:
    state = load_json(STATE, {})
    seen = state.get("seen") or {}
    fb = state.get("feedback") or {}
    rt = (load_json(RATINGS, {}) or {}).get("items") or {}

    rows: list[dict] = []
    for bvid, v in seen.items():
        if not isinstance(v, dict):
            continue
        ts = int(v.get("ts") or 0)
        rec = rt.get(bvid) if isinstance(rt.get(bvid), dict) else None
        if not rec:
            f = fb.get(bvid) if isinstance(fb.get(bvid), dict) else {}
            rec = {"stars": int(f.get("stars") or 0), "note": str(f.get("note") or ""), "fav": f.get("fav"), "ts": f.get("ts")}
        rows.append(
            {
                "bvid": bvid,
                "title": str(v.get("title") or bvid),
                "ts": ts,
                "date": time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "",
                "score": int(v.get("score") or 0),
                "shared": bool(v.get("shared")),
                "keyword": str(v.get("keyword") or ""),
                "up": str(v.get("up") or ""),
                "cover": str(v.get("cover") or ""),
                "stars": int(rec.get("stars") or 0),
                "note": str(rec.get("note") or ""),
                "fav": bool(rec.get("fav")),
                "day": time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "",
                "day_label": _day_label(ts),
                "hm": time.strftime("%H:%M", time.localtime(ts)) if ts else "",
            }
        )
    rows.sort(key=lambda r: r["ts"], reverse=True)
    return rows[:limit]


def save_rate(bvid: str, stars: int, note: str, fav=None) -> dict:
    stars = max(0, min(5, int(stars or 0)))
    note = (note or "").strip()[:200]
    with _lock:
        obj = load_json(RATINGS, {})
        if not isinstance(obj, dict) or "items" not in obj:
            obj = {"version": 1, "items": {}}
        items = obj.setdefault("items", {})
        cur = items.get(bvid) if isinstance(items.get(bvid), dict) else {}
        fv = bool(cur.get("fav")) if fav is None else bool(fav)
        if stars <= 0 and not note and not fv:
            items.pop(bvid, None)
        else:
            items[bvid] = {
                "stars": stars,
                "note": note,
                "fav": fv,
                "ts": time.time(),
                "at": time.strftime("%Y-%m-%d %H:%M"),
            }
        obj["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_json(RATINGS, obj)
    return {"ok": True, "bvid": bvid, "stars": stars}


# ---------------------------------------------------------------- 封面
_cover_memo: dict[str, str] = {}  # bvid -> 本地文件名 or ""


def _http_get(url: str, referer: str | None = None, timeout: float = 8.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if referer:
        req.add_header("Referer", referer)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _cover_url_from_api(bvid: str) -> str:
    url = f"https://api.bilibili.com/x/web-interface/view?bvid={urllib.parse.quote(bvid)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
    sd = sessdata()
    if sd:
        req.add_header("Cookie", f"SESSDATA={sd}")
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return str(((data.get("data") or {}).get("pic")) or "")
    except Exception:
        return ""


def cover_file(bvid: str, src_url: str = "", max_w: int = 480) -> str:
    """确保封面在本地，返回文件名；失败返回空串。"""
    if bvid in _cover_memo:
        return _cover_memo[bvid]
    COVERS.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9]", "", bvid) + ".jpg"
    path = COVERS / name
    if path.exists() and path.stat().st_size > 800:
        _cover_memo[bvid] = name
        return name
    with _cover_lock:
        if path.exists() and path.stat().st_size > 800:
            _cover_memo[bvid] = name
            return name
        url = src_url.strip()
        if not url:
            idx = load_json(COVER_INDEX, {}) or {}
            rec = idx.get(bvid) if isinstance(idx.get(bvid), dict) else None
            url = str((rec or {}).get("url") or "")
        if not url:
            # 机房 IP 直连 B站 view 接口会 412，封面统一由 backfill_covers.py 灌进 index.json
            _cover_memo[bvid] = ""
            return ""
        if url.startswith("//"):
            url = "https:" + url
        try:
            raw = _http_get(url, referer="https://www.bilibili.com/")
        except Exception:
            _cover_memo[bvid] = ""
            return ""
        try:
            from PIL import Image  # type: ignore

            im = Image.open(io.BytesIO(raw)).convert("RGB")
            if im.width > max_w:
                im = im.resize((max_w, int(im.height * max_w / im.width)), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=76, optimize=True)
            path.write_bytes(buf.getvalue())
        except Exception:
            if len(raw) < 3_000_000:
                path.write_bytes(raw)
            else:
                _cover_memo[bvid] = ""
                return ""
        _cover_memo[bvid] = name
        return name


def warm_covers(rows: list[dict]) -> None:
    """后台把缺的封面补齐，别让页面等。"""
    def _run() -> None:
        for r in rows:
            try:
                if not _cover_memo.get(r["bvid"]):
                    cover_file(r["bvid"], r.get("cover") or "")
            except Exception:
                continue

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------- 页面
def _esc(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render(items: list[dict], *, snapshot: bool, tk: str) -> str:
    total = len(items)
    rated = sum(1 for i in items if i["stars"] > 0)
    favs = sum(1 for i in items if i.get("fav") or i["stars"] >= 5)
    shared_n = sum(1 for i in items if i["shared"])
    avg = (sum(i["stars"] for i in items if i["stars"] > 0) / rated) if rated else 0.0

    # 按天分组（保持原有顺序：新的在前）
    groups: list[list] = []
    for it in items:
        day = it.get("day") or "未知日期"
        if not groups or groups[-1][0] != day:
            groups.append([day, it.get("day_label") or day, []])
        groups[-1][2].append(it)

    idx = 0
    blocks: list[str] = []
    for day, label, rows in groups:
        cards: list[str] = []
        for it in rows:
            idx += 1
            if snapshot:
                name = cover_file(it["bvid"], it.get("cover") or "", max_w=360)
                if name:
                    try:
                        b64 = base64.b64encode((COVERS / name).read_bytes()).decode()
                        img = f'<img class="cv" src="data:image/jpeg;base64,{b64}" alt="">'
                    except Exception:
                        img = '<div class="cv ph">没有封面</div>'
                else:
                    img = '<div class="cv ph">没有封面</div>'
            else:
                name = cover_file(it["bvid"], it.get("cover") or "")
                img = f'<img class="cv" loading="lazy" src="/cover/{name}" alt="">' if name else '<div class="cv ph">没有封面</div>'

            stars = "".join(
                f'<span class="st{" on" if s <= it["stars"] else ""}" data-s="{s}">★</span>' for s in range(1, 6)
            )
            chips = [f'<span class="chip">{_esc(it.get("hm") or "")}</span>']
            if it["score"]:
                chips.append(f'<span class="chip">她给 {it["score"]} 分</span>')
            if it["shared"]:
                chips.append('<span class="chip hot">推给我过</span>')
            if it["keyword"]:
                chips.append(f'<span class="chip tag" title="{_esc(it["keyword"])}">#{_esc(it["keyword"][:22])}</span>')
            chips.append(f'<span class="chip mono">{_esc(it["bvid"])}</span>')

            is_fav = bool(it.get("fav"))
            if snapshot:
                act = (
                    f'<div class="rate"><div class="stars ro">{stars}</div><span class="tip">你给的星</span></div>'
                    + (f'<div class="qnote">你写的：{_esc(it["note"])}</div>' if it.get("note") else "")
                    + '<div class="hint">手机上想打分：跟她说「#%d 5星 这种多推」</div>' % idx
                )
            else:
                act = (
                    f'<div class="rate"><div class="stars" role="radiogroup">{stars}</div><span class="tip">点星星打分</span>'
                    f'<button class="favbtn{" on" if is_fav else ""}" title="收藏这条">{_FAV_ON if is_fav else _FAV_OFF}</button></div>'
                    f'<textarea class="note" rows="2" placeholder="想对她说点什么？比如「这种多推」「别再刷这个了」">{_esc(it["note"])}</textarea>'
                    f'<div class="foot"><span class="stat">待保存</span></div>'
                )
            blink = (
                f'<a class="blink" href="https://www.bilibili.com/video/{_esc(it["bvid"])}" '
                f'target="_blank" rel="noopener">▶ 去B站打开</a>'
            )
            cards.append(
                f"""
<article class="card" data-bvid="{_esc(it['bvid'])}" data-stars="{it['stars']}" data-fav="{1 if is_fav else 0}"
         data-shared="{1 if it['shared'] else 0}" data-q="{_esc((it['title'] + ' ' + it['keyword'] + ' ' + it['note']).lower())}">
  <div class="thumb">{img}<span class="idx">#{idx}</span></div>
  <div class="body">
    <h3 title="{_esc(it['title'])}">{_esc(it['title'])}</h3>
    <div class="chips">{''.join(chips)}</div>
    {act}
    {blink}
  </div>
</article>"""
            )
        blocks.append(
            f'<section class="dayblock" id="day-{_esc(day)}" data-day="{_esc(day)}">'
            f'<h4 class="dayh">{_esc(label)}<span>· {len(rows)} 条</span></h4>'
            f'<div class="grid">{"".join(cards)}</div></section>'
        )

    toc = "".join(
        f'<a href="#day-{_esc(g[0])}" data-day="{_esc(g[0])}">{_esc(g[1])}<span>{len(g[2])}</span></a>'
        for g in groups
    )

    api = "" if not snapshot else "http://127.0.0.1:%d" % PORT
    pub = "" if snapshot else public_url()
    phone_bar = ""
    if pub:
        link = f"{pub.rstrip('/')}/watch?t={tk}"
        phone_bar = (
            '<div class="pub"><span>手机上（同一链接，直接打卡分）</span>'
            f'<a href="{link}" target="_blank" rel="noopener">{link}</a></div>'
        )
    head_extra = (
        '<p class="lead">点星星、写两句、点收藏，都能直接存回服务器；电脑上双击 看观看历史.bat 也是这个页面。</p>'
        + phone_bar
        if not snapshot
        else '<p class="lead">这是离线快照：能看，存不了。要打分就打开发你的在线版链接。</p>'
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>大肥鱼看过什么</title>
<style>
:root{{--bg:#0b0f19;--card:#151b2b;--card2:#1b2334;--line:#26314a;--tx:#e8eefc;--dim:#8b9ab5;--acc:#5ee1c1;--acc2:#7aa2ff;--warn:#ffcf6b;--bad:#ff7a8a;--pink:#ff8fb1}}
*{{box-sizing:border-box}}
html{{overflow-x:hidden}}
body{{margin:0;max-width:100vw;overflow-x:hidden;background:radial-gradient(1200px 600px at 15% -10%,#16233d 0%,transparent 60%),radial-gradient(900px 500px at 90% 0%,#1d2a3f 0%,transparent 55%),var(--bg);color:var(--tx);font:15px/1.6 "PingFang SC","Microsoft YaHei",system-ui,sans-serif}}
.wrap{{max-width:1180px;margin:0 auto;padding:28px 18px 80px}}
header h1{{margin:0 0 6px;font-size:30px;letter-spacing:.5px}}
header h1 span{{background:linear-gradient(90deg,var(--acc),var(--acc2));-webkit-background-clip:text;background-clip:text;color:transparent}}
.pnav{{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}}
.pnav a{{text-decoration:none;font-size:13px;color:var(--dim);background:var(--card);border:1px solid var(--line);border-radius:999px;padding:6px 14px}}
.pnav a.pnav-on{{color:#08131a;background:linear-gradient(90deg,var(--acc),var(--acc2));border-color:transparent;font-weight:700}}
.lead{{color:var(--dim);margin:2px 0 14px;font-size:13.5px}}
.stats{{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0 16px}}
.kpi{{background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--line);border-radius:14px;padding:10px 14px;min-width:104px}}
.kpi b{{display:block;font-size:22px;line-height:1.2}}
.kpi i{{font-style:normal;color:var(--dim);font-size:12px}}
.tools{{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:0 0 6px}}
input.search{{flex:1 1 260px;background:var(--card);border:1px solid var(--line);color:var(--tx);border-radius:12px;padding:11px 14px;font-size:14px;outline:none}}
input.search:focus{{border-color:var(--acc2)}}
.tabs{{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 18px}}
.tabs button{{background:var(--card);border:1px solid var(--line);color:var(--dim);border-radius:999px;padding:8px 16px;font-size:13px;cursor:pointer}}
.tabs button.on{{color:#08131a;background:linear-gradient(90deg,var(--acc),var(--acc2));border-color:transparent;font-weight:700}}
.tabs button.fav.on{{background:linear-gradient(90deg,var(--warn),var(--pink))}}
.layout{{display:flex;gap:22px;align-items:flex-start}}
main{{flex:1 1 auto;min-width:0}}
.toc{{position:sticky;top:16px;flex:0 0 186px;max-height:calc(100vh - 32px);overflow:auto;
  background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--line);border-radius:14px;padding:10px}}
.toc .tt{{font-size:12px;color:var(--dim);padding:2px 8px 8px;letter-spacing:.5px}}
.toc a{{display:flex;justify-content:space-between;gap:8px;padding:7px 9px;border-radius:9px;
  color:#cfe0ff;text-decoration:none;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.toc a:hover{{background:#1d2740}}
.toc a.on{{background:linear-gradient(90deg,rgba(94,225,193,.2),rgba(122,162,255,.16));color:#fff}}
.toc a span{{color:var(--dim);font-size:12px;flex:0 0 auto}}
.blink{{display:inline-flex;align-items:center;gap:6px;margin-top:9px;font-size:12.5px;color:var(--acc2);
  text-decoration:none;border:1px solid var(--line);border-radius:9px;padding:5px 10px;background:#121a2a}}
.blink:hover{{border-color:var(--acc2);color:#fff;background:#16213a}}
@media(max-width:860px){{
  .layout{{display:block}}
  .toc{{position:static;display:flex;gap:8px;overflow-x:auto;max-height:none;margin:0 0 12px;padding:8px}}
  .toc .tt{{display:none}}
  .toc a{{flex:0 0 auto;background:#121a2a;border:1px solid var(--line);padding:6px 11px}}
}}
.dayblock{{margin:0 0 26px;scroll-margin-top:14px}}
.dayh{{margin:8px 0 12px;font-size:15px;color:#cfe0ff;font-weight:700;display:flex;align-items:center;gap:8px}}
.dayh span{{font-size:12px;color:#5f6d88;font-weight:400}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px}}
.card{{background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--line);border-radius:18px;overflow:hidden;display:flex;flex-direction:column;min-width:0;transition:.18s transform,.18s border-color}}
.card:hover{{transform:translateY(-2px);border-color:#31406a}}
.card[data-fav="1"]{{border-color:rgba(255,207,107,.55);box-shadow:0 0 0 1px rgba(255,207,107,.18) inset}}
.thumb{{position:relative;aspect-ratio:16/9;background:#0e1421;overflow:hidden}}
.cv{{width:100%;height:100%;object-fit:cover;display:block}}
.cv.ph{{display:flex;align-items:center;justify-content:center;color:#3d4a63;font-size:13px;height:100%}}
.idx{{position:absolute;left:10px;top:10px;background:rgba(8,12,20,.72);border:1px solid rgba(255,255,255,.14);border-radius:8px;padding:2px 8px;font-size:12px;color:#cfe0ff}}
.body{{padding:14px 14px 12px;display:flex;flex-direction:column;gap:9px}}
.body h3{{margin:0;font-size:15px;line-height:1.45;max-height:3em;overflow:hidden}}
.chips{{display:flex;gap:6px;flex-wrap:wrap;min-width:0}}
.chip{{background:#101828;border:1px solid var(--line);border-radius:999px;padding:2px 9px;font-size:11.5px;color:var(--dim);max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.chip.hot{{color:#08131a;background:var(--acc);border-color:transparent;font-weight:600}}
.chip.tag{{color:#9fd0ff}}
.chip.mono{{font-family:ui-monospace,Consolas,monospace;letter-spacing:.3px}}
.rate{{display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
.stars{{display:flex;gap:2px;cursor:pointer;user-select:none}}
.stars .st{{font-size:22px;color:#33405c;transition:.12s transform,.12s color}}
.stars .st.on{{color:var(--warn);text-shadow:0 0 14px rgba(255,207,107,.45)}}
.pub{{margin:14px 0 0;padding:12px 16px;border:1px solid rgba(94,225,193,.35);border-radius:14px;background:rgba(94,225,193,.08);font-size:13.5px;line-height:1.6}}
.pub span{{display:block;color:#8b9ab5;margin-bottom:4px}}
.pub a{{color:#5ee1c1;word-break:break-all;text-decoration:none}}
.stars.ro{{cursor:default}}
.stars:hover .st{{transform:scale(1.06)}}
.tip{{font-size:11.5px;color:#5f6d88}}
.favbtn{{margin-left:auto;background:#101828;border:1px solid var(--line);color:var(--dim);border-radius:999px;padding:5px 12px;font-size:12px;cursor:pointer;transition:.15s}}
.favbtn:hover{{border-color:var(--warn);color:var(--warn)}}
.favbtn.on{{background:linear-gradient(90deg,var(--warn),var(--pink));border-color:transparent;color:#221600;font-weight:700}}
.hint{{margin-top:6px;padding:8px 10px;border-radius:10px;background:rgba(94,225,193,.10);border:1px dashed rgba(94,225,193,.45);color:var(--acc);font-size:13px}}
.qnote{{margin-top:8px;color:var(--dim);font-size:13px}}
textarea.note{{width:100%;resize:vertical;background:#0f1626;border:1px solid var(--line);border-radius:12px;color:var(--tx);padding:9px 11px;font:13.5px/1.5 inherit;outline:none}}
textarea.note:focus{{border-color:var(--acc2)}}
.foot{{display:flex;justify-content:space-between;align-items:center;font-size:12px;color:#5f6d88}}
.stat.saved{{color:var(--acc)}}
.stat.err{{color:var(--bad)}}
.toast{{position:fixed;left:50%;bottom:26px;transform:translate(-50%,20px);background:#12203a;border:1px solid var(--acc);color:var(--tx);padding:10px 18px;border-radius:12px;opacity:0;transition:.25s;pointer-events:none;font-size:13.5px;z-index:9}}
.toast.on{{opacity:1;transform:translate(-50%,0)}}
.empty{{color:#5f6d88;padding:28px 4px;font-size:14px}}
footer{{margin-top:34px;color:#4d5a74;font-size:12px;text-align:center}}
@media(max-width:560px){{
  .wrap{{padding:18px 12px 64px}}
  header h1{{font-size:24px}}
  .stats{{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}}
  .kpi{{min-width:0;padding:8px 10px}}
  .kpi b{{font-size:19px}}
  .grid{{grid-template-columns:1fr;gap:14px}}
  .tools{{gap:8px}}
  input.search{{flex:1 1 auto;min-width:0}}
  .tabs button{{padding:7px 13px;font-size:12.5px}}
}}
</style></head>
<body><div class="wrap">
<header>
  <nav class="pnav"><a class="pnav-on" href="#">她看过什么</a><a href="/diary?t={tk}">她的日记本</a></nav>
  <h1>大肥鱼<span>看过什么</span></h1>
  {head_extra}
</header>
<div class="layout">
<nav class="toc" id="toc"><div class="tt">按天看</div>{toc}</nav>
<main>
<div class="stats">
  <div class="kpi"><b>{total}</b><i>看过的视频</i></div>
  <div class="kpi"><b>{rated}</b><i>已打分</i></div>
  <div class="kpi"><b>{favs}</b><i>收藏 / 五星</i></div>
  <div class="kpi"><b>{shared_n}</b><i>推给我的</i></div>
  <div class="kpi"><b>{avg:.1f}</b><i>平均星级</i></div>
  <div class="kpi"><b>{time.strftime("%m-%d %H:%M")}</b><i>生成时间</i></div>
</div>
<div class="tools">
  <input class="search" id="q" placeholder="搜标题 / 主题 / 你的评价……">
</div>
<div class="tabs" id="tabs">
  <button data-f="all" class="on">全部</button>
  <button data-f="shared">她推给我的</button>
  <button data-f="unshared">没推给我的</button>
  <button data-f="fav" class="fav">收藏</button>
  <button data-f="todo">待我评价</button>
  <button data-f="done">评过的</button>
</div>
<div id="feed">{''.join(blocks)}</div>
<div class="empty" id="empty" style="display:none">这下面没有内容。</div>
<footer>大肥鱼 · 观看历史打分台 · 数据存在服务器 state.json / ratings.json</footer>
</main>
</div>
</div>
<div class="toast" id="toast"></div>
<script>
const API = "{api}";
const TK = "{tk}";
const SNAP = {str(snapshot).lower()};
const feed = document.getElementById('feed');
const toast = document.getElementById('toast');
let tt = null;
function say(msg) {{
  toast.textContent = msg; toast.className = 'toast on';
  clearTimeout(tt); tt = setTimeout(() => toast.className = 'toast', 2200);
}}
function save(card) {{
  if (SNAP) return;
  const bvid = card.dataset.bvid;
  const stars = parseInt(card.dataset.stars || '0', 10);
  const fav = card.dataset.fav === '1';
  const ta = card.querySelector('.note');
  const note = ta ? ta.value.trim() : '';
  const stat = card.querySelector('.stat');
  if (stat) {{ stat.textContent = '保存中…'; stat.className = 'stat'; }}
  fetch(API + '/rate?t=' + encodeURIComponent(TK), {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{bvid, stars, note, fav}})
  }}).then(r => r.json()).then(j => {{
    if (j.ok) {{ if (stat) {{ stat.textContent = '已保存'; stat.className = 'stat saved'; }} }}
    else throw new Error(j.message || 'fail');
  }}).catch(e => {{
    if (stat) {{ stat.textContent = '保存失败'; stat.className = 'stat err'; }}
    say('保存失败：' + e.message);
  }});
}}
feed.addEventListener('click', e => {{
  const st = e.target.closest('.st');
  if (st && !st.parentElement.classList.contains('ro')) {{
    const card = st.closest('.card');
    const s = parseInt(st.dataset.s, 10);
    const cur = parseInt(card.dataset.stars || '0', 10);
    const val = (cur === s) ? 0 : s;
    card.dataset.stars = val;
    card.querySelectorAll('.st').forEach(x => x.classList.toggle('on', parseInt(x.dataset.s, 10) <= val));
    save(card);
    return;
  }}
  const fb = e.target.closest('.favbtn');
  if (fb) {{
    const card = fb.closest('.card');
    const on = card.dataset.fav !== '1';
    card.dataset.fav = on ? '1' : '0';
    fb.classList.toggle('on', on);
    fb.textContent = on ? '{_FAV_ON}' : '{_FAV_OFF}';
    save(card);
    filt();
  }}
}});
feed.addEventListener('blur', e => {{
  if (e.target.classList && e.target.classList.contains('note')) save(e.target.closest('.card'));
}}, true);
feed.addEventListener('keydown', e => {{
  if (e.target.classList && e.target.classList.contains('note') && e.key === 'Enter' && e.ctrlKey) save(e.target.closest('.card'));
}});
document.getElementById('q').addEventListener('input', filt);
document.getElementById('tabs').addEventListener('click', e => {{
  const b = e.target.closest('button'); if (!b) return;
  document.querySelectorAll('#tabs button').forEach(x => x.classList.remove('on'));
  b.classList.add('on'); filt();
}});
function pass(card, f) {{
  const s = parseInt(card.dataset.stars || '0', 10);
  const fav = card.dataset.fav === '1' || s >= 5;
  const shared = card.dataset.shared === '1';
  if (f === 'todo') return s === 0;
  if (f === 'done') return s > 0;
  if (f === 'fav') return fav;
  if (f === 'shared') return shared;
  if (f === 'unshared') return !shared;
  return true;
}}
function filt() {{
  const q = document.getElementById('q').value.trim().toLowerCase();
  const f = (document.querySelector('#tabs button.on') || {{}}).dataset?.f || 'all';
  let shown = 0;
  document.querySelectorAll('.dayblock').forEach(blk => {{
    let n = 0;
    blk.querySelectorAll('.card').forEach(c => {{
      const ok = (!q || (c.dataset.q || '').includes(q)) && pass(c, f);
      c.style.display = ok ? '' : 'none';
      if (ok) n++;
    }});
    blk.style.display = n ? '' : 'none';
    const cnt = blk.querySelector('.dayh span');
    if (cnt) cnt.textContent = '· ' + n + ' 条';
    shown += n;
  }});
  document.getElementById('empty').style.display = shown ? 'none' : '';
  syncToc();
}}

// 左侧「按天看」目录：点一下跳过去，滚到哪天就高亮哪天
function syncToc() {{
  const toc = document.getElementById('toc');
  if (!toc) return;
  toc.querySelectorAll('a').forEach(a => {{
    const blk = document.querySelector(a.getAttribute('href'));
    if (blk) a.style.display = (blk.style.display === 'none') ? 'none' : '';
  }});
}}
const _toc = document.getElementById('toc');
if (_toc) {{
  const links = [..._toc.querySelectorAll('a')];
  links.forEach(a => a.addEventListener('click', ev => {{
    ev.preventDefault();
    const blk = document.querySelector(a.getAttribute('href'));
    if (blk) blk.scrollIntoView({{behavior:'smooth', block:'start'}});
    links.forEach(x => x.classList.toggle('on', x === a));
  }}));
  const io = new IntersectionObserver(es => {{
    es.forEach(en => {{
      if (!en.isIntersecting) return;
      links.forEach(x => x.classList.toggle('on', x.getAttribute('href') === '#' + en.target.id));
    }});
  }}, {{rootMargin:'-15% 0px -70% 0px', threshold:0}});
  document.querySelectorAll('.dayblock').forEach(b => io.observe(b));
  syncToc();
}}
</script>
</body></html>"""


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "watchui/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音
        pass

    # -- 工具
    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text: str, code: int = 200):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query(self) -> dict:
        q = urllib.parse.urlparse(self.path).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}

    def _authed(self) -> bool:
        return self._query().get("t") == token()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            return self._json({"ok": True, "total": len(load_items()), "ts": time.time()})
        if path.startswith("/cover/"):
            name = os.path.basename(path[len("/cover/"):])
            f = COVERS / name
            if not f.exists():
                # 现场补一张（页面首屏请求可能早于后台预热）
                bvid = name.rsplit(".", 1)[0]
                got = cover_file(bvid, "")
                f = COVERS / got if got else f
            if f.exists():
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "max-age=86400")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            return self._json({"ok": False}, 404)
        if not self._authed():
            if path in ("/", "/watch"):
                # 只可能经 SSH 隧道到达（服务只监听 127.0.0.1），所以直接给个进门按钮
                tk = token()
                return self._html(
                    "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                    "<title>大肥鱼看过什么</title></head>"
                    "<body style=\"margin:0;background:#0b0f19;color:#e8eefc;font:15px/1.7 'PingFang SC','Microsoft YaHei',system-ui,sans-serif;"
                    "display:flex;align-items:center;justify-content:center;min-height:100vh\">"
                    "<div style='text-align:center;padding:28px'>"
                    "<h2 style='margin:0 0 10px'>大肥鱼看过什么</h2>"
                    "<p style='color:#8b9ab5;margin:0 0 22px'>隧道已经通了，点下面进打分台</p>"
                    f"<a href='/watch?t={tk}' style='display:inline-block;padding:14px 36px;border-radius:999px;"
                    "background:linear-gradient(90deg,#5ee1c1,#7aa2ff);color:#08131a;font-weight:700;text-decoration:none;margin:0 6px 10px'>进打分台</a>"
                    f"<a href='/diary?t={tk}' style='display:inline-block;padding:14px 36px;border-radius:999px;"
                    "background:#1b2334;border:1px solid #26314a;color:#e8eefc;font-weight:700;text-decoration:none;margin:0 6px 10px'>看她的日记本</a>"
                    "</div></body></html>"
                )
            return self._html("<h3>需要口令</h3>", 403)
        if path in ("/", "/watch"):
            rows = load_items()
            warm_covers(rows)
            return self._html(render(rows, snapshot=False, tk=token()))
        if path in ("/diary", "/diary/"):
            if diary_entries is None or render_diary is None:
                return self._json({"ok": False, "message": "diary_view 未加载"}, 500)
            rows = diary_entries(DIARY_FILE)
            return self._html(render_diary(rows, tk=token(), public=public_url()))
        if path == "/diary.json":
            if diary_entries is None:
                return self._json({"ok": False}, 500)
            return self._json({"ok": True, "entries": diary_entries(DIARY_FILE)})
        if path == "/snapshot":
            rows = load_items()
            return self._html(render(rows, snapshot=True, tk=token()))
        return self._json({"ok": False, "message": "not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            return self._json({"ok": False, "message": "坏请求"}, 400)
        if path == "/rate":
            if self._query().get("t") != token():
                return self._json({"ok": False, "message": "口令不对"}, 403)
            bvid = str(data.get("bvid") or "").strip()
            if not re.fullmatch(r"BV[0-9A-Za-z]{8,14}", bvid):
                return self._json({"ok": False, "message": "bvid 不合法"}, 400)
            try:
                stars = int(data.get("stars") or 0)
            except Exception:
                stars = 0
            fav = data.get("fav")
            return self._json(save_rate(bvid, stars, str(data.get("note") or ""), fav))
        return self._json({"ok": False, "message": "not found"}, 404)


def main() -> None:
    COVERS.mkdir(parents=True, exist_ok=True)
    try:
        rows = load_items()
        warm_covers(rows)
    except Exception:
        pass
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[watchui] listening 127.0.0.1:{PORT} data={DATA}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
