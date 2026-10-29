"""鲸鱼日记的两本存书：公开本 diary.md 与私人本 diary-private.md。

设计要点：
- 两本都是 markdown，**只追加**：每次写入是 `## 日期 时间` 一节，节内是
  「看过哪些视频」表格 +「### 想说」散文 +「### 给他的信」，绝不复写整个文件；
  云端一天醒三次、一年下来也不会越写越慢。
- 另存 index.json 记条目数与最后日期，stats() 先读它；索引丢了/写坏了就回扫
  markdown 重建（这是写明的恢复路径，不是把异常吞掉）。真正的 IO 失败一律抛。
- 全部同步 IO（Path.read_text / open(..., "a")），不依赖 aiofiles；调用方要异步
  就直接 await asyncio.to_thread(...)，本模块不假设事件循环。
- 本模块刻意不 import astrbot，纯文件逻辑，方便脱离 AstrBot 直接跑测试。
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

PUBLIC_FILE = "diary.md"
PRIVATE_FILE = "diary-private.md"
INDEX_FILE = "index.json"

PUBLIC_HEADER = """# 鲸鱼日记

她刷完 B站留下的一页：看过什么、想说什么、给主人的信。
"""

PRIVATE_HEADER = """# 鲸鱼日记 · 私

不给别人看的那本：偏好校准、真实想法、记账。
"""

# 一条「给他的信」= ### 给他的信 之后的正文，到下一个标题/分隔线为止
_LETTER_RE = re.compile(r"^###\s*给他的信[^\n]*\n(.*?)(?=^###\s|^##\s|^---\s*$|\Z)", re.S | re.M)
# 统计条目：## 开头的节标题（含日期时间）
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)

_APPEND_RETRIES = 3
_RETRY_SLEEP = 0.05


@dataclass
class DiaryEntry:
    """一次醒来的完整产出。三个 text 字段都允许为空，空就不落那一节。"""

    date: str  # "2026-10-27 21:14"
    videos: list[dict] = field(default_factory=list)  # [{"bvid","title","up","url","note"}]
    thoughts: str = ""  # 公开本散文（她今天看视频的感想）
    letter: str = ""  # 「给他的信」——给主人的一封短信
    private: str = ""  # 私人本：偏好校准、真实想法


def _md_cell(text: object, limit: int = 60) -> str:
    """把任意文本塞进 markdown 表格单元格：压成一行、砍掉竖线、截断。"""
    s = " ".join(str(text or "").split())
    s = s.replace("|", "｜")  # 表格里的竖线会把列切坏，换全角
    if limit and len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s or "—"


def _video_url(video: dict) -> str:
    """视频链接：优先用给的 url，没有就拿 bvid 拼。"""
    url = str(video.get("url") or "").strip()
    if url:
        return url
    bvid = str(video.get("bvid") or "").strip()
    return f"https://www.bilibili.com/video/{bvid}" if bvid else ""


def _clean_body(text: str) -> str:
    """摘出来的段落：去首尾空白，去掉结尾多余的分隔线。"""
    body = (text or "").strip()
    body = re.sub(r"\n-{3,}\s*$", "", body).strip()
    return body


class DiaryStore:
    """两本日记的读写口。同一实例可跨线程用（内部有锁）。"""

    def __init__(self, dir_path: Path | str, max_read_chars: int = 3000):
        self.dir = Path(dir_path)
        self.max_read_chars = max(200, int(max_read_chars))
        # 目录不存在就建，AstrBot 里第一次跑的时候 data 目录是空的
        self.dir.mkdir(parents=True, exist_ok=True)
        self.public_path = self.dir / PUBLIC_FILE
        self.private_path = self.dir / PRIVATE_FILE
        self.index_path = self.dir / INDEX_FILE
        self._lock = threading.Lock()

    # ---------- 读 ----------
    def read_public(self) -> str:
        """公开本最近 max_read_chars 字，给 LLM 续写时当上下文。"""
        return self._read_tail(self.public_path)

    def read_private(self) -> str:
        """私人本最近 max_read_chars 字。"""
        return self._read_tail(self.private_path)

    def latest_letter(self) -> str:
        """最近一次非空的「给他的信」正文；一封都没有就返回空串。"""
        if not self.public_path.exists():
            return ""
        text = self.public_path.read_text(encoding="utf-8")
        for m in reversed(list(_LETTER_RE.finditer(text))):
            body = _clean_body(m.group(1))
            if body:
                return body
        return ""

    def stats(self) -> dict:
        """{"entries": int, "last_date": str, "path": str}"""
        with self._lock:
            data = self._load_index()
        return {
            "entries": int(data.get("entries", 0) or 0),
            "last_date": str(data.get("last_date", "") or ""),
            "path": str(self.public_path),
        }

    def _read_tail(self, path: Path) -> str:
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8")
        limit = self.max_read_chars
        if len(text) <= limit:
            return text
        tail = text[-limit:]
        # 能从节首开始读就从节首开始，别给 LLM 递半句话
        cut = tail.find("\n## ")
        if cut != -1 and (len(tail) - cut) >= limit // 2:
            tail = tail[cut + 1 :]
        return "……（前略）\n" + tail.lstrip("\n")

    # ---------- 写 ----------
    def append_entry(self, entry: DiaryEntry) -> None:
        """追加一条：公开本一节 + 私人本一节（private 为空则不写私人本）。"""
        date = (entry.date or "").strip()
        if not date:
            raise ValueError("DiaryEntry.date 不能为空，写成 \"2026-10-27 21:14\" 这种")

        public_block = self._render_public(entry, date)
        private_block = self._render_private(entry, date)

        with self._lock:
            # 先取旧索引再落盘：索引缺失时要回扫 markdown 重建，绝不能把刚写进去的这节
            # 也数进去，否则 entries 从第一条起就偏大
            index = self._load_index()
            self._append(self.public_path, PUBLIC_HEADER, public_block)
            if private_block:
                self._append(self.private_path, PRIVATE_HEADER, private_block)
            self._write_index(index, date)

    def _render_public(self, entry: DiaryEntry, date: str) -> str:
        lines: list[str] = [f"## {date}", ""]
        if entry.videos:
            lines.append("| 视频 | UP 主 | 标题 | 一句话看法 |")
            lines.append("| --- | --- | --- | --- |")
            for v in entry.videos:
                if not isinstance(v, dict):
                    continue
                bvid = str(v.get("bvid") or "").strip()
                url = _video_url(v)
                link = f"[{bvid}]({url})" if bvid and url else (bvid or "—")
                lines.append(
                    "| {} | {} | {} | {} |".format(
                        link,
                        _md_cell(v.get("up"), 30),
                        _md_cell(v.get("title"), 70),
                        _md_cell(v.get("note"), 90),
                    )
                )
            lines.append("")
        if entry.thoughts.strip():
            lines += ["### 想说", "", entry.thoughts.strip(), ""]
        if entry.letter.strip():
            lines += ["### 给他的信", "", entry.letter.strip(), ""]
        lines += ["---", ""]
        return "\n".join(lines).rstrip("\n") + "\n\n"

    def _render_private(self, entry: DiaryEntry, date: str) -> str:
        if not entry.private.strip():
            return ""
        lines = [f"## {date}", "", entry.private.strip(), "", "---"]
        return "\n".join(lines).rstrip("\n") + "\n\n"

    def _append(self, path: Path, header: str, block: str) -> None:
        """追加写。文件被别的进程占着（Windows/云端同步盘都可能）就重试几次。"""
        last: Exception | None = None
        for attempt in range(1, _APPEND_RETRIES + 1):
            try:
                fresh = (not path.exists()) or path.stat().st_size == 0
                with path.open("a", encoding="utf-8", newline="\n") as fh:
                    if fresh:
                        fh.write(header.rstrip("\n") + "\n\n")
                    fh.write(block)
                return
            except OSError as e:
                last = e
                if attempt < _APPEND_RETRIES:
                    time.sleep(_RETRY_SLEEP * attempt)
        raise OSError(f"追加日记失败（重试 {_APPEND_RETRIES} 次）：{path}：{last}") from last

    # ---------- 索引 ----------
    def _write_index(self, base: dict, date: str) -> None:
        payload = {
            "entries": int(base.get("entries", 0) or 0) + 1,
            "last_date": date,
            "updated": _now_text(),
        }
        tmp = self.index_path.with_name(INDEX_FILE + ".tmp")
        # 先写临时文件再替换，避免进程被杀时留下半个 json
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.index_path)

    def _load_index(self) -> dict:
        """读索引；文件不存在或内容坏了，就按 markdown 里现有节数重建。"""
        try:
            raw = self.index_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            pass
        return self._rebuild_index()

    def _rebuild_index(self) -> dict:
        count = 0
        last_date = ""
        if self.public_path.exists():
            text = self.public_path.read_text(encoding="utf-8")
            for m in _SECTION_RE.finditer(text):
                count += 1
                last_date = m.group(1).strip()
        return {"entries": count, "last_date": last_date, "updated": ""}


def _now_text() -> str:
    """索引里的 updated 时间戳（本地时间，给人工排查用）。"""
    return time.strftime("%Y-%m-%d %H:%M:%S")
