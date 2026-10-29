"""刷B站的大肥鱼 —— AstrBot 插件入口。

她会在夜场里自己醒来：搜近期视频 → 挑一条有感觉的深读（字幕/弹幕/评论）→
写两本日记 → 在B站评论区 @主人 或私信主人 → 把细节讲到 QQ 私聊 →
把喜欢的视频转发到配置好的群。

命令（需要管理员）：
  /刷B站            立刻刷一轮
  /刷B站状态        看她还活着吗、今天分享了几条
  /刷B站群列表      看机器人都在哪些群
  /刷B站日记        看最近一封「给他的信」
  /刷B站测试        B站登录态与各接口自检
  /刷B站暂停 / 刷B站恢复
"""

from __future__ import annotations

import asyncio
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig

from .core.bili import BiliClient
from .core.config import PluginConfig
from .core.pipeline import WhalePipeline
from .core.scheduler import WhaleScheduler
from .core.sender import QQSender
from .core.store import StateStore

# 这两个模块由子模块实现；缺失时降级，不让整个插件加载失败
DiaryStore = None
LLMService = None
try:  # pragma: no cover
    from .core.diary import DiaryStore  # type: ignore
except Exception as _e:  # noqa: F841
    DiaryStore = None
try:  # pragma: no cover
    from .core.llm import LLMService  # type: ignore
except Exception as _e2:  # noqa: F841
    LLMService = None


def _clean_target(v: str) -> str:
    """把「3号」「#3」「第3个」「3 号」「BV1xx」统一成 "3" 或 "BV1xx"。"""
    t = str(v or "").strip()
    t = t.replace("＃", "#").lstrip("#").strip()
    if not t:
        return ""
    up = t.upper()
    if "BV" in up:
        i = up.find("BV")
        return t[i: i + 12]
    for ch in ("号", "第", "个", "条", "视频", "那个", "的", "\u3000", " "):
        t = t.replace(ch, "")
    return t


class _NullLLM:
    """LLM 模块缺失时的兜底：什么也不生成，但接口齐全。"""

    async def text(self, system: str, prompt: str) -> str:
        return ""

    async def score(self, candidates, profile):
        return [{**c, "score": 0, "reason": ""} for c in candidates]

    async def debrief(self, video, transcript, danmaku, comments):
        return {"summary": "", "highlights": [], "note": ""}

    async def write_diary(self, today, history_public, history_private):
        items = today.get("items") or []
        lines = "\n".join(f"- 看了《{i.get('title','')}》" for i in items)
        return {
            "thoughts": lines or "今天没看什么。",
            "letter": "",
            "private": "",
            "table_note": "",
        }

    async def bili_share_text(self, video, note, where):
        return f"这个不错：《{video.get('title','')}》"

    async def qq_share_text(self, video, debrief, letter):
        return f"翻到个视频：{video.get('title','')}\n{video.get('url','')}"

    async def comment_reply_text(self, comment, video):
        return "谢谢～"

    async def at_reply_text(self, item):
        return ""


class _NullDiary:
    """日记模块缺失时的兜底：只在内存里记，不落盘。"""

    def __init__(self, *a, **kw):
        self._entries = []

    def read_public(self) -> str:
        return ""

    def read_private(self) -> str:
        return ""

    def append_entry(self, entry) -> None:
        self._entries.append(entry)

    def latest_letter(self) -> str:
        for e in reversed(self._entries):
            if getattr(e, "letter", ""):
                return e.letter
        return ""

    def stats(self) -> dict:
        return {"entries": len(self._entries), "last_date": "", "path": ""}


class BiliWhalePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.cfg = PluginConfig(config, context)

        self.bili = BiliClient(self.cfg)
        self.store = StateStore(self.cfg.data_dir)
        self.sender = QQSender(context, self.cfg)
        self.llm = LLMService(context, self.cfg) if LLMService else _NullLLM()
        self.diary = DiaryStore(self.cfg.data_dir) if DiaryStore else _NullDiary()
        self.pipeline = WhalePipeline(
            self.cfg, self.bili, self.store, self.sender, self.llm, self.diary
        )
        self.scheduler = WhaleScheduler(
            self.cfg, self.pipeline, inbox_interval_min=self.cfg.inbox_interval_min
        )
        self._paused = False
        self._boot_at = time.time()

    # ------------------------------------------------------------ 生命周期
    async def initialize(self) -> None:
        logger.info("[bili-whale] 插件加载中……")
        if not self.cfg.enable:
            logger.info("[bili-whale] 自动刷B站已关闭（enable=false），只保留手动命令")
            return
        ok, who = await self.bili.healthy()
        if ok:
            logger.info(f"[bili-whale] B站账号就绪：{who}")
            try:
                me = await self.bili.self_info()
                lv = int(me.get("level") or 0)
                if lv < 1:
                    logger.warning(
                        f"[bili-whale] 这个B站号还是 Lv{lv}（未转正）：评论和私信会被平台拒（-107 账号未转正 / 21016 等级不够），"
                        "只能点赞+写日记。转正后（答题过 60 分）会自动恢复，不用改代码。"
                    )
                else:
                    logger.info(f"[bili-whale] B站等级 Lv{lv}，评论/私信可用")
            except Exception:
                pass
            try:
                rel = await self.bili.check_owner_relation()
                logger.info(f"[bili-whale] 和主人的B站关系：attribute={rel.get('attribute')}")
            except Exception:
                pass
        else:
            logger.warning(f"[bili-whale] B站登录态不可用：{who}（会自动重试）")
        self.scheduler.start()
        st = self.store.summary()
        logger.info(f"[bili-whale] 已就绪｜{st}")
        # 预热人设：立刻把「人设从哪来、多少字」打进日志，免得人格没同步上来还看不出来
        try:
            asyncio.create_task(self.pipeline.probe_persona())
        except Exception as e:
            logger.warning(f"[bili-whale] 人设预热任务没起来：{e}")

    async def terminate(self) -> None:
        self.scheduler.shutdown()
        self.store.save()
        logger.info("[bili-whale] 插件已卸载")

    # ------------------------------------------------------------ 命令
    @filter.command("刷B站", alias={"看看B站", "刷b站"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_run(self, event: AstrMessageEvent):
        """立刻让她刷一轮B站"""
        yield event.plain_result("我去了……等我一会儿，别催。")
        try:
            r = await self.pipeline.run_once(trigger="manual", force=True)
        except Exception as e:
            yield event.plain_result(f"翻了，翻车了：{e}")
            return
        yield event.plain_result(self._fmt_result(r))

    @filter.command("刷B站状态")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_status(self, event: AstrMessageEvent):
        """看她现在什么状态"""
        lines = [
            "【大肥鱼的B站行程】",
            f"自动刷：{'开' if self.cfg.enable else '关'}"
            f"{'（临时暂停中）' if self._paused else ''}",
            f"B站账号：{await self._who()}",
            f"状态：{self.store.summary()}",
            f"分享方式：{self.cfg.share_via}｜今日额度 {self.store.shares_today()}/{self.cfg.share_daily_quota}",
            f"群转发：{'开' if self.cfg.group_share_enable and self.cfg.group_ids else '关'}"
            f"（{', '.join(self.cfg.group_ids) or '没配群'}）",
            f"QQ汇报：{'开' if self.cfg.qq_share_enable else '关'} -> {self.cfg.owner_qq or '没配'}",
            f"日记：{self._diary_stats()}",
        ]
        nr = self.scheduler.next_runs()
        if nr:
            lines.append("接下来的安排：")
            for x in nr[:5]:
                lines.append(f"  · {x['name']} @ {x['next']}")
        last = self.store.data.get("last_run_summary")
        if last:
            lines.append(f"上一轮：{last}")
        yield event.plain_result("\n".join(lines))

    @filter.command("刷B站群列表")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_groups(self, event: AstrMessageEvent):
        """看机器人在哪些群（用于配置转发目标）"""
        groups = await self.sender.group_list()
        if not groups:
            yield event.plain_result("没拿到群列表……可能适配器不支持 get_group_list。")
            return
        lines = ["【我在这些群】"]
        for g in groups[:50]:
            mark = " ← 已配" if g["group_id"] in self.cfg.group_ids else ""
            lines.append(f"· {g['group_id']} {g['group_name']}（{g['member_count']}人）{mark}")
        lines.append("把想转发的群号填进插件配置的 group_ids 里就行。")
        yield event.plain_result("\n".join(lines))

    @filter.command("刷B站日记")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_diary(self, event: AstrMessageEvent):
        """看她最近写了什么"""
        st = self._diary_stats()
        letter = ""
        try:
            letter = self.diary.latest_letter()
        except Exception as e:
            letter = f"（读不出来：{e}）"
        txt = f"【日记】{st}\n\n"
        txt += letter if letter else "还没写过什么给你的信……哼，不是没想过。"
        yield event.plain_result(txt)

    @filter.command("刷B站测试")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_selftest(self, event: AstrMessageEvent):
        """B站接口自检"""
        yield event.plain_result("自检中……")
        msgs: list[str] = []
        ok, who = await self.bili.healthy()
        msgs.append(f"登录态：{'✓ ' + who if ok else '✗ ' + who}")
        if ok:
            try:
                rel = await self.bili.check_owner_relation()
                msgs.append(f"和主人关系：attribute={rel.get('attribute')}（6=互关，私信要 6）")
            except Exception as e:
                msgs.append(f"和主人关系：✗ {e}")
            try:
                feed = await self.bili.feed(self.cfg.keywords[:1], days=7, per_kw=5)
                msgs.append(f"搜索：✓ 拿到 {len(feed)} 条")
                if feed:
                    v = feed[0]
                    info = await self.bili.deep_read(v["bvid"], max_subtitle_chars=1500)
                    msgs.append(
                        f"详情：✓ {info.get('title')}｜弹幕 {len(info.get('danmakus') or [])} 条"
                        f"｜评论 {len(info.get('comments') or [])} 条｜字幕 {len(info.get('subtitle') or '')} 字"
                    )
            except Exception as e:
                msgs.append(f"搜索/详情：✗ {e}")
            try:
                dms = await self.bili.dms(talker_id=self.cfg.owner_bili_mid, limit=3)
                msgs.append(f"私信读取：✓ {len(dms)} 条")
            except Exception as e:
                msgs.append(f"私信读取：✗ {e}")
            try:
                u = await self.bili.unread()
                msgs.append(f"未读：{u}")
            except Exception as e:
                msgs.append(f"未读：✗ {e}")
            try:
                ats = await self.bili.ats()
                if ats:
                    a = ats[0]
                    msgs.append(
                        f"@消息：✓ {len(ats)} 条｜最新 {a.get('uname')}｜"
                        f"bvid={a.get('bvid') or '✗'} root={a.get('root') or '✗'} parent={a.get('parent') or '-'}"
                    )
                else:
                    msgs.append("@消息：0 条")
            except Exception as e:
                msgs.append(f"@消息：✗ {e}")
            try:
                reps = await self.bili.replies()
                msgs.append(f"回复我的：✓ {len(reps)} 条")
            except Exception as e:
                msgs.append(f"回复我的：✗ {e}")
        msgs.append(f"LLM：{'✓ 已接入' if LLMService else '✗ 模块缺失'}")
        msgs.append(f"日记模块：{'✓' if DiaryStore else '✗ 模块缺失'}")
        msgs.append(
            f"评论区互动：{'开' if self.cfg.comment_reply_enable else '关'}"
            f"｜主人{'回' if self.cfg.reply_owner else '不回'}"
            f"｜陌生人{'回' if self.cfg.reply_stranger else '不回'}"
            f"｜别的鱼{'开' if self.cfg.peer_enable else '关'}"
            f"（今天已用 {self.store.peer_count_today}/{self.cfg.peer_daily_limit}）"
        )
        msgs.append(f"QQ主人：{self.cfg.owner_qq or '✗ 未配置'}｜群：{self.cfg.group_ids or '未配置'}")
        yield event.plain_result("\n".join(msgs))

    @filter.command("刷B站收件箱")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_inbox(self, event: AstrMessageEvent):
        """立刻扫一遍 @我的/回复我的 并按规则回评论"""
        if not self.cfg.comment_reply_enable:
            yield event.plain_result("评论区互动现在关着（comment_reply_enable=false），开了才回。")
            return
        yield event.plain_result("我去翻翻有没有人 @ 我……")
        try:
            out = await self.pipeline.inbox_once()
        except Exception as e:
            yield event.plain_result(f"翻失败了：{e}")
            return
        parts = [
            f"@我 {out.get('at', 0)} 条（回了 {out.get('at_replied', 0)}）",
            f"回复我 {out.get('reply', 0)} 条（回了 {out.get('reply_replied', 0)}）",
            f"私信 {out.get('dm', 0)} 条",
        ]
        if out.get("at_error"):
            parts.append(f"@收件箱出错：{out['at_error']}")
        if out.get("reply_error"):
            parts.append(f"回复收件箱出错：{out['reply_error']}")
        parts.append(f"今天评论回复已用 {self.store.peer_count_today}/{self.cfg.peer_daily_limit}")
        yield event.plain_result("；".join(parts))

    # ------------------------------------------------------------ 观看历史 / 打分
    @staticmethod
    def _parse_rate_args(text: str) -> tuple[str, int, str] | None:
        """解析「/打分 3 5 太水了」→ ("3", 5, "太水了")；序号或 BV 号都行。"""
        raw = str(text or "").strip()
        for pre in ("/刷B站打分", "刷B站打分", "/打分", "打分"):
            if raw.startswith(pre):
                raw = raw[len(pre):]
                break
        parts = raw.split()
        if len(parts) < 2:
            return None
        target = _clean_target(parts[0])
        try:
            stars = int(_clean_target(parts[1]).rstrip("星分") or 0)
        except Exception:
            return None
        if not 1 <= stars <= 5:
            return None
        note = " ".join(parts[2:]).strip()
        return target, stars, note

    @filter.command("打分", alias={"刷B站打分"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_rate(self, event: AstrMessageEvent, target: str = "", stars: str = ""):
        """给某条视频打分：/打分 3 5 这种多来点（序号来自 /刷B站观看历史）"""
        full = getattr(event, "message_str", "") or ""
        parsed = self._parse_rate_args(full)
        if not parsed and target and stars:
            parsed = self._parse_rate_args(f"{target} {stars}")
        if not parsed:
            yield event.plain_result(
                "打分格式：/打分 <序号或BV号> <1-5星> [评价]\n"
                "例：/打分 3 5 这种多来点\n"
                "先用 /刷B站观看历史 看序号。"
            )
            return
        tgt, star, note = parsed
        bvid = tgt if tgt.upper().startswith("BV") else self.store.bvid_by_index(tgt)
        if not bvid:
            yield event.plain_result(
                f"没找到「{tgt}」对应的视频。先用 /刷B站观看历史 看一眼序号，或者直接发 BV 号。"
            )
            return
        fav = None
        low = note.lower()
        if any(k in note for k in ("收藏", "存起来", "收进")):
            fav = True
            note = note.replace("收藏", "").replace("存起来", "").strip()
        rec = self.store.rate(bvid, star, note, fav)
        self.pipeline.mirror_rating(bvid, star, note, fav)
        self.pipeline.refresh_watch_page()
        title = ""
        for v in self.store.watch_history(200):
            if v["bvid"] == bvid:
                title = v.get("title", "")
                break
        msg = f"记住了：{'★' * star}{'☆' * (5 - star)}《{title or bvid}》"
        if note:
            msg += f"｜{note}"
        if fav:
            msg += "\n这条我收进收藏夹了（记着呢）。"
        if star >= 4:
            msg += "\n哼，那我下次多找点这种的。"
        elif star <= 2:
            msg += "\n行吧，这种我下次躲着走。"
        yield event.plain_result(msg)

    @filter.command("收藏", alias={"刷B站收藏"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_fav(self, event: AstrMessageEvent, target: str = ""):
        """给某条视频打收藏标签：/收藏 3（序号来自 /刷B站观看历史）"""
        full = getattr(event, "message_str", "") or ""
        raw = full
        for pre in ("/刷B站收藏", "刷B站收藏", "/收藏", "收藏"):
            if raw.startswith(pre):
                raw = raw[len(pre):]
                break
        tgt = _clean_target(raw.strip() or target)
        if not tgt:
            rows = [v for v in self.store.watch_history(50) if v.get("fav")]
            if not rows:
                yield event.plain_result("收藏夹还空着。发 /收藏 <序号> 把喜欢的收进来（先用 /刷B站观看历史 看序号）。")
                return
            lines = [f"【收藏夹 · {len(rows)} 条】"]
            for i, v in enumerate(rows, 1):
                _st = int(v.get("stars") or 0)
                lines.append(f"{i}. 《{v.get('title','')[:32]}》{('★' * _st) if _st else '·未评'}")
            yield event.plain_result("\n".join(lines))
            return
        bvid = tgt if tgt.upper().startswith("BV") else self.store.bvid_by_index(tgt)
        if not bvid:
            yield event.plain_result(f"没找到「{tgt}」对应的视频，先用 /刷B站观看历史 看序号。")
            return
        on = self.store.toggle_fav(bvid)
        self.pipeline.mirror_rating(bvid, int((self.store.data.get("feedback", {}).get(bvid) or {}).get("stars") or 5), "", on)
        self.pipeline.refresh_watch_page()
        title = ""
        for v in self.store.watch_history(200):
            if v["bvid"] == bvid:
                title = v.get("title", "")
                break
        yield event.plain_result(
            ("收进收藏夹了：《%s》。以后同类的我多留意。" % (title or bvid))
            if on
            else ("从收藏夹拿掉了：《%s》。" % (title or bvid))
        )

    @filter.command("刷B站观看历史", alias={"观看历史", "刷B站列表"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_watch_history(self, event: AstrMessageEvent):
        """看她最近看过什么，带主人给的分数"""
        rows = self.store.watch_history(15)
        if not rows:
            yield event.plain_result("还没看过什么……不是我不想看，是还没轮到我。")
            return
        self.store.set_last_list([v["bvid"] for v in rows])
        self.store.save()
        lines = ["【她最近看过】"]
        for i, v in enumerate(rows, 1):
            star = int(v.get("stars") or 0)
            flag = ("★" * star) if star else "·未评"
            shared = "推" if v.get("shared") else "看"
            if v.get("fav"):
                shared += "★"
            lines.append(
                f"{i}. {flag} [{shared}] {v.get('title','')[:28]}\n   {v.get('bvid','')} {v.get('date','')}"
            )
            if v.get("note"):
                lines.append(f"   主人说：{v['note']}")
        lines.append("")
        lines.append("打分：/打分 <序号> <1-5> [评价]，比如 /打分 2 5 这种多来点")
        lines.append("收藏：/收藏 <序号>（或打 5 星自动算收藏）")
        page = self.pipeline.refresh_watch_page()
        if page:
            lines.append("网页版在服务器：%s（双击 看观看历史.bat 拉一份）" % page)
        pub = self.pipeline._public_watch_url()
        if pub:
            lines.append("手机上直接开：%s" % pub)
        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="watch_list")
    async def watch_list(self, event: AstrMessageEvent, limit: int = 10):
        """列出一份大肥鱼最近看过的B站视频（带序号、她给的分数、是否已经推给主人），供主人打分。

        Args:
            limit(number): 列几条，默认 10，最多 20
        """
        n = max(3, min(20, int(limit or 10)))
        rows = self.store.watch_history(n)
        if not rows:
            return "她还没看过什么视频，没有可以列出来的。"
        self.store.set_last_list([v["bvid"] for v in rows])
        self.store.save()
        out = [f"她最近看过的 {len(rows)} 条（点序号就能打分）："]
        for i, v in enumerate(rows, 1):
            star = int(v.get("stars") or 0)
            flag = ("★" * star) if star else "未评"
            out.append(
                f"{i}. {flag} {v.get('title', '')[:34]}｜{v.get('date', '')}"
                f"｜她给{v.get('score') or 0}分｜{v.get('bvid', '')}"
                + (f"｜主人评价：{v['note']}" if v.get("note") else "")
            )
        out.append("打分方式：主人说「3号 5星，这种多推」即可，你调用 rate_video。")
        return "\n".join(out)

    @filter.llm_tool(name="rate_video")
    async def rate_video(
        self,
        event: AstrMessageEvent,
        target: str = "",
        stars: int = 5,
        note: str = "",
    ):
        """主人让你给某条视频打分时调用（1-5 星，可带一句评价）。

        Args:
            target(string): 视频序号（1 开始，来自最近一次观看历史列表）或 BV 号
            stars(number): 1 到 5 的整数星数
            note(string): 主人对这条视频的评价原话，可空
        """
        tgt = _clean_target(target)
        star = max(1, min(5, int(stars or 5)))
        bvid = tgt if tgt.upper().startswith("BV") else self.store.bvid_by_index(tgt)
        if not bvid and tgt.isdigit():
            # 序号越界时给出人话提示
            n = len(self.store.watch_history(50))
            return f"「{tgt}」超出范围了，她最近一共 {n} 条，先看列表再报序号。"
        if not bvid:
            return f"没找到「{tgt}」对应的视频，请让主人先发 /刷B站观看历史 看序号，或者直接报 BV 号。"
        self.store.rate(bvid, star, note)
        self.pipeline.mirror_rating(bvid, star, note)
        self.pipeline.refresh_watch_page()
        return f"已记下：{bvid} 给 {star} 星" + (f"，评价「{note}」" if note else "")

    @filter.command("刷B站打分表", alias={"打分表", "推送打分表"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_push_panel(self, event: AstrMessageEvent):
        """立刻把打分链接（+离线文件）发一份给你"""
        yield event.plain_result("行，我把打分链接发你，等会儿。")
        res = await self.pipeline.push_watch_report(force=True)
        if not res.get("ok"):
            yield event.plain_result(f"没弄成：{res.get('error') or res.get('skip') or '未知原因'}")
            return
        link = res.get("link") or ""
        if link:
            msg = f"链接发你了：\n{link}\n手机点开就能打分、收藏，打完下一轮我就照着调。"
        else:
            msg = (
                f"发你了（{int(res.get('bytes') or 0) // 1024} KB）。\n"
                "打不开手机链接的话，电脑上双击「看观看历史.bat」也能点星星、写评价。"
            )
        yield event.plain_result(msg)

    @filter.command("刷B站暂停")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_pause(self, event: AstrMessageEvent):
        """暂停自动刷"""
        self._paused = True
        self.scheduler.shutdown()
        yield event.plain_result("行吧，那我躺着了。想让我动的时候喊 /刷B站恢复。")

    @filter.command("刷B站恢复")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_resume(self, event: AstrMessageEvent):
        """恢复自动刷"""
        self._paused = False
        self.scheduler.start()
        yield event.plain_result("起来了起来了……真麻烦。夜场照旧。")

    # ------------------------------------------------------------ 聊天时的记忆
    @filter.on_llm_request()
    async def inject_bili_memory(self, event: AstrMessageEvent, req) -> None:
        """每次对话前，把「她刷B站的真实记忆」塞进 system prompt。

        不然 QQ 里聊到「你刷的那个视频」她一脸茫然，只会说不知道。
        """
        try:
            block = self._memory_block()
        except Exception as e:
            logger.debug(f"[bili-whale] 记忆注入失败：{e}")
            return
        if not block:
            return
        req.system_prompt = (req.system_prompt or "") + "\n\n" + block
        try:
            now = time.time()
            if now - getattr(self, "_last_mem_log", 0) > 300:
                self._last_mem_log = now
                logger.info(f"[bili-whale] 聊天记忆已注入 system prompt：{len(block)} 字")
        except Exception:
            pass

    @filter.llm_tool(name="recall_bili_memory")
    async def recall_bili_memory(self, event: AstrMessageEvent, keyword: str = ""):
        """回想你自己刷B站时看过什么视频、写过什么日记。主人问起你刷过的东西、或者你自己想知道之前干了什么时调用。

        Args:
            keyword(string): 想回想的关键词，例如视频标题里的词、UP主名字、"日记"、"信"。留空则返回最近看过的。
        """
        try:
            return self._memory_block(keyword=keyword, full=True) or "最近什么都没看……"
        except Exception as e:
            return f"想不起来了：{e}"

    def _memory_block(self, keyword: str = "", *, full: bool = False) -> str:
        """拼一段「我刷B站的真实记忆」，注入给聊天人格。"""
        kw = str(keyword or "").strip()
        limit = 8 if (full or kw) else 4
        mems = self.store.recent_memories(limit)
        letters: list[str] = []
        if kw:
            kwl = kw.lower()
            if kwl in ("日记", "diary", "信", "letter"):
                try:
                    letters = [self.diary.latest_letter() or ""]
                except Exception:
                    letters = []
            else:
                mems = [
                    m
                    for m in mems
                    if kwl in (m.get("title", "") + m.get("up", "") + m.get("note", "") + m.get("summary", "")).lower()
                ]
        if not mems and not any(letters):
            return ""
        lines = [
            "【你自己刷B站的记忆｜这些是真实发生过的，不是设定】",
            "你有一个自己的B站号（ChenZRui的蓝色大肥鱼），每天会自己挑视频看、写日记、给主人写信。"
            "主人一旦聊到这些，你就当自己真的记得，自然地接话；细节记不清就说记不清，别编。",
        ]
        for m in mems:
            head = f"· {m.get('date','')} 看了《{m.get('title','')}》"
            if m.get("up"):
                head += f"（UP：{m['up']}）"
            if m.get("shared"):
                head += "，这条分享给主人了"
            lines.append(head)
            if m.get("note"):
                lines.append(f"  你当时的想法：{m['note'][:200] if not full else m['note']}")
            elif m.get("summary"):
                lines.append(f"  讲的是：{m['summary'][:200]}")
            if m.get("url"):
                lines.append(f"  链接：{m['url']}")
        if not letters:
            try:
                lt = self.diary.latest_letter() or ""
            except Exception:
                lt = ""
            if lt:
                letters = [lt]
        if letters and letters[0]:
            lines.append("")
            lines.append("你最近给他写的信里说：")
            lines.append(letters[0][: 1200 if full else 450])
        return "\n".join(lines)

    # ------------------------------------------------------------ 内部
    async def _who(self) -> str:
        ok, who = await self.bili.healthy()
        return who if ok else f"未就绪（{who}）"

    def _diary_stats(self) -> str:
        try:
            st = self.diary.stats() or {}
            return f"{st.get('entries', 0)} 条记录｜最近 {st.get('last_date') or '无'}｜{st.get('path') or ''}"
        except Exception as e:
            return f"读取失败：{e}"

    def _fmt_result(self, r: dict) -> str:
        if not r.get("ok"):
            return f"这轮没跑成：{r.get('error') or r.get('reason') or r.get('skipped') or '未知原因'}"
        lines = [f"跑完了，用了 {r.get('elapsed')} 秒。"]
        v = r.get("video") or {}
        if v:
            lines.append(f"挑了：《{v.get('title')}》（{v.get('bvid')}）")
        else:
            lines.append("这轮只看没分享，没那么值得推的。")
        lines.append(f"候选 {r.get('pool', 0)} 个，打分 {r.get('scored', 0)} 条")
        qq = r.get("qq") or []
        if qq:
            lines.append(f"投递了 {len(qq)} 处：{', '.join(str(x).split(':')[-1] for x in qq)}")
        inbox = r.get("inbox") or {}
        if inbox.get("at") or inbox.get("dm"):
            lines.append(f"顺手处理了 @{inbox.get('at', 0)} 条 / 私信 {inbox.get('dm', 0)} 条")
        return "\n".join(lines)
