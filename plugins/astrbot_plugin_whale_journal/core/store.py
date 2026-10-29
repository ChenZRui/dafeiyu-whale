"""日记本的存储层：entries.jsonl（数据）+ <日期>.md（人手看）+ index.html（浏览器看）。

三种产物各管一件事：
- `entries.jsonl`：一行一条，给程序用（增量追加，永不重写）；
- `日记/YYYY-MM-DD.md`：仿那个网友的格式（时间点 + 心情 + 正文），当纯文本本子；
- `index.html`：所有日记的网页版（心情标签着色、按天分组、带搜索），主人对着它看。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger

WEEKDAYS = "一二三四五六日"


def _now_hm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


class JournalStore:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.entries_path = self.dir / "entries.jsonl"
        self.state_path = self.dir / "state.json"
        self.html_path = self.dir / "index.html"

    # ------------------------------------------------------------ 读写
    def load_entries(self) -> list[dict]:
        out: list[dict] = []
        if not self.entries_path.exists():
            return out
        try:
            with self.entries_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except Exception as e:
            logger.warning(f"[whale-journal] 读 entries 失败：{e}")
        return out

    def append_entry(self, entry: dict) -> None:
        try:
            with self.entries_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"[whale-journal] 写 entries 失败：{e}")
            return
        self.write_day(datetime.fromtimestamp(entry["ts"]).date().isoformat())
        self.write_html()

    def load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_state(self, state: dict) -> None:
        try:
            self.state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"[whale-journal] 写 state 失败：{e}")

    # ------------------------------------------------------------ markdown
    def write_day(self, day: str) -> None:
        entries = [e for e in self.load_entries() if e.get("day") == day]
        if not entries:
            return
        d = datetime.strptime(day, "%Y-%m-%d")
        lines = [f"# {day} 星期{WEEKDAYS[d.weekday()]}", ""]
        for e in entries:
            mood = (e.get("mood") or "").strip()
            lines.append(e.get("hm") or _now_hm(e.get("ts", 0)))
            lines.append("")
            if mood:
                lines.append(f"> 心情：{mood}")
                lines.append("")
            body = (e.get("body") or "").strip()
            if body:
                lines.append(body)
                lines.append("")
            for l in (e.get("lessons") or [])[:4]:
                lines.append(f"> 主人教我的：{l}")
            if e.get("lessons"):
                lines.append("")
        path = self.dir / f"{day}.md"
        try:
            path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        except Exception as e:
            logger.warning(f"[whale-journal] 写日记 md 失败：{e}")

    def day_text(self, day: str) -> str:
        p = self.dir / f"{day}.md"
        try:
            return p.read_text(encoding="utf-8")
        except Exception:
            return ""

    # ------------------------------------------------------------ html
    def write_html(self) -> None:
        entries = self.load_entries()
        try:
            self.html_path.write_text(render_html(entries), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[whale-journal] 写 index.html 失败：{e}")


# ---------------------------------------------------------------- 渲染
_PALETTE = ["#f4b8c1", "#a8d8ea", "#c3e88d", "#ffd6a5", "#bdb2ff", "#9ad5c8", "#f7a072", "#cbaacb"]


def _mood_color(mood: str) -> str:
    if not mood:
        return "#9e9e9e"
    return _PALETTE[sum(ord(c) for c in mood) % len(_PALETTE)]


def render_html(entries: list[dict], *, title: str = "大肥鱼的日记本") -> str:
    by_day: dict[str, list[dict]] = {}
    for e in entries:
        by_day.setdefault(str(e.get("day") or ""), []).append(e)
    for v in by_day.values():
        v.sort(key=lambda x: x.get("ts", 0))
    days = sorted(by_day.keys(), reverse=True)

    cards: list[str] = []
    for day in days:
        try:
            d = datetime.strptime(day, "%Y-%m-%d")
            head = f"{day}　星期{WEEKDAYS[d.weekday()]}"
        except Exception:
            head = day
        items: list[str] = []
        for e in by_day[day]:
            mood = (e.get("mood") or "").strip()
            color = _mood_color(mood)
            hm = e.get("hm") or _now_hm(e.get("ts", 0))
            body = (e.get("body") or "").strip().replace("\n", "<br>")
            extra = ""
            videos = e.get("videos") or []
            if videos:
                lis = "".join(f"<li>{v}</li>" for v in videos[:5])
                extra = f'<div class="vids"><span class="vh">看了</span><ul>{lis}</ul></div>'
            lessons = e.get("lessons") or []
            if lessons:
                lis = "".join(f"<li>{l}</li>" for l in lessons[:4])
                extra += f'<div class="vids lessons"><span class="vh">主人教我的</span><ul>{lis}</ul></div>'
            mood_tag = (
                f'<span class="mood" style="background:{color}">心情 · {mood}</span>'
                if mood
                else ""
            )
            items.append(
                f'<div class="entry"><div class="time">{hm}</div>'
                f'<div class="ehead">{mood_tag}</div>'
                f'<div class="body">{body}</div>{extra}</div>'
            )
        cards.append(
            f'<section class="day"><h2>{head}</h2>{"".join(items)}</section>'
        )

    total = len(entries)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
* {{ box-sizing: border-box; }}
body {{ margin:0; padding:32px 16px 80px; background:#faf7f2;
  font-family: -apple-system, "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
  color:#3a3a3a; line-height:1.75; }}
.wrap {{ max-width: 760px; margin:0 auto; }}
h1 {{ font-size:26px; margin:0 0 4px; letter-spacing:1px; }}
.sub {{ color:#8d8d8d; font-size:13px; margin-bottom:26px; }}
.search {{ width:100%; padding:10px 14px; border:1px solid #e6dfd3; border-radius:10px;
  background:#fff; font-size:14px; margin-bottom:26px; outline:none; }}
.search:focus {{ border-color:#d9c8a9; }}
.day {{ background:#fff; border:1px solid #eee6d8; border-radius:14px;
  padding:20px 22px; margin-bottom:22px; box-shadow:0 1px 2px rgba(0,0,0,.03); }}
.day h2 {{ font-size:15px; margin:0 0 16px; color:#9b8f7a; font-weight:600;
  letter-spacing:.5px; border-bottom:1px dashed #eee4d4; padding-bottom:10px; }}
.entry {{ padding:12px 0 14px; border-bottom:1px dotted #f0eade; }}
.entry:last-child {{ border-bottom:none; padding-bottom:2px; }}
.time {{ font-size:13px; color:#b3a892; letter-spacing:1px; }}
.ehead {{ margin:6px 0 8px; }}
.mood {{ display:inline-block; padding:2px 12px; border-radius:20px; font-size:12.5px;
  color:#4a4038; box-shadow:inset 0 0 0 1px rgba(0,0,0,.04); }}
.body {{ font-size:15.5px; }}
.vids {{ margin-top:10px; font-size:13px; color:#8a8172; }}
.vh {{ display:inline-block; margin-right:6px; color:#b3a892; }}
.vids ul {{ margin:4px 0 0; padding-left:20px; }}
.vids li {{ margin:2px 0; }}
.hidden {{ display:none; }}
.empty {{ text-align:center; color:#b3a892; padding:40px 0; }}
</style></head><body><div class="wrap">
<h1>{title}</h1>
<div class="sub">共 {total} 条 ｜ 更新于 {time.strftime("%Y-%m-%d %H:%M")}</div>
<input class="search" id="q" placeholder="搜点什么…… 心情、视频名、某句话">
{''.join(cards) if cards else '<div class="empty">本子还是空的。</div>'}
</div>
<script>
const q = document.getElementById('q');
q.addEventListener('input', () => {{
  const k = q.value.trim();
  document.querySelectorAll('.entry').forEach(el => {{
    el.classList.toggle('hidden', k && !el.innerText.includes(k));
  }});
  document.querySelectorAll('.day').forEach(sec => {{
    const any = [...sec.querySelectorAll('.entry')].some(el => !el.classList.contains('hidden'));
    sec.classList.toggle('hidden', !any);
  }});
}});
</script>
</body></html>
"""
