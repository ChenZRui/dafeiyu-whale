"""一轮「醒来」的完整编排：刷视频 → 深读 → 写日记 → B站分享 → QQ 汇报 → 群转发。

对外只暴露 WhalePipeline.run_once() / inbox_once()，调度器与手动命令都调它。
每一步都单独兜底，任何一步挂了都不影响后面的步骤。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from datetime import datetime
from typing import Any

from astrbot.api import logger

from .bili import BiliClient, BiliError, strip_links
from .config import PluginConfig
from .peer import OWNER, PEER, STRANGER, classify, judge_by_llm, looks_suspicious
from .sender import QQSender
from .store import StateStore

MIN_DURATION_SEC = 45  # 太短的视频（搬运/鬼畜）跳过


def _topic_key(s: Any) -> str:
    """把「猫咪（黑猫，三花）」「cs2比赛/战术」变成长度合适的关键词主干。"""
    txt = str(s or "").strip()
    if not txt:
        return ""
    for sep in ("（", "(", "：", ":", "，", ",", "/", "｜", "|"):
        if sep in txt:
            txt = txt.split(sep)[0].strip()
    return txt[:20]


def _topic_hit(blob: str, blob_ns: str, kw: Any) -> bool:
    """标题/简介/标签是否命中主人写的主题。

    逐字全文匹配太死（「猫咪」碰不到「猫主子」「奶猫」），所以再放宽三层：
    ① 去掉空格整体匹配；② 按空格/顿号拆词，任一词命中即可；
    ③ 英文/数字片段（CS2、github、DeepSeek）单独匹配；④ 两字中文主题取首字。
    """
    key = _topic_key(kw)
    if not key:
        return False
    k2 = key.replace(" ", "")
    if k2 and k2 in blob_ns:
        return True
    for tok in re.split(r"[\s,、/·]+", key):
        tok = tok.strip()
        if len(tok) >= 2 and (tok in blob or tok in blob_ns):
            return True
    low = blob.lower()
    for run in re.findall(r"[A-Za-z][A-Za-z0-9.+#_-]{2,}", key):
        if run.lower() in low:
            return True
    if len(key) == 2 and not key.isascii() and key[0] in blob:
        return True
    return False


class WhalePipeline:
    def __init__(
        self,
        cfg: PluginConfig,
        bili: BiliClient,
        store: StateStore,
        sender: QQSender,
        llm: Any,
        diary: Any,
    ) -> None:
        self.cfg = cfg
        self.bili = bili
        self.store = store
        self.sender = sender
        self.llm = llm
        self.diary = diary
        self._running = False

    # ================================================================ 主流程
    async def run_once(self, *, trigger: str = "cron", force: bool = False) -> dict:
        """醒来一次。返回本轮摘要（也会写进 state）。"""
        if self._running:
            logger.info("[bili-whale] 上一轮还没跑完，跳过这次")
            return {"ok": False, "skipped": "busy"}
        self._running = True
        started = time.time()
        # 每轮重读一次人格：主人在面板里改了人设，不用重启插件也能生效
        try:
            self.llm.invalidate_persona()
        except Exception:
            pass
        result: dict[str, Any] = {
            "ok": False,
            "trigger": trigger,
            "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            "steps": [],
        }
        try:
            ok, who = await self.bili.healthy()
            if not ok:
                result["error"] = f"B站登录态不可用：{who}"
                logger.warning(f"[bili-whale] {result['error']}")
                await self.sender.send_friend(f"主人，我这边B站登录态出问题了：{who}\n今晚刷不了了，你去看看凭证吧。")
                return result
            result["self"] = who
            logger.info(f"[bili-whale] 醒来一轮（{trigger}），B站账号 {who}")

            # ⓪ 先把打分台里主人新打的星合进来（她这一轮就要按这个调权重）
            try:
                adopted = self.store.merge_ratings(self.cfg.data_dir / "ratings.json")
                if adopted:
                    logger.info(f"[bili-whale] 从打分台收到主人新打的 {adopted} 条评分")
            except Exception as e:
                logger.warning(f"[bili-whale] 合并打分失败：{e}")

            # ① 收件箱（@我的 / 私信）
            try:
                result["inbox"] = await self.inbox_once()
            except Exception as e:
                logger.warning(f"[bili-whale] 收件箱处理失败：{e}")
                result["inbox"] = {"error": str(e)}

            # ② 选题
            pool = await self._pick_candidates(force=force)
            result["pool"] = len(pool)
            if not pool:
                result["reason"] = "没有新视频可看"
                await self._diary_only(pool=[], note="今晚翻了一圈没找到没看过的，算了。")
                self._finish(result, started)
                return result

            # ③ 打分挑一条
            picked, scored = await self._score(pool)
            result["scored"] = len(scored)
            share_left = self.store.share_quota_left(self.cfg.share_daily_quota)
            can_share = picked is not None and (share_left > 0 or force)
            if picked:
                logger.info(
                    f"[bili-whale] 选中 {picked.get('bvid')} 分数 {picked.get('score')}：{picked.get('title')}"
                )
            else:
                logger.info("[bili-whale] 这轮分数都不够，只看不分享")

            # ④ 深读
            debrief: dict = {}
            video: dict = {}
            if picked:
                try:
                    video = await self.bili.deep_read(
                        picked["bvid"], max_subtitle_chars=self.cfg.subtitle_max_chars
                    )
                    video["score"] = picked.get("score", 0)
                    video["score_reason"] = picked.get("reason", "")
                    debrief = await self.llm.debrief(
                        video,
                        video.get("subtitle") or "",
                        video.get("danmakus") or [],
                        video.get("comments") or [],
                    )
                    result["steps"].append("deep_read")
                except Exception as e:
                    logger.warning(f"[bili-whale] 深读失败：{e}")
                    result["steps"].append(f"deep_read_failed:{e}")
                    video = video or picked
            # 浅看其它候选（给日记提供素材，不读字幕）
            light: list[dict] = []
            for c in scored:
                if picked and c.get("bvid") == picked.get("bvid"):
                    continue
                if len(light) >= 2:
                    break
                light.append(c)

            # ⑤ 写日记
            entry = None
            if self.cfg.diary_enable:
                try:
                    entry = await self._write_diary(video if picked else None, debrief, light)
                    result["steps"].append("diary")
                except Exception as e:
                    logger.warning(f"[bili-whale] 写日记失败：{e}")
                    result["steps"].append(f"diary_failed:{e}")

            # ⑥ B站分享（评论 @ / 私信）
            bili_share: dict = {"skipped": True}
            if picked and can_share and self.cfg.share_via != "off":
                try:
                    bili_share = await self._share_on_bili(video if picked else picked, debrief)
                    result["steps"].append(f"bili_share:{bili_share}")
                except Exception as e:
                    logger.warning(f"[bili-whale] B站分享失败：{e}")
                    bili_share = {"ok": False, "error": str(e)}
            elif picked and not can_share:
                bili_share = {"ok": False, "reason": "今日分享额度用完"}

            # ⑦ 点赞
            if picked and self.cfg.like_enable:
                try:
                    if picked.get("score", 0) >= self.cfg.min_score:
                        await self.bili.like(picked["bvid"])
                        result["steps"].append("like")
                except Exception as e:
                    logger.debug(f"[bili-whale] 点赞失败：{e}")

            # ⑦b 收藏（只收真的想留的：分数够高才收，一轮最多一条）
            if picked and self.cfg.fav_enable:
                try:
                    if int(picked.get("score", 0)) >= self.cfg.fav_min_score:
                        ok_fav = await self.bili.favorite(
                            picked["bvid"],
                            aid=(video or picked).get("aid"),
                            folder_id=self.cfg.fav_folder_id,
                        )
                        if ok_fav:
                            result["steps"].append("favorite")
                except Exception as e:
                    logger.debug(f"[bili-whale] 收藏失败：{e}")

            # ⑦c 每天一枚币，投给主人打星最高的那条
            try:
                coin_info = await self._daily_coin()
                if coin_info:
                    result["steps"].append(f"coin:{coin_info}")
            except Exception as e:
                logger.debug(f"[bili-whale] 投币失败：{e}")

            # ⑧ QQ 私聊汇报 + 群转发
            qq_sent = await self._report_qq(picked, video, debrief, bili_share, entry)
            result["qq"] = qq_sent
            if picked:
                result["video"] = {"bvid": picked["bvid"], "title": picked.get("title")}

            # ⑨ 记账（顺带把当时的想法存下来，之后聊天时她要能想起来）
            v_now = video or picked or {}
            for c in scored[: max(1, self.cfg.max_videos)]:
                is_pick = bool(picked and c.get("bvid") == picked.get("bvid"))
                extra = None
                if is_pick:
                    extra = {
                        "up": str(v_now.get("up") or "")[:60],
                        "url": str(v_now.get("url") or "")[:120],
                        "note": str((debrief or {}).get("note") or "")[:300],
                        "summary": str((debrief or {}).get("summary") or "")[:300],
                        "keyword": str(v_now.get("keyword") or self._matched_topic(v_now))[:30],
                    }
                self.store.mark_seen(
                    c["bvid"],
                    title=c.get("title", ""),
                    score=int(c.get("score") or 0),
                    shared=bool(is_pick and bili_share.get("ok")),
                    cover=str(c.get("cover") or ""),
                    extra=extra,
                )
            self._finish(result, started)
            return result
        except Exception as e:
            logger.exception(f"[bili-whale] run_once 异常：{e}")
            result["error"] = repr(e)
            return result
        finally:
            self._running = False

    def _finish(self, result: dict, started: float) -> None:
        result["ok"] = True
        result["elapsed"] = round(time.time() - started, 1)
        self.store.note_wake()
        self.store.set_summary(self._summary_text(result))
        self.store.save()
        self.refresh_watch_page()

    async def probe_persona(self) -> str:
        """启动时预热一次人设，把来源打进日志（确认真的吃到了 AstrBot 的人格）。"""
        try:
            await self.llm._ensure_persona()
            return self.llm._persona_origin
        except Exception as e:
            logger.warning(f"[bili-whale] 人设预热失败：{e}")
            return ""

    def refresh_watch_page(self) -> str:
        """重画「她看过什么」页面，返回路径（失败返回空串）。"""
        if not self.cfg.watch_page_enable:
            return ""
        try:
            from .report import build_watch_page

            path = build_watch_page(self.store, self.cfg)
            return str(path or "")
        except Exception as e:
            logger.debug(f"[bili-whale] 刷新观看历史页失败：{e}")
            return ""

    def mirror_rating(self, bvid: str, stars: int, note: str = "", fav=None) -> None:
        """QQ 里打的星也要写进打分台那份 ratings.json，网页上才看得到。"""
        path = self.cfg.data_dir / "ratings.json"
        try:
            obj = {}
            if path.exists():
                obj = json.loads(path.read_text(encoding="utf-8")) or {}
            if not isinstance(obj, dict):
                obj = {}
            items = obj.setdefault("items", {})
            if not isinstance(items, dict):
                items = {}
                obj["items"] = items
            old = items.get(str(bvid)) or {}
            rec = {
                "stars": max(1, min(5, int(stars))),
                "note": str(note or "")[:200],
                "fav": bool(old.get("fav")) if fav is None else bool(fav),
                "ts": time.time(),
                "src": "qq",
            }
            items[str(bvid)] = rec
            obj["version"] = 1
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[bili-whale] 回写打分台评分失败：{e}")

    async def push_watch_report(self, *, force: bool = False) -> dict:
        """每天 21:00 给主人发「手机能直接打开的打分链接」，并（可选）附离线 HTML。

        主人要的是：晚上收到一条链接，手机上点开就能打分收藏，打完立刻写回服务器。
        """
        if not (self.cfg.daily_push_enable or force):
            return {"ok": False, "skip": "未开启每日推送"}
        tk = ""
        try:
            tk = self.cfg.watchui_token_file.read_text(encoding="utf-8").strip()
        except Exception:
            logger.warning("[bili-whale] 读不到打分台口令，跳过每日推送")
            return {"ok": False, "error": "no token"}

        rows = self.store.watch_history(200)
        todo = sum(1 for i in rows if int(i.get("stars") or 0) == 0)
        favs = len(self.store.fav_set())
        today = sum(1 for i in rows if str(i.get("date") or "") == time.strftime("%Y-%m-%d"))
        phone = self._public_watch_url(tk)
        sent_link = False
        if self.cfg.daily_push_link and phone:
            head = (
                f"主人，今天又看了 {today} 条，累计 {len(rows)} 条。\n"
                f"这条链接点开就能打分：手机浏览器直接开，点星星、写两句、点收藏，"
                f"存了我就按这个调。\n"
                f"（还有 {todo} 条你没评过，收藏着 {favs} 条）\n{phone}"
            )
            sent_link = await self.sender.send_friend(head)
            logger.info(f"[bili-whale] 每日打分链接已推送（{'成功' if sent_link else '失败'}）：{phone}")

        if not self.cfg.daily_push_send_file and self.cfg.daily_push_link and phone:
            return {"ok": sent_link, "link": phone, "file": ""}

        url = f"{self.cfg.watchui_url}/snapshot"
        full = f"{url}?t={tk}"

        def _fetch() -> str:
            import urllib.request

            req = urllib.request.Request(full, headers={"User-Agent": "bili-whale/1.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read().decode("utf-8", "replace")

        try:
            html = await asyncio.to_thread(_fetch)
        except Exception as e:
            logger.warning(f"[bili-whale] 取打分文件失败：{e}")
            return {"ok": sent_link, "link": phone, "error": repr(e)[:120]}
        if len(html) < 3000:
            logger.warning(f"[bili-whale] 打分文件太小（{len(html)} 字节），不发")
            return {"ok": sent_link, "link": phone, "error": "empty snapshot"}

        path = self.cfg.data_dir / self.cfg.daily_push_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(html, encoding="utf-8")
        except Exception as e:
            logger.warning(f"[bili-whale] 写打分文件失败：{e}")
            return {"ok": sent_link, "link": phone, "error": repr(e)[:120]}

        if sent_link:
            caption = f"顺手存一份离线版（{len(html) // 1024} KB），看不看都行，打分还是走上面那条链接。"
        else:
            caption = (
                f"主人，今天我看过的视频整理成这个文件了，{len(html) // 1024} KB。\n"
                f"帮我打个分呗——还有 {todo} 条我没被你评过。\n"
                f"打完下次醒来我就照着这个调了；当天投币也投你打星最高的那条。"
            )
        ok = await self.sender.send_file(str(path), caption=caption)
        logger.info(f"[bili-whale] 每日打分文件已推送：{path}（{len(html)} 字节，{'成功' if ok else '失败'}）")
        return {"ok": ok or sent_link, "path": str(path), "link": phone, "bytes": len(html)}

    def _public_watch_url(self, tk: str = "") -> str:
        """打分台的公网地址（手机能直接打开在线版）。读不到就返回空。"""
        try:
            u = (self.cfg.data_dir / "public_url.txt").read_text(encoding="utf-8").strip()
        except Exception:
            u = ""
        if not u.startswith("http"):
            try:
                u = (self.cfg.watchui_token_file.parent / "public_url.txt").read_text(encoding="utf-8").strip()
            except Exception:
                u = ""
        if not u.startswith("http"):
            return ""
        if not tk:
            try:
                tk = self.cfg.watchui_token_file.read_text(encoding="utf-8").strip()
            except Exception:
                tk = ""
        return f"{u.rstrip('/')}/watch?t={tk}" if tk else f"{u.rstrip('/')}/watch"

    def _warn_once(self, key: str) -> bool:
        """同一天同类提醒只说一次（免得每轮都复读同一句）。"""
        day = time.strftime("%Y-%m-%d")
        k = f"{day}-{key}"
        if self.store.is_handled("warn", k):
            return False
        self.store.mark_handled("warn", k)
        return True

    def _summary_text(self, r: dict) -> str:
        v = r.get("video") or {}
        parts = [f"本轮 {r.get('pool', 0)} 个候选、{r.get('scored', 0)} 条打分"]
        if v:
            parts.append(f"分享《{v.get('title')}》")
        else:
            parts.append("这轮只看不分享")
        if r.get("qq"):
            parts.append(f"QQ投递 {len(r['qq'])} 处")
        return "｜".join(parts)

    # ================================================================ 选题
    async def _pick_candidates(self, *, force: bool = False) -> list[dict]:
        kws = [str(k).strip() for k in self.cfg.keywords if str(k).strip()]
        use_home = bool(getattr(self.cfg, "feed_homepage", True))
        if not kws and not use_home:
            return []
        # 每轮随机抽 3 个主题，保证长期能覆盖到列表里的每一项
        random.shuffle(kws)
        use = kws[: min(3, len(kws))]
        try:
            raw = await self.bili.feed(
                use, days=7, per_kw=20, use_homepage=use_home
            )
        except Exception as e:
            logger.warning(f"[bili-whale] 选题失败：{e}")
            return []
        pool: list[dict] = []
        dis = [d.strip() for d in self.cfg.dislike_topics if d.strip()]
        for v in raw:
            if not force and self.store.is_seen(v["bvid"]):
                continue
            if int(v.get("duration_sec") or 0) < MIN_DURATION_SEC:
                continue
            # 搜索来的垃圾新视频（零播放零弹幕）直接丢；首页推荐是B站自己算的，信它
            if v.get("from") == "search":
                if int(v.get("play") or 0) < 300 and int(v.get("danmaku") or 0) < 3:
                    continue
            title = v.get("title") or ""
            if any(d and d in title for d in dis):
                continue
            pool.append(v)
        return self._diversify(pool)

    def _diversify(self, pool: list[dict]) -> list[dict]:
        """按来源/主题做配额，别让首页推荐一家独大（否则永远只有数码）。"""
        want = max(1, self.cfg.max_videos)
        size = max(want * 2, int(getattr(self.cfg, "topic_pool_size", 10) or 10))
        home = [v for v in pool if v.get("from") != "search"]
        by_kw: dict[str, list[dict]] = {}
        for v in pool:
            if v.get("from") == "search":
                by_kw.setdefault(str(v.get("keyword") or ""), []).append(v)
        home.sort(key=self._rank, reverse=True)
        for lst in by_kw.values():
            lst.sort(key=self._rank, reverse=True)

        taken: set[str] = set()
        picked: list[dict] = []

        def take(v: dict) -> bool:
            if v["bvid"] in taken or len(picked) >= size:
                return False
            taken.add(v["bvid"])
            picked.append(v)
            return True

        # 首页最多占一半，留出配额给主题搜索
        for v in home[: max(2, size // 2)]:
            take(v)
        # 各主题轮流取，保证每个主题至少有一条能进池
        while len(picked) < size and by_kw:
            progressed = False
            for lst in by_kw.values():
                if len(picked) >= size:
                    break
                while lst:
                    v = lst.pop(0)
                    if v["bvid"] in taken:
                        continue
                    take(v)
                    progressed = True
                    break
            if not progressed:
                break
        # 还不够就用首页剩下的补齐
        for v in home:
            if len(picked) >= size:
                break
            take(v)
        picked.sort(key=self._rank, reverse=True)
        logger.info(
            f"[bili-whale] 候选池 {len(picked)} 条（首页 {sum(1 for v in picked if v.get('from') != 'search')}"
            f" / 主题 {sum(1 for v in picked if v.get('from') == 'search')}）"
        )
        return picked

    def _matched_topic(self, v: dict) -> str:
        """这条视频命中了哪个主人写的主题（没有就空）。"""
        blob = f"{v.get('title','')} {v.get('desc','')} {v.get('tag','')}"
        blob_ns = blob.replace(" ", "")
        for k in self.cfg.keywords:
            if _topic_hit(blob, blob_ns, k):
                return str(k)
        return ""

    def _rank(self, v: dict) -> float:
        score = 0.0
        blob = f"{v.get('title','')} {v.get('desc','')} {v.get('tag','')}"
        blob_ns = blob.replace(" ", "")
        # 主人写的主题（keywords）优先，权重最大
        for k in self.cfg.keywords:
            if _topic_hit(blob, blob_ns, k):
                score += 5.0
        for t in self.cfg.like_topics:
            key = str(t).split("（")[0].strip()
            if key and key in blob:
                score += 3.0
        for d in self.cfg.dislike_topics:
            if d and d in blob:
                score -= 4.0
        # 弹幕密度：每万播放的弹幕数，比播放量更能说明内容有意思
        view = max(1, int(v.get("play") or 0))
        dm = int(v.get("danmaku") or 0)
        score += min(4.0, (dm / view) * 10000 * 0.5)
        score += min(2.0, dm / 50.0)
        # 新鲜度：12 小时内加分
        age_h = (time.time() - int(v.get("pubdate") or 0)) / 3600.0
        if 0 < age_h <= 12:
            score += 2.0
        elif age_h <= 72:
            score += 1.0
        return score

    async def _score(self, pool: list[dict]) -> tuple[dict | None, list[dict]]:
        profile = {
            "like": self.cfg.like_topics,
            "dislike": self.cfg.dislike_topics,
            "topics": self.cfg.keywords,
            "history": self.store.seen_titles(30),
            "recent_topics": self.store.recent_topics(12),
            "rated": self.store.rated_brief(),
            "topic_weights": self.store.topic_weights() if self.cfg.topic_weight_enable else {},
        }
        try:
            scored = await self.llm.score(pool, profile)
        except Exception as e:
            logger.warning(f"[bili-whale] 打分失败，退回启发式排序：{e}")
            scored = [{**v, "score": 0, "reason": ""} for v in pool]
        # 把 score 合并回候选（LLM 可能只返回 bvid/score）
        by_bvid = {str(s.get("bvid")): s for s in scored or []}
        merged: list[dict] = []
        for v in pool:
            s = by_bvid.get(v["bvid"]) or {}
            item = {**v, "score": int(s.get("score") or 0), "reason": str(s.get("reason") or "")}
            merged.append(item)
        merged.sort(key=lambda x: x["score"], reverse=True)
        # 主题轮换：主人写了想看的方向，就别连着几轮都推首页那串数码
        recent = {str(t) for t in self.store.recent_topics(2)}

        # 主人标了收藏的视频，把它们命中的主题记下来 —— 同主题下次多推
        seen_map = self.store.data.get("seen") or {}
        fav_topics: set[str] = set()
        for _bv in self.store.fav_set():
            _title = str((seen_map.get(_bv) or {}).get("title") or "")
            if not _title:
                continue
            _tp = self._matched_topic({"title": _title})
            if _tp:
                fav_topics.add(_tp)

        def _adjusted(c: dict) -> float:
            tp = self._matched_topic(c)
            base = float(c["score"])
            if tp and tp in fav_topics:
                base += 5.0  # 主人收藏过的主题，继续多推同类
            if tp and tp not in recent:
                base += 3.0
            if self.cfg.topic_weight_enable and tp:
                base += self.store.topic_bonus(tp)  # 主人打星高的主题加权，差评主题降权
            return base

        merged.sort(key=_adjusted, reverse=True)
        best = next((c for c in merged if c["score"] >= self.cfg.min_score), None)
        if best:
            logger.info(
                f"[bili-whale] 选题主题：{self._matched_topic(best) or '首页推荐'}"
                f"（分数 {best['score']}，近期方向 {sorted(recent) or '无'}）"
            )
        return best, merged

    # ================================================================ 日记
    async def _write_diary(self, video: dict | None, debrief: dict, light: list[dict]) -> Any:
        from .diary import DiaryEntry  # 延迟导入，模块缺失时不影响主流程

        items: list[dict] = []
        if video:
            items.append(
                {
                    "title": video.get("title", ""),
                    "up": video.get("up", ""),
                    "url": video.get("url", ""),
                    "note": (debrief or {}).get("note", ""),
                    "summary": (debrief or {}).get("summary", ""),
                }
            )
        for c in light:
            items.append(
                {
                    "title": c.get("title", ""),
                    "up": c.get("up", ""),
                    "url": c.get("url", ""),
                    "note": "",
                    "summary": "",
                }
            )
        today = {
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "items": items,
            "letter_hint": "今天看到的东西里，有没有哪句想对主人说的？",
        }
        data = await self.llm.write_diary(
            today,
            self.diary.read_public() if self.diary else "",
            self.diary.read_private() if self.diary else "",
        )
        table_note = str((data or {}).get("table_note") or "")
        vids = []
        for i, it in enumerate(items):
            vids.append(
                {
                    "bvid": _bvid_from_url(it.get("url", "")),
                    "title": it.get("title", ""),
                    "up": it.get("up", ""),
                    "url": it.get("url", ""),
                    "note": table_note if i == 0 and table_note else it.get("note", ""),
                }
            )
        entry = DiaryEntry(
            date=today["date"],
            videos=vids,
            thoughts=str((data or {}).get("thoughts") or ""),
            letter=str((data or {}).get("letter") or ""),
            private=str((data or {}).get("private") or ""),
        )
        self.diary.append_entry(entry)
        return entry

    async def _diary_only(self, pool: list[dict], note: str) -> None:
        if not self.cfg.diary_enable or not self.diary:
            return
        try:
            await self._write_diary(None, {"note": note}, pool[:1])
        except Exception as e:
            logger.warning(f"[bili-whale] 空轮日记失败：{e}")

    # ================================================================ B站分享
    async def _daily_coin(self) -> dict[str, Any] | None:
        """每天最多投 1 枚币，投给主人打星最高的那条（投过的不重复投）。

        她的硬币很少（新号只有 3 枚），所以只认主人的评分，主人没打分就不投。
        """
        if not self.cfg.coin_enable or self.cfg.coin_daily_max <= 0:
            return None
        today = time.strftime("%Y-%m-%d")
        st = self.store.data.setdefault("coin", {})
        if str(st.get("date") or "") != today:
            st["date"] = today
            st["count"] = 0
        if str(st.get("blocked_date") or "") == today:
            return None  # 今天已经被风控拦过（要求人机验证），不再重试
        if int(st.get("count") or 0) >= self.cfg.coin_daily_max:
            return None
        used = set(st.get("done") or [])
        fb = self.store.data.get("feedback") or {}
        cands: list[tuple[int, int, str]] = []
        for bvid, rec in fb.items():
            if not isinstance(rec, dict):
                continue
            stars = int(rec.get("stars") or 0)
            if stars < self.cfg.coin_min_stars or bvid in used:
                continue
            cands.append((stars, int(rec.get("ts") or 0), str(bvid)))
        if not cands:
            return None
        cands.sort(reverse=True)
        stars, _, bvid = cands[0]
        r = await self.bili.coin(bvid, count=1)
        if not r.get("ok"):
            st["last_error"] = str(r.get("message") or "")[:120]
            if r.get("blocked"):
                st["blocked_date"] = today
                await self._ask_owner_to_coin(bvid, stars)
            self.store.save()
            return {
                "ok": False,
                "bvid": bvid,
                "blocked": bool(r.get("blocked")),
                "message": str(r.get("message") or "")[:80],
            }
        st["count"] = int(st.get("count") or 0) + 1
        st["done"] = list(used | {bvid})[-80:]
        st["last_bvid"] = bvid
        st["last_stars"] = stars
        st.pop("last_error", None)
        self.store.save()
        logger.info(f"[bili-whale] 今日投币：{bvid}（主人打了 {stars} 星）")
        return {"ok": True, "bvid": bvid, "stars": stars}

    async def _ask_owner_to_coin(self, bvid: str, stars: int) -> None:
        """投币被 B站 风控拦住时，去 QQ 找主人帮个忙（一天最多一次）。"""
        try:
            if not self.cfg.qq_share_enable:
                return
            title = str(((self.store.data.get("seen") or {}).get(bvid) or {}).get("title") or bvid)
            url = f"https://www.bilibili.com/video/{bvid}"
            text = (
                f"主人，今天挑出来`《{title[:40]}》`想投一枚币"
                f"（你给它打了 {stars} 星），结果B站让我做人机验证，我投不了……"
                f"\n{url}\n你帮我投一个呗，别让我白惦记。"
            )
            await self.sender.send_friend(text)
            logger.info(f"[bili-whale] 投币被风控，已在 QQ 请主人代投 {bvid}")
        except Exception as e:
            logger.debug(f"[bili-whale] 请主人代投失败：{e}")

    async def _share_on_bili(self, video: dict, debrief: dict) -> dict:
        via = self.cfg.share_via
        note = str((debrief or {}).get("note") or "")
        out: dict[str, Any] = {"via": via}
        errs: list[str] = []
        owner_name = await self.bili.uname(self.cfg.owner_bili_mid) or "主人"
        text = await self.llm.bili_share_text(video, note, "comment" if via in ("comment", "both") else "dm")
        text = (text or "").strip()
        if via in ("comment", "both"):
            # 降频 + 去链接：新号每轮都评论、评论里带网址，是 B站 折叠「仅自己可见」的头号原因
            if random.random() > self.cfg.comment_probability:
                out["comment"] = {"ok": False, "skipped": "本轮按降频规则不评论"}
                logger.info(
                    f"[bili-whale] 本轮跳过评论（降频，概率 {self.cfg.comment_probability}）"
                )
            else:
                body = f"@{owner_name} {text}".strip()
                if self.cfg.comment_strip_links:
                    body = strip_links(body) or body
                try:
                    r = await self.bili.send_comment(
                        video["bvid"], body, aid=video.get("aid")
                    )
                    out["comment"] = {"ok": True, "text": body, "rpid": r.get("rpid")}
                except BiliError as e:
                    out["comment"] = {"ok": False, "error": str(e)}
                    errs.append(f"评论：{e}")
                    logger.warning(f"[bili-whale] 评论没发出去：{e}")
        if via in ("dm", "both"):
            try:
                dm_text = text if via == "dm" else await self.llm.bili_share_text(video, note, "dm")
                card_ok = False
                if self.cfg.dm_share_card_enable:
                    try:
                        shared = await self.bili.share_video(
                            self.cfg.owner_bili_mid,
                            aid=int(video.get("aid") or 0),
                            title=str(video.get("title") or ""),
                            author=str(video.get("up") or ""),
                            cover=str(video.get("cover") or ""),
                            bvid=str(video.get("bvid") or ""),
                        )
                        card_ok = bool((shared or {}).get("ok"))
                    except BiliError as e:
                        logger.warning(f"[bili-whale] 视频卡片没发成，退回文本：{e}")
                # 默认只发链接（封面图在手机上点不开，主人还得自己搜）
                title = str(video.get("title") or "").strip()
                url = str(video.get("url") or "").strip()
                if card_ok:
                    body = dm_text
                else:
                    head = f"{title}\n{url}".strip() if title else url
                    body = f"{head}\n{dm_text}".strip()
                    logger.info(f"[bili-whale] B站私信按链接方式发送：{url}")
                await self.bili.send_dm(self.cfg.owner_bili_mid, body)
                out["dm"] = {"ok": True, "text": dm_text, "card": card_ok}
            except BiliError as e:
                out["dm"] = {"ok": False, "error": str(e)}
                errs.append(f"私信：{e}")
                logger.warning(f"[bili-whale] 私信没发出去：{e}")
        out["ok"] = bool((out.get("comment") or {}).get("ok") or (out.get("dm") or {}).get("ok"))
        # 每日额度按「轮」记，不按评论/私信条数记：一轮里 both 也只算一次，
        # 否则一轮就吃掉 2 个额度，一天根本推不了几次。
        if out["ok"]:
            self.store.add_share()
        if errs:
            out["error"] = "；".join(errs)
        return out

    # ================================================================ QQ 汇报
    async def _report_qq(
        self,
        picked: dict | None,
        video: dict,
        debrief: dict,
        bili_share: dict,
        entry: Any,
    ) -> list[str]:
        if not self.cfg.qq_share_enable:
            return []
        done: list[str] = []
        letter = getattr(entry, "letter", "") if entry else ""

        # ① 视频分享（私聊 + 群）
        if picked:
            try:
                text = await self.llm.qq_share_text(video or picked, debrief or {}, letter)
            except Exception as e:
                logger.warning(f"[bili-whale] 生成QQ分享文案失败：{e}")
                text = _fallback_share_text(video or picked, debrief or {})
            cover = (video or picked).get("cover") or ""
            images = [cover] if cover else None

            # 私聊：带上 B站分享结果，让她知道自己在B站干了什么
            extra = ""
            if bili_share.get("comment", {}).get("ok"):
                extra = "\n（在评论区 @你了）"
            elif bili_share.get("dm", {}).get("ok"):
                extra = "\n（B站私信也发你了）"
            elif bili_share.get("error") and self._warn_once("bili_share_blocked"):
                raw = str(bili_share.get("error") or "")
                if "转正" in raw or "等级不够" in raw:
                    extra = "\n（本来想去B站@你、再私信你一遍的，结果账号还没转正，发不出去……这条就先在QQ说给你听）"
                else:
                    extra = f"\n（B站那边没发出去：{raw[:100]}）"
            if await self.sender.send_friend(f"{text}{extra}", images):
                done.append(self.sender.umo_friend(self.cfg.owner_qq))

            # 群：短一点，不要刷屏
            if self.cfg.group_share_enable and self.cfg.group_ids:
                short = f"翻到个视频：{video.get('title','')}\n{video.get('url','')}"
                if debrief.get("note"):
                    short += f"\n{debrief['note'][:80]}"
                for gid in self.cfg.group_ids:
                    if await self.sender.send_group(gid, short, images):
                        done.append(self.sender.umo_group(gid))
        # ② 给他的信
        if letter and self.cfg.diary_push:
            try:
                if await self.sender.send_friend(f"今天的日记写好了。\n\n{letter}"):
                    done.append(self.sender.umo_friend(self.cfg.owner_qq) + "#letter")
            except Exception as e:
                logger.warning(f"[bili-whale] 推送日记失败：{e}")
        return done

    # ================================================================ 收件箱
    async def _bootstrap_inbox(self) -> int:
        """升级/首次运行：把收件箱里现存的都标记成已读，别把历史消息当新消息炸一遍。"""
        n = 0
        try:
            for a in await self.bili.ats():
                self.store.mark_handled("at", _inbox_key(a))
                n += 1
        except Exception as e:
            logger.debug(f"[bili-whale] 初始化 @ 失败：{e}")
        try:
            for r in await self.bili.replies():
                self.store.mark_handled("reply", _inbox_key(r))
                n += 1
        except Exception as e:
            logger.debug(f"[bili-whale] 初始化回复失败：{e}")
        try:
            for m in await self.bili.dms(talker_id=int(self.cfg.owner_bili_mid), limit=20):
                if m.get("from_me"):
                    continue
                self.store.mark_handled("dm", f"{m.get('talker_id')}-{m.get('ts')}")
                n += 1
        except Exception as e:
            logger.debug(f"[bili-whale] 初始化私信失败：{e}")
        self.store.data["inbox_inited"] = True
        self.store.save()
        return n

    async def inbox_once(self) -> dict:
        """扫 @我的 / 回复我的 / 私信 / 未读：该回的评论回掉，再汇报到 QQ。"""
        out: dict[str, Any] = {}
        if not self.store.data.get("inbox_inited"):
            n = await self._bootstrap_inbox()
            logger.info(
                f"[bili-whale] 收件箱第一次扫描：把现存 {n} 条历史消息标记为已读（不回复）"
            )
            return {"bootstrapped": n}
        # 未读计数
        try:
            unread = await self.bili.unread()
            delta = self.store.unread_delta(unread)
            self.store.set_last_unread(unread)
            out["unread"] = unread
            out["delta"] = delta
        except Exception as e:
            logger.debug(f"[bili-whale] 未读检查失败：{e}")

        # @我的
        if self.cfg.at_listen_enable:
            try:
                ats = await self.bili.ats()
                fresh = [
                    a
                    for a in ats
                    if not self.store.is_handled("at", _inbox_key(a))
                ]
                done = []
                for a in fresh[:5]:
                    info = await self._handle_inbox_item(a)
                    self.store.mark_handled("at", _inbox_key(a))
                    if info:
                        done.append(info)
                out["at"] = len(done)
                out["at_replied"] = sum(1 for d in done if d.get("replied"))
                if ats:
                    self.store.set_last_at_id(int(ats[0].get("ts") or 0))
            except Exception as e:
                logger.debug(f"[bili-whale] @收件箱失败：{e}")
                out["at_error"] = str(e)

        # 回复我的（别人在我的评论下发东西）
        if self.cfg.comment_reply_enable:
            try:
                reps = await self.bili.replies()
                fresh = [r for r in reps if not self.store.is_handled("reply", _inbox_key(r))]
                done = []
                for r in fresh[:5]:
                    info = await self._handle_inbox_item(r)
                    self.store.mark_handled("reply", _inbox_key(r))
                    if info:
                        done.append(info)
                out["reply"] = len(done)
                out["reply_replied"] = sum(1 for d in done if d.get("replied"))
                if reps:
                    self.store.set_last_reply_id(int(reps[0].get("ts") or 0))
            except Exception as e:
                logger.debug(f"[bili-whale] 回复收件箱失败：{e}")
                out["reply_error"] = str(e)

        # 私信：主人的私信要回（双向），别人的私信只汇报
        if self.cfg.dm_listen_enable:
            try:
                msgs = await self.bili.dms(talker_id=int(self.cfg.owner_bili_mid), limit=10)
                fresh = [
                    m
                    for m in msgs
                    if not m.get("from_me")
                    and not self.store.is_handled("dm", f"{m.get('talker_id')}-{m.get('ts')}")
                ]
                # 主人连发多条时只回最后一条，其余直接标已读——否则同一轮会连回好几条刷屏
                reply_ts = fresh[-1].get("ts") if fresh else None
                done_dm = []
                for m in fresh:
                    if m.get("ts") == reply_ts:
                        info = await self._handle_dm(m, context=msgs)
                        if info:
                            done_dm.append(info)
                    self.store.mark_handled("dm", f"{m.get('talker_id')}-{m.get('ts')}")
                out["dm"] = len(fresh)
                out["dm_replied"] = sum(1 for d in done_dm if d.get("replied"))
            except Exception as e:
                logger.debug(f"[bili-whale] 私信收件箱失败：{e}")
                out["dm_error"] = str(e)
        return out

    # ------------------------------------------------------------ 私信里的打分/列表
    _RE_RATE = re.compile(
        r"(?:#|第)?\s*(\d{1,3})\s*(?:号|个|条)?\s*[，,、\s]*(\d)\s*(?:星|分)"
    )
    _KW_LIST = ("列表", "看了啥", "看了什么", "观看历史", "最近看", "看了哪些", "看过的")

    def _owner_text_action(self, text: str) -> str | None:
        """主人在 B站私信里直接说「打分 3号 5星 这种多推」/「列表」时的确定性处理。"""
        raw = str(text or "").strip()
        if not raw:
            return None
        m = self._RE_RATE.search(raw)
        if m:
            idx, stars = m.group(1), int(m.group(2))
            stars = max(1, min(5, stars))
            note = raw[m.end():].strip(" ：:，,。.")
            bvid = self.store.bvid_by_index(idx)
            if not bvid:
                return f"找不到第 {idx} 条……先跟我说「列表」看看序号？"
            self.store.rate(bvid, stars, note)
            self.mirror_rating(bvid, stars, note)
            self.refresh_watch_page()
            tail = f"，评价「{note}」" if note else ""
            return f"记下了：第 {idx} 条 {stars} 星{tail}。明天的选题和投币我会看这个。"
        if any(k in raw for k in self._KW_LIST):
            rows = self.store.watch_history(10)
            if not rows:
                return "我还没看过什么视频呢。"
            self.store.set_last_list([v["bvid"] for v in rows])
            self.store.save()
            lines = [f"我最近看了 {len(rows)} 条："]
            for i, v in enumerate(rows, 1):
                star = int(v.get("stars") or 0)
                flag = ("★" * star) if star else "未评"
                lines.append(f"{i}. {flag} {str(v.get('title') or '')[:30]}")
            lines.append("想打分就回我「3号 5星 这种多推」。")
            return "\n".join(lines)
        return None

    async def _handle_dm(self, msg: dict, *, context: list[dict] | None = None) -> dict[str, Any]:
        """一条 B站私信：主人的就回一条，别人的只汇报到 QQ。"""
        msg = msg if isinstance(msg, dict) else {}
        talker = int(msg.get("talker_id") or 0)
        text = str(msg.get("text") or "")
        uname = str(msg.get("uname") or "")
        is_owner = bool(talker) and talker == int(self.cfg.owner_bili_mid or 0)
        info: dict[str, Any] = {
            "talker": talker,
            "uname": uname,
            "text": text,
            "is_owner": is_owner,
            "replied": False,
            "why": "",
        }

        vid = msg.get("video") if isinstance(msg.get("video"), dict) else None
        if is_owner and vid and self.cfg.dm_video_review:
            bvid = str(vid.get("bvid") or "")
            detail: dict = {}
            if bvid:
                try:
                    detail = await self.bili.video_info(bvid) or {}
                except BiliError as e:
                    logger.warning(f"[bili-whale] 拉视频详情失败 {bvid}：{e}")
            try:
                reply = (await self.llm.dm_watch_text({**vid, **(detail or {})}) or "").strip()
                if not reply:
                    raise BiliError("私信评价为空")
                await self.bili.send_dm(talker, reply)
                info["replied"] = True
                info["reply_text"] = reply
                info["video"] = bvid
                logger.info(f"[bili-whale] 主人私信推的视频已看完并回评 {bvid}：{reply[:40]}")
            except Exception as e:
                info["why"] = f"私信评价失败：{e}"
                logger.warning(f"[bili-whale] 私信评价失败：{e}")
            if info["replied"]:
                return info
            hit = None
        else:
            hit = self._owner_text_action(text) if is_owner else None
        if hit:
            try:
                await self.bili.send_dm(talker, hit)
                info["replied"] = True
                info["reply_text"] = hit
                logger.info(f"[bili-whale] 私信里的指令已处理 {talker}：{text[:30]}")
            except Exception as e:
                info["why"] = f"发私信失败：{e}"
        elif not is_owner and not self.cfg.dm_reply_stranger:
            info["why"] = "不是主人的私信，只汇报"
        elif not self.cfg.dm_auto_reply:
            info["why"] = "私信自动回复关着"
        else:
            try:
                reply = await self.llm.dm_reply_text(context or [], last=msg)
            except Exception as e:
                reply = ""
                info["why"] = f"生成私信回复失败：{e}"
            if reply:
                delay = self.cfg.dm_reply_delay_sec
                if delay:
                    await asyncio.sleep(random.randint(0, max(1, delay)))
                try:
                    await self.bili.send_dm(talker, reply)
                    info["replied"] = True
                    info["reply_text"] = reply
                    logger.info(f"[bili-whale] 已回私信 {talker}：{reply[:40]}")
                except Exception as e:
                    info["why"] = f"发私信失败：{e}"
                    logger.warning(f"[bili-whale] 发私信失败 {talker}：{e}")

        if is_owner and not self.cfg.dm_report_to_qq and info.get("replied"):
            # 主人在 B站私信里自己看得到，不用再往 QQ 复述一遍
            logger.debug(f"[bili-whale] 主人私信已回，跳过 QQ 汇报")
            return info

        lines = [f"B站私信：{uname or talker}（{'主人' if is_owner else '别人'}）"]
        if text:
            lines.append(text[:300])
        if info["replied"]:
            lines.append(f"我已回：{info.get('reply_text','')[:150]}")
        elif info["why"]:
            lines.append(f"没回（{info['why']}）")
        try:
            await self.sender.send_friend("\n".join(lines))
        except Exception as e:
            logger.debug(f"[bili-whale] 汇报私信失败：{e}")
        return info

    async def _handle_inbox_item(self, item: dict) -> dict[str, Any] | None:
        """处理一条 @我 / 回复我的：判身份 → 该回就在评论区回掉 → 汇报到 QQ。"""
        uid = int(item.get("uid") or 0)
        uname = item.get("uname") or ""
        text = item.get("text") or ""
        title = item.get("title") or ""
        kind = item.get("kind") or "at"
        bvid = item.get("bvid") or ""
        root = int(item.get("root") or 0)
        parent = int(item.get("parent") or 0)

        # 1) 判身份：关键词快筛 → 可疑才花一次 LLM
        role = classify(
            uid=uid,
            uname=uname,
            text=text,
            owner_mid=int(self.cfg.owner_bili_mid or 0),
            self_mid=int(self.cfg.self_bili_mid or 0),
            keywords=self.cfg.peer_keywords,
        )
        basis = "keyword"
        if role == STRANGER and self.cfg.peer_enable and self.cfg.peer_llm_judge:
            if looks_suspicious(uname, text, self.cfg.peer_keywords):
                if await judge_by_llm(self.llm, uname=uname, text=text, title=title):
                    role = PEER
                    basis = "llm"
        if role == PEER and basis == "keyword":
            # 关键词命中也要过一遍模型确认？不：昵称带"大肥鱼"已经足够强，省 token
            pass

        info: dict[str, Any] = {
            "kind": kind,
            "uid": uid,
            "uname": uname,
            "role": role,
            "basis": basis,
            "text": text,
            "title": title,
            "uri": item.get("uri") or "",
            "replied": False,
            "why": "",
        }

        # 2) 决定回不回
        can_comment = self.cfg.comment_reply_enable and bool(root) and bool(bvid or item.get("aid"))
        thread_key = _thread_key(item)
        turns = self.store.peer_turns(thread_key) if thread_key else 0
        if role == "self":
            info["why"] = "自己的消息，跳过"
            return info
        if not can_comment:
            info["why"] = "缺 bvid/root，没法定位评论"
        elif role == OWNER:
            if not self.cfg.reply_owner:
                info["why"] = "主人回复开关关了"
        elif role == PEER:
            if not self.cfg.peer_enable:
                info["why"] = "同类互动开关关了"
            elif turns >= self.cfg.peer_thread_max_turns:
                info["why"] = f"这条已经聊了 {turns} 轮，收"
            elif self.store.peer_left(self.cfg.peer_daily_limit) <= 0:
                info["why"] = "今天的评论回复额度用完了"
        elif role == STRANGER:
            if not self.cfg.reply_stranger:
                info["why"] = "陌生人回复关了"
            elif self.store.peer_left(self.cfg.peer_daily_limit) <= 0:
                info["why"] = "今天的评论回复额度用完了"

        # 3) 生成文案 + 回评论
        if not info["why"]:
            payload = dict(item)
            payload["title"] = title
            try:
                if role == PEER:
                    txt = await self.llm.peer_chat_text(payload, turns=turns)
                else:
                    txt = await self.llm.at_reply_text(payload)
            except Exception as e:
                txt = ""
                info["why"] = f"生成回复失败：{e}"
            if txt:
                delay = self.cfg.reply_delay_sec
                if delay:
                    await asyncio.sleep(random.randint(0, max(1, delay)))
                try:
                    await self.bili.reply_to_comment(
                        bvid=bvid,
                        aid=int(item.get("aid") or 0),
                        root=root,
                        parent=parent or 0,
                        text=txt,
                    )
                    info["replied"] = True
                    info["reply_text"] = txt
                    self.store.peer_used(uid)
                    if thread_key:
                        self.store.peer_bump(thread_key, uid)
                    logger.info(
                        f"[bili-whale] 已在评论区回复 {uname or uid}（{role}/{basis}）：{txt[:40]}"
                    )
                except Exception as e:
                    info["why"] = f"回复评论失败：{e}"
                    logger.warning(f"[bili-whale] 回复评论失败 {bvid}/{root}：{e}")

        # 4) 汇报到 QQ（回了就写回在哪，没回写为什么）
        tag = {"owner": "主人", "peer": "别的鱼", "stranger": "网友", "self": "她自己"}.get(role, role)
        lines = [f"B站{'@我' if kind == 'at' else '有人回复我'}：{uname or uid}（{tag}）"]
        if text:
            lines.append(text[:160])
        if title:
            lines.append(f"在《{title}》下")
        if info["replied"]:
            lines.append(f"我已回：{info.get('reply_text','')[:150]}")
        elif info["why"]:
            lines.append(f"没回（{info['why']}）")
        if item.get("uri"):
            lines.append(str(item["uri"]))
        try:
            await self.sender.send_friend("\n".join(lines))
        except Exception as e:
            logger.debug(f"[bili-whale] 汇报评论互动失败：{e}")
        return info



def _inbox_key(item: dict) -> str:
    """去重键：优先用消息 id，其次 bvid+时间。

    注意：这里不要再带 kind 前缀——store.is_handled() 自己会拼 `kind:`，
    以前多带一层会变成 `at:at:123`，历史记录就永远对不上、老消息被当新的重复回。
    """
    raw = item.get("raw") or {}
    mid = raw.get("id") if isinstance(raw, dict) else None
    if mid:
        return str(mid)
    return f"{item.get('bvid','')}:{item.get('root',0)}:{item.get('ts',0)}"


def _thread_key(item: dict) -> str:
    """对话线程键：同一个视频的同一条楼层算一条线程。"""
    bvid = item.get("bvid") or ""
    root = int(item.get("root") or 0)
    if not bvid or not root:
        return ""
    return f"{bvid}:{root}"


def _fallback_share_text(video: dict, debrief: dict) -> str:
    title = video.get("title") or ""
    up = video.get("up") or ""
    url = video.get("url") or ""
    note = debrief.get("note") or ""
    summary = debrief.get("summary") or ""
    parts = [f"翻到个视频：《{title}》", f"UP：{up}  链接：{url}"]
    if summary:
        parts.append(f"讲了啥：{summary}")
    if note:
        parts.append(f"我觉得：{note}")
    return "\n".join(parts)


def _bvid_from_url(url: str) -> str:
    import re

    m = re.search(r"(BV[0-9A-Za-z]+)", str(url or ""))
    return m.group(1) if m else ""
