"""B站客户端：把 bilibili-api-python 包一层，只暴露插件需要的那几个动作。

设计原则：
1. 所有网络动作都在这里，其它模块不直接 import bilibili_api；
2. 每个动作都 fail-soft（拿不到就返回空/默认值 + 记日志），不让一次风控把整轮流程炸掉；
3. 只读动作可以并发（asyncio.gather），写动作（评论/私信/点赞）串行做，避免触发风控。
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

try:
    from bilibili_api import Credential, comment, homepage, search, video
    from bilibili_api import session as bsession
    from bilibili_api import user as buser
    from bilibili_api.comment import CommentResourceType
    from bilibili_api.session import EventType
    from bilibili_api.utils.picture import Picture
    from bilibili_api.utils.network import Api

    BILI_OK = True
    BILI_IMPORT_ERR = ""
except Exception as _e:  # 依赖缺失时插件仍能加载，只是功能不可用
    BILI_OK = False
    BILI_IMPORT_ERR = repr(_e)
    Credential = comment = search = video = bsession = buser = homepage = None  # type: ignore
    CommentResourceType = EventType = Api = Picture = None  # type: ignore

_TAG_RE = re.compile(r"<[^>]+>")
# 评论里绝不能带链接：B站对带外链的评论判定最狠，直接折叠成「仅自己可见」
_URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s，。！？、；：\"'）】」…]+"
    r"|(?:b23\.tv|bili2233\.cn|bilibili\.com)/\S+"
    r"|(?:[a-z0-9-]+\.)+(?:com|cn|net|org|tv|cc|me|top)(?:/[^\s，。！？、；：\"'）】」…]*)?",
    re.IGNORECASE,
)


def strip_links(text: str | None) -> str:
    """把正文里的网址清掉，并收拾掉留下的空括号/多余空白。"""
    if not text:
        return ""
    out = _URL_RE.sub("", str(text))
    out = re.sub(r"[（(]\s*[）)]", "", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\s+([，。！？、；：])", r"\1", out)
    return out.strip(" \t\r\n，、；:：")


class BiliError(Exception):
    """B站接口调用失败（风控、未登录、接口变动等）。"""


def clean_text(s: str | None) -> str:
    """去掉搜索高亮标签、反转义 HTML 实体。"""
    if not s:
        return ""
    return html.unescape(_TAG_RE.sub("", str(s))).strip()


def _clean_keyword(s: Any) -> str:
    """把「猫咪（黑猫，三花）」「cs2比赛/战术」这类写法修成能搜的词。

    取括号前、斜杠前的主干，去掉多余空白；太长的截断到 20 字。
    """
    txt = str(s or "").strip()
    if not txt:
        return ""
    for sep in ("（", "(", "：", ":", "，", ",", "/", "｜", "|"):
        if sep in txt:
            txt = txt.split(sep)[0].strip()
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:20]


def fmt_duration(sec: int | str | None) -> str:
    try:
        s = int(sec or 0)
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    return f"{s // 60}:{s % 60:02d}"


def ts2str(ts: int | None) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except Exception:
        return ""


class BiliClient:
    """B站只写/只读动作封装。"""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._cred: Any = None
        self._self_info: dict = {}
        self._name_cache: dict[int, str] = {}
        self._fav_folder_id: int = 0
        self._lock = asyncio.Lock()  # 写动作串行

    # ---------------------------------------------------------------- 凭证
    def _load_credential(self) -> Any:
        if self._cred is not None:
            return self._cred
        if not BILI_OK:
            raise BiliError(f"bilibili-api 不可用：{BILI_IMPORT_ERR}")
        path = Path(self.cfg.bili_credential_file)
        if not path.exists():
            raise BiliError(f"凭证文件不存在：{path}")
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            raise BiliError(f"凭证文件解析失败：{e}") from e
        sessdata = str(d.get("sessdata") or d.get("SESSDATA") or "")
        bili_jct = str(d.get("bili_jct") or d.get("biliJct") or "")
        buvid3 = str(d.get("buvid3") or d.get("BUVID3") or "")
        if not sessdata:
            raise BiliError("凭证里没有 sessdata")
        self._cred = Credential(sessdata=sessdata, bili_jct=bili_jct, buvid3=buvid3)
        return self._cred

    @property
    def credential(self) -> Any:
        return self._load_credential()

    async def self_info(self, refresh: bool = False) -> dict:
        """登录态自检：返回 {"mid","name","level"}，未登录会抛 BiliError。"""
        if self._self_info and not refresh:
            return self._self_info
        try:
            info = await buser.get_self_info(self.credential)
        except Exception as e:
            raise BiliError(f"B站登录态失效：{e}") from e
        mid = int((info or {}).get("mid") or 0)
        if not mid:
            raise BiliError("B站登录态失效：拿不到 mid")
        self._self_info = {
            "mid": mid,
            "name": str((info or {}).get("name") or ""),
            "level": int((info or {}).get("level") or 0),
        }
        return self._self_info

    async def healthy(self) -> tuple[bool, str]:
        if not BILI_OK:
            return False, f"依赖缺失：{BILI_IMPORT_ERR}"
        try:
            me = await self.self_info()
            return True, f"{me['name']}({me['mid']})"
        except Exception as e:
            return False, str(e)

    async def uname(self, mid: int) -> str:
        """拿昵称（带缓存），失败返回空串。"""
        mid = int(mid or 0)
        if not mid:
            return ""
        if mid in self._name_cache:
            return self._name_cache[mid]
        try:
            info = await buser.User(uid=mid, credential=self.credential).get_user_info()
            name = str((info or {}).get("name") or "")
        except Exception as e:
            logger.debug(f"[bili-whale] 拿昵称失败 mid={mid}: {e}")
            name = ""
        if name:
            self._name_cache[mid] = name
        return name

    # ---------------------------------------------------------------- 选题
    async def homepage_feed(self, limit: int = 24) -> list[dict]:
        """首页推荐流（登录态个性化）——最像「自己在刷B站」。

        返回 30 条左右，带推荐理由 rcmd_reason，质量远好于按时间搜索。
        """
        if not BILI_OK:
            return []
        try:
            r = await homepage.get_videos(credential=self.credential)
        except Exception as e:
            logger.warning(f"[bili-whale] 首页推荐取不到：{e}")
            return []
        out: list[dict] = []
        for it in (r or {}).get("item") or []:
            bvid = it.get("bvid")
            if not bvid:
                continue
            stat = it.get("stat") or {}
            owner = it.get("owner") or {}
            pub = int(it.get("pubdate") or 0)
            reason = it.get("rcmd_reason")
            if isinstance(reason, dict):
                reason = reason.get("content") or ""
            out.append(
                {
                    "bvid": str(bvid),
                    "title": clean_text(it.get("title")),
                    "up": clean_text(owner.get("name")),
                    "up_mid": int(owner.get("mid") or 0),
                    "desc": clean_text(it.get("desc")),
                    "tag": "",
                    "tag_list": [],
                    "play": int(stat.get("view") or 0),
                    "danmaku": int(stat.get("danmaku") or 0),
                    "like": int(stat.get("like") or 0),
                    "duration_sec": int(it.get("duration") or 0),
                    "duration": fmt_duration(it.get("duration")),
                    "pubdate": pub,
                    "pubdate_str": ts2str(pub),
                    "keyword": "",
                    "reason": clean_text(reason),
                    "cover": str(it.get("pic") or ""),
                    "url": f"https://www.bilibili.com/video/{bvid}",
                    "from": "home",
                }
            )
            if len(out) >= limit:
                break
        logger.info(f"[bili-whale] 首页推荐 {len(out)} 条")
        return out

    async def feed(
        self,
        keywords: list[str],
        *,
        days: int = 7,
        per_kw: int = 20,
        use_homepage: bool = True,
        homepage_limit: int = 24,
    ) -> list[dict]:
        """选题：首页推荐（主源）+ 关键词搜索（补充），去重后返回。"""
        results: list[dict] = []
        if use_homepage:
            results.extend(await self.homepage_feed(homepage_limit))
        for kw in keywords:
            k = _clean_keyword(kw)
            if not k:
                continue
            try:
                r = await search.search_by_type(
                    k,
                    search_type=search.SearchObjectType.VIDEO,
                    order_type=search.OrderVideo.TOTALRANK,
                    time_range=days,
                    page=1,
                )
                for it in r.get("result") or []:
                    bvid = it.get("bvid")
                    if not bvid:
                        continue
                    results.append(
                        {
                            "bvid": bvid,
                            "title": clean_text(it.get("title")),
                            "up": clean_text(it.get("author")),
                            "up_mid": int(it.get("mid") or 0),
                            "desc": clean_text(it.get("description")),
                            "tag": clean_text(it.get("tag")),
                            "tag_list": [x for x in str(it.get("tag") or "").split(",") if x],
                            "play": int(it.get("play") or 0),
                            "danmaku": int(it.get("video_review") or 0),
                            "like": 0,
                            "duration_sec": _parse_duration(it.get("duration")),
                            "duration": str(it.get("duration") or ""),
                            "pubdate": int(it.get("pubdate") or 0),
                            "pubdate_str": ts2str(it.get("pubdate")),
                            "keyword": kw,
                            "reason": "",
                            "cover": "",
                            "url": f"https://www.bilibili.com/video/{bvid}",
                            "from": "search",
                        }
                    )
            except Exception as e:
                logger.warning(f"[bili-whale] 搜索失败 kw={kw}: {e}")
        # 去重（保留先出现的：首页推荐优先）
        seen: set[str] = set()
        uniq: list[dict] = []
        for v in results:
            if v["bvid"] in seen:
                continue
            seen.add(v["bvid"])
            uniq.append(v)
        return uniq

    async def video_info(self, bvid: str) -> dict:
        """视频详情。"""
        try:
            info = await video.Video(bvid=bvid, credential=self.credential).get_info()
        except Exception as e:
            raise BiliError(f"拿视频详情失败 {bvid}: {e}") from e
        stat = (info or {}).get("stat") or {}
        owner = (info or {}).get("owner") or {}
        return {
            "bvid": bvid,
            "aid": int((info or {}).get("aid") or 0),
            "cid": int((info or {}).get("cid") or 0),
            "title": clean_text((info or {}).get("title")),
            "desc": clean_text((info or {}).get("desc")),
            "up": clean_text(owner.get("name")),
            "up_mid": int(owner.get("mid") or 0),
            "view": int(stat.get("view") or 0),
            "danmaku": int(stat.get("danmaku") or 0),
            "reply": int(stat.get("reply") or 0),
            "like": int(stat.get("like") or 0),
            "coin": int(stat.get("coin") or 0),
            "favorite": int(stat.get("favorite") or 0),
            "share": int(stat.get("share") or 0),
            "duration_sec": int((info or {}).get("duration") or 0),
            "pubdate": int((info or {}).get("pubdate") or 0),
            "pubdate_str": ts2str((info or {}).get("pubdate")),
            "pages": len((info or {}).get("pages") or []),
            "cover": str((info or {}).get("pic") or ""),
            "tags": [],
            "url": f"https://www.bilibili.com/video/{bvid}",
        }

    async def video_tags(self, bvid: str) -> list[str]:
        try:
            tags = await video.Video(bvid=bvid, credential=self.credential).get_tags()
            return [clean_text(t.get("tag_name")) for t in (tags or []) if t.get("tag_name")]
        except Exception as e:
            logger.debug(f"[bili-whale] 拿标签失败 {bvid}: {e}")
            return []

    async def subtitle(self, bvid: str, cid: int | None = None, max_chars: int = 6000) -> str:
        """取 AI 字幕并拼成纯文本；没有字幕返回空串。"""
        try:
            v = video.Video(bvid=bvid, credential=self.credential)
            if not cid:
                info = await v.get_info()
                cid = int((info or {}).get("cid") or 0)
            data = await v.get_subtitle(cid=cid)
            subs = (data or {}).get("subtitles") or []
            if not subs:
                return ""
            url = str(subs[0].get("subtitle_url") or subs[0].get("url") or "")
            if not url:
                return ""
            if url.startswith("//"):
                url = "https:" + url
            raw = await _http_get_json(url)
            body = raw.get("body") or []
            text = "".join(str(x.get("content") or "") for x in body)
            text = re.sub(r"\s+", "", text)
            if len(text) > max_chars:
                text = text[:max_chars] + "……"
            return text
        except Exception as e:
            logger.debug(f"[bili-whale] 取字幕失败 {bvid}: {e}")
            return ""

    async def danmakus(self, bvid: str, *, pages: int = 2, limit: int = 80) -> list[str]:
        """取弹幕文本（按时间顺序铺开），用来看观众的真实反应。"""
        out: list[str] = []
        try:
            v = video.Video(bvid=bvid, credential=self.credential)
            for p in range(max(1, pages)):
                ds = await v.get_danmakus(page_index=p)
                for d in ds or []:
                    t = clean_text(getattr(d, "text", "") or "")
                    if t:
                        out.append(t)
                if len(out) >= limit:
                    break
        except Exception as e:
            logger.debug(f"[bili-whale] 取弹幕失败 {bvid}: {e}")
        return out[:limit]

    async def comments(self, bvid: str, aid: int | None = None, *, limit: int = 20) -> list[dict]:
        """取热门评论（按热度），返回 [{uname, mid, text, like, rpid, ctime}]。"""
        try:
            if not aid:
                info = await video.Video(bvid=bvid, credential=self.credential).get_info()
                aid = int((info or {}).get("aid") or 0)
            if not aid:
                return []
            data = await comment.get_comments(
                oid=aid,
                type_=CommentResourceType.VIDEO,
                page_index=1,
                credential=self.credential,
            )
            reps = (data or {}).get("replies") or []
            out = []
            for c in reps[:limit]:
                out.append(
                    {
                        "rpid": int(c.get("rpid") or 0),
                        "mid": int(((c.get("member") or {}).get("mid")) or 0),
                        "uname": clean_text(((c.get("member") or {}).get("uname"))),
                        "text": clean_text(((c.get("content") or {}).get("message"))),
                        "like": int(c.get("like") or 0),
                        "rcount": int(c.get("rcount") or 0),
                        "ctime": int(c.get("ctime") or 0),
                    }
                )
            return out
        except Exception as e:
            logger.warning(f"[bili-whale] 取评论失败 {bvid}: {e}")
            return []

    async def deep_read(self, bvid: str, *, max_subtitle_chars: int = 6000) -> dict:
        """一次把详情/字幕/弹幕/评论抓齐（并发），供 debrief 用。"""
        info = await self.video_info(bvid)
        cid = info.get("cid") or 0
        sub, dms, cms, tags = await asyncio.gather(
            self.subtitle(bvid, cid=cid, max_chars=max_subtitle_chars),
            self.danmakus(bvid),
            self.comments(bvid, aid=info.get("aid")),
            self.video_tags(bvid),
            return_exceptions=True,
        )
        info["subtitle"] = sub if isinstance(sub, str) else ""
        info["danmakus"] = dms if isinstance(dms, list) else []
        info["comments"] = cms if isinstance(cms, list) else []
        info["tags"] = tags if isinstance(tags, list) else []
        return info

    # ---------------------------------------------------------------- 写动作
    async def like(self, bvid: str) -> bool:
        async with self._lock:
            try:
                await video.Video(bvid=bvid, credential=self.credential).like(True)
                logger.info(f"[bili-whale] 已点赞 {bvid}")
                return True
            except Exception as e:
                logger.warning(f"[bili-whale] 点赞失败 {bvid}: {e}")
                return False

    async def follow(self, mid: int) -> bool:
        if not mid:
            return False
        async with self._lock:
            try:
                u = buser.User(uid=int(mid), credential=self.credential)
                await u.modify_relation(buser.RelationType.SUBSCRIBE)
                logger.info(f"[bili-whale] 已关注 {mid}")
                return True
            except Exception as e:
                logger.warning(f"[bili-whale] 关注失败 {mid}: {e}")
                return False

    async def favorite(self, bvid: str, *, aid: int | None = None, folder_id: int = 0) -> bool:
        """收藏到默认收藏夹（folder_id 为 0 时先查一次账号的默认收藏夹）。

        走 x/v3/fav/resource/deal（这个接口从服务器 IP 直接调是通的，
        而 Video.set_favorite 在这版库里会抛 KeyError('data')）。
        """
        async with self._lock:
            try:
                fid = int(folder_id or 0)
                if not fid:
                    fid = await self.default_fav_folder()
                if not fid:
                    logger.warning(f"[bili-whale] 没有可用的收藏夹，跳过收藏 {bvid}")
                    return False
                if not aid:
                    info = await video.Video(bvid=bvid, credential=self.credential).get_info()
                    aid = int((info or {}).get("aid") or 0)
                if not aid:
                    logger.warning(f"[bili-whale] 拿不到 aid，跳过收藏 {bvid}")
                    return False
                api = Api(
                    "https://api.bilibili.com/x/v3/fav/resource/deal",
                    "POST",
                    verify=True,
                    credential=self.credential,
                )
                api.update_data(rid=int(aid), type=2, add_media_ids=str(fid), del_media_ids="")
                r = await api.request()
                ok_num = int((r or {}).get("success_num") or 0) if isinstance(r, dict) else 0
                if ok_num <= 0:
                    # success_num=0 可能只是「早就收藏过了」，用 favoured 复核一次
                    if await self.is_favoured(aid):
                        logger.info(f"[bili-whale] {bvid} 早就在收藏夹里了")
                        return True
                    logger.warning(
                        f"[bili-whale] 收藏没成功 {bvid}：{json.dumps(r, ensure_ascii=False)[:160] if isinstance(r, dict) else r}"
                    )
                    return False
                logger.info(f"[bili-whale] 已收藏 {bvid} -> 收藏夹 {fid}")
                return True
            except Exception as e:
                logger.warning(f"[bili-whale] 收藏出错 {bvid}: {e}")
                return False

    async def is_favoured(self, aid: int) -> bool:
        """这个视频是否已经在收藏夹里。"""
        try:
            api = Api(
                "https://api.bilibili.com/x/v2/fav/video/favoured",
                "GET",
                credential=self.credential,
            )
            api.update_params(aid=int(aid))
            r = await api.request() or {}
            if isinstance(r, dict):
                return bool(r.get("favoured"))
        except Exception as e:
            logger.debug(f"[bili-whale] 查收藏状态失败 {aid}: {e}")
        return False

    async def default_fav_folder(self) -> int:
        """取账号的默认收藏夹 id（缓存）。"""
        if self._fav_folder_id:
            return self._fav_folder_id
        try:
            if not BILI_OK:
                return 0
            mid = int((await self.self_info()).get("mid") or 0)
            api = Api(
                "https://api.bilibili.com/x/v3/fav/folder/created/list-all",
                "GET",
                credential=self.credential,
            )
            api.update_params(up_mid=mid)
            r = await api.request() or {}
            folders = r.get("list") if isinstance(r, dict) else None
            if folders is None and isinstance(r, dict):
                folders = (r.get("data") or {}).get("list")
            folders = folders or []
            if folders:
                self._fav_folder_id = int(folders[0].get("id") or 0)
        except Exception as e:
            logger.debug(f"[bili-whale] 取收藏夹失败: {e}")
        return self._fav_folder_id

    async def coin(self, bvid: str, count: int = 1) -> dict:
        """给视频投币。

        返回 {"ok":bool,"code":int,"message":str,"blocked":bool}。
        注意：从机房 IP 投币 B站 会返回 -401「非法访问」（要求人机验证），
        blocked=True 表示今天别试了。
        """
        async with self._lock:
            try:
                api = Api(
                    "https://api.bilibili.com/x/web-interface/coin/add",
                    "POST",
                    verify=True,
                    credential=self.credential,
                )
                api.update_data(
                    bvid=bvid, multiply=max(1, min(2, int(count))), select_like=0
                )
                r = await api.request()
                logger.info(f"[bili-whale] 已投币 {bvid}×{count}")
                return {"ok": True, "code": 0, "message": "投币成功", "data": r}
            except Exception as e:
                txt = str(e)
                code = -1
                m = re.search(r"错误代码[：:]\s*(-?\d+)", txt) or re.search(
                    r"code[=:\s]+(-?\d+)", txt
                )
                if m:
                    try:
                        code = int(m.group(1))
                    except Exception:
                        pass
                blocked = code in (-401, -352, -412)
                logger.warning(
                    f"[bili-whale] 投币失败 {bvid}：code={code}"
                    + ("（风控要求人机验证，今天不再试）" if blocked else "")
                    + f" {txt[:120]}"
                )
                return {"ok": False, "code": code, "message": txt[:160], "blocked": blocked}

    async def send_comment(self, bvid: str, text: str, aid: int | None = None) -> dict:
        """在视频下发一条评论（@主人 靠正文里的 @昵称 文本实现）。"""
        text = strip_links(text).strip()
        if not text:
            raise BiliError("评论内容为空")
        if len(text) > 900:
            text = text[:900]
        async with self._lock:
            try:
                if not aid:
                    info = await video.Video(bvid=bvid, credential=self.credential).get_info()
                    aid = int((info or {}).get("aid") or 0)
                r = await comment.send_comment(
                    text=text,
                    oid=int(aid),
                    type_=CommentResourceType.VIDEO,
                    credential=self.credential,
                )
                ok = bool(((r or {}).get("success_action") is not False))
                if not ok:
                    raise BiliError(f"评论接口返回异常：{json.dumps(r, ensure_ascii=False)[:200]}")
                logger.info(f"[bili-whale] 已评论 {bvid}: {text[:40]}")
                return {"ok": True, "rpid": ((r or {}).get("rpid") or 0), "text": text}
            except BiliError:
                raise
            except Exception as e:
                raise BiliError(f"发评论失败 {bvid}: {e}") from e

    async def send_dm(self, receiver_mid: int, text: str) -> dict:
        """给指定 UID 发 B站私信（需要两个号互关）。"""
        text = (text or "").strip()
        if not text:
            raise BiliError("私信内容为空")
        if not receiver_mid:
            raise BiliError("私信接收者 UID 为空")
        async with self._lock:
            try:
                r = await bsession.send_msg(
                    credential=self.credential,
                    receiver_id=int(receiver_mid),
                    msg_type=EventType.TEXT,
                    content=text[:900],
                )
                logger.info(f"[bili-whale] 已私信 {receiver_mid}: {text[:40]}")
                return {"ok": True, "raw": r}
            except Exception as e:
                raise BiliError(f"发私信失败 {receiver_mid}: {e}") from e

    async def send_picture(self, receiver_mid: int, image: bytes) -> dict:
        """给指定 UID 发一张图片私信（JPEG/PNG 字节）。"""
        if not image:
            raise BiliError("图片内容为空")
        if Picture is None or EventType is None:  # pragma: no cover
            raise BiliError("bilibili_api 不可用，发不了图片")
        pic = Picture().from_content(bytes(image), "jpeg")
        async with self._lock:
            try:
                r = await bsession.send_msg(
                    credential=self.credential,
                    receiver_id=int(receiver_mid),
                    msg_type=EventType.PICTURE,
                    content=pic,
                )
                logger.info(f"[bili-whale] 已给 {receiver_mid} 发图片私信")
                return {"ok": True, "raw": r}
            except Exception as e:
                raise BiliError(f"发图片私信失败 {receiver_mid}: {e}") from e

    async def share_video(
        self,
        receiver_mid: int,
        *,
        aid: int,
        title: str,
        author: str = "",
        cover: str = "",
        bvid: str = "",
    ) -> dict:
        """像「分享视频」那样把视频发到私信里。

        B站 的真实分享卡片是 msg_type=7，但服务端对第三方调 send_msg 发 7 号消息
        一律回 -400 请求错误（2026-10-29 实测：同样的请求发 text/图片都通，只有 7 不通），
        所以务实做法：先发封面图（视觉上就是一张大卡片），再补一条带标题/UP主/链接的文字。
        """
        if not receiver_mid:
            raise BiliError("私信接收者 UID 为空")
        out: dict = {"ok": False, "card": False, "picture": False}
        # ① 封面图（相当于卡片的上半部分）
        if cover:
            try:
                img = await self._cover_jpeg(cover)
                if img:
                    await self.send_picture(receiver_mid, img)
                    out["picture"] = True
            except BiliError as e:
                logger.warning(f"[bili-whale] 封面图没发成：{e}")
        out["ok"] = out["picture"]
        return out

    async def _cover_jpeg(self, cover_url: str, *, max_side: int = 1280) -> bytes:
        """把封面 URL 抓下来转成 B站 能接受的 JPEG。"""
        url = cover_url.replace("http://", "https://", 1)
        try:
            import httpx

            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                r = await client.get(
                    url, headers={"Referer": "https://www.bilibili.com/", "User-Agent": "Mozilla/5.0"}
                )
                raw = r.content
            if not raw:
                return b""
        except Exception as e:
            logger.warning(f"[bili-whale] 下载封面失败：{e}")
            return b""
        try:
            import io as _io

            from PIL import Image

            im = Image.open(_io.BytesIO(raw)).convert("RGB")
            im.thumbnail((max_side, max_side))
            buf = _io.BytesIO()
            im.save(buf, "JPEG", quality=88)
            return buf.getvalue()
        except Exception as e:
            logger.warning(f"[bili-whale] 封面转 JPEG 失败：{e}")
            return raw

    async def reply_comment(self, bvid: str, rpid: int, text: str, aid: int | None = None) -> dict:
        """在别人的评论下回复一条。"""
        text = (text or "").strip()
        if not text:
            raise BiliError("回复内容为空")
        async with self._lock:
            try:
                if not aid:
                    info = await video.Video(bvid=bvid, credential=self.credential).get_info()
                    aid = int((info or {}).get("aid") or 0)
                r = await comment.send_comment(
                    text=text[:500],
                    oid=int(aid),
                    type_=CommentResourceType.VIDEO,
                    credential=self.credential,
                    root=int(rpid),
                    parent=int(rpid),
                )
                return {"ok": True, "raw": r}
            except Exception as e:
                raise BiliError(f"回复评论失败 {bvid}/{rpid}: {e}") from e

    async def reply_to_comment(
        self,
        *,
        bvid: str,
        aid: int | None = None,
        root: int,
        parent: int | None = None,
        text: str,
    ) -> dict:
        """在某条评论下回复。root=楼层主评论 rpid，parent=要接的那条 rpid（缺省同 root）。"""
        text = (text or "").strip()
        if not text:
            raise BiliError("回复内容为空")
        if not bvid and not aid:
            raise BiliError("缺少 bvid/aid，没法定位视频")
        async with self._lock:
            try:
                oid = int(aid or 0)
                if not oid:
                    info = await video.Video(bvid=bvid, credential=self.credential).get_info()
                    oid = int((info or {}).get("aid") or 0)
                r = await comment.send_comment(
                    text=text[:500],
                    oid=oid,
                    type_=CommentResourceType.VIDEO,
                    credential=self.credential,
                    root=int(root),
                    parent=int(parent or root),
                )
                return {"ok": True, "raw": r}
            except Exception as e:
                raise BiliError(f"回复评论失败 {bvid}/{root}: {e}") from e

    # ---------------------------------------------------------------- 互动收件箱
    async def ats(self, *, last_id: int | None = None) -> list[dict]:
        """B站 @我的 消息。返回 [{uid, uname, text, uri, ts, bvid, aid, root, parent}]。"""
        try:
            kw: dict[str, Any] = {}
            if last_id:
                kw["last_id"] = int(last_id)
            data = await bsession.get_at(credential=self.credential, **kw)
        except Exception as e:
            logger.debug(f"[bili-whale] 取@消息失败: {e}")
            return []
        items = (data or {}).get("items") or []
        return [_parse_inbox_item(it, kind="at") for it in items]

    async def replies(self, *, last_reply_id: int | None = None) -> list[dict]:
        """B站「回复我的」消息（别人在我的评论下发的东西）。"""
        try:
            kw: dict[str, Any] = {}
            if last_reply_id:
                kw["last_reply_id"] = int(last_reply_id)
            data = await bsession.get_replies(credential=self.credential, **kw)
        except Exception as e:
            logger.debug(f"[bili-whale] 取回复消息失败: {e}")
            return []
        items = (data or {}).get("items") or []
        return [_parse_inbox_item(it, kind="reply") for it in items]

    async def dms(self, *, talker_id: int | None = None, limit: int = 10) -> list[dict]:
        """最近的私信会话内容（默认只看和主人的会话）。"""
        out: list[dict] = []
        try:
            if talker_id:
                talkers = [int(talker_id)]
            else:
                ses = await bsession.get_sessions(credential=self.credential)
                talkers = [
                    int(s.get("talker_id") or 0)
                    for s in (ses or {}).get("session_list") or []
                    if int(s.get("unread_count") or 0) > 0
                ][:5]
            # 自报 mid：优先用户信息缓存，退到配置里的自己的 mid
            # （新实例还没调 self_info() 时缓存是空的，会把自家发的私信当成别人发的）
            my_mid = int(self._self_info.get("mid") or 0) or int(
                getattr(self.cfg, "self_bili_mid", 0) or 0
            )
            for t in talkers:
                if not t:
                    continue
                data = await bsession.fetch_session_msgs(
                    talker_id=t, credential=self.credential, session_type=1
                )
                # B站返回的 messages 是「新→旧」倒序，直接取尾部会拿到最老的那几条，
                # 导致新私信永远轮不到（表现为「私信不回」）。这里按时间正序再取最新 limit 条。
                raw = list((data or {}).get("messages") or [])
                raw.sort(key=lambda m: int((m or {}).get("timestamp") or 0))
                for m in raw[-limit:]:
                    sender = int(m.get("sender_uid") or 0)
                    text = _dm_text(m.get("content"))
                    video = _dm_video(m.get("content"))
                    if video and not video.get("bvid"):
                        # 分享卡片里只有 aid，主人要的是「她真的去看了」，所以得换成 bvid
                        try:
                            video["bvid"] = await self.bvid_of_aid(int(video.get("aid") or 0))
                        except Exception:
                            pass
                    if not text and not video:
                        continue
                    if not text:
                        text = f"【视频】{video.get('title') or ''}".strip()
                    out.append(
                        {
                            "talker_id": t,
                            "sender_uid": sender,
                            "from_me": sender == my_mid,
                            "text": text,
                            "video": video or None,
                            "ts": int(m.get("timestamp") or 0),
                            "ts_str": ts2str(m.get("timestamp")),
                        }
                    )
        except Exception as e:
            logger.debug(f"[bili-whale] 取私信失败: {e}")
        return out

    async def bvid_of_aid(self, aid: int) -> str:
        """aid → bvid（私信分享卡片里只有 aid）。"""
        if not aid:
            return ""
        try:
            info = await video.Video(aid=int(aid), credential=self.credential).get_info()
            return str((info or {}).get("bvid") or "")
        except Exception:
            return ""

    async def unread(self) -> dict:
        try:
            d = await bsession.get_unread_messages(credential=self.credential)
            return dict(d or {})
        except Exception as e:
            logger.debug(f"[bili-whale] 取未读计数失败: {e}")
            return {}

    async def check_owner_relation(self) -> dict:
        """检查我和主人的关系（私信要求互关）。"""
        try:
            rel = await buser.User(
                uid=int(self.cfg.owner_bili_mid), credential=self.credential
            ).get_relation()
            return {
                "attribute": int((rel or {}).get("attribute") or 0),
                "is_followed": int((rel or {}).get("attribute") or 0) == 6,
            }
        except Exception as e:
            return {"attribute": -1, "error": str(e)}


# -------------------------------------------------------------------- 工具函数
def _parse_duration(v: Any) -> int:
    s = str(v or "").strip()
    if not s:
        return 0
    if ":" in s:
        parts = s.split(":")
        try:
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
        except (TypeError, ValueError):
            return 0
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return 0


async def _http_get_json(url: str) -> dict:
    """下载字幕 json（bilibili_api 的请求器不保证带对 headers，这里直连）。"""
    import aiohttp

    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
            return await r.json(content_type=None)


def _dm_video(content: Any) -> dict:
    """私信里的「视频分享卡片」→ {aid,title,author,cover}；不是卡片就返回 {}。"""
    if isinstance(content, dict):
        c = content
    else:
        try:
            c = json.loads(content or "{}")
        except Exception:
            return {}
    if not isinstance(c, dict):
        return {}
    aid = c.get("id") or c.get("avid") or 0
    try:
        aid = int(aid)
    except Exception:
        aid = 0
    if not aid or int(c.get("source") or 0) not in (0, 5):
        return {}
    return {
        "aid": aid,
        "title": clean_text(c.get("title") or ""),
        "author": clean_text(c.get("author") or ""),
        "cover": str(c.get("thumb") or ""),
    }


def _dm_text(content: Any) -> str:
    """私信 content 是个嵌套 json 字符串，尽量把正文抠出来。"""
    if isinstance(content, dict):
        c = content
    else:
        try:
            c = json.loads(content or "{}")
        except Exception:
            return clean_text(str(content or ""))
    for k in ("content", "msg", "message", "text"):
        v = c.get(k) if isinstance(c, dict) else None
        if isinstance(v, str) and v.strip():
            # 有的 content 里还是 json
            try:
                inner = json.loads(v)
                if isinstance(inner, dict):
                    for k2 in ("content", "msg", "message", "text"):
                        v2 = inner.get(k2)
                        if isinstance(v2, str) and v2.strip():
                            return clean_text(v2)
                    if inner.get("jump_url"):
                        return clean_text(inner.get("title") or "") or "[卡片消息]"
            except Exception:
                pass
            return clean_text(v)
    return ""


def _parse_inbox_item(it: dict, kind: str) -> dict:
    """@我的 / 回复我的 单条消息解析（接口结构会变，容错优先）。

    除了给人看的内容，还要抠出「能回复的坐标」：
    bvid（从 uri 里捞 BV 号）、aid（subject_id）、root/parent（native_uri 里的 comment_root_id / comment_secondary_id）。
    """
    user = it.get("user") or {}
    item = it.get("item") or {}
    source = it.get("source") or ""
    content: dict = {}
    for key in ("content", "reply", "at"):
        if isinstance(it.get(key), dict):
            content = it[key]
            break
    text = clean_text(
        content.get("text")
        or content.get("title")
        or content.get("message")
        or content.get("content")
        or item.get("source_content")
        or ""
    )
    uri = content.get("uri") or content.get("jump_url") or item.get("uri") or ""
    title = clean_text(item.get("title") or content.get("title") or "")
    native = str(item.get("native_uri") or "")

    # 视频号：优先从 uri 里捞，其次从 native_uri 的 /video/<aid> 反查不到 bvid，只能靠 uri
    bvid = ""
    for cand in (uri, native, str(item.get("business_url") or "")):
        m = _BV_RE.search(cand or "")
        if m:
            bvid = m.group(1)
            break

    aid = _to_int(item.get("subject_id") or item.get("business_id") or content.get("subject_id"))
    # 评论坐标：native_uri 里最准；接口字段是兜底
    root = _to_int(_param(native, "comment_root_id"))
    parent = _to_int(_param(native, "comment_secondary_id"))
    if not root:
        root = _to_int(item.get("root_id") or content.get("root_id") or item.get("target_id"))
    if not parent:
        parent = _to_int(item.get("target_id") or content.get("target_id") or item.get("source_id"))
    if root and parent and parent == root:
        parent = 0  # 同一个就不要重复传
    if not root:
        root = parent

    return {
        "kind": kind,
        "uid": int(it.get("uid") or user.get("mid") or user.get("uid") or 0),
        "uname": clean_text(user.get("nickname") or user.get("uname") or it.get("uname") or ""),
        "text": text,
        "title": title,
        "uri": uri,
        "source": source,
        "ts": int(it.get("at_time") or it.get("reply_time") or it.get("timestamp") or content.get("timestamp") or 0),
        "ts_str": ts2str(it.get("at_time") or it.get("reply_time") or it.get("timestamp")),
        "bvid": bvid,
        "aid": aid,
        "root": root,
        "parent": parent,
        "raw": it,
    }


_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")


def _param(url: str, key: str) -> str:
    m = re.search(rf"[?&]{key}=(\d+)", url or "")
    return m.group(1) if m else ""


def _to_int(v: Any) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0

