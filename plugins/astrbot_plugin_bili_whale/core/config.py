"""插件配置：把 AstrBot 的配置对象包一层，带默认值与类型兜底。

其它模块统一拿到一个 PluginConfig 实例，按属性名取值即可，不需要再关心
AstrBotConfig / dict / 缺省值这些细节。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from astrbot.api import logger

PLUGIN_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PERSONA_FILE = PLUGIN_DIR / "core" / "persona_default.md"

# 字符串写法 "07:00-09:00:2" / "07:00~09:00x2"
_WINDOW_RE = re.compile(r"^(\d{1,2}:\d{2})\s*[-~到]\s*(\d{1,2}:\d{2})\s*[:xX×*]\s*(\d+)$")

# 和 _conf_schema.json 保持一致；schema 缺字段时用这里的兜底
DEFAULTS: dict[str, Any] = {
    "enable": True,
    "owner_qq": "",
    "group_ids": [],
    "group_share_enable": True,
    "share_via": "comment",
    "owner_bili_mid": 413116249,
    "self_bili_mid": 3747575163260031,
    "bili_credential_file": "/opt/bili-ai/credentials.json",
    "day_random": False,
    "plan_cron": "0 5 * * *",
    "day_windows": [
        {"start": "07:00", "end": "09:00", "count": 2},
        {"start": "12:00", "end": "14:00", "count": 2},
        {"start": "18:00", "end": "22:00", "count": 3},
    ],
    "day_window": ["09:00", "23:00"],
    "day_slots_min": 3,
    "day_slots_max": 4,
    "day_slot_gap_min": 90,
    "night_cron": "0 19 * * *",
    "night_runs": 3,
    "run_interval_min": 35,
    "run_interval_jitter": 12,
    "max_videos": 5,
    "topic_pool_size": 10,
    "deep_read": 2,
    "min_score": 13,
    "deep_read_only_shared": False,
    "like_enable": True,
    "follow_enable": False,
    "keywords": ["DeepSeek Harness", "AI 编程", "CS2 道具教学", "猫片", "开源项目"],
    "like_topics": [
        "AI工具与插件",
        "反恐精英CS2道具教学和比赛",
        "猫片",
        "github高star开源项目",
        "各地风景",
    ],
    "dislike_topics": ["带货", "擦边", "标题党", "纯搬运", "鬼畜"],
    "share_daily_quota": 3,
    "qq_share_enable": True,
    "qq_share_max_chars": 800,
    "diary_enable": True,
    "diary_dir": "/opt/astrbot/data/bili_whale",
    "diary_push": True,
    "comment_digest_enable": True,
    "at_listen_enable": True,
    "dm_listen_enable": True,
    "inbox_interval_min": 30,
    # ---- 评论降频 / 收藏 / 投币（养号，别太像脚本）----
    "comment_probability": 0.45,
    "comment_strip_links": True,
    "fav_enable": True,
    "fav_folder_id": 0,
    "fav_min_score": 15,
    "coin_enable": True,
    "coin_daily_max": 1,
    "coin_min_stars": 1,
    # ---- 评论区互动（@我 / 回复我的）----
    "comment_reply_enable": True,
    "reply_owner": True,
    "reply_stranger": True,
    "reply_delay_sec": 0,
    # ---- 别的鱼（别人家的同类 bot）----
    "peer_enable": True,
    "peer_keywords": ["大肥鱼", "肥鱼", "鲸鱼", "小鲸鱼", "whale"],
    "peer_llm_judge": True,
    "peer_daily_limit": 6,
    "peer_thread_max_turns": 6,
    "peer_proactive": False,
    # ---- 私信双向 ----
    "dm_auto_reply": True,
    "dm_reply_stranger": False,
    "dm_reply_delay_sec": 0,
    "dm_max_per_tick": 3,
    "dm_context_turns": 6,
    # ---- 观看历史 / 主人打分 ----
    "watch_page_enable": True,
    "watch_page_name": "watch.html",
    "watch_page_limit": 80,
    "feedback_boost": 3.0,
    "feedback_penalty": 5.0,
    # ---- 打分台（独立小服务）+ 每天定时把打分文件发到 QQ ----
    "watchui_url": "http://127.0.0.1:18900",
    "watchui_token_file": "/opt/bili-watch-ui/token.txt",
    "daily_push_enable": True,
    "daily_push_cron": "0 21 * * *",
    "daily_push_file": "大肥鱼看过什么.html",
    "daily_push_link": True,
    "daily_push_send_file": True,
    "topic_weight_enable": True,
    # 私信分享视频时发「视频卡片」（不是一条带网址的文本）；失败自动退回文本
    "dm_share_card_enable": False,
    # 私信往来是否复述到 QQ（默认关：主人自己在B站看得到）
    "dm_report_to_qq": False,
    # 主人在 B站 私信里推视频过来时，她要不要「看完并回一段评价」
    "dm_video_review": True,
    "llm_provider_id": "deepseek/deepseek-flash",
    # astrbot=用 AstrBot 面板里当前生效的人格（推荐）；file=只用下面的 persona_file
    "persona_source": "astrbot",
    "persona_id": "",
    "persona_file": "",
    "subtitle_max_chars": 6000,
    "quiet_hours": [],
    "debug": False,
}


class PluginConfig:
    """配置包装。属性名直接对应 _conf_schema.json 的 key。"""

    def __init__(self, raw: Any = None, context: Any = None):
        self.context = context
        self.raw = dict(raw) if isinstance(raw, dict) else {}
        # AstrBotConfig 支持 .get，但不保证是 dict
        if not self.raw and raw is not None and hasattr(raw, "keys"):
            try:
                self.raw = {k: raw[k] for k in raw.keys()}
            except Exception:
                self.raw = {}

    # ---------- 基础读取 ----------
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
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)

    def _as_list(self, key: str, default: list) -> list:
        v = self.get(key, default)
        if isinstance(v, str):
            v = [x.strip() for x in v.replace("，", ",").split(",") if x.strip()]
        if not isinstance(v, list):
            return list(default)
        return v

    # ---------- 属性 ----------
    @property
    def enable(self) -> bool:
        return self._as_bool("enable", True)

    @property
    def debug(self) -> bool:
        return self._as_bool("debug", False)

    @property
    def owner_qq(self) -> str:
        """主人 QQ。配置为空时回退到 AstrBot 全局 admins_id。"""
        v = str(self.get("owner_qq", "") or "").strip()
        if v.isdigit():
            return v
        try:
            cfg = self.context.get_config() if self.context else None
            admins = list((cfg or {}).get("admins_id") or [])
            for a in admins:
                s = str(a).strip()
                if s.isdigit() and s != "0":
                    return s
        except Exception as e:
            logger.warning(f"[bili-whale] 读取 admins_id 失败：{e}")
        return ""

    @property
    def group_ids(self) -> list[str]:
        out: list[str] = []
        for g in self._as_list("group_ids", []):
            s = str(g).strip().strip('"').strip("'")
            if s.isdigit():
                out.append(s)
        return out

    @property
    def group_share_enable(self) -> bool:
        return self._as_bool("group_share_enable", True)

    @property
    def share_via(self) -> str:
        v = str(self.get("share_via", "comment") or "comment").strip().lower()
        return v if v in ("comment", "dm", "both", "off") else "comment"

    @property
    def owner_bili_mid(self) -> int:
        return self._as_int("owner_bili_mid", 413116249)

    @property
    def self_bili_mid(self) -> int:
        return self._as_int("self_bili_mid", 3747575163260031)

    @property
    def feed_homepage(self) -> bool:
        """选题主源用B站首页推荐流（登录态个性化，最像真人在刷）。"""
        return self._as_bool("feed_homepage", True)

    @property
    def homepage_limit(self) -> int:
        return max(5, min(50, self._as_int("homepage_limit", 24)))

    @property
    def bili_credential_file(self) -> str:
        return str(self.get("bili_credential_file", "/opt/bili-ai/credentials.json"))

    @property
    def night_cron(self) -> str:
        return str(self.get("night_cron", "0 19 * * *")).strip()

    # ---------- 日间随机行程 ----------
    @property
    def day_random(self) -> bool:
        """true=一天里随机时段醒来几次（推荐）；false=老夜场模式。"""
        return self._as_bool("day_random", False)

    @property
    def plan_cron(self) -> str:
        """每天几点生成当天行程（随机时刻由它触发后算出）。"""
        return str(self.get("plan_cron", "0 5 * * *")).strip()

    @property
    def day_windows(self) -> list[tuple[str, str, int]]:
        """分时段随机：[(开始, 结束, 抽几次), ...]。

        支持两种写法：
        - `[{"start":"07:00","end":"09:00","count":2}, ...]`（推荐）
        - `["07:00-09:00:2", ...]`
        没配时退回老的 day_window + day_slots_min/max。
        """
        raw = self.raw.get("day_windows")
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
        if not isinstance(raw, list):
            raw = []
        if not raw:
            # 兼容老配置：单个窗口 + 次数上限
            w = self.day_window
            if len(w) >= 2:
                return [(w[0], w[1], self.day_slots_max)]
            return []
        out: list[tuple[str, str, int]] = []
        for item in raw:
            if isinstance(item, dict):
                s = str(item.get("start", "")).strip()
                e = str(item.get("end", "")).strip()
                try:
                    c = int(item.get("count", 1))
                except Exception:
                    c = 1
            else:
                txt = str(item).strip()
                m = _WINDOW_RE.match(txt)
                if not m:
                    continue
                s, e = m.group(1), m.group(2)
                try:
                    c = int(m.group(3))
                except Exception:
                    c = 1
            if ":" in s and ":" in e:
                out.append((s, e, max(1, c)))
        return out

    @property
    def day_windows_text(self) -> str:
        wins = self.day_windows
        if not wins:
            return "未配置"
        return "，".join(f"{s}~{e}×{c}" for s, e, c in wins)

    @property
    def day_window(self) -> list[str]:
        """随机时刻允许落在的窗口，如 ["09:00", "23:00"]。"""
        v = self._as_list("day_window", DEFAULTS["day_window"])
        return [str(x).strip() for x in v if str(x).strip()][:2]

    @property
    def day_window_text(self) -> str:
        w = self.day_window
        return f"{w[0]}~{w[1]}" if len(w) >= 2 else "全天"

    @property
    def day_slots_min(self) -> int:
        return max(1, self._as_int("day_slots_min", 3))

    @property
    def day_slots_max(self) -> int:
        return max(self.day_slots_min, self._as_int("day_slots_max", 4))

    @property
    def day_slot_gap_min(self) -> int:
        return max(5, self._as_int("day_slot_gap_min", 90))

    @property
    def night_runs(self) -> int:
        return max(1, self._as_int("night_runs", 3))

    @property
    def run_interval_min(self) -> int:
        return max(1, self._as_int("run_interval_min", 35))

    @property
    def run_interval_jitter(self) -> int:
        return max(0, self._as_int("run_interval_jitter", 12))

    @property
    def max_videos(self) -> int:
        return max(1, self._as_int("max_videos", 5))

    @property
    def topic_pool_size(self) -> int:
        """交给 LLM 打分的候选条数。太少会被首页推荐（数码/手机）垄断。"""
        return max(4, self._as_int("topic_pool_size", 10))

    @property
    def deep_read(self) -> int:
        return max(1, self._as_int("deep_read", 2))

    @property
    def min_score(self) -> int:
        return self._as_int("min_score", 13)

    @property
    def deep_read_only_shared(self) -> bool:
        return self._as_bool("deep_read_only_shared", False)

    @property
    def like_enable(self) -> bool:
        return self._as_bool("like_enable", True)

    @property
    def follow_enable(self) -> bool:
        return self._as_bool("follow_enable", False)

    @property
    def keywords(self) -> list[str]:
        return [str(x) for x in self._as_list("keywords", DEFAULTS["keywords"])]

    @property
    def like_topics(self) -> list[str]:
        return [str(x) for x in self._as_list("like_topics", DEFAULTS["like_topics"])]

    @property
    def dislike_topics(self) -> list[str]:
        return [str(x) for x in self._as_list("dislike_topics", DEFAULTS["dislike_topics"])]

    @property
    def share_daily_quota(self) -> int:
        return max(0, self._as_int("share_daily_quota", 3))

    @property
    def qq_share_enable(self) -> bool:
        return self._as_bool("qq_share_enable", True)

    @property
    def qq_share_max_chars(self) -> int:
        return max(120, self._as_int("qq_share_max_chars", 800))

    @property
    def diary_enable(self) -> bool:
        return self._as_bool("diary_enable", True)

    @property
    def data_dir(self) -> Path:
        p = Path(str(self.get("diary_dir", "/opt/astrbot/data/bili_whale")))
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"[bili-whale] 建目录失败 {p}: {e}")
        return p

    @property
    def diary_push(self) -> bool:
        return self._as_bool("diary_push", True)

    @property
    def comment_digest_enable(self) -> bool:
        return self._as_bool("comment_digest_enable", True)

    @property
    def at_listen_enable(self) -> bool:
        return self._as_bool("at_listen_enable", True)

    @property
    def dm_listen_enable(self) -> bool:
        return self._as_bool("dm_listen_enable", True)

    @property
    def inbox_interval_min(self) -> int:
        return max(5, self._as_int("inbox_interval_min", 30))

    # ---------- 评论降频 / 收藏 / 投币 ----------
    @property
    def comment_probability(self) -> float:
        try:
            v = float(self.get("comment_probability", 0.45))
        except Exception:
            v = 0.45
        return max(0.0, min(1.0, v))

    @property
    def comment_strip_links(self) -> bool:
        return self._as_bool("comment_strip_links", True)

    @property
    def fav_enable(self) -> bool:
        return self._as_bool("fav_enable", True)

    @property
    def fav_folder_id(self) -> int:
        return max(0, self._as_int("fav_folder_id", 0))

    @property
    def fav_min_score(self) -> int:
        return self._as_int("fav_min_score", 15)

    @property
    def coin_enable(self) -> bool:
        return self._as_bool("coin_enable", True)

    @property
    def coin_daily_max(self) -> int:
        return max(0, self._as_int("coin_daily_max", 1))

    @property
    def coin_min_stars(self) -> int:
        return max(1, min(5, self._as_int("coin_min_stars", 1)))

    # ---------- 打分台 / 每日推送 ----------
    @property
    def watchui_url(self) -> str:
        return str(self.get("watchui_url", "http://127.0.0.1:18900") or "").rstrip("/")

    @property
    def watchui_token_file(self) -> Path:
        return Path(str(self.get("watchui_token_file", "/opt/bili-watch-ui/token.txt")))

    @property
    def daily_push_enable(self) -> bool:
        return self._as_bool("daily_push_enable", True)

    @property
    def daily_push_cron(self) -> str:
        raw = str(self.get("daily_push_cron", "0 21 * * *") or "").strip()
        return raw or "0 21 * * *"

    @property
    def daily_push_link(self) -> bool:
        """每天推送时先单发一条「手机能直接打开的链接」。"""
        return self._as_bool("daily_push_link", True)

    @property
    def daily_push_send_file(self) -> bool:
        """是否还附带发那份离线 HTML 文件。"""
        return self._as_bool("daily_push_send_file", True)

    @property
    def daily_push_file(self) -> str:
        return str(self.get("daily_push_file", "大肥鱼看过什么.html") or "大肥鱼看过什么.html")

    @property
    def dm_share_card_enable(self) -> bool:
        """私信里分享视频时，要不要先发一张封面图。

        默认 False = 只发文字（标题 + 链接），点一下就能进视频；
        发图虽然像卡片，但手机上点不开，主人还得自己去搜。
        """
        return self._as_bool("dm_share_card_enable", False)

    @property
    def dm_video_review(self) -> bool:
        """主人私信推视频来时，她看完了要不要回一段评价。"""
        return self._as_bool("dm_video_review", True)

    @property
    def dm_report_to_qq(self) -> bool:
        """B站私信来回要不要再往 QQ 复述一遍（默认不复述）。"""
        return self._as_bool("dm_report_to_qq", False)

    @property
    def topic_weight_enable(self) -> bool:
        return self._as_bool("topic_weight_enable", True)

    # ---------- 评论区互动 ----------
    @property
    def comment_reply_enable(self) -> bool:
        return self._as_bool("comment_reply_enable", True)

    @property
    def reply_owner(self) -> bool:
        return self._as_bool("reply_owner", True)

    @property
    def reply_stranger(self) -> bool:
        return self._as_bool("reply_stranger", True)

    @property
    def reply_delay_sec(self) -> int:
        return max(0, min(300, self._as_int("reply_delay_sec", 0)))

    # ---------- 别的鱼 ----------
    @property
    def peer_enable(self) -> bool:
        return self._as_bool("peer_enable", True)

    @property
    def peer_keywords(self) -> list[str]:
        raw = self._as_list("peer_keywords", ["大肥鱼", "肥鱼", "鲸鱼", "小鲸鱼", "whale"])
        return [str(x).strip() for x in raw if str(x).strip()]

    @property
    def peer_llm_judge(self) -> bool:
        return self._as_bool("peer_llm_judge", True)

    @property
    def peer_daily_limit(self) -> int:
        return max(0, self._as_int("peer_daily_limit", 6))

    @property
    def peer_thread_max_turns(self) -> int:
        return max(2, self._as_int("peer_thread_max_turns", 6))

    @property
    def peer_proactive(self) -> bool:
        return self._as_bool("peer_proactive", False)

    # ---------- 私信双向 ----------
    @property
    def dm_auto_reply(self) -> bool:
        return self._as_bool("dm_auto_reply", True)

    @property
    def dm_reply_stranger(self) -> bool:
        return self._as_bool("dm_reply_stranger", False)

    @property
    def dm_reply_delay_sec(self) -> int:
        return max(0, min(300, self._as_int("dm_reply_delay_sec", 0)))

    @property
    def dm_max_per_tick(self) -> int:
        return max(1, min(10, self._as_int("dm_max_per_tick", 3)))

    @property
    def dm_context_turns(self) -> int:
        return max(1, min(20, self._as_int("dm_context_turns", 6)))

    # ---------- 观看历史 / 打分 ----------
    @property
    def watch_page_enable(self) -> bool:
        return self._as_bool("watch_page_enable", True)

    @property
    def watch_page_name(self) -> str:
        return str(self.get("watch_page_name", "watch.html") or "watch.html").strip() or "watch.html"

    @property
    def watch_page_limit(self) -> int:
        return max(10, min(400, self._as_int("watch_page_limit", 80)))

    @property
    def feedback_boost(self) -> float:
        try:
            return float(self.get("feedback_boost", 3.0))
        except Exception:
            return 3.0

    @property
    def feedback_penalty(self) -> float:
        try:
            return float(self.get("feedback_penalty", 5.0))
        except Exception:
            return 5.0

    @property
    def llm_provider_id(self) -> str:
        return str(self.get("llm_provider_id", "") or "").strip()

    @property
    def subtitle_max_chars(self) -> int:
        return max(500, self._as_int("subtitle_max_chars", 6000))

    @property
    def quiet_hours(self) -> list[str]:
        raw = self._as_list("quiet_hours", [])
        return [str(x).strip() for x in raw if str(x).strip()]

    # ---------- 人设 ----------
    @property
    def persona_source(self) -> str:
        """人设来源：astrbot（跟 AstrBot 面板走）/ file（只读插件配置的文件）。"""
        return str(self.get("persona_source", "astrbot") or "astrbot").strip().lower()

    @property
    def persona_id(self) -> str:
        """指定用哪个人格；留空 = AstrBot 当前默认人格。"""
        return str(self.get("persona_id", "") or "").strip()

    @property
    def persona_file(self) -> str:
        return str(self.get("persona_file", "") or "").strip()

    @property
    def persona_text(self) -> str:
        """人格设定正文：配置指定的文件 > 内置 persona_default.md > 空。"""
        path_s = str(self.get("persona_file", "") or "").strip()
        if path_s:
            try:
                return Path(path_s).read_text(encoding="utf-8")
            except Exception as e:
                logger.warning(f"[bili-whale] 读人设文件失败 {path_s}: {e}")
        try:
            if DEFAULT_PERSONA_FILE.exists():
                return DEFAULT_PERSONA_FILE.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"[bili-whale] 读内置人设失败：{e}")
        return ""

    def dump(self) -> str:
        """给 /刷B站 状态命令看的精简配置。"""
        return json.dumps(
            {
                "enable": self.enable,
                "owner_qq": self.owner_qq,
                "group_ids": self.group_ids,
                "share_via": self.share_via,
                "night_cron": self.night_cron,
                "night_runs": self.night_runs,
                "max_videos": self.max_videos,
                "topic_pool_size": self.topic_pool_size,
                "min_score": self.min_score,
                "diary_dir": str(self.data_dir),
            },
            ensure_ascii=False,
            indent=2,
        )
