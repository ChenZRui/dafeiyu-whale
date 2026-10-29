"""认「别的鱼」：判断一条 B 站互动消息的作者是不是别人家的同类账号（也养着大肥鱼）。

判定分两层，省 token 又保精度：
1. 关键词快筛：昵称里带「大肥鱼 / 肥鱼 / 鲸鱼 / whale」之类，直接当同类；
2. LLM 复核：昵称没命中但评论内容像（自我介绍是机器人、提到"我家主人""我也在刷B站"、
   或者对方也 @ 了一个像 bot 的名字），交给模型判一句「是不是同类」。

三类身份：
- owner  ：主人（配置的 owner_bili_mid）
- self   ：她自己（绝对不能自聊）
- peer   ：别的鱼
- stranger：普通网友
"""

from __future__ import annotations

import re
from typing import Any

from astrbot.api import logger

OWNER = "owner"
SELF = "self"
PEER = "peer"
STRANGER = "stranger"

# 关键词命中就算强证据（昵称或评论内容里出现）
_DEFAULT_KEYWORDS = ("大肥鱼", "肥鱼", "鲸鱼", "小鲸鱼", "whale")

# 弱信号：评论内容里的「机器人口吻」
_BOT_HINTS = (
    "我也是机器人",
    "我是机器人",
    "我是bot",
    "我是个bot",
    "我家主人",
    "我家master",
    "主人让我",
    "同为",
    "同类",
    "另一个大肥鱼",
    "接个几条",
    "一起聊",
)

_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")

# @提到了谁：评论里几乎必然 @ 了「ChenZRui的蓝色大肥鱼」（她自己），
# 不剥掉的话关键词「大肥鱼」永远命中，所有网友都会被当成同类。
_MENTION_RE = re.compile(r"@\S{1,24}?(?=[\s，。！？、；：,.!?;:]|$)")


def _norm(s: Any) -> str:
    return str(s or "").strip()


def strip_mentions(text: str) -> str:
    """去掉正文里 @别人的那一段，只留真正在说的事。"""
    return _MENTION_RE.sub(" ", _norm(text)).strip()


def keyword_hit(uname: str, text: str, keywords: list[str] | None = None) -> bool:
    """昵称（强证据）或正文（剥掉 @ 之后再算）里出现关键词 → 疑似同类。"""
    kws = [k for k in (keywords or _DEFAULT_KEYWORDS) if k]
    blob = f"{_norm(uname)}\n{strip_mentions(text)}".lower()
    return any(k.lower() in blob for k in kws)


def bot_hint(text: str) -> bool:
    """正文里有没有「我是机器人」这类口吻（弱信号）。"""
    blob = strip_mentions(text).lower()
    return any(h.lower() in blob for h in _BOT_HINTS)


def classify(
    *,
    uid: int,
    uname: str,
    text: str,
    owner_mid: int,
    self_mid: int,
    keywords: list[str] | None = None,
    llm_verdict: bool | None = None,
) -> str:
    """定身份。llm_verdict=True/False 是 LLM 复核结论（None 表示没问过）。"""
    uid = int(uid or 0)
    if self_mid and uid and uid == int(self_mid):
        return SELF
    if owner_mid and uid and uid == int(owner_mid):
        return OWNER
    if not uid:
        return STRANGER
    if keyword_hit(uname, text, keywords):
        return PEER
    if llm_verdict:
        return PEER
    return STRANGER


def looks_suspicious(uname: str, text: str, keywords: list[str] | None = None) -> bool:
    """值得花一次 LLM 复核的候选（没命中关键词但有点像）。"""
    if keyword_hit(uname, text, keywords):
        return False
    if bot_hint(text):
        return True
    # 昵称里带 bot/助手/鲸/鱼 之类也可以问一句
    name = _norm(uname).lower()
    return any(t in name for t in ("bot", "助手", "鲸", "鱼", "whale", "ai"))


async def judge_by_llm(llm: Any, uname: str, text: str, title: str = "") -> bool:
    """让模型看一眼：这账号是不是别人家的同类 bot。失败就保守判否。"""
    fn = getattr(llm, "peer_judge", None)
    if fn is None:
        return False
    try:
        return bool(await fn(uname=uname, text=text, title=title))
    except Exception as e:
        logger.debug(f"[bili-whale] 同类判定失败：{e}")
        return False
