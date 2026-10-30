"""主人画像 + 「主人教我的」分类库。

两个文件都放在日记插件的 data_dir 下，面板插件直接读同一份：

- ``owner_profile.json``  主人的画像（性格 / 生活习惯 / 口头禅 / 心情指数 / 近期睡眠）
- ``lessons.json``        她学到的规矩，按「黑话 / 梗 / 调情 / 其他」分类

设计原则：文件是唯一事实来源，坏了就重建，绝不让插件因为读文件失败而停摆。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

CATEGORIES = ("黑话", "梗", "调情", "其他")

# 口头禅/黑话这类短句，超过这个长度大概率不是口头禅
_MAX_LESSON_CHARS = 160


def _now_iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or time.time()))


def _as_list(v: Any, limit: int = 12) -> list[str]:
    out: list[str] = []
    if isinstance(v, str):
        v = [s for s in v.replace("、", ",").replace("；", ",").split(",")]
    if isinstance(v, (list, tuple)):
        for item in v:
            s = str(item or "").strip()
            if s and s not in out:
                out.append(s)
    return out[:limit]


def normalize_category(v: Any) -> str:
    s = str(v or "").strip()
    for c in CATEGORIES:
        if c in s:
            return c
    low = s.lower()
    alias = {
        "slang": "黑话", "meme": "梗", "joke": "梗", "flirt": "调情",
        "tease": "调情", "other": "其他", "其它": "其他",
    }
    return alias.get(low, "其他")


def _lesson_id(text: str, category: str) -> str:
    h = hashlib.sha1(f"{category}|{text}".encode("utf-8")).hexdigest()[:12]
    return h


# --------------------------------------------------------------------------- #
# 主人画像
# --------------------------------------------------------------------------- #
_PROFILE_EMPTY: dict[str, Any] = {
    "version": 1,
    "updated_at": "",
    "days_covered": 0,
    "summary": "",
    "personality": [],
    "habits": [],
    "catchphrases": [],
    "mood": {"index": 0, "label": "", "note": ""},
    "sleep": {"quality": "", "note": ""},
    "highlights": [],
}


class OwnerProfile:
    """主人的画像：读写 + 合并 + 给页面的视图。"""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "owner_profile.json"

    # ---------------- 读写 ----------------
    def load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict):
                return dict(_PROFILE_EMPTY)
        except Exception:
            return dict(_PROFILE_EMPTY)
        data = dict(_PROFILE_EMPTY)
        data.update(raw)
        # 兜底：字段类型不对就当空
        for key in ("personality", "habits", "catchphrases", "highlights"):
            data[key] = _as_list(data.get(key))
        if not isinstance(data.get("mood"), dict):
            data["mood"] = dict(_PROFILE_EMPTY["mood"])
        if not isinstance(data.get("sleep"), dict):
            data["sleep"] = dict(_PROFILE_EMPTY["sleep"])
        return data

    def save(self, profile: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # ---------------- 合并 ----------------
    def merge(self, prev: dict[str, Any], fresh: dict[str, Any], *, days: int = 1) -> dict[str, Any]:
        """把 LLM 这轮的结论并进旧画像：列表去重后保留最新的，心情/睡眠以新一轮为准。"""
        out = dict(prev or _PROFILE_EMPTY)
        for key in ("personality", "habits", "catchphrases"):
            merged = _as_list(fresh.get(key)) + [v for v in _as_list(prev.get(key)) if v not in _as_list(fresh.get(key))]
            out[key] = merged[:12]
        out["highlights"] = _as_list(fresh.get("highlights"))[:8]
        out["summary"] = str(fresh.get("summary") or prev.get("summary") or "").strip()[:600]

        mood = fresh.get("mood") if isinstance(fresh.get("mood"), dict) else {}
        try:
            index = int(mood.get("index") or 0)
        except (TypeError, ValueError):
            index = 0
        out["mood"] = {
            "index": max(0, min(10, index)),
            "label": str(mood.get("label") or "").strip()[:20],
            "note": str(mood.get("note") or "").strip()[:200],
        }
        sleep = fresh.get("sleep") if isinstance(fresh.get("sleep"), dict) else {}
        out["sleep"] = {
            "quality": str(sleep.get("quality") or "").strip()[:20],
            "note": str(sleep.get("note") or "").strip()[:200],
        }
        out["updated_at"] = _now_iso()
        out["days_covered"] = int(prev.get("days_covered") or 0) + max(1, days)
        return out

    # ---------------- 给页面 ----------------
    def view(self) -> dict[str, Any]:
        p = self.load()
        mood = p.get("mood") or {}
        return {
            "updated_at": p.get("updated_at") or "",
            "days_covered": p.get("days_covered") or 0,
            "summary": p.get("summary") or "",
            "personality": p.get("personality") or [],
            "habits": p.get("habits") or [],
            "catchphrases": p.get("catchphrases") or [],
            "mood": {
                "index": mood.get("index") or 0,
                "label": mood.get("label") or "",
                "note": mood.get("note") or "",
            },
            "sleep": {
                "quality": (p.get("sleep") or {}).get("quality") or "",
                "note": (p.get("sleep") or {}).get("note") or "",
            },
            "highlights": p.get("highlights") or [],
            "has_data": bool(p.get("updated_at")),
        }


# --------------------------------------------------------------------------- #
# 主人教我的（分类库）
# --------------------------------------------------------------------------- #
class LessonBook:
    """「主人教我的」条目库，按黑话 / 梗 / 调情 / 其他 分类。"""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "lessons.json"

    def load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict) and isinstance(raw.get("items"), list):
                return raw
        except Exception:
            pass
        return {"version": 1, "items": []}

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, text: str, category: str = "其他", *, note: str = "", source: str = "chat",
            day: str = "") -> dict[str, Any] | None:
        text = " ".join(str(text or "").split())[:_MAX_LESSON_CHARS]
        if len(text) < 2:
            return None
        cat = normalize_category(category)
        data = self.load()
        items = data.get("items") or []
        lid = _lesson_id(text, cat)
        for it in items:
            if it.get("id") == lid:
                # 已存在：只更新时间和次数，不重复堆
                it["ts"] = it.get("ts") or _now_iso()
                it["hits"] = int(it.get("hits") or 1) + 1
                if note and not it.get("note"):
                    it["note"] = note
                self.save(data)
                return it
        item = {
            "id": lid,
            "ts": _now_iso(),
            "day": day or time.strftime("%Y-%m-%d"),
            "category": cat,
            "text": text,
            "note": str(note or "").strip()[:200],
            "source": source,
            "hits": 1,
        }
        items.append(item)
        data["items"] = items[-800:]
        data["version"] = 1
        self.save(data)
        return item

    def add_many(self, pairs: list[dict[str, Any]], *, source: str = "diary", day: str = "") -> int:
        n = 0
        for p in pairs or []:
            if isinstance(p, str):
                p = {"text": p, "category": "其他"}
            if not isinstance(p, dict):
                continue
            if self.add(p.get("text") or p.get("content") or "", p.get("category") or "其他",
                        note=p.get("note") or "", source=source, day=day):
                n += 1
        return n

    def stats(self) -> dict[str, Any]:
        data = self.load()
        items = data.get("items") or []
        counts = {c: 0 for c in CATEGORIES}
        for it in items:
            counts[normalize_category(it.get("category"))] = counts.get(normalize_category(it.get("category")), 0) + 1
        return {"total": len(items), "counts": counts}

    def view(self, limit: int = 400, q: str = "") -> dict[str, Any]:
        data = self.load()
        items = list(data.get("items") or [])
        items.sort(key=lambda it: str(it.get("ts") or ""), reverse=True)
        q = (q or "").strip().lower()
        if q:
            items = [it for it in items
                     if q in str(it.get("text", "")).lower()
                     or q in str(it.get("note", "")).lower()
                     or q in str(it.get("category", "")).lower()]
        items = items[:limit]
        for it in items:
            it["category"] = normalize_category(it.get("category"))
        groups = {c: [it for it in items if it["category"] == c] for c in CATEGORIES}
        return {
            "total": len(items),
            "counts": {c: len(v) for c, v in groups.items()},
            "groups": [{"category": c, "items": groups[c]} for c in CATEGORIES],
        }

    # ---------------- 从聊天里认「这是哪一类」 ----------------
    @staticmethod
    def detect_category(text: str) -> str:
        """看这句话里有没有明说类别，比如「这是黑话」「算个梗」。"""
        s = str(text or "")
        for c in ("黑话", "梗", "调情"):
            if c in s:
                return c
        return ""

    @staticmethod
    def strip_category_words(text: str) -> str:
        s = str(text or "").strip()
        for junk in ("黑话", "梗", "调情", "这是一类", "这一类", "这个类别", "类别", "分类"):
            s = s.replace(junk, "")
        for lead in ("记住", "记好", "你记住", "教你的", "教你", "以后", "这是", "这是:", "这是："):
            s = s.replace(lead, "")
        return " ".join(s.split()).strip(" ：:,，。.、-—")
