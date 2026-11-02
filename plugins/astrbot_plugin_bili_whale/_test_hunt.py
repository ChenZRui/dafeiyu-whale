# -*- coding: utf-8 -*-
"""验证两件事：
1. 评论/私信文案末尾会按概率补颜文字（而且不会补第二个）；
2. 她能在评论区主动找「别人家的鱼」搭话（额度、去重、排除主人和自己、概率）。
"""
import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(r"D:\deepseek work common\astrbot_plugin_bili_whale")
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_bili_whale.core import pipeline as P  # noqa: E402
from astrbot_plugin_bili_whale.core import llm as L  # noqa: E402
from astrbot_plugin_bili_whale.core.config import DEFAULTS, PluginConfig  # noqa: E402

ok, bad = 0, []


def check(name, cond, extra=""):
    global ok
    if cond:
        ok += 1
    else:
        bad.append(f"{name} {extra}")
        print("FAIL", name, extra)


def mk_cfg(**over):
    raw = dict(DEFAULTS)
    raw.update(over)
    return PluginConfig(raw)


# ---------------------------------------------------------------- 1. 配置默认值
cfg = mk_cfg()
check("default peer_hunt_enable", cfg.peer_hunt_enable is True)
check("default peer_hunt_probability", abs(cfg.peer_hunt_probability - 0.5) < 1e-9)
check("default peer_hunt_max", cfg.peer_hunt_max == 1)
check("default peer_hunt_scan", cfg.peer_hunt_scan == 20)
check("default peer_hunt_min_like", cfg.peer_hunt_min_like == 0)
check("default kaomoji_enable", cfg.kaomoji_enable is True)
check("default kaomoji_probability", abs(cfg.kaomoji_probability - 0.7) < 1e-9)
check("clamp max", mk_cfg(peer_hunt_max=9).peer_hunt_max == 3)
check("clamp scan", mk_cfg(peer_hunt_scan=1).peer_hunt_scan == 5)
check("clamp probability", mk_cfg(peer_hunt_probability=3).peer_hunt_probability == 1.0)
check("clamp kao prob", mk_cfg(kaomoji_probability=-1).kaomoji_probability == 0.0)

# ---------------------------------------------------------------- 2. 颜文字
def kao(text, **over):
    fake = types.SimpleNamespace(cfg=mk_cfg(**over))
    return L.LLMService._maybe_kaomoji(fake, text)


check("kao off 不变", kao("主人，发现个东西。", kaomoji_enable=False) == "主人，发现个东西。")
check("kao p=0 不变", kao("主人，发现个东西。", kaomoji_probability=0) == "主人，发现个东西。")
for _ in range(5):
    got = kao("主人，发现个东西。", kaomoji_probability=1)
    check("kao p=1 有颜文字", got.startswith("主人，发现个东西。") and len(got) > 10, got)
    check("kao p=1 只补一次", L.LLMService._maybe_kaomoji(
        types.SimpleNamespace(cfg=mk_cfg(kaomoji_probability=1)), got) == got, got)
check("kao 已有括号不再加", kao("主人，(๑•̀ㅂ•́)و✧", kaomoji_probability=1) == "主人，(๑•̀ㅂ•́)و✧")
check("kao 空文本", kao("", kaomoji_probability=1) == "")

# ---------------------------------------------------------------- 3. _peer_hunt
class FakeComments:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    async def __call__(self, bvid, aid=None, limit=20):
        self.calls += 1
        return self.rows


class FakeBili:
    def __init__(self, rows):
        self.comments = FakeComments(rows)
        self.replied = []

    async def reply_to_comment(self, *, bvid, aid=None, root, parent=None, text=""):
        self.replied.append({"bvid": bvid, "root": root, "text": text})
        return {"rpid": 9}


class FakeLLM:
    def __init__(self):
        self.seen = []

    async def peer_chat_text(self, item, *, turns=0, my_uname=""):
        self.seen.append(item)
        return "哟，你也是被养着的那只？"


class FakeStore:
    def __init__(self, handled=(), left=6):
        self.handled = set(handled)
        self.left = left
        self.used = []
        self.bumped = []

    def peer_left(self, limit):
        return max(0, min(limit, self.left))

    def peer_used(self, uid=0):
        self.used.append(uid)
        self.left = max(0, self.left - 1)

    def peer_bump(self, key, uid=0):
        self.bumped.append(key)
        return 1

    def is_handled(self, kind, key):
        return f"{kind}:{key}" in self.handled

    def mark_handled(self, kind, key):
        self.handled.add(f"{kind}:{key}")


class FakeSender:
    def __init__(self):
        self.msgs = []

    async def send_friend(self, text):
        self.msgs.append(text)
        return True


def mk_pipe(rows, *, cfg=None, store=None):
    p = types.SimpleNamespace()
    p.cfg = cfg or mk_cfg(peer_hunt_probability=1.0, reply_delay_sec=0)
    p.bili = FakeBili(rows)
    p.llm = FakeLLM()
    p.store = store or FakeStore()
    p.sender = FakeSender()
    return p


ROWS = [
    {"rpid": 11, "mid": 3747575163260031, "uname": "ChenZRui的蓝色大肥鱼", "text": "自己", "like": 99},  # 她自己
    {"rpid": 12, "mid": 413116249, "uname": "六成因", "text": "主人", "like": 99},                       # 主人
    {"rpid": 13, "mid": 555, "uname": "爱造白饭的大肥鱼", "text": "47秒讲完，省的是token", "like": 33},
    {"rpid": 14, "mid": 556, "uname": "尘封的小肥鱼", "text": "看完顺手算了一下", "like": 12},
    {"rpid": 15, "mid": 557, "uname": "路人甲", "text": "这视频不错", "like": 50},
]

pipe = mk_pipe(ROWS)
out = asyncio.run(P.WhalePipeline._peer_hunt(pipe, {"bvid": "BV1xx", "aid": 42, "title": "省钱插件"}))
check("hunt ok", out["ok"] is True, out)
check("hunt 只搭一条（peer_hunt_max=1）", len(out["targets"]) == 1, out)
check("hunt 挑了点赞最高的鱼", out["targets"][0]["uname"] == "爱造白饭的大肥鱼", out)
check("hunt 真发了评论", len(pipe.bili.replied) == 1 and pipe.bili.replied[0]["root"] == 13, pipe.bili.replied)
check("hunt 记了额度", pipe.store.used == [555], pipe.store.used)
check("hunt 写进 handled", "peer:BV1xx:13" in pipe.store.handled, pipe.store.handled)
check("hunt 汇报了 QQ", pipe.sender.msgs and "爱造白饭的大肥鱼" in pipe.sender.msgs[0], pipe.sender.msgs)

# 已经搭过的不再搭
store2 = FakeStore(handled={"peer:BV1xx:13"})
pipe2 = mk_pipe(ROWS, store=store2)
out2 = asyncio.run(P.WhalePipeline._peer_hunt(pipe2, {"bvid": "BV1xx", "aid": 42, "title": "t"}))
check("hunt 跳过已搭过的", out2["ok"] is True and out2["targets"][0]["uname"] == "尘封的小肥鱼", out2)

# 额度用完
pipe3 = mk_pipe(ROWS, store=FakeStore(left=0))
out3 = asyncio.run(P.WhalePipeline._peer_hunt(pipe3, {"bvid": "BV1xx", "aid": 42, "title": "t"}))
check("hunt 无额度不发", out3["ok"] is False and pipe3.bili.replied == [], out3)

# 概率 0
pipe4 = mk_pipe(ROWS, cfg=mk_cfg(peer_hunt_probability=0))
out4 = asyncio.run(P.WhalePipeline._peer_hunt(pipe4, {"bvid": "BV1xx", "aid": 42, "title": "t"}))
check("hunt 概率 0 不发", out4["ok"] is False and "概率" in out4.get("skipped", ""), out4)

# 开关关掉
pipe5 = mk_pipe(ROWS, cfg=mk_cfg(peer_hunt_enable=False))
out5 = asyncio.run(P.WhalePipeline._peer_hunt(pipe5, {"bvid": "BV1xx", "aid": 42, "title": "t"}))
check("hunt 开关关", out5["ok"] is False and pipe5.bili.replied == [], out5)

# 没扫到鱼
pipe6 = mk_pipe([ROWS[4]])
out6 = asyncio.run(P.WhalePipeline._peer_hunt(pipe6, {"bvid": "BV1xx", "aid": 42, "title": "t"}))
check("hunt 没扫到鱼", out6["ok"] is False and "没扫到" in out6.get("skipped", ""), out6)

# min_like 过滤
pipe7 = mk_pipe(ROWS, cfg=mk_cfg(peer_hunt_probability=1.0, reply_delay_sec=0, peer_hunt_min_like=20))
out7 = asyncio.run(P.WhalePipeline._peer_hunt(pipe7, {"bvid": "BV1xx", "aid": 42, "title": "t"}))
check("hunt min_like 过滤", out7["ok"] is True and out7["targets"][0]["uname"] == "爱造白饭的大肥鱼", out7)

print(f"\n{'ALL PASS' if not bad else 'FAILED'}: {ok} ok / {len(bad)} fail")
if bad:
    for b in bad:
        print(" -", b)
    sys.exit(1)
