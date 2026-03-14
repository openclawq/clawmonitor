# ClawMonitor 0.1.6：当 Session 多到像海鲜市场，怎么只盯住“该盯的那几只”？

如果你用 OpenClaw 跑过一阵子，大概率见过这类名场面：

- 队列看起来是空的（系统很安静）
- 你却没收到任何回复
- 你开始怀疑：它是**跑完了没送达**？**跑着但不吭声**？**中断了**？还是**压根没收到**？

这不是你焦虑，这是监控视角缺失：你缺一个围绕 Session/Thread 的、足够实时的“最后一条消息 + 当前工作状态”面板。

ClawMonitor 的目标一直没变：**把最关键的两个问题，做成抬眼就能看见答案**。

---

## 它解决的核心痛点

### 1) 最关键的两条消息：最后一次 user / 最后一次 assistant

排查“为什么没反馈”，本质上就是对齐这两条时间线：

- 这个 Session 最近一次“真实用户输入”是什么？什么时候到的？
- 最近一次 assistant 输出是什么？什么时候发的？

只要这两条对不上，就能很快判断是：

- `NO_FEEDBACK`：用户更新了，但 assistant 没跟上（而且也不在跑）
- `DELIVERY_FAILED`：可能有结果，但投递队列失败
- `WORKING`：还在跑（锁文件/ACPX 给你证据）

### 2) “Session 太多”才是新问题

OpenClaw 的 sessionKey 很容易变成“生物多样性展示”：

- 主会话、心跳会话、channel 会话、ACP 会话、subagent 会话
- 再加上 cron jobs（可能还有 run 记录）

当你一屏几十条 session 时，你真正关心的通常只有：

- 正在跑的
- 需要关注的（投递失败 / pending reply / 线程绑错 / safeguard 掉了）
- 最近活跃的
- 你自己标注过名字的（否则 `ou_...` 看到眼花）

---

## 0.1.6 这次更新了什么？

### 1) Focus 模式：一键把“无聊 session”收起来

TUI 新增 `x`（Focus filter）：

- `focus`：只显示“值得盯”的 session（WORKING/ALERT/最近活跃/你标注过的/投递失败等）
- `all`：显示全部

底部会显示 `sessions=shown/total`，你能直观看到过滤效果。

### 2) 直接在 TUI 里改名字：R 一键给 `ou_...` 起外号

TUI 新增 `R`（Rename/label）：

- 对当前选中的 session 写入/清除 label
- label 会落盘到你的 `config.toml` 里的 `[labels]` 段

从此：

- `agent:main:feishu:...:ou_...` 可以变成 `数学题`
- `agent:main:telegram:...:8561...` 可以变成 `我的电报私聊`

### 3) Cron 更“可见”

如果你有 cron jobs：

- TUI 树里可以展开 cron job 列表（并显示最近运行状态）
- CLI 新增 `clawmonitor cron`：一条命令列出 jobs + last run

### 4) 看起来更清爽

- Related Logs 标题更醒目
- 左侧 FLAGS 更紧凑，给 SESSION 留出更多空间
- `?` 帮助里补全了 STATE/FLAGS 缩写含义（不用记忆体背诵）

---

## 最推荐的使用姿势

1) 先初始化：

```bash
clawmonitor init
```

2) 打开 TUI：

```bash
clawmonitor tui
```

3) session 多了就按：

- `x`：Focus（只看该看的）
- `R`：给 opaque id 起名字
- `?`：随时查快捷键和缩写解释

---

ClawMonitor 继续坚持一个原则：**不做“全平台监控大而全”，只做“排障最关键的那两条线索 + 可解释的状态”。**

如果你最近也在经历“队列空闲但我心不空闲”，希望这次更新能让你少走几圈排障回环。  

---

## 获取与安装（Git / PyPI / ClawHub Skill）

项目地址（GitHub）：

- `https://github.com/openclawq/clawmonitor`

### 方式 1：PyPI（推荐）

```bash
pip install -U clawmonitor
clawmonitor init
clawmonitor tui
```

### 方式 2：Git 安装（适合想改代码/提 PR）

```bash
git clone https://github.com/openclawq/clawmonitor
cd clawmonitor
python3 -m pip install -e .
clawmonitor tui
```

### 方式 3：ClawHub Skill（OpenClaw 生态）

如果你在 ClawHub 里通过 “Import from GitHub” 导入该 repo，会自动识别 `skills/claw-monitor/SKILL.md`。
更详细说明见仓库内：

- `docs/clawhub-skill.md`
