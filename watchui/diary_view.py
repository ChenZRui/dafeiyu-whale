"""日记本的在线页面：把 whale_journal 的 entries.jsonl 渲染成一个能看到、
能在电脑和手机上直接读的网页（和 /watch 共用同一个服务和口令）。"""

from __future__ import annotations

import html
import json
import time
from datetime import datetime
from pathlib import Path

WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

_PALETTE = [
    ("#f4b8c1", "#5b2b33"),
    ("#a8d8ea", "#1f4653"),
    ("#c3e88d", "#33461f"),
    ("#ffd6a5", "#5c3d16"),
    ("#bdb2ff", "#332a63"),
    ("#9ad5c8", "#194a44"),
    ("#f7a072", "#5c2a12"),
    ("#cbaacb", "#4a2b49"),
]


def _esc(s) -> str:
    return html.escape(str(s or ""))


def _mood_colors(mood: str) -> tuple[str, str]:
    if not mood:
        return "#e7e3dc", "#7a7268"
    return _PALETTE[sum(ord(c) for c in mood) % len(_PALETTE)]


def _now_hm(ts: float) -> str:
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except Exception:
        return ""


def load_entries(path: Path, limit: int = 600) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                rec.setdefault("ts", 0)
                rec.setdefault("day", "")
                rec.setdefault("hm", _now_hm(rec.get("ts") or 0))
                rec.setdefault("mood", "")
                rec.setdefault("title", "")
                rec.setdefault("body", "")
                rec.setdefault("videos", [])
                rec.setdefault("lessons", [])
                out.append(rec)
    except Exception:
        return out
    out.sort(key=lambda r: (str(r.get("day") or ""), float(r.get("ts") or 0)))
    return out[-limit:]


def _day_label(day: str) -> str:
    try:
        d = datetime.strptime(day, "%Y-%m-%d")
        return f"{day} {WEEKDAYS[d.weekday()]}"
    except Exception:
        return day or "未知日期"


def render_diary(entries: list[dict], *, tk: str = "", public: str = "") -> str:
    total = len(entries)
    days: dict[str, list[dict]] = {}
    for e in entries:
        days.setdefault(str(e.get("day") or ""), []).append(e)
    day_keys = sorted(days.keys(), reverse=True)
    n_days = len(day_keys)
    n_lessons = sum(len(e.get("lessons") or []) for e in entries)
    last = entries[-1] if entries else {}
    last_label = f"{_day_label(str(last.get('day') or ''))} {last.get('hm') or ''}".strip() if last else "还没有"

    toc_items = []
    for d in day_keys:
        rows = days[d]
        moods = "、".join({str(r.get("mood") or "") for r in rows if r.get("mood")})
        toc_items.append(
            f'<a class="tocrow" href="#d-{_esc(d)}" data-day="{_esc(d)}">'
            f'<span class="td">{_esc(_day_label(d))}</span>'
            f'<span class="tn">{len(rows)} 条</span>'
            f'<span class="tm">{_esc(moods[:14])}</span></a>'
        )

    blocks: list[str] = []
    for d in day_keys:
        rows = sorted(days[d], key=lambda x: float(x.get("ts") or 0))
        items: list[str] = []
        for e in rows:
            mood = str(e.get("mood") or "").strip()
            bg, fg = _mood_colors(mood)
            title = str(e.get("title") or "").strip()
            body = _esc(e.get("body") or "").replace("\n", "<br>")
            vids = e.get("videos") or []
            lessons = e.get("lessons") or []
            vblock = ""
            if vids:
                vblock = (
                    '<div class="more"><span class="mh">看了</span><ul>'
                    + "".join(f"<li>{_esc(v)}</li>" for v in vids[:5])
                    + "</ul></div>"
                )
            lblock = ""
            if lessons:
                lblock = (
                    '<div class="more lessons"><span class="mh">主人教我的</span><ul>'
                    + "".join(f"<li>{_esc(l)}</li>" for l in lessons[:4])
                    + "</ul></div>"
                )
            meta = f'<span class="chip">{_esc(e.get("hm") or "")}</span>'
            span = e.get("span") or []
            if len(span) == 2 and span[0]:
                meta += f'<span class="chip quiet">{_esc(span[0])}–{_esc(span[1])}</span>'
            cc = e.get("chat_count")
            if cc:
                meta += f'<span class="chip quiet">{int(cc)} 句对话</span>'
            items.append(
                '<article class="entry">'
                f'<div class="ehead"><span class="mood" style="background:{bg};color:{fg}">心情 · {_esc(mood) or "未记"}</span>'
                f'{f"<span class=ttl>{_esc(title)}</span>" if title else ""}</div>'
                f'<div class="body">{body}</div>{lblock}{vblock}'
                f'<div class="meta">{meta}</div>'
                "</article>"
            )
        blocks.append(
            f'<section class="day" id="d-{_esc(d)}" data-day="{_esc(d)}">'
            f'<h2>{_esc(_day_label(d))} <span class="cnt">{len(rows)} 条</span></h2>'
            + "".join(items)
            + "</section>"
        )

    watch_link = f"/watch?t={_esc(tk)}" if tk else "/watch"
    nav = (
        f'<nav class="nav"><a class="nava" href="{watch_link}">她看过什么</a>'
        '<a class="nava on" href="#">她的日记本</a></nav>'
    )

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>大肥鱼的日记本</title>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<style>
*{{box-sizing:border-box}}
:root{{--paper:#faf7f2;--card:#ffffff;--ink:#2f2a25;--ink2:#6b625a;--line:#e7e1d7;--hot:#c0743c;--less:#fff4d8;--lessline:#e9c98b}}
html,body{{margin:0;overflow-x:hidden}}
body{{background:var(--paper);color:var(--ink);
 font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif;
 padding:24px 18px 70px;-webkit-text-size-adjust:100%}}
.wrap{{max-width:1180px;margin:0 auto}}
.nav{{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}}
.nava{{text-decoration:none;font-size:13px;color:var(--ink2);background:#fff;border:1px solid var(--line);
 border-radius:999px;padding:6px 13px}}
.nava.on{{background:#2f2a25;color:#fff;border-color:#2f2a25}}
h1{{font-size:27px;margin:2px 0 6px;letter-spacing:.5px}}
.sub{{color:var(--ink2);font-size:13px;line-height:1.7;margin-bottom:16px}}
.kpis{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px 15px;min-width:104px}}
.kpi b{{display:block;font-size:20px;line-height:1.25}}
.kpi span{{font-size:12px;color:var(--ink2)}}
.search{{width:100%;background:#fff;border:1px solid var(--line);border-radius:12px;padding:11px 14px;
 font-size:14px;color:var(--ink);outline:none;margin-bottom:16px}}
.search:focus{{border-color:#c8bda9}}
.layout{{display:grid;grid-template-columns:220px minmax(0,1fr);gap:22px;align-items:start}}
.toc{{position:sticky;top:16px;background:#fff;border:1px solid var(--line);border-radius:14px;padding:10px;max-height:74vh;overflow:auto}}
.toc h3{{margin:2px 0 8px;font-size:12px;color:var(--ink2);font-weight:600;letter-spacing:1px}}
.tocrow{{display:flex;flex-direction:column;gap:2px;text-decoration:none;color:var(--ink);
 border-radius:10px;padding:7px 9px;font-size:13px}}
.tocrow:hover{{background:#f3ede2}}
.tocrow.on{{background:#f3ede2}}
.tocrow .tn{{font-size:11px;color:var(--ink2)}}
.tocrow .tm{{font-size:11px;color:#a08a72}}
.day{{margin-bottom:26px}}
.day h2{{font-size:15px;margin:0 0 10px;color:#4a423a;display:flex;align-items:baseline;gap:8px}}
.day h2 .cnt{{font-size:12px;color:var(--ink2);font-weight:400}}
.entry{{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px 16px;margin-bottom:12px}}
.ehead{{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:8px}}
.mood{{font-size:12px;border-radius:999px;padding:3px 11px}}
.ttl{{font-size:13px;color:#8a7a68}}
.body{{font-size:14.5px;line-height:1.85;white-space:normal}}
.more{{margin-top:11px;border-top:1px dashed var(--line);padding-top:9px}}
.more .mh{{font-size:12px;color:var(--ink2);letter-spacing:1px}}
.more ul{{margin:6px 0 0;padding-left:18px}}
.more li{{font-size:13px;color:#5d554c;line-height:1.7}}
.more.lessons{{background:var(--less);border:1px solid var(--lessline);border-radius:12px;padding:10px 12px;margin-top:12px}}
.more.lessons .mh{{color:var(--hot)}}
.more.lessons li{{color:#6b4a1f}}
.meta{{display:flex;gap:7px;flex-wrap:wrap;margin-top:11px}}
.chip{{font-size:11.5px;color:#5d554c;background:#f4efe6;border-radius:999px;padding:3px 9px}}
.chip.quiet{{background:transparent;border:1px solid var(--line);color:#8c8378}}
.empty{{background:#fff;border:1px dashed var(--line);border-radius:14px;padding:26px;text-align:center;color:var(--ink2)}}
@media(max-width:820px){{
 .layout{{grid-template-columns:minmax(0,1fr);gap:14px}}
 .toc{{position:static;max-height:none;display:flex;gap:8px;overflow-x:auto;padding:8px}}
 .toc h3{{display:none}}
 .tocrow{{flex:0 0 auto;background:#faf5ec;border:1px solid var(--line);padding:6px 11px}}
 .tocrow .tm{{display:none}}
 h1{{font-size:23px}}
 .kpi{{flex:1 1 44%}}
}}
@media(max-width:430px){{
 body{{padding:16px 12px 60px}}
 .entry{{padding:12px 13px}}
 .body{{font-size:15px}}
}}
</style></head><body><div class="wrap">
{nav}
<h1>大肥鱼的日记本</h1>
<div class="sub">她自己写的小本子：跟主人聊了什么、被教了什么、B站上刷到了什么。<br>
电脑和手机都能看；想看她在 B站刷过哪些视频、给它们打分，点上面「她看过什么」。</div>
<div class="kpis">
 <div class="kpi"><b>{total}</b><span>一共记了</span></div>
 <div class="kpi"><b>{n_days}</b><span>记了几天</span></div>
 <div class="kpi"><b>{n_lessons}</b><span>主人教过的事</span></div>
 <div class="kpi"><b>{_esc(last.get('mood') or '—')}</b><span>最近心情</span></div>
 <div class="kpi"><b>{_esc(str(last.get('day') or '—'))}</b><span>最近一篇</span></div>
</div>
<input class="search" id="q" placeholder="搜日记正文 / 心情 / 视频 / 主人教的事……">
<div class="layout">
 <aside class="toc"><h3>按天翻</h3>{''.join(toc_items) or '<div class="empty">还没有</div>'}</aside>
 <main id="days">{''.join(blocks) or '<div class="empty">她还没写第一篇呢。</div>'}</main>
</div>
</div>
<script>
const q = document.getElementById('q');
const entries = [...document.querySelectorAll('.entry')];
const days = [...document.querySelectorAll('.day')];
q && q.addEventListener('input', () => {{
  const kw = q.value.trim().toLowerCase();
  entries.forEach(el => {{
    const hit = !kw || el.textContent.toLowerCase().includes(kw);
    el.style.display = hit ? '' : 'none';
  }});
  days.forEach(d => {{
    const visible = [...d.querySelectorAll('.entry')].some(e => e.style.display !== 'none');
    d.style.display = visible ? '' : 'none';
    const row = document.querySelector('.tocrow[data-day="' + d.dataset.day + '"]');
    if (row) row.style.opacity = visible ? '1' : '.35';
  }});
}});
document.querySelectorAll('.tocrow').forEach(a => a.addEventListener('click', () => {{
  document.querySelectorAll('.tocrow').forEach(x => x.classList.remove('on'));
  a.classList.add('on');
}}));
</script></body></html>"""
