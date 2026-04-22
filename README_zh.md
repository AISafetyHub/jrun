# jrun

基于 airsctl 平台的 Job 提交与管理命令行工具。支持网格搜索参数展开和 Job 追踪。

[English](README.md)

## 安装

```bash
# 使用 uv 安装（推荐）
uv tool install .

# 或开发模式安装
uv sync
uv run jrun --help
```

## 快速开始

### 1. 初始化

在项目目录下运行 `jrun init`，创建 `.jrun/` 配置目录：

```bash
jrun init -N <实验名称>
# 或同时指定实验 ID
jrun init -N <实验名称> -e <实验ID>
```

这会生成 `.jrun/settings.json`，保存实验信息。所有 `jrun` 命令会从当前目录向上查找该目录。

### 2. 编写配置文件

**网格搜索**（`config/search.yaml`）：

```yaml
description: 在多个数据集上评估模型
environment:
  image: my-image:latest
target:
  service: sing
  name: barlow05
  workspace_name: gcraml-wus2
search:
  job_template:
    name: eval_{model}_{data}_{time}
    sku: G4-V100
    command:
      - "python evaluate.py --model {model} --data {data} --time {time}"
    submit_args:
      env:
        CUDA_LAUNCH_BLOCKING: 1
  sampling: grid
  max_trials: 100
  params:
    - name: model
      values: ["llama3_8b", "gpt35"]
    - name: data
      values: ["news", "paper"]
    - name: time
      values: [1, 2, 3]
```

**单个任务**（`config/single.yaml`）：

```yaml
description: 单次评估任务
environment:
  image: my-image:latest
target:
  service: sing
  name: barlow05
  workspace_name: gcraml-wus2
job:
  name: eval_llama3_news
  sku: G4-V100
  command:
    - "python evaluate.py --model llama3_8b --data news"
```

### 3. 提交任务

```bash
# 预览（不实际提交）
jrun submit config/search.yaml --dry-run

# 正式提交
jrun submit config/search.yaml
```

### 4. 查看状态

```bash
# 查看所有已追踪的 job
jrun status

# 按搜索名称查看
jrun status eval

# 按 job ID 查看单个 job
jrun status <job-id>
```

### 5. 管理任务

```bash
# 停止运行中的 job
jrun stop <job-id>

# 归档已完成的 job
jrun cancel <job-id>

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
| `{auto:5s}` | 基于参数的确定性 5 位哈希 |

## 目录结构

```
your-project/
├── .jrun/
│   ├── settings.json    # 实验名称/ID
│   └── jobs.json        # 已提交 job 追踪记录
└── config/
    └── search.yaml      # Job 配置文件
```
