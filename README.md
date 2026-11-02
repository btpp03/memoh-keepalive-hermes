# memoh-keepalive-hermes

Memoh Cloud（`app.memoh.net`）免费 bot 工作区里那枚哪吒探针（面板 `nezhak2.btpp.ggff.net`，节点 id=14 `memho`）的看护腿。

## 为什么需要

Memoh 的 bot 容器会不定时重启（平台侧回收 / 重新调度）。容器起来后，`/data/nezha/boot.sh` 这条 60 秒看门狗**不会自动运行** —— 它只是一个 `nohup` 进程，容器重启即消失。结果是：哪吒面板上 `memho` 的 `lastActive` 冻结，节点显示掉线，直到有人进容器手动拉起。

## 它做什么（每 10 分钟一次）

1. 用 btpp04 账号登录 Memoh，拿会话 cookie + active team id
2. 读 `/api/memoh/bots/<id>/container` —— 容器没在跑就 `POST .../container/start`
3. 走容器终端 WebSocket 检查 `pgrep -f '[n]ezha/agent -s'`
4. 没有进程 → `setsid nohup bash /data/nezha/boot.sh`，等 25 秒复查一次
5. 复查仍失败 → 非零退出（Actions 里会标红）

## 检测为什么要绕

容器终端 WS 会把敲进去的命令原样回显，所以「输出里含 `AGENT_UP`」永远为真（假阳性——我第一版就踩了这个坑，明明 agent 已经被杀，脚本仍报 healthy）。

现在的做法：检查命令只往 `/tmp/nz.state` 写 `UP`/`DOWN` + `DONE`，第二步 `cat /tmp/nz.state` 读回来判定。命令里用 `'[n]ezha/agent -s'` 也是为了让 `pgrep` 自己的参数不匹配到自己。

## 定时

GitHub Actions 原生 `schedule` 在 btpp03 账号上基本不触发，所以**只用 cron-job.org 外部腿**每 10 分钟 `workflow_dispatch`：

```
POST https://api.github.com/repos/btpp03/memoh-keepalive-hermes/actions/workflows/keepalive.yml/dispatches
body: {"ref":"main"}
header: Authorization: token <PAT>
```

（`push` 也会触发一次，方便改完立刻验证。）

## Secrets

| 名字 | 值 |
|---|---|
| `MEMOH_USER` | `btpp04` |
| `MEMOH_PASS` | Memoh 账号密码 |
| `MEMOH_BOT_ID` | bot uuid（btpp04bot） |
| `MEMOH_TEAM_ID` | team uuid（slug `btpp04`） |

仓库是 public，但 secret 加密，fork 和普通读取都看不到。

## 本机对照测试

```bash
MEMOH_USER=btpp04 MEMOH_PASS='...' MEMOH_BOT_ID=... MEMOH_TEAM_ID=... python3 keepalive.py
```

实测（2026-11-02 北京 11:47）：容器 running、agent 先被杀成 DOWN → 脚本拉起 boot.sh → 25 秒后复查 UP；面板 `id=14 lastActive` 随即刷新到 0.0 min。

## 已知限制

- AGENT 起来后还要等哪吒 agent 首次上报（`--report-delay 4`），面板刷新有几十秒延迟。
- 如果容器整个被平台删掉（不是停），`container/start` 救不回来，需要重建 bot 再装一次 agent。
- 掉线到修复的空窗 = 最长 10 分钟（cron-job.org 间隔）。
