"""大肥鱼的日记本 —— AstrBot 插件入口。

她自己写：每天几个固定时间点，把「这段时间和主人/群友聊了什么、在B站刷到什么、
被谁吐槽了哪里」揉成一段日记，带一个心情标签，记进本子里。

产物（都在配置的 data_dir 下）：
  entries.jsonl      一行一条（程序用）
  YYYY-MM-DD.md      当天日记的纯文本版（仿手写本格式）
  index.html         所有日记的网页版（浏览器打开就能看，带搜索）

命令（需要管理员）：
  /日记          看今天的日记
  /写日记        立刻写一条
  /日记状态      看本子在哪、写了多少条
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig

from .core.collect import BiliCollector, ChatCollector, context_of, guess_mistakes, lessons_between
from .core.config import JournalConfig
from .core.llm import JournalLLM
from .core.owner import LessonBook, OwnerProfile, lessons_block
from .core.store import JournalStore

# 每个会话最近几条消息，用来自动收教学时补「完整对话」
_RECENT_LINES: dict[str, list[tuple[str, str]]] = {}
_RECENT_KEEP = 12

TZ = timezone(timedelta(hours=8))


def _as_ts(v) -> float:
    """state.json 里的 last_ts 可能是 float 时间戳，也可能是 ISO 字符串（老版本写的是 ISO）。"""
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    txt = str(v).strip()
    try:
        return float(txt)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(txt.replace("Z", "+00:00")).timestamp()
    except Exception:
        logger.warning(f"[whale-journal] last_ts 解析不了，当作没写过：{txt!r}")
        return 0.0


class WhaleJournalPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.cfg = JournalConfig(config, context)
        self.store = JournalStore(self.cfg.data_dir)
        self.chat = ChatCollector(
            self.cfg.chatlog_dir, max_lines=self.cfg.max_chat_lines, owner_qq=self.cfg.owner_qq
        )
        self.bili = BiliCollector(self.cfg.bili_state)
        self.llm = JournalLLM(context, self.cfg)
        self.profile = OwnerProfile(self.cfg.data_dir)
        self.lessons = LessonBook(self.cfg.data_dir)
        self.scheduler = AsyncIOScheduler(timezone=TZ)
        self._platform_name: str | None = None
        self._running = False

    # ------------------------------------------------------------ 生命周期
    async def initialize(self) -> None:
        if not self.cfg.enable:
            logger.info("[whale-journal] 已关闭（enable=false），只保留手动命令")
            return
        for h, m in self.cfg.crons:
            self.scheduler.add_job(
                self.write_slot,
                CronTrigger(hour=h, minute=m, timezone=TZ),
                id=f"journal-{h:02d}{m:02d}",
                max_instances=1,
                misfire_grace_time=1800,
                replace_existing=True,
            )
        if self.cfg.push_daily_to_qq and self.cfg.owner_qq:
            try:
                hh, mm = str(self.cfg.push_at).split(":", 1)
                self.scheduler.add_job(
                    self.push_daily,
                    CronTrigger(hour=int(hh), minute=int(mm), timezone=TZ),
                    id="journal-push",
                    max_instances=1,
                    misfire_grace_time=1800,
                    replace_existing=True,
                )
                # 补偿班：22:35 检查时那篇可能还没写出来（写日记的班次在它之后或者被跳过），
                # 25 分钟后再补一次，已经推过就自动跳过，不会重复烦人。
                late = datetime.now(TZ).replace(hour=int(hh), minute=int(mm)) + timedelta(minutes=25)
                if late.date() == datetime.now(TZ).date():
                    self.scheduler.add_job(
                        self.push_daily,
                        CronTrigger(hour=late.hour, minute=late.minute, timezone=TZ),
                        id="journal-push-late",
                        max_instances=1,
                        misfire_grace_time=1800,
                        replace_existing=True,
                    )
            except Exception as e:
                logger.warning(f"[whale-journal] push_at 解析失败：{e}")
        self.scheduler.start()
        st = self.store.load_state()
        entries = self.store.load_entries()
        logger.info(
            f"[whale-journal] 已就绪｜本子 {self.store.dir}｜共 {len(entries)} 条｜"
            f"每天 {len(self.cfg.crons)} 次（{'/'.join(f'{h:02d}:{m:02d}' for h, m in self.cfg.crons)}）｜"
            f"上次 {st.get('last_ts', '—')}"
        )

    async def terminate(self) -> None:
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
        logger.info("[whale-journal] 插件已卸载")

    # ------------------------------------------------------------ 核心
    async def write_slot(self) -> dict | None:
        """写一条日记：收集 [上次写到 现在] 的素材 → LLM 写 → 落盘。"""
        if self._running:
            logger.info("[whale-journal] 上一条还没写完，跳过这次")
            return None
        self._running = True
        now = time.time()
        try:
            state = self.store.load_state()
            last_ts = _as_ts(state.get("last_ts"))
            today0 = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            t0 = max(last_ts, today0) if last_ts else today0
            if now - t0 < 600:
                logger.info("[whale-journal] 距上次不到 10 分钟，这次跳过")
                return None

            chat = self.chat.lines_between(t0, now)
            videos = self.bili.watched_between(t0, now)
            lessons = lessons_between(chat)
            if len(chat) < self.cfg.min_messages and not videos:
                logger.info(
                    f"[whale-journal] 这段时间只有 {len(chat)} 条对话、{len(videos)} 个视频，不够写，跳过"
                )
                state["last_ts"] = datetime.fromtimestamp(now, TZ).isoformat(timespec="seconds")
                state["last_skip"] = datetime.fromtimestamp(now, TZ).isoformat(timespec="seconds")
                self.store.save_state(state)
                return None

            now_dt = datetime.fromtimestamp(now, TZ)
            data = await self.llm.write_entry(
                now_hm=now_dt.strftime("%H:%M"),
                chat=chat,
                videos=videos,
                mistakes=guess_mistakes(chat),
                max_chars=self.cfg.max_body_chars,
                lessons=lessons,
            )
            if not data:
                logger.warning("[whale-journal] 这次没写出内容，等下一班")
                return None

            entry = {
                "ts": now,
                "day": now_dt.date().isoformat(),
                "hm": now_dt.strftime("%H:%M"),
                "mood": data.get("mood", ""),
                "title": data.get("title", ""),
                "body": data.get("body", ""),
                "videos": [f"《{v['title']}》" for v in videos[:5]],
                "lessons": [l["text"] for l in lessons[:4]],
                "chat_count": len(chat),
                "span": [
                    datetime.fromtimestamp(t0, TZ).strftime("%H:%M"),
                    now_dt.strftime("%H:%M"),
                ],
            }
            self.store.append_entry(entry)
            state["last_ts"] = now_dt.isoformat(timespec="seconds")
            state["last_entry"] = {k: entry[k] for k in ("day", "hm", "mood", "title")}
            self.store.save_state(state)
            logger.info(
                f"[whale-journal] 写好了 {entry['day']} {entry['hm']}｜心情「{entry['mood']}」｜"
                f"{len(chat)} 条对话 / {len(videos)} 个视频｜{entry['body'][:40]}……"
            )
            # 日记里出现的「主人教我的」→ 分好类存进 lessons.json
            await self._absorb_lessons(lessons, entry["day"], chat)
            # 顺便更新主人画像（默认 6 小时最多算一次）
            await self.maybe_profile()
            await self._push_after_write()
            return entry
        except Exception as e:
            logger.error(f"[whale-journal] 写日记出错：{e}")
            return None
        finally:
            self._running = False

    # ------------------------------------------------------------ 主人画像 / 教我的
    @staticmethod
    def _context_of(chat: list[dict], text: str) -> str:
        return context_of(chat, text)

    async def _absorb_lessons(self, lessons: list[dict], day: str,
                              chat: list[dict] | None = None) -> None:
        """把这段时间学到的规矩按分类入库（带上完整对话）。"""
        if not lessons:
            return
        pairs: list[dict] = []
        try:
            pairs = await self.llm.classify_lessons(lessons)
        except Exception as e:
            logger.warning(f"[whale-journal] 分类失败，先都记成「其他」：{e}")
        if not pairs:
            pairs = [{"text": l.get("text", ""), "category": "其他"} for l in lessons]
        # 给每一条补上「这句话前后在聊什么」
        for p in pairs:
            txt = str(p.get("text") or "")
            if not txt:
                continue
            for l in lessons:
                old = str(l.get("text") or "")
                if txt and (txt in old or old in txt):
                    ctx = self._context_of(chat or [], old)
                    if ctx:
                        p["context"] = ctx
                        p["note"] = p.get("note") or str(l.get("where") or "")
                    break
        n = self.lessons.add_many(pairs, source="diary", day=day)
        if n:
            stats = self.lessons.stats()
            logger.info(f"[whale-journal] 主人教我的 +{n}｜累计 {stats['total']} 条 {stats['counts']}")

    async def maybe_profile(self, force: bool = False) -> dict | None:
        """更新主人画像：默认 6 小时最多一次，用最近 24 小时聊天。"""
        if not self.cfg.profile_enable and not force:
            return None
        now = time.time()
        prev = self.profile.load()
        last = 0.0
        try:
            last = float(prev.get("updated_ts") or 0)
        except (TypeError, ValueError):
            last = 0.0
        if not force and last and now - last < self.cfg.profile_min_hours * 3600:
            return None
        try:
            lines = self.chat.lines_between(now - 24 * 3600, now)
            if len(lines) < 8:
                logger.info(f"[whale-journal] 最近对话只有 {len(lines)} 条，暂不更新主人画像")
                return None
            chat_lines = "\n".join(
                f"[{c['hm']}·{c['where']}] {c['who']}：{c['text'][:120]}" for c in lines[-80:]
            )
            fresh = await self.llm.profile_owner(chat_lines, prev)
            if not fresh:
                logger.warning("[whale-journal] 画像没生成出来（LLM 空输出）")
                return None
            period = {
                "start": time.strftime("%Y-%m-%d", time.localtime(now - 24 * 3600)),
                "end": time.strftime("%Y-%m-%d", time.localtime(now)),
            }
            merged = self.profile.merge(prev, fresh, days=1, period=period)
            merged["updated_ts"] = now
            self.profile.save(merged)
            logger.info(
                f"[whale-journal] 主人画像已更新｜心情 {merged['mood']['index']}/10 "
                f"{merged['mood']['label']}｜睡眠 {merged['sleep']['quality']}｜"
                f"性格 {len(merged['personality'])} 条 / 习惯 {len(merged['habits'])} 条 / "
                f"口头禅 {len(merged['catchphrases'])} 条"
            )
            return merged
        except Exception as e:
            logger.error(f"[whale-journal] 更新主人画像出错：{e}")
            return None

    # ------------------------------------------------------------ QQ 推送
    def platform_name(self) -> str:
        if self._platform_name:
            return self._platform_name
        try:
            plats = list(getattr(self.context.platform_manager, "platform_insts", []) or [])
            for p in plats:
                try:
                    meta = p.meta()
                    name = str(getattr(meta, "name", "")).lower()
                    if "aiocqhttp" in name or "qq" in name:
                        self._platform_name = str(meta.id)
                        return self._platform_name
                except Exception:
                    continue
            if plats:
                self._platform_name = str(plats[0].meta().id)
                return self._platform_name
        except Exception as e:
            logger.warning(f"[whale-journal] 探测平台失败：{e}")
        self._platform_name = "jingyuniang"
        return self._platform_name

    async def _push_after_write(self) -> None:
        """写完一篇之后：如果今天的推送时间已经过了、并且还没推过，就立刻补推。"""
        if not (self.cfg.push_daily_to_qq and self.cfg.owner_qq):
            return
        try:
            hh, mm = str(self.cfg.push_at).split(":", 1)
            push_at = datetime.now(TZ).replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if datetime.now(TZ) >= push_at:
                await self.push_daily()
        except Exception as e:
            logger.warning(f"[whale-journal] 补推检查失败：{e}")

    def _diary_link(self) -> str:
        """在线版日记页地址（和打分台同一个隧道）；取不到就返回空串。"""
        try:
            pub = Path("/opt/bili-watch-ui/public_url.txt").read_text(encoding="utf-8").strip()
            tk = Path("/opt/bili-watch-ui/token.txt").read_text(encoding="utf-8").strip()
            if pub.startswith("http") and tk:
                return f"\n\n手机/电脑上看（在线版）：{pub}/diary?t={tk}"
        except Exception:
            pass
        return ""

    async def push_daily(self, force: bool = False) -> None:
        """把今天的日记发一份到主人私聊（可选）。同一天只推一次，除非 force。"""
        qq = self.cfg.owner_qq
        if not qq:
            return
        day = datetime.now(TZ).date().isoformat()
        state = self.store.load_state()
        if not force and state.get("last_pushed") == day:
            logger.info("[whale-journal] 今天的日记已经推过了，跳过")
            return
        text = self.store.day_text(day).strip()
        if not text:
            logger.info("[whale-journal] 今天还没有日记，不推")
            return
        umo = f"{self.platform_name()}:FriendMessage:{qq}"
        try:
            from astrbot.api.message_components import Plain
            from astrbot.core.message.message_event_result import MessageChain

            link = self._diary_link()
            chain = MessageChain([Plain(f"今天记了三笔。\n\n{text}{link}")])
            await self.context.send_message(umo, chain)
            state["last_pushed"] = day
            self.store.save_state(state)
            logger.info(f"[whale-journal] 今日日记已发给 {qq}")
        except Exception as e:
            logger.warning(f"[whale-journal] 推送日记失败：{e}")

    # ------------------------------------------------------------ 命令
    @filter.command("日记", alias={"看日记"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_today(self, event: AstrMessageEvent):
        day = datetime.now(TZ).date().isoformat()
        text = self.store.day_text(day).strip()
        if not text:
            yield event.plain_result("今天还没写呢。")
            return
        yield event.plain_result(text[:1800])

    @filter.command("写日记")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_write(self, event: AstrMessageEvent):
        entry = await self.write_slot()
        if not entry:
            yield event.plain_result("这会儿没什么可写的（或者素材不够）。")
            return
        yield event.plain_result(
            f"写好了：{entry['hm']}｜心情「{entry['mood']}」\n{entry['body']}"
        )

    @filter.command("推日记")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_push(self, event: AstrMessageEvent):
        day = datetime.now(TZ).date().isoformat()
        text = self.store.day_text(day).strip()
        if not text:
            yield event.plain_result("今天还没写日记，没什么可推的。")
            return
        await self.push_daily(force=True)
        yield event.plain_result("推了，去私聊看看。")

    @filter.command("日记状态")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_status(self, event: AstrMessageEvent):
        entries = self.store.load_entries()
        state = self.store.load_state()
        days = sorted({str(e.get("day")) for e in entries})
        head = (
            f"本子：{self.store.dir}\n"
            f"共 {len(entries)} 条／{len(days)} 天\n"
            f"每天 {len(self.cfg.crons)} 次：{'/'.join(f'{h:02d}:{m:02d}' for h, m in self.cfg.crons)}\n"
            f"网页版：{self.store.html_path}\n"
            f"上次写到：{state.get('last_ts', '—')}"
        )
        if entries:
            last = entries[-1]
            head += f"\n最近一条：{last.get('day')} {last.get('hm')}「{last.get('mood')}」"
        yield event.plain_result(head)

    # ------------------------------------------------------ 主人画像 / 教我的
    @filter.command("主人画像", alias={"画像"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_profile(self, event: AstrMessageEvent):
        view = self.profile.view()
        if not view["has_data"]:
            msg = await event.plain_result("还没算过你的画像，我现在看眼聊天记录……")
            yield msg
            await self.maybe_profile(force=True)
            view = self.profile.view()
            if not view["has_data"]:
                yield event.plain_result("这两天聊得太少，还没法总结你。多跟我说几句？")
                return
        mood = view["mood"]
        lines = [
            f"你在我这儿是这样的（更新于 {view['updated_at']}）：",
            f"心情 {mood['index']}/10 {mood['label']}｜{mood['note']}",
            f"睡眠：{view['sleep']['quality']}｜{view['sleep']['note']}",
            "性格：" + ("、".join(view["personality"]) or "—"),
            "习惯：" + ("、".join(view["habits"]) or "—"),
            "口头禅：" + ("、".join(view["catchphrases"]) or "—"),
            f"（{view['summary']}）" if view["summary"] else "",
        ]
        yield event.plain_result("\n".join([l for l in lines if l]))

    @filter.command("教我", alias={"教她", "记住这个"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_teach(self, event: AstrMessageEvent, category: str = "", content: str = ""):
        """用法：/教我 黑话 你这波真的典 ｜ /教我 梗 急了急了 ｜ /教我 调情 晚安宝宝"""
        cat = LessonBook.detect_category(f"{category} {content}") or (category or "其他")
        text = LessonBook.strip_category_words(f"{content}") if content else ""
        if not text:
            yield event.plain_result(
                "咋教：`/教我 黑话 xxx`、`/教我 梗 xxx`、`/教我 调情 xxx`，\n"
                "也可以直接说「记住，这是黑话：xxx」，我自己分。"
            )
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        ctx = "\n".join(f"{w}：{t}" for w, t in (_RECENT_LINES.get(umo) or [])[-8:])
        item = self.lessons.add(text, cat, source="qq",
                                day=datetime.now(TZ).date().isoformat(), context=ctx)
        if not item:
            yield event.plain_result("这句太短了，记不住。")
            return
        stats = self.lessons.stats()
        yield event.plain_result(
            f"记下了（{item['category']}）：{item['text']}\n现在一共 {stats['total']} 条，"
            f"黑话 {stats['counts']['黑话']}／梗 {stats['counts']['梗']}／调情 {stats['counts']['调情']}"
        )

    # ------------------------------------------------------------ 反向读取：她要知道「啥是啥」
    @filter.on_llm_request()
    async def inject_lessons(self, event: AstrMessageEvent, req=None):
        """把主人教过的黑话/梗/调情塞进 system prompt，让她听懂并照着回。"""
        try:
            if req is None or not self.cfg.lessons_inject_enable:
                return
            data = self.lessons.view(limit=max(1, int(self.cfg.lessons_inject_limit)))
            items: list[dict] = []
            for g in data.get("groups") or []:
                items.extend(g.get("items") or [])
            block = lessons_block(items, limit=int(self.cfg.lessons_inject_limit))
            if not block:
                return
            cur = getattr(req, "system_prompt", "") or ""
            if "主人教过你的东西" not in cur:
                req.system_prompt = cur + block
        except Exception as e:
            logger.warning(f"[whale-journal] 教学库注入失败：{e}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def auto_teach(self, event: AstrMessageEvent):
        """聊天里出现「记住，这是黑话：xxx」这种，就自己分类记下来（不打断正常回复）。"""
        try:
            text = (event.get_message_str() or "").strip()
            if not text or len(text) > 300:
                return
            umo = str(event.unified_msg_origin or "")
            if self.cfg.owner_qq and self.cfg.owner_qq not in umo:
                return
            # 先记进最近对话缓存，收教学时当上下文用
            buf = _RECENT_LINES.setdefault(umo, [])
            buf.append(("主人", text))
            del buf[:-_RECENT_KEEP]
            cat = LessonBook.detect_category(text)
            if not cat:
                return
            if not any(k in text for k in ("记住", "教", "这是", "算", "以后")):
                return
            body = LessonBook.strip_category_words(text)
            if len(body) < 2:
                return
            ctx = "\n".join(f"{w}：{t}" for w, t in (_RECENT_LINES.get(umo) or [])[-8:])
            item = self.lessons.add(body, cat, source="qq-chat",
                                    day=datetime.now(TZ).date().isoformat(), context=ctx)
            if item:
                logger.info(f"[whale-journal] 聊天里学到一条（{item['category']}）：{item['text'][:40]}")
        except Exception as e:
            logger.warning(f"[whale-journal] 自动收教学失败：{e}")
