# Always-on Claude Agent

部署在腾讯云轻量应用服务器上的单用户助手。通过 SSH 提交任务、设定计划、审批跟进和查看结果。systemd 负责开机启动与异常重启，PostgreSQL 持久化任务与 checkpoint。队列空闲时，worker 每两秒检查一次数据库，Claude API 用量保持为零。

## 在腾讯云安装

安装包面向采用 systemd 的 Ubuntu 和 Debian，建议使用 Ubuntu 22.04、24.04 或 Debian 12。默认在同一台机器安装 PostgreSQL，并通过 Unix socket 和 peer 认证连接。SSH 是操作入口，数据库沿用发行版默认的本地监听设置。实例需要能访问 `api.anthropic.com:443`，并符合 Anthropic 的地区政策。 [腾讯云 Linux 实例登录指引](https://cloud.tencent.com/document/product/1207/47147/) · [Anthropic 支持地区](https://www.anthropic.com/legal/supported-regions)

先把 `always-on-agent.tar.gz` 下载到本机。在终端进入下载目录，把 `SERVER_IP` 换成实例公网 IP，把 `ubuntu` 换成镜像对应的 SSH 用户。

```bash
scp always-on-agent.tar.gz ubuntu@SERVER_IP:~/
ssh ubuntu@SERVER_IP
```

登录服务器后安装。

```bash
tar -xzf always-on-agent.tar.gz
cd always-on-agent
sudo bash install.sh
sudoedit /etc/alwayson/anthropic_api_key
sudo systemctl enable --now alwayson
sudo systemctl status alwayson --no-pager
```

密钥文件里只填 API 密钥本身，一行即可。systemd 通过 `LoadCredential` 把它交给 worker。安装脚本会创建专用系统用户、数据库角色和数据库。重新安装会保留已有密钥、配置和数据。升级后用 `sudo systemctl restart alwayson` 加载新版本。

## 跑通第一条任务

```bash
sudo alwayson ask --session demo '帮我列一份明天的三步工作计划。'
sudo alwayson events --session demo
sudo alwayson outbox --session demo
```

`ask` 返回 `event_id`。结果生成后会出现在 `outbox` 中。以下示例假设对应的 ID 是 `1`，操作时替换为实际值。

| 命令 | 用途 |
| --- | --- |
| `sudo alwayson show 1` | 查看任务状态、回执，以及未完成任务的 checkpoint 轮数 |
| `sudo alwayson ask --session demo '把刚才的第二步拆细。'` | 在同一会话继续 |
| `sudo alwayson outbox --session demo --after 1` | 按 ID 增量读取回执，适合轮询 |
| `sudo alwayson instructions '默认用简体中文，给出下一步。'` | 保存每次运行都加载的长期指令 |
| `sudo alwayson status` | 查看队列状态、待审批数和当日 API 用量 |
| `sudo alwayson --help` | 查看完整命令 |

对脚本提交的任务，可以传 `ask --key TASK_KEY`。同一去重键和相同任务返回原事件 ID。多行输入可以把文本管道传给 `ask -`。

## 定时任务与审批

```bash
sudo alwayson schedule --session demo --in 1m '根据当前会话的计划，提醒我下一步。'
sudo alwayson schedule --session daily --in 1h --every 1d '根据该会话已有信息，列出需要我处理的事项。'
sudo alwayson timers
sudo alwayson approvals
```

也可以用 `--at 2026-12-01T09:00:00+08:00` 指定带时区的开始时间。请换成未来的时间。周期按固定间隔执行，停机期间错过的多个周期合并为最多一次补跑。同一周期任务最多保留一条待运行事件。用 `cancel-timer ID` 可以取消未来触发和已入队但尚未运行的事件。

可用下面的任务体验审批。

```bash
sudo alwayson ask --session demo '请提议在两分钟后提醒我回顾这份计划。'
sudo alwayson approvals
```

查看实际参数后，用 `sudo alwayson approve APPROVAL_ID` 批准，或用 `sudo alwayson reject APPROVAL_ID` 拒绝。批准的跟进从批准时开始计算延时。批准或拒绝都会进入同一会话的事件队列。长期记忆也使用这套审批流程。用 `memory` 查看已保存条目，用 `forget KEY` 删除一条。

## PostgreSQL 怎么保存状态

| Anthropic 原文组件 | 本项目实现 |
| --- | --- |
| Event inbox | 持久化事件状态与去重键。同一个数据库只运行一个 worker，各会话按队列顺序处理。 |
| Timers | 调度器将到期动作写入队列，并在同一事务内更新定时器。 |
| Memory | 保存长期指令、已批准记忆、会话进度和近期对话。Claude 可以按 key 读取记忆。 |
| Permissions | 内置工具只能读记忆、暂存会话进度、提议已限定范围的记忆写入或跟进，以及结束本次运行。特定审批 ID 绑定实际参数。 |
| Outbox | 将回执、审批请求和错误持久化为可按 ID 读取的记录，定时任务也可静默完成。 |

`checkpoints.state` 是 PostgreSQL 中的 JSONB 字段。每轮工具调用后，worker 保存 Claude 消息、工具结果、已完成轮数及暂存动作。进程重启后从已提交的 checkpoint 继续。完成时，进度、审批、回执和任务状态在一个事务内提交，该任务的临时 checkpoint 随之清理。失败任务保留 checkpoint，便于重试。

如果进程在 Anthropic 收到请求后、checkpoint 提交前中断，该轮请求可能重发并再次计费。本地已提交的回执和审批不会因此重复写入。 [Anthropic《Building always-on agents》](https://claude.com/resources/articles/building-always-on-agents) · [Claude 工具调用协议](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)

## 查看日志、限制用量和备份

```bash
sudo journalctl -u alwayson -n 80 --no-pager
sudoedit /etc/alwayson/alwayson.conf
sudo systemctl restart alwayson
```

默认模型为 `claude-sonnet-5-5`。可在配置中修改模型和 API 版本。单个任务默认最多调用 Claude 6 轮，每轮最多输出 1,536 tokens。每日最多尝试 100 次 API 请求，按 UTC 日计算，到上限后排队等到下一个 UTC 日。此设置限制请求次数，API 费用以 Anthropic 账单为准。网络错误和限流会退避重试，持续失败会写入发件箱。 [Claude 模型列表](https://platform.claude.com/docs/en/models/overview) · [Messages API](https://platform.claude.com/docs/en/api/messages/create)

用 `sudo alwayson retry EVENT_ID` 让失败任务从 checkpoint 继续。改了模型、轮数或输出上限后，如需用新配置从第一轮重跑，请加 `--fresh`。用 `sudo alwayson cancel EVENT_ID` 取消排队中或失败的任务。

备份 PostgreSQL 中的队列、会话、记忆、审批、回执和 checkpoint。

```bash
umask 077
sudo -u postgres pg_dump --format=custom alwayson > alwayson.dump
```

任务内容、近期历史和相关记忆会随请求发送给 Anthropic。使用时请把密钥留在单独的凭据文件中。
