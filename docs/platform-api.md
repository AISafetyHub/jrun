# 九鼎平台 REST API 参考

本文档汇总 jrun 依赖的九鼎平台 REST API。jrun 中所有直接 HTTP 调用都封装在
`src/jrun/api.py`(`PlatformAPI`)，本文档是该模块（以及未来新增接口调用）的
权威参考。

**维护约定**：平台接口变动时，先更新本文档，再改 `api.py` / 相关代码；反之，
代码里新增任何平台接口调用，也必须回填本文档。CLAUDE.md 将此列为强制规则。

## 通用说明

### Base URL

- 统一入口：`https://platform-multi.baai.ac.cn`（jrun 代码使用的域名，
  `api.py` 中的 `PLATFORM_API_BASE`，可用环境变量 `JRUN_API_BASE` 覆盖）
- 各集群前端入口（如 `http://platform-dx6.baai.ac.cn` 大兴）只是域名不同，
  接口路径完全一致。本文档所有路径均省略 host。

### 认证方式

两种认证头，按接口要求使用：

1. **AK/SK 签名认证**：仅用于 `/api/v1/users/token/exchange`，换取访问 Token。
2. **Token 认证**：其余所有接口，请求头 `AIRS-Token: <token>`。

Token 有效期默认 7200 秒（2 小时），过期需重新换取。

### 通用请求头

除 `AIRS-Token` 外，projset 级接口通常还需要（header 名大小写不敏感，
不同文档/抓包中写作 `Airs-Proj-id` 或 `AIRS-Proj-ID`，均可用）：

| Header | 说明 |
|---|---|
| `AIRS-Proj-ID` | 项目 ID |
| `AIRS-Projset-ID` | 项目集 ID |
| `AIRS-Cluster-ID` | 集群 ID（部分接口需要） |
| `AIRS-Zone-ID` | 可用区 ID（部分接口需要） |

注意：`queue/select`、`job/select`、`job-snapshots/select` 等接口是 projset
作用域的，不传 projset 上下文会返回 403（`job/select` 已实测确认；
`experiment/delete` 按抓包认证头推断同样需要）。

### 分页约定

列表类接口统一使用：

```json
{"paging": {"page": 1, "pageSize": 100, "sort": "", "order": ""}}
```

响应中带 `paging.total`，jrun 在 `job_snapshots_all` 中按页拉取直到
某页不足 `pageSize`。

---

## 1. AK/SK 换取 Token

- **路径**：`POST /api/v1/users/token/exchange`
- **认证**：HMAC-SHA256 签名（无 Token）

### 签名算法

```
CanonicalRequest      = "POST" + "\n" + "/api/v1/users/token/exchange" + "\n" + Timestamp
CanonicalRequestHash  = Hex(SHA256(CanonicalRequest))
StringToSign          = "AIRS-HMAC-SHA256" + "\n" + Timestamp + "\n" + CanonicalRequestHash
Signature             = Hex(HMAC-SHA256(SK, StringToSign))
```

- 时间戳格式：ISO 8601 UTC，如 `20260514T055649Z`，签名后 **5 分钟内有效**。
- 实现参考：`api.py` 的 `compute_signature()`。

### 请求头

| Header | 示例 |
|---|---|
| `Authorization` | `AIRS-HMAC-SHA256 Credential=ak-xxx, Signature=abc123...` |
| `X-Airs-Date` | `20260514T055649Z` |

### 响应

```json
{
  "token": "DBL0xg5YQ4SR3D9bpT4Y...",
  "expiresIn": "7200",
  "userId": "310419076713283592",
  "alias": "yzh"
}
```

### 常见错误

| 错误信息 | 原因 | 解决方法 |
|---|---|---|
| `invalid authorization header` | Authorization 头格式错误 | 检查格式 `AIRS-HMAC-SHA256 Credential=xxx, Signature=xxx` |
| `timestamp expired` | 时间戳超过 5 分钟 | 同步客户端时间，重新生成签名 |
| `signature verification failed` | 签名计算错误 | 检查 SK 与签名算法 |
| `invalid access key` | AK 不存在或已删除 | 检查 AK |
| `access key is disabled` | AK 已被禁用 | 联系管理员启用 |
| `user has been disabled` | 用户账号已被禁用 | 联系管理员解除 |

---

## 2. 获取当前用户信息

- **路径**：`GET /api/v1/users/token/userinfo`
- **认证**：`AIRS-Token`

jrun 用途：`create_experiment` 前取 `creatorId`（必填的 int64 proto 字段，
服务端不会从 token 推导）。

响应（外层包 `code`/`data`）：

```json
{
  "code": "...",
  "data": {
    "id": "310419076713283592",
    "realName": "张三",
    "alias": "zhangsan",
    "email": "...", "region": "...", "status": 1
  }
}
```

---

## 3. 获取账号加入的项目集

- **路径**：`POST /api/v1/projsets/select-joined`
- **认证**：`AIRS-Token`

Body：

```json
{"paging": {"order": "", "page": 1, "pageSize": 100, "sort": "", "total": 0}}
```

响应 `items[]`，每项的 `projsetInfo` 为项目集对象（含 `id`、`name`）。

jrun 用途：为 projset 作用域接口（`queue/select`、`job-snapshots/select`）
补全 projset 上下文 —— 未显式指定 projset 时遍历所有已加入的 projset。

---

## 4. 获取队列详情（配额/空闲资源）

- **路径**：`POST /api/v1/queue/select`
- **认证**：`AIRS-Token` + `AIRS-Proj-ID` + `AIRS-Projset-ID`
  （可选 `AIRS-Cluster-ID` / `AIRS-Zone-ID`）

Body：

```json
{
  "paging": {"order": "", "page": 1, "pageSize": 100, "sort": "", "total": 0},
  "queueId": "ef1017d7-59ad-4f00-9cac-a21bdf677973",
  "projId": "b121aae3-2198-4c91-9e72-355aa5a0e050",
  "projsetId": "a8b8f665-03ed-4d6a-9b2e-2cb236a59d83"
}
```

`queueId` / `projId` 可省略以列出 projset 下全部队列。

响应 `queueSummaryInfos[]`，关键字段：

- `id` / `name` / `projId` / `projsetId` / `clusterId` / `zoneId` —— 定位信息，
  jrun 的 `queue_location_info()` 从这里提取提交所需的 queue/quota/cluster/zone。
- `usedQuota.computing.<acceleratorModel>.{total,used}` —— 按优先级分桶的
  配额总量与用量，`PlatformAPI.queue_free_resources()` 据此算空闲卡数。
- `quotaDetailList[].resourceDetail.acceleratorModel` —— 队列的加速卡型号。
- `projsetName` / `projName` —— 项目集/项目名称；`quotaInfoList[].{clusterDisplayName,
  zoneQuotaInfoList[].zoneDisplayName}` —— 地域/可用区显示名。jrun 的 YAML
  使用 `project_set_name`、`project_name`、`region_name`、`zone_name`、
  `queue_name` 进行名称链解析，平台 ID 只保留在内部提交 payload 中。
- `status`：`QUEUE_STATUS_ACTIVE` 等。

<details>
<summary>完整响应示例</summary>

```json
{
  "paging": {"pageSize": 100, "page": 1, "total": 1},
  "queueSummaryInfos": [
    {
      "id": "ef1017d7-59ad-4f00-9cac-a21bdf677973",
      "name": "jiuding_airs1_h100",
      "quotaId": "8273b556-2773-4ba3-8bef-a1bcdfdcd0c3",
      "projsetId": "a8b8f665-03ed-4d6a-9b2e-2cb236a59d83",
      "projId": "b121aae3-2198-4c91-9e72-355aa5a0e050",
      "quotaDetailList": [
        {
          "resourceDetail": {
            "acceleratorModel": "J1_ADV_H1-SXM4-80GB",
            "acceleratorCount": 8, "cpuCores": 160, "memGib": 1800
          },
          "quotaId": "8273b556-2773-4ba3-8bef-a1bcdfdcd0c3",
          "priority": "high"
        }
      ],
      "usedQuota": {
        "computing": {
          "J1_ADV_H1-SXM4-80GB": {"total": {"high": 8}, "used": {}},
          "cpu": {"total": {"high": 160}, "used": {}},
          "memory": {"total": {"high": 1800}, "used": {}}
        }
      },
      "zoneId": "0922cfc7-8710-4c6d-9660-c9dd448ad1ac",
      "clusterId": "cb363735-bf90-47c3-9d7f-49f4965cbb27",
      "quotaInfoList": [
        {
          "clusterId": "cb363735-bf90-47c3-9d7f-49f4965cbb27",
          "clusterName": "sz2-calc1",
          "clusterDisplayName": "北京上庄",
          "zoneQuotaInfoList": [
            {"zoneId": "0922cfc7-8710-4c6d-9660-c9dd448ad1ac",
             "zoneName": "sz2-calc1-zonea", "zoneDisplayName": "可用区A"}
          ]
        }
      ],
      "queueType": "QUEUE_TYPE_DEFAULT",
      "status": "QUEUE_STATUS_ACTIVE"
    }
  ]
}
```

</details>

---

## 5. 查询 Job 列表（按队列 / 按实验）

- **路径**：`POST /api/v1/job/select`
- **认证**：`AIRS-Token` + `AIRS-Proj-ID` + `AIRS-Projset-ID`

jrun 实现：按实验过滤用于 `jrun experiment jobs`（`experiment_jobs()` /
`experiment_jobs_all()`，自动翻页）；按队列过滤目前未使用。

与 `queue/select` 一样是 projset 作用域：不传 projset 上下文返回 403。
`experiment_jobs_all` 在未显式传 `projset_id` 时会用一个 `pageSize=1` 的
探测请求遍历账号加入的 projset（`_probe_job_select_projset`），取第一个不
403 的 projset 翻页；全部 403 则抛出最后一个错误。

同一个接口支持多种过滤方式：

### 5.1 按队列过滤（平台「队列详情」页）

```json
{
  "paging": {"page": 1, "pageSize": 10, "sort": "", "order": ""},
  "queueId": "ef1017d7-59ad-4f00-9cac-a21bdf677973",
  "experimentType": 1,
  "job_state": [0, 1, 5, 6],
  "extendHeader": {
    "AIRS-Projset-ID": "a8b8f665-03ed-4d6a-9b2e-2cb236a59d83",
    "AIRS-Proj-ID": "b121aae3-2198-4c91-9e72-355aa5a0e050"
  }
}
```

### 5.2 按实验过滤（平台「实验详情」页，2026-09 抓包）

```json
{
  "paging": {"page": 1, "pageSize": 10, "sort": "", "order": ""},
  "archiveType": "ACTIVE_TYPE",
  "userId": null,
  "experimentType": 1,
  "experimentId": "df9a4985-b0f1-4531-a8cd-b7559b0f68ff",
  "projset_ids": null,
  "proj_ids": null,
  "clusterId": "",
  "zoneId": "",
  "creatorIds": null
}
```

### 响应

`jobInfos[]`，关键字段：

- `id` / `experimentId` / `experimentName` / `configName` / `configId`
- `status`：`Running` / `Pending` / `Approving` / `Cancelled` 等；`states` 为
  状态流转历史（JSON 字符串，State 码：0=Pending, 1=Running, 4=Cancelled,
  5=Starting, 6=Scheduling, 9=Approving）。`Approving` 是公共队列任务等待审批
  （2026-11-24 实测，在审批中 stop 后变为 `Cancelled`）；`Pending` 是平台上等待
  调度的真实 job。两者 jrun 都视为活跃、可停止的状态。
- `queueId` / `queueName` / `priority` / `roleInfos[]`（含资源请求明细）
- `clusterId` / `zoneId` / `clusterName` / `zoneName` 及显示名

<details>
<summary>单个 job 响应示例</summary>

```json
{
  "id": "ad8d3c9d-a005-42eb-b4b7-31e34baeda14",
  "status": "Running",
  "createdTime": "1788362902921",
  "creatorName": "王宁",
  "basicImage": "harbor.platform.baai-inner.ac.cn/library/vllm:v0.9.0.3",
  "command": "sleep 1440000000000000000",
  "queueName": "tech-platform_research_queue-dev",
  "priority": "medium",
  "roleInfos": [
    {
      "name": "Master",
      "replicas": 1,
      "resourceRequestDetail": {
        "acceleratorModel": "NVIDIA_A100-SXM4-40GB",
        "acceleratorCount": 8, "cpuCores": 180, "memGib": 760
      }
    }
  ],
  "experimentName": "vllm1",
  "experimentId": "f665672b-05c9-4d22-af59-d26cff75df62",
  "experimentType": 1,
  "states": "{\"States\": [{\"Time\": 1788362902921, \"State\": 0, \"Status\": \"Pending\"}]}",
  "configName": "config1-2-1",
  "zoneId": "da27a37a-ef05-4972-873b-97dc96e62688",
  "clusterId": "7ca8c76a-0974-4a69-a232-44e4de9254af",
  "clusterName": "dx-calc1",
  "queueStatus": "QUEUE_STATUS_ACTIVE"
}
```

</details>

## 5.3 公共资源池与闲时资源

- **公共队列列表**：`POST /api/v1/shared-queue/list`
- **项目集共享队列作业计数**：`POST /api/v1/shared-queue/job-count`
- **共享队列限制**：`POST /api/v1/shared-queue/config`
- **认证**：`AIRS-Token`，有对应值时附带 `AIRS-Proj-ID` / `AIRS-Projset-ID`

公共（public）和闲时（idle）是同一批共享资源池的两种提交方式：平台只有一份
共享资源池列表，`queueCategory` 表示提交模式。`jrun queues` 因此把每个共享
资源池在 `public` 和 `idle` 下各列一次，RUN/LIMIT 按模式分别统计。

### shared-queue/list

jrun 实现：`PlatformAPI.list_shared_queues()`（`jrun queues`、`find_queue` 的
public/idle 分支）。

Body（`projId` / `projsetId` / `queueCategory` 只在有值时发送）：

```json
{
  "paging": {"order": "", "page": 1, "pageSize": 100, "sort": "", "total": 0},
  "projId": "b121aae3-2198-4c91-9e72-355aa5a0e050",
  "projsetId": "a8b8f665-03ed-4d6a-9b2e-2cb236a59d83",
  "queueCategory": "QUEUE_CATEGORY_PUBLIC"
}
```

响应：行列表位于 `sharedQueues[]`（部分版本为 `items[]`）。每行可能是队列对象
本身，也可能包了一层 `{"queue": {...}}`，jrun 两种都接受，并把请求的
`queueCategory` 写回每行。队列对象的关键字段：

- `queueId`（或 `id`）/ `name` / `quotaId` / `sharedQuotaId` / `clusterId` /
  `zoneId` —— 提交所需定位信息，经 `queue_location_info()` 提取。
- `acceleratorModel` / `quota.acceleratorCount` / `used.acceleratorCount` ——
  `queue_free_resources()` 据此计算总量/已用/空闲卡数。
- `clusterName` / `clusterDisplayName` / `zoneName` / `zoneDisplayName`（或
  `quotaInfoList[]` 中的同名字段）—— 地域/可用区名称，用于 `region_name` /
  `zone_name` 匹配。
- `projsetName` / `projName` / `projId` / `projsetId` —— 若存在，用于
  `project_set_name` / `project_name` 过滤。

```json
{
  "sharedQueues": [
    {
      "queue": {
        "queueId": "5f0c...", "name": "Public-Pool-NVIDIA-V100",
        "quotaId": "9a1e...", "sharedQuotaId": "77b2...",
        "acceleratorModel": "NVIDIA_V100-PCIE-32GB",
        "quota": {"acceleratorCount": 64}, "used": {"acceleratorCount": 40},
        "clusterId": "7ca8...", "clusterDisplayName": "北京-大兴",
        "zoneId": "da27...", "zoneDisplayName": "可用区A"
      }
    }
  ]
}
```

提交时，`resourceConfig.queueCategory` 使用 `QUEUE_CATEGORY_PUBLIC` 或
`QUEUE_CATEGORY_IDLE`，并复用对应队列位置字段（`queue_id`、`quota_id`、
`shared_quota_id`、`cluster_id`、`zone_id`）。在 `airsctl experiment modify`
的 JSON 中，`queue_category` 是 protobuf 枚举数字：`dedicated=0`、
`public=1`、`idle=2`；jrun 负责转换。

### shared-queue/job-count

jrun 实现：`PlatformAPI.list_shared_queue_job_counts()`（`jrun queues` 的
RUN 列）。未显式传 `projset_id` 时使用账号第一个已加入的项目集。

Body（`projsetId` / `zoneId` 只在有值时发送）：

```json
{
  "paging": {"order": "", "page": 1, "pageSize": 100, "sort": "", "total": 0},
  "queueCategory": "QUEUE_CATEGORY_IDLE",
  "allProjsets": false,
  "onlyQueueing": false,
  "projsetId": "a8b8f665-03ed-4d6a-9b2e-2cb236a59d83",
  "zoneId": "da27a37a-ef05-4972-873b-97dc96e62688"
}
```

`queueCategory` 只能是 `QUEUE_CATEGORY_PUBLIC` 或 `QUEUE_CATEGORY_IDLE`。

响应：`items[]`（部分版本为 `sharedQueueJobCounts[]`），每行对应一个共享队列：
`queue.queueId`（或 `queue.id`）标识队列，`runningCount`、`queueingCount`
及审批计数为本项目集在该模式下的作业数。`jrun queues` 只读取 `queue.queueId`
和 `runningCount`；`PlatformAPI.shared_queue_budgets()`（auto 选队列和
scheduler）把所有行的 `runningCount + queueingCount` 相加，作为该模式已占用的
提交名额。

```json
{
  "items": [
    {"queue": {"queueId": "5f0c...", "name": "Public-Pool-NVIDIA-V100"},
     "runningCount": 2, "queueingCount": 1}
  ]
}
```

### shared-queue/config

jrun 实现：`PlatformAPI.shared_queue_limits()`（`jrun queues` 的 LIMIT 列）。
未显式传 `projset_id` 时使用账号第一个已加入的项目集。

Body：

```json
{"projId": "", "projsetId": "a8b8f665-03ed-4d6a-9b2e-2cb236a59d83"}
```

响应：`items[]`，每项为 `{configKey, configValue}`；`configValue` 可能是字符串，
jrun 能转成整数时转成整数。public 模式的上限取 `max_jobs_per_projset`，idle
模式取 `max_idle_jobs_per_projset`。

```json
{
  "items": [
    {"configKey": "max_jobs_per_projset", "configValue": "4"},
    {"configKey": "max_idle_jobs_per_projset", "configValue": "8"}
  ]
}
```

在 `jrun queues` 中，job-count 和 config 只用于展示；任一失败时仍会打印队列
列表，RUN/LIMIT 显示为 `-`。

共享队列的 `priority` 是固定的：公共队列只接受 `high`，闲时队列只接受 `low`，
其他值会被 `experiment modify` 拒绝（`QueueCategoryPriorityMismatch`：“This queue
category only accepts priority「high」/「low」”，2026-11-24 两者都已实测；专属队列不
限制）。jrun 对没写 `priority` 的公共/闲时任务自动写入要求的值
（`models.CATEGORY_PRIORITY`），写了冲突的值则在提交前报配置错误。注意 modify 会
克隆实验中第一个 config 作模板，所以不写这个值时会继承模板的 priority，换类别提交
就会被拒。

`queue_name: auto` 的提交和 scheduler 会把两者合起来算剩余名额
（`PlatformAPI.shared_queue_budgets()`）：剩余 = 上限 − Σ(`runningCount` +
`queueingCount`)，按 public/idle 模式分别计算；没有配置上限时视为不限。
auto 分配（`placement.py`）在此基础上，结合 queue/select 和 shared-queue/list
返回的空闲卡数（`queue_free_resources`）为每个任务选择队列；public 和 idle 是
同一个物理池，空卡只计算一次。scheduler 每轮轮询前读取一次剩余名额，名额为 0
时不启动该模式的等待任务；读取失败时直接尝试 `job run`。

---

## 6. 获取队列下开发环境

- **路径**：`POST /api/v1/workspaces/select`
- **认证**：`AIRS-Token` + `AIRS-Proj-ID` + `AIRS-Projset-ID`

Body：

```json
{
  "paging": {"page": 1, "pageSize": 10, "sort": "", "order": ""},
  "queueId": "d357ad08-4c85-471e-9df4-4f8b30066fcb",
  "queryStatus": [7, 1],
  "userId": null
}
```

响应 `items[]`：开发环境（workspace）对象，含 `name`、`status`（1=运行中）、
`quotaDetail.resourceDetail`（资源）、`podName`、`clusterId`/`zoneId` 等。
jrun 目前未使用此接口。

---

## 7. 创建实验

- **路径**：`POST /api/v1/experiment`
- **认证**：`AIRS-Token` + `AIRS-Proj-ID` + `AIRS-Projset-ID`
  (+ `AIRS-Cluster-ID` / `AIRS-Zone-ID`）

jrun 实现：`PlatformAPI.create_experiment()`。要点：

- `creatorId` 是必填 int64 字段，服务端不从 token 推导 —— 必须先调
  `userinfo` 获取。
- 必须带一个占位 `advanceConfigInfos`（config1）。jrun 首次实际提交时会
  用目标 YAML 的当前任务列表整体替换这个占位 config；占位项只是提供
  后续 modify 所需的平台结构模板。
- `resourceConfigList[0]` 中的 `queueId`/`queueName`/`clusterId`/`zoneId`/
  `acceleratorModel` 来自 `queue/select` 结果（`queue_location_info`）。
- `experimentType` 传字符串 `"1"`。

Body（jrun 实际发送的结构）：

```json
{
  "projId": "4c5ea2c6-02c3-4c28-a874-7b638f05a8e5",
  "projsetId": "67b36b88-c73b-4d9a-97b1-88bba474bd2f",
  "name": "test-create-experiment",
  "description": "",
  "experimentType": "1",
  "trainFrame": "PyTorch",
  "timeType": 60000,
  "duration": 0, "duration1": 0,
  "codeConfig": "0",
  "createdTime": "",
  "delivery": false,
  "storageInfo": [
    {"directoryType": 5, "accessMode": 1},
    {"directoryType": 4, "accessMode": 1},
    {"directoryType": 2, "accessMode": 1}
  ],
  "advanceConfigInfos": [
    {
      "codeConfig": "0",
      "configName": "config1",
      "command": "sleep 300",
      "hyperParameterPresent": [{"key": "ENV", "value": "ENV_VAL"}],
      "hyperParameter": {"ENV": "ENV_VAL"},
      "slotsPerWorker": 0,
      "resourceConfigList": [
        {
          "priority": "high",
          "queueId": "eeed1116-9721-4a5f-87d6-6daa56d5bd54",
          "queueName": "jiuding-test_airs-test1_queue1",
          "podRestartPolicy": "Never",
          "restartScope": "FailedInstanceOnly",
          "roleInfoList": [
            {
              "name": "Master", "replicas": 1,
              "resourceRegion": "CUSTOMIZED_ACCELERATOR",
              "resourceRequestDetail": {
                "acceleratorModel": "NVIDIA_A100-SXM4-40GB",
                "acceleratorCount": 0, "cpuCores": 2, "memGib": 2,
                "sharedMemGib": 1, "rdmaSharedCount": 0
              }
            }
          ],
          "basicImage": "harbor.platform.baai-inner.ac.cn/library/pytorch-sshd:23.08",
          "imageRegion": "PUBLIC",
          "zoneId": "da27a37a-ef05-4972-873b-97dc96e62688",
          "clusterId": "7ca8c76a-0974-4a69-a232-44e4de9254af",
          "clusterDisplayName": "北京-大兴",
          "zoneDisplayName": "可用区A"
        }
      ]
    }
  ],
  "experimentId": "",
  "creator": "于志浩",
  "creatorId": "319833125557370885",
  "profilerInfo": {"enabled": false, "level": "typical"},
  "heteroType": 1,
  "modelStorageInfo": {},
  "flag": false
}
```

响应：

```json
{"experimentId": "ea62a26e-2a63-43cf-9399-a15a5524139e", "experimentName": "test-create-experiment"}
```

---

## 8. 编辑实验

- **路径**：`PUT /api/v1/experiment/{experimentId}`
- **认证**：`AIRS-Token` + `AIRS-Proj-ID` + `AIRS-Projset-ID`
- **来源**：2026-09 平台前端抓包（实验详情页保存）

Body 结构为 `{"experimentId": ..., "createExperimentRequest": {...}}`，其中
`createExperimentRequest` 就是完整的实验对象（结构与创建接口的 body 基本一致，
外加 `experimentId`、`createdTime`、`updatedTime`、`totalJobCount`、
`projsetName`/`projName` 等回显字段），改哪里就整体提交哪里：

```json
{
  "experimentId": "c347c52e-5772-4cfc-b03b-cf3b0499f79e",
  "createExperimentRequest": {
    "experimentId": "c347c52e-5772-4cfc-b03b-cf3b0499f79e",
    "experimentName": "test-job",
    "description": "",
    "trainFrame": "PyTorch",
    "restartPolicy": "Never",
    "advanceConfigInfos": [
      {
        "configName": "config1",
        "command": "sleep 360",
        "resourceConfigList": ["...同创建接口..."]
      }
    ],
    "creatorId": "310419076713283592",
    "projsetId": "00b35824-fc96-4e8b-ba38-5c9ef2acadd0",
    "projId": "2d440447-1c37-4a90-9ced-86ea7db203fc",
    "experimentType": 1
  }
}
```

注意：这是**全量替换**语义 —— `advanceConfigInfos` 会被 body 中的列表整体覆盖，
所以「删除实验里的某个 config」也可以通过此接口提交不含该 config 的列表实现。
jrun 的 `PlatformClient.modify_configs()` 通过 `airsctl experiment modify -f`
实现同样的全量替换：每次 submit 只写入当前选中的 config，并在同一次
modify 成功后用 `run_config()` 逐个执行 `airsctl job run`（输出中找不到 job
ID 视为失败）。被省略的 config 会从 experiment 工作集移除，但不会因此
stop/cancel 已经启动的 job；只有被覆盖（同名重新提交）的任务会在 modify 之前
由 `tracking.release_job` 通过 `airsctl job stop` 停止旧 job。job ID 和历史
状态由本地 `.jrun/jobs.json` 维护。`remove_configs()` 仍保留给显式的
`jrun job remove` 生命周期操作，submit 本身不会调用它；remove 会先 `job stop`
仍在进行中的 job 并等待其结束，再 `job cancel` 归档（cancel 只对已结束的 job 生效）。

airsctl 侧的数据格式：`airsctl experiment list -e <id>` / `-N <name>` 返回的
是**裸的 snake_case 实验文档**（顶层直接是 `experiment_id`、
`experiment_name`、`advance_config_infos`、`resource_config_list`、
`role_info_list` 等，没有 `data` 包装，已实测确认），`experiment modify -f`
也接受同样的 snake_case 文档。因此 `platform.py` 只处理 snake_case；这里的
camelCase 字段名只适用于 REST API。按名称查找时 jrun 要求返回文档的
`experiment_name` 与请求名称精确一致。

因此，使用 jrun 的 experiment 应视为当前 YAML 的专属容器；共享 experiment
会在每次 submit 时被完整替换。

---

## 9. 删除实验

- **路径**：`POST /api/v1/experiment/delete`
- **认证**：`AIRS-Token` + `AIRS-Proj-ID` + `AIRS-Projset-ID`
- **来源**：2026-09 平台前端抓包（实验管理列表页删除操作）

Body（支持批量）：

```json
{
  "experimentItems": [
    {
      "experimentId": "c347c52e-5772-4cfc-b03b-cf3b0499f79e",
      "experimentName": "test-job",
      "creatorId": "310419076713283592",
      "archive": 1
    }
  ]
}
```

> **注意**：body 中 `"archive": 1` 表明平台侧是「归档」语义而非物理删除
> （前端「实验管理」页有 `archive=false` 查询参数）。对已提交 job 的影响
> 未实测确认，`jrun experiment delete` 的命令 help 中向用户注明了归档语义。

jrun 实现：`PlatformAPI.delete_experiment()`（`jrun experiment delete`）。
`creatorId` 为必填字段，服务端不从 token 推导，调用前经 `userinfo` 解析
（同 `create_experiment`）。projset 上下文缺省时回落到账号第一个已加入的
projset（同 `job_snapshots` 的 `_default_projset_id`）。

---

## 10. 任务明细（资源利用率快照）

- **路径**：`POST /api/v1/job-snapshots/select`
- **认证**：`AIRS-Token` + `AIRS-Proj-ID` + `AIRS-Projset-ID`（projset 作用域，缺省 403）

Body：

```json
{
  "paging": {"page": 1, "pageSize": 10, "sort": "", "order": ""},
  "timeBegin": 1788192000000,
  "timeEnd": 1788537599000,
  "projsetId": null,
  "projId": "4c5ea2c6-02c3-4c28-a874-7b638f05a8e5",
  "queueIds": [],
  "clusterIds": ["7ca8c76a-0974-4a69-a232-44e4de9254af"],
  "zoneIds": ["da27a37a-ef05-4972-873b-97dc96e62688"],
  "ownerName": "luna-openclaw"
}
```

`timeBegin` / `timeEnd` / `ownerName` 可选。jrun 实现：`job_snapshots()` /
`job_snapshots_all()`（自动翻页）。

响应 `items[]`，关键字段：

- `jobId` / `ownerName` / `ownerType`（job 或 workspace）/ `queueName`
- `state` / `stateName`：`1=Running`、`4=Cancelled` 等（与 job/select 的
  State 码一致）
- `beginTime` / `endTime`（毫秒时间戳字符串）
- `acceleratorType` / `acceleratorReq` / `acceleratorUtil` / `fbmemUtil` /
  `cpuReq` / `cpuUtil` / `memReqGib` / `memUtil` —— 利用率字段，
  `jrun gpu-usage` 的数据来源；`-1` 表示无数据（如无卡任务）

---

## 11. 任务运行时环境变量（实测）

2026-11-24 用一个探测任务实测：jrun 提交到闲时 `Public-Pool-NVIDIA-V100`，单节点、
0 卡，镜像 `harbor.baai.ac.cn/llm-safety-center/enviroments:agents-v1.0.0`，
任务把 `env` 写到共享存储。下表只列平台注入的变量；`CUDA_VERSION`、
`NCCL_VERSION`、`VLLM_*`、`UV_*`、`PIP_INDEX_URL`、`HOME` 等来自镜像，换镜像会变，
不要依赖。

| 变量 | 实测值 / 含义 |
|---|---|
| `AIRS_WORKLOAD_TYPE` | `experiment`（实验任务）；开发环境（workspace）中为 `workspace` |
| `AIRS_EXPERIMENT` | `job` |
| `AIRS_EXPERIMENT_ID` | 所属实验 UUID |
| `AIRS_WORKSPACE_ID` | 只在开发环境中存在，实验任务中没有 |
| `HOSTNAME` | `job-<job UUID>-master-0`；没有单独的 job ID 变量，只能从这里解析 |
| `MASTER_ADDR` / `MASTER_PORT` | master pod 主机名 / `23456` |
| `RANK` / `WORLD_SIZE` | 单节点实测为 `0` / `1` |
| `PET_NNODES` / `PET_NODE_RANK` / `PET_WORLD_SIZE` | torchrun（`torch.distributed.run`）读取的节点数 / 节点序号 / 节点总数，单节点为 `1` / `0` / `1` |
| `PET_MASTER_ADDR` / `PET_MASTER_PORT` | 同 `MASTER_ADDR` / `MASTER_PORT`，供 torchrun 使用 |
| `PET_NPROC_PER_NODE` | `auto`：不写 `--nproc_per_node` 时 torchrun 按可见 GPU 数启动进程 |
| `NVIDIA_VISIBLE_DEVICES` | 0 卡任务实测为 `all` |
| `PATH` | 前置 `/client-tools`（内含 `airsctl`） |

jrun 写入的变量也会出现在任务环境中（实测）：config 的 `envs:` 写入
`hyper_parameter`，平台把它们作为环境变量注入；`queue_name: auto` 分配后的任务
还带有 `JRUN_QUEUE_NAME`、`JRUN_ACCELERATOR_MODEL`、`JRUN_ACCELERATOR_COUNT`。

平台不提供：job ID 变量、队列名、卡型和卡数（因此 jrun 对 auto 任务注入
`JRUN_*`）。0 卡任务的镜像里没有 `nvidia-smi`。多节点任务的 `RANK`/`PET_*`
取值尚未实测，预期是每个节点不同的节点序号。

---

## 附：jrun 接口使用对照表

| 接口 | jrun 模块/函数 | 用途 |
|---|---|---|
| `POST /users/token/exchange` | `api.py` `exchange_token` / `compute_signature` | AK/SK 换 Token |
| `GET /users/token/userinfo` | `api.py` `get_userinfo` | 取 `creatorId`（建实验前） |
| `POST /projsets/select-joined` | `api.py` `list_joined_projsets` / `resolve_project_context` | projset 上下文补全；`project_set_name`/`project_name` → ID |
| `POST /queue/select` | `api.py` `list_queues` / `list_category_queues` / `find_queue` / `queue_free_resources`；`placement.py` `collect_candidates` | `jrun queues`、队列名→ID 解析、空闲配额、auto 选队列的专属候选 |
| `POST /shared-queue/list` | `api.py` `list_shared_queues` / `list_category_queues`；`placement.py` `collect_candidates` | 公共/闲时队列与卡型、配额、集群/可用区查询、auto 选队列的共享候选 |
| `POST /shared-queue/job-count` | `api.py` `list_shared_queue_job_counts` / `shared_queue_budgets` | 公共/闲时项目运行数和排队数；auto 选队列和 scheduler 的剩余提交名额 |
| `POST /shared-queue/config` | `api.py` `shared_queue_limits` / `shared_queue_budgets` | 公共/闲时项目集上限；剩余提交名额 |
| `POST /job/select` | `api.py` `experiment_jobs` / `experiment_jobs_all` | `jrun experiment jobs`（按实验过滤；按队列过滤未使用） |
| `POST /workspaces/select` | 未使用 | — |
| `POST /experiment` | `api.py` `create_experiment` | `jrun experiment create` / submit 自动建实验 |
| `PUT /experiment/{id}` | 未使用（config 工作集走 `PlatformClient.modify_configs` + airsctl `experiment modify`） | 全量编辑实验 |
| `POST /experiment/delete` | `api.py` `delete_experiment` | `jrun experiment delete`（归档语义） |
| `POST /job-snapshots/select` | `api.py` `job_snapshots` / `job_snapshots_all` | `jrun gpu-usage` |

认证来源优先级（`api.py` `_resolve_token`）：`AIRS_AK`/`AIRS_SK` 环境变量 →
`~/.jrun/auth.json`（`jrun login`）→ `/etc/accesskey/{user-ak,user-sk}`
（base64，k8s secret 挂载）→ `/tmp/.airs/.token`（airsctl 缓存，直接用）。

HTTP 实现注意：一律绕过环境代理（`ProxyHandler({})`），否则平台访问会失败。
