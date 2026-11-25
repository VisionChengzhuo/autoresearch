# 资源红线（集群规则归档）

<!-- ar 派每张涉及训练机的单时附上。按实际集群填写部署参数。 -->

4 用卡与占卡
只有两条：①不管跑什么程序，机器时刻满足下列指标，否则报警；②最大化用卡，占着卡照样跑 MVP/测试。
指标（逐卡持续）：占卡 GPU 100%，真实实验推理高峰≥90%；均须显存≥20%、内存≥2%、共享内存≥2%；CPU>10%。
H200 占卡常驻，禁停、禁 kill、禁删气球：轻量测试/MVP 压着跑；正式实验（单卡≥90%）先 keeper.py release（业务上卡才暂停，不达标/报警/结束即顶上），完后 unhold；>95GB 显存才 --exclusive。见 <REDACTED_KEEPER_DOC>“实验接入”。
报警先占卡：H200 立即 keeper.py alarm --on，消警后 --off；其他机器立即起占卡。
B 卡（含 B300）无需占卡。启动前核实 GPU 归属和空闲，不凭一次 0% 接管；巡检端不打断训练或抢卡。

6 存储环境
项目与仓库：<REDACTED_PROJECT_ROOT>/<project>/。
模型位于 <REDACTED_MODEL_ROOT>，数据位于同级 Datasets 目录。下载前先查共享目录和 <REDACTED_MODEL_MIRROR>，已有则复用。
/root、/usr/local、/tmp 容器重建时不能假定保留；<REDACTED_LOCAL_DISK> 是宿主机本地盘，不是共享盘。
Conda Base 位于 <REDACTED_CONDA_BASE>。新环境建在项目目录，使用 Conda；已登记环境不装包不升级；pip 优先内网镜像 <REDACTED_PACKAGE_MIRROR>。

7 红线
/dev/shm 与内存：达上限（shm 为 min(8,000,000,000 字节, tmpfs)）85% 预警、95% 强杀实验；禁放数据集、模型、批次、缓存；占卡气球不许删；派发任务须传达。
凭据（密钥、令牌）不输出、不复制到文档/聊天/Git；不输出环境变量全集。
“共享运行环境中不要自行执行 logout 或更换账号。”

---

补充（AutoResearch 的落实方式）：卡只从 slots.json 领；实验只经 ar-run 起（独立 tmux、登记账本、自动 release/unhold、每分钟查 /dev/shm 和内存、95% 只杀本实验不动气球）；训练机用集群代理；所有实验带检查点，产物放共享盘项目目录。占卡、机器核查及环境修补证据写入 ops.md。
