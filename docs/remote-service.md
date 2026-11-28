# 远程提交服务（jrun-server）

> 代码：客户端在 `jrun/remote.py`（`RemoteClient` / `RemoteRegistry`）及
> `jrun/submission.py`（`split_remote` / `submit_remote`）、`jrun/tracking.py`
> （远程 job 的状态同步 / stop / remove）；服务端是独立包 `server/`
> （`jrun_server`，只通过子进程调用 `jrun` CLI，不 import jrun）。
> 修改协议或 JSON 结构时，本文、客户端、服务端和两侧测试需同时更新。

## 1. 背景与模型

部分用户（如实习生）用自己的 key 只能提交专属队列和闲时（idle）队列，无权
以公共（public）模式提交。正式员工可以。jrun-server 用一个正式员工账号的
AK/SK 运行（docker，非九鼎机器即可），替验证过身份的用户提交 **public** 任务：

```
本地 jrun ──HTTP JSON + X-Jrun-Airs-Token: <调用者自己的 token>──► jrun-server (服务账号 AK/SK)
  │ auto 选队列、按类别拆分                                      │ 以调用者身份: jrun whoami / jrun queues
  │ public → 远程，其余本地提交                                  │ 改写配置、dry-run 规划、逐 job 鉴权
  │ 记录 remote: <url>，status/stop/remove 走服务                │ 以服务账号: jrun submit --json
  └──────────────────────────────────────────────────────────►  │ 每个调用者独立工作区 /data/users/<user id>
```

- 任务在平台上属于**服务账号**（实验也建在服务账号下），所以调用者自己的
  key 无法 stop/remove，只能通过服务操作；服务只允许操作调用者本人在服务端
  工作区里提交过的 job。
- 服务端每个调用者一个 jrun 工作区（`jrun init` 的项目目录），其 `.jrun/jobs.json`
  就是“哪个 job 属于谁”的权威记录；`parallel_trials` / 等待名额的任务由服务端
  容器内的 jrun scheduler 处理。

## 2. 客户端用法（YAML 开启）

```yaml
remote:
  url: http://jrun-svc.example.com:8080   # 也可以写 $JRUN_REMOTE_URL
  categories: [public]                     # 默认；哪些类别的任务走远程
```

- 有 `remote:` 块时，`jrun submit` 先做 auto 选队列，再按每个任务**最终的**队列
  类别拆分（`submission.split_remote`）：类别在 `categories` 中的走远程，其余照常
  用本地凭据提交（本地实验）。`--dry-run` 会显示每个任务的 `Route:`。
- 走远程的任务必须能确定 `project_set_name`（任务级或 experiment 级），否则提交前报
  `ConfigError`。仍然需要 `experiment:` 块；服务端实验名为 `<调用者 alias>-<name>`。
- 发送给服务的是 **变量替换后的配置**（`ConfigLoader.config`，去掉 `remote:`）加
  `only=[选中的任务名]`；服务端会重新展开网格，只提交这些名字（`jrun submit --only`）。
  auto 任务以原样发送（仍是 `queue_name: auto`），服务端把类别收窄到它提交的类别后
  重新选队列、必要时由服务端 scheduler 等待名额。
- 覆盖：同名旧任务若是本地任务，先在本地 `release_job` 停止；若是远程任务，由服务端
  `jrun submit --overwrite` 替换。服务接受后才删除本地旧映射。
- 追踪：远程任务在本地 `.jrun/jobs.json` 中带 `remote: <url>` 及服务端实验
  id/name。`jrun job status` 每个服务 URL 调一次 `/v1/jobs/status` 刷新状态；服务端
  scheduler 启动了的 `queued-*` 任务按 (search, name) 换成真实 job ID。
  `job stop` / `job remove` 对远程任务调用服务的 stop / remove。服务不可达时：
  status 打印 warning；stop 尽力而为；覆盖时的 release 严格失败；remove 保留追踪记录。
- 调用者 token：`PlatformAPI().token()`（与本地 jrun 相同的凭据来源：
  `AIRS_TOKEN` → `AIRS_AK/AIRS_SK` → `~/.jrun/auth.json` → `/etc/accesskey` →
  `/tmp/.airs/.token`），只在发生远程调用时解析。

## 3. 协议

所有请求：`POST <url>/v1/...`，JSON body，请求头
`X-Jrun-Airs-Token: <调用者 token>`、`X-Jrun-Protocol: 1`。错误：HTTP 状态码 +
`{"error": "..."}`（400 请求格式错误、401 token 无效、403 鉴权/配置不允许、404 路径
不存在、413 body 过大、500 jrun 执行失败）。`GET /healthz` → `{"ok": true}`（无需 token）。

| 路径 | 请求 body | 响应 |
|---|---|---|
| `/v1/whoami` | `{}` | `{id, alias, identity_type, project_sets, categories}`（`project_sets` = 调用者已加入且服务允许的项目集） |
| `/v1/submit` | `{config, only: [name...], overwrite: true}` | 服务端 `jrun submit --json` 的结果（见 §4） |
| `/v1/jobs/status` | `{}` | `{"jobs": [...]}`：调用者工作区全部 job（`jrun job status --json -n 0`，已刷新） |
| `/v1/jobs/stop` | `{job_ids: [...]}` | `{"results": {job_id: {ok, status?, error?, missing?}}}` |
| `/v1/jobs/remove` | `{job_ids: [...]}` | `{"removed": [...], "still_active": {id: status}, "missing": [...]}` |

`only` / `job_ids` 每项须匹配 `^[A-Za-z0-9][A-Za-z0-9._:-]*$`（防止被当成命令行选项），
每次最多 500 项。同一调用者的请求串行执行（`experiment modify` 是整表替换）。

## 4. jrun `--json` 输出（服务端依赖的契约）

服务端只解析这些 JSON；改动字段必须同步 `server/` 和本文。`--json` 模式下 stdout 只有
JSON，其余信息走 stderr，且**不会交互**：确认类提示按 `--yes` 处理，已存在任务在没有
`-o` 时视为跳过。

- `jrun whoami --json`：`{id, alias, real_name, identity_type, active, projsets: [{id, name}]}`。
  `active` = userinfo 的 `disabledTime` 为空或 `"0"`。
- `jrun queues --json`：`{"queues": [{category, queue_name, queue_id, region_name, zone_name,
  cluster_id, zone_id, project_set_name, projset_id, project_name, proj_id,
  resources: [{model, total, used, free}], running, limit, waiting}]}`。
  共享资源池行通常没有项目集字段；用 `--projset-id` 限定调用者视角。
- `jrun submit --json`（含 `--dry-run`）：
  ```json
  {"dry_run": false, "search_name": "s", "experiment": {"name": "...", "id": "..."},
   "jobs": [{"name", "params", "route": "local|remote", "auto", "accelerator_choices",
             "queue_name", "queue_category", "project_set_name", "project_name",
             "region_name", "zone_name",
             "placement": {"status": "start|queue|wait", "queue_name", "queue_category",
                           "model", "count"} | null,
             "job_id", "status", "search_name", "experiment_id", "experiment_name",
             "platform", "remote", "submitted_at"}],
   "not_submitted": ["..."], "errors": ["..."]}
  ```
  dry-run 时 `jobs` 是全部计划任务（无 `job_id`）；正式提交时 `jobs` 只包含本次新出现的
  tracker 条目（含 scheduler 的 `queued-*`），未提交的名字在 `not_submitted`。
  `auto` / `accelerator_choices` 取自 auto 选队列**之前**的任务。
- `jrun job status --json`：`{"jobs": [{job_id, name, search_name, status, params,
  submitted_at, experiment_id, experiment_name, platform, remote}], "searches": {name:
  {config_file, submitted_at, job_ids, scheduler_pid}}}`（忽略 `-n`）。
- `jrun job stop --json`：`{"results": {job_id: {ok, name, status, error?}}}`；未追踪的
  名字/ID 返回 `{ok: false, missing: true}`，退出码 0。
- `jrun job remove --json`：`{"removed", "still_active", "missing"}`，未追踪时 `missing`。

## 5. 服务端鉴权

1. **身份**：`AIRS_TOKEN=<调用者 token> jrun whoami --json`（服务账号的 AK/SK 从子进程
   环境中移除）。token 无效 → 401；账号停用 → 403；设置了 `JRUN_SERVE_USERS` 时 alias
   必须在列表中。结果按 `sha256(token)` 缓存 5 分钟（`JRUN_SERVE_IDENTITY_TTL`）。
2. **配置改写**（`jrun_server/rewrite.py`，白名单，未列出的键一律 403）：
   - 顶层只允许 `experiment` / `job` / `search`（`remote` 被丢弃）。
   - experiment：身份、位置、`train_frame`、`restart_policy`、`mounts`、
     `native_cluster`、`profiler_info`；**不允许** `platform_overrides`、
     `model_storage_info`、`storage_info`。
   - job / job_template：`name`、`command(s)`、`envs`、`image`、`image_region`、
     `resource_config`、`run_options`、`train_frame`、`restart_policy`、`selected_nodes`；
     **不允许** `code`、`git_*`、`gitee`、`dataset`、`model_storage_info(_items)`、
     `platform_overrides`（它们可能触达服务账号的私有资产）。
   - resource_config / roles：资源与位置字段；**不允许** `platform_overrides` 及任何
     未知键（jrun 会把未知键当作平台原始字段透传）。
   - `image_region` 必须是 PUBLIC（不能使用服务账号的私有镜像）。
   - 实验名改为 `<alias>-<name>`；`mounts.home` 强制为 `false`（否则会挂载服务账号的
     home），其余挂载保持；队列类别收窄到 `JRUN_SERVE_CATEGORIES`（默认 public）。
   - 写文件时把所有 `$` 转义为 `$$`：收到的配置已在客户端完成变量替换，服务端 jrun
     不能再替换一次（否则 `$AIRS_SK` 这类写法会展开成服务账号的密钥）。
3. **规划**：以服务账号运行 `jrun submit <cfg> --dry-run --json --only=...`。
4. **逐 job 检查**（`jrun_server/access.py`）：
   - 类别在 `JRUN_SERVE_CATEGORIES` 中；
   - 有 `project_set_name`，且在 `JRUN_SERVE_PROJECT_SETS` 中，且调用者已加入；
   - 队列可见：`AIRS_TOKEN=<调用者> jrun queues --category idle --projset-id <id> --json`
     必须包含目标资源池（公共和闲时是同一批物理资源池的两种提交模式，调用者能以 idle
     看到即说明有该资源池的访问权限）。auto 任务要求服务账号在该项目集中所有提供其
     卡型的公共资源池都对调用者可见（实际选择在真正提交时才发生）。
5. **提交**：以服务账号运行 `jrun submit <cfg> --yes --json --only=... [--overwrite]`，
   在调用者工作区中、持有该调用者的锁。

## 6. 部署

```bash
cd server
cp /client-tools/airsctl ./airsctl      # 从九鼎开发环境拷贝 airsctl（已 gitignore）
cp .env.example .env                    # 填 AIRS_AK / AIRS_SK / JRUN_SERVE_PROJECT_SETS
docker compose up -d --build            # 或 docker build -t jrun-server . && docker run ...
```

```bash
docker run -d --name jrun-server -p 8080:8080 -v jrun-data:/data \
  -e AIRS_AK=... -e AIRS_SK=... -e JRUN_SERVE_PROJECT_SETS=baai-safety jrun-server
```

- 镜像从 GitHub 安装 jrun（`--build-arg JRUN_REF=<分支/标签>`，默认 `main`）。服务依赖
  本文 §4 的 `--json` 输出，部署的 jrun 版本需包含这些功能。
- 环境变量：`AIRS_AK`/`AIRS_SK`（必填，服务账号）、`JRUN_SERVE_PROJECT_SETS`（必填，
  逗号分隔，服务账号须已加入）、`JRUN_SERVE_USERS`（可选 alias 白名单）、
  `JRUN_SERVE_CATEGORIES`（默认 `public`）、`JRUN_SERVE_DATA`（默认 `/data`）、
  `JRUN_SERVE_PORT`、`JRUN_AIRSCTL_GATEWAY`（默认 `http://platform-multi.baai.ac.cn`）、
  `JRUN_AIRSCTL_PROJSET_ID`/`JRUN_AIRSCTL_PROJ_ID`（airsctl 默认项目上下文，可选）。
- 容器入口 `docker-entrypoint.sh` 模拟九鼎 pod：把 AK/SK 以 base64 写入
  `/etc/accesskey/user-{ak,sk}`，把 `gateway-base-url` 写入
  `/etc/downwardapi/annotations`；jrun 在 `JRUN_AIRSCTL_GATEWAY` 设置时为每次 airsctl
  调用追加 `-u <gateway> -P <projset> -p <project>`（见 `docs/airsctl.md`）。
- 启动检查：`jrun`、`airsctl` 在 PATH 上；服务账号 `jrun whoami` 成功；服务账号已加入
  所有 `JRUN_SERVE_PROJECT_SETS`。
- 本地开发：`cd server && uv run --extra dev pytest`；在仓库根目录运行
  `uv run pytest server/tests` 会额外执行白名单与 jrun 模型字段的一致性检查。

## 7. 安全注意事项与限制

- 调用者的 token（有效期 2 小时）会发送给服务。服务前面务必加 TLS（反向代理），或只在
  内网暴露。服务不持久化调用者 token。
- 任务以服务账号身份运行：其 project/projset 存储挂载按服务账号权限；home 已强制关闭。
- 2026-11-27 实测：经公网网关（`-u`）的 airsctl 在不可达的集群内 endpoint 下仍可完成
  experiment list/modify、job run/list/stop/cancel；`-P/-p` 生效；在 pod 上 `-t` 会被
  忽略（airsctl 优先使用 `/etc/accesskey` / `/tmp/.airs/.token`）。在真正的非九鼎机器
  上的容器测试需部署后验证。
- 容器重启会结束正在运行的 scheduler（服务端 `parallel_trials` / 等待名额的任务停在
  `Queued`），目前没有自动恢复。
