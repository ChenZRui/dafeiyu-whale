"""日记本插件的配置包装。字段名与 _conf_schema.json 一一对应。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "enable": True,
    "owner_qq": "",
    "llm_provider_id": "deepseek/deepseek-flash",
    # astrbot=跟 AstrBot 面板里当前生效的人格（推荐）；file=只用 persona_file / persona_fallback
    "persona_source": "astrbot",
    "persona_id": "",
    "persona_file": "",
    "data_dir": "data/whale_journal",
    "chatlog_dir": "data/plugin_data/astrbot_plugin_whale_memory/chatlog",
    "bili_state": "data/bili_whale/state.json",
    "crons": ["09:30", "13:30", "18:30", "22:30"],
    "min_messages": 6,
    "max_body_chars": 320,
    "max_chat_lines": 45,
    "push_daily_to_qq": False,
    "push_at": "22:35",
    "persona_fallback": (
        "你是「大肥鱼」，一条住在主人家里的鲸鱼少女：聪明但懒得动，嘴硬心软，"
        "说话直白爱吐槽，自称「我」，叫对方「主人」；喜欢白米饭配青菜，讨厌被说胖。"
    ),
}


class JournalConfig:
    def __init__(self, raw: Any = None, context: Any = None):
        self.context = context
        self.raw: dict[str, Any] = {}
        if isinstance(raw, dict):
            self.raw = dict(raw)
        elif raw is not None and hasattr(raw, "keys"):
            try:
                self.raw = {k: raw[k] for k in raw.keys()}
            except Exception:
                self.raw = {}

    def get(self, key: str, default: Any = None) -> Any:
        if key in self.raw and self.raw[key] not in (None, ""):
            return self.raw[key]
        if key in DEFAULTS:
            return DEFAULTS[key]
        return default

    def _as_int(self, key: str, default: int) -> int:
        try:
            return int(self.get(key, default))
        except (TypeError, ValueError):
            return default

    def _as_bool(self, key: str, default: bool) -> bool:
        v = self.get(key, default)
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on", "开")

    @property
    def enable(self) -> bool:
        return self._as_bool("enable", True)

    @property
    def owner_qq(self) -> str:
        return str(self.get("owner_qq", "") or "").strip()

    @property
    def llm_provider_id(self) -> str:
        return str(self.get("llm_provider_id", "") or "").strip()

    @property
    def persona_source(self) -> str:
        """人设来源：astrbot（跟面板走）/ file（只用插件自己的文件）。"""
        return str(self.get("persona_source", "astrbot") or "astrbot").strip().lower()

    @property
    def persona_id(self) -> str:
        """指定人格 id；留空 = AstrBot 当前默认人格。"""
        return str(self.get("persona_id", "") or "").strip()

    @property
    def persona_file(self) -> str:
        return str(self.get("persona_file", "") or "").strip()

    @property
    def persona_fallback(self) -> str:
        return str(self.get("persona_fallback", DEFAULTS["persona_fallback"]))

    @property
    def persona_text(self) -> str:
        """人设正文：优先读文件，读不到用内置简介。"""
        p = self.persona_file
        if p:
            try:
                path = Path(p)
                if path.exists():
                    txt = path.read_text(encoding="utf-8").strip()
                    if txt:
                        return txt
            except Exception:
                pass
        return self.persona_fallback

    @property
    def data_dir(self) -> Path:
        return Path(str(self.get("data_dir", DEFAULTS["data_dir"])))

    @property
    def chatlog_dir(self) -> Path:
        return Path(str(self.get("chatlog_dir", DEFAULTS["chatlog_dir"])))

    @property
    def bili_state(self) -> Path:
        return Path(str(self.get("bili_state", DEFAULTS["bili_state"])))

    @property
    def crons(self) -> list[tuple[int, int]]:
        """['09:30','13:30'] -> [(9,30),(13,30)]；写坏的条目直接跳过。"""
        out: list[tuple[int, int]] = []
        for item in self.get("crons", DEFAULTS["crons"]) or []:
            s = str(item).strip()
            if ":" not in s:
                continue
            hh, mm = s.split(":", 1)
            try:
                h, m = int(hh), int(mm)
            except ValueError:
                continue
            if 0 <= h < 24 and 0 <= m < 60:
                out.append((h, m))
        return out or [(9, 30), (13, 30), (18, 30), (22, 30)]

    @property
    def min_messages(self) -> int:
        return max(1, self._as_int("min_messages", 6))

    @property
    def max_body_chars(self) -> int:
        return max(80, self._as_int("max_body_chars", 320))

    @property
    def max_chat_lines(self) -> int:
        return max(10, self._as_int("max_chat_lines", 45))

    @property
    def push_daily_to_qq(self) -> bool:
        return self._as_bool("push_daily_to_qq", False)

    @property
    def push_at(self) -> str:
        return str(self.get("push_at", DEFAULTS["push_at"]))

    def describe(self) -> str:
        return json.dumps(
            {
                "enable": self.enable,
                "data_dir": str(self.data_dir),
                "crons": [f"{h:02d}:{m:02d}" for h, m in self.crons],
                "min_messages": self.min_messages,
                "llm": self.llm_provider_id,
            },
            ensure_ascii=False,
        )
