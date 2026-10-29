"""QQ 发送器：私聊主人、转发到群、安静时段、长文本分段。

主动发送统一走 AstrBot 的 `context.send_message(umo, MessageChain)`，
umo 形如 `aiocqhttp:FriendMessage:10001` / `aiocqhttp:GroupMessage:123456`。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time as dtime
from typing import Any

from astrbot.api import logger

try:
    from astrbot.api.message_components import File, Image, Plain
    from astrbot.core.message.message_event_result import MessageChain
except Exception as e:  # pragma: no cover
    raise

MAX_SEG = 1200  # 单段字数（QQ 单条上限很大，但太长不好读）


class QQSender:
    def __init__(self, context, cfg) -> None:
        self.context = context
        self.cfg = cfg
        self._platform_name: str | None = None

    # ------------------------------------------------------------ 平台
    def platform_name(self) -> str:
        """拿 aiocqhttp 平台的 id（这个实例里是 aiocqhttp）。"""
        if self._platform_name:
            return self._platform_name
        try:
            from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterType

            plats = list(getattr(self.context.platform_manager, "platform_insts", []) or [])
            # 优先 aiocqhttp
            for p in plats:
                try:
                    meta = p.meta()
                    if "aiocqhttp" in str(getattr(meta, "name", "")).lower() or "qq" in str(
                        getattr(meta, "name", "")
                    ).lower():
                        self._platform_name = str(meta.id)
                        return self._platform_name
                except Exception:
                    continue
            if plats:
                self._platform_name = str(plats[0].meta().id)
                return self._platform_name
            _ = PlatformAdapterType  # 保留导入，方便以后加过滤
        except Exception as e:
            logger.warning(f"[bili-whale] 探测平台 id 失败：{e}")
        # 兜底：用配置文件里见过的名字
        self._platform_name = "aiocqhttp"
        return self._platform_name

    def umo_friend(self, qq: str | int) -> str:
        return f"{self.platform_name()}:FriendMessage:{qq}"

    def umo_group(self, gid: str | int) -> str:
        return f"{self.platform_name()}:GroupMessage:{gid}"

    # ------------------------------------------------------------ 时段
    def in_quiet_hours(self) -> bool:
        qh = self.cfg.quiet_hours
        if len(qh) < 2:
            return False
        try:
            start = _parse_hm(qh[0])
            end = _parse_hm(qh[1])
        except Exception:
            return False
        now = datetime.now().time()
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end  # 跨午夜

    # ------------------------------------------------------------ 发送
    def _chain(self, text: str, images: list[str] | None = None) -> Any:
        comps: list[Any] = []
        if images:
            for url in images:
                if url and str(url).startswith("http"):
                    try:
                        comps.append(Image.fromURL(str(url)))
                    except Exception:
                        pass
        comps.append(Plain(str(text)))
        return MessageChain(chain=comps)

    async def send_umo(self, umo: str, text: str, images: list[str] | None = None) -> bool:
        if not (text or "").strip() and not images:
            return False
        if self.in_quiet_hours():
            logger.info(f"[bili-whale] 安静时段，跳过发送 -> {umo}")
            return False
        ok = True
        try:
            await self.context.send_message(umo, self._chain(text, images))
            logger.info(f"[bili-whale] 已发送 -> {umo}（{len(text)} 字）")
            return True
        except Exception as e:
            logger.warning(f"[bili-whale] 发送失败 -> {umo}: {e}")
            ok = False
        return ok

    async def send_file(self, path: str, caption: str = "") -> bool:
        """把服务器上的本地文件发给主人（QQ 文件消息段）。"""
        qq = self.cfg.owner_qq
        if not qq:
            logger.warning("[bili-whale] 没配置主人 QQ，文件发不出去")
            return False
        import os

        if not os.path.isfile(path):
            logger.warning(f"[bili-whale] 要发的文件不存在：{path}")
            return False
        comps: list[Any] = []
        if (caption or "").strip():
            comps.append(Plain(str(caption)))
        comps.append(File(name=os.path.basename(path), file=str(path)))
        try:
            await self.context.send_message(self.umo_friend(qq), MessageChain(chain=comps))
            logger.info(f"[bili-whale] 已发送文件 -> {self.umo_friend(qq)}（{os.path.basename(path)}）")
            return True
        except Exception as e:
            logger.warning(f"[bili-whale] 发送文件失败：{e}")
            return False

    async def send_friend(self, text: str, images: list[str] | None = None) -> bool:
        qq = self.cfg.owner_qq
        if not qq:
            logger.warning("[bili-whale] 没配置主人 QQ，也无法从 admins_id 兜底，发不出去")
            return False
        segs = _split(text, MAX_SEG) or [""]
        ok = True
        for i, seg in enumerate(segs):
            ok = await self.send_umo(self.umo_friend(qq), seg, images if i == 0 else None) and ok
            await asyncio.sleep(0.6)
        return ok

    async def send_group(self, gid: str | int, text: str, images: list[str] | None = None) -> bool:
        segs = _split(text, MAX_SEG) or [""]
        ok = True
        for i, seg in enumerate(segs):
            ok = await self.send_umo(self.umo_group(gid), seg, images if i == 0 else None) and ok
            await asyncio.sleep(0.6)
        return ok

    async def broadcast(self, text: str, groups: bool = True, friend: bool = True, images: list[str] | None = None) -> list[str]:
        """私聊 + 群一起发，返回成功的目标列表（umo 字符串）。"""
        done: list[str] = []
        if friend:
            if await self.send_friend(text, images):
                done.append(self.umo_friend(self.cfg.owner_qq))
        if groups and self.cfg.group_share_enable:
            for gid in self.cfg.group_ids:
                if await self.send_group(gid, text, images):
                    done.append(self.umo_group(gid))
        return done

    # ------------------------------------------------------------ 群列表
    async def group_list(self) -> list[dict]:
        """拿机器人所在的群（给 /刷B站 群列表 用）。"""
        try:
            from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterType

            plat = self.context.get_platform(PlatformAdapterType.AIOCQHTTP)
        except Exception as e:
            logger.debug(f"[bili-whale] get_platform 失败：{e}")
            return []
        if not plat:
            return []
        for attr in ("get_client", "bot"):
            try:
                obj = getattr(plat, attr, None)
                client = obj() if callable(obj) and attr == "get_client" else obj
                if client is None:
                    continue
                action = getattr(client, "call_action", None)
                if action is None:
                    continue
                data = await action("get_group_list")
                out = []
                for g in data or []:
                    out.append(
                        {
                            "group_id": str(g.get("group_id") or ""),
                            "group_name": str(g.get("group_name") or ""),
                            "member_count": int(g.get("member_count") or 0),
                        }
                    )
                return out
            except Exception as e:
                logger.debug(f"[bili-whale] 拿群列表失败({attr})：{e}")
                continue
        return []


def _split(text: str, limit: int) -> list[str]:
    """按段落切长文本，尽量在换行处断开。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 3:
            cut = limit
        out.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        out.append(rest)
    return [s for s in out if s]


def _parse_hm(s: str) -> dtime:
    hh, mm = str(s).strip().split(":")
    return dtime(int(hh), int(mm))
