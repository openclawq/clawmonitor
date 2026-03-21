# ClawMonitor 0.2.0：从“看 Session”升级到真正的 OpenClaw 运维观察台

如果你最近一直在用 OpenClaw，你大概率已经不是只想回答一个问题了：

- 这个 Session 现在有没有在跑？

你更想知道的是：

- 是不是模型本身挂了？
- fallback 为什么没接住？
- 这个 Session 之前到底干过什么？
- 最近 1 天 / 7 天 / 30 天，哪个 agent / session 最烧 token？
- openclaw-gateway.service 里是不是已经堆了很多 helper、zombie 或孤儿进程？

换句话说，问题已经不只是“某个对话没回复”，而是：

**OpenClaw 这整套运行面，现在到底健康不健康？**

这就是 ClawMonitor 0.2.0 想解决的事。

---

## ClawMonitor 现在主要是什么

ClawMonitor 不是聊天 UI，也不是日志浏览器。

它现在更明确地定位成一个：

**面向 OpenClaw 运维和排障的键盘优先监控台**

它把原来分散在 Session、模型、Gateway、systemd、usage 这些层面的信息，尽量统一成一个可以快速切换、快速定位问题的 TUI。

你现在可以把它理解成 4 个互相补位的观察面：

1. **Session**
   - 这个任务现在在不在跑
   - 最后一条 user / assistant 消息是什么
   - 有没有 silent working、delivery failure、no feedback

2. **Model**
   - 配置里的模型链到底通不通
   - 是 provider API 挂了，还是走 OpenClaw 的链路卡住了

3. **Token**
   - 当前 session 的 token 压力如何
   - 最近 1d / 7d / 30d 哪些 session 最耗 token

4. **System**
   - gateway service 本身是否健康
   - cgroup 里有没有残留 helper
   - 有没有 zombie / orphan
   - 如果后续人工清理或干净重启，大概能回收多少内存

这四层组合起来，ClawMonitor 才开始像一个真正的 OpenClaw 观察台，而不只是一个“Session 列表”。

---

## 从 0.1.9 到 0.2.0，这次升级了什么

### 1）任务历史终于能看了

之前你能看到当前状态，但很难快速回答：

- 这个 session 过去一天到底做了哪些任务？
- 哪些已经 done，哪些还在 doing？
- 是不是刚好卡在某个阶段？

现在在 Session 视图里，可以按需读取历史任务列表。

这个设计不是默认一直自动扫，因为有些 session 很重、历史很多，强行后台一直刷只会拖慢整体体验。

所以现在的策略是：

- 用户选中某个 session
- 主动触发读取
- UI 明确显示当前正在读取、已经读取完成还是失败
- 读取结果缓存成 JSON，后续尽量断点续扫，避免重复重读

这让历史任务列表真正开始可用，而不是“理论上能做，但一读就卡很久”。

---

### 2）Token 监控补上了

这个需求其实很关键。

因为很多时候一个 session 看起来“没坏”，但它其实已经：

- context 很深
- input 很高
- output 很少
- cache read / write 很异常
- 或者最近几天成本明显偏高

现在 ClawMonitor 会同时展示两种 token 视角：

#### 当前快照

来自本地 session 状态文件，适合看：

- 当前 session 正在用哪个 model/provider
- input / output / cache read / cache write
- context 使用压力

#### 时间窗口统计

来自 Gateway usage，适合看：

- `1d`
- `7d`
- `30d`

这意味着你不只知道“这个 session 现在多大”，还知道“最近谁最烧 token”。

对于多 agent、多 cron、多长任务环境，这个差别很大。

---

### 3）System 视图上线了

这是这次最重要的新能力之一。

现实里很多 OpenClaw 的问题，并不是 session 或 model 单独出错，而是 gateway service 周边已经开始变脏：

- Playwright / Chrome helper 残留
- ssh-agent 等辅助进程越来越多
- zombie 进程存在
- orphan 进程越来越多
- `KillMode` 不理想
- cgroup 里积了很多本该被清掉的东西

这些问题光看 Session，是看不出来的。

所以现在有了独立的 `System` 顶层视图，用来回答：

- `openclaw-gateway.service` 现在是不是 healthy
- 当前 cgroup 里到底有多少活进程
- 哪些 family 风险更高
- 这些潜在有问题的进程大概占了多少 RSS
- 如果后续人工做一次干净重启/清理，可能释放多少内存

这里特别强调一点：

**ClawMonitor 仍然是只读观察，不替你 kill，也不替你 restart。**

它负责把风险和收益说清楚，再由运维或用户决定要不要执行清理。

---

### 4）Operator Note 让 System 不只是“看不懂的一堆数字”

纯展示 service / cgroup / RSS / zombie 数量还不够。

真正的问题是：看到这些数字以后，用户该怎么理解？

所以现在在 System 视图里可以直接打开英文版 `Operator Note`。

它会结合当前快照，给出更像 runbook 的说明：

- 现在风险大概在哪里
- 为什么这些 helper / orphan 值得关注
- 如果后续人工做清理，可能回收多少内存
- 为什么 `KillMode=control-group` 更适合
- 重启前后建议看哪些点

这样用户看到的就不是一堆缩写，而是一段可执行的判断逻辑。

---

### 5）TUI 交互整体成熟了很多

这次其实还做了很多“平时不容易单独发文章，但用起来差别很大”的改动。

比如：

- `v` 现在不只是 Session / Model 二选一，而是轮转 `Sessions / Models / System`
- `s` 可以直接进 `System`
- `z` 可以切不同 pane 宽度
- `Z` 可以切详情全屏
- `Esc` 统一回默认界面，再按才退出
- `?` 现在更偏当前视图帮助，再按一次才看总帮助
- 帮助、history、operator note 都支持翻页
- 状态栏会更明确显示当前是 `WAITING / RUNNING / READY / ERROR`
- 正在读取 history、token usage、model probe、system snapshot 时，会有更明显的运行提示

这些改动的目标很明确：

**不要让用户再出现“我到底按上了没、它到底在不在跑、现在卡在哪一层”的不确定感。**

---

### 6）模型监控不再只是“能跑”，而是更像真正的探测

从 0.1.9 开始已经有模型监控了，但这次配合 TUI 体验和整体观察面，模型视图更像一个成熟功能了。

现在它的价值更明确：

- 直连 provider/API 探测
- 通过 OpenClaw 自身链路探测
- 并行探测多个模型
- UI 明确展示正在运行和耗时变化

也就是说，当模型很多、fallback 很多时，你不会再只得到一个模糊的“好像有点慢”，而是更快知道：

- 哪个模型不通
- 哪种方式不通
- 问题更像 timeout / auth / billing / rate limit / overloaded / error 中的哪一类

---

## 这次版本更适合哪些人

如果你只是偶尔本地跑一下 OpenClaw，0.2.0 的变化可能不会立刻全部打中你。

但如果你符合下面任意一种情况，这次升级会明显更有价值：

- 你配置了多模型链和 fallback
- 你有多个 agent、多个 channel、多个 cron
- 你经常遇到“看起来没回复，但也不知道卡在哪”
- 你已经碰到过 helper / zombie / service 脏状态导致主进程异常
- 你开始关心 token 消耗和时间窗口使用情况

它的重点不是做一个更花的界面，而是把 OpenClaw 运维真正关心的几个观察面补齐。

---

## 一句话总结这次升级

如果说 0.1.9 主要是让 ClawMonitor 从“只看 Session”走向“也能看模型”；

那么 0.2.0 更像是把它正式推进到了：

**一个面向 OpenClaw 运行、排障和运维判断的综合监控台。**

---

## 安装 / 更新

```bash
pip install -U clawmonitor
```

启动：

```bash
clawmonitor tui
```

常用操作：

- `v`
  - 轮转 `Sessions / Models / System`
- `s`
  - 直接进入 `System`
- `h`
  - Session 右侧切换 `Status / History`
- `u`
  - 切换 token window：`now / 1d / 7d / 30d`
- `r`
  - 刷新当前视图
- `z`
  - 切换 pane 宽度
- `Z`
  - 详情全屏
- `o`
  - 在 `System` 视图打开 operator note
- `PgUp / PgDn`
  - 翻页
- `g / G`
  - 跳到顶部 / 底部

---

如果你现在已经把 OpenClaw 跑成一个长期在线、多 agent、多模型、多任务的系统，那 ClawMonitor 0.2.0 会比之前更接近一个真正能帮你做判断的工具，而不是只给你多看几行状态。
