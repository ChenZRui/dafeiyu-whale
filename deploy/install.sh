#!/usr/bin/env bash
# 大肥鱼 B站分身 —— 一键安装脚本
#
# 用法（最简单）：
#   curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/ChenZRui/dafeiyu-whale/main/deploy/install.sh -o install.sh
#   sudo bash install.sh
#
# 常用参数：
#   -y, --yes            不提问，全部用环境变量/默认值
#       --with-watchui   顺便装打分台（watchui，systemd 服务，需要 root）
#       --local DIR      不从网上下载，直接从本地仓库目录装（开发用）
#       --dry-run        只打印要做什么，不落盘
#   -h, --help           看帮助
#
# 可用环境变量（非交互模式必填）：
#   ASTRBOT_DIR=/opt/astrbot        AstrBot 安装目录（含 main.py）
#   OWNER_QQ=123456                 你的 QQ
#   GROUP_IDS=111,222               要分享的群（逗号分隔，可留空）
#   OWNER_MID=413000000             你的 B站 mid
#   SELF_MID=3747570000000000       大肥鱼自己的 B站 mid
#   LLM_PROVIDER=deepseek/deepseek-chat
#   CRED_FILE=data/bili_whale/credentials.json
#   MIRROR=https://gh-proxy.com/    GitHub 加速前缀（国内建议保留）
#   PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
#   SERVICE=astrbot                 systemd 服务名（AutoDL/裸跑 可设空）
#   SKIP_DEPS=1 SKIP_RESTART=1      跳过装依赖 / 跳过重启
set -euo pipefail

REPO="ChenZRui/dafeiyu-whale"
BRANCH="main"
MIRROR="${MIRROR:-https://gh-proxy.com/}"
PIP_INDEX="${PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
ASTRBOT_DIR="${ASTRBOT_DIR:-/opt/astrbot}"
SERVICE="${SERVICE-astrbot}"

ASSUME_YES=0
WITH_WATCHUI=0
LOCAL_DIR=""
DRY=0

c_info() { printf '\033[36m[大肥鱼]\033[0m %s\n' "$*"; }
c_ok()   { printf '\033[32m[  OK  ]\033[0m %s\n' "$*"; }
c_warn() { printf '\033[33m[注意!]\033[0m %s\n' "$*"; }
c_err()  { printf '\033[31m[出错!]\033[0m %s\n' "$*" >&2; }
die() { c_err "$*"; exit 1; }

usage() { sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) ASSUME_YES=1 ;;
    --with-watchui) WITH_WATCHUI=1 ;;
    --local) shift; LOCAL_DIR="${1:-}" ;;
    --local=*) LOCAL_DIR="${1#*=}" ;;
    --dry-run) DRY=1 ;;
    -h|--help) usage ;;
    *) die "不认识的参数：$1（-h 看帮助）" ;;
  esac
  shift
done

run() {
  if [ "$DRY" = 1 ]; then printf '  [dry] %s\n' "$*"; else "$@"; fi
}

ask() { # ask <变量名> <提示> <默认值>
  local __var="$1" __prompt="$2" __def="${3:-}" __ans=""
  if [ "$ASSUME_YES" = 1 ] || [ ! -t 0 ]; then
    printf -v "$__var" '%s' "$__def"; return 0
  fi
  printf '\033[36m?\033[0m %s' "$__prompt"
  [ -n "$__def" ] && printf ' [%s]' "$__def"
  printf ': '
  read -r __ans || true
  [ -z "$__ans" ] && __ans="$__def"
  printf -v "$__var" '%s' "$__ans"
}

banner() {
  cat <<'EOF'

  ┌──────────────────────────────────────────┐
  │   大肥鱼 · B站分身  一键安装              │
  │   刷B站 / 评论@你 / 私信 / 日记 / 打分台  │
  └──────────────────────────────────────────┘

EOF
}

# ---------- 1. 找 AstrBot ----------
detect_astrbot() {
  if [ -f "$ASTRBOT_DIR/main.py" ]; then return 0; fi
  for d in /opt/astrbot /root/astrbot /usr/local/astrbot "$HOME/astrbot"; do
    if [ -f "$d/main.py" ]; then ASTRBOT_DIR="$d"; return 0; fi
  done
  die "没找到 AstrBot（$ASTRBOT_DIR/main.py 不存在）。用 ASTRBOT_DIR=/你的/路径 指定。"
}

py_works() { "$1" -c 'print(1)' >/dev/null 2>&1; }

detect_python() {
  local cand
  for cand in "$ASTRBOT_DIR/venv/bin/python" "$ASTRBOT_DIR/.venv/bin/python" \
              "$ASTRBOT_DIR/venv/bin/python3" "$(command -v python3 || true)" "$(command -v python || true)"; do
    [ -n "$cand" ] || continue
    [ -x "$cand" ] || continue
    if py_works "$cand"; then
      PY="$cand"
      case "$cand" in
        "$ASTRBOT_DIR"/*) ;;
        *) c_warn "没找到 AstrBot 的 venv，退而用 $cand（依赖可能装错地方）" ;;
      esac
      return 0
    fi
  done
  die "没有可用的 python（AstrBot 的 venv/bin/python 也跑不起来）。先确认 AstrBot 本身能启动再装插件。"
}

# ---------- 2. 取源码 ----------
SRC=""
fetch_source() {
  if [ -n "$LOCAL_DIR" ]; then
    [ -d "$LOCAL_DIR/plugins" ] || die "本地目录里没有 plugins/：$LOCAL_DIR"
    SRC="$LOCAL_DIR"
    c_ok "使用本地源码：$SRC"
    return 0
  fi
  local tmp url
  tmp="$(mktemp -d)"
  url="${MIRROR}https://github.com/${REPO}/archive/refs/heads/${BRANCH}.tar.gz"
  c_info "下载源码：$url"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 2 -m 120 "$url" -o "$tmp/src.tar.gz" || die "下载失败（换 MIRROR= 试试）"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -T 120 "$url" -O "$tmp/src.tar.gz" || die "下载失败"
  else
    die "系统里没有 curl 也没有 wget"
  fi
  tar xzf "$tmp/src.tar.gz" -C "$tmp" || die "解压失败，包可能下坏了"
  SRC="$(find "$tmp" -maxdepth 1 -type d -name "${REPO##*/}-*" | head -1)"
  [ -n "$SRC" ] || SRC="$(find "$tmp" -maxdepth 1 -mindepth 1 -type d | head -1)"
  [ -d "$SRC/plugins" ] || die "包内容不对（找不到 plugins/）"
  c_ok "源码就绪：$SRC"
}

# ---------- 3. 装插件 ----------
PLUGIN_DIR=""
install_plugins() {
  local ts dst
  ts="$(date +%Y%m%d%H%M%S)"
  PLUGIN_DIR="$ASTRBOT_DIR/data/plugins"
  run mkdir -p "$PLUGIN_DIR"
  for name in astrbot_plugin_bili_whale astrbot_plugin_whale_journal; do
    dst="$PLUGIN_DIR/$name"
    if [ -d "$dst" ]; then
      c_warn "$name 已存在 → 备份成 $name.bak-$ts"
      run mv "$dst" "$dst.bak-$ts"
    fi
    run cp -r "$SRC/plugins/$name" "$dst"
    c_ok "已安装 $name"
  done
}

install_deps() {
  [ "${SKIP_DEPS:-0}" = 1 ] && { c_warn "SKIP_DEPS=1，跳过依赖安装"; return 0; }
  local req="$PLUGIN_DIR/astrbot_plugin_bili_whale/requirements.txt"
  [ -f "$req" ] || return 0
  c_info "安装 Python 依赖（bilibili-api-python 等）…"
  if run "$PY" -m pip install -q -i "$PIP_INDEX" -r "$req"; then
    c_ok "依赖装好了"
  else
    c_warn "用清华源装失败，换官方源再试一次"
    run "$PY" -m pip install -q -r "$req" || c_warn "依赖没装全，自己跑一遍：$PY -m pip install -r $req"
  fi
}

# ---------- 4. 写配置 ----------
write_config() {
  local cfgdir="$ASTRBOT_DIR/data/config"
  local cfg="$cfgdir/astrbot_plugin_bili_whale_config.json"
  run mkdir -p "$cfgdir"
  if [ -f "$cfg" ]; then
    c_warn "配置已存在，保留不动：$cfg"
    return 0
  fi

  local owner_qq="${OWNER_QQ:-}" group_ids="${GROUP_IDS:-}"
  local owner_mid="${OWNER_MID:-}" self_mid="${SELF_MID:-}"
  local provider="${LLM_PROVIDER:-}" cred="${CRED_FILE:-data/bili_whale/credentials.json}"

  if [ "$ASSUME_YES" = 0 ] && [ -t 0 ]; then
    ask owner_qq   "你的 QQ 号（私聊推送对象）" "$owner_qq"
    ask group_ids  "要分享到的群号（多个用逗号，留空=不发群）" "$group_ids"
    ask owner_mid  "你的 B站 mid（她评论@的人）" "$owner_mid"
    ask self_mid   "大肥鱼自己的 B站 mid" "$self_mid"
    ask provider   "用哪个模型（AstrBot 里的 provider id）" "${provider:-deepseek/deepseek-chat}"
  fi

  run "$PY" - "$SRC/deploy/config.example.json" "$cfg" "$owner_qq" "$group_ids" "$owner_mid" "$self_mid" "$provider" "$cred" <<'PYEOF'
import json, sys
src, dst, qq, groups, omid, smid, provider, cred = sys.argv[1:9]
cfg = json.loads(open(src, encoding="utf-8").read())
if qq.strip():
    cfg["owner_qq"] = qq.strip()
if groups.strip():
    cfg["group_ids"] = [g.strip() for g in groups.replace("，", ",").split(",") if g.strip()]
if omid.strip().isdigit():
    cfg["owner_bili_mid"] = int(omid)
if smid.strip().isdigit():
    cfg["self_bili_mid"] = int(smid)
if provider.strip():
    cfg["llm_provider_id"] = provider.strip()
cfg["bili_credential_file"] = cred
open(dst, "w", encoding="utf-8").write(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
PYEOF
  c_ok "配置已写入 $cfg"
}

write_journal_config() {
  local cfgdir="$ASTRBOT_DIR/data/config"
  local cfg="$cfgdir/astrbot_plugin_whale_journal_config.json"
  [ -f "$cfg" ] && { c_warn "日记配置已存在，保留不动"; return 0; }
  run "$PY" - "$SRC/deploy/config.journal.example.json" "$cfg" "$ASTRBOT_DIR" "${OWNER_QQ:-}" "${LLM_PROVIDER:-}" <<'PYEOF'
import json, sys
src, dst, root, qq, provider = sys.argv[1:6]
cfg = json.loads(open(src, encoding="utf-8").read())
if qq.strip():
    cfg["owner_qq"] = qq.strip()
if provider.strip():
    cfg["llm_provider_id"] = provider.strip()
cfg["journal_dir"] = root.rstrip("/") + "/data/whale_journal"
open(dst, "w", encoding="utf-8").write(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
PYEOF
  c_ok "日记配置已写入"
}

write_credential_template() {
  local cred="${CRED_FILE:-$ASTRBOT_DIR/data/bili_whale/credentials.json}"
  case "$cred" in /*) ;; *) cred="$ASTRBOT_DIR/$cred" ;; esac
  if [ -f "$cred" ]; then c_warn "B站凭证已存在，保留不动"; return 0; fi
  run mkdir -p "$(dirname "$cred")"
  if [ "$DRY" = 0 ]; then
    cat > "$cred" <<'EOF'
{
  "sessdata": "在这里填你的 SESSDATA",
  "bili_jct": "在这里填你的 bili_jct（CSRF）",
  "buvid3": "在这里填你的 buvid3"
}
EOF
    chmod 600 "$cred" 2>/dev/null || true
  fi
  c_warn "B站凭证占位文件：$cred —— 必须填真值，否则她刷不了B站"
}

# ---------- 5. 打分台 ----------
install_watchui() {
  [ "$WITH_WATCHUI" = 1 ] || return 0
  local dir="/opt/bili-watch-ui"
  run mkdir -p "$dir"
  run cp "$SRC/watchui/watchui.py" "$dir/watchui.py"
  run cp "$SRC/watchui/backfill_covers.py" "$dir/backfill_covers.py"
  if [ ! -f "$dir/token.txt" ] && [ "$DRY" = 0 ]; then
    "$PY" -c "import secrets;print(secrets.token_urlsafe(12))" > "$dir/token.txt"
    c_ok "打分台口令：$(cat "$dir/token.txt")（访问要带 ?t=这个）"
  fi
  if [ "$IS_ROOT" = 1 ]; then
    run cp "$SRC/watchui/qqbot-watchui.service" /etc/systemd/system/qqbot-watchui.service
    run sed -i "s#WATCHUI_DATA=/opt/astrbot/data/bili_whale#WATCHUI_DATA=$ASTRBOT_DIR/data/bili_whale#" \
        /etc/systemd/system/qqbot-watchui.service
    run systemctl daemon-reload
    run systemctl enable --now qqbot-watchui
    c_ok "打分台已启动：http://127.0.0.1:18900/watch?t=<上面的口令>"
  else
    c_warn "--with-watchui 需要 root 才能装 systemd 服务，已跳过服务注册（文件已放到 $dir）"
  fi
}

restart_astrbot() {
  [ "${SKIP_RESTART:-0}" = 1 ] && { c_warn "SKIP_RESTART=1，跳过重启"; return 0; }
  [ -z "$SERVICE" ] && { c_warn "没指定服务名，自己重启 AstrBot 生效"; return 0; }
  local unit=""
  for cand in "$SERVICE" "qqbot-$SERVICE" "astrbot"; do
    if systemctl list-unit-files 2>/dev/null | grep -q "^${cand}\.service"; then unit="$cand"; break; fi
  done
  [ -z "$unit" ] && { c_warn "没找到 systemd 服务，手动重启 AstrBot 才生效"; return 0; }
  run systemctl restart "$unit" && c_ok "已重启 $unit"
}

summary() {
  cat <<EOF

────────────────────────────────────────────
安装完成。接下来三件事（缺一不可）：

1. 填 B站 凭证
   $ASTRBOT_DIR/data/bili_whale/credentials.json
   浏览器登录 B站 → F12 → Application → Cookies，抄 sessdata / bili_jct / buvid3

2. 核对配置里的 ID
   $ASTRBOT_DIR/data/config/astrbot_plugin_bili_whale_config.json
   owner_qq / group_ids / owner_bili_mid / self_bili_mid 必须是你自己的

3. 重启 AstrBot，看日志出现下面这行就活了：
   [bili-whale] 已就绪｜运行 0 轮｜今天已分享 0 条｜记着 0 个看过的视频

想让她按你的口味挑视频：装打分台后把 watchui_url 填进配置，
每天 21:00 她会把打分链接发到你 QQ。
────────────────────────────────────────────
EOF
}

main() {
  banner
  IS_ROOT=0; [ "$(id -u 2>/dev/null || echo 1)" = 0 ] && IS_ROOT=1
  [ "$IS_ROOT" = 1 ] || c_warn "不是 root：写 /opt 和 systemd 那几步可能失败"
  detect_astrbot; c_ok "AstrBot：$ASTRBOT_DIR"
  detect_python;  c_ok "Python：$PY"
  fetch_source
  install_plugins
  install_deps
  write_config
  write_journal_config
  write_credential_template
  install_watchui
  restart_astrbot
  summary
}

main "$@"
