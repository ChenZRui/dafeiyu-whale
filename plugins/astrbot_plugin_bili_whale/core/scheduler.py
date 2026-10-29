"""每日行程调度：在几个固定时间段里随机抽时刻醒来刷B站；另外定时扫 @我的 / 私信。

用 APScheduler 的 AsyncIOScheduler，两种模式：

1. **分时段随机模式**（`day_random=true`，推荐）
   - 配置 `day_windows`：如 [{"start":"07:00","end":"09:00","count":2}, ...]，
     每个窗口里随机抽 `count` 个时刻，同窗口内相邻至少隔 `day_slot_gap_min` 分钟
     （窗口太短时自动收紧，保证放得下）；
   - 每天 `plan_cron` 到点生成当天全部窗口的行程；计划写进 state.json 的 `plan`
     字段，进程重启后把没跑完的接回来（过期 5 分钟以上标记 skipped，不补跑）；
   - 启动时如果今天还没排（或窗口是后来才加的），自动把今天剩下的窗口补排上。
2. **旧夜场模式**（`day_random=false`）
   - `night_cron` 开场，一场 `night_runs` 轮，
     轮间隔 `run_interval_min ± run_interval_jitter` 分钟。

收件箱用 IntervalTrigger，只在她真的有新消息时才往 QQ 发东西。
"""

from __future__ import annotations

import random
import time
import zoneinfo
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import logger

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    APS_OK = True
except Exception as e:  # pragma: no cover
    APS_OK = False
    APS_ERR = repr(e)

TZ = zoneinfo.ZoneInfo("Asia/Shanghai")
LATE_TOLERANCE_SEC = 300  # 重启恢复时，超过这么久没跑的轮次就放弃


class WhaleScheduler:
    def __init__(self, cfg, pipeline, *, inbox_interval_min: int = 30) -> None:
        self.cfg = cfg
        self.pipeline = pipeline
        self.inbox_interval_min = inbox_interval_min
        self.scheduler: Any = None
        self._started = False
        self._token = 0  # 每次重排计划都 +1，旧的 DateTrigger 自动作废

    # ------------------------------------------------------------ 生命周期
    def start(self) -> bool:
        if not APS_OK:
            logger.error(f"[bili-whale] APScheduler 不可用：{APS_ERR}")
            return False
        if self._started:
            return True
        try:
            self.scheduler = AsyncIOScheduler(timezone=TZ)
            self.scheduler.start()
            self._started = True
        except Exception as e:
            logger.error(f"[bili-whale] 调度器启动失败：{e}")
            return False

        if self.cfg.day_random:
            try:
                self.scheduler.add_job(
                    self._plan_day,
                    CronTrigger.from_crontab(self.cfg.plan_cron, timezone=TZ),
                    name="bili_whale_plan",
                    max_instances=1,
                    misfire_grace_time=3600,
                    replace_existing=True,
                )
            except Exception as e:
                logger.error(f"[bili-whale] 计划 cron 不合法 {self.cfg.plan_cron!r}：{e}")
            self._ensure_plan()
        else:
            try:
                self.scheduler.add_job(
                    self._night_open,
                    CronTrigger.from_crontab(self.cfg.night_cron, timezone=TZ),
                    name="bili_whale_night",
                    max_instances=1,
                    misfire_grace_time=3600,
                    replace_existing=True,
                )
            except Exception as e:
                logger.error(f"[bili-whale] 夜场 cron 不合法 {self.cfg.night_cron!r}：{e}")

        if self.cfg.at_listen_enable or self.cfg.dm_listen_enable:
            try:
                self.scheduler.add_job(
                    self._inbox_tick,
                    IntervalTrigger(minutes=max(5, self.inbox_interval_min), timezone=TZ),
                    name="bili_whale_inbox",
                    max_instances=1,
                    misfire_grace_time=600,
                    replace_existing=True,
                )
            except Exception as e:
                logger.warning(f"[bili-whale] 收件箱任务注册失败：{e}")

        if self.cfg.daily_push_enable:
            try:
                self.scheduler.add_job(
                    self._daily_push,
                    CronTrigger.from_crontab(self.cfg.daily_push_cron, timezone=TZ),
                    name="bili_whale_daily_push",
                    max_instances=1,
                    misfire_grace_time=1800,
                    replace_existing=True,
                )
                logger.info(f"[bili-whale] 每日打分文件推送：{self.cfg.daily_push_cron}")
            except Exception as e:
                logger.warning(f"[bili-whale] 每日推送 cron 不合法 {self.cfg.daily_push_cron!r}：{e}")

        if self.cfg.day_random:
            logger.info(
                f"[bili-whale] 调度器已启动：时段 {self.cfg.day_windows_text}，"
                f"每天 {self.cfg.plan_cron} 重排，同窗口内间隔至少 {self.cfg.day_slot_gap_min} 分钟，"
                f"收件箱每 {self.inbox_interval_min} 分钟"
            )
        else:
            logger.info(
                f"[bili-whale] 调度器已启动：夜场 {self.cfg.night_cron} 每次 {self.cfg.night_runs} 轮，"
                f"收件箱每 {self.inbox_interval_min} 分钟"
            )
        return True

    def shutdown(self) -> None:
        self._started = False
        self._token += 1
        try:
            if self.scheduler:
                self.scheduler.shutdown(wait=False)
        except Exception as e:
            logger.debug(f"[bili-whale] 调度器关闭异常：{e}")
        self.scheduler = None

    # ------------------------------------------------------------ 工具
    @property
    def _store(self):
        return self.pipeline.store

    async def _daily_push(self) -> None:
        """每天 daily_push_cron 到点：把「看过什么」打分文件发到 QQ。"""
        try:
            res = await self.pipeline.push_watch_report()
            if not res.get("ok"):
                logger.warning(f"[bili-whale] 每日打分文件没发出去：{res}")
        except Exception as e:
            logger.exception(f"[bili-whale] 每日推送异常：{e}")

    def _windows(self, day: datetime) -> list[dict]:
        """把配置里的时段展开成今天的 (start, end, count) 列表。"""
        out: list[dict] = []
        for i, spec in enumerate(self.cfg.day_windows):
            try:
                s, e, cnt = spec
                h1, m1 = _hm(s)
                h2, m2 = _hm(e)
            except Exception:
                logger.warning(f"[bili-whale] 时段配置写错了，跳过：{spec!r}")
                continue
            start = day.replace(hour=h1, minute=m1, second=0, microsecond=0)
            end = day.replace(hour=h2, minute=m2, second=0, microsecond=0)
            if end <= start:
                # 跨天窗口（如 22:00~02:00）按 +1 天算
                end = end + timedelta(days=1)
            out.append({"idx": i, "start": start, "end": end, "count": max(1, int(cnt)), "text": f"{s}~{e}"})
        return out

    def _save_plan(self, slots: list[dict], reason: str) -> None:
        self._store.data["plan"] = {
            "date": datetime.now(TZ).strftime("%Y-%m-%d"),
            "slots": slots,
            "planned_at": int(time.time()),
            "reason": reason,
        }
        self._store.save()

    def _mark_slot(self, index: int, *, done: bool = False, skipped: bool = False) -> None:
        plan = self._store.data.get("plan") or {}
        slots = plan.get("slots") or []
        if 0 <= index < len(slots):
            if done:
                slots[index]["done"] = True
            if skipped:
                slots[index]["skipped"] = True
            self._store.data["plan"] = plan
            self._store.save()

    # ------------------------------------------------------------ 随机计划
    async def _plan_day(self) -> None:
        """每天 plan_cron 到点：给今天每个时段各排一串随机时刻。"""
        if not self.cfg.enable:
            logger.info("[bili-whale] 自动刷B站已关闭，跳过排程")
            return
        self._commit(self._make_slots(datetime.now(TZ)), reason="cron")

    def _make_slots(self, now: datetime, only_wins: set[int] | None = None) -> list[dict]:
        """按今天的各个时段现抽时刻，返回 [{'ts':int,'win':idx}, ...]。"""
        out: list[dict] = []
        for w in self._windows(now):
            if only_wins is not None and w["idx"] not in only_wins:
                continue
            if w["end"] <= now + timedelta(minutes=5):
                logger.info(f"[bili-whale] 时段 {w['text']} 已经过了，跳过")
                continue
            begin = max(w["start"], now + timedelta(minutes=2))
            span_min = (w["end"] - begin).total_seconds() / 60.0
            if span_min < 5:
                logger.info(f"[bili-whale] 时段 {w['text']} 快结束了，不再排")
                continue
            n = w["count"]
            gap = max(5, min(self.cfg.day_slot_gap_min, int(span_min / n)))
            whens = _random_slots(begin, w["end"] - timedelta(minutes=1), n, gap)
            pretty = " / ".join(t.strftime("%H:%M") for t in whens)
            logger.info(f"[bili-whale] {w['text']} 抽了 {len(whens)} 次：{pretty}")
            out += [{"ts": int(t.timestamp()), "win": w["idx"]} for t in whens]
        return out

    def _commit(self, entries: list[dict], *, reason: str = "cron") -> list[datetime]:
        """把一批时刻写进 state.json 并挂到调度器上；旧的一批自动作废。"""
        if not entries:
            logger.info("[bili-whale] 今天没有可排的时段")
            return []
        entries = sorted(entries, key=lambda x: int(x["ts"]))
        self._token += 1
        token = self._token
        slots_rec = [
            {"ts": int(e["ts"]), "win": int(e.get("win", -1)), "done": False, "skipped": False} for e in entries
        ]
        self._save_plan(slots_rec, reason=reason)

        whens: list[datetime] = []
        for i, e in enumerate(entries):
            when = datetime.fromtimestamp(int(e["ts"]), TZ)
            whens.append(when)
            try:
                self.scheduler.add_job(
                    self._run_slot,
                    DateTrigger(run_date=when, timezone=TZ),
                    args=[i, token],
                    name=f"bili_whale_wake_{when.strftime('%m%d_%H%M')}",
                    max_instances=1,
                    misfire_grace_time=1800,
                    replace_existing=True,
                )
            except Exception as ex:
                logger.error(f"[bili-whale] 安排第 {i + 1} 次醒来失败：{ex}")
        pretty = " / ".join(w.strftime("%H:%M") for w in whens)
        logger.info(f"[bili-whale] 今天排了 {len(whens)} 次醒来：{pretty}")
        return whens

    def _ensure_plan(self) -> None:
        """启动时：接回今天没跑完的，顺手把还没排的时段补上。"""
        now = datetime.now(TZ)
        today = now.strftime("%Y-%m-%d")
        plan = self._store.data.get("plan") or {}
        planned_wins: set[int] = set()
        kept: list[dict] = []

        if plan.get("date") == today:
            slots = plan.get("slots") or []
            for s in slots:
                planned_wins.add(int(s.get("win", -1)))
                if s.get("done") or s.get("skipped"):
                    continue
                ts = int(s.get("ts") or 0)
                if ts >= time.time() - LATE_TOLERANCE_SEC:
                    kept.append({"ts": ts, "win": int(s.get("win", -1))})
                else:
                    s["skipped"] = True
            self._store.data["plan"] = plan
            self._store.save()

        if not self.cfg.enable:
            return

        # 今天已经有计划：只补「配置里新增、今天还没排过」的时段
        fresh: list[dict] = []
        if plan.get("date") == today:
            all_wins = {w["idx"] for w in self._windows(now)}
            missing_wins = all_wins - planned_wins
            if missing_wins:
                fresh = self._make_slots(now, only_wins=missing_wins)
        else:
            fresh = self._make_slots(now)

        entries = kept + fresh
        if not entries:
            if plan.get("date") == today:
                logger.info("[bili-whale] 今天的行程都跑过了")
            return
        if kept and fresh:
            logger.info(f"[bili-whale] 接着跑 {len(kept)} 次，另补排 {len(fresh)} 次")
        elif kept:
            logger.info(f"[bili-whale] 接着跑今天的行程：还有 {len(kept)} 次")
        elif plan.get("date") == today:
            logger.info("[bili-whale] 今天有新增时段，现在补排")
        else:
            logger.info("[bili-whale] 今天还没有行程，现在排")
        self._commit(entries, reason="restart")

    # ------------------------------------------------------------ 旧夜场
    async def _night_open(self) -> None:
        """旧模式：cron 到点，安排今晚的几次醒来。"""
        if not self.cfg.enable:
            logger.info("[bili-whale] 自动刷B站已关闭，跳过夜场")
            return
        self._token += 1
        token = self._token
        runs = self.cfg.night_runs
        delay_total = 0
        now = datetime.now(TZ)
        logger.info(f"[bili-whale] 今晚开场，共 {runs} 轮")
        for i in range(runs):
            if i == 0:
                when = now + timedelta(seconds=random.randint(10, 90))
            else:
                base = self.cfg.run_interval_min
                jit = self.cfg.run_interval_jitter
                gap = max(3, base + random.randint(-jit, jit))
                delay_total += gap
                when = now + timedelta(minutes=delay_total)
            try:
                self.scheduler.add_job(
                    self._run_slot,
                    DateTrigger(run_date=when, timezone=TZ),
                    args=[i, token],
                    name=f"bili_whale_wake_{when.strftime('%m%d_%H%M')}",
                    max_instances=1,
                    misfire_grace_time=1800,
                    replace_existing=True,
                )
                logger.info(f"[bili-whale] 第 {i + 1} 次醒来安排在 {when.strftime('%H:%M')}")
            except Exception as e:
                logger.error(f"[bili-whale] 安排第 {i + 1} 次醒来失败：{e}")

    # ------------------------------------------------------------ 醒来干活
    async def _run_slot(self, index: int, token: int) -> None:
        if token != self._token:
            logger.info("[bili-whale] 行程已重排，跳过这次")
            return
        if not self.cfg.enable:
            return
        plan = self._store.data.get("plan") or {}
        slots = plan.get("slots") or []
        win = slots[index]["win"] if 0 <= index < len(slots) else -1
        if self.cfg.day_random:
            label = f"day#{index + 1}" + (f"(时段{win + 1})" if win >= 0 else "")
        else:
            label = f"night#{index + 1}"
        try:
            r = await self.pipeline.run_once(trigger=label)
            logger.info(f"[bili-whale] 第 {index + 1} 轮结束：{r.get('ok')} {r.get('reason') or ''}")
        except Exception as e:
            logger.exception(f"[bili-whale] 第 {index + 1} 轮异常：{e}")
        finally:
            try:
                self._mark_slot(index, done=True)
            except Exception as e:
                logger.debug(f"[bili-whale] 记录轮次失败：{e}")

    # ------------------------------------------------------------ 收件箱
    async def _inbox_tick(self) -> None:
        if not self.cfg.enable:
            return
        try:
            await self.pipeline.inbox_once()
        except Exception as e:
            logger.debug(f"[bili-whale] 收件箱轮询失败：{e}")

    # ------------------------------------------------------------ 手动
    async def run_now(self, *, force: bool = True) -> dict:
        """手动触发一轮（/刷B站 命令用）。"""
        return await self.pipeline.run_once(trigger="manual", force=force)

    def next_runs(self) -> list[dict]:
        """看看接下来排了啥（给状态命令用）。"""
        if not self.scheduler:
            return []
        out = []
        try:
            for job in self.scheduler.get_jobs():
                out.append(
                    {
                        "name": job.name,
                        "next": job.next_run_time.strftime("%m-%d %H:%M:%S")
                        if getattr(job, "next_run_time", None)
                        else "",
                    }
                )
        except Exception:
            pass
        return out


def _hm(s: Any) -> tuple[int, int]:
    hh, mm = str(s).strip().split(":")[:2]
    return int(hh), int(mm)


def _random_slots(begin: datetime, end: datetime, n: int, gap_min: int, tries: int = 400) -> list[datetime]:
    """在 [begin, end] 里随机取 n 个时刻，相邻至少 gap_min 分钟。"""
    span = (end - begin).total_seconds()
    if span <= 0 or n <= 0:
        return [begin] if n > 0 else []
    if n == 1:
        return [begin + timedelta(seconds=random.uniform(0, span))]
    gap = gap_min * 60
    for _ in range(tries):
        pts = sorted(random.uniform(0, span) for _ in range(n))
        if all(pts[i + 1] - pts[i] >= gap for i in range(n - 1)):
            return [begin + timedelta(seconds=p) for p in pts]
    # 退化：等分 + 抖动，保证可用
    step = span / n
    return [
        begin + timedelta(seconds=step * i + random.uniform(0, max(1.0, min(step / 2, gap / 3))))
        for i in range(n)
    ]
