#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给「看过什么」页面补封面：用 bilibili-api 拿 pic（bilibili 的 view 接口在机房 IP 上会 412，
必须走库里的签名/头），再把图下载到 covers/ 并写 covers/index.json。

    python3 backfill_covers.py [--limit N] [--force]
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import urllib.request
from pathlib import Path

DATA = Path("data/bili_whale")
STATE = DATA / "state.json"
COVERS = DATA / "covers"
INDEX = COVERS / "index.json"
CRED = Path("data/bili_whale/credentials.json")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def save(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def download(url: str, dest: Path, max_w: int = 480) -> bool:
    if url.startswith("//"):
        url = "https:" + url
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read()
    except Exception as e:
        print("  下载失败", repr(e)[:80])
        return False
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(raw)).convert("RGB")
        if im.width > max_w:
            im = im.resize((max_w, int(im.height * max_w / im.width)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=76, optimize=True)
        dest.write_bytes(buf.getvalue())
    except Exception:
        dest.write_bytes(raw)
    return dest.stat().st_size > 800


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    sys.path.insert(0, "data/plugins/astrbot_plugin_bili_whale")
    from bilibili_api import Credential, video  # type: ignore

    cred_raw = load(CRED, {})
    cred = Credential(
        sessdata=cred_raw.get("sessdata") or "",
        bili_jct=cred_raw.get("bili_jct") or "",
        buvid3=cred_raw.get("buvid3") or "",
    )
    state = load(STATE, {})
    seen = state.get("seen") or {}
    COVERS.mkdir(parents=True, exist_ok=True)
    index = load(INDEX, {}) or {}

    todo = []
    for bvid, v in seen.items():
        name = bvid + ".jpg"
        if not a.force and (COVERS / name).exists() and (COVERS / name).stat().st_size > 800:
            index.setdefault(bvid, index.get(bvid) or {"url": str((v or {}).get("cover") or ""), "file": name})
            continue
        todo.append(bvid)
    if a.limit:
        todo = todo[: a.limit]
    print(f"待补封面 {len(todo)} 条 / 共 {len(seen)} 条")

    ok = fail = 0
    for i, bvid in enumerate(todo, 1):
        pic = ""
        for attempt in range(3):
            try:
                info = await video.Video(bvid=bvid, credential=cred).get_info()
                pic = str((info or {}).get("pic") or "")
                break
            except Exception as e:
                msg = repr(e)[:90]
                await asyncio.sleep(1.5 * (attempt + 1))
        if not pic:
            print(f"[{i}/{len(todo)}] {bvid} 取 pic 失败")
            fail += 1
            continue
        name = bvid + ".jpg"
        if download(pic, COVERS / name):
            index[bvid] = {"url": pic, "file": name}
            ok += 1
            if i % 10 == 0:
                save(INDEX, index)
                print(f"[{i}/{len(todo)}] ok={ok} fail={fail}")
        else:
            fail += 1
        await asyncio.sleep(0.35)

    save(INDEX, index)
    print(f"完成 ok={ok} fail={fail}")


if __name__ == "__main__":
    asyncio.run(main())
