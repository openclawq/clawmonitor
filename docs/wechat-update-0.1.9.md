# ClawMonitor 0.1.9：现在不只盯 Session，也能直接盯模型了

如果你最近在用 OpenClaw，大概率遇到过这种情况：

- `openclaw.json` 里配了多个模型，主模型、次选模型、fallback 都很完整
- 结果真实运行时，第一个模型卡住，后面的起不来
- 或者 API 本身其实通，但走 OpenClaw 的链路时又堵住了
- 你盯着 Session 看半天，还是无法迅速回答一句话：
  **到底是哪一个模型坏了，坏在 API 侧，还是坏在 OpenClaw 侧？**

这就是 ClawMonitor 0.1.9 这次要解决的问题。

---

## 这次更新的核心：模型监控

从 0.1.9 开始，ClawMonitor 不再只看 Session，也开始看模型本身。

它会把 OpenClaw 里实际生效的模型链解析出来，然后按 `agent + modelRef` 做监控。

也就是说，你现在不仅能看到：

- 哪个 Session 正在跑
- 哪个 Session 没反馈

还可以直接看到：

- 主模型现在通不通
- fallback 模型现在通不通
- 这个问题发生在 provider/API 侧，还是 OpenClaw 调用链路里

---

## 两种探测，不再只靠一种视角

### 1）直连 provider/API 探测

ClawMonitor 会独立调用 provider 接口，发一个非常短的探测请求。

这样可以知道：

- 返回是否成功
- 延迟大概是多少
- 响应效率怎么样
- 错误更像是：
  - `timeout`
  - `network`
  - `auth`
  - `billing`
  - `rate_limit`
  - `overloaded`
  - `unsupported`
  - `error`

这一步解决的是：**模型本身有没有通。**

### 2）通过 OpenClaw 自己探测

这一步不是直接打 provider API，而是让 OpenClaw 自己跑一遍：

- `sessions.patch`
- `agent`
- `agent.wait`
- 结束后自动清理临时 session

这一步解决的是：**即便 API 本身是通的，OpenClaw 这条链路是不是也真的通。**

很多卡点恰恰就在这里。

---

## 为什么这个功能很重要

以前看到一个 Session 没回复时，你很难第一时间判断：

- 是这个 Session 卡住了
- 是主模型慢到夸张
- 是余额/权限有问题
- 是被限频了
- 还是 direct API 正常，但 OpenClaw 自己这条路堵住了

现在可以直接分开看：

- **Session 视图**：看对话、工作状态、日志、投递
- **Model 视图**：看模型链是否健康

一个看“任务”，一个看“底层能力”。

这两个视角放在一起，排障速度会快很多。

---

## TUI 也更新了：模型视图更明确了

这次顺手把模型视图的交互也补完整了。

### 1）`v` 切换 Session / Model 视图

不用把模型行硬塞进 Session 列表里，界面更清楚。

### 2）模型视图默认手动刷新

切到 Model 视图后，按：

```bash
r
```

才会执行一次模型探测。

原因也很简单：模型探测本身是“主动测试”，不适合像 Session 状态那样一直自动刷。

### 3）现在有显眼状态条了

之前你可能会遇到：

- 看到 “press r to probe”
- 但按了以后不知道到底有没有开始
- 也不知道现在是在跑、跑完了，还是报错了

现在顶部会直接显示：

- `WAITING`
- `RUNNING`
- `DONE`
- `ERROR`

而且在 `RUNNING` 时，会继续显示当前步骤、当前探测消息和耗时。

这意味着你按完 `r` 以后，不会再有“到底按上了没”的不确定感。

### 4）列表太长时可以翻页了

现在支持：

- `PgUp / PgDn`：整页翻
- `g / G`：跳到顶部 / 底部

如果模型或 Session 很多，不用再一行一行慢慢按。

---

## 适合哪些场景

### 场景一：主模型慢，但 fallback 明明是好的

你可以很快看到：

- 主模型是不是高延迟/超时
- fallback 有没有真实可用

### 场景二：API 能通，但 OpenClaw 调用不通

你会看到：

- direct probe 是 `OK`
- OpenClaw probe 却是 `TIMEOUT` 或 `ERROR`

这种情况下，问题就不在 provider，而在 OpenClaw 链路或本地运行环境。

### 场景三：余额、权限、限频问题

很多报错以前只能在日志里慢慢看，现在会直接归类出来。

这对多 provider、多模型链尤其有用。

---

## 命令行也能直接用

```bash
clawmonitor models
clawmonitor models --mode direct --format json
clawmonitor models --mode openclaw --timeout 20
```

你可以只跑 direct，只跑 OpenClaw，或者两种一起跑。

---

## 我们想保持的设计原则

ClawMonitor 不是想做一个“什么都展示一点”的观察面板。

它更想做的是：

- 当你怀疑“为什么没反馈”时，能最快缩小问题范围
- 当你配了多个模型链时，能最快找出到底是哪一层坏了

所以 0.1.9 的重点不是“多一个表格”，而是把排障视角补齐：

- **Session 是否健康**
- **模型是否健康**
- **Provider 路径是否健康**
- **OpenClaw 路径是否健康**

这样你看到问题时，不会只剩“感觉卡住了”这一个模糊结论。

---

## 安装 / 更新

```bash
pip install -U clawmonitor
```

启动：

```bash
clawmonitor tui
```

切换到模型视图：

- `v`

执行一次模型探测：

- `r`

翻页：

- `PgUp / PgDn`
- `g / G`

---

如果你最近也正好被 “主模型挂了但 fallback 没顶上” 这种问题反复折腾，这一版应该会比之前更接近一个真正能用的排障面板。
