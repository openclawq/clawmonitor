# ClawMonitor：当“队列空闲”却没有回复时，我们到底在监控什么？

你有没有遇到过这种令人抓狂的场景：

- 你在 IM 里丢给 Agent 一条任务（可能是 15 分钟的活儿，也可能是 1 小时的长跑）。
- Visual Studio / 管理界面里看队列（Queue）是空的，系统看起来“很安静”。
- 但是你并没有收到任何回复，也不知道它是：
  - 已经干完了但没送达？
  - 还在干，只是没有反馈？
  - 中途被中断了？
  - 或者根本没收到你的消息？

这不是你“焦虑”，这是监控视角缺失：**我们缺少一个以 Session/Thread 为中心的、足够实时的“最后一条消息 + 当前工作状态”面板**。

于是就有了：**ClawMonitor**。

---

## 它解决的痛点（也是难点）

### 1) “最后一条消息是什么？什么时候到的？”

在排查“为什么没反馈”时，最关键的问题永远是：

- 这个 Session/Thread 最近一次收到的用户消息是哪条？时间是多少？
- 最近一次发出的 assistant 消息是哪条？时间是多少？

ClawMonitor 会基于 OpenClaw 的本地状态与 transcript tail，把这两条信息直接摆在你面前。

### 2) “它现在到底算不算在工作？”

对监控来说，“队列空闲”不是答案；你需要的是：

- `WORKING`：正在跑（通常能在 `.jsonl.lock` 看到 run 的 pid/createdAt）
- `FINISHED`：没有 lock，assistant 没落后于 user
- `INTERRUPTED`：`abortedLastRun=true` 且 user 更新但 assistant 没跟上
- `NO_MESSAGE`：根本没看到 user 消息

以及更现实的报警维度：

- `NO_FEEDBACK`：user 比 assistant 新，但也没在跑（最像“卡住/掉回复/送达失败”的那类）
- `DELIVERY_FAILED`：结果可能存在，但 delivery-queue 里发送失败了
- `SAFE/SAFEGUARD_OFF`：掉出 safeguarding/遇到安全拦截的线索（启发式）

### 3) 长任务：跑一小时也要有人看得懂

长任务最怕两种情况：

- 它真的在跑，但你完全不知道它还活着
- 它已经死了，但你被“心跳一小时一次”的反馈拖得团团转

ClawMonitor 把 “runFor” 显示在列表里，且支持手动刷新和调整刷新间隔。

### 4) Feishu/Telegram “看起来没问题，但就是没回复”

一些常见的“卡住类问题”并不是模型不努力，而是：

- Polling stall / proxy/NO_PROXY 配置不对（Telegram）
- channel health restart / stale-socket 重启（Feishu）
- queuedFinal=false / replies=0（跑完了但没排队最终回复）
- delivery 失败（有结果但没送达）

ClawMonitor 可以从 Gateway 的 logs.tail 与 channels.status 里抓取线索，给出诊断提示。

---

## 安装

```bash
cd ~/program/clawmonitor
python3 -m pip install -e .
```

---

## 第一次运行：初始化配置（推荐）

```bash
clawmonitor init
```

它会帮你生成 `~/.config/clawmonitor/config.toml`，并让你确认 OpenClaw 的状态目录（通常是 `~/.openclaw`）。

如果你直接运行 `clawmonitor tui/status` 且没有配置文件，ClawMonitor 也会在交互式终端里提示你进行初始化。

---

## 使用方式

### 1) 全屏监控（推荐）

```bash
clawmonitor tui
```

- 列表行会用颜色区分健康度：`OK` 绿 / `RUN` 青 / `IDLE` 黄 / `ALERT` 红
- 按 `?` 可弹出详细帮助
- 按 `Enter` 可以手动发送一条 “请汇报进度” 的 nudge（chat.send）
- 如果 session 太多，按 `x` 可切换 Focus 模式，只看“正在跑/需要关注/最近活跃/你标注过的”
- 遇到飞书 `ou_...` 这类 id，按 `R` 可直接在 TUI 里起名字（写入配置 `[labels]`）

### 2) 命令行输出（适合脚本/CI）

```bash
clawmonitor status
clawmonitor status --format json
clawmonitor status --format md
```

### 3) 导出单个 Session 的诊断报告（适合发给同事）

```bash
clawmonitor report --session-key 'agent:main:main' --format both
```

默认会落盘到：

- `~/.local/state/clawmonitor/reports/`

（JSON + Markdown 两份，便于机器处理和人阅读）

---

## 最后：分享与安全

ClawMonitor 会对 token-like 字符串进行 redaction，但这不等于“百分百可公开”：

- 报告里可能仍包含业务内容、用户 id、群 id、内部路径等信息
- **对外分享前请人工检查**（尤其是截图、md 报告和相关日志）

---

如果你也经历过“队列空闲但我心不空闲”，欢迎试试 ClawMonitor。
它的目标不是面面俱到，而是把最关键的两个问题做成“抬眼就能看到答案”。  

近期更新文章：

- `docs/wechat-update-0.1.6.md`
