# 药店健康陪伴边界台

连锁药店的用药陪伴需要在"能服务"与"不能越界"之间有一条可审计的边界。
本项目是边界台的服务端领域内核：把**顾客授权版本、药师执业资质、药品计划、
依从反馈、异常信号、转诊建议、应急保供批次、门店交接**放在同一条审计链上运行，
并在每一次处理记录前执行"资质 × 授权"双重覆盖判定。

> 边界声明：药店只做用药陪伴、依从记录、转诊建议与保供安排，
> **不能替代诊断和治疗**。任何系统响应都不得包含自行生成的诊断结论。

## 边界规则（与业务要求逐条对应）

1. **双重覆盖**：只有资质（事项范围、有效期、所属门店）与授权（事项、渠道、
   未撤回、版本一致）都覆盖当前事项的药师才能处理记录；任一不满足即拒绝并给出原因码。
2. **风险阈值**：反馈命中规则且 severity ≥ 4 时，自动建议**立即暂停计划并转交人工**，
   同时生成转诊建议；severity = 3 只转人工不暂停。系统不自行诊疗。
3. **过去不可改**：新信息只能取消/重排**未发送**提醒；已发送提醒在数据库层禁止改写。
4. **撤回但保留**：撤回授权停止全部未来联系（取消未发送提醒），已发送记录原样保留；
   法规要求保留的最小事实只有合规角色可查。
5. **销售无权关单**：销售目标负责人不能批准异常关闭（尝试也会留审计）。
6. **重复反馈幂等**：同一 `idempotency_key` 的重复反馈沿用首条回执 `receipt_no`。
7. **标识隔离**：同一标识（手机号/会员号）关联出不同顾客的健康内容时自动隔离，
   合规隐私复核前阻断该标识下的新提醒与反馈。
8. **交接与中断续跑**：交接完成后保供责任转移到承接门店；到期提醒由调度器按
   原计划时间继续捞取，进程中断/换实例后重跑 `run_due` 即可续跑，发送前实时重判覆盖。
9. **三视图各取所需**：顾客（计划来源、未提醒原因、转诊进度、保供责任）、
   药师（+信号/反馈明细）、合规（+完整哈希链审计与保留事实）看到各自权限内的内容。
10. **统一审计链**：所有变更进入同一条 SHA-256 哈希链，篡改任意历史载荷即断链，
    `Store.verify_audit_chain()` 可复验。

## 目录

- `src/pharmacy_care_boundary/domain.py` — 领域记录与状态常量。
- `src/pharmacy_care_boundary/policy.py` — 双重覆盖、风险阈值、角色限制、视图白名单、诊断守卫。
- `src/pharmacy_care_boundary/store.py` — SQLite 表、只追加哈希链审计、不可变更新保护。
- `src/pharmacy_care_boundary/service.py` — 全部用例编排（授权/计划/提醒/反馈/风险/转诊/保供/交接/隔离/三视图）。
- `contracts/record.json` — 输入契约、事项码与未提醒原因码。
- `tests/test_boundary.py` — 需求级测试（35 项），`tests/test_baseline.py` 为旧骨架兼容测试。

## 运行

```bash
# 测试
PYTHONPATH=src python3 -m unittest discover -s tests

# 语法检查
python3 -m compileall -q src tests

# 完整故事演示（授权→计划→提醒→反馈→风险暂停→关单→交接→顾客视图→审计验链）
PYTHONPATH=src python3 -m pharmacy_care_boundary.cli demo

# 健康检查 / 旧契约登记
PYTHONPATH=src python3 -m pharmacy_care_boundary.cli health
PYTHONPATH=src python3 -m pharmacy_care_boundary.cli validate data/sample.json
```

仅依赖 Python 3.11+ 标准库与本地 SQLite。

## 未提醒原因码

| 原因码 | 含义 |
| --- | --- |
| `consent_revoked` | 授权已撤回 |
| `consent_scope_missing` / `consent_channel_missing` | 授权未覆盖事项/渠道 |
| `consent_version_stale` | 计划依据的授权版本已过期，需人工复核 |
| `credential_scope_missing` / `credential_expired` / `credential_store_mismatch` | 药师资质不覆盖/过期/非承接门店 |
| `identity_quarantined` | 标识隔离隐私复核中 |
| `plan_paused` / `plan_closed` | 计划已暂停/关闭 |
