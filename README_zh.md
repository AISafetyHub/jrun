# jrun

基于 airsctl 平台的 Job 提交与管理命令行工具。支持网格搜索参数展开、多节点任务、本地调试和 Job 追踪。

[English](README.md)

## 安装

```bash
# 使用 uv 安装（推荐）
uv tool install .

# 或开发模式安装
uv sync
uv run jrun --help
```

> **注意：** 运行 jrun 前需取消设置 `http_proxy` 和 `https_proxy`，否则 airsctl 子进程调用会失败。

## 快速开始

### 1. 初始化

```bash
jrun init -N <实验名称>
# 或同时指定实验 ID
jrun init -N <实验名称> -e <实验ID>
```

创建 `.jrun/settings.json`，保存实验信息和平台 ID（自动提取，用于状态链接）。所有 `jrun` 命令会从当前目录向上查找该目录。

### 2. 编写配置文件

使用 `jrun template` 生成带完整注释的配置模板：

```bash
jrun template                    # 输出到终端
jrun template -f config/my.yaml  # 写入文件
```

**网格搜索**（`config/search.yaml`）：

```yaml
description: 在多个数据集上评估模型
search:
  name: my_eval
  job_template:
    name: eval_{model}_{data}_{auto:4s}
    commands:
      - python evaluate.py --model {model} --data {data}
    envs:
      CUDA_LAUNCH_BLOCKING: "1"
      MODEL_NAME: "{model}"
    resource_config:
      queue_name: my-queue
      priority: high
      accelerator_model: J1_ADV_H1-SXM4-80GB
      accelerator_count: 1
      cpu_cores: 8
      mem_gib: 128
      shared_mem_gib: 64
  sampling: grid
  max_trials: 100
  parallel_trials: 4    # TODO: 尚未实现
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

### 4. 查看状态

```bash
# 查看所有已追踪的 job
jrun status

# 按搜索名称查看
jrun status my_eval

# 按任务名或 ID 查看
jrun status <任务名或ID>

# 仅显示特定状态的任务
jrun status -s Running
jrun status my_eval -s Failed -s Stopped
```

状态输出包含彩色状态指示和可点击的平台链接（需平台 ID 可用）。

### 5. 管理任务

```bash
# 停止运行中的 job
jrun stop <任务名或ID>

# 移除任务或整个搜索（停止、删除配置、从追踪记录中移除）
jrun remove <任务名或ID>
jrun remove <搜索名称>

# 列出所有实验
jrun list
```

## 配置变量

| 变量 | 展开为 |
|---|---|
| `$CONFIG_DIR` | 配置文件所在目录 |
| `$HOME` | 用户主目录 |
| `$$VAR` | 字面量 `$VAR`（用于运行时环境变量） |
| `{param}` | 搜索网格中的参数值 |
| `{auto:Ns}` | 基于参数的确定性 N 位 MD5 哈希 |

## 配置字段参考

| 字段 | 位置 | 说明 |
|---|---|---|
| `description` | 顶层 | 可读描述（jrun 不使用） |
| `name` | job / job_template | 任务名（支持 `{param}` 和 `{auto:Ns}`） |
| `commands` | job / job_template | 命令列表（用 `&&` 连接） |
| `envs` | job / job_template | 环境变量（支持 `{param}` 和 `$$`） |
| `resource_config` | job / job_template | 资源配置 |
| `resource_config.queue_name` | 嵌套 | 队列名称（`local` 为本地执行） |
| `resource_config.priority` | 嵌套 | `low`、`medium`、`high` |
| `resource_config.accelerator_model` | 嵌套 | GPU 型号 |
| `resource_config.accelerator_count` | 嵌套 | GPU 数量 |
| `resource_config.cpu_cores` | 嵌套 | CPU 核数 |
| `resource_config.mem_gib` | 嵌套 | 内存 (GiB) |
| `resource_config.shared_mem_gib` | 嵌套 | 共享内存 (GiB) |
| `resource_config.worker` | 嵌套 | 多节点 worker 配置（`replicas`、资源覆盖） |
| `search.name` | search | 搜索名称（可选，自动推导） |
| `search.params` | search | `{name, values}` 列表，用于网格展开 |
| `search.sampling` | search | `grid` |
| `search.max_trials` | search | 最大参数组合数 |
| `search.parallel_trials` | search | 最大并发任务数（TODO: 尚未实现） |

## 目录结构

```
your-project/
├── .jrun/
│   ├── settings.json    # 实验名称/ID/平台 ID
│   └── jobs.json        # 已提交 job 追踪记录
└── config/
    └── search.yaml      # Job 配置文件
```
