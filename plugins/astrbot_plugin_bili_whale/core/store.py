"""插件状态持久化：看过哪些视频、今天分享了几条、哪些互动已经处理过。

用一个 state.json，原子写入（先写 .tmp 再 replace），坏文件自动重建，
不需要数据库依赖。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger

STATE_FILE = "state.json"
MAX_SEEN = 800  # 最多记多少条看过的视频
MAX_HANDLED = 1200  # 最多记多少条已处理互动
SEEN_TTL_DAYS = 90


class StateStore:
    def __init__(self, dir_path: Path | str):
        self.dir = Path(dir_path)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / STATE_FILE
        self.data: dict[str, Any] = self._load()

    # ------------------------------------------------------------ 读写
    def _default(self) -> dict[str, Any]:
        return {
            "version": 1,
            "seen": {},  # bvid -> {"ts": int, "title": str, "score": int, "shared": bool}
            "handled": {},  # "kind:key" -> ts
            "shares": {},  # "YYYY-MM-DD" -> int
            "last_wake": 0,
            "last_run_summary": "",
            "runs": 0,
            "last_at_id": 0,
            "last_reply_id": 0,
            "last_unread": {},
            "peer": {"date": "", "count": 0, "threads": {}},
            "feedback": {},  # bvid -> {"stars": 1-5, "note": "主人说的话", "ts": int}
            "last_list": [],  # 上一次 /观看历史 发出去的 bvid 顺序，给 /打分 用序号
        }

    def _load(self) -> dict[str, Any]:
        base = self._default()
        if not self.path.exists():
            return base
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in base.items():
                    raw.setdefault(k, v)
                return raw
        except Exception as e:
            logger.warning(f"[bili-whale] state.json 损坏，重建：{e}")
        return base

    def save(self) -> None:
        try:
            self._trim()
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self.data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except Exception as e:
            logger.warning(f"[bili-whale] 保存 state.json 失败：{e}")

    def _trim(self) -> None:
        seen: dict = self.data.get("seen") or {}
        if len(seen) > MAX_SEEN:
            keep = sorted(seen.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)[:MAX_SEEN]
            self.data["seen"] = dict(keep)
        cutoff = time.time() - SEEN_TTL_DAYS * 86400
        self.data["seen"] = {
            k: v for k, v in (self.data.get("seen") or {}).items() if v.get("ts", 0) >= cutoff
        }
        handled: dict = self.data.get("handled") or {}
        if len(handled) > MAX_HANDLED:
            keep = sorted(handled.items(), key=lambda kv: kv[1], reverse=True)[:MAX_HANDLED]
            self.data["handled"] = dict(keep)
        # shares 只留最近 30 天
        shares: dict = self.data.get("shares") or {}
        dcut = datetime.now().strftime("%Y-%m-%d")
        recent = sorted(shares.keys())[-30:]
        self.data["shares"] = {k: shares[k] for k in recent} if dcut else shares

    # ------------------------------------------------------------ 看过的视频
    def is_seen(self, bvid: str) -> bool:
        return str(bvid) in (self.data.get("seen") or {})

    def mark_seen(
        self,
        bvid: str,
        *,
        title: str = "",
        score: int = 0,
        shared: bool = False,
        cover: str = "",
        extra: dict | None = None,
    ) -> None:
        rec = {
            "ts": int(time.time()),
            "title": str(title)[:200],
            "score": int(score),
            "shared": bool(shared),
        }
        if cover:
            rec["cover"] = str(cover)[:300]
        if extra:
            rec.update({k: v for k, v in extra.items() if isinstance(v, (str, int, float, bool))})
        self.data.setdefault("seen", {})[str(bvid)] = rec

    def seen_titles(self, limit: int = 40) -> list[str]:
        items = sorted(
            (self.data.get("seen") or {}).items(),
            key=lambda kv: kv[1].get("ts", 0),
            reverse=True,
        )[:limit]
        return [v.get("title", "") for _, v in items if v.get("title")]

    def recent_topics(self, limit: int = 12) -> list[str]:
        """最近看过的视频所属主题（凑近 keywords 的那一项），用来避免连着推同一类。"""
        items = sorted(
            (self.data.get("seen") or {}).items(),
            key=lambda kv: kv[1].get("ts", 0),
            reverse=True,
        )
        out: list[str] = []
        for _, v in items:
            kw = str(v.get("keyword") or "").strip()
            if kw and kw not in out:
                out.append(kw)
            if len(out) >= limit:
                break
        return out

    def recent_memories(self, limit: int = 5) -> list[dict[str, Any]]:
        """最近真正看过/分享过的视频，带当时的想法——用来喂给聊天人格当记忆。"""
        items = sorted(
            (self.data.get("seen") or {}).items(),
            key=lambda kv: kv[1].get("ts", 0),
            reverse=True,
        )
        out: list[dict[str, Any]] = []
        for bvid, v in items:
            ts = int(v.get("ts") or 0)
            out.append(
                {
                    "bvid": bvid,
                    "title": v.get("title", ""),
                    "ts": ts,
                    "date": time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "",
                    "score": int(v.get("score") or 0),
                    "shared": bool(v.get("shared")),
                    "up": v.get("up", ""),
                    "url": v.get("url", ""),
                    "note": v.get("note", ""),
                    "summary": v.get("summary", ""),
                    "letter": v.get("letter", ""),
                }
            )
            if len(out) >= max(1, limit):
                break
        return out

    # ------------------------------------------------------------ 主人打分
    def rate(self, bvid: str, stars: int, note: str = "", fav=None) -> dict[str, Any]:
        """主人给一条视频打分（1–5 星）+ 一句评价，影响她下次怎么选题。

        fav=None 表示「不动收藏状态」，True/False 才是明确改。
        """
        bvid = str(bvid or "").strip()
        stars = max(1, min(5, int(stars)))
        old = self.data.get("feedback", {}).get(bvid) or {}
        rec = {
            "stars": stars,
            "note": str(note or "")[:200],
            "fav": bool(old.get("fav")) if fav is None else bool(fav),
            "ts": int(time.time()),
        }
        self.data.setdefault("feedback", {})[bvid] = rec
        self.save()
        return rec

    def merge_ratings(self, path: Path | str) -> int:
        """把打分台（watchui）写出的 ratings.json 合进 feedback。

        打分台的记录带 ts（float）；只采纳比本地 feedback 更新的那些，避免来回覆盖。
        返回新采纳的条数。
        """
        p = Path(path)
        if not p.exists():
            return 0
        try:
            obj = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception:
            return 0
        items = (obj or {}).get("items") or {}
        if not isinstance(items, dict):
            return 0
        fb = self.data.setdefault("feedback", {})
        adopted = 0
        for bvid, rec in items.items():
            if not isinstance(rec, dict):
                continue
            try:
                stars = int(rec.get("stars") or 0)
            except Exception:
                continue
            if stars <= 0:
                continue
            ts = float(rec.get("ts") or 0)
            old = fb.get(str(bvid)) or {}
            # 同一秒内的重复合并不要来回覆盖：按整秒比较，只有严格更新才采纳
            old_ts = float(old.get("ts") or 0)
            if ts and int(ts) <= int(old_ts):
                continue
            fb[str(bvid)] = {
                "stars": max(1, min(5, stars)),
                "note": str(rec.get("note") or "")[:200],
                "fav": bool(rec.get("fav")) or bool(old.get("fav")),
                "ts": int(ts or time.time()),
            }
            adopted += 1
        if adopted:
            self.save()
        return adopted

    def topic_weights(self) -> dict[str, dict[str, Any]]:
        """按主题统计主人打星情况：{主题: {n, avg, stars:[...]}}。用来给她第二天的选择调权重。"""
        seen = self.data.get("seen") or {}
        fb = self.data.get("feedback") or {}
        buckets: dict[str, list[int]] = {}
        for bvid, rec in fb.items():
            if not isinstance(rec, dict):
                continue
            stars = int(rec.get("stars") or 0)
            if stars <= 0:
                continue
            kw = str((seen.get(bvid) or {}).get("keyword") or "").strip()
            if not kw:
                continue
            buckets.setdefault(kw, []).append(stars)
        out: dict[str, dict[str, Any]] = {}
        for kw, arr in buckets.items():
            out[kw] = {
                "n": len(arr),
                "avg": round(sum(arr) / len(arr), 2),
                "stars": sorted(arr),
            }
        return out

    def topic_bonus(self, keyword: str) -> float:
        """把主题权重折算成一个数值加成（喂给排序 / 送进打分 prompt）。"""
        kw = str(keyword or "").strip()
        if not kw:
            return 0.0
        w = self.topic_weights().get(kw)
        if not w:
            return 0.0
        # 平均 3 星为中性；每差 1 星折算 ±1.5 分，最多 ±6
        bonus = (float(w["avg"]) - 3.0) * 1.5
        return round(max(-6.0, min(6.0, bonus)), 2)

    def stars_of(self, bvid: str) -> int:
        rec = (self.data.get("feedback") or {}).get(str(bvid or ""))
        return int(rec.get("stars") or 0) if isinstance(rec, dict) else 0

    def feedback_of(self, bvid: str) -> dict[str, Any]:
        rec = (self.data.get("feedback") or {}).get(str(bvid or ""))
        return dict(rec) if isinstance(rec, dict) else {}

    def watch_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """看过的视频 + 主人的打分/评价，按时间倒序。页面、榜单、选题都吃这一份。"""
        items = sorted(
            (self.data.get("seen") or {}).items(),
            key=lambda kv: kv[1].get("ts", 0),
            reverse=True,
        )[: max(1, limit)]
        out: list[dict[str, Any]] = []
        for bvid, v in items:
            ts = int(v.get("ts") or 0)
            fb = self.feedback_of(bvid)
            out.append(
                {
                    "bvid": bvid,
                    "title": v.get("title", ""),
                    "ts": ts,
                    "date": time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "",
                    "score": int(v.get("score") or 0),
                    "shared": bool(v.get("shared")),
                    "keyword": v.get("keyword", ""),
                    "url": v.get("url") or f"https://www.bilibili.com/video/{bvid}",
                    "stars": int(fb.get("stars") or 0),
                    "note": fb.get("note", ""),
                }
            )
        return out

    def rated_brief(self, limit: int = 12) -> dict[str, list[dict[str, Any]]]:
        """把主人的评分整理成「喜欢 / 不喜欢」两组，喂给打分模型。"""
        items = sorted(
            (self.data.get("feedback") or {}).items(),
            key=lambda kv: kv[1].get("ts", 0),
            reverse=True,
        )
        seen = self.data.get("seen") or {}
        up: list[dict[str, Any]] = []
        down: list[dict[str, Any]] = []
        for bvid, rec in items:
            if not isinstance(rec, dict):
                continue
            stars = int(rec.get("stars") or 0)
            row = {
                "title": str((seen.get(bvid) or {}).get("title") or bvid)[:60],
                "note": str(rec.get("note") or "")[:80],
                "stars": stars,
                "fav": bool(rec.get("fav")),
            }
            if stars >= 4 and len(up) < limit:
                up.append(row)
            elif stars <= 2 and len(down) < limit:
                down.append(row)
        return {"liked": up, "disliked": down}

    def fav_set(self) -> set[str]:
        """主人打了收藏标签（或 5 星）的视频集合。"""
        out: set[str] = set()
        for bvid, rec in (self.data.get("feedback") or {}).items():
            if not isinstance(rec, dict):
                continue
            if rec.get("fav") or int(rec.get("stars") or 0) >= 5:
                out.add(str(bvid))
        return out

    def toggle_fav(self, bvid: str) -> bool:
        fb = self.data.setdefault("feedback", {})
        rec = fb.get(bvid) or {"stars": 0, "note": "", "ts": int(time.time())}
        rec["fav"] = not bool(rec.get("fav"))
        rec["ts"] = int(time.time())
        fb[bvid] = rec
        self.save()
        return bool(rec["fav"])

    def set_last_list(self, bvids: list[str]) -> None:
        self.data["last_list"] = [str(b) for b in (bvids or [])][:30]

    def bvid_by_index(self, idx: int) -> str:
        """主人用「第 3 条」这种口语打分时，回查上次列表里的 BV 号。"""
        lst = self.data.get("last_list") or []
        try:
            i = int(idx)
        except Exception:
            return ""
        if 1 <= i <= len(lst):
            return str(lst[i - 1])
        return ""

    def unseen(self, bvids: list[str]) -> list[str]:
        return [b for b in bvids if not self.is_seen(b)]

    # ------------------------------------------------------------ 每日配额
    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def shares_today(self) -> int:
        return int((self.data.get("shares") or {}).get(self._today(), 0))

    def add_share(self) -> None:
        shares = self.data.setdefault("shares", {})
        d = self._today()
        shares[d] = int(shares.get(d, 0)) + 1

    def share_quota_left(self, quota: int) -> int:
        return max(0, int(quota) - self.shares_today())

    # ------------------------------------------------------------ 互动去重
    def is_handled(self, kind: str, key: str | int) -> bool:
        return f"{kind}:{key}" in (self.data.get("handled") or {})

    def mark_handled(self, kind: str, key: str | int) -> None:
        self.data.setdefault("handled", {})[f"{kind}:{key}"] = int(time.time())

    # ------------------------------------------------------------ 其它
    def note_wake(self) -> None:
        self.data["last_wake"] = int(time.time())
        self.data["runs"] = int(self.data.get("runs", 0)) + 1

    def set_summary(self, text: str) -> None:
        self.data["last_run_summary"] = str(text)[:500]

    @property
    def last_wake_str(self) -> str:
        ts = int(self.data.get("last_wake") or 0)
        if not ts:
            return "还没有过"
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))

    @property
    def runs(self) -> int:
        return int(self.data.get("runs") or 0)

    def set_last_at_id(self, v: int) -> None:
        self.data["last_at_id"] = int(v or 0)

    @property
    def last_at_id(self) -> int:
        return int(self.data.get("last_at_id") or 0)

    def set_last_unread(self, d: dict) -> None:
        self.data["last_unread"] = {str(k): int(v or 0) for k, v in (d or {}).items()}

    def unread_delta(self, now: dict) -> dict:
        """和上次比，哪类未读变多了。"""
        old = self.data.get("last_unread") or {}
        out = {}
        for k, v in (now or {}).items():
            try:
                nv = int(v or 0)
                ov = int(old.get(k) or 0)
            except (TypeError, ValueError):
                continue
            if nv > ov:
                out[str(k)] = nv - ov
        return out

    def summary(self) -> str:
        return (
            f"运行 {self.runs} 轮｜上次醒来 {self.last_wake_str}｜"
            f"今天已分享 {self.shares_today()} 条｜记着 {len(self.data.get('seen') or {})} 个看过的视频"
        )

    # ------------------------------------------------------------ 评论区互动 / 别的鱼
    def set_last_reply_id(self, v: int) -> None:
        self.data["last_reply_id"] = int(v or 0)

    @property
    def last_reply_id(self) -> int:
        return int(self.data.get("last_reply_id") or 0)

    def _peer(self) -> dict[str, Any]:
        p = self.data.get("peer")
        if not isinstance(p, dict):
            p = {"date": "", "count": 0, "threads": {}}
            self.data["peer"] = p
        p.setdefault("date", "")
        p.setdefault("count", 0)
        p.setdefault("threads", {})
        if p.get("date") != self._today():
            p["date"] = self._today()
            p["count"] = 0
        return p

    @property
    def peer_count_today(self) -> int:
        return int(self._peer().get("count") or 0)

    def peer_left(self, limit: int) -> int:
        return max(0, int(limit or 0) - self.peer_count_today)

    def peer_used(self, uid: int = 0) -> None:
        p = self._peer()
        p["count"] = int(p.get("count") or 0) + 1

    def peer_turns(self, thread_key: str) -> int:
        t = (self._peer().get("threads") or {}).get(str(thread_key)) or {}
        return int(t.get("turns") or 0)

    def peer_bump(self, thread_key: str, uid: int = 0) -> int:
        """记一轮对话，返回累计轮数。"""
        p = self._peer()
        threads = p.setdefault("threads", {})
        key = str(thread_key)
        t = threads.get(key) or {"turns": 0, "uid": int(uid or 0), "ts": 0}
        t["turns"] = int(t.get("turns") or 0) + 1
        t["uid"] = int(uid or t.get("uid") or 0)
        t["ts"] = int(time.time())
        threads[key] = t
        return int(t["turns"])
