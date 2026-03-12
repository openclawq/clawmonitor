# ClawMonitor

[English](README.md) | 简体中文

用于 **OpenClaw** 的实时 Session/Thread 监控工具，核心能力：

- 每个 Session 的最后一条 **user** / **assistant** 消息（预览 + 时间）
- 工作状态：`WORKING` / `FINISHED` / `INTERRUPTED` / `NO_MESSAGE`（并标记 `NO_FEEDBACK`）
- 通过 `*.jsonl.lock` 识别长任务运行中状态（即使 Gateway 掉线也能判断）
- 可选：Gateway 的 `logs.tail` + `channels.status` 关联（偏 Feishu/Telegram 的卡住类诊断）
- 全屏 TUI + 手动 “nudge”（通过 `chat.send` 让它继续汇报进度）

## 安装（editable）

```bash
cd ~/program/clawmonitor
python3 -m pip install -e .
```

## 安装（PyPI / uv / pipx）

如果发布到 PyPI，安装会更简单：

```bash
pip install clawmonitor
# 或
pipx install clawmonitor
# 或
uv tool install clawmonitor
```

## 运行

推荐先初始化配置：

```bash
clawmonitor init
```

然后启动 TUI：

```bash
clawmonitor tui
```

其他命令：

```bash
clawmonitor snapshot --format json
clawmonitor snapshot --format md
clawmonitor nudge --session-key 'agent:main:main' --template progress
clawmonitor nudge --session-key 'agent:main:main' --template continue
clawmonitor push --session-key 'agent:main:main' --dry-run
clawmonitor status
clawmonitor status --format json
clawmonitor status --format md
clawmonitor report --session-key 'agent:main:main' --format both
clawmonitor watch --interval 1
```

## 配置

默认配置路径：

- `~/.config/clawmonitor/config.toml`

示例配置：`config.example.toml`

运行时数据（不会写进本仓库）：

- 事件日志：`~/.local/state/clawmonitor/events.jsonl`
- 报告导出：`~/.local/state/clawmonitor/reports/`
- 缓存：`~/.cache/clawmonitor/`

## TUI 快捷键

- `↑/↓`：切换选中 session
- `Enter`：对选中 session 发送 nudge（选择模板）
- `?`：显示帮助说明
- `l`：切换 Related Logs 面板
- `d`：重新计算诊断（强制刷新）
- `e`：导出该 session 的脱敏报告（JSON+MD）
- `r`：立即刷新
- `f`：切换刷新间隔（最长 10 分钟）
- `q`：退出

若终端支持颜色，行会按健康度着色：`OK` 绿 / `RUN` 青 / `IDLE` 黄 / `ALERT` 红。

## Telegram 说明：ACP 线程绑定（thread bindings）

OpenClaw 可能会把某个 Telegram chat 路由到另一个 sessionKey（例如 ACP session），导致你以为 “main 不收消息了”。

ClawMonitor 会做提示：

- `clawmonitor status`：`BOUND_OTHER`
- TUI 列表：`BIND`

相关文件/开关：

- 线程绑定：`~/.openclaw/telegram/thread-bindings-default.json`
- 配置开关：`~/.openclaw/openclaw.json` → `channels.telegram.threadBindings.spawnAcpSessions`

## 首次运行提示

如果配置文件不存在，`clawmonitor tui/status/watch/...` 在交互式终端下会提示你运行初始化向导（非交互环境不会卡住）。

更多介绍见：`docs/launch-post.md`。
