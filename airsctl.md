# 命令行工具说明文档

平台支持用户 SSH 登录开发环境后，使用 `airsctl` 命令进行一系列操作，常用的命令如下：

### 1. 基础常用命令

* **列出实验列表**
    `airsctl experiment list`
    > 说明：列出用户在当前项目下所有的实验列表。实验列表信息包括：实验 ID、实验名称、实验创建时间。
* **按 ID 查看实验详情**
    `airsctl experiment list -e <实验ID>`
    > 说明：根据实验 ID 列出某个实验的实验参数详情。
* **按名称查看实验详情**
    `airsctl experiment list -N <实验名称>`
    > 说明：根据实验名称列出某个实验的实验参数详情。
* **列出 Job 列表**
    `airsctl job list`
    > 说明：列出用户在当前项目下所有的 job 列表。job 列表信息包括：JobID、Job 状态、job 创建时间。
* **查看 Job 配置详情**
    `airsctl job list -j <job ID>`
    > 说明：根据 job id 查看 job 配置详细信息。
* **停止 Job**
    `airsctl job stop -j <job ID>`
    > 说明：将某个 job 停止。注意：只有排队中、启动中或者运行中的 job 才能停止。
* **删除/归档 Job**
    `airsctl job cancel -j <job ID>`
    > 说明：将某个 job 删除（归档）。注意：只有运行结束的 job 才能删除（归档）；如果某个 job 还在运行中，请先用 stop 命令停止后再进行 cancel；提交命令后，返回 cancel 成功或者失败。
* **修改实验配置**
    `airsctl experiment modify -f <配置文件>`
    > 说明：配置文件可以通过查看实验详情并重定向到个人目录中获取。例如：`airsctl experiment list -N <expName> > /home/zhangsan/exp.json`；用户可根据自己的需要修改配置文件并保存（例如将 worker 数从 1 修改为 4，将 worker 的 GPU 个数从 2 修改为 4），然后执行 modify 命令使修改生效。用户此时在实验管理详情页面查看实验详情，可以看到相应的参数发生了变化。
* **启动 Job（按 ID）**
    `airsctl job run -e <experiment-id> -c <conf-id>`
    > 说明：根据实验 id 和配置 id 启动 job。
* **启动 Job（按名称）**
    `airsctl job run -N <experiment-name> -n <config-name>`
    > 说明：根据实验名称和配置名称启动 job。

---

### 2. 更多命令和参数详情

| 参数1 | 参数2 | 参数3 | 命令示例 | 说明 |
| :--- | :--- | :--- | :--- | :--- |
| **experiment** | | | `airsctl experiment` | 展示该命令的说明文档。其下一级参数主要有 list 和 modify。 |
| **experiment** | **list** | | `airsctl experiment list` | 列出用户在当前项目下所有的实验列表。 |
| **experiment** | **list** | **-h** | `airsctl experiment list -h` | `airsctl experiment list` 命令的帮助文档，重点关注 `-e` 和 `-N` 参数。 |
| **experiment** | **list** | **-e or -N** | `airsctl experiment list -e 实验ID` / `airsctl experiment list -N 实验名称` | 分别根据实验 ID 和实验名称列出某个实验的实验参数详情。 |
| **experiment** | **modify** | **-f** | `airsctl experiment modify -f 配置文件` | 指定配置文件路径修改实验，使修改生效。 |
| **job** | | | `airsctl job` | 展示该命令的下一级参数，分别为 list、cancel、stop、run。 |
| **job** | **list** | | `airsctl job list` | 列出用户在当前项目下所有的 job 列表。 |
| **job** | **list** | **-j** | `airsctl job list -j job ID` | 根据 job id 查看 job 配置详细信息。 |
| **job** | **stop** | **-h** | `airsctl job stop -h` | `airsctl job stop` 的命令行提示，重点关注参数 `-j, --job-id string`。 |
| **job** | **stop** | **-j** | `airsctl job stop -j job id` | 停止排队中、启动中或运行中的 job。 |
| **job** | **cancel** | **-h** | `airsctl job cancel -h` | `airsctl job cancel` 的命令行提示，重点关注参数 `-j, --job-id string`。 |
| **job** | **cancel** | **-j** | `airsctl job cancel -j job id` | 删除（归档）已结束的任务。 |
| **job** | **run** | **-h** | `airsctl job run -h` | `airsctl job run` 的使用帮助，重点关注 `-c`、`-n`、`-e`、`-N`。 |
| **job** | **run** | | `airsctl job run -e <exp-id> -c <conf-id>` | 填写 `(-c or -n)` 和 `(-e or -N)` 参数即可启动 job。 |
| **job** | **run** | | `airsctl job run -N exp1 -n config1` | 启动实验名称为 exp1，配置名为 config1 的 job。 |
| **version** | | | `airsctl version` | 查看命令行工具版本。 |
| **help** | | | `airsctl help` | 查看命令行帮助文档。 |

---

### 3. 注意事项
* 使用 `airsctl job run` 命令启动 job 的前提是已经创建了实验，并填写完参数。
* 使用 `airsctl job run (-c or -n) and (-e or -N)` 命令启动 job 后，会直接在当前实验下增加一条 job。