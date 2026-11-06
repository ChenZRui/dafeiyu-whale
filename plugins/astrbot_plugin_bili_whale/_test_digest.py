# -*- coding: utf-8 -*-
"""digest（每晚汇总）模式自测：
- config.qq_push_mode 解析
- store.add_digest / digest_today / clear_digest / 跨天重置 / 同 bvid 去重
- _report_qq 在 digest 模式下不私聊、只攒
- _send_digest 文案含标题/分数/链接，发完清空
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(r"D:\deepseek work common\astrbot_plugin_bili_whale")
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_bili_whale.core.config import PluginConfig  # noqa: E402
from astrbot_plugin_bili_whale.core.store import StateStore  # noqa: E402

ok, bad = 0, 0


def check(name, cond, extra=""):
    global ok, bad
    if cond:
        ok += 1
    else:
        bad += 1
        print("FAIL:", name, extra)


# ---------------- config
c = PluginConfig({"qq_push_mode": "digest"})
check("mode=digest", c.qq_push_mode == "digest", c.qq_push_mode)
c2 = PluginConfig({"qq_push_mode": "DIGEST"})
check("大小写不敏感", c2.qq_push_mode == "digest", c2.qq_push_mode)
c3 = PluginConfig({"qq_push_mode": "???"})
check("非法值回落 each", c3.qq_push_mode == "each", c3.qq_push_mode)
c4 = PluginConfig({})
check("默认 each", c4.qq_push_mode == "each", c4.qq_push_mode)

# ---------------- store
tmp = tempfile.mkdtemp()
st = StateStore(Path(tmp))
check("默认 digest 存在", isinstance(st.data.get("digest"), dict))
check("默认空", st.digest_today() == [])

n1 = st.add_digest({"bvid": "BV1aa", "title": "标题A", "url": "u1", "score": 18, "note": "她说的"})
check("攒第 1 条", n1 == 1, n1)
n2 = st.add_digest({"bvid": "BV1bb", "title": "标题B", "url": "u2", "score": 17})
check("攒第 2 条", n2 == 2, n2)
n3 = st.add_digest({"bvid": "BV1aa", "title": "标题A改", "url": "u1", "score": 19})
check("同 bvid 去重后仍 2 条", n3 == 2 and len(st.digest_today()) == 2, (n3, len(st.digest_today())))
check("同 bvid 覆盖为新值", [i for i in st.digest_today() if i["bvid"] == "BV1aa"][0]["score"] == 19)

st2 = StateStore(Path(tmp))
check("落盘可读", len(st2.digest_today()) == 2, len(st2.digest_today()))

# 跨天重置
st2.data["digest"]["date"] = "2000-01-01"
st2.save()
st3 = StateStore(Path(tmp))
check("跨天自动清空", st3.digest_today() == [], st3.digest_today())

st3.add_digest({"bvid": "BV1cc", "title": "C", "url": "u3"})
st3.clear_digest()
check("clear 后为空", st3.digest_today() == [])

# ---------------- pipeline: digest 分支
from astrbot_plugin_bili_whale.core import pipeline as pl  # noqa: E402


class FakeSender:
    def __init__(self):
        self.friends = []

    async def send_friend(self, text, images=None):
        self.friends.append(text)
        return True

    async def send_group(self, gid, text, images=None):
        return True

    def umo_friend(self, qq):
        return f"umo:{qq}"


class FakeLLM:
    async def qq_share_text(self, *a, **k):
        raise AssertionError("digest 模式不该生成 QQ 分享文案")


def make_pipe(mode):
    p = object.__new__(pl.WhalePipeline)
    p.cfg = types.SimpleNamespace(
        qq_share_enable=True,
        qq_push_mode=mode,
        owner_qq=2675150339,
        group_share_enable=False,
        group_ids=[],
        diary_push=False,
        daily_push_enable=False,
        data_dir=Path(tmp),
    )
    p.store = StateStore(Path(tmp))
    p.sender = FakeSender()
    p.llm = FakeLLM()
    p._warned = set()

    def _warn_once(k):
        if k in p._warned:
            return False
        p._warned.add(k)
        return True

    p._warn_once = _warn_once
    return p


async def main():
    picked = {"bvid": "BV1dd", "title": "干货视频", "url": "https://b/BV1dd", "score": 18}
    p = make_pipe("digest")
    done = await p._report_qq(picked, picked, {"note": "短评"}, {"comment": {"ok": True, "text": "主人，这条值得看"}}, None)
    check("digest 不发私聊", done == [], done)
    items = p.store.digest_today()
    check("攒进 digest", len(items) == 1, items)
    check("标题正确", items and items[0]["title"] == "干货视频", items)
    check("note 复用评论文案", items and items[0]["note"] == "主人，这条值得看", items)

    n = await p._send_digest()
    check("发送条数", n == 1, n)
    check("真的发了 1 条", len(p.sender.friends) == 1, p.sender.friends)
    txt = p.sender.friends[0] if p.sender.friends else ""
    check("含条数", "挑了 1 条" in txt, txt)
    check("含标题", "干货视频" in txt, txt)
    check("含分数", "18 分" in txt, txt)
    check("含链接", "https://b/BV1dd" in txt, txt)
    check("含她说的话", "主人，这条值得看" in txt, txt)
    check("发完清空", p.store.digest_today() == [])

    # 空 digest 不发
    before = len(p.sender.friends)
    n0 = await p._send_digest()
    check("空不发", n0 == 0 and len(p.sender.friends) == before, (n0, len(p.sender.friends)))

    # each 模式回归：仍然走原来的私聊
    p2 = make_pipe("each")
    p2.llm = types.SimpleNamespace(qq_share_text=lambda *a, **k: asyncio.sleep(0, result="正常文案"))

    async def _qq(*a, **k):
        return "正常文案"

    p2.llm.qq_share_text = _qq
    done2 = await p2._report_qq(picked, picked, {"note": "n"}, {"comment": {"ok": True}}, None)
    check("each 模式仍私聊", len(done2) == 1, done2)
    check("each 模式不攒", p2.store.digest_today() == [])

    print(f"\n{ok} 通过 / {bad} 失败")
    return bad == 0


raise SystemExit(0 if asyncio.run(main()) else 1)
