"""把「她看过的视频 + 主人的打分」渲染成一个静态页面，方便主人在电脑上翻、评。

页面写在 diary_dir 下（默认 data/bili_whale/watch.html），
本地用 看观看历史.bat 拉一份就能双击打开；打分本身走 QQ 的 /打分 指令。
"""

from __future__ import annotations

import html
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

_PAGE_TMPL = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>大肥鱼看过什么</title>
<style>
:root{{--bg:#f6f7fb;--card:#fff;--ink:#2b2f36;--dim:#8a919c;--line:#e7eaf1;--acc:#5b8def;--warm:#f0b429}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
 font:15px/1.7 "Microsoft YaHei","PingFang SC",system-ui,sans-serif}}
.wrap{{max-width:820px;margin:0 auto;padding:28px 18px 60px}}
h1{{font-size:24px;margin:0 0 6px}}
.meta{{color:var(--dim);font-size:13px;margin-bottom:18px}}
.tools{{display:flex;gap:10px;margin-bottom:18px;flex-wrap:wrap}}
input[type=search]{{flex:1;min-width:220px;padding:10px 14px;border:1px solid var(--line);
 border-radius:12px;background:#fff;font-size:14px;outline:none}}
input[type=search]:focus{{border-color:var(--acc)}}
.stat{{display:flex;gap:18px;color:var(--dim);font-size:13px;margin-bottom:18px;flex-wrap:wrap}}
.stat b{{color:var(--ink);font-weight:600}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;
 margin-bottom:14px;box-shadow:0 1px 2px rgba(20,30,60,.03)}}
.card.dim{{opacity:.72}}
.row{{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}}
.idx{{color:var(--dim);font-size:12px;font-variant-numeric:tabular-nums;min-width:26px}}
.title{{font-size:15px;font-weight:600;color:var(--ink);text-decoration:none;flex:1;min-width:200px}}
.title:hover{{color:var(--acc)}}
.tags{{margin-top:6px;display:flex;gap:8px;flex-wrap:wrap;font-size:12px;color:var(--dim)}}
.tag{{background:#f1f3f9;border-radius:999px;padding:1px 9px}}
.stars{{color:var(--warm);letter-spacing:1px}}
.stars .off{{color:#dfe3ec}}
.note{{margin-top:8px;padding:8px 12px;background:#fbfaf4;border-left:3px solid var(--warm);
 border-radius:0 8px 8px 0;font-size:13.5px;color:#5b5340}}
.cmd{{margin-top:8px;font-family:ui-monospace,Consolas,monospace;font-size:12.5px;color:#41506b;
 background:#f5f7fc;border:1px dashed var(--line);border-radius:8px;padding:6px 10px;display:inline-block}}
.hint{{color:var(--dim);font-size:13px;margin-top:22px;line-height:1.9}}
.hint code{{background:#eef1f8;border-radius:6px;padding:1px 6px;font-size:12.5px}}
.empty{{color:var(--dim);text-align:center;padding:40px 0}}
@media (prefers-color-scheme:dark){{
 :root{{--bg:#14161a;--card:#1c1f25;--ink:#e6e9ef;--dim:#8b93a1;--line:#2a2f38;
  --acc:#7aa2f7;--warm:#e0af68}}
 .tag{{background:#242932}} .note{{background:#221f18;color:#d8c9a8}}
 .cmd{{background:#22262e;color:#a9b6d0}} .hint code{{background:#242932}}
}}
</style></head><body><div class="wrap">
<h1>大肥鱼看过什么</h1>
<div class="meta">共 {total} 条 ｜ {rated} 条已打分 ｜ 更新于 {now}</div>
<div class="tools"><input id="q" type="search" placeholder="搜标题、主题、评价……"></div>
<div class="stat"><span>看过 <b>{total}</b></span><span>推出去过 <b>{shared}</b></span>
<span>主人给高分 <b>{good}</b></span><span>主人嫌弃 <b>{bad}</b></span></div>
{cards}
<div class="hint">
在 QQ 里给她打分（越主观越好）：<br>
<code>/打分 3 5 这种多来点</code> —— 3 是上面的序号，5 是 1–5 星；<br>
<code>/打分 BV18EsZ6kE3m 2 又是数码，别推了</code> —— 直接用 BV 号；<br>
她的下一次选题会按主人的高分方向加权重、低分方向压低。<br>
页面由插件每轮自动刷新，双击 <b>看观看历史.bat</b> 可拉最新版。
</div>
</div>
<script>
const q=document.getElementById('q');
q.addEventListener('input',()=>{{
  const k=q.value.trim().toLowerCase();
  document.querySelectorAll('.card').forEach(c=>{{
    c.style.display=!k||c.dataset.s.toLowerCase().includes(k)?'':'none';
  }});
}});
</script>
</body></html>"""

_CARD_TMPL = """<div class="card{cls}" data-s="{hay}">
<div class="row"><span class="idx">#{idx}</span>
<a class="title" href="{url}" target="_blank" rel="noreferrer">{title}</a>
<span class="stars">{stars}</span></div>
<div class="tags"><span class="tag">{date}</span><span class="tag">她给 {score} 分</span>
{shared_tag}{kw_tag}<span class="tag">{bvid}</span></div>
{note}
<div class="cmd">/打分 {idx} {next_star} 你的评价</div>
</div>"""


def _stars_html(stars: int) -> str:
    stars = max(0, min(5, int(stars or 0)))
    if not stars:
        return '<span class="off">☆☆☆☆☆</span>'
    return "★" * stars + f'<span class="off">{"☆" * (5 - stars)}</span>'


def _esc(s: Any) -> str:
    return html.escape(str(s or ""), quote=True)


def build_watch_page(store: Any, cfg: Any) -> Path | None:
    """生成页面文件，返回路径；出错只记日志不抛。"""
    try:
        limit = int(getattr(cfg, "watch_page_limit", 80))
        rows = store.watch_history(limit)
        now = time.strftime("%Y-%m-%d %H:%M")
        cards: list[str] = []
        rated = shared = good = bad = 0
        for i, v in enumerate(rows, 1):
            stars = int(v.get("stars") or 0)
            if stars:
                rated += 1
            if stars >= 4:
                good += 1
            elif stars and stars <= 2:
                bad += 1
            if v.get("shared"):
                shared += 1
            note = (v.get("note") or "").strip()
            cards.append(
                _CARD_TMPL.format(
                    cls=" dim" if v.get("shared") is False and stars == 0 else "",
                    hay=_esc(
                        f"{v.get('title','')} {v.get('keyword','')} {note} {v.get('bvid','')}"
                    ),
                    idx=i,
                    url=_esc(v.get("url") or f"https://www.bilibili.com/video/{v.get('bvid','')}"),
                    title=_esc(v.get("title") or v.get("bvid") or "（没记住标题）"),
                    stars=_stars_html(stars),
                    date=_esc(v.get("date") or ""),
                    score=int(v.get("score") or 0),
                    shared_tag='<span class="tag">推出去过</span>' if v.get("shared") else "",
                    kw_tag=f'<span class="tag">#{_esc(v.get("keyword"))}</span>' if v.get("keyword") else "",
                    bvid=_esc(v.get("bvid") or ""),
                    next_star=5 if stars else 5,
                    note=f'<div class="note">主人说：{_esc(note)}</div>' if note else "",
                )
            )
        page = _PAGE_TMPL.format(
            total=len(rows),
            rated=rated,
            shared=shared,
            good=good,
            bad=bad,
            now=now,
            cards="\n".join(cards) or '<div class="empty">还没有记录，等她刷几条。</div>',
        )
        out_dir = Path(str(getattr(cfg, "diary_dir", "") or "data/bili_whale"))
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / str(getattr(cfg, "watch_page_name", "watch.html") or "watch.html")
        path.write_text(page, encoding="utf-8")
        return path
    except Exception as e:
        logger.warning(f"[bili-whale] 生成观看历史页失败：{e}")
        return None
