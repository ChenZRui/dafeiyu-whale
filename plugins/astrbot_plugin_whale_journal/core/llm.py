"""日记本的 LLM 层：把这段时间的素材交给她自己，让她用自己的口吻写一段。

provider 解析照抄 bili_whale 的做法：先 `context.llm_generate(chat_provider_id=...)`，
拿不到再回退 AstrBot 当前默认 provider 的 `text_chat`。
"""

from __future__ import annotations

import json
import re
from typing import Any

from astrbot.api import logger

_HARD_RULES = """写作硬规矩：
- 全部用中文，第一人称「我」。
- 这是写给自己看的本子，不是汇报：不要罗列清单、不要总结"今天学到了…"这种套话。
- 可以提到具体的人（主人、群友）和具体的视频名，但别把素材原样抄一遍。
- 不要 emoji，不要颜文字，不要写日期。
- 只输出 JSON，不要任何解释、不要 markdown 代码块。"""


def _strip_fence(txt: str) -> str:
    t = (txt or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _resp_text(resp: Any) -> str:
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp.strip()
    for attr in ("completion_text", "text", "content", "message"):
        v = getattr(resp, attr, None)
        if isinstance(v, str) and v.strip():
            return v.strip()
    try:
        return str(resp).strip()
    except Exception:
        return ""


def _persona_prompt(obj: Any) -> str:
    """AstrBot 的人格对象/dict -> 纯 prompt 正文。"""
    if not obj:
        return ""
    if isinstance(obj, dict):
        for key in ("prompt", "system_prompt", "content"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""
    for attr in ("prompt", "system_prompt", "content"):
        val = getattr(obj, attr, None)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


class JournalLLM:
    def __init__(self, context: Any, cfg):
        self.context = context
        self.cfg = cfg
        self._persona: str | None = None

    @property
    def persona(self) -> str:
        """已加载的人设（同步读缓存；真正的加载在 complete() 里异步做）。"""
        return self._persona or ""

    async def _ensure_persona(self) -> str:
        """优先用 AstrBot 面板里当前生效的人格，取不到才退回插件自己的文件/内置简介。"""
        if self._persona is not None:
            return self._persona
        text = ""
        src = str(self.cfg.persona_source or "astrbot").strip().lower()
        if src not in ("file", "off", "local"):
            text = await self._persona_from_astrbot()
        if not text:
            try:
                text = self.cfg.persona_text or ""
            except Exception as e:
                logger.warning(f"[whale-journal] 读人设失败：{e}")
        self._persona = text or ""
        origin = "AstrBot 人格" if src not in ("file", "off", "local") else "插件文件/内置"
        if origin == "AstrBot 人格" and text and text == (self.cfg.persona_text or ""):
            origin = "插件文件/内置（AstrBot 没取到）"
        logger.info("[whale-journal] 人设来源：%s（%d 字）" % (origin, len(self._persona)))
        return self._persona

    async def _persona_from_astrbot(self) -> str:
        pm = getattr(self.context, "persona_manager", None)
        if pm is None:
            return ""
        pid = str(self.cfg.persona_id or "").strip()
        if pid:
            try:
                obj = getattr(pm, "get_persona_v3_by_id", None)
                obj = obj(pid) if callable(obj) else None
                if obj is None:
                    getter = getattr(pm, "get_persona", None)
                    if callable(getter):
                        obj = await getter(pid)
                txt = _persona_prompt(obj)
                if txt:
                    return txt
            except Exception as e:
                logger.warning(f"[whale-journal] 读指定人格 {pid} 失败：{e}")
        try:
            getter = getattr(pm, "get_default_persona_v3", None)
            if callable(getter):
                return _persona_prompt(await getter())
        except Exception as e:
            logger.warning(f"[whale-journal] 读 AstrBot 默认人格失败：{e}")
        return ""

    async def _with_persona(self, system: str) -> str:
        persona = await self._ensure_persona()
        return f"{persona}\n\n{system}" if persona else system

    async def complete(self, system: str, prompt: str) -> str:
        provider_id = self.cfg.llm_provider_id
        if provider_id:
            gen = getattr(self.context, "llm_generate", None)
            if callable(gen):
                try:
                    resp = await gen(
                        chat_provider_id=provider_id, prompt=prompt, system_prompt=system
                    )
                    out = _resp_text(resp)
                    if out:
                        return out
                    logger.warning(f"[whale-journal] llm_generate 返回空（{provider_id}），回退默认 provider")
                except Exception as e:
                    logger.warning(f"[whale-journal] llm_generate 失败（{provider_id}）：{e}，回退默认 provider")
        prov = None
        getter = getattr(self.context, "get_using_provider_async", None)
        if callable(getter):
            try:
                prov = await getter()
            except Exception:
                prov = None
        if prov is None:
            getter2 = getattr(self.context, "get_using_provider", None)
            if callable(getter2):
                try:
                    prov = getter2()
                except Exception:
                    prov = None
        if prov is None:
            logger.warning("[whale-journal] 没有可用的 LLM provider，这条日记跳过")
            return ""
        try:
            resp = await prov.text_chat(system_prompt=system, prompt=prompt)
        except Exception as e:
            logger.warning(f"[whale-journal] text_chat 失败：{e}")
            return ""
        return _resp_text(resp)

    # ------------------------------------------------------------ 主入口
    async def write_entry(self, *, now_hm: str, chat: list[dict], videos: list[dict],
                          mistakes: list[str], max_chars: int) -> dict | None:
        """让 LLM 写一条日记，返回 {mood, title, body}；失败返回 None。"""
        # 每次写日记都重读一次人格：主人在面板里改人设不用重启插件
        self._persona = None
        chat_lines = "\n".join(
            f"[{c['hm']}·{c['where']}] {c['who']}：{c['text'][:120]}" for c in chat
        ) or "（这段时间没什么人说话）"
        video_lines = "\n".join(
            f"- {v['title']}（up：{v['up'] or '未知'}）" for v in videos
        ) or "（这段时间没刷视频）"
        mistake_lines = "\n".join(f"- {m}" for m in mistakes) or "（没有明显的）"

        prompt = f"""现在是今天的 {now_hm}。把这段时间里真实发生的事，写成你日记本上的一段话。

【这段时间和人的对话（按时间顺序，节选）】
{chat_lines}

【这段时间你在 B 站刷到/看过的视频】
{video_lines}

【可能让你被吐槽或犯错的地方（仅供参考，没有就不用提）】
{mistake_lines}

写的时候：
- 心情标签：2-6 个字，要具体（比如「有点想笑」「踏实」「有点得意」「有点心虚」「烦」）。
- 正文 60-200 字，像随手记在本子上的一小段。挑最有感觉的一两件事写，别面面俱到。
- 视频可以只提一个名字或一句观感，对话可以只提一句被戳到的话。
- 如果是你自己的小失误、被主人纠正了，就老老实实写下来（可以带点不服气或者心虚）。

只输出 JSON：{{"mood": "...", "title": "...", "body": "..."}}
title 是给这段起的一句话小标题（10 字以内），可以留空。"""

        system = await self._with_persona(_HARD_RULES)
        raw = await self.complete(system, prompt)
        if not raw:
            return None
        data = self._parse(raw)
        if data is None:
            logger.warning(f"[whale-journal] LLM 输出不是 JSON，按纯文本处理：{raw[:120]}")
            body = raw.strip()
            if not body:
                return None
            return {"mood": "", "title": "", "body": body[:max_chars]}
        body = str(data.get("body") or "").strip()
        if not body:
            return None
        return {
            "mood": str(data.get("mood") or "").strip()[:12],
            "title": str(data.get("title") or "").strip()[:24],
            "body": body[:max_chars],
        }

    @staticmethod
    def _parse(raw: str) -> dict | None:
        txt = _strip_fence(raw)
        try:
            obj = json.loads(txt)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        m = re.search(r"\{.*\}", txt, re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict):
                    return obj
            except ValueError:
                return None
        return None
