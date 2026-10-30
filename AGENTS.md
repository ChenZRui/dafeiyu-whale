# AGENTS.md —— 给 AI 助手的安装说明

> 这份文件是写给「被用户要求帮忙装这个项目」的 AI 助手看的。
> 你（助手）要在一台已经有 AstrBot 的机器上，把这个「大肥鱼 B站分身」装起来。
> 请按顺序做，每一步都先确认再做，**不要跳过验证**。

## 0. 先搞清楚这是什么

两个 AstrBot 插件 + 一个可选的网页打分台：

| 组件 | 作用 | 必需吗 |
| --- | --- | --- |
| `plugins/astrbot_plugin_bili_whale` | 让 bot 自己刷 B站、评论 @ 主人、私信、汇报到 QQ | 必需 |
| `plugins/astrbot_plugin_whale_journal` | 每天多次写日记（读聊天记录 + B站 记录） | 可选但推荐 |
| `watchui/watchui.py` | 观看历史打分台（主人给她打星，影响选题） | 可选 |

一句话：**先说清楚「必须向用户要哪些信息」，再动手装。**

## 1. 开工前必须向用户确认的信息

不要自己猜，也不要沿用 README 里的占位值（那些都是 `0` / `占位`）。逐项问：

1. **AstrBot 装在哪**（包含 `main.py` 的那层目录，常见 `/opt/astrbot`）。
2. **用 systemd 跑吗？服务名是什么**（常见 `astrbot` / `qqbot-astrbot`；`systemctl list-units | grep -i astrbot` 查）。
3. **主人的 QQ 号**（私聊推送对象）。
4. **要分享到的群号**（可以留空 = 不发群）。
5. **主人的 B站 mid**：让他打开自己的 B站 个人空间，地址形如
   `https://space.bilibili.com/413116249`，那串数字就是 mid。
6. **bot 自己那个 B站 号的 mid**：同理，用 bot 的小号登录后看空间地址。
7. **用哪个模型**（AstrBot 面板里 provider 的 id，形如 `deepseek/deepseek-chat`）。
8. **B站 凭证**：这一步没法代劳，必须用户自己去浏览器拿（见第 4 步）。
9. 问一句：**「这是小号吗？」** 如果不是小号，明确提醒他机器人自动操作 B站 有账号风险，建议用小号。

## 2. 前置检查（不满足就别往下走）

```bash
# AstrBot 目录里要有 main.py
ls "$ASTRBOT_DIR/main.py"

# Python 能跑（优先用 AstrBot 自带的 venv）
"$ASTRBOT_DIR/venv/bin/python" -c 'import sys;print(sys.version)'

# AstrBot 版本 >= 4.29（低版本缺 persona_manager 接口，插件会报错）
grep -r "__version__" "$ASTRBOT_DIR"/astrbot/__init__.py 2>/dev/null | head -3
```

- AstrBot < 4.29：先让用户升级 AstrBot，**不要**硬装。
- 没有 venv：确认用户的 AstrBot 是怎么跑的，用同一个解释器装依赖。
- 网络：GitHub 直连在国内经常超时，一律用镜像前缀 `https://gh-proxy.com/`。

## 3. 装（推荐走一键脚本）

**Linux / macOS（有一键脚本就用它，别手搓）**

```bash
cd /tmp
curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/ChenZRui/dafeiyu-whale/main/deploy/install.sh -o install.sh
bash -n install.sh          # 先做语法检查（拿到别人给的不明脚本时永远这样做）
sudo ASTRBOT_DIR=/opt/astrbot \
     OWNER_QQ=<QQ号> \
     GROUP_IDS=<群号,逗号分隔,可空> \
     OWNER_MID=<主人mid> \
     SELF_MID=<bot的mid> \
     LLM_PROVIDER=<provider id> \
     SERVICE=<systemd服务名> \
     bash install.sh -y
```

- 用户想要打分台：再加 `--with-watchui`（需要 root，会注册 `qqbot-watchui` 服务并打印访问口令）。
- AstrBot 不是 systemd 跑的：加 `SKIP_RESTART=1`，最后自己安排重启。
- 依赖装不上：`PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple`（脚本默认已经是清华源，失败会自动回退官方源）。

**Windows**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\install.ps1 `
  -AstrBotDir "D:\path\to\AstrBot" -OwnerQQ <QQ号> -GroupIds "111,222" `
  -OwnerMid <主人mid> -SelfMid <bot的mid> -LlmProvider <provider id>
```

> `.ps1` 必须是 **UTF-8 with BOM**，否则 Windows PowerShell 5.1 会按 ANSI 解码，中文字符串被截断成语法错误。

**装完脚本自己会做**：备份已存在的同名插件目录（`*.bak-<时间戳>`）→ 拷插件 → 装 pip 依赖 → 写
`data/config/*.json` → 生成 B站 凭证占位文件（`chmod 600`）→ 重启服务。

**脚本不会替你做的三件事**（必须在第 4、5 步补上）：填 B站 凭证、核对 ID、确认重启后日志正常。

## 4. B站 凭证（必须用户自己做）

告诉用户：浏览器登录 B站 → F12 → Application/存储 → Cookies → 抄三个值：

```json
// $ASTRBOT_DIR/data/bili_whale/credentials.json
{ "sessdata": "...", "bili_jct": "...", "buvid3": "..." }
```

- `bili_jct` 是 CSRF token，**少了它评论/点赞/收藏全报 `-111`**。
- 这个文件是密钥级敏感内容：`chmod 600`，**绝对不要**写进 git、日志或聊天记录里转述。
- 别让用户把 SESSDATA 贴到公开场合；粘贴完提醒他这份凭据等同于账号登录态。

## 5. 装完必须验证（缺一条都算没装好）

```bash
# a) 服务在跑
systemctl is-active <服务名>

# b) 插件加载成功（关键日志）
journalctl -u <服务名> --no-pager | tail -300 | grep -E "bili-whale|whale-journal"

# 期望看到类似：
#   [bili-whale] B站账号就绪：<昵称>(<mid>)
#   [bili-whale] 人设来源：AstrBot 默认人格「xxx」（N 字）
#   [bili-whale] 调度器已启动：时段 3 段，收件箱每 10 分钟
#   [bili-whale] 已就绪｜运行 0 轮｜今天已分享 0 条｜记着 0 个看过的视频

# c) 配置文件里的 ID 真的是用户的（不是 0）
grep -E "owner_qq|group_ids|owner_bili_mid|self_bili_mid" \
     "$ASTRBOT_DIR/data/config/astrbot_plugin_bili_whale_config.json"

# d) 打分台（如果装了）
curl -s -o /dev/null -w "%{http_code}\n" \
     "http://127.0.0.1:18900/watch?t=$(cat /opt/bili-watch-ui/token.txt)"   # 期望 200
```

再给用户一句话验证法：**在 QQ 里给 bot 发 `/刷B站`**（或面板里重载插件后等一次调度），
看它有没有挑视频、有没有私聊汇报；B站 那边看它有没有评论/点赞。

## 6. 常见失败与处理

| 现象 | 原因 / 处理 |
| --- | --- |
| 日志 `读人设文件失败 / File name too long` | 配置里 `persona_file` 被填成了人设正文。清空它 = 用 AstrBot 面板人格 |
| 日志 `B站账号就绪` 之后立刻 `B站凭证无效` | SESSDATA 过期或抄错；重新取一次 |
| 评论报 `-111` | `bili_jct` 缺失/错误 |
| 评论报 `-412` / 被折叠 | 评论里带外链，确认 `comment_strip_links=true`、`comment_probability` 别调太高 |
| 投币报 `-401 非法访问` | 服务器机房 IP 触发人机验证，**绕不过去**。这是已知限制，代码会降级成「QQ 请主人代投」，如实告诉用户 |
| 私信不回 | 确认 `dm_auto_reply=true`、`owner_bili_mid` 对；另外确认 AstrBot 版本 ≥ 4.29 |
| 某天没推送 | 别急着改代码。先看服务是不是当天被重启过（重启会作废当天已排的轮次），以及 `night_cron` / `day_windows` 时间段 |
| `pip install` 卡住 | 换清华源；`bilibili-api-python` 尽量 >= 17.4 |
| 面板里插件显示「没有 README 文件」 | 无害提示，忽略 |
| 插件目录名被改过 | 插件目录名必须等于 `metadata.yaml` 里的 `name`，否则 AstrBot 不加载 |

## 7. 边界（重要）

- **改动用户的现有配置前先备份**（`cp xxx.json xxx.json.bak-<时间戳>`），脚本已做，手工改也要做。
- **重启服务会打断正在进行的对话/任务**。动手前告诉用户一声；别在用户正聊天时重启。
- **不要**把 B站 凭证、QQ 号、mid 写进提交、issue、截图或对外分享的文件。
- **不要**替用户做「自动答题转正」「刷量」「批量关注」这类操作。
- 用户如果只想「先看看效果」，可以只装 `astrbot_plugin_bili_whale`，
  把 `share_via` 设成 `comment`、`group_share_enable=false`，先跑一两轮再放开。
- 插件行为都可以在 `data/config/astrbot_plugin_bili_whale_config.json` 里关掉：
  `like_enable` / `fav_enable` / `coin_enable` / `comment_probability=0` / `dm_auto_reply=false`。

## 8. 卸载 / 回滚

```bash
# 停用插件：配置里 enable=false 并重启，或直接把目录挪走
mv "$ASTRBOT_DIR/data/plugins/astrbot_plugin_bili_whale"  /tmp/
mv "$ASTRBOT_DIR/data/plugins/astrbot_plugin_whale_journal" /tmp/
systemctl restart <服务名>

# 恢复旧版本：脚本装之前留了 *.bak-<时间戳>，改回原名即可
# 打分台：systemctl disable --now qqbot-watchui
```

数据目录（`data/bili_whale`、`data/whale_journal`）是纯本地状态，删掉等于清空历史记录，删之前问用户。
