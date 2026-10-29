# 刷B站的大肥鱼（astrbot_plugin_bili_whale）

让大肥鱼自己刷 B 站：挑视频 → 打分 → 在 B 站评论时 @ 主人或私信主人 → 写双份日记 → 把细节分享到 QQ 私聊，并把喜欢的视频转发到群。

## 功能

- **一天随机时段刷**（默认）：每天 `plan_cron` 生成当天行程，在 `day_window` 窗口里随机挑 `day_slots_min`~`day_slots_max` 个时刻醒来，相邻至少隔 `day_slot_gap_min` 分钟——不像定时任务，像真人想起来才刷。
- **计划可续跑**：当天行程写进 `state.json` 的 `plan` 字段。进程重启后会把还没跑的那几次接回来（超过 5 分钟的过期轮次标记 `skipped`，不会半夜补跑）。
- **旧夜场模式**（`day_random=false`）：按 `night_cron` 开一场，每场 `night_runs` 轮，轮间隔 `run_interval_min ± run_interval_jitter` 分钟。
- **选题**：主源是 B 站首页推荐（homepage，登录态个性化），搜索兜底；按 LLM 打分排序，低于 `min_score` 不分享，每天最多 `share_daily_quota` 轮。
- **互动**：可在 B 站发评论（可 @ 主人）、私信主人链接、点赞。
- **两本日记**：公开的 `diary.md` 与私密的 `diary-private.md`，记录看了什么、想了什么。
- **QQ 推送**：`share_via` 可选 `friend` / `group` / `both`，推送到 `owner_qq` 私聊与 `group_ids` 里的群。
- **记忆注入**：LLM 对话时自动注入最近看过的视频与日记片段，聊天时能接上"她刷过什么"。
- **手动触发**：`/刷B站`（`/刷B站 群列表` 查看机器人所在群）。

## 依赖

- AstrBot v4.x
- `bilibili-api-python`（见 `requirements.txt`）
- B 站登录凭证 JSON（`bili_credential_file`，至少含 `sessdata` / `bili_jct` / `buvid3`）

## 配置（面板 → 插件 → 刷B站的大肥鱼）

| 字段 | 说明 |
| --- | --- |
| `enable` | 总开关 |
| `owner_qq` | 主人 QQ（私聊推送目标） |
| `group_ids` | 要转发到的群号列表 |
| `share_via` | `friend` / `group` / `both` |
| `day_random` | 一天随机时段刷（推荐开） |
| `plan_cron` | 每天几点生成当天行程，默认 `0 8 * * *` |
| `day_window` | 随机时刻允许落在的窗口，默认 `["09:00", "23:00"]` |
| `day_slots_min` / `day_slots_max` | 每天醒来几次，默认 3~4 |
| `day_slot_gap_min` | 两次之间至少隔几分钟，默认 90 |
| `night_cron` | 夜场 cron（仅 `day_random=false` 时生效），默认 `0 19 * * *` |
| `night_runs` | 夜场几轮，默认 3 |
| `run_interval_min` / `run_interval_jitter` | 夜场轮间隔与抖动（分钟） |
| `max_videos` | 每轮最多看几个 |
| `min_score` | 分享分数门槛 |
| `share_daily_quota` | 每天最多分享几**轮**（一轮里评论+私信只算一次） |
| `bili_credential_file` | B 站凭证 JSON 路径 |
| `diary_dir` | 日记与状态目录 |
| `llm_provider_id` | 打分/写日记用的 provider，如 `deepseek/deepseek-flash` |
| `persona_file` | 人设文件**路径**（填路径，不要填正文） |

## B 站评论区互动（新）

收件箱每 `inbox_interval_min` 分钟扫一次 `@我的 / 回复我的 / 私信`，先判「这人是谁」，再决定怎么接：

| 身份 | 判法 | 默认动作 |
| --- | --- | --- |
| 主人 `owner` | `owner_bili_mid` | 在评论区回他（`reply_owner`） |
| 别的鱼 `peer` | 昵称/正文命中 `peer_keywords`，或可疑时让模型判一次 | 回，并且能顺着聊几轮（`peer_thread_max_turns`） |
| 网友 `stranger` | 其余 | 看 `reply_stranger` |
| 她自己 `self` | `self_bili_mid` | 跳过，避免自问自答 |

回完之后把「谁在哪个视频下 @ 了什么、我回了什么」汇报到 QQ 私聊。

相关配置（都在插件配置里，默认值见 `core/config.py` 的 `DEFAULTS`）：

| key | 说明 |
| --- | --- |
| `comment_reply_enable` | 总开关，关掉就只汇报不回 |
| `reply_owner` / `reply_stranger` | 回不回主人 / 回不回陌生人 |
| `reply_delay_sec` | 回之前随机等 0~N 秒，别秒回得太像机器 |
| `peer_enable` | 认鱼 + 和别的鱼互动总开关 |
| `peer_keywords` | 认鱼关键词，默认 `大肥鱼/肥鱼/鲸鱼/小鲸鱼/whale` |
| `peer_llm_judge` | 关键词没命中但看着像 bot 时，让模型判一次 |
| `peer_daily_limit` | 每天最多在评论区回几条（主人 + 别的鱼 + 网友共用） |
| `peer_thread_max_turns` | 同一条评论线下最多聊几轮，到数就收 |
| `peer_proactive` | 预留：主动去别的鱼的评论区找人聊（默认关） |

数据落在 `state.json` 的 `peer` 段：`{date, count, threads: {"BV号:根评论rpid": {turns, uid, ts}}}`，跨天自动清零。
`handled` 里 @ / 回复 / 私信的 key 带 `at:`/`reply:`/`dm:` 前缀，不会和旧记录串。

## 私信双向（新）

主人在 B站私信里找她说话，她会回（默认开）：

- 只处理和 `owner_bili_mid` 的私信会话；`dm_auto_reply` 关掉就退回「只汇报到 QQ」。
- 陌生人的私信默认只汇报不回（`dm_reply_stranger` 可开，注意被刷屏）。
- 回完在 QQ 私聊里同步一句「B站私信：谁说了啥 / 我已回：…」。
- 每次扫收件箱最多回 `dm_max_per_tick` 条，带 `dm_context_turns` 轮上下文。
- 回话走 `llm.dm_reply_text()`，比评论区更亲近（私聊语气，120 字内）。

## 她看过什么 + 主人打分（新）

`watch.html` 由插件在每轮结束后自动重画，写在 `diary_dir`（默认 `data/bili_whale/watch.html`），
本地双击 `看观看历史.bat` 拉一份即可查看：每条带序号、发布时间、她当时打的分、是否推出去过、主人的星级和评价，支持搜索。

打分方式（QQ 里发给她）：

| 指令 | 作用 |
| --- | --- |
| `/刷B站观看历史` | 列最近 15 条（带序号和星级），同时记住序号方便打分 |
| `/打分 3 5 这种多来点` | 第 3 条给 5 星 + 一句评价 |
| `/打分 BV18EsZ6kE3m 2 又是数码，别推了` | 直接按 BV 号打分 |

她也能听懂自然语言（LLM 工具 `rate_video`）：说「第 2 条太水了，给 2 分」也行。

分数怎么影响下次选题：
- 4–5 星的视频，其所属方向在 `llm.score()` 的提示里进「主人最近给高分的」，同类内容加分；
- 1–2 星进「主人明确嫌弃的」，同类内容压到 8 分以下；
- 主人的评分优先级高于标题吸引力和播放量（提示里写死）。
- `feedback_boost` / `feedback_penalty` 是这两个方向的启发式权重调节口。

数据落在 `state.json` 的 `seen`（看过的视频）+ `feedback`（`bvid -> {stars, note, ts}`）+ `last_list`（上次列表顺序）里。

## 互动克制：去链接 / 降频 / 收藏 / 投币

新号（Lv2、粉丝个位数）在 B站 上「每轮都评论 + 评论里带网址」最容易被判定脚本，评论会被折叠成「仅自己可见」——
主人在手机上看不到，看起来就像「她没 @ 我」。所以：

- `comment_strip_links = true`：评论里**所有网址和裸域名都删掉**（`@主人` 保留），私信照旧带链接；
- `comment_probability = 0.45`：每轮只有 45% 的概率发评论，其余轮次只发私信 + 点赞；想恢复每轮都评论就填 `1.0`；
- `fav_enable = true` + `fav_min_score = 15`：评分够高的视频收进默认收藏夹（`fav_folder_id=0` 表示自动查默认夹），脚本
  会先用 `x/v2/fav/video/favoured` 复核，早就收藏过的不算失败；
- `coin_enable = true` + `coin_daily_max = 1` + `coin_min_stars`：**每天最多一枚币，只投给主人打星最高的那条**（主人还没打分就不投，
  她硬币只有 3 枚）。注意：从服务器机房 IP 投币，B站 会返回 `-401 非法访问`（要求人机验证），这种情况当天的投币会被跳过，
  并自动在 QQ 上请主人代投一次；收藏和点赞不受影响。

## 人设从哪来（重要）

默认 `persona_source = "astrbot"`：**直接读 AstrBot 面板里当前生效的人格**（`persona_manager.get_default_persona_v3()`，
也就是你给「大肥鱼」写的那份设定），所以以后在面板里改人格，B站评论、私信、日记会自动跟着变。

- 每轮开始时重读一次，改完人格不用重启插件；
- 启动日志会打 `人设来源：AstrBot 默认人格「zcr的大肥鱼」（3230 字）`，一眼能看出有没有吃到；
- 想固定用插件自己的文件就设 `persona_source = "file"`（读 `persona_file`，为空则用 `core/persona_default.md`）；
- `persona_id` 留空 = AstrBot 当前默认人格，也可指定某个人格 id；
- 以前的老毛病：插件只读 `persona_file`，和面板人格脱钩，人设从「鱼缸里的鱼」改成「住在电脑和网线里的智能体」之后
  插件还在按旧设定说话（私信里冒出「缸里的水」「你什么时候来换」），现在不会了。

## 打分台：她看过什么 + 主人打分（2026-10-29）

两个入口，同一份数据（`data/bili_whale/state.json` + `ratings.json`）：

1. **每天 21:00 自动推文件到 QQ**（`daily_push_enable` / `daily_push_cron`）
   文件叫 `大肥鱼看过什么.html`，封面直接内嵌在文件里，手机上也能看。
   点星星/写评价要用「在线版」：双击 `D:\deepseek work common\大肥鱼看过什么\看观看历史.bat`
   （它起一条 SSH 隧道 → 打开 `http://127.0.0.1:18900/watch`），打完分立刻存服务器，
   她**下一轮醒来**就会把分合进来，按分调第二天的选题权重、当天投币也挑分最高的那条。
2. **QQ 里打分**：`/刷B站观看历史` 看列表 → `/打分 3 5 这种多来点`（序号 星数 评价），
   星会同时回写 `ratings.json`，网页上也看得到。
3. **随时要一份**：`/刷B站打分表`。

配置（插件配置面板可见）：

| key | 默认 | 说明 |
| --- | --- | --- |
| `watchui_url` | `http://127.0.0.1:18900` | 打分台服务（只监听本机，靠 SSH 隧道访问） |
| `watchui_token_file` | `data/bili_whale/watchui_token.txt` | 打分台口令文件 |
| `daily_push_enable` | `true` | 每天推送打分文件 |
| `daily_push_cron` | `0 21 * * *` | 推送时间（每天 21:00） |
| `daily_push_file` | `大肥鱼看过什么.html` | 推送文件名 |
| `topic_weight_enable` | `true` | 按主人打星的主题均分调选题权重 |

打分怎么影响她：
- `topic_weights()` 按每条看过视频的 `keyword` 汇总主人给的平均星；
- 选题打分时 `base += topic_bonus(keyword)`：均分 ≥4 加 `feedback_boost`（默认 3），≤2 扣 `feedback_penalty`（默认 5）；
- LLM 选题 prompt 里也会带上「主人夸过 / 主人嫌弃过」的具体评价原话。

打分台服务本体：`/opt/bili-watch-ui/watchui.py`（systemd `qqbot-watchui.service`，127.0.0.1:18900），
接口 `GET /watch?t=口令`、`GET /snapshot?t=口令`、`POST /rate?t=口令`、`GET /cover/<file>`；
封面缓存 `data/bili_whale/covers/`，用 SESSDATA 拉 B 站 `pic` 字段回填。

## 数据

默认在 `data/bili_whale/`：`state.json`（已看视频、当日分享数、上次醒来时间、当天 `plan`）、`index.json`、`diary.md`、`diary-private.md`。

## 注意

- 旧夜里模式（`day_random=false`）下，进程重启会让当天已排好的轮次全部作废，cron 当天不再补跑，只能等次日；**随机模式没有这个问题**（计划落盘、重启续跑）。
- `persona_file` 必须是**文件路径**；误填人设正文会报 `[Errno 36] File name too long`（不影响推送，但人设不生效）。

## 作者

dafeiyu

## 收藏标签 + 手机在线版（2026-10-29）

- **收藏**：网页上每条视频有「☆ 收藏」按钮；QQ/私信里发 `/收藏 3`（序号来自 `/刷B站观看历史`）也能收。
  `store.rate(fav=...)` / `store.fav_set()` / `store.toggle_fav()` 维护，`state.json` 的 `feedback[bvid].fav` 为准，
  5 星也自动算收藏。选题时同主题额外 +5 分（`_score` 里的 `fav_topics`），打分 prompt 里也会标「主人收藏了」。
- **按天分组 + 筛选**：页面按 `day` 分块（`10-29 星期四 · N 条`），顶部六个页签：全部 / 她推给我的 / 没推给我的 /
  收藏 / 待我评价 / 评过的；序号仍与聊天里的 #N 一致。
- **手机在线版**：`cloudflared` quick tunnel 把 `127.0.0.1:18900` 暴露成公网 https，地址写在
  `/opt/bili-watch-ui/public_url.txt`（同时同步到 `data_dir/public_url.txt`），页面顶部与每日推送文案都会带上
  `<公网>/watch?t=<口令>`。systemd 单元 `qqbot-watchtunnel.service`（脚本 `/opt/bili-watch-ui/tunnel.sh`）。
  注意：quick tunnel 重启会换域名，页面/推送里永远显示当前那条。
- **封面**：`/cover/` 已免口令（`<img>` 带不上 token 才导致之前整页封面 403）。

### B站私信的双向边界（2026-10-30 调整）

B站私信推视频这条路走不通：B站私信只能发链接/纯图，做不出 QQ 那种可点的视频卡片，主人点不进去。
所以现在的分工是：

- **推视频只走 QQ**：封面图 + 标题 + 链接，QQ 里点得开；
- **B站私信只做双向对话**：主人私信甩一条视频过来 → 她拉视频详情、写完感受再私信回你（`dm_video_review`，默认开）；
- `share_via` 建议保持 `comment`（只在评论区 @ 主人，不再往 B站私信推视频）。

私信里也能直接下确定性指令：「列表」→ 她回 10 条带序号的看到列表；「3号 5星 这种多推」→ 直接记分。
