# full_flow 两轮演化记录

本记录对应 campaign `6eb7bdac44e94227b27fddf42e989687`。第一轮完成候选设计、合同修复和一次正式九组评估，成绩为 **-6.593792926992297**。第二轮包含 draft-2 的设计、研究修订与恢复，以及 draft-3 的设计和修复，最终停在 `submit_ready` 检查点。第二次正式评估尚未提交。

时间统一使用 UTC+8。运行阶段覆盖 2026-11-16 20:46:35 至 2026-11-17 04:48:54 的验收快照；远端原始记录于 2026-11-17 15:28:16 开始导出。该 campaign 包含开发期升级和同 campaign 恢复，各阶段运行包版本见对应 manifest。

| 入口 | 内容 |
| --- | --- |
| [第一轮模型回合](round-1/turns/) | 两个回合各自的 prompt、完整事件、命令、结果、最终回复及 stderr。 |
| [第一轮合同与预检](round-1/drafts/draft-1/) | 初稿、修复、源码权限及官方反馈证据。 |
| [第一轮正式反馈](round-1/official-evaluation/feedback.json) | 九组原始反馈、评分、有效性及正式评估 ID。 |
| [九组阶段日志](round-1/official-evaluation/cell-archives/) | 每个算例、种子一个压缩包，包含阶段日志、JSON 指标与时序报告。 |
| [第一轮验收与候选差异](round-1/history/acceptance/) | 当时采集的状态、费用快照、回执和 candidate.diff。 |
| [第二轮模型回合](round-2/turns/) | 四个新增模型回合的完整记录。 |
| [第二轮合同与预检](round-2/drafts/) | draft-2、draft-3 在此次导出时的合同、提示、预检和审查。 |
| [第二轮历史快照](round-2/history/) | draft-2 初稿、修订失败、原请求恢复和 draft-3 最终检查点。 |
| [第二轮内容审查](round-2/reviews/) | 开发者对旧草稿、修订后草稿及最终草稿的内容审查。 |
| [Controller 事件与状态](shared/controller/) | 同一 campaign 的演化事件、state 和 summary。 |
| [请求 journal](shared/journal.json) | 以只读方式导出的 intents、events、metadata，保留 request ID。 |
| [启动、失败与恢复日志](shared/launch-and-recovery/) | launcher 日志、升级前快照、启动回执与版本清单。 |
| [来源与文件清单](manifest.json) | 每份证据的来源、时间、原始和发布版本摘要、存储路径及归档成员。 |
| [发布检查](verification.json) | 文件数量、格式检查、凭据检查及归档完整性结果。 |

六个独立模型回合如下。开始时间取 Controller 的 `model_edit` 事件，耗时取模型回执。

| 轮次与阶段 | Turn request ID | 开始时间 | 耗时 |
| --- | --- | --- | --- |
| 第一轮 draft-1 设计 | `d7727f34441b17aeadf69002a23796847c26c1da` | 11-16 20:46:35 | 289.616 秒 |
| 第一轮 draft-1 合同修复 | `af4a2728d62c53d27405b488499ec7907ea0378d` | 11-16 20:51:33 | 50.322 秒 |
| 第二轮 draft-2 设计 | `5f2d6063b29103a6ad122defc923df8f1f63dbca` | 11-17 02:21:15 | 171.527 秒 |
| 第二轮 draft-2 研究修订 | `5e402ec71f510e94be73342860da383df53e58f3` | 11-17 04:24:56 | 156.491 秒 |
| 第二轮 draft-3 设计 | `a2452c4295cb2a4146900ce2ade98fd6d680a461` | 11-17 04:41:11 | 142.118 秒 |
| 第二轮 draft-3 合同修复 | `448c3a70151caf67f2beb80c7a3ee2d6a77d7793` | 11-17 04:44:22 | 50.367 秒 |

唯一正式评估请求为 `49518aee056fd7d032539e65aa231e3266846902`，11 月 16 日 21:20:44 受理，11 月 17 日 01:20:18 完成。三个训练算例与三个种子共九组，全部 `valid=true`。首轮候选 `ecf5ef1b19b8aeb89338114ef61274dbd70086193f4e5d6a8d87c654eecb9cbc` 由框架接收，保留为本次基准与交付候选。

第二轮的关键过程由历史快照与原始事件共同记录。

1. draft-2 于 11 月 17 日 02:24:32 到达首次设计检查点，初稿另存于 [draft-2-initial](round-2/history/draft-2-initial/)。
2. 增加研究校验接口后，模型成功完成研究字段修订。Controller 读取旧预算记录的 `turn_request_id` 时触发 KeyError，会话失败，记录位于 [draft-2-revision-failed](round-2/history/draft-2-revision-failed/)。
3. 修复后的 Controller 于 04:39:41 按原 request ID `5e402ec71f510e94be73342860da383df53e58f3` 复用成功回执。这次恢复对应原模型回合，新增模型回合数为零。04:40:23，研究校验拒绝 draft-2 的两条不兼容 support，随后进入 draft-3。
4. draft-3 经一次修复到达新检查点，最终官方 session succeeded，正式评估和 submit intent 均保持一次。详细恢复核验见 [recovery-acceptance.json](round-2/history/recovery-acceptance.json)。

自动验收覆盖回执恢复、历史记录保留、源码范围、合同引用及检查点状态。开发侧内容审查仍发现 12% 阈值使用的统计集合与 PlaceDB 消费者不同，真实九组计数均低于该阈值。最终修复将三个测量标签改为 hypothesis，算法保持原样。第二轮草稿的物理有效性和正式分数仍待其自身评估；详见 [最终内容审查](round-2/reviews/draft-3-final.md)。

模型费用按各阶段官方快照列出，表内均为累计值。一个 Codex turn 可包含多笔 provider 请求。

| 阶段及 11 月 17 日采样时间 | settled 请求数 | 已结算金额 | uncertain 请求数 | 仍预留金额 |
| --- | --- | --- | --- | --- |
| 首轮验收，01:50:27 | 63 | 0.31092516 元 | 1 | 2.228224 元 |
| draft-2 初稿，02:29:10 | 93 | 0.57398400 元 | 1 | 2.228224 元 |
| 研究修订失败，04:30:25 | 125 | 1.01759352 元 | 1 | 2.228224 元 |
| 最终设计检查点，04:48:54 | 169 | 1.29919808 元 | 1 | 2.228224 元 |

预留已经占用可用余额，其对应请求的实际费用未知。原 uncertain 请求持续保留，所以整个实验的实际总费用为 unknown。已结算费用与预留金额分别保留。

本次发布包含 2,971 份新采集的远端记录，以及 81 份先前采集的快照、验收记录和开发审查，共 3,052 份证据。第一轮公开产物中选择了 2,872 份 JSON、JSONL、LOG、LOG.GZ、RPT、RPT.GZ 文件，其余 708 份 CSV、压缩物理输入及部分 TXT/YAML 保留在远端。完整公开产物索引仍保存在 [files.json](round-1/official-evaluation/files.json)。大型数据库、完整物理源码检出和输入数据属于原环境材料，重现实验还需要官方框架与训练数据。

发布副本检查了配置的模型密钥、常见 API token、Bearer、凭据赋值和私钥块，压缩文件也解压检查。所选材料未发现匹配的凭据。原始内容保持原样，大型 JSON 增加 gzip 压缩，阶段材料按九组打包，SQLite journal 转为 JSON。原始与发布摘要及变换记录见 manifest。历史日志中的绝对路径和历史校验摘要继续表示原运行环境中的对象。

从本目录可校验全部发布文件，或查看某组阶段日志。

```bash
sha256sum -c SHA256SUMS
tar -tzf round-1/official-evaluation/cell-archives/bp_fe-ng45-s1.tar.gz
tar -xzf round-1/official-evaluation/cell-archives/bp_fe-ng45-s1.tar.gz -C /tmp
gzip -dc round-2/history/draft-3-final/snapshot.json.gz | python3 -m json.tool
```

这些材料用于开发回看。运行包从 `master` 的 Agent 代码构建，历史成绩、候选差异和人工审查留在此证据分支。本次发布复用已有运行记录，模型和物理评估调用次数均为零。
