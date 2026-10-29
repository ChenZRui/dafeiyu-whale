"""给日记本收集素材：这段时间都聊了什么、B站看了什么。

素材来源（都是别的插件已经落好的盘，这里只读不写）：
- whale_memory 的 chatlog/<日期>.jsonl：每条 {t, umo, role, text}
- bili_whale 的 state.json 里 seen 记录：{ts, title, score, shared, ...}
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger


def _parse_ts(s: Any) -> float:
    """ISO 字符串 / 秒级时间戳 -> epoch 秒；解析不了给 0。"""
    if isinstance(s, (int, float)):
        return float(s)
    txt = str(s or "").strip()
    if not txt:
        return 0.0
    try:
        return datetime.fromisoformat(txt.replace("Z", "+00:00")).timestamp()
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(txt, fmt).timestamp()
        except Exception:
            continue
    return 0.0


def _umo_label(umo: str) -> str:
    """aiocqhttp:GroupMessage:20001 -> 群 20001；FriendMessage -> 私聊。"""
    parts = str(umo or "").split(":")
    if len(parts) >= 3:
        kind, target = parts[1], parts[2]
        if "Group" in kind:
            return f"群 {target}"
        if "Friend" in kind:
            return "私聊"
        return kind
    return "聊天"


class ChatCollector:
    def __init__(self, chatlog_dir: Path, *, max_lines: int = 45):
        self.dir = Path(chatlog_dir)
        self.max_lines = max_lines

    def lines_between(self, t0: float, t1: float) -> list[dict]:
        """取 [t0, t1] 之间的对话，按时间正序。"""
        day_files = []
        d0 = datetime.fromtimestamp(t0).date()
        d1 = datetime.fromtimestamp(t1).date()
        cur = d0
        while cur <= d1:
            p = self.dir / f"{cur.isoformat()}.jsonl"
            if p.exists():
                day_files.append(p)
            cur = datetime.fromordinal(cur.toordinal() + 1).date()
        out: list[dict] = []
        for p in day_files:
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
                        ts = _parse_ts(rec.get("t"))
                        if ts < t0 or ts > t1:
                            continue
                        text = str(rec.get("text") or "").strip()
                        if not text:
                            continue
                        out.append(
                            {
                                "ts": ts,
                                "hm": time.strftime("%H:%M", time.localtime(ts)),
                                "who": "我" if rec.get("role") == "assistant" else "别人",
                                "where": _umo_label(rec.get("umo")),
                                "text": text,
                            }
                        )
            except Exception as e:
                logger.warning(f"[whale-journal] 读 chatlog 失败 {p}: {e}")
        out.sort(key=lambda x: x["ts"])
        if len(out) > self.max_lines:
            # 太多就每段抽：保住开头和结尾，中间抽样
            head = out[: self.max_lines // 3]
            tail = out[-self.max_lines // 2 :]
            mid_pool = out[len(head) : len(out) - len(tail)]
            step = max(1, len(mid_pool) // max(1, self.max_lines - len(head) - len(tail)))
            mid = mid_pool[::step][: self.max_lines - len(head) - len(tail)]
            out = head + mid + tail
        return out

    def count_between(self, t0: float, t1: float) -> int:
        return len(self.lines_between(t0, t1))


class BiliCollector:
    def __init__(self, state_path: Path):
        self.path = Path(state_path)

    def watched_between(self, t0: float, t1: float, limit: int = 6) -> list[dict]:
        """这段时间她看过的视频（只要真正看过的，最多 limit 条）。"""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[whale-journal] 读 bili state 失败：{e}")
            return []
        seen = data.get("seen") or {}
        out: list[dict] = []
        for bvid, rec in seen.items():
            if not isinstance(rec, dict):
                continue
            ts = float(rec.get("ts") or 0)
            if ts < t0 or ts > t1:
                continue
            out.append(
                {
                    "bvid": bvid,
                    "title": str(rec.get("title") or ""),
                    "up": str(rec.get("up") or ""),
                    "score": int(rec.get("score") or 0),
                    "shared": bool(rec.get("shared")),
                    "keyword": str(rec.get("keyword") or ""),
                    "note": str(rec.get("note") or ""),
                    "ts": ts,
                }
            )
        out.sort(key=lambda x: x["ts"], reverse=True)
        return out[:limit]


def guess_mistakes(lines: list[dict], limit: int = 5) -> list[str]:
    """从对话里挑出「可能是在吐槽她」的几句话，当作「今天翻车」的线索。"""
    cues = ("错了", "不对", "你怎么", "又", "翻车", "离谱", "别乱", "重新", "听不懂", "答非所问", "搞错")
    out: list[str] = []
    for ln in lines:
        if ln.get("who") == "我":
            continue
        text = ln.get("text") or ""
        if len(text) > 60:
            continue
        if any(c in text for c in cues):
            out.append(f"{ln['hm']} {text}")
        if len(out) >= limit:
            break
    return out
