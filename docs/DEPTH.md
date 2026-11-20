# 深度登记 质谱空白扣除与检出限复算

- 规模：中型
- 核心规则：最小二乘标曲必须满秩，空白扣除保留负值，浓度按稀释倍数复算；低于检出限保留限定符

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 仪器接收 | 运行仪器接收并核对证据 | `POST /api/1` | `app/modules/m1_intake/rules.py` | `tests/test_intake.py` |
| M2 内标归一 | 运行内标归一并核对证据 | `POST /api/2` | `app/modules/m2_internal/rules.py` | `tests/test_internal.py` |
| M3 标曲拟合 | 运行标曲拟合并核对证据 | `POST /api/3` | `app/modules/m3_curve/rules.py` | `tests/test_curve.py` |
| M4 空白扣除 | 运行空白扣除并核对证据 | `POST /api/4` | `app/modules/m4_blank/rules.py` | `tests/test_blank.py` |
| M5 浓度复算 | 运行浓度复算并核对证据 | `POST /api/5` | `app/modules/m5_concentration/rules.py` | `tests/test_concentration.py` |
| M6 质控判定 | 运行质控判定并核对证据 | `POST /api/6` | `app/modules/m6_quality/rules.py` | `tests/test_quality.py` |
| M7 结果复核 | 运行结果复核并核对证据 | `POST /api/7` | `app/modules/m7_review/rules.py` | `tests/test_review.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | GB18030中文 | 静默采信错误值 | 编码回退链 | `app/modules/m1_intake/rules.py` | `tests/test_intake.py::test_intake_encoding` |
| TRAP-05 | 不齐列 | 静默采信错误值 | 列数校验隔离 | `app/modules/m1_intake/rules.py` | `tests/test_intake.py::test_intake_column_isolation` |
| TRAP-06 | 全角与千分位 | 静默采信错误值 | NFKC后Decimal | `app/modules/m2_internal/rules.py` | `tests/test_internal.py::test_internal_fullwidth` |
| TRAP-07 | 空值哨兵 | 静默采信错误值 | 拒绝参与运算 | `app/modules/m2_internal/rules.py` | `tests/test_internal.py::test_internal_sentinel` |
| TRAP-10 | 浮点误差 | 静默采信错误值 | Decimal精确计算 | `app/modules/m2_internal/rules.py` | `tests/test_internal.py::test_internal_decimal_exact` |
| TRAP-14 | 业务键冲突 | 静默采信错误值 | 保留首条并隔离冲突 | `app/modules/m1_intake/rules.py` | `tests/test_intake.py::test_intake_conflicting_key` |
| TRAP-15 | 单位混合 | 静默采信错误值 | 浓度单位归一 | `app/modules/m2_internal/rules.py` | `tests/test_internal.py::test_internal_units` |
| TRAP-19 | 多个质控标记 | 静默采信错误值 | 显式优先级 | `app/modules/m6_quality/rules.py` | `tests/test_quality.py::test_quality_priority` |
| TRAP-20 | 归属猜错整行泄露 | 按猜测机构外发 | 未归属显式标记并阻断外发 | `app/identity.py`, `app/store.py` | `tests/test_attribution.py::test_client_export_blocked_when_unassigned_rows_exist` |
| TRAP-21 | 跨机构凭编号串联历史 | 全机构共用一套编号 | 每委托方独立别名序列，原编号仅内部留存 | `app/identity.py::mint_alias` | `tests/test_attribution.py::test_clients_have_independent_alias_sequences` |
| TRAP-22 | 换号对不上旧记录 | 每次导出重新编号 | 别名持久化＋唯一约束，同原编号恒得同别名 | `app/identity.py::mint_alias` | `tests/test_attribution.py::test_alias_stable_across_runs_for_same_client` |
| TRAP-23 | 重试产生新编号/新登记 | 导出即铸号即登记 | (run_id,client_id) 主键去重，重试零新增 | `app/store.py::prepare_client_export` | `tests/test_attribution.py::test_retry_export_creates_no_new_alias_or_ledger_entry` |
| TRAP-24 | 旧数据升级后无法复核 | 强填归属或丢弃旧记录 | 无归属列按 UNASSIGNED 处理，实验室全量可读 | `app/store.py::_schema`, `get` | `tests/test_attribution.py::test_legacy_run_without_attribution_still_reviewable_by_lab` |
| TRAP-25 | 接口探测其他机构批次 | 返回 403/422 暴露存在性 | 委托方视角统一 404，历史列表按机构过滤 | `app/main.py`, `app/security.py` | `tests/test_attribution.py::test_client_cannot_see_other_clients_run` |

## 对账规则

输入记录=采信记录+隔离记录+空行；重复记录归隔离；算法输出另记业务单位，不冒充输入行数。金额/浓度/时长使用Decimal；证据含原始行号。

委托方证据对账只覆盖本机构归属行：manifest 中 rows_result 与 rows_quarantine
之和等于该机构可见行数；其他机构行不计入，未归属行以阻断代替计数外发。
实验室全量证据额外含 owner 列，可核对每行归属与未归属清单。

## 已知不覆盖

不支持超过2MB输入，不执行生产自动放行；异常值需人员复核。
演示令牌为静态种子，生产环境须改为密钥管理下发并轮换；委托方名册暂不提供
自助注册接口，新增机构由数据库迁移维护。
