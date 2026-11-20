# 接口

鉴权：所有接口通过 `Authorization: Bearer <token>` 或 `X-Client-Token` 识别主体。
不带令牌视为实验室内部角色（仅适用于受控内网/演示）。演示令牌：

| 角色 | 令牌 | 可见范围 |
|---|---|---|
| 实验室 | `demo-token-lab` | 全部批次、原始样品编号、行级归属 |
| 委托方 CL-ACME | `demo-token-acme` | 仅归属本机构的批次与行 |
| 委托方 CL-GREEN | `demo-token-green` | 仅归属本机构的批次与行 |

`POST /api/run`：{"text":"CSV内容"}，可选 `client_id`（整文件归属）与
`line_clients`（`{"物理行号":"委托方编号"}` 行级覆盖，仅实验室可用）。
委托方调用时整文件自动归属本机构，不得代其他机构登记。未注册委托方返回 422
`CLIENT_UNKNOWN`，且不落库任何批次。同规则版本与输入摘要只产生一条批次；
已确认的归属不会被后续无归属的重复提交覆盖。

`GET/POST /api/1` 至 `/api/7`：独立样例或自定义文件计算。各模块返回 report、
quarantine、balance。

`POST /api/review`：id、整数 revision、decision 取通过或退回。旧版本 409；
阻断项不能通过。仅实验室角色可调用，委托方调用返回 403。

`GET /api/runs`：实验室返回全部批次；委托方仅返回含本机构行的批次，且归属分布
字段被剥离。`GET /api/runs/{id}`：委托方访问非本机构或未归属批次统一返回 404，
避免探测其他机构批次是否存在。

`GET /api/export/{id}`：
- 实验室：全量证据 CSV（`evidence-lab.csv`），保留原始样品编号、归属与全部隔离原因；
- 委托方：仅含本机构行的证据 CSV（`evidence-<client>.csv`），原始样品编号替换为
  机构内稳定别名，隔离原因随 reason 列输出可核对。存在未归属行时返回 409
  `UNASSIGNED_ROWS` 阻止外发；本机构在该批次无任何归属行时返回 403 `NO_OWNERSHIP`。
  重试同一导出逐字节一致，不新增别名、不新增导出登记。

`GET /api/exports`：导出登记。委托方只看到本机构的登记。
`GET /api/clients`：委托方名册，仅实验室可用。
`GET /health`：存活检查。

错误：结构/数值 422，版本冲突 409，不存在/无权 404，认证失败 401，权限不足 403，
文件过大 413。逐行异常进入 quarantine。
