"""把「大肥鱼面板」插件的四个视图直接搬到独立站上。

为什么要这么干：以前独立站（watchui.py）和面板插件各维护一套 HTML，
每次加功能都要改两遍，用户就会遇到「面板有新功能、双击 bat 打开的还是旧的」。
现在两边共用同一份 `views/<视图名>/index.html`，独立站只做两件事：

1. 把面板的视图 HTML 原样发出去，并注入一段 bridge 垫片（把
   `window.AstrBotPluginView` 映射到本站的 `/__api/...`）；
2. 提供 `/__api/<endpoint>` 接口，内部直接调用面板插件的 `core/store.py`。

于是面板和独立站的数据、界面、排序、皮肤永远是同一份，改一处两边都生效。
"""

from __future__ import annotations

import importlib.util
import json
import os
import urllib.parse
from pathlib import Path

PANEL_DIR = Path(os.environ.get("WATCHUI_PANEL_DIR", "/opt/astrbot/data/plugins/astrbot_plugin_dafeiyu_panel"))
VIEWS_DIR = Path(os.environ.get("WATCHUI_VIEWS", str(PANEL_DIR / "views")))

DATA = Path(os.environ.get("WATCHUI_DATA", "/opt/astrbot/data/bili_whale"))
JOURNAL = Path(os.environ.get("WATCHUI_JOURNAL", "/opt/astrbot/data/whale_journal/entries.jsonl"))

# URL → 视图目录名（目录名同时也是面板里的 tab 标题）
ROUTES = {
    "/watch": "她看过什么",
    "/diary": "她的日记本",
    "/profile": "主人画像",
    "/lessons": "主人教我的",
}

_CONF = {
    "bili_state_file": str(DATA / "state.json"),
    "cover_dir": str(DATA / "covers"),
    "journal_file": str(JOURNAL),
    "own_dir": str(Path(os.environ.get("WATCHUI_PLUGIN_DATA", "/opt/astrbot/data/plugin_data/astrbot_plugin_dafeiyu_panel"))),
    "page_size": 24,
    "journal_limit": 300,
    "lessons_limit": 400,
    "allow_edit": True,
    "show_score": True,
}

_SHIM = """
<script>
(function () {
  var TK = (new URLSearchParams(location.search)).get('t') || '';
  function qs(params) {
    var p = new URLSearchParams();
    if (TK) p.set('t', TK);
    Object.keys(params || {}).forEach(function (k) {
      var v = params[k];
      if (v !== undefined && v !== null && v !== '') p.set(k, v);
    });
    return p.toString();
  }
  async function apiGet(endpoint, params) {
    var r = await fetch('/__api/' + endpoint + '?' + qs(params), { headers: { 'Accept': 'application/json' } });
    var j = await r.json();
    if (j && j.status === 'error') throw new Error(j.message || '接口出错');
    return (j && Object.prototype.hasOwnProperty.call(j, 'data')) ? j.data : j;
  }
  async function apiPost(endpoint, body) {
    var r = await fetch('/__api/' + endpoint + '?' + qs({}), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
    var j = await r.json();
    if (j && j.status === 'error') throw new Error(j.message || '保存失败');
    return (j && Object.prototype.hasOwnProperty.call(j, 'data')) ? j.data : j;
  }
  window.AstrBotPluginView = window.AstrBotPluginPage = {
    ready: function () { return Promise.resolve(); },
    apiGet: apiGet, apiPost: apiPost,
    download: function (u) { window.open('/__api/' + u + '?' + qs({}), '_blank'); },
    upload: function () { return Promise.resolve({}); },
    subscribeSSE: function () { return { close: function () {} }; }
  };
  // 面板里的「← →」导航指向 ../另一个视图/index.html，独立站要换成自己的路由
  var MAP = {
    '她看过什么': '/watch', '她的日记本': '/diary', '主人画像': '/profile', '主人教我的': '/lessons'
  };
  function fixLinks() {
    document.querySelectorAll('a[href]').forEach(function (a) {
      var h = a.getAttribute('href') || '';
      var m = h.match(/\\.\\.\\/([^/]+)\\/index\\.html/);
      if (m && MAP[m[1]]) a.setAttribute('href', MAP[m[1]] + (TK ? ('?t=' + TK) : ''));
    });
  }
  // 视图里的导航还有点 JS 直接写 location.href 的，capture 阶段先拦下来换成路由
  var NAV = { watch: '/watch', journal: '/diary', profile: '/profile', lessons: '/lessons' };
  document.addEventListener('click', function (e) {
    var a = e.target && e.target.closest ? e.target.closest('a[data-nav]') : null;
    if (!a) return;
    var route = NAV[a.getAttribute('data-nav')];
    if (!route) return;
    e.preventDefault();
    e.stopPropagation();
    location.href = route + (TK ? ('?t=' + TK) : '');
  }, true);
  var VIEWS = { '她看过什么': '/watch', '她的日记本': '/diary', '主人画像': '/profile', '主人教我的': '/lessons' };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fixLinks);
  else fixLinks();
})();
</script>
"""


def _load_store():
    """按文件路径加载面板插件的数据层，避免包名和 AstrBot 的 core 撞车。"""
    if hasattr(_load_store, "_store"):
        return _load_store._store
    spec = importlib.util.spec_from_file_location("dafeiyu_panel_store", PANEL_DIR / "core" / "store.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("找不到面板插件的 core/store.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _load_store._store = mod.PanelData(dict(_CONF), "astrbot_plugin_dafeiyu_panel")
    return _load_store._store


def has_views() -> bool:
    return VIEWS_DIR.is_dir() and any((VIEWS_DIR / v / "index.html").exists() for v in ROUTES.values())


def route_for(path: str) -> str:
    """把 /她看过什么/index.html 这类相对链接也认成 /watch。"""
    path = urllib.parse.unquote(path)
    for url, name in ROUTES.items():
        if path in (url, url + "/", "/%s/index.html" % name, "/%s/" % name):
            return url
    return path


def page(path: str) -> str | None:
    """返回某个路由的完整 HTML（已经注入 bridge 垫片）；没有这个视图就返回 None。"""
    path = route_for(path)
    name = ROUTES.get(path)
    if not name:
        return None
    f = VIEWS_DIR / name / "index.html"
    if not f.exists():
        return None
    html = f.read_text(encoding="utf-8")
    if "</body>" in html:
        return html.replace("</body>", _SHIM + "</body>", 1)
    return html + _SHIM


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def api(endpoint: str, query: dict, body: dict | None = None) -> dict:
    """把面板的接口原样复刻给独立站用，返回 {'status': 'ok', 'data': ...}。"""
    st = _load_store()
    if endpoint == "items":
        payload = st.page(
            filter_=(query.get("filter") or "all"),
            q=(query.get("q") or ""),
            page=_int(query.get("page"), 1) or 1,
            size=_int(query.get("size"), 24) or 24,
        )
        payload["allow_edit"] = True
        payload["status"] = st.status()
        return {"status": "ok", "data": payload}
    if endpoint == "cover":
        bvid = (query.get("bvid") or "").strip()
        return {"status": "ok", "data": {"bvid": bvid, "data": st.cover_b64(bvid)}}
    if endpoint == "journal":
        entries = st.journal(q=(query.get("q") or ""), limit=_int(query.get("limit"), 300) or 300)
        days: dict[str, int] = {}
        for e in entries:
            days[e["day"]] = days.get(e["day"], 0) + 1
        return {
            "status": "ok",
            "data": {
                "total": len(entries),
                "days": [{"day": d, "n": n} for d, n in sorted(days.items(), reverse=True)],
                "entries": entries,
                "status": st.status(),
            },
        }
    if endpoint == "profile":
        data = st.profile()
        data["status"] = st.status()
        return {"status": "ok", "data": data}
    if endpoint == "lessons":
        payload = st.lessons(q=(query.get("q") or ""), limit=_int(query.get("limit"), 400) or 400)
        payload["status"] = st.status()
        return {"status": "ok", "data": payload}
    if endpoint == "status":
        return {
            "status": "ok",
            "data": {"enabled": True, "allow_edit": True, "summary": st.summary(), "sources": st.status()},
        }
    if endpoint == "rate" and body is not None:
        result = st.rate(
            str(body.get("bvid") or "").strip(),
            _int(body.get("stars"), 0),
            str(body.get("note") or ""),
            body.get("fav"),
        )
        if not result.get("ok"):
            return {"status": "error", "message": str(result.get("message") or "保存失败")}
        return {"status": "ok", "data": result}
    return {"status": "error", "message": "不认识的接口：%s" % endpoint}


def home_page(token: str) -> str:
    """独立站首页：四个入口 + 说明（面板里就是四个 tab，这里给个菜单）。"""
    cards = [
        ("/watch", "她看过什么", "点星星、写两句、点收藏，她下一轮选题按这个调权重"),
        ("/diary", "她的日记本", "她每天写的小本子：跟谁聊了、被教了什么、B站刷到什么"),
        ("/profile", "主人画像", "她眼里的你：性格、习惯、口头禅、心情指数、睡眠"),
        ("/lessons", "主人教我的", "黑话 / 梗 / 调情，你在 QQ 里教她的都在这里"),
    ]
    items = "".join(
        "<a class='card' href='%s%s'><b>%s</b><span>%s</span></a>"
        % (u, ("?t=" + token) if token else "", t, d)
        for u, t, d in cards
    )
    return (
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>大肥鱼面板</title><style>"
        "html,body{margin:0;background:#0b0f19;color:#e8eefc;"
        "font:15px/1.7 'PingFang SC','Microsoft YaHei',system-ui,sans-serif}"
        ".wrap{max-width:880px;margin:0 auto;padding:40px 20px}"
        "h1{font-size:26px;margin:0 0 6px}h1 i{color:#5ee1c1;font-style:normal}"
        ".sub{color:#8b9ab5;margin:0 0 24px;font-size:13.5px}"
        ".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}"
        ".card{display:block;background:#111827;border:1px solid #1f2b45;border-radius:16px;"
        "padding:18px;text-decoration:none;color:inherit;transition:.18s}"
        ".card:hover{border-color:#5ee1c1;transform:translateY(-2px)}"
        ".card b{display:block;font-size:17px;margin-bottom:6px}"
        ".card span{color:#8b9ab5;font-size:13px}"
        "</style></head><body><div class='wrap'>"
        "<h1>大肥鱼<i>面板</i></h1>"
        "<p class='sub'>和 AstrBot 面板里的四个视图是同一套页面、同一份数据。</p>"
        "<div class='grid'>%s</div></div></body></html>" % items
    )
