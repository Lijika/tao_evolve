# 最终研究修订内容审查

本次自动校验和会话验收通过，`draft-3` 的设计质量仍存在实质问题，建议保留检查点，暂不消耗第二次正式评估。修复将三条测量从 `support` 改为 `hypothesis`，合同正文仍声称 bp_fe 会触发12%开关。当前源码与可复用的真实输入计数表明，九组输入均保持父代的自动权重规则。

审查基于2026-11-17 04时48分54秒（UTC+8）的 [最终采集](legacy-observation-fix/acceptance/acceptance.json)。官方 session succeeded，`draft-3` 位于 `submit_ready`，repair_count 为1，[最终预检](legacy-observation-fix/acceptance/draft/preflight-1.json) 的 errors 为空，原首轮冠军已被框架接收。正式评估和 submit intent 均保持1，草稿尚无正式物理分数。模型已结算169笔、合计1.29919808元，另有1笔 uncertain 和原2.228224元预留，实际总费未知。

核心缺陷可由 [合同](legacy-observation-fix/acceptance/draft/contract.json)、[最终报告](legacy-observation-fix/acceptance/final-report.md) 和 [源码差异](legacy-observation-fix/acceptance/candidate.diff) 共同确认。`measurement_checks[0]` 仍写0.12845超过门槛，所以 bp_fe 禁用权重；`[1]` 仍把公开汇总的3411直接视为函数分子。`reason`、reflection 与 `endpoint_expected_effect` 延续相同判断。最终报告承认代理量无法证明分支触发，但建议立即用九组正式评估检验 bp_fe 恢复。修复前后的候选摘要完全相同，算法没有随标签修正。

本次复用了 [旧九组研究](../round-2-design/design-quality-review.json) 的公开日志计数，并核对了复用条件。当前与旧材料的 task、campaign、父代、request、task_digest、九组反馈及公开 files_index 摘要相同；父代文件逐字节相同。计数辅助函数 AST 相同；旧权重函数内联 `cross_die_count = len(cross_die_ids)` 后，AST 与新函数完全相同。最终预检中的源码摘要也与本次采集文件一致。因此旧计数适用于当前12%判断，历史函数测试仍保留其原日期和执行对象。

| 算例 | 三个种子的实际跨 die 比例 | 当前12%开关 |
| --- | --- | --- |
| bp_fe | 11.189%、11.956%、11.403% | 三组均保留自动权重 |
| bp | 3.370%、3.425%、3.351% | 三组均保留自动权重 |
| Ariane | 6.750%、11.236%、7.368% | 三组均保留自动权重 |

例如 bp_fe 1001 的 partition 指标为3411/26555，函数实际输入计数为3604/32210，分子与分母都不同。当前代码在这些真实计数下继续采用父代的1.30、1.15、1.05倍率。因此这些输入无法检验合同所述“关闭 bp_fe 权重”的机制。该结论针对函数决策和权重行为；新草稿的完整物理流程、运行时间与正式分数尚未测量。

按 [七项审查依据](../round-2-design/review-criteria.md) 判断如下。

| 项目 | 内容结论 |
| --- | --- |
| 反馈与父代身份 | 支持。reflection_subject、编辑父代和公开反馈均绑定当前 campaign 的首轮 ecf5 候选，最终预检保留身份。 |
| 完整九组与退化 | 基本支持。保留三算例、三个种子的九组，识别 bp_fe 持续退化及 Ariane 波动；合同仍误写 nine seeds。 |
| 对准正式终点 | 有进展。五项数值、定义和单位准确，Ariane 1001 的官方 WNS 改善与 TNS 严重退化已被纳入反证。 |
| 跨阶段解释与因果边界 | 支持边界声明。保留同阶段比较要求、端点映射未知及 density、legalization、CTS/routing、seed 等替代解释。 |
| 机制与实际代码 | 不满足。12%阈值依赖不一致的统计集合，真实九组均未产生所述权重关闭行为，改标签未纠正正文推理。 |
| 收益、代价与可推翻假设 | 部分满足。新合同补充列表内存和额外线性遍历，符合源码；物理代价未测量，所选实验对声称的关闭机制缺少区分能力。 |
| 证据不足时的判断 | 部分满足。结构字段与报告承认 unknown，合同其他段落仍将分支触发当作事实，判断尚未统一。 |

自动检查的作用已实际体现。[恢复回执](legacy-observation-fix/recovered-draft-check.json) 显示旧草稿的两条不兼容 support 被拒绝；新草稿首次预检也拒绝三条，有限修复后允许 hypothesis 保留。[最终研究校验](legacy-observation-fix/acceptance/draft/preflight-1.json) 明确限定为引用与一致性检查，`current_candidate_behavior_verified=false`。B0 已在该候选的 synthetic fixture 上通过，修复后按精确输入复用缓存；它验证所列接口不变量，覆盖范围不含真实九组的12%分支有效性。

资源分析较初修更具体。源码确实新增跨 die ID 列表和一次线性遍历，额外空间随跨 die 网络数增长。声明“无新增布局迭代”有 diff 支撑，实际耗时、内存峰值、面积及功耗变化继续未知。候选唯一改动位于授权权重函数，其他阶段与配置保持源码原状；物理输出是否保持合法仍由候选自己的评估确认。

当前材料展示了准确读数、反证提取、结构化反思和有限修复能力，也显示模型仍可能用 `unknown` 标签保留已经受到真实输入反驳的推理。此次审查未修改候选或提供替代算法。建议先处理统计集合与实验区分能力，再决定是否申请下一次评估。结构化证据、摘要和复用边界见 [JSON记录](revision-quality-review.json)；首次失败会话的历史保存在 [初修审查](initial-revision-quality-review.md)。
