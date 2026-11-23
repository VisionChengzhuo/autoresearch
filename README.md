# AutoResearch 循环脚本

`ar.py` 默认管理 `~/autoresearch/campaigns`。用 `--root` 或 `AR_ROOT` 隔离测试目录。每个课题独立加锁和保存 Git 检查点。用 `ar tick <课题>` 推进一个课题，省略课题名则扫描所有已初始化的课题。

```sh
ar init demo --config config.json --charter charter.md
ar start demo
ar status demo
```

配置中的 `card` 是现有长期课题卡，`linear_owner_id` 是用于核对状态变更历史的 Linear 用户 ID。 `teams` 以队名为键，每队指定 `family`、可选周期卡时 `gpu_hours` 和队长模型 `captain_model`。`budget` 包含 `daily`、`total`、`concurrency`。`evaluation.script` 和 `evaluation.scenarios` 各包含 `path`、`sha256`。 `owner_log` 默认是 `~/.local/state/ambition/linear-owner-messages.jsonl`，格式为 `id`、`received_at`、`text`，由可信消息接收端写入。

- 训练机启动器先运行 `ar register <课题> <运行JSON>`，等待登记成功再启动实验。记录包含 `id`、`team`、`plan`、`code`、`config`、`seed`、`heartbeat`、`result`、`artifact`、`eta`。GPU 运行还包含 `gpu`、`machine`、`tmux`、`workdir`、`lease_until`，使用已分配的 `slots`。时间可用 UTC Unix 秒或带时区的 ISO 8601 字符串。
- 巡检器为每台机器原子更新 `resources[].snapshot`，快照包含 `time`、`alarm`、`shm_fraction`、`memory_fraction`。训练机的占卡、限额和 95% 终止由训练机脚本处理，本脚本消费告警和实验心跳。配置 `resource_rules_file` 可将资源红线附在章程和派工说明内。
- `ar apply <课题> <评审JSON>` 读取 `summary`、`next` 和按队名索引的 `teams` 决定。大评审可附 `acceptance`，包含 `team`、`plan`、`criteria`、`artifacts`。`ar block <课题> <阻塞JSON>` 读取 `team`、`who`、`what`、`recheck_at`，用 `team` 和 `clear: true` 解除。
- `ar question <课题> <提问JSON>` 读取 `id`、`question`、`options`、`recommendation`、`waiting`。提问卡与课题大卡同项目，登记后同步走飞书；团队、实验和单次运行均没有卡。验收使用课题大卡，兼容 Linear 的 `Human Intervention` 列名。
- `outbox.jsonl` 记录章程、大评审决定、裁决、提问、验收和告警。`ar` 经 `amb align` 把通知交给 amb 的飞书发送通道，只有章程、小裁决及有团队调整的大评审同时评论课题大卡。`outbox_sent` 记录已进入 amb 队列的条数，飞书返回的送达回执在 amb 的独立队列处理。独立 watchdog 在心跳停更超过 10 分钟时也使用同一个发送通道。
- `timing` 可按秒覆盖 `review_earliest`、`review_interval`、`review_late`、`block_escalation`、`ruling_fallback`、`ruling_late`、`progress_min` 和 `progress_max`。未覆盖时，评审间隔 6 小时、阻塞升级 2 小时、大卡无事件最长 2 小时刷新。
- `ar stop <课题> --owner-msg <消息编号>` 核对可信记录中的本人完整原话。请用户单独发送 `停止课题 <课题目录名>` 或 `停止 <课题大卡编号>`。验收可用 Linear 给出的 `同意验收 <课题大卡编号> <申请时间>`，要求该卡的对应申请仍待验收，且记录的 `created_at` 和 `received_at` 晚于申请时间。暂停、附带条件、问句和指向其他课题的原话会被拒绝。Linear 关卡仍走其验收流程。
- 假课题用 `drill: true`。其 amb 单用 `campaign` 路由完成事件，取消挂靠和自动建卡，仅处理本课题的假数据。
- 假课题自测用 `python3 -m unittest discover -s tests -v`。单独重跑恢复演练用 `python3 -m unittest discover -s tests -v -k recovery_drill`。可用 `python3 tests/check_interfaces.py /path/to/linear-app` 离线检查暂存版的进度和验收写入接口。

## 运行记录传输与课题配额

VPS 的 `ar-transfer` 每分钟经同步入口主机（`--host` 或环境变量 `AR_SYNC_HOST`）拉取共享盘上的运行记录。训练进程仍由 `ar-run` 在目标 Pod 的独立 tmux 内管理，SSH 退出不影响训练。共享盘根目录由 `AR_SHARED_ROOT` 指定（默认 `/srv/autoresearch/runs`），keepalive 状态文件由 `AR_KEEPER_STATUS` 指定。传输不会请求 keeper 让卡。支持手工补拉 `~/autoresearch/ar-transfer`。传输的最近成功时间在 `~/autoresearch/transfer/status.json`，cron 错误写到 `transfer.log`。跨机器路径如下。

| 数据 | 共享盘路径 | VPS 路径 |
|---|---|---|
| 心跳、结果 JSON、实验标准输出／错误日志 | `$AR_SHARED_ROOT/<课题>/<运行>/` | `~/autoresearch/transfer/runs/<课题>/<运行>/` |
| 课题 GPU 租约 | `$AR_SHARED_ROOT/../leases/<课题>/<运行>.json` | `~/autoresearch/transfer/leases/<课题>/<运行>.json` |
| 两个 Pod 的 keeper 资源摘要 | 同步入口主机的 keepalive 状态文件 | `~/autoresearch/transfer/resources/{launcher,worker}.json` |

同步按文件增量传输，不删除已收到的文件。VPS 收好结果和日志才发布终态心跳。断线期间，已有传输记录保留，实验不会仅因 VPS 收不到心跳而判失败。连通恢复、源端心跳持续过期时，登记为基础设施中断。有 GPU 租约的运行至少保留卡位到租约结束。训练日志留在受限目录，账本记录日志路径。模型、数据和大型检查点仍留在机房共享盘。

GPU 运行用 `ar register <课题> <运行JSON>` 登记。对 H200 使用 `transport: "h200"`，由 `ar` 生成本地心跳、结果和日志路径。GPU 用 `gpu_slots` 数组，旧的单卡 `gpu` 键仍可使用。H200 槽位需要配置物理 `index`、Pod `machine`（主机名）和团队。登记给出同一 Pod 的 `machine`、绝对 `workdir`、`tmux`（`ar-<课题>-<运行>`）和带时区的 `lease_until`。使用资源摘要作 `resources[].snapshot`，把同一 Pod 主机名填进 `resources[].machine`。Pod 重建换主机时更新槽位与资源配置，沿用共享盘检查点，用新运行 ID 续跑。

登记后等待共享盘上的租约收据出现，再在相应 Pod 或正式实验的开发机协调入口运行 `ar-run start`。保留默认 `--state-dir`，传入收据中的 GPU 序号（`--gpus`）、同一个 `--lease-until` 和工作目录。收据中的 UTC 截止时间以 `ar register` 生成的文件为准。轻量运行按占卡规则直接执行。正式运行仍经先前部署的 keeper 协调器和 Pod tmux。训练或评测程序先原子写入 `AUTORESEARCH_RESULT_FILE`（至少包含有限数值 `score`），再以零状态结束。任意评分仍需通过已有评测哈希校验。

`ar status` 的 `quota` 按模型、团队报告会话数和秒数，按团队报告本评审周期的实际卡秒、预留卡秒和上限。模型使用量读取同课题的 amb 父单、子单、重试和评审阶段。使用 amb 的进程秒数，缺失时回退到执行阶段墙钟时间。已排队且未开始的单只计预留。北京时间每日 80% 预警，100% 限速，到次日自动重算。达到总额会在课题项目开一次提问卡。限速时在跑实验照常收尾，队长每天最多一轮消化结果，挡路裁决继续。课题没有预算触发的停机路径。

GPU 登记预留到期前的全部卡秒，同时核对卡位和本队剩余额度。回收时由 `ar-run` 的真实启动／退出时间乘以卡数结算，业务失败、资源终止同样记账，启动前失败为零。中断时按最后已观测心跳估算。评审周期边界会切分实际用量，跨周期的未完成租约仍占用本周期的剩余预留。

`ar status` 的 `quota.gpu_usage` 汇总课题累计的已识别卡数、逐卡用量和实际卡时。在跑的实验计到最近同步的心跳，租约预留仍可在 `quota.gpu` 中查。H200 卡号使用登记时保存的 Pod 主机和索引，旧登记使用当时的槽位名。历史账本仅有卡时、缺卡号的记录单列，卡时仍计入总量。每天北京时间 09:00 后首次生效的大评审随飞书日报和评审文件附上前一自然日的相同用量统计，跨午夜的运行按时间切分。
