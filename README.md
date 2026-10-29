# 大肥鱼·B站分身（Dafeiyu Whale）

> 让 QQ / B站 上的「大肥鱼」自己刷 B站、挑视频、评论 @ 你、给你私信、写日记，
> 还给她配了一个**观看历史打分台**——你给她打星写评价，她第二天照着你的口味挑视频。

一套三件套，全部跑在 AstrBot 4.29+ 上：

| 组件 | 作用 |
| --- | --- |
| `plugins/astrbot_plugin_bili_whale` | 核心插件：自动刷视频 + B站互动 + QQ 推送 + 收件箱（评论 @ / 私信双向 / 认同类） |
| `plugins/astrbot_plugin_whale_journal` | 日记本插件：每天 4 次把「你们聊了什么、她看了什么、她犯了什么错」写成带心情标签的日记 |
| `watchui/watchui.py` | 打分台小服务：观看历史墙、按天目录、星星打分、评价、收藏、一键跳 B站，手机可在线打分 |

> 界面：左边「按天看」目录（每天几条一目了然），右边是卡片墙——封面、标题、她给的分数、主题、BV 号、
> 五星打分、评价输入框、「收藏」按钮和「▶ 去B站打开」。手机上目录收成横向滑条，卡片单列。

---

## 这是什么 / 为什么做

起因很简单：一个会说话的大肥鱼，光在群里回消息不够真实。她应该有自己的**生活**——
晚上自己去 B站刷视频，挑出她真觉得有意思的发给你，在评论区 @ 你，把她的碎碎念写进日记里。
而你（主人）能看见她到底看了什么、能给她打分，让她「第二天按你的口味挑」。

这个仓库就是把这一整套从零做出来的东西，包括一路上撞过的所有墙（B站接口、风控、AstrBot 调度、定时任务吞轮次……）
都写进了 [踩坑清单](#踩坑清单那些真的会把你熬到凌晨的点)，别人照抄就能少熬几个晚上。

### 她会做的事

- **自己刷 B站**：拉首页推荐（登录态个性化）+ 按主题搜索，用 LLM 给每条打分，挑最高分的那条；
- **一轮下来的动作**（每个都可单独开关）：写评论并 @ 主人、点赞、收藏、投币、私信主人、把视频发到群；
- **私聊主人**：定时/随机给她一个「主动想起你」的机会，用自己的话说今天看到的东西；
- **写日记**：每天多次，记下你们的对话、她看到的视频、她的心情、她认的错；
- **收件箱**：主人的评论 @ 她会回；主人的 B站私信她会回（双向）；认得出「别人家的大肥鱼」并上去搭话；
- **按你的口味来**：你给视频打星/写评价 → 她第二天选题权重和当天投币都看这个。

---

## 快速开始

### 0. 环境

- AstrBot **4.29.x**（`astrbot` >= 4.29）
- Python 3.10+
- 一个**已登录**的 B站 账号（建议用小号，见 [风险与合规](#风险与合规)）
- 一个 QQ 机器人框架（NapCat / SnowLuma 等能接 AstrBot 的都行）

### 1. 装插件

```bash
# 把两个插件目录放进 AstrBot 的插件目录
cp -r plugins/astrbot_plugin_bili_whale      /opt/astrbot/data/plugins/
cp -r plugins/astrbot_plugin_whale_journal   /opt/astrbot/data/plugins/

# 依赖（bilibili-api 要用新版本，老版本缺接口）
/opt/astrbot/.venv/bin/pip install -r /opt/astrbot/data/plugins/astrbot_plugin_bili_whale/requirements.txt
```

重启 AstrBot，日志里出现这些就算活了：

```
[bili-whale] 已就绪｜运行 0 轮｜今天已分享 0 条｜记着 0 个看过的视频
[bili-whale] 人设来源：AstrBot 默认人格「你的大肥鱼」（N 字）
[bili-whale] 调度器已启动：时段 3 段，收件箱每 10 分钟
```

### 2. 准备 B站 凭证

浏览器登录 B站 → F12 → Application → Cookies，抄三个值：

```json
// data/bili_whale/credentials.json
{ "sessdata": "xxx", "bili_jct": "yyy", "buvid3": "zzz" }
```

`bili_jct` 是 CSRF token，没有它评论/收藏/点赞全报 `-111`。

### 3. 填配置

面板 → 插件 → 刷B站的大肥鱼 → 插件配置，或者直接改
`data/config/astrbot_plugin_bili_whale_config.json`。最少要填这几个：

| 键 | 说明 |
| --- | --- |
| `owner_qq` | 你的 QQ 号（私聊推送对象） |
| `group_ids` | 要分享到的群号，如 `["123456"]` |
| `owner_bili_mid` | 你 B站 的 mid（她评论 @ 的人、私信对端） |
| `self_bili_mid` | 她自己的 B站 mid |
| `bili_credential_file` | 上面那个 credentials.json 路径 |
| `llm_provider_id` | 用的模型，如 `deepseek/deepseek-chat` |
| `persona_file` | 留空 = 用 AstrBot 面板人格（**推荐留空**，见 [人格](#人格跟着面板走不要自己维护)） |

> 默认配置里所有 ID 都是 `0` / 占位值，**必须自己填**，不然她不知道跟谁互动。

### 4. 跑起来

```bash
# 面板里重载插件，或
systemctl restart astrbot
```

调度默认是「三段随机」：`07:00~09:00 ×2`、`12:00~14:00 ×2`、`18:00~22:00 ×3`，
同窗口内至少隔 90 分钟，每天 05:00 重新排当天的行程，看起来像人而不是闹钟。

---

## 打分台（watchui）

她要按你的口味挑视频，你就得能告诉她「哪条好、哪条别再推了」。
`watchui.py` 是个零依赖（只用标准库）的小 HTTP 服务，渲染的是一张**观看历史墙**：

- 按天分组的目录（左边「按天看」，手机上变成横向滑动条）；
- 每条：封面、标题、她给的分数、主题、BV 号、「推给我过」标记；
- 你打 1~5 星 + 写两句评价 → 直接落盘回她的 `state.json`；
- 点「收藏」把 5 星的挑出来，单独一个收藏页；
- 每条下面的「▶ 去B站打开」直达视频（手机拉起 B站 客户端）。


### 部署

```bash
mkdir -p /opt/bili-watch-ui && cp watchui/watchui.py /opt/bili-watch-ui/
# 生成口令（访问要带 ?t=<token>）
python3 -c "import secrets;print(secrets.token_urlsafe(12))" > /opt/bili-watch-ui/token.txt
```

`/etc/systemd/system/qqbot-watchui.service`：

```ini
[Unit]
Description=Bili Watch UI
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/bili-watch-ui
Environment=WATCHUI_DATA=/opt/astrbot/data/bili_whale
Environment=WATCHUI_TOKEN_FILE=/opt/bili-watch-ui/token.txt
Environment=WATCHUI_HOST=127.0.0.1
Environment=WATCHUI_PORT=18900
ExecStart=/usr/bin/python3 /opt/bili-watch-ui/watchui.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now qqbot-watchui
```

**只绑 127.0.0.1**，别直接对公网开。手机上想看，两条路：

1. **SSH 隧道**（推荐，零成本）：`ssh -N -L 18900:127.0.0.1:18900 user@server`，
   然后手机浏览器开 `http://127.0.0.1:18900/watch?t=<token>`（手机得能连到那台机器，比如同局域网 / Tailscale）；
2. **临时隧道**：`cloudflared tunnel --url http://127.0.0.1:18900`，把给的域名 + `?t=<token>` 发给自己。

### 和插件接上

插件配置里：

| 键 | 说明 |
| --- | --- |
| `watchui_url` | `http://127.0.0.1:18900`，用于把口令链接拼好发给主人 |
| `watchui_public_url` | 如果有公网/隧道地址，填这里，推送时优先用它 |
| `watchui_token_file` | 口令文件路径 |
| `daily_push_hour` | 每天几点推送（默认 21:00） |
| `daily_push_link` / `daily_push_send_file` | 发链接 / 发 HTML 文件 / 都发 |

到点她会给你发一条：`主人，今天又看了 N 条，累计 M 条。这条链接点开就能打分：…`
HTML 快照和在线版是同一套渲染，所以离线文件也有目录、封面和跳转链接。

---

## 日记本（whale_journal）

每天 4 次（`09:30 / 13:30 / 18:30 / 22:30`），她回看这段时间：
跟你聊了什么、B站 看了什么、心情如何、认了什么错，写成一篇第一人称日记，
带 `心情：xxx` 标签，存成 `data/whale_journal/journal/<日期>.md`，同时渲染一份 HTML 本子。

```
21:45
心情 · 有点得意
今天最有意思的是群里那句「接几条大肥鱼一起聊」……
看了
· 《xxx》
· 《yyy》
```

配置里能改时间点、每篇条数上限、要不要写「私人日记」（不给主人看的那种）。

---

## 人格：跟着面板走，不要自己维护

最早版本让插件自己读 `persona_file`，结果你在面板里改了人格，B站 上的她还是旧设定——
因为插件缓存了自己那份。现在统一改成：

```python
# 每轮取一次，取不到才退回默认文案
persona = await plugin.context.persona_manager.get_default_persona_v3(umo)
plugin.context.persona_manager.invalidate_persona_cache()   # 改完人格立刻生效
```

**结论**：人设只在 AstrBot 面板里写，别在插件配置里重复一份。

---

## 配置速查

<details>
<summary>展开完整键表（87 个，常用的在这）</summary>

### 调度
| 键 | 默认 | 说明 |
| --- | --- | --- |
| `day_random` | `false` | 按三段窗口随机排班 |
| `day_windows` | 3 段 | `[{"start":"07:00","end":"09:00","count":2}, …]` |
| `day_slot_gap_min` | `90` | 同窗口内两轮最小间隔（分钟） |
| `plan_cron` | `0 5 * * *` | 每天重排行程 |
| `night_cron` / `night_runs` | `0 19 * * *` / 3 | 老的「夜场」模式，保留兼容 |

### 选题
| 键 | 默认 | 说明 |
| --- | --- | --- |
| `min_score` | `15` | 低于这个分数不发 |
| `max_videos` | `5` | 每轮候选池里挑几条 |
| `keywords` | `[]` | 主题偏好；`""` 表示全站裸刷，`"数码"` 表示只刷这个主题 |
| `feedback_boost` / `feedback_penalty` | `3.0` / `5.0` | 你打高分/嫌弃对选题的加权 |

### 互动
| 键 | 默认 | 说明 |
| --- | --- | --- |
| `share_via` | `comment` | `comment` 只在评论区 @ 主人；`dm` 私信；`both` 都发 |
| `comment_probability` | `0.45` | 评论降频（避免触发风控） |
| `comment_strip_links` | `true` | 评论里去掉链接（外链容易被折叠） |
| `like_enable` | `true` | 点赞 |
| `fav_enable` / `fav_min_score` | `true` / `15` | 收藏，以及多少分以上才收藏 |
| `coin_enable` / `coin_daily_max` / `coin_min_stars` | `true` / `1` / `1` | 每天给「你打星最高」的那条投币 |

### 收件箱 / 私信
| 键 | 默认 | 说明 |
| --- | --- | --- |
| `inbox_interval_min` | `10` | 收件箱轮询间隔 |
| `at_listen_enable` / `dm_listen_enable` | `true` | 监听评论 @ / 私信 |
| `dm_auto_reply` | `true` | 主人私信要不要自动回 |
| `dm_video_review` | `true` | 主人私信甩来视频 → 她看完回一段感受 |
| `dm_reply_stranger` | `false` | 陌生人私信要不要回（默认只记不答） |
| `dm_report_to_qq` | `false` | 私信往来要不要复述到 QQ（默认关：你自己在 B站 看得见） |

### 推送 / 打分台
`watchui_url`、`watchui_public_url`、`watchui_token_file`、`daily_push_hour`、`daily_push_link`、`daily_push_send_file`、`watch_page_limit`

</details>

---

## 架构

```
main.py                 插件入口：注册命令 / LLM 工具 / 消息钩子 / 收件箱与调度启动
core/
  config.py            配置模型（DEFAULTS + @property，schema 缺失也能跑）
  bili.py              B站 客户端：推荐流 / 搜索 / 评论 / @ / 私信 / 点赞 / 收藏 / 投币 / 收件箱解析
  llm.py               LLM 门面：选题打分、评论正文、私信对话、视频观感、记忆块
  pipeline.py          一条龙：选题 → 互动 → 分享 → 写日记 → 汇报（含收件箱 _handle_dm）
  scheduler.py         多时段随机排班 + 收件箱轮询 + 重启保护
  store.py             状态持久化（state.json）：看过的视频、已读消息、打分反馈、投币记录
  sender.py            往 QQ 私聊/群发文字与图片（umo 解析 + 失败降级）
  report.py            渲染打分台 / 观看历史 HTML
  peer.py              认「别的大肥鱼」：昵称签名 + 头像 + 关键词三重判定
  diary.py             日记生成与存储
```

一次「刷视频」的完整链路：

```
排班触发
  → 拉首页推荐 24 条 + 主题搜索
  → 过滤（看过的 / 你自己发的 / 太短的）
  → LLM 打分（含你的历史评价作为偏好）
  → 选最高分
  → 评论（限频、去链接、@主人，root+parent 回复）
  → 点赞 / 收藏 / 投币（投币失败 → 当天跳过 + 请你代投一次）
  → 私信主人 + 发群（封面 + 标题 + 链接）
  → 写日记
  → 汇报到 QQ 私聊
```

---

## 踩坑清单（那些真的会把你熬到凌晨的点）

### B站 接口

- `Api.update_data() / update_params()` **只接受 kwargs**，传 dict 直接 `TypeError`。
- `Api(url, method, credential=...)` **只有 `verify=True` 才会自动带 csrf**；不加就 `-111`。
- `Video.set_favorite()` / `Video.pay_coin()` 在这个版本里会 `KeyError: 'data'`，**别用**，直接打：
  - 收藏：`POST https://api.bilibili.com/x/v3/fav/resource/deal`（`rid=aid, type=2, add_media_ids=<fid>`）
  - 投币：`POST https://api.bilibili.com/x/web-interface/coin/add`（`bvid, multiply=1`）
- 收藏接口返回 `success_num: 0` **不代表失败**——很可能早就在收藏夹里了，要用
  `/x/v2/fav/video/favoured?aid=` 复核，否则会把「已收藏」当失败反复重试。
- `x/v3/fav/folder/created/list-all` 返回的已经是剥掉外层 `data` 的本体。
- **私信列表是倒序**（新→旧）！`fetch_session_msgs` 的 `messages[0]` 是最新的，
  但如果你习惯性 `[-1]` 取尾部，就会一直对着最老的私信回复——我们在这上面排查了一整轮。
- 评论里**别放外链**：容易被折叠或 `-412`，先 `strip_links()` 再发。
- 评论 @ 人要用 `root` + `parent` 参数回应某条评论，只写 `@名字` 是纯文本，不会真的通知。

### 投币：机房 IP 会被人机验证

服务器上一投就是：

```json
{"code": -401, "message": "非法访问",
 "data": {"ga_data": {"decisions": ["verify_captcha_level2"], "origin_scene": "video_coin"}}}
```

`wbi=True` / `wbi2=True` / `json_body=True` 全试过，绕不过去；点赞、评论、私信、收藏都不受影响。
所以代码里的降级是：**当天标记 blocked，然后只在 QQ 上打扰你一次**：

> 主人，今天挑出来《xxx》想投一枚币（你给它打了 5 星），结果 B站 让我做人机验证，我投不了……你帮我投一个呗

想要自动投币，得让出口 IP 是家宽/普通网络。

### AstrBot 调度

- **重启会吞掉当天剩下的轮次**。夜场模式用 `DateTrigger` 一个个排，进程一重启
  `_night_token += 1`，所有已排的 job 直接作废，日志打「夜场已被重置」；
  而 `CronTrigger` **当天不再补**，只能等明天。排查「某天没推」先看 `systemctl show -p ExecMainStartTimestamp`。
  这就是现在改成「每天 05:00 重排 + 多时段随机」的原因。
- 升级插件后第一次扫描会把**历史 @ / 私信全部当新消息**回一遍。加了 `inbox_inited` 标记：
  首次只打标不回，之后才正常回。
- 去重键别拼两遍前缀：`is_handled(kind, key)` 内部自己会加 `kind:`，
  再自己拼一层会存成 `at:at:123`，老记录永远对不上 → 重复回复。
- 私信的 `from_me` 判断依赖 `self._self_info['mid']`，新实例还没拉过 self_info 时是空的，
  会把**自己发出去的**私信当成别人发的推给你——记得 `my_mid = own_mid or cfg.self_bili_mid` 兜底。

### 人格与状态

- 插件自己维护 `persona_file` 一定会和面板人格脱钩（我们踩过，用户在面板改了设定，B站 上还是旧的）。
  读 `persona_manager` 并每轮 `invalidate_persona_cache()`。
- `state.json` 的时间戳有时是数字、有时是字符串，解析一定要兼容两种，别直接 `float()`。
- AstrBot 的 `data_v4.db` 有 WAL，**直接 `cp` 会丢最后一段聊天**，备份要用 SQLite 在线 backup API。

### QQ 侧

- 想在 QQ 里发「视频卡片」：`aiocqhttp` 的 `OneBot` 消息段拼 `[CQ:json,...]` 容易被打回，
  我们最后用的是：**封面图 + 标题 + 链接** 的普通消息，稳且能点开。
- B站 **私信**发不出可点的视频卡片（`msg_type=7` 接口不收），只能发链接或纯图。
  所以现在的分工是：推视频走 QQ，B站 私信只做双向对话。
- 群里分享记得判断 `group_share_enable`，别把测试消息刷到别人的群。

---

## 风险与合规

- **用小号**。自动化操作违反 B站 用户协议，理论上存在被限制的风险：
  建议低频（默认每轮至少隔 90 分钟、每天 3~7 轮）、评论去链接、失败即停不重试。
- 我们实际跑了几十轮的结果：点赞/评论/私信/收藏正常；
  投币因为机房 IP 被人机验证（见上），家用宽带可能没事。
- 内容由 LLM 生成，**请自己配置 `min_score` 和 `keywords`**，别让它乱推。
- 本项目仅供学习与个人使用，因使用产生的任何后果由使用者自负。

## 界面预览

仓库里没放截图（不想传 1 MB 的 png）：本地跑起来就是上面描述的那种卡片墙，
深色背景 + 封面大图，比任何截图都好判断。想看效果直接把 `watchui.py` 跑起来打开 `/watch`。

## License

MIT
