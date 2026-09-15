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

- **Project（项目）**—— 当前目录下的 `.jrun/` 文件夹，记录该目录下的所有提交（job、search、配置），存储在 `.jrun/jobs.json` 中。`jrun init` 创建它，其余命令会从当前目录向上查找。
- **Experiment（实验）**—— 九鼎平台上的实验（即 `airsctl experiment` 操作的对象）。由配置中的 `experiment:` 块定义，不存在时 jrun 会自动创建。通过 `jrun experiment create / list / jobs / delete` 管理。
- **Job（任务）**—— 提交到平台上、隶属于某个 experiment 的一次运行。网格搜索中的每个 trial 就是一个 job。通过 `jrun job status / stop / remove` 管理。

命令布局与概念一一对应：`jrun submit` 创建 job，`jrun job ...` 管理已追踪的 job，`jrun experiment ...` 管理平台实验，所有记录按 project 归档。

## 快速开始

### 1. 初始化与登录

```bash
jrun init       # 在当前目录创建 .jrun/
jrun login      # 将平台 AK/SK 存入 ~/.jrun/auth.json（创建实验、查询队列需要）
```

`jrun init` 只创建 `.jrun/` 目录，所有 `jrun` 命令会从当前目录向上查找该目录。`jrun login` 会用你的 AK/SK 换取 token 并验证。凭据解析顺序：`AIRS_AK`/`AIRS_SK` 环境变量 → `~/.jrun/auth.json` → `/etc/accesskey/{user-ak,user-sk}`（base64 编码，九鼎平台 pod 上自动挂载）→ airsctl 缓存的 token（`/tmp/.airs/.token`）。在平台 pod 上凭据会自动获取，无需登录。

### 2. 编写配置文件

使用 `jrun template` 生成带完整注释的配置模板：

```bash
jrun template                    # 输出到终端
jrun template -f config/my.yaml  # 写入文件
```

**网格搜索**（`config/search.yaml`）：

```yaml
description: 在多个数据集上评估模型

# 可选：要提交到的实验（实验只是 job config 的容器）。jrun 按名称查找，
# 不存在则通过平台 API 自动创建（占位 config 使用 job 的 image/queue）。
experiment:
  name: my_experiment

search:
  name: my_eval
  # experiment_id: "custom-uuid"    # 可选：提交到已有的实验（按 ID）
  # experiment_name: other_exp      # 可选
  job_template:
    name: eval_{model}_{data}_{auto:4s}
    image: harbor.platform.baai-inner.ac.cn/library/pytorch-sshd:23.08
    # image_region: PRIVATE         # 可选：PUBLIC（默认）或 PRIVATE
    commands:
      - python evaluate.py --model {model} --data {data}
    envs:
      CUDA_LAUNCH_BLOCKING: "1"
      MODEL_NAME: "{model}"
    resource_config:
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
job:
  name: eval_llama3_news
  # experiment_id: "custom-uuid"    # 可选：提交到已有的实验（按 ID）
  # experiment_name: other_exp      # 可选
  commands:
    - python evaluate.py --model llama3_8b --data news
  envs:
    HOME: /home/user
  resource_config:
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
description: 多节点训练 - 1 master + N worker
job:
  name: train_multinode
  commands:
    - cd /workspace
    - torchrun --nproc_per_node=1 --nnodes=2 train.py
  envs:
    NCCL_DEBUG: INFO
    MASTER_PORT: "29500"
  resource_config:
    queue_name: my-queue
    priority: high
    accelerator_model: J1_ADV_H1-SXM4-80GB
    accelerator_count: 1
    cpu_cores: 8
    mem_gib: 32
    shared_mem_gib: 16
    worker:
      replicas: 1
```

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

当 `queue_name` 为 `local` 时，命令直接在本机执行，不提交到平台。

### 3. 提交任务

```bash
# 预览（不实际提交）
jrun submit config/search.yaml --dry-run

# 正式提交
jrun submit config/search.yaml

# 自动覆盖已有同名任务
jrun submit config/search.yaml --overwrite

# 仅覆盖特定状态的任务
jrun submit config/search.yaml -o -s Failed -s Stopped
```

当任务名已存在于追踪记录中时，jrun 会提示覆盖或跳过。使用 `--overwrite`（`-o`）自动覆盖，可配合 `--status`（`-s`）按状态过滤。

每次非 dry-run 提交都会把目标 experiment 当作“当前工作集”处理。jrun 先展开
YAML 并按照“新任务/覆盖/跳过”的决定筛选任务，然后只调用一次完整的
`airsctl experiment modify`，其中恰好包含本次选中的任务。跳过的任务以及上次
提交遗留的 config 都不会保留在这次列表中。现有第一个 config 只用作平台结构
模板，不会继续追加到列表。

modify 成功后，每个选中的 config 再调用一次只运行的 `job run`。覆盖任务不会
主动 stop/cancel 九鼎上旧的 job；新任务成功启动后，`--overwrite` 只更新本地
`.jrun/jobs.json` 中的名称到 ID 映射。如果 modify 或 run 失败，旧的本地映射
会保留。`.jrun/jobs.json` 是项目的 job 历史和状态来源，experiment config 只
表示最近一次提交的工作集。每次提交都会完整替换共享 experiment，因此除非这
正是预期用途，否则请使用专用 experiment。设置 `parallel_trials` 时，首次
modify 会写入全部选中 config，后台 scheduler 只负责运行尚未启动的 config。

如果配置中有 `experiment:` 块且实验尚不存在，jrun 会询问是否创建（加 `-y` 跳过询问）。只想创建实验、不提交任务时：

```bash
jrun experiment create config/search.yaml
```

### 4. 查看集群容量

```bash
# 按集群分组列出各队列的剩余卡数（帮助选择提交到哪个集群）
jrun queues
jrun queues --ids      # 同时显示 queue/cluster/zone ID

# 查看集群中所有任务的 GPU 利用率（需要管理员 token）
jrun gpu-usage
jrun gpu-usage -s Running -s Pending     # 按状态过滤（可重复，默认只看 Running）
jrun gpu-usage --all                     # 不限状态
jrun gpu-usage --include-cpu             # 包含不占卡的纯 CPU 任务（默认只显示占卡任务）
jrun gpu-usage --sort gpus               # 按卡数排序（可选 time/gpus/gpu/gmem/cpu/mem/owner）
jrun gpu-usage --sort gpu --asc          # 按 GPU 利用率升序
jrun gpu-usage --owner my-workspace -n 20
```

### 5. 查看状态

```bash
# 查看所有已追踪的 job
jrun job status

# 按搜索名称查看
jrun job status my_eval

# 按任务名或 ID 查看
jrun job status <任务名或ID>

# 仅显示特定状态的任务
jrun job status -s Running
jrun job status my_eval -s Failed -s Stopped
```

状态输出包含彩色状态指示和可点击的平台链接（需平台 ID 可用）。

### 6. 管理任务

```bash
# 停止运行中的 job
jrun job stop <任务名或ID>

# 移除任务或整个搜索（停止、删除配置、从追踪记录中移除）
jrun job remove <任务名或ID>
jrun job remove <搜索名称>

# 列出平台上的所有实验
jrun experiment list

# 列出某个实验在平台侧的所有 job（按名称或 ID，-s 按状态过滤）。
# 与 `jrun job status` 不同，它也会显示未被本项目追踪的 job。
jrun experiment jobs <实验名或ID>

# 删除（归档）实验；-y 跳过确认提示
jrun experiment delete <实验名或ID>
```

## 配置变量

| 变量 | 展开为 |
|---|---|
| `$CONFIG_DIR` | 配置文件所在目录（jrun 内置变量） |
| `$VAR` | 提交时从本地环境变量解析，未定义则报错 |
| `$$VAR` | 提交后变为 `$VAR`（远程环境变量引用） |
| `{param}` | 搜索网格中的参数值 |
| `{auto:Ns}` | 基于参数的确定性 N 位 MD5 哈希 |

## 配置字段参考

| 字段 | 位置 | 说明 |
|---|---|---|
| `description` | 顶层 | 可读描述（jrun 不使用） |
| `experiment` | 顶层 | 实验定义：jrun 按名称查找，不存在则创建 |
| `experiment.name` | 嵌套 | 实验名称（必填） |
| `experiment.cluster_id` / `zone_id` | 嵌套 | 可选显式 ID，覆盖队列解析结果 |
| `experiment.proj_id` / `projset_id` | 嵌套 | 可选显式项目 ID，跳过 API 解析 |
| `experiment.image` / `image_region` / `queue_name` | 嵌套 | 已废弃：作为 job 级字段的兜底，建议用 job 级字段 |
| `image` | job / job_template | 任务运行的 Docker 镜像（search 中支持 `{param}`） |
| `image_region` | job / job_template | 镜像仓库类型：`PUBLIC`（默认）或 `PRIVATE` |
| `name` | job / job_template | 任务名（支持 `{param}` 和 `{auto:Ns}`） |
| `commands` | job / job_template | 命令列表（用 `&&` 连接） |
| `envs` | job / job_template | 环境变量（支持 `{param}` 和 `$$`） |
| `resource_config` | job / job_template | 资源配置 |
| `resource_config.queue_name` | 嵌套 | 提交队列（通过 API 解析 queueId/cluster/zone；`local` 为本地执行） |
| `resource_config.priority` | 嵌套 | `low`、`medium`、`high` |
| `resource_config.accelerator_model` | 嵌套 | GPU 型号 |
| `resource_config.accelerator_count` | 嵌套 | GPU 数量 |
| `resource_config.cpu_cores` | 嵌套 | CPU 核数 |
| `resource_config.mem_gib` | 嵌套 | 内存 (GiB) |
| `resource_config.shared_mem_gib` | 嵌套 | 共享内存 (GiB) |
| `resource_config.worker` | 嵌套 | 多节点 worker 配置（`replicas`、资源覆盖） |
| `search.name` | search | 搜索名称（可选，自动推导） |
| `search.experiment_id` | search | 提交到已有的实验（按 ID） |
| `search.experiment_name` | search | 提交到已有的实验（按名称） |
| `search.params` | search | `{name, values}` 列表，用于网格展开 |
| `search.sampling` | search | `grid` |
| `search.max_trials` | search | 最大参数组合数 |
| `search.parallel_trials` | search | 最大并发任务数；其余 config 由 scheduler 运行 |
| `job.experiment_id` | job | 提交到已有的实验（按 ID） |
| `job.experiment_name` | job | 提交到已有的实验（按名称） |

## 目录结构

```
your-project/
├── .jrun/
│   ├── settings.json    # 项目设置（实验默认值、平台 ID）
│   └── jobs.json        # 已提交 job 追踪记录
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
