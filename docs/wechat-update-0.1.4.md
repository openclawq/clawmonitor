说明：这篇是 **0.1.4** 的历史更新文章。最新版本更新见：`docs/wechat-update-0.1.6.md`。

---

# ClawMonitor v0.1.4：终于能“看懂”你的 OpenClaw 在忙啥了

如果你也遇到过这种场景：

- VS 里看 Queue 是空的，但你不确定 **之前的任务到底跑完没有**
- Agent 跑了 15 分钟就结束了，但你却要等 1 小时心跳才“回魂”
- Telegram/Feishu 明明收到了消息，却感觉 **main session 不动了**（其实可能被路由到别的 session）

那恭喜，你跟我一样：不是你焦虑，是可观测性真的不够。

这一版（`0.1.4`）是一次“体感升级”的更新：**信息更聚合、更好扫一眼就懂**。

---

## 这次解决了什么痛点？

### 1) Session 列表终于像“结构图”了（tree view）

以前你看到一堆 sessionKey，像在读一段加密文本。

现在 `clawmonitor tui` 左侧默认开启 tree view：

- 先按 **agent 分组**
- 再把 `acp/subagent` 这类“衍生会话”用缩进展示
- 同时把列表里的 `SESSION` 显示为 **sessionKey 的 tail**（减少被窗口宽度截断的痛苦）

你不需要先理解 OpenClaw 的 sessionKey 语法，才能判断“这堆东西哪一个才是主线”。

快捷键：

- `t`：tree / flat 一键切换

### 2) 右侧状态面板：Task / Thinking / Trigger 直接“高亮提示”

当 session 正在跑长任务时，最想知道两件事：

- 它现在在忙什么（Task/Trigger）？
- 它最近在想什么（Thinking）？有没有卡住/报错？

这版把 `Task:` / `Thinking:` / `Trigger:` 行在右侧状态区做了颜色高亮（终端支持颜色时为 **magenta**），让你“扫一眼就抓住重点”。

### 3) 快捷键提示更清楚

`?` 会弹出更完整的帮助说明，包括左侧列表的列含义与截断提示。

---

## 如何升级 / 安装

### pip（推荐）

```bash
pip install -U clawmonitor
```

### 开发版（editable）

```bash
cd ~/program/clawmonitor
python3 -m pip install -e .
```

---

## 怎么用（最短路径）

首次运行建议先初始化：

```bash
clawmonitor init
```

然后打开 TUI：

```bash
clawmonitor tui
```

常用按键：

- `↑/↓`：选中不同 session
- `Enter`：给当前 session 发一个 nudge（用模板）
- `r`：手动刷新
- `f`：切换刷新间隔（最长 10 分钟）
- `t`：tree/flat 切换
- `?`：帮助说明

---

## 一句真心话：它不是“代替心跳”，而是“把真相从文件里捞出来”

ClawMonitor 的思路是：**不等心跳**，直接读取本地状态（sessions/transcript/locks/delivery queue），再用 Gateway 的日志/状态做增强。

所以你能得到更及时的答案：

- “它是不是在跑？”（lock 在不在）
- “它最后一次收到的用户消息是什么？”（Last User Send）
- “它最后一次发出去的 assistant 消息是什么？”（Last Claw Send）
- “它是不是被 ACP/thread binding 路由走了？”（BIND/BOUND_OTHER 提示）

---

## 结尾：把焦虑变成一个面板

以前：你盯着空 Queue，心里默念“它应该跑完了吧？”

现在：你盯着 TUI，心里默念“嗯，它确实跑完了。”

欢迎试用、提需求、提 PR —— 只要别让我再盯着空 Queue 猜谜语。
<<<<<<< HEAD

=======
>>>>>>> dev
