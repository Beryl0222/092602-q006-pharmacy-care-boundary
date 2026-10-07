# 药店健康陪伴边界台

围绕"顾客撤回随访授权后仍收到跨店用药提醒"的合规事故，本项目实现健康陪伴
边界台：顾客授权版本、药师执业资质、药品计划、依从反馈、异常信号、转诊
建议、应急保供批次与门店交接运行在**同一条可校验的哈希审计链**上，并在
每一步执行"资质 × 授权"双覆盖判定。药店只提供用药依从陪伴与转诊协助，
**不替代诊断和治疗**，任何系统响应都不包含自行生成的诊断结论。

## 边界规则

| 规则 | 实现位置 |
| --- | --- |
| 药师资质与顾客当前授权版本必须同时覆盖当前事项 | `Service._assert_covered` / `_cover` |
| 撤回授权追加版本，全连锁立即停止未来联系，历史不删 | `Service.withdraw_consent` |
| 新信息只调整未发送(`scheduled`)任务，已发送(`sent`)冻结 | `Service.apply_plan_update` |
| 风险达阈值(10)自动暂停、建议转诊、转交人工 | `Service.report_signals` / `record_feedback` |
| 销售目标负责人无权关闭异常，关闭仅限合规角色 | `Service.close_anomaly` |
| 重复反馈以幂等键沿用原回执 | `Service.record_feedback` |
| 同一标识出现不同健康内容先隔离，合规隐私复核 | `Service.ingest_health_content` / `review_quarantine` |
| 门店交接/服务中断后到期任务在新店续跑，保供责任随转 | `Service.accept_handover` / `run_due` |
| 撤回后法规最小事实仅合规可见，健康内容屏蔽 | `Service.compliance_view` |
| 系统文本禁止自行诊断结论，统一附免责口径 | `domain.assert_no_generated_diagnosis` |

三类角色各取所需：顾客看到**计划来源、未提醒原因、转诊进度、保供责任**；
药师在双覆盖通过后处理记录；合规人员查看完整审计链，撤回后切换
`minimal_facts` 视图。

## 目录

- `src/pharmacy_care_boundary/domain.py` — 角色/事项/状态词汇、风险阈值、无诊断筛查、哈希工具与不可变记录。
- `src/pharmacy_care_boundary/store.py` — SQLite 表结构；业务写入与审计区块在同一事务提交，审计链可重算校验。
- `src/pharmacy_care_boundary/service.py` — 双覆盖判定、风险规则、撤回链级生效、幂等、隔离、交接续跑、角色视图。
- `src/pharmacy_care_boundary/cli.py` — `validate` 基础登记与 `scenario` 端到端事故场景。
- `contracts/record.json` — 操作信封、状态机与审计动作契约。
- `data/sample.json` — 基础登记冒烟数据。
- `tests/` — 基础契约 + 24 项边界规则测试。

## 运行

运行测试：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

检查源码：

```bash
python3 -m compileall -q src tests
```

基础登记冒烟：

```bash
PYTHONPATH=src python3 -m pharmacy_care_boundary.cli validate data/sample.json
```

端到端事故场景（跨店撤回拦截、风险自动暂停、销售被拒、交接续跑、
隐私隔离复核、审计链校验）：

```bash
PYTHONPATH=src python3 -m pharmacy_care_boundary.cli scenario
```

项目只使用 Python 标准库和本地 SQLite 文件，不需要连接其他运行服务。
