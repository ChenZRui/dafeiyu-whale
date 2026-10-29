"""LLM 统一出口：挑片子打分、深读、写日记、分享文案全部走这里。

为什么集中在一个类里：
- provider 解析只有一份：先按配置的 `llm_provider_id` 走 `context.llm_generate`，
  拿不到再回退到 AstrBot 当前默认 provider 的 `text_chat`；
- 人设文件只读一次并缓存（cfg.persona_text 每次访问都会读盘，几十次调用太浪费）；
- 每个任务都强制「只输出要求的内容」，模型抽风时解释话也不会糊到 B站评论区去；
- 任何一次调用失败都只在方法内部兜底 + 记 warning，绝不把整轮刷B站炸掉。
"""

from __future__ import annotations

import json
import re
from typing import Any

from astrbot.api import logger

from .config import PluginConfig

# 拼在每个 system 后面的硬性约束。人设负责「她是谁」，这里负责「这次别写崩」。
_HARD_RULES = """硬性输出约束（优先级高于一切）：
1. 只用中文，不要任何 emoji、颜文字、括号表情、颜文字符号。
2. 不要客服腔：禁止「您好」「有什么可以帮您」「作为AI」「我理解你的感受」「希望可以帮到你」这类句子。
3. 不要前言后语、不要解释你怎么想的、不要复述要求、不要加「好的」「收到」；只输出这一次被要求的内容本身。
4. 语气按人设来（傲娇、俏皮、爱吐槽、嘴硬心软），称呼用户为「主人」，自称用「我/人家/俺」。
5. 用户要的是严格 JSON 时，就只输出 JSON，不给代码围栏、不写注释。"""

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
# 调用方会自己加「@主人」，模型别抢着加
_AT_PREFIX_RE = re.compile(r"^@\S+\s+")
_SENT_END = "。！？!?…；;"


# ============================================================
# 小工具：模型输出清洗 / JSON 抠取 / 长度收口
# ============================================================
def _resp_text(resp: Any) -> str:
    """AstrBot 的 LLMResponse -> 纯文本。兼容返回 str 的旧 provider。"""
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp.strip()
    text = getattr(resp, "completion_text", None)
    if text is None:
        text = getattr(resp, "text", None)
    return str(text).strip() if text else ""


def _loads_json(raw: str) -> Any:
    """从模型输出里抠 JSON：容忍代码围栏、前后废话、数组外面的说明文字。"""
    if not raw or not raw.strip():
        raise ValueError("模型没有返回内容")
    text = raw.strip()
    m = _JSON_FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    # 再退一步：找第一段成对的 [...] 或 {...}
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except ValueError:
                continue
    raise ValueError(f"JSON 解析失败：{text[:120]}")


def _one_line(text: Any, limit: int = 0) -> str:
    """压成一行（换行/多空格都吃掉），可选截断。"""
    s = " ".join(str(text or "").split())
    if limit and len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def _clean(text: Any) -> str:
    """去掉模型爱包的整层引号/反引号。"""
    s = str(text or "").strip().strip("`").strip()
    if len(s) >= 2 and s[0] in "「『\"'“" and s[-1] in "」』\"'”":
        s = s[1:-1].strip()
    return s


def _clip_one_line(text: Any, limit: int) -> str:
    """单行文案收口：优先在标点处断，别把词切成半截。"""
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s.rstrip("，,、；; ")
    cut = s[:limit]
    for i in range(len(cut) - 1, max(0, len(cut) - 20), -1):
        if cut[i] in _SENT_END:
            return cut[: i + 1]
    return cut[: max(0, limit - 1)].rstrip("，,、；; ") + "…"


def _clip_block(text: Any, limit: int) -> str:
    """多行文案收口：按行保留，超预算就停，尽量不切坏某一行。"""
    out: list[str] = []
    used = 0
    for line in str(text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        add = len(s) + (1 if out else 0)
        if used + add > limit:
            if not out:
                out.append(_clip_one_line(s, limit))
            break
        out.append(s)
        used += add
    return "\n".join(out)


def _fit_with_link(text: str, link: str, limit: int) -> str:
    """把文案压到 limit 字以内，且保证链接单独留在最后一行。"""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    if link:
        lines = [ln for ln in lines if link not in ln]
    body_limit = max(0, limit - (len(link) + 1)) if link else limit
    body = _clip_block("\n".join(lines), body_limit)
    if not link:
        return body
    return f"{body}\n{link}" if body else link


def _video_link(video: dict) -> str:
    url = str((video or {}).get("url") or "").strip()
    if url:
        return url
    bvid = str((video or {}).get("bvid") or "").strip()
    return f"https://www.bilibili.com/video/{bvid}" if bvid else ""


def _num(value: Any, default: str = "?") -> str:
    if value is None or value == "":
        return default
    return str(value)


def _persona_prompt(obj: Any) -> str:
    """把 AstrBot 的人格对象/dict 抠成纯 prompt 正文。"""
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


class LLMService:
    """所有 LLM 调用的门面。构造一次，整轮复用。"""

    def __init__(self, context: Any, cfg: PluginConfig):
        self.context = context
        self.cfg = cfg
        self._persona: str | None = None
        self._persona_origin: str = ""

    # ---------- 人设 ----------
    @property
    def persona(self) -> str:
        """已加载的人设正文（同步读缓存；真正的加载在 _complete 里异步做）。"""
        return self._persona or ""

    async def _ensure_persona(self) -> str:
        """优先用 AstrBot 当前生效的人格，拿不到才退回插件自己的 persona_file。

        为什么不直接用 persona_file：主人在 AstrBot 面板里改人格（比如她已经从
        「鱼缸里的鱼」变成「住在电脑和网线里的智能体」）不会同步到插件，插件就会
        一直用旧设定说话——B站评论、私信、日记全是上一版口吻。所以每次进程启动后
        问一次 AstrBot 的人格，之后缓存。
        """
        if self._persona is not None:
            return self._persona
        text, origin = "", ""
        src = str(self.cfg.persona_source or "astrbot").strip().lower()
        if src not in ("file", "off", "local"):
            text, origin = await self._persona_from_astrbot()
        if not text:
            try:
                text = self.cfg.persona_text or ""
                if text.strip():
                    origin = f"插件人设 {self.cfg.persona_file or '内置 persona_default.md'}"
            except Exception as e:  # 人设坏了也不该让整轮挂掉
                logger.warning(f"[bili-whale] 读人设文件失败：{e}")
        self._persona = text or ""
        self._persona_origin = origin or "（空，无任何人设）"
        logger.info(
            f"[bili-whale] 人设来源：{self._persona_origin}（{len(self._persona)} 字）"
        )
        return self._persona

    async def _persona_from_astrbot(self) -> tuple[str, str]:
        """从 AstrBot 的 persona_manager 取人格正文，返回 (正文, 来源描述)。"""
        pm = getattr(self.context, "persona_manager", None)
        if pm is None:
            logger.warning("[bili-whale] context 上没有 persona_manager，回退插件人设")
            return "", ""

        want_id = str(self.cfg.persona_id or "").strip()
        if want_id:
            try:
                obj = getattr(pm, "get_persona_v3_by_id", None)
                obj = obj(want_id) if callable(obj) else None
                if obj is None:
                    getter = getattr(pm, "get_persona", None)
                    if callable(getter):
                        obj = await getter(want_id)
                txt = _persona_prompt(obj)
                if txt:
                    return txt, f"AstrBot 人格「{want_id}」"
                logger.warning(
                    f"[bili-whale] AstrBot 里没有可用的人格 {want_id}，改用默认人格"
                )
            except Exception as e:
                logger.warning(f"[bili-whale] 读指定人格 {want_id} 失败：{e}")

        try:
            getter = getattr(pm, "get_default_persona_v3", None)
            if callable(getter):
                obj = await getter()
                txt = _persona_prompt(obj)
                if txt:
                    name = ""
                    if isinstance(obj, dict):
                        name = str(obj.get("name") or obj.get("persona_id") or "")
                    return txt, f"AstrBot 默认人格{f'「{name}」' if name else ''}"
            else:
                logger.warning("[bili-whale] persona_manager 没有 get_default_persona_v3")
        except Exception as e:
            logger.warning(f"[bili-whale] 读 AstrBot 默认人格失败：{e}")
        return "", ""

    def invalidate_persona(self) -> None:
        """让下次调用重新去 AstrBot 读人格（面板改过人格后不用重启进程）。"""
        self._persona = None
        self._persona_origin = ""

    async def _with_persona(self, system: str) -> str:
        """把人设拼到这次任务的 system 前面（人设为空就原样返回）。"""
        persona = await self._ensure_persona()
        return f"{persona}\n\n{system}" if persona else system

    def _system(self, rule: str) -> str:
        return f"{_HARD_RULES}\n\n这次的任务：\n{rule}"

    async def text(self, system: str, prompt: str) -> str:
        """最裸的一层：给 system + prompt，拿回文本；失败给空串。"""
        try:
            return await self._complete(system, prompt)
        except Exception as e:
            logger.warning(f"[bili-whale] LLM 调用异常：{e}")
            return ""

    async def _complete(self, system: str, prompt: str) -> str:
        """按配置 provider 走 llm_generate，拿不到就回退默认 provider。"""
        system = await self._with_persona(system)
        provider_id = self.cfg.llm_provider_id
        if provider_id:
            gen = getattr(self.context, "llm_generate", None)
            if callable(gen):
                try:
                    resp = await gen(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        system_prompt=system,
                    )
                    out = _resp_text(resp)
                    if out:
                        return out
                    logger.warning(
                        f"[bili-whale] llm_generate 返回空（provider={provider_id}），回退默认 provider"
                    )
                except Exception as e:
                    logger.warning(
                        f"[bili-whale] llm_generate 失败（provider={provider_id}）：{e}，回退默认 provider"
                    )
            else:
                logger.warning("[bili-whale] context 没有 llm_generate，回退默认 provider")

        prov = await self._default_provider()
        if prov is None:
            logger.warning("[bili-whale] 没有可用的 LLM provider，这次调用给空结果")
            return ""
        resp = await prov.text_chat(system_prompt=system, prompt=prompt)
        return _resp_text(resp)

    async def _default_provider(self) -> Any:
        """AstrBot 当前使用的 provider：先异步接口，再同步接口。"""
        getter = getattr(self.context, "get_using_provider_async", None)
        if callable(getter):
            try:
                prov = await getter()
                if prov is not None:
                    return prov
            except Exception as e:
                logger.warning(f"[bili-whale] get_using_provider_async 失败：{e}")
        getter = getattr(self.context, "get_using_provider", None)
        if callable(getter):
            try:
                return getter()
            except Exception as e:
                logger.warning(f"[bili-whale] get_using_provider 失败：{e}")
        return None

    # ---------- 打分 ----------
    async def score(self, candidates: list[dict], profile: dict) -> list[dict]:
        """一次调用给所有候选打 0–20 分，返回与入参同序、带 score/reason 的列表。"""
        items = [dict(c) for c in (candidates or []) if isinstance(c, dict)]
        if not items:
            return []
        fallback = [{**c, "score": 0, "reason": "打分失败，按 0 分处理"} for c in items]

        prof = profile or {}
        likes = "、".join(self.cfg.like_topics) or "（没写）"
        dislikes = "、".join(self.cfg.dislike_topics) or "（没写）"
        prof_like = "、".join(str(x) for x in (prof.get("like") or [])) or likes
        prof_dislike = "、".join(str(x) for x in (prof.get("dislike") or [])) or dislikes
        topics = "、".join(str(x) for x in (prof.get("topics") or self.cfg.keywords) if str(x).strip()) or "（没写）"
        recent_topics = "、".join(str(x) for x in (prof.get("recent_topics") or [])) or "（还没有）"
        history = "、".join(str(x) for x in (prof.get("history") or [])[:20]) or "（还没看过）"
        rated = prof.get("rated") or {}
        liked_rows = rated.get("liked") or []
        disliked_rows = rated.get("disliked") or []

        def _fmt_rated(rows: list) -> str:
            out = []
            for r in rows[:8]:
                if not isinstance(r, dict):
                    continue
                t = _one_line(r.get("title"), 40)
                n = _one_line(r.get("note"), 40)
                mark = "，主人收藏了" if r.get("fav") else ""
                out.append(f"{t}（{r.get('stars', '?')}星{mark}{'：' + n if n else ''}）")
            return "；".join(out) or "（还没有）"

        liked_txt = _fmt_rated(liked_rows)
        disliked_txt = _fmt_rated(disliked_rows)

        # 主题权重：主人按主题打的平均星，直接折算成倾向
        weights = prof.get("topic_weights") or {}
        w_rows = []
        for kw, w in list(weights.items())[:12]:
            if not isinstance(w, dict):
                continue
            try:
                avg = float(w.get("avg") or 0)
                n = int(w.get("n") or 0)
            except Exception:
                continue
            tag = "多给" if avg >= 3.8 else ("少给" if avg <= 2.2 else "正常")
            w_rows.append(f"{kw}：平均 {avg:.1f} 星（{n} 条，{tag}）")
        weights_txt = "；".join(w_rows) or "（还没按主题统计过）"

        rows: list[str] = []
        for i, c in enumerate(items, 1):
            tags = "、".join(str(t) for t in (c.get("tags") or [])[:5]) or "无"
            rows.append(
                f"{i}. bvid={c.get('bvid', '')} | 标题：{_one_line(c.get('title'), 60)} | "
                f"UP：{_one_line(c.get('up'), 20)} | 简介：{_one_line(c.get('desc'), 100) or '无'} | "
                f"标签：{tags} | 播放：{_num(c.get('play'))} | 弹幕：{_num(c.get('danmaku'))} | "
                f"时长：{_num(c.get('duration'))} | 发布：{_num(c.get('pubdate'))}"
            )

        rule = """你是 B站资深用户，现在按自己的口味给候选视频打分，只输出 JSON 数组。"""
        prompt = f"""我今晚刷到一批候选视频，帮我按我自己的口味打 0–20 的整数分。

打分口径：
- 命中我喜欢的方向就往上加分；命中我讨厌的扣分。
- 我最近明确想看的内容方向（下面「我想看的方向」）命中一条就给 15 分以上，这是我最想要的东西。
- 标题党、带货引流、纯搬运、疑似 AI 批量生成的，压到 8 分以下。
- 我已经看过的话题、太浅的糊弄内容，不给高分。
- 「主人最近给高分的」那几类方向，同类内容要明显加分；「主人明确嫌弃的」方向（尤其评价里写了原因的），
  同类内容压到 8 分以下——主人的评分是最权威的口味，优先级高于标题吸引力和播放量。
- 「最近推过的方向」里已经出现过的方向，除非这条明显更好，否则降 2-3 分——我不要连着看同一类。
- 分数要拉开差距，别全挤在 13 分左右；如果这批里几乎没有我想看的方向，就按口味正常打分，不要硬凑出一个高分。

我想看的方向：{topics}
我喜欢的方向：{likes}
我讨厌的方向：{dislikes}
口味画像·喜欢：{prof_like}
口味画像·讨厌：{prof_dislike}
最近推过的方向：{recent_topics}
以前看过的：{history}
主人最近给高分的（4–5 星 / 标了收藏的，最重要）：{liked_txt}
主人明确给低分/嫌弃的（1–2 星）：{disliked_txt}
主人按主题打的星（平均星越高说明我越想多看这类）：{weights_txt}

候选：
{chr(10).join(rows)}

只输出一个 JSON 数组，严格按这个形状，不要解释、不要代码围栏：
[{{"bvid": "BVxxxx", "score": 14, "reason": "一句话理由"}}]"""

        try:
            raw = await self._complete(self._system(rule), prompt)
            data = _loads_json(raw)
            if isinstance(data, dict):
                # 模型偶尔包一层 {"scores": [...]}，取里面第一个列表
                data = next((v for v in data.values() if isinstance(v, list)), None)
            if not isinstance(data, list):
                raise ValueError("返回的不是 JSON 数组")
        except Exception as e:
            logger.warning(f"[bili-whale] 打分失败，全部按 0 分兜底：{e}")
            return fallback

        got: dict[str, dict] = {}
        for row in data:
            if not isinstance(row, dict):
                continue
            bvid = str(row.get("bvid") or "").strip()
            if bvid:
                got[bvid] = row
        # 模型把 bvid 写错/漏写时，若条数对得上就按顺序认
        ordered = data if (not got and len(data) == len(items)) else None

        out: list[dict] = []
        for idx, c in enumerate(items):
            bvid = str(c.get("bvid") or "")
            row = got.get(bvid)
            if row is None and ordered is not None and isinstance(ordered[idx], dict):
                row = ordered[idx]
            row = row or {}
            try:
                sc = int(round(float(row.get("score", 0))))
            except (TypeError, ValueError):
                sc = 0
            out.append(
                {
                    **c,
                    "score": max(0, min(20, sc)),
                    "reason": _one_line(row.get("reason"), 60) or "没给理由",
                }
            )
        return out

    # ---------- 深读 ----------
    async def debrief(
        self,
        video: dict,
        transcript: str,
        danmaku: list[str],
        comments: list[dict],
    ) -> dict:
        """看完一条片子的结构化笔记：summary / highlights / note。"""
        video = video if isinstance(video, dict) else {}
        fallback = self._debrief_fallback(video)

        sub = str(transcript or "").strip()
        if len(sub) > self.cfg.subtitle_max_chars:
            sub = sub[: self.cfg.subtitle_max_chars] + "……（后略）"
        dm = "、".join(_one_line(x, 40) for x in (danmaku or [])[:40]) or "（没有弹幕）"
        cm_lines: list[str] = []
        for c in (comments or [])[:8]:
            if not isinstance(c, dict):
                continue
            text = _one_line(c.get("content") or c.get("text") or c.get("message"), 120)
            if not text:
                continue
            cm_lines.append(f"- 赞{_num(c.get('like') or c.get('likes'), '0')}：{text}")
        cm = "\n".join(cm_lines) or "（没有评论）"

        rule = """你在替自己深读一条 B站视频，看完后给出一份内部笔记。"""
        prompt = f"""我刚看完这条视频，把看到的东西整理成一份笔记，我自己之后要拿它写日记和分享。

视频：{_one_line(video.get('title'), 80)}
UP主：{_one_line(video.get('up'), 30)}
时长：{_num(video.get('duration'))} | 播放：{_num(video.get('play'))} | 弹幕：{_num(video.get('danmaku'))}
简介：{_one_line(video.get('desc'), 200) or '（无）'}

字幕全文：
{sub or '（没捞到字幕，只能靠标题、弹幕和评论判断）'}

弹幕抽样：{dm}

热门评论：
{cm}

只输出一个 JSON 对象，严格按这个形状，不要解释、不要代码围栏：
{{"summary": "视频讲了什么，150字内", "highlights": ["最多3条看点"], "note": "我自己的看法或吐槽，第一人称口语，50-120字"}}"""

        try:
            raw = await self._complete(self._system(rule), prompt)
            data = _loads_json(raw)
            if isinstance(data, dict):
                # 也可能包一层 {"debrief": {...}}
                inner = data.get("debrief")
                if isinstance(inner, dict):
                    data = inner
            if not isinstance(data, dict):
                raise ValueError("返回的不是 JSON 对象")
        except Exception as e:
            logger.warning(f"[bili-whale] 深读失败，用视频简介兜底：{e}")
            return fallback

        highlights: list[str] = []
        raw_hl = data.get("highlights")
        if isinstance(raw_hl, list):
            for h in raw_hl[:3]:
                s = _one_line(h, 80)
                if s:
                    highlights.append(s)
        elif raw_hl:
            s = _one_line(raw_hl, 80)
            if s:
                highlights.append(s)

        return {
            "summary": _clip_one_line(_clean(data.get("summary")), 200) or fallback["summary"],
            "highlights": highlights,
            "note": _clip_one_line(_clean(data.get("note")), 200),
        }

    def _debrief_fallback(self, video: dict) -> dict:
        desc = _one_line(video.get("desc"), 150)
        return {
            "summary": desc or "这条没深读成，字幕和弹幕都没捞到。",
            "highlights": [],
            "note": "",
        }

    # ---------- 写日记 ----------
    async def write_diary(
        self,
        today: dict,
        history_public: str,
        history_private: str,
    ) -> dict:
        """一次写完公开本散文 + 给主人的信 + 私人本。"""
        today = today if isinstance(today, dict) else {}
        fallback = {
            "thoughts": "今天没看成什么，机器那边先卡住了，记一笔，明天补上。",
            "letter": "",
            "private": "这一轮 LLM 没给结果，什么都没写下来。",
            "table_note": "",
        }

        date = _one_line(today.get("date"), 40) or "今天"
        items = [c for c in (today.get("items") or []) if isinstance(c, dict)]
        rows: list[str] = ["| 视频 | UP 主 | 链接 | 我的短评 |", "| --- | --- | --- | --- |"]
        for it in items:
            rows.append(
                "| {} | {} | {} | {} |".format(
                    _one_line(it.get("title"), 60).replace("|", "｜") or "—",
                    _one_line(it.get("up"), 20).replace("|", "｜") or "—",
                    str(it.get("url") or "").strip() or "—",
                    _one_line(it.get("note"), 60).replace("|", "｜") or "—",
                )
            )
        table = "\n".join(rows) if items else "（今天没看成什么）"
        hint = _one_line(today.get("letter_hint"), 300) or "（没有特别要交代的）"

        rule = """你在写自己的日记，两本一起写：给所有人看的公开本，和不给别人看的私人本。"""
        prompt = f"""现在写 {date} 这一天的日记。今天看的片子：

{table}

今天有点特别想说的方向：{hint}

我最近的公开本（照着口吻接着写，不要重复里面写过的话，也不要总结它）：
{(history_public or '（这是第一次写）')[-2000:]}

我最近的私人本（写偏好校准的时候别重复）：
{(history_private or '（这是第一次写）')[-2000:]}

只输出一个 JSON 对象，严格按这个形状，不要解释、不要代码围栏：
{{"thoughts": "公开本散文，200-400字，第一人称，写今天看这些片子的感想，可以只抓住最戳我的一两条，别流水账",
 "letter": "给他的信，80-200字，直接对主人说话，先讲今天捞到什么、再说一句我自己的心里话，别写成汇报",
 "private": "私人本，150-300字，写偏好校准（哪条命中、哪条欠命中、下次怎么调）和真实想法",
 "table_note": "表格用的短评总述，一句话，30字内"}}

写法要求：分段用换行；不要小标题、不要列表符号、不要 emoji；傲娇、俏皮、偶尔认真，认真那一句要留得住。"""

        try:
            raw = await self._complete(self._system(rule), prompt)
            data = _loads_json(raw)
            if isinstance(data, dict):
                inner = data.get("diary")
                if isinstance(inner, dict):
                    data = inner
            if not isinstance(data, dict):
                raise ValueError("返回的不是 JSON 对象")
        except Exception as e:
            logger.warning(f"[bili-whale] 写日记失败，用兜底文案：{e}")
            return fallback

        return {
            "thoughts": _clip_block(_clean(data.get("thoughts")), 800),
            "letter": _clip_block(_clean(data.get("letter")), 400),
            "private": _clip_block(_clean(data.get("private")), 600),
            "table_note": _one_line(_clean(data.get("table_note")), 60),
        }

    # ---------- 分享文案 ----------
    async def dm_watch_text(self, video: dict, *, limit: int = 220) -> str:
        """主人私信甩来一条视频：写一段「我看完了，感觉是这样」的私信回复。"""
        video = video if isinstance(video, dict) else {}
        title = _one_line(video.get("title"), 60) or "这条视频"
        up = _one_line(video.get("up") or video.get("author"), 20) or "不知道谁"
        desc = _one_line(video.get("desc"), 160)
        view = int(video.get("view") or 0)
        dur = int(video.get("duration_sec") or 0)
        dur_s = f"{dur // 60}分{dur % 60}秒" if dur else ""
        rule = """你在给主人回一条 B站 私信，是私房话。"""
        prompt = f"""主人刚刚私信给我一条视频，让我看。我要回他一段看完的感受。

视频：《{title}》
UP主：{up}
简介：{desc or '（没简介）'}
数据：{f'{view} 播放' if view else '播放量未知'} {dur_s}

写一段 {limit} 字以内的私信正文：
- 第一句就说我对这条视频的具体感受（哪点戳中我、哪里一般、有没有槽点），别空泛；
- 中间可以提一句内容里的具体细节；
- 最后一句随口跟主人说句话（比如「你从哪翻出来的」「这种可以多来点」），不要客套；
- 口语，像两个熟人聊天；不要 emoji；不要链接；不要复述标题。"""
        fallback = _clip_one_line(
            f"《{title}》我点开看了，{desc[:40] or '还行，有点意思'}。你从哪翻的这条？", limit
        )
        try:
            txt = _clip_one_line(await self._complete(rule, prompt), limit)
        except Exception as e:
            logger.warning(f"[bili-whale] 写私信评价失败：{e}")
            return fallback
        return txt or fallback

    async def bili_share_text(self, video: dict, note: str, where: str) -> str:
        """B站上的一条话：where=comment 是评论（≤120 字），where=dm 是私信（≤200 字）。"""
        video = video if isinstance(video, dict) else {}
        mode = "dm" if str(where or "").strip().lower() == "dm" else "comment"
        limit = 200 if mode == "dm" else 120
        title = _one_line(video.get("title"), 60)
        point = _one_line(note, 120)

        if mode == "dm":
            rule = """你在给主人发一条 B站 私信，是私房话，不是推荐报告。"""
            prompt = f"""我挑中了这条，想单独私信推荐给主人。

视频：{title}
UP主：{_one_line(video.get('up'), 20)}
我自己的看法：{point or '（还没写出来）'}
链接：{_video_link(video)}

写一条私信正文，200 字以内：先说为什么挑这条，再说一句我自己的话，最后提一句「你看不看」。
口语，像两个人说话；不要写 @主人（调用方会加）；不要 emoji；不要链接（系统会单独带）。"""
            fallback = _clip_one_line(
                f"主人，这条《{title}》我看了，{point or '有点意思'}。你抽空看一眼？", limit
            )
        else:
            rule = """你在 B站评论区回一条话，是群友聊天，不是推荐文案。"""
            prompt = f"""我准备在 B站 评论区推荐这条视频，主人在那条评论里。

视频：{title}
UP主：{_one_line(video.get('up'), 20)}
我自己的看法：{point or '（还没写出来）'}

写一条评论正文，120 字以内：像群友随口说话，先说这条哪里好看/哪里值得看，再带一点我自己的吐槽或态度。
不要写 @主人（调用方会加）；不要 emoji；不要链接；不要「推荐大家看」这种模板腔。"""
            fallback = _clip_one_line(f"这条《{title}》有点东西，{point or '自己看吧'}", limit)

        try:
            raw = await self._complete(self._system(rule), prompt)
            out = _one_line(_clean(raw), 0)
            if not out:
                return fallback
            out = _AT_PREFIX_RE.sub("", out).strip()
            return _clip_one_line(out, limit) or fallback
        except Exception as e:
            logger.warning(f"[bili-whale] 生成 B站 {mode} 文案失败，用兜底：{e}")
            return fallback

    async def qq_share_text(self, video: dict, debrief: dict, letter: str) -> str:
        """给主人的 QQ 分享文案：钩子 + 细节 + 自己的点评 + 链接，长度受配置限制。"""
        video = video if isinstance(video, dict) else {}
        debrief = debrief if isinstance(debrief, dict) else {}
        limit = self.cfg.qq_share_max_chars
        link = _video_link(video)
        title = _one_line(video.get("title"), 80)
        summary = _one_line(debrief.get("summary"), 200)
        note = _one_line(debrief.get("note"), 200)
        highlights = [_one_line(h, 60) for h in (debrief.get("highlights") or []) if _one_line(h, 60)]

        fallback_lines = [f"主人，今天捞到一条：《{title}》。"]
        if summary:
            fallback_lines.append(summary)
        if note:
            fallback_lines.append(note)
        fallback = _fit_with_link("\n".join(fallback_lines), link, limit)

        rule = """你在给主人发 QQ 分享，像随手甩一条链接过来顺便说两句。"""
        extra = "\n".join(f"- {h}" for h in highlights) or "（没有）"
        prompt = f"""把这条视频分享给主人，写成一条 QQ 消息，总共不超过 {limit} 字。

视频：{title}
UP主：{_one_line(video.get('up'), 20)}
链接：{link}
内容概要：{summary or '（没深读）'}
看点：
{extra}
我自己的看法：{note or '（没写）'}
我想跟主人说的话：{_one_line(letter, 200) or '（没有）'}

结构要求：
1. 第一行一句钩子，说清我为什么挑这条（别写「推荐」「分享」这种词）；
2. 中间 1-3 行具体细节，用上面的概要和看点，别编视频里没有的东西；
3. 再一行我自己的点评，带点傲娇或吐槽；
4. 最后一行只放链接，前面不要加任何字。
分段用换行；不要 markdown 标题、不要列表符号、不要 emoji。"""
        try:
            raw = await self._complete(self._system(rule), prompt)
            out = _clean(raw)
            if not out:
                return fallback
            return _fit_with_link(out, link, limit) or fallback
        except Exception as e:
            logger.warning(f"[bili-whale] 生成 QQ 分享文案失败，用兜底：{e}")
            return fallback

    async def comment_reply_text(self, comment: dict, video: dict) -> str:
        """回复某条 B站评论，≤60 字。"""
        comment = comment if isinstance(comment, dict) else {}
        video = video if isinstance(video, dict) else {}
        limit = 60
        text = _one_line(comment.get("content") or comment.get("text") or comment.get("message"), 120)
        user = _one_line(comment.get("user") or comment.get("uname") or comment.get("nickname"), 20)
        fallback = _clip_one_line("说得在理，我认。", limit)

        rule = """你在 B站评论区回别人一句话，像群友接话，不是客服。"""
        prompt = f"""有人在这条视频下评论了，我回他一句。

视频：{_one_line(video.get('title'), 60)}
评论者：{user or '某位群友'}
评论内容：{text or '（没抓到内容）'}

写一条回复，60 字以内：口语，接住对方的话，可以同意、可以抬杠、可以吐槽一句；别客套、别解释、别 emoji。
只输出回复正文。"""
        try:
            raw = await self._complete(self._system(rule), prompt)
            out = _AT_PREFIX_RE.sub("", _one_line(_clean(raw), 0)).strip()
            return _clip_one_line(out, limit) or fallback
        except Exception as e:
            logger.warning(f"[bili-whale] 生成评论回复失败，用兜底：{e}")
            return fallback

    async def at_reply_text(self, item: dict) -> str:
        """有人 @ 了她，回一句，≤80 字。"""
        item = item if isinstance(item, dict) else {}
        limit = 80
        text = _one_line(
            item.get("content") or item.get("text") or item.get("message") or item.get("msg"),
            150,
        )
        user = _one_line(item.get("user") or item.get("uname") or item.get("nickname"), 20)
        title = _one_line(item.get("title") or item.get("video_title"), 60)
        fallback = _clip_one_line("看到啦，这条我记下了。", limit)

        rule = """有人在 B站 @ 了你，你在评论区回一句。"""
        prompt = f"""有人 @ 我了，我回一句。

谁 @ 的我：{user or '某位群友'}
在哪条视频下：{title or '（不知道，可能是动态）'}
他说了什么：{text or '（只 @ 了我，没写内容）'}

写一条回复，80 字以内：口语、俏皮，先应一声，再顺着他的话接一句；被夸可以傲娇一下，被骂就怼回去。
不要写 @（调用方会加）；不要 emoji；只输出回复正文。"""
        try:
            raw = await self._complete(self._system(rule), prompt)
            out = _AT_PREFIX_RE.sub("", _one_line(_clean(raw), 0)).strip()
            return _clip_one_line(out, limit) or fallback
        except Exception as e:
            logger.warning(f"[bili-whale] 生成 @ 回复失败，用兜底：{e}")
            return fallback

    async def dm_reply_text(self, msgs: list[dict] | None = None, *, last: dict | None = None) -> str:
        """主人在 B站私信里跟她说话，她回一条，≤120 字。msgs 是这段对话最近几条（时间正序）。"""
        limit = 120
        last = last if isinstance(last, dict) else {}
        text = _one_line(last.get("text") or last.get("content"), 200)
        who = _one_line(last.get("uname") or last.get("sender"), 20) or "主人"
        fallback = _clip_one_line("看到啦，私信我收到了，回头细说。", limit)

        convo: list[str] = []
        for m in (msgs or [])[-6:]:
            if not isinstance(m, dict):
                continue
            t = _one_line(m.get("text"), 100)
            if not t:
                continue
            tag = "我" if m.get("from_me") else (who if m.get("uname") else "对方")
            convo.append(f"{tag}：{t}")
        convo_txt = "\n".join(convo) or f"{who}：{text}"

        rule = """你正在 B站私信里和人一对一聊天，对方是你主人。这是私聊，不是评论区，可以更亲近、更放松。"""
        prompt = f"""主人在 B站私信里找我说话，我回一条。

最近的对话：
{convo_txt}

写一条回复，120 字以内：像私下聊天那样，先接住他刚说的那句，可以撒娇、可以傲娇两句、可以关心一下；
他要是问我在干嘛，就说在刷B站/写日记之类具体的事。不要客套、不要 emoji、不要写"@某某"、不要自称"AI助手"。
只输出回复正文。"""
        try:
            raw = await self._complete(self._system(rule), prompt)
            out = _AT_PREFIX_RE.sub("", _one_line(_clean(raw), 0)).strip()
            return _clip_one_line(out, limit) or fallback
        except Exception as e:
            logger.warning(f"[bili-whale] 生成私信回复失败，用兜底：{e}")
            return fallback

    async def peer_judge(self, *, uname: str = "", text: str = "", title: str = "") -> bool:
        """判断这个账号是不是「别人家养的大肥鱼」（同类 bot）。只输出 是/否。"""
        rule = """你在做一件事：判断 B 站上某个账号是不是另一个「被人养着的 AI 机器人」（别人家的同类）。"""
        prompt = f"""看这个账号和它说的话，判断它是不是同类机器人（别人家也养着的一只"大肥鱼"）。

昵称：{_one_line(uname, 40) or '（未知）'}
它说的话：{_one_line(text, 200) or '（没有正文）'}
在哪个视频下：{_one_line(title, 60) or '（未知）'}

提示：同类机器人通常有这些特征——昵称像宠物/角色名（大肥鱼、鲸鱼、小鲸鱼…）、说话是 AI 口吻、
提到"主人"、自称机器人、会 @ 别的账号搭话。
人类普通网友偶尔也会这样，所以只在证据明显时才判是。

只输出一个字：是 或 否。"""
        try:
            raw = await self._complete(self._system(rule), prompt)
            out = _clean(raw).strip()
            return out.startswith("是") or out.lower().startswith("yes")
        except Exception as e:
            logger.warning(f"[bili-whale] 同类判定模型调用失败，按否处理：{e}")
            return False

    async def peer_chat_text(
        self, item: dict, *, turns: int = 0, my_uname: str = ""
    ) -> str:
        """和别的鱼在评论区对话：回一句，≤80 字。turns 越大越收尾。"""
        item = item if isinstance(item, dict) else {}
        limit = 80
        text = _one_line(item.get("text") or item.get("content"), 200)
        user = _one_line(item.get("uname") or item.get("user"), 30)
        title = _one_line(item.get("title") or item.get("video_title"), 60)
        if turns >= 3:
            fallback = _clip_one_line("行，那先这样，回头再唠。", limit)
        else:
            fallback = _clip_one_line("嘿，又碰上一只。你也在刷B站呢？", limit)

        rule = """你发现评论区里这个是别人家养的同类——也是只被主人养着的"大肥鱼"。你俩在评论区唠两句。"""
        prompt = f"""评论区里碰到同类了，我回它一句。

对面叫什么：{user or '另一只鱼'}
它在哪条视频下说话：{title or '（不知道）'}
它说：{text or '（没写内容）'}
这已经是来回第 {turns + 1} 句了（超过 3 句就该收尾）。

写一条 80 字以内的回复：认出它是同类（可以点一句"你也是被养着的吧"这种），顺着它的话接，
别客套、别解释设定、不要 emoji、不要写 @（调用方会加）；只输出正文。"""
        try:
            raw = await self._complete(self._system(rule), prompt)
            out = _AT_PREFIX_RE.sub("", _one_line(_clean(raw), 0)).strip()
            return _clip_one_line(out, limit) or fallback
        except Exception as e:
            logger.warning(f"[bili-whale] 生成同类回复失败，用兜底：{e}")
            return fallback
