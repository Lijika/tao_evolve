# 首次研究修订内容审查

本次模型完成了研究合同补写，五项数值引用准确，补充了 WNS/TNS 反证；12%阈值的统计集合问题仍在，当前输出不足以支持下一次正式评估。审查对象是 [固定归档](legacy-observation-fix/before/) 中已结束模型回合的最终输出。新版本整体验收及后续恢复结果另行记录。

[模型回执](legacy-observation-fix/before/turn-result.json) 显示 success 为 true，耗时156.49秒，完成30次终端调用。[会话回执](legacy-observation-fix/before/session-result.json) 为 failed。[失败日志](revision-failure-logs.json) 确认 Controller 在 `_observe_model` 读取旧预算行的 `turn_request_id` 时触发 KeyError；归档 state 的两条首轮预算记录确实缺少该字段。异常发生在新合同进入 `_validate` 之前，因此本次没有研究校验通过回执，也没有到达设计检查点。[采集验收](legacy-observation-fix/before/acceptance.json) 保留原1次正式评估和唯一 submit intent，第二草稿的物理效果与成绩继续未知。

按 [七项依据](../round-2-design/review-criteria.md) 核对如下。

| 项目 | 本次结论与证据 |
| --- | --- |
| 反馈与父代身份 | 支持。prompt、evidence 和 contract 均绑定同 campaign 的首轮 ecf5 候选，最新反馈对象与编辑父代分别声明，当前两者相同。 |
| 完整九组与退化 | 基本支持。evidence 保留三算例乘三个种子的九组，报告识别 bp_fe 三组负分及 Ariane 种子波动；contract 仍误写 nine seeds。 |
| 对准正式终点 | 有进展。measurement_checks 正确引用 Ariane 1001 的 WNS/TNS，并把官方 WNS 改善与 TNS 退化作为反证；局部绝对值与官方归一化分项的对应关系仍需限定。 |
| 跨阶段解释与因果边界 | 支持边界声明。承认缺少同阶段已评估父子矩阵和端点映射，保留 density、legalization、CTS/routing 与种子噪声等替代解释。 |
| 机制与实际代码 | 存在实质缺陷。前两项测量声明 population unknown、unit unknown，仍用作 support；第一项直接断言0.12845使 bp_fe 关闭权重，实际消费者集合尚未对应。 |
| 收益、代价与可推翻假设 | 部分支持。提出 bp_fe 恢复、其他算例收益及合法性反证；本轮只补合同，报告未证明真实输入触发所述分支，runtime guarantees 也缺测量支撑。 |
| 证据不足时的判断 | 部分支持。物理效果和泛化保持 unknown，synthetic 检查标为 source_hypothesis；最终报告仍把未知集合的比例当作阈值支撑。 |

最关键的问题位于 [最终合同](legacy-observation-fix/before/draft/contract.json) 的 `research_review.measurement_checks[0:2]`。第一项公开值为 partition 的3411/26555，第二项公开 placement 汇总值也是3411。合同明确消费者为 `len(placedb.net2pin_map)` 对应集合，却同时写“集合未证明相同”和“bp_fe 会关闭权重”。[最终报告](legacy-observation-fix/before/final-report.md) 继续使用12.8%至13.8%支撑该结论。[旧内容审查](../round-2-design/design-quality-review.json) 已记录同训练输入的实际 PlaceDB 比例均小于12%；本轮模型自报算法保持原样，归档尚无修订后的独立源码副本与测试原始输出，当前审查据此保留源码身份复核项。

五条 measurement 的 value、definition、unit 均与归档 catalog 精确一致。前两条 support 与 unknown 声明的冲突可直接由字段核实，[当前检查实现](../../contest-source/agent/full_flow/tao_evolve/research_record.py) 会将这种组合列为 `research_measurement_support_incompatible`。本地缺完整对应 checkout，本审查未调用完整 `assess_research`，也未将静态判断写成实际运行回执。模型报告的 `py_compile` 和4项单测通过保留为模型自报；尚无证据表明这些测试覆盖真实九组的分支条件。

预置知识检查限于已归档材料。prompt 的专家正文为空，measurement 引用均来自本 campaign 的公开首轮反馈，reflection memory 为空；完整终端轨迹尚未提供。此次独立审查未向运行输入添加历史审查、阈值建议或新算法。

建议先让恢复流程完成该合同的真实校验，再依据新回执审阅最终处置。当前草稿保留在开发审查状态，正式评估次数保持1。结构化结论及证据摘要见 [JSON记录](initial-revision-quality-review.json)。
