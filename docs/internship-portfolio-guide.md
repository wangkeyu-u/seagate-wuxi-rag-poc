# 希捷实习作品集与面试指南

这是一个使用合成数据、面向 SeaTrack 类 MES 良率异常调查的证据分诊系统。它要解决的核心问题是：异常已经被发现，但产品、工艺和质量工程师仍要跨系统寻找历史案例、有效 SOP、FA 报告、维护与变更证据，才能开始可信的根因调查。

系统的价值不是“生成一段更自然的回答”，而是把分散证据整理成一份符合当前产品、站点、物料和软件版本的首轮调查包，并通过检查记录、质量复核和人工发布形成知识闭环。AI 负责缩短取证路径，不代替工程师确认根因，也不获得生产控制权。

这套项目最适合证明你理解了制造痛点，并能把 AI、后端、制造数据、安全治理和工程交付放在同一个解决方案里。

## 0. 一句话产品定义

> 当 SeaTrack 类 MES 出现良率异常时，自动为工程师找到与当前上下文真正相关、仍然有效且有权访问的历史证据，生成可核验的首轮排查路径，并将人工确认的调查结果沉淀为可复用知识。

## 1. 从痛点到产品能力

| 工厂调查痛点 | 产品能力 | 用户得到的结果 |
| --- | --- | --- |
| 资料分散、检索时间长 | 聚合历史案例、SOP、FA、维护和变更证据 | 一份按相关性排序的调查证据包 |
| 相同 Failure Code 对应不同根因 | 比较站点分布、批次集中度和版本变更 | 设备、物料、程序等假设被上下文区分 |
| SOP 过期或资料越权 | 检索前执行身份、范围、版本和状态过滤 | 只返回当前用户能打开的有效证据 |
| 无证据时 AI 容易猜测 | 阈值、引用闭环、拒答和人工升级 | 不把语言流畅度冒充根因可信度 |
| 调查经验停留在个人手中 | 检查记录、质量复核、人工发布 | 已确认调查可被下一次异常复用 |
| 上游数据不可靠 | 签名、schema、主数据对账、游标和检疫 | 不可信记录不能静默进入知识库 |

## 2. 与公开岗位的能力映射

| 希捷公开方向 | 仓库中的可演示证据 | 面试中要诚实说明的差距 |
| --- | --- | --- |
| SeaTrack MES 的 GenAI 知识管理、RAG、后端服务和微服务思维 | 上下文驱动混合检索、REST API、证据化答案、人工调查状态机 | 未接真实 SeaTrack；当前是单体 PoC，不是假装微服务 |
| Python、SQL、测试、调试、性能和文档 | 标准库 Python、SQLite 迁移/事务、80 个单元测试、24 个评测、62 个 HTTP 系统检查、并发场景和完整文档 | 还没有真实流量、容量规划和分布式压测 |
| 消息/事件驱动集成 | 幂等来源作业、租约锁、游标、重放识别、版本历史、检疫和恢复语义 | 这是事件系统的可靠性语义，不是已经接入 Kafka |
| RAG、Copilot、Agent/MCP 和企业知识集成 | 权限前置检索、版本化证据、拒答/升级，以及带严格结构和确定性降级的可选模型网关 | 没有为了追热点伪造 Agent；企业模型审批、MCP 和真实向量库仍是下一阶段工作 |
| 制造良率监控、早期问题发现与工厂 IT 部署 | Failure Code、站点、物料、程序版本、异常范围和趋势上下文 | 数据和 Failure Code 全部虚构，没有生产业务收益数据 |
| 安全、质量和跨团队交付 | OIDC/JWKS 路径、RBAC+ABAC、签名交付、主数据对账、引用闭环、质量审核和 runbook | 企业 IdP、DLP、告警、审批与安全验收仍需现场团队完成 |

这些方向来自希捷当前公开岗位：[GenAI & Software Engineering Intern](https://seagatecareers.com/job/Wuxi-GenAI-%26-Software-Engineering-Intern/1405184300/)、[Product Performance Engineer Intern](https://seagatecareers.com/job/Wuxi-Product-Performance-Engineer-Intern/1403909300/)、[Factory AI and Automation Intern](https://seagatecareers.com/job/Wuxi-Software-Engineering-Intern-Factory-AI-and-Automation/1405179600/) 和 [AI Engineering Hub Intern](https://seagatecareers.com/job/Wuxi-AI-Engineering-Hub-Intern/1406632700/)。希捷公开的智能制造案例也强调数据清洗、异常检测、实时推理、遗留系统集成与可衡量业务价值：[Smart Manufacturing AI](https://www.seagate.com/innovation/smart-manufacturing-ai/)。公开资料只能用于理解岗位方向，不能推断内部字段、架构或流程。

## 3. 90 秒中文项目介绍

> 我做的是一个面向硬盘制造良率异常的证据分诊 Copilot，场景是假设工程师从 SeaTrack 类 MES 看到 Failure Code 异常后，需要快速判断更像设备、物料还是程序变更问题。系统会结合产品、站点、批次和软件版本检索历史案例与当前有效文档，但不会自动停线、放行或确认最终根因。
>
> 我没有只做问答效果，而是把生产边界一起实现了：身份来自服务端验证的 OIDC 或签名令牌；角色、产线和站点权限在检索前过滤；每个建议步骤必须能打开有效证据；低置信度和高风险动作会升级或拒绝。离线数据接入也有严格 schema、主数据对账、签名清单、幂等游标、回滚和检疫。
>
> 项目全部使用合成数据，当前通过 80 个单元测试、24 个业务评测和 62 个 HTTP 全系统检查。它已实现可选的 Responses API 兼容模型网关，但下一步不是声称已经生产化，而是接真实只读上下文、企业检索和批准的模型端点，并在工程师 golden set 与影子模式中验证语义引用准确率和首轮取证时间。

## 4. 60 秒英文版本

> I built an evidence-grounded yield anomaly copilot for a SeaTrack-like manufacturing workflow. Given a failure code plus product, station, material lot, and software context, it ranks similar investigations and effective engineering documents, then proposes a traceable first-pass checklist. It never makes production-control decisions.
>
> The main engineering work is around trustworthy boundaries: server-verified identity, RBAC and station-level ABAC before retrieval, version-aware citations, deterministic escalation, an optional Responses API gateway with strict structured output and safe fallback, an auditable human review workflow, and governed offline ingestion. The repository uses synthetic data and currently passes 80 unit tests, 24 business evaluations, and 62 end-to-end HTTP checks. For production, I would connect approved sparse, vector, reranking, and model services, then validate them in shadow mode on a human-labeled factory dataset.

## 5. 8 分钟演示路线

1. **问题（45 秒）**：工程师不是缺一段流畅回答，而是缺能打开、版本正确、符合权限的调查证据。
2. **三个同码异因场景（2 分钟）**：依次运行单站、物料集中和程序变更，展示相同 `F127` 因上下文不同而排序不同。
3. **安全边界（1 分钟）**：运行未知 Failure Code 和“跳过测试/直接放行”，展示升级与拒绝。
4. **证据与工作流（1.5 分钟）**：打开引用，记录检查结论，提交质量审核并发布。
5. **权限（1 分钟）**：切换合成角色，展示受限案例和跨站点资源不可见。
6. **来源运营（1 分钟）**：展示签名清单、严格导入、来源健康和脱敏检疫，而不是现场上传任意文件。
7. **工程质量（45 秒）**：运行 `make check`，说明三层测试及生产差距。

演示前先运行：

```bash
make check
make run
```

## 6. 可直接改写到简历的三条要点

- 设计并实现面向 SeaTrack 类 MES 良率异常的证据分诊 PoC，融合 Failure Code、产品、站点、物料批次与软件版本上下文，对同码不同根因进行可解释混合排序，并为每个排查步骤建立版本化引用闭环。
- 构建服务端身份与数据治理边界：OIDC RS256/JWKS、RBAC+产线/站点 ABAC、受控拒答、人工审核状态机，以及带签名清单、主数据对账、幂等游标、回滚与检疫的离线来源同步。
- 建立可重复质量门禁，覆盖 80 个单元/回归测试、24 个合成业务评测和 62 个真实 HTTP 系统检查，包括模型降级、越权、篡改、过期证据、持久化、32 请求并发和来源恢复路径。

不要写“提升良率 X%”“减少停机 Y 小时”或“部署到希捷生产”，因为仓库没有这些事实。

## 7. 高频追问与回答框架

**为什么不用 LLM 直接回答？** 生产调查的首要问题是证据、权限和可重复性。当前确定性控制层先完成拒答、升级、步骤和引用闭环；可选 LLM 只接收授权证据包，输出结构化候选假设，并接受确定性引用校验，失败时不影响调查完成。

**哈希向量算真正的向量检索吗？** 它是可重复、零依赖的检索替身，不是生产 embedding。价值在于接口、融合、权限和评测方法已经分离；下一步用真实 golden set 对比 BM25、多语言 embedding 与 reranker。

**如何避免敏感信息泄露？** 身份由服务端验证，角色和站点范围在召回前过滤，详情 API 再做同一套授权；无权资源返回 404。来源记录同时携带版本、状态和 ACL。

**Kafka 在哪里？** 当前实现的是调度友好的一次性作业及事件消费必须具备的幂等、游标、重放、检疫和恢复语义，没有声称已经使用 Kafka。适配 Kafka 时这些不变量不变。

**如何接 SeaTrack？** 第一阶段只读：从异常页面传入事件 ID 与授权上下文，或由服务身份读取最小字段；结果写入独立调查库，只回写人工确认后的调查链接/摘要，不申请设备控制权限。

**怎么证明有业务价值？** 先建立真实问题 golden set 和人工基线，在影子模式测 Recall@K、引用准确率、权限泄露、拒答召回、证据首屏 P95 与工程师首轮取证时间；没有基线前不承诺 ROI。

## 8. 面试前一周清单

- 能不看稿讲清在线请求和来源同步两条路径；
- 现场跑一次 `make check` 并解释单元测试、业务评测、系统测试的区别；
- 准备一次失败案例：为什么连接关闭暴露了测试事务未提交，以及如何修正；
- 准备一次安全权衡：为何 ACL 在检索前执行、为何无权详情返回 404；
- 准备一次架构演进：SQLite/文件作业如何迁移到企业数据库、Kafka、向量库和模型网关；
- 把“真实已完成”和“生产待验证”分开说，宁可少 claim，也不要透支可信度。
