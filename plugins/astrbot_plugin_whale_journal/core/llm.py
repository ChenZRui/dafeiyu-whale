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



def _salvage(txt: str) -> dict:
    """JSON 解析失败时的兜底：逐字段正则捞回来。

    模型偶尔会在 body 里写没转义的双引号，json.loads 直接失败；
    以前这种情况会把整段 JSON 原样当正文存进日记，页面上显示成一坨花括号。
    """
    raw = _strip_fence(txt)
    out: dict[str, str] = {}
    for key in ("mood", "title"):
        m = re.search(r'"%s"\s*:\s*"([^"]{1,40})"' % key, raw)
        if m:
            out[key] = m.group(1).strip()
    m = re.search(r'"body"\s*:\s*"([\s\S]*?)"\s*[,}]', raw)
    if m:
        body = m.group(1)
        body = body.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t")
        out["body"] = body.strip()
    return out


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
                          mistakes: list[str], max_chars: int,
                          lessons: list[dict] | None = None) -> dict | None:
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
        lesson_lines = "\n".join(
            f"- {l.get('hm','')}【{l.get('where','')}】{l.get('text','')}" for l in (lessons or [])
        ) or "（这段时间没教你什么新东西）"

        prompt = f"""现在是今天的 {now_hm}。把这段时间里真实发生的事，写成你日记本上的一段话。

【这段时间和人的对话（按时间顺序，节选）】
{chat_lines}

【这段时间你在 B 站刷到/看过的视频】
{video_lines}

【主人这段时间教你、给你立的规矩（最重要，必须写进去）】
{lesson_lines}

【可能让你被吐槽或犯错的地方（仅供参考，没有就不用提）】
{mistake_lines}

写的时候：
- 心情标签：2-6 个字，要具体（比如「有点想笑」「踏实」「有点得意」「有点心虚」「烦」）。
- 正文 60-200 字，像随手记在本子上的一小段。挑最有感觉的一两件事写，别面面俱到。
- 视频可以只提一个名字或一句观感，对话可以只提一句被戳到的话。
- **必须写一条你今天被主人教的东西**：他跟你说了什么规矩、要求、纠正（比如怎么回话、别做什么、以后要怎样），用你自己的话写进正文里，可以带一句你的态度（认下/不服/心里记着）。如果这段素材里真的没有教学内容，才跳过这条。
- 主人是在私聊里跟你说的（一对一），这部分要占正文的主要位置；群里别人说的话是背景。
- 如果是你自己的小失误、被主人纠正了，就老老实实写下来（可以带点不服气或者心虚）。

只输出 JSON：{{"mood": "...", "title": "...", "body": "..."}}
title 是给这段起的一句话小标题（10 字以内），可以留空。"""

        system = await self._with_persona(_HARD_RULES)
        raw = await self.complete(system, prompt)
        if not raw:
            return None
        data = self._parse(raw)
        if data is None and _strip_fence(raw).lstrip().startswith("{"):
            # 模型输出的是 JSON，但格式坏了（常见：body 里没转义双引号）
            data = _salvage(raw) or None
            if data:
                logger.info("[whale-journal] LLM 的 JSON 有问题，已按字段捞回")
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

    async def profile_owner(self, chat_lines: str, prev: dict | None = None) -> dict | None:
        """从最近的聊天里总结主人的画像（性格/习惯/口头禅/心情/睡眠）。"""
        prev = prev or {}
        prev_txt = json.dumps(
            {
                "personality": prev.get("personality") or [],
                "habits": prev.get("habits") or [],
                "catchphrases": prev.get("catchphrases") or [],
                "summary": prev.get("summary") or "",
            },
            ensure_ascii=False,
        )
        prompt = f"""你是「大肥鱼」，下面是最近你和主人（以及其他群友）的聊天记录。

【聊天记录（按时间顺序）】
{chat_lines}

【你之前对他的印象（有更新就改，没有就沿用）】
{prev_txt}

请只根据聊天记录里的**证据**，总结主人这个人。不要编，看不出来就留空。

输出 JSON：
{{
  "personality": ["性格特征，4-8 个字一条，3-6 条，如「嘴上硬心里软」「自嘲型」"],
  "habits": ["生活习惯，具体一点，2-6 条，如「凌晨两三点才睡」「边吃边聊」"],
  "catchphrases": ["他常说的话/口头禅，原样摘 1-5 条，没有就空数组"],
  "mood": {{"index": 1-10 的整数（今天整体心情，5=一般）, "label": "2-6 个字", "note": "一句依据"}},
  "sleep": {{"quality": "很好/还行/不太好/很差/不知道", "note": "从哪句话看出来的，一句"}},
  "summary": "这两三句话概括他最近这几天的状态和生活节奏（60-90 字，要说人话，见下方规矩）",
  "highlights": ["最近让你印象深的 1-3 件关于他的事，每条 15 字内"]
}}

【summary 的写法（重要）】
- 用现在通行的口语化书面中文，主谓宾顺过来，别倒装、别生造词。反面例子：「插件不忙了」「人松快下来了」这种主人看不懂的就算了，要写成「他最近不那么忙着折腾插件了，人明显松快下来，睡觉也早了」。
- 先说这段时间的整体节奏（忙不忙、累不累、心情松还是紧），再说 1-2 个具体表现（几点睡、在忙什么、说话的方式变了没）。
- 不要用「松快」「绷着」「膈应」这种半古半土的词；不要出现「总体来说」「综上所述」这类套话。
- 如果这段时间和之前明显不一样，就直接写清楚变化（比如「前几天熬到两三点，这两天十二点就睡了」）。

只输出 JSON。"""
        system = await self._with_persona(_HARD_RULES)
        raw = await self.complete(system, prompt)
        if not raw:
            return None
        return self._parse(raw)

    async def describe_lesson(self, context: str, text: str) -> dict:
        """把「一句话 + 前后对话」概括成 {title, gist}，给教学库当卡片标题和概述。

        title：6~14 字的小标题，像给这段聊天起名（例：康神开播了，真的假的）
        gist ：一到两句复述前因后果，写清谁说了什么、她该懂什么（≤120 字）
        """
        ctx = "\n".join(
            ln for ln in str(context or "").splitlines() if ln.strip()
        )[:1200]
        if not ctx:
            return {}
        prompt = f"""下面是一段聊天记录（主人和「大肥鱼」），其中有一句话被主人当成「要教给她的东西」。

【聊天记录】
{ctx}

【被收录的那句话】
{str(text or "")[:160]}

请输出 JSON：
{{"title": "给这段聊天起的短标题，6~14 字，像人话，不要书名号、不要句号",
  "gist": "一到两句复述这段对话的前因后果：主人说了什么、她该回什么/该懂什么；用「主人：…；大肥鱼：…」这种口吻，不要照抄整段，≤120 字"}}

例子：聊天是「主人：康神开播了 / 主人：你应该回真的假的 / 大肥鱼：真的假的（）行，是这个口令对吧」
则 title=「康神开播了，真的假的」，gist=「主人说康神开播了，教她这时候要接一句「真的假的」，她回：真的假的（）行，是这个口令对吧。」

只输出 JSON。"""
        system = await self._with_persona(_HARD_RULES)
        raw = await self.complete(system, prompt)
        if not raw:
            return {}
        txt = _strip_fence(raw)
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            return {}
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            return {}
        if not isinstance(obj, dict):
            return {}
        title = " ".join(str(obj.get("title") or "").split())[:24]
        gist = " ".join(str(obj.get("gist") or "").split())[:160]
        out: dict = {}
        if title:
            out["title"] = title
        if gist:
            out["gist"] = gist
        return out

    async def classify_lessons(self, lessons: list[dict]) -> list[dict]:
        """把「主人教的东西」分到黑话 / 梗 / 调情 / 其他。"""
        texts = [str(l.get("text") or "").strip() for l in (lessons or []) if str(l.get("text") or "").strip()]
        if not texts:
            return []
        listed = "\n".join(f"{i + 1}. {t[:160]}" for i, t in enumerate(texts))
        prompt = f"""下面是主人教「大肥鱼」的话。请把每一条分到下面四类之一：

- 黑话：圈内/网络用语、只有特定群体懂的说法（如「肝」「抽象」「典」）
- 梗：网络梗、流行语、动漫/游戏/直播里的名场面说法
- 调情：亲昵、撒娇、暧昧、带点小撩的话（如「想你了」「晚安宝宝」）
- 其他：规矩、要求、习惯、纠正，不属于上面三类的

【条目】
{listed}

输出 JSON 数组，按顺序，每条一个对象：
[{{"i": 1, "category": "黑话", "text": "原句（可精简到 40 字内）"}}]

category 只能是 黑话 / 梗 / 调情 / 其他 四个词之一。只输出 JSON。"""
        system = await self._with_persona(_HARD_RULES)
        raw = await self.complete(system, prompt)
        if not raw:
            return []
        txt = _strip_fence(raw)
        m = re.search(r"\[.*\]", txt, re.S)
        if not m:
            return []
        try:
            arr = json.loads(m.group(0))
        except ValueError:
            return []
        out: list[dict] = []
        for obj in arr if isinstance(arr, list) else []:
            if not isinstance(obj, dict):
                continue
            try:
                i = int(obj.get("i") or 0) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(texts):
                out.append({
                    "text": str(obj.get("text") or texts[i]).strip()[:160],
                    "category": str(obj.get("category") or "其他").strip(),
                })
        return out

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
