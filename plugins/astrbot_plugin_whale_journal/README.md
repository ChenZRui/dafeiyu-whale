# 大肥鱼的日记本（astrbot_plugin_whale_journal）

让大肥鱼自己写日记：每天几个固定时间点，把**这段时间和主人/群友聊了什么**、**在B站刷到了什么**、
**被谁吐槽、自己哪里翻车了**揉成一小段，配一个心情标签，记进本子里。

## 产出

全部落在 `data_dir`（默认 `data/whale_journal/`）：

| 文件 | 用途 |
| --- | --- |
| `entries.jsonl` | 一行一条，给程序用（增量追加，不重写） |
| `YYYY-MM-DD.md` | 当天日记的纯文本版，格式仿手写本：时间点 + `> 心情：xxx` + 正文 |
| `index.html` | 所有日记的网页版，浏览器打开就能看，带心情标签着色和搜索 |
| `state.json` | 上次写到什么时候（决定下一次的取材窗口） |

## 素材从哪来

不给插件额外记东西，直接读已经存在的两份数据：

- 对话：`astrbot_plugin_whale_memory` 的 `chatlog/<日期>.jsonl`（`{t, umo, role, text}`）
- B站：`astrbot_plugin_bili_whale` 的 `state.json` 里 `seen` 记录（`{ts, title, up, score, shared}`）

「翻车」是启发式抓的：对话里出现「错了 / 不对 / 你怎么 / 又 / 翻车 / 离谱 / 重新」这类词的、
非她自己发的短句，最多 5 条，作为线索交给模型，让它决定要不要写。

## 什么时候写

`crons` 里配几个 `HH:MM`，每到点就写一条。取材窗口 = 上一次写到的时刻 → 现在（跨天自动从当天 0 点算起）。
窗口里对话少于 `min_messages` 条、又没刷到视频，就**跳过不写**（免得记一堆空话），只把时间戳往前推。

## 命令

| 命令 | 作用 |
| --- | --- |
| `/日记`（别名 `/看日记`） | 把今天的日记发回聊天里 |
| `/写日记` | 立刻写一条（调试用） |
| `/日记状态` | 本子在哪、共几条、上次写到什么时候 |

三个命令都是管理员权限。

## 配置要点

- `persona_file`：填**人设文件的路径**（不是人设正文），读不到会退回内置的大肥鱼简介。
- `llm_provider_id`：默认 `deepseek/deepseek-flash`，留空则用 AstrBot 当前默认 provider。
- `push_daily_to_qq` + `push_at`：开着的话，每天 `push_at` 把当天日记发一份到 `owner_qq` 私聊。
- 改配置要重启 AstrBot 才生效（插件在 `initialize()` 里排定时任务）。

## 在电脑上看

本机 `D:\deepseek work common\大肥鱼日记\看日记.bat`：一条命令从服务器把 `index.html` 拉到本地并打开。

## 已知边界

- 只写「有素材」的时段，深夜没人说话就不会硬凑一条。
- 心情标签由模型给，长度限制 12 字；正文默认限 320 字。
- 依赖 whale_memory / bili_whale 的数据格式，任一方改文件名要同步改这里（`chatlog_dir` / `bili_state`）。

## 人设从哪来（重要）

默认 `persona_source = "astrbot"`：**直接读 AstrBot 面板里当前生效的人格**（`persona_manager.get_default_persona_v3()`，
也就是你给「大肥鱼」写的那份设定），所以以后在面板里改人格，B站评论、私信、日记会自动跟着变。

- 每轮开始时重读一次，改完人格不用重启插件；
- 启动日志会打 `人设来源：AstrBot 默认人格「zcr的大肥鱼」（3230 字）`，一眼能看出有没有吃到；
- 想固定用插件自己的文件就设 `persona_source = "file"`（读 `persona_file`，为空则用 `core/persona_default.md`）；
- `persona_id` 留空 = AstrBot 当前默认人格，也可指定某个人格 id；
- 以前的老毛病：插件只读 `persona_file`，和面板人格脱钩，人设从「鱼缸里的鱼」改成「住在电脑和网线里的智能体」之后
  插件还在按旧设定说话（私信里冒出「缸里的水」「你什么时候来换」），现在不会了。

## 主人画像与教学库（v1.1.0 新增）

- **主人画像**：每 6 小时（`profile_min_hours`）用最近 24 小时聊天算一次，写
  `whale_journal/owner_profile.json`：性格 / 生活习惯 / 口头禅 / 心情指数 0-10 /
  睡眠质量 / 120 字总结。命令 `/主人画像` 可以立刻算一次。
- **教学库**：主人在 QQ 里说「记住，这是黑话：xxx」「这是个梗：xxx」「这是调情：xxx」，
  会自动归类存进 `whale_journal/lessons.json`（四类：黑话 / 梗 / 调情 / 其他）；
  也可以 `/教我 黑话 xxx` 手动教。每次写完日记，会把日记里的「主人教我的」条目也分类收进去。
- 这两份数据由「大肥鱼面板」插件（`astrbot_plugin_dafeiyu_panel`）的
  「主人画像」「主人教我的」两个视图读取展示。

## 更新日志

### v1.2.0（2026-11-01）

- 「主人教我的」支持自定义分类、删除进回收站（30 天可还原）、改分类、保存完整对话。
- 教学库注入：把教过的黑话/梗/调情塞进她的 system prompt，她能听懂并顺着回（配置项 `lessons_inject_enable` / `lessons_inject_limit`）。
