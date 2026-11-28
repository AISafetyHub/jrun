# jrun

基于 airsctl 平台的 Job 提交与管理命令行工具。支持网格搜索参数展开、多节点任务、本地调试和 Job 追踪。

[English](README.md)

## 安装

```bash
# 使用 uv 从 GitHub 直接安装（推荐）
uv tool install git+https://github.com/AISafetyHub/jrun.git
```

升级到最新版：

```bash
uv tool upgrade jrun
```

无需安装，直接运行一次：

```bash
uvx --from git+https://github.com/AISafetyHub/jrun.git jrun --help
```

如需参与开发：

```bash
git clone https://github.com/AISafetyHub/jrun.git
cd jrun
uv sync
uv run jrun --help
```

> **注意：** 运行 jrun 前需取消设置 `http_proxy` 和 `https_proxy`，否则 airsctl 子进程调用会失败。

## 核心概念

jrun 按三个层次组织工作：

- **Project（项目）**—— 当前目录下的 `.jrun/` 文件夹，记录该目录下的所有提交（job 和 search），存储在 `.jrun/jobs.json` 中。`jrun init` 创建它，其余命令会从当前目录向上查找。
- **Experiment（实验）**—— 九鼎平台上的实验（即 `airsctl experiment` 操作的对象）。由配置中的 `experiment:` 块指定名称，不存在时 jrun 会自动创建。通过 `jrun experiment create / list / jobs / delete` 管理。
- **Job（任务）**—— 提交到平台上、隶属于某个 experiment 的一次运行。网格搜索中的每个 trial 就是一个 job。通过 `jrun job status / stop / remove` 管理。

命令布局与概念一一对应：`jrun submit` 创建 job，`jrun job ...` 管理已追踪的 job，`jrun experiment ...` 管理平台实验，所有记录按 project 归档。

## 快速开始

### 1. 初始化与登录

```bash
jrun init                       # 在当前目录创建 .jrun/
jrun login                      # 将平台 AK/SK 存入 ~/.jrun/auth.json（交互式输入）
jrun login --ak <AK> --sk <SK>  # 非交互方式
jrun whoami                     # 查看账号、身份类型和已加入的项目集（支持 --json）
```

`jrun init` 只创建 `.jrun/` 目录（其中的 `settings.json` 只是空的标记文件，不绑定任何实验），所有 `jrun` 命令会从当前目录向上查找该目录。`jrun login` 会用你的 AK/SK 换取 token 并验证；创建实验、解析队列和查询队列都需要 REST API 凭据。凭据解析顺序：`AIRS_TOKEN`（现成 token）→ `AIRS_AK`/`AIRS_SK` 环境变量 → `~/.jrun/auth.json` → `/etc/accesskey/{user-ak,user-sk}`（base64 编码，九鼎平台 pod 上自动挂载）→ airsctl 缓存的 token（`/tmp/.airs/.token`）。在平台 pod 上凭据会自动获取，无需登录。

### 2. 编写配置文件

使用 `jrun template` 生成带完整注释的配置模板：

```bash
jrun template                    # 输出到终端
jrun template -f config/my.yaml  # 写入文件
```

所有要提交到平台的配置都必须有顶层 `experiment:` 块，且至少包含 `name:`。jrun 按名称精确查找实验，不存在则通过平台 API 创建（占位 config 使用第一个 job 的 image 和队列）。该块还可以为 job 提供默认值（`image`、`image_region`、`queue_name`、`queue_category`、`project_set_name`、`project_name`、`region_name`、`zone_name`），以及实验级设置（`description`、`train_frame`、`restart_policy`、`mounts` 等）。job 级的值总是优先于实验默认值。缺少该块时 `--dry-run` 只给出警告，正式提交会失败。

`mounts` 控制挂载到该实验所有任务中的存储：`home`（`/home/<用户>`）、`project`（`/share/project`）、`projset`（`/share/projset`），取值为 `rw`、`ro` 或 `false`（不挂载）。没写的目录按 `rw` 挂载；完全不写 `mounts` 时保持平台默认（三个目录都 `rw`）。该设置在创建实验时和每次提交时都会写入，因此修改 YAML 后之后的任务就会生效。旧的原始 `storage_info` 列表会被拒绝；如确需原始列表，可放到 `platform_overrides` 下。

```yaml
experiment:
  name: my-exp
  mounts:
    project: false   # 不挂载 /share/project
    projset: ro      # /share/projset 只读
```

**网格搜索**（`config/search.yaml`）：

```yaml
description: 在多个数据集上评估模型

# 必填：要提交到的实验。jrun 按名称查找，不存在则通过平台 API 自动创建。
experiment:
  name: my_experiment
  # image: ...                     # 可选：job 未填写 image 时的默认镜像

search:
  name: my_eval
  job_template:
    name: eval_{model}_{data}_{auto:4s}
    image: harbor.platform.baai-inner.ac.cn/library/pytorch-sshd:23.08
    # image_region: PRIVATE         # 可选：PUBLIC（默认）或 PRIVATE
    commands:
      - "python evaluate.py --model {model} --data {data}"
    envs:
      CUDA_LAUNCH_BLOCKING: "1"
      MODEL_NAME: "{model}"
    resource_config:
      queue_category: dedicated
      project_set_name: baai-safety
      project_name: baai-safety_research
      region_name: 北京-大兴
      zone_name: 可用区A
      queue_name: jiuding_airs1_h100  # 可选队列见 `jrun queues`
      priority: high
      accelerator_model: J1_ADV_H1-SXM4-80GB
      accelerator_count: 1
      cpu_cores: 8
      mem_gib: 128
      shared_mem_gib: 64
  sampling: grid
  max_trials: 100
  parallel_trials: 4    # 最大并发任务数；其余任务由 scheduler 调度
  params:
    - name: model
      values: ["llama3_8b", "gpt35"]
    - name: data
      values: ["news", "paper"]
```

**单个任务**（`config/single.yaml`）：

```yaml
description: 单次评估任务

experiment:
  name: my_experiment

job:
  name: eval_llama3_news
  image: harbor.platform.baai-inner.ac.cn/library/pytorch-sshd:23.08
  commands:
    - "python evaluate.py --model llama3_8b --data news"
  envs:
    HOME: /home/user
  resource_config:
    queue_category: dedicated
    project_set_name: baai-safety
    project_name: baai-safety_research
    region_name: 北京-大兴
    zone_name: 可用区A
    queue_name: my-queue
    priority: high
    accelerator_model: J1_ADV_H1-SXM4-80GB
    accelerator_count: 1
    cpu_cores: 8
    mem_gib: 128
    shared_mem_gib: 64
```

**多节点任务**（`config/multinode.yaml`）：

```yaml
description: 多节点训练 - 1 master + 3 worker

experiment:
  name: my_experiment

job:
  name: train_multinode
  image: harbor.platform.baai-inner.ac.cn/library/pytorch-sshd:23.08
  commands:
    - cd /workspace
    - torchrun --nproc_per_node=1 --nnodes=4 train.py
  envs:
    NCCL_DEBUG: INFO
    MASTER_PORT: "29500"
  resource_config:
    queue_category: dedicated
    project_set_name: baai-safety
    project_name: baai-safety_research
    region_name: 北京-大兴
    zone_name: 可用区A
    queue_name: my-queue
    priority: high
    accelerator_model: J1_ADV_H1-SXM4-80GB
    accelerator_count: 1
    cpu_cores: 8
    mem_gib: 32
    shared_mem_gib: 16
    roles:
      - name: Master
        replicas: 1
      - name: Worker
        replicas: 3
```

`roles` 中每一项是一个平台节点角色（`name`、`replicas`、资源字段、`resource_region`、`extra_label_info`、`platform_overrides`）。角色会继承外层 `resource_config` 的资源字段（`accelerator_model`、`accelerator_count`、`cpu_cores`、`mem_gib`、`shared_mem_gib`、`rdma_shared_count`、`accelerator_resource_name`、`fb_mem_gib`、`accelerator_mem_gib`、`cpu_model`）以及 `resource_region`，角色自己填写的值会覆盖继承值。不写 `roles` 时为单节点任务（一个 Master），`resource_config.resource_region` 作用于该 master 角色。旧的 `resource_config.worker` 字段已删除，填写会报错。

**本地调试**（`config/local.yaml`）：

```yaml
description: 本地运行测试
job:
  name: test_local
  commands:
    - echo "Hello from local"
  resource_config:
    queue_name: local
```

当 `queue_name` 为 `local` 时，命令直接在本机执行，不提交到平台（不需要 experiment 块或凭据），`jrun submit` 的退出码就是该命令的退出码。本地运行 `search:` 时只执行展开后的第一个任务。

`commands:` 列表中的每一项用换行连接，每项是一行 shell 命令；它们不会遇错即停，如果某行失败后不能继续执行后续行，请以 `set -e` 开头（或在同一行里用 `&&` 串联）。`command:` 只接受单个字符串。

队列名称可能在不同项目或地域重复。需要指定位置时，按
`project_set_name` → `project_name` → `region_name` → `zone_name` →
`queue_name` 填写名称，jrun 会自动解析平台 ID。`queue_category` 可填写
`dedicated`、`public` 或 `idle`；省略时默认是 `dedicated`。
不要把平台 UUID 填进 YAML；旧的 ID/显示名字段已经删除，填写后会直接报配置错误。

公共资源池和闲时资源使用 `jrun queues` 显示的队列名称：

```yaml
resource_config:
  queue_category: public       # dedicated、public 或 idle
  project_set_name: baai-safety
  project_name: baai-safety_research
  region_name: 北京-大兴
  zone_name: 可用区A
  queue_name: Public-Pool-Nvidia-A100
  accelerator_model: NVIDIA_A100-SXM4-40GB
  accelerator_count: 1
run_options:
  duration_hours: 12
  queue_priority_mode: normal
  usage_desc: evaluation benchmark
```

`run_options` 位于 job 层级（与 `resource_config` 平级），只接受 `duration_hours`、`queue_priority_mode`（`normal` 或 `high`）、`usage_desc` 和 `enable_checkpoint`，未知字段会报错。公共任务必须填写 `duration_hours`、`queue_priority_mode` 和 `usage_desc`；闲时任务使用相同的资源字段，但不需要公共队列审批参数。只要填写了 `run_options`，就会传给 `airsctl job run`。

可回收的低峰资源使用 `queue_category: idle`。共享队列的 `priority` 是固定的：
公共队列只接受 `high`，闲时队列只接受 `low`。这两类队列不要写 `priority`：jrun
会自动写入要求的值，写了冲突的值会报错。专属队列使用
`queue_category: dedicated`（默认，priority 不限）：

```yaml
resource_config:
  queue_category: idle       # 或 dedicated
  project_set_name: baai-safety
  project_name: baai-safety_research
  region_name: 北京-大兴
  zone_name: 可用区A
  queue_name: Public-Pool-Nvidia-A100
```

**自动选队列**（`examples/auto.yaml`）。空卡往往分散在多个队列和卡型上。可以设置 `queue_name: auto`，列出可接受的 `(accelerator_model, accelerator_count)` 组合，而不是写死一个队列：

```yaml
resource_config:
  queue_name: auto
  queue_category: [public, idle]      # 按优先级排列；也可以只写一个值
  project_set_name: baai-safety       # 位置名称用于过滤候选队列
  project_name: baai-safety_research
  accelerator_model: [NVIDIA_A100-SXM4-40GB, NVIDIA_V100-PCIE-32GB]
  accelerator_count: [4, 8]           # 按位置配对；写标量则套用到每个卡型
  cpu_cores: 32                       # 所有组合共用：按最大的组合设置
  mem_gib: 256
run_options:                          # 允许 public 时必填
  duration_hours: 12
  queue_priority_mode: normal
  usage_desc: evaluation benchmark
```

提交时，jrun 读取每个候选队列的空卡数，以及项目集剩余的公共/闲时提交名额（项目集上限减去运行中和排队中的任务数，即 `jrun queues` 的 RUN/LIMIT 列），然后按顺序为本次要提交的任务分配队列：

1. **立即开跑**：按类别优先级、组合优先级、空卡数从多到少排序，选第一个空卡足够（`accelerator_count` × 所有角色的 replicas 之和）、前面没有排队任务（见下文）且（public/idle）还有提交名额的候选。
2. **在平台排队**：放不下的任务，如果类别中有 `dedicated`，就分到专属队列排队（专属队列允许排队提交）；否则分到仍有提交名额的公共/闲时队列排队。在这些队列中优先选前面排队任务最少的，其次选空卡最多的。
3. **等待名额**：公共/闲时名额都用完时，任务 config 会写到首选队列上，由后台 scheduler 在名额释放后启动（无论是否设置 `parallel_trials`）。单个 `job:` 如果需要等待，则直接报错。

**FIFO 队列。** 队列可能显示有空卡，但前面已经有任务在排队（例如排队任务需要的卡比空卡多）。先进先出调度下，排在它后面的任务都不会启动，因此 jrun 不会把这样的队列当作「立即开跑」。专属队列除调度策略为 Traverse 外都按 FIFO 处理（Smart 也按 FIFO）。共享资源池总是 FIFO：公共任务（high 优先级）排在等待中的公共任务之后，闲时任务（low 优先级）排在等待中的公共和闲时任务之后，按所有项目集统计。读取排队数失败时，退回只看空卡。`jrun queues` 的 WAITING 列显示这些数量，分配预览会打印 `N waiting ahead`。

公共和闲时是同一批物理资源池，因此空卡只计算一次，提交名额按模式分别计算。卡型名需要精确匹配（不区分大小写）；总容量小于任务所需卡数的队列不会被选中。分配后的任务就是所选队列上的普通任务，并带有 `JRUN_QUEUE_NAME`、`JRUN_ACCELERATOR_MODEL`、`JRUN_ACCELERATOR_COUNT` 环境变量，命令可以据此调整（例如 `echo $$JRUN_ACCELERATOR_MODEL`）；直接运行 `torchrun`（不写 `--nproc_per_node`）就会按可见 GPU 数启动进程（见[远程任务环境变量](#远程任务环境变量)）。`--dry-run` 会用只读的平台查询预览分配结果；真正提交时会按当时的容量重新分配。`accelerator_model`/`accelerator_count`/`queue_category` 写成列表时必须使用 `queue_name: auto`（也可以写在 `experiment:` 块中），auto 任务的角色不能覆盖卡型和卡数字段。等待名额的任务，其队列在提交时就已确定。

job（`job:` 或 `search.job_template:`）中的未知字段会被当作拼写错误拒绝。jrun 未建模的平台原始字段请放到 `platform_overrides:` 下（job、`resource_config`、每个角色和 `experiment` 都支持；`resource_config`、角色和 `experiment` 中的未知字段也会作为 override 透传）。配置错误（未定义的 `$VAR`、YAML 语法错误、`worker`/`submission`/`experiment_id` 等已删除字段、错误的 `queue_category`、长度不匹配的卡型/卡数列表）只输出一行 `Error: ...` 并以退出码 1 结束。

### 3. 提交任务

```bash
# 预览（不实际提交）
jrun submit config/search.yaml --dry-run

# 正式提交
jrun submit config/search.yaml

# 创建实验时跳过确认
jrun submit config/search.yaml -y

# 自动覆盖已有同名任务
jrun submit config/search.yaml --overwrite

# 仅覆盖特定状态的任务
jrun submit config/search.yaml -o -s Failed -s Stopped

# 只提交网格中的部分任务（可重复）
jrun submit config/search.yaml --only my_eval_lr0.01

# 以 JSON 输出结果（不交互；提示信息输出到 stderr）
jrun submit config/search.yaml --dry-run --json
```

当任务名已存在于追踪记录中时，jrun 会提示覆盖或跳过。使用 `--overwrite`（`-o`）自动覆盖，可配合 `--status`（`-s`）按状态过滤。

覆盖会先停止旧任务：在安装新 config 之前，jrun 会 stop 仍未结束的旧平台 job（scheduler 持有的 `queued-*` 记录只在本地标记为 `Stopped`；平台上仍处于 `Pending` 或 `Approving` 的 job 会和运行中的 job 一样被 stop；已结束的 job 不做处理）。网格搜索中，如果某个旧 job 无法停止，只有这一个任务会报错并跳过，其余任务继续；单个任务的 stop 失败则会中止本次提交。旧的本地名称到 ID 映射只有在新任务成功启动后才会删除；如果 modify 或 run 失败，旧映射会保留。

每次非 dry-run 提交都会把目标 experiment 当作“当前工作集”处理。jrun 先展开
YAML 并按照“新任务/覆盖/跳过”的决定筛选任务，然后只调用一次完整的
`airsctl experiment modify`，其中恰好包含本次选中的任务。跳过的任务以及上次
提交遗留的 config 都不会保留在这次列表中。现有第一个 config 只用作平台结构
模板，不会继续追加到列表。modify 之后，每个选中的 config 再调用一次只运行的
`airsctl job run`；单个任务 run 失败（包括输出中找不到 job ID）会逐个报告，
其他任务继续执行。

`.jrun/jobs.json` 是项目的 job 历史和状态来源，experiment config 只表示最近
一次提交的工作集。每次提交都会完整替换共享 experiment，因此除非这正是预期
用途，否则请使用专用 experiment。设置 `parallel_trials` 时，首次 modify 会
写入全部选中 config，后台 scheduler 只负责运行尚未启动的 config。如果实验已
有 100 个及以上 config，jrun 会警告并询问是否继续。

#### 通过远程服务提交公共队列任务

如果你的账号只能提交专属队列和闲时队列、不能提交公共资源池，可以由 jrun 远程提交服务（见[远程提交服务](#远程提交服务)）代为提交公共任务。在配置中加顶层 `remote:` 块即可开启：

```yaml
remote:
  url: https://jrun-svc.example.com   # 也可以写 $JRUN_REMOTE_URL
  categories: [public]                # 默认：只有公共任务走远程
```

auto 选队列之后，最终类别在列表中的任务发往服务，其余任务照常在本地提交；`--dry-run` 会为每个任务显示 `Route:`。走远程的任务需要 `project_set_name`。服务会用你自己的凭据验证身份，检查你已加入该项目集，并且能以闲时视角看到目标资源池，然后用服务账号提交到实验 `<你的 alias>-<name>`（`mounts.home` 关闭）。这些任务在本地记录服务地址，`jrun job status / stop / remove` 会通过服务操作它们。示例见 `examples/remote-public.yaml`，详见 `docs/remote-service.md`。

如果实验尚不存在，jrun 会询问是否创建（加 `-y` 跳过询问）。只想创建实验、不提交任务时：

```bash
jrun experiment create config/search.yaml
```

### 4. 查看集群容量

```bash
# 按集群分组列出专属、公共、闲时队列的空闲卡数
jrun queues
jrun queues --category public                     # dedicated / public / idle（可重复）
jrun queues --category public --category idle
jrun queues --ids                                 # 同时显示 queue/cluster/zone ID
jrun queues --projset-id <ID> --proj-id <ID>      # 限定到某个项目集 / 项目

# 查看集群中所有任务的 GPU 利用率（需要管理员 token）
jrun gpu-usage
jrun gpu-usage -s Running -s Pending   # 按状态过滤（可重复，默认只看 Running）
jrun gpu-usage --all                   # 不限状态
jrun gpu-usage --include-cpu           # 包含不占卡的纯 CPU 任务（默认只显示占卡任务）
jrun gpu-usage --sort gpus             # 按卡数排序（可选 time/gpus/gpu/gmem/cpu/mem/owner）
jrun gpu-usage --sort gpu --asc        # 按 GPU 利用率升序（默认降序）
jrun gpu-usage --cluster-id <ID> --zone-id <ID>   # 按集群 / 可用区过滤（可重复）
jrun gpu-usage --owner my-workspace -n 20         # 按 owner 过滤，显示 20 行（默认 50）
```

公共和闲时是同一批共享资源池的两种提交方式，因此 `jrun queues` 会把每个共享资源池在 `public` 和 `idle` 下各列一次，各自带有 RUN/LIMIT（运行中任务数 / 项目集上限）。WAITING 是新任务前面排队的任务数：专属队列为队列中 Pending/Scheduling 的任务（Traverse 队列显示 `-`，因为排队任务不会阻塞）；共享资源池为所有项目集的排队任务（public 只算公共任务，idle 算公共 + 闲时任务）。有空卡但 WAITING > 0 的队列不会立刻启动新任务。`--proj-id` / `--projset-id`（`gpu-usage`、`experiment jobs`、`experiment delete` 也支持）用于设置项目 / 项目集请求头；默认不发送项目，项目集从账号已加入的项目集中自动确定。

### 5. 查看状态

```bash
# 已追踪的 job 和 search（默认显示 10 条）
jrun job status
jrun job status -n 50      # 最多显示 50 条
jrun job status -n 0       # 显示全部

# 按搜索名称查看
jrun job status my_eval

# 按任务名或 ID 查看
jrun job status <任务名或ID>

# 仅显示特定状态的任务
jrun job status -s Running
jrun job status my_eval -s Failed -s Stopped

# 以 JSON 输出刷新后的任务（全部匹配项）
jrun job status my_eval --json
```

状态输出包含彩色状态指示和可点击的平台链接（需平台 ID 可用）。通过远程服务提交的任务从该服务刷新状态（每个服务一次请求）。

`jrun queues`、`jrun job stop`、`jrun job remove` 也支持 `--json`，JSON 结构见 `docs/remote-service.md`。

### 6. 管理任务

```bash
# 停止运行中的 job
jrun job stop <任务名或ID>

# 停止某个搜索中所有可停止的 job，并停止其 scheduler
jrun job stop <搜索名称>

# 停止所有处于指定状态的已追踪 job（只会停止可停止状态的 job）
jrun job stop -s Running -s Queued

# 移除任务或整个搜索（cancel、删除配置、从追踪记录中移除）
jrun job remove <任务名或ID>
jrun job remove <搜索名称>
jrun job remove <搜索名称> -s Failed   # 只移除该搜索中指定状态的 job

# 列出平台上的所有实验
jrun experiment list

# 列出某个实验在平台侧的所有 job（按名称或 ID，-s 按状态过滤）。
# 与 `jrun job status` 不同，它也会显示未被本项目追踪的 job。
jrun experiment jobs <实验名或ID>

# 删除（归档）实验；-y 跳过确认提示
jrun experiment delete <实验名或ID>
```

`jrun job remove` 会先停止仍在进行中（Approving/Pending/Running）的 job，并最多等待 60 秒让它结束，因为 `airsctl job cancel` 只能归档已经结束的 job。之后再归档（cancel）每个 job，从该 job 自己所属的实验中删除对应 config，并将其从 `.jrun/jobs.json` 中移除。等待后仍未结束的 job 会保留在记录中并给出提示，待其停止后再执行一次 `jrun job remove` 即可（搜索会保留到其中所有 job 都被移除）。实验名称需要精确匹配；UUID 会按 ID 查找。

## 配置变量

| 变量 | 展开为 |
|---|---|
| `$CONFIG_DIR` | 配置文件所在目录（jrun 内置变量） |
| `$VAR` | 提交时从本地环境变量解析，未定义则报错（纯注释行不展开） |
| `$$VAR` | 提交后变为 `$VAR`（远程环境变量引用） |
| `{param}` | 搜索网格中的参数值 |
| `{auto:Ns}` | 基于参数的确定性 N 位 MD5 哈希 |

由 `queue_name: auto` 分配的任务还会在 `envs` 中带有 `JRUN_QUEUE_NAME`、`JRUN_ACCELERATOR_MODEL`、`JRUN_ACCELERATOR_COUNT`（命令中用 `$$JRUN_ACCELERATOR_COUNT` 引用）。

## 远程任务环境变量

九鼎实验任务运行时能看到的变量（2026-11-24 用单节点探测任务实测；命令中用 `$$VAR` 引用）：

| 变量 | 来源 | 取值 |
|---|---|---|
| `AIRS_WORKLOAD_TYPE` | 平台 | 实验任务为 `experiment`（开发环境中为 `workspace`） |
| `AIRS_EXPERIMENT_ID` | 平台 | 所属实验 UUID（`AIRS_EXPERIMENT` 为 `job`） |
| `HOSTNAME` | 平台 | `job-<job UUID>-master-0`；job ID 只出现在这里 |
| `MASTER_ADDR` / `MASTER_PORT` | 平台 | master pod 主机名 / `23456` |
| `RANK` / `WORLD_SIZE` | 平台 | 单节点为 `0` / `1` |
| `PET_NNODES` / `PET_NODE_RANK` / `PET_WORLD_SIZE` / `PET_MASTER_ADDR` / `PET_MASTER_PORT` | 平台 | `torchrun` 读取它们作为 `--nnodes`/`--node_rank` 等参数的默认值 |
| `PET_NPROC_PER_NODE` | 平台 | `auto`：直接运行 `torchrun`（不写 `--nproc_per_node`）时按可见 GPU 数启动进程 |
| `NVIDIA_VISIBLE_DEVICES` | 平台 | 可见 GPU |
| `PATH` | 平台 | 以 `/client-tools` 开头（内含 `airsctl`） |
| 你的 `envs:` | jrun | `envs:` 下的每个键都会成为环境变量 |
| `JRUN_QUEUE_NAME` / `JRUN_ACCELERATOR_MODEL` / `JRUN_ACCELERATOR_COUNT` | jrun | 由 `queue_name: auto` 分配的任务才有 |

平台不提供 job ID、队列名或卡型/卡数变量，所以 jrun 会为 auto 任务注入 `JRUN_*`。`CUDA_VERSION`、`HOME`、`PIP_INDEX_URL` 等变量来自镜像，换镜像就会变。多节点任务中 `RANK`/`PET_*` 的取值尚未实测。

## 配置字段参考

| 字段 | 位置 | 说明 |
|---|---|---|
| `description` | 顶层 | 可读描述（jrun 不使用） |
| `experiment` | 顶层 | 实验定义（提交必填）：按名称精确查找，不存在则创建 |
| `experiment.name` | experiment | 实验名称（必填） |
| `experiment.image` / `image_region` | experiment | job 未填写时使用的默认镜像和仓库类型 |
| `experiment.project_set_name` / `project_name` | experiment | 队列解析的默认项目集和项目 |
| `experiment.region_name` / `zone_name` / `queue_name` | experiment | 默认地域、可用区和队列名称 |
| `experiment.queue_category` | experiment | 默认队列类型：`dedicated`、`public` 或 `idle`（`queue_name: auto` 时可写优先级列表） |
| `experiment.description` | experiment | jrun 创建实验时使用的描述 |
| `experiment.train_frame` / `restart_policy` / `native_cluster` / `profiler_info` / `model_storage_info` | experiment | 提交时写入实验的实验级设置 |
| `experiment.mounts` | experiment | `{home, project, projset}`：`rw` / `ro` / `false`；挂载到任务中的存储（默认全部 `rw`） |
| `name` | job / job_template | 任务名（支持 `{param}` 和 `{auto:Ns}`） |
| `image` | job / job_template | 任务运行的 Docker 镜像（search 中支持 `{param}`）；覆盖 `experiment.image` |
| `image_region` | job / job_template | 镜像仓库类型：`PUBLIC`（默认）或 `PRIVATE`，不区分大小写 |
| `command` | job / job_template | 单条命令字符串 |
| `commands` | job / job_template | 命令列表（用换行连接） |
| `envs` | job / job_template | 环境变量（支持 `{param}` 和 `$$`） |
| `train_frame` / `restart_policy` | job / job_template | 单个 config 的训练框架和重启策略 |
| `code` / `code_path` / `git_prefix` / `git_branch` / `git_commit` / `gitee` | job / job_template | 写入平台 config 的代码来源字段 |
| `dataset` / `model_storage_info` / `model_storage_info_items` | job / job_template | 写入平台 config 的数据集和模型存储挂载 |
| `selected_nodes` | job / job_template、resource_config | 将任务固定到指定节点 |
| `platform_overrides` | experiment / job / resource_config / role | 新增平台字段的原始透传（传递未建模 job 字段的唯一方式） |
| `resource_config` | job / job_template | 资源配置；覆盖实验默认值 |
| `resource_config.queue_name` | resource_config | 提交队列（通过 API 解析；`local` 为本地执行；`auto` 按空卡和排队任务自动选队列） |
| `resource_config.queue_category` | resource_config | `dedicated`（默认）、`public` 或 `idle`；`queue_name: auto` 时可写优先级列表 |
| `resource_config.project_set_name` / `project_name` | resource_config | 用于解析队列的项目集和项目名称 |
| `resource_config.region_name` / `zone_name` | resource_config | 用于解析队列的地域和可用区名称 |
| `resource_config.priority` | resource_config | `low`、`medium`、`high` |
| `resource_config.accelerator_model` | resource_config | GPU 型号；`queue_name: auto` 时为可接受卡型列表 |
| `resource_config.accelerator_count` | resource_config | 每个节点的 GPU 数量；`queue_name: auto` 时为与 `accelerator_model` 按位置配对的列表（或一个通用卡数） |
| `resource_config.cpu_cores` | resource_config | 每个节点的 CPU 核数 |
| `resource_config.mem_gib` | resource_config | 每个节点的内存 (GiB) |
| `resource_config.shared_mem_gib` | resource_config | 每个节点的共享内存 (GiB) |
| `resource_config.rdma_shared_count` / `accelerator_resource_name` / `fb_mem_gib` / `accelerator_mem_gib` / `cpu_model` | resource_config | 其他单节点资源请求字段 |
| `resource_config.resource_region` | resource_config | master 角色的资源区域（角色会继承） |
| `resource_config.pod_restart_policy` / `restart_scope` | resource_config | Pod 重启行为 |
| `resource_config.roles` | resource_config | 节点角色：`{name, replicas, <资源字段>, resource_region, extra_label_info, platform_overrides}` 列表，每个角色继承上面的资源字段。单节点时省略 |
| `resource_config.roles[].extra_label_info` | role | 该角色的额外平台标签 |
| `run_options.duration_hours` | job / job_template | 公共队列预计运行时长 |
| `run_options.queue_priority_mode` | job / job_template | 公共队列模式：`normal` 或 `high` |
| `run_options.usage_desc` | job / job_template | 公共队列用途说明 |
| `run_options.enable_checkpoint` | job / job_template | 是否启用公共队列 checkpoint |
| `search.name` | search | 搜索名称（可选，自动推导） |
| `search.job_template` | search | 带 `{param}` 占位符的任务定义 |
| `search.params` | search | `{name, values}` 列表，用于网格展开 |
| `search.sampling` | search | `grid` |
| `search.max_trials` | search | 最大参数组合数（默认 9999） |
| `search.parallel_trials` | search | 最大并发任务数；其余 config 由 scheduler 运行 |
| `search.poll_interval` | search | scheduler 轮询间隔秒数（默认 30） |
| `remote.url` | remote | jrun 远程提交服务地址；路由到这里的任务由服务提交 |
| `remote.categories` | remote | 走远程服务的队列类别，按 auto 选队列后的结果判断（默认 `[public]`） |

## 远程提交服务

`server/` 是独立的 `jrun-server` 包，自带 Dockerfile 和 `docker-compose.yml`。它用某个账号的 AK/SK 运行（例如可以使用公共资源池的 key），为通过验证的九鼎用户提交任务：每个调用者有独立的工作区，服务在其中调用 `jrun` CLI（`--json`）。

```bash
cd server
cp /client-tools/airsctl ./airsctl   # 从九鼎开发环境拷贝
cp .env.example .env                 # AIRS_AK、AIRS_SK、JRUN_SERVE_PROJECT_SETS，可选 JRUN_SERVE_USERS
docker compose up -d --build
```

鉴权检查：
- **身份**：调用者的 token 必须属于可用账号（设置了 `JRUN_SERVE_USERS` 时还须在名单中）。
- **项目集**：必须在白名单中，且调用者已加入。
- **资源池**：调用者必须能以闲时视角看到目标资源池。
- **配置**：按白名单重建（不允许平台原始字段、代码/数据集/模型存储字段和私有镜像），并重新转义 `$`，服务自身的环境变量不可能被替换进去。

调用者会发送自己 2 小时有效的 token，请在服务前加 TLS。详见 `docs/remote-service.md`。

jrun 本身也可以在九鼎 pod 之外运行：设置 `JRUN_AIRSCTL_GATEWAY=http://platform-multi.baai.ac.cn` 后，每次 airsctl 调用都会追加 `-u <gateway>`，以及任务所在队列的项目上下文（`-P/-p`），见 `docs/airsctl.md`。

## 目录结构

```
your-project/
├── .jrun/
│   ├── settings.json    # 项目根目录的空标记文件
│   ├── jobs.json        # 已提交 job 追踪记录
│   └── scheduler/       # parallel_trials scheduler 的 PID 文件和日志
└── config/
    └── search.yaml      # Job 配置文件
```

平台 REST API 的凭据保存在 `~/.jrun/auth.json`（用户级，由 `jrun login` 写入），不在项目的 `.jrun/` 中。

## jrun Agent Skill

仓库内置了自包含的 `jrun-workflow` Skill，源目录为
`skills/jrun-workflow/`。它帮助 Codex 和 Claude Code 生成、检查和预览
jrun YAML，提交并监控单任务/网格搜索/多节点任务，以及管理实验和已追踪
任务。Skill 不会修改 jrun CLI，也不会自动安装 jrun；使用前请先按上面的
“安装”章节安装 jrun。Skill 所需的配置规则都打包在自身目录中，安装后不
依赖当前仓库或外部文档。

### 安装到 Codex

将 Skill 源目录复制到 Codex 的 Skill 根目录；默认目标是
`~/.codex/skills`，如果环境设置了自定义 `CODEX_HOME`，会使用该目录：

```bash
dest="${CODEX_HOME:-$HOME/.codex}/skills/jrun-workflow"
mkdir -p "$dest"
cp -a skills/jrun-workflow/. "$dest/"
```

Codex 支持将仓库中提交的 Skill 用于其应用、CLI 和 IDE 界面，详见 [Codex Skill 介绍](https://openai.com/index/introducing-the-codex-app/)。

安装后可以显式调用：

```text
Use $jrun-workflow to prepare and preview a grid-search job.
```

### 安装到 Claude Code

项目共享的 Skill 放到当前项目的 `.claude/skills`：

```bash
dest=".claude/skills/jrun-workflow"
mkdir -p "$dest"
cp -a skills/jrun-workflow/. "$dest/"
```

如果希望所有项目都能使用，将 `dest` 改为
`~/.claude/skills/jrun-workflow`。在 Claude Code 中可以使用
`/jrun-workflow`，也可以直接用自然语言描述 jrun 任务。Claude Code 同时
支持项目级和用户级 Skill 目录，详见 [Claude Code 配置目录文档](https://code.claude.com/docs/en/claude-directory)。

安装新 Skill 后，如果宿主没有立即发现新目录，请重新启动 Codex 或 Claude
Code 会话。Skill 更新后重新执行对应的复制命令即可同步安装副本。
