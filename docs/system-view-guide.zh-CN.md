# System 视角速读

更新时间：2026-03-20

这个文档只回答一个问题：`clawmonitor tui` 里的 `System` 视角现在怎么看。

## 顶部两行

第一行是 `SYSTEM SNAPSHOT` 状态条：

- `WAITING`
  - 还没取到快照
- `LOADING`
  - 正在读取 `systemctl + ps + /proc/cgroup`
- `READY`
  - 当前快照可用
- `ERROR`
  - 本次刷新失败；如果之前有旧快照，会继续显示旧快照

这一行还会显示：

- `sampleAge`
  - 当前快照距离现在多久
- `reclaim~`
  - 估算可释放内存
- `zombies`
  - 僵尸进程数
- `helpers`
  - helper/子进程数
- `risk`
  - 总体风险级别

第二行是简短汇总：

- `Svc`
  - service 当前状态
- `KillMode`
  - systemd 杀进程策略
- `MainPID`
  - 主进程 pid
- `Tasks`
  - 当前 task 数
- `Mem`
  - service 级内存
- `Procs`
  - 本次纳入监控的 cgroup 进程数
- `Prob`
  - 当前判定为“潜在有问题”的进程数
- `Reclaim`
  - 估算可释放内存

如果其中一些值超过默认阈值，会更醒目：

- `Prob >= 1`
  - 开始偏黄
- `Prob >= 5`
  - 更接近红色高风险提示
- `Reclaim >= 256MB`
  - 开始偏黄
- `Reclaim >= 1GB`
  - 高风险强调更明显
- `ORPH > 0`
  - 直接按高风险看
- `Z >= 1`
  - 提醒
- `Z >= 3`
  - 更强风险提示

## 左栏怎么读

左栏第一行不是 family，而是总览行：

- `SERVICE`
  - 整个 `openclaw-gateway.service` 的汇总

下面每一行才是 family：

- `chrome/playwright`
- `ssh-agent`
- `qmd`
- `node`
- `other`

列含义：

- `FAMILY`
  - 进程家族/分组
- `RISK`
  - `OK / WARN / ALERT`
- `PROC`
  - 该组进程数量
- `RSS`
  - 存活进程的内存总和
- `CPU`
  - 该组 CPU 总占用
- `Z`
  - 僵尸进程数
- `ORPH`
  - 孤儿进程数，通常指 `PPID=1`
- `RECL`
  - 估算可回收 RSS

其中 `RECL` 不是精确值，只是保守估算：

- 如果后续人工清理这些“潜在有问题的进程”，大概可能释放多少内存

## 右栏怎么读

右栏现在分成固定分区：

- `SERVICE`
  - service 名称、状态、`KillMode`、总体风险、快照年龄
- `RESOURCES`
  - `MainPID`、`TasksCurrent`、`MemoryCurrent`、`CPUUsageNSec`、`CGroup`
- `COUNTS`
  - `procs / helpers / zombies / orphans / problematic / reclaim`
- `ISSUES`
  - 当前为什么是 `WARN` 或 `ALERT`
- `MONITOR EVENTS`
  - monitor 自己最近做过的动作
  - 例如：刷新 system、读取 history、加载 token usage、跑 model probe
- `PROCESS DETAIL`
  - 如果左边选中 `SERVICE`，这里显示最值得关注的进程
  - 如果左边选中某个 family，这里显示该 family 的详细进程列表

进程表里常见字段：

- `PID`
  - 进程 id
- `STAT`
  - 进程状态，`Z` 通常表示 zombie
- `RSS`
  - 单进程内存
- `CPU`
  - 单进程 CPU
- `REL`
  - 跟 service 的关系
  - 常见有 `main`、`service-child`、`orphan`
- `CMD`
  - 启动命令预览

## 快捷键

- `s`
  - 直接进入 `System`
- `v`
  - 在 `Sessions / Models / System` 三个顶层视角间切换
- `r`
  - 立即刷新
- `z`
  - 切换布局
  - `10/90`
    - 左边 10%，右边 90%
  - `50/50`
    - 左右平分
  - `left100`
    - 只看左栏
  - `right100`
    - 只看右栏
- `Esc`
  - 退回默认界面；再按一次退出
- `o`
  - 打开英文版 operator note / runbook note
  - 内容会结合当前快照，说明：
  - 如果后续人工做一次“干净重启/清理”，大概可能释放多少内存
  - 为什么 `KillMode=control-group` 更合适
  - 重启前后建议检查哪些命令
  - 弹窗里可以翻页：
  - `j/k` 或方向键上下滚动
  - `PgUp/PgDn`、空格翻整页
  - `g/G` 跳到顶部/底部
- `?`
  - 第一次优先显示当前 `System` 视角帮助
  - 在帮助界面里再按一次 `?` 才切到总说明书

## 颜色怎么理解

- 绿色
  - 正常
- 青色
  - 正在运行、读取中、当前活跃
- 黄色
  - 需要留意，但未必已经严重异常
- 红色
  - 明显风险，通常包括：
  - service 不健康
  - `KillMode` 不理想
  - 有 orphan helper
  - 僵尸较多
  - 可回收内存估算较大
- 洋红色
  - monitor 自己的动作、事件、任务轨迹
- 蓝色
  - 分区标题、结构性标签

底部状态栏第二行现在也用了 badge：

- `READY`
  - 最近一次刷新成功
- `LOADING / RUNNING`
  - 当前正在拉数据或探测
- `ERROR`
  - 最近一次操作失败
- `STALE`
  - 有缓存，但可能不是最新

## 当前设计边界

这一版只做观察，不做操作：

- 不会自动 kill 进程
- 不会自动 restart gateway
- 不会直接替用户做清理

它的目标是先把问题看清楚，再让运维/用户决定后续动作。
