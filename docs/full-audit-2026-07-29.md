# 无锡制造 RAG 全量测试与希捷适配审计

审计日期：2026-07-29  
审计对象：当前合成数据版 SelfTest Yield Anomaly Copilot  
决策目的：判断当前系统是否可作为希捷无锡工厂 PoC、技术是否达到 2026 年生产基线，以及最值得优先验证的业务痛点。

> 修复状态（2026-07-29）：下述 24/35 是首次审计基线。11 个缺口已经在本地代码中修复；修复后的原 35 项为 35/35，追加 6 项绕过检查和 1 项开发身份正向检查后为 42/42。修复关闭了当前 PoC 的已知边界，但不等于已经完成希捷企业 SSO、SeaTrack、真实知识库或生产模型对接。验证细节见 [`security-remediation-2026-07-29.md`](./security-remediation-2026-07-29.md)。

## Executive Summary

- **已知的本地安全和正确性缺口已经关闭，但还不能称为希捷生产级 RAG。** 首次审计为 24/35；修复后单元测试 18/18、合成评测 24/24、端到端与绕过检查 42/42。仍缺企业身份、SeaTrack、真实 ACL/版本同步、正式语义检索、模型网关和现场 golden set。
- **业务方向与希捷公开信号吻合，但具体场景尚未被无锡现场确认。** 无锡公开岗位明确涉及 HDD 性能与良率分析、Incoming/Assembly 早期问题检测、Outgoing DPPM 预测，以及用 LLM/RAG 为 SeaTrack MES 建设知识管理能力。当前项目的产品、站点、HSA 批次、程序版本和历史案例结构与这些任务相容；但 `SelfTest`、`F127`、字段名、阈值和根因全部是虚构设计。
- **最有价值的第一切口应调整为“嵌入 SeaTrack 的良率异常证据分诊”，而不是独立的通用问答机器人。** 希捷公开资料显示其全球 drive factory 已有 GenAI 根因分析能力，因此无锡项目应优先解决本地 SeaTrack 上下文、版本化知识、跨系统证据和权限闭环，避免重复建设已有的全球 RCA 平台。
- **技术升级顺序应先治理，再换模型。** 第一优先级是 SSO/服务端授权、输入模式校验、证据闭环和真实评测集；随后再引入正式嵌入、稀疏＋稠密混合检索、重排序和企业批准的大模型。直接换成最新模型不能解决当前最严重的权限和业务正确性问题。

## 1. 全量测试结论

### 1.1 覆盖范围

本次测试包括：

- 合成数据生成与完整性校验；
- 混合检索排序、有效/失效文档过滤；
- 缺少上下文、未知 Failure Code、受限内容和高风险动作；
- HTTP 健康检查、元数据、统计、案例、文档、调查、反馈和静态资源；
- 请求体边界、错误 JSON、路径穿越、分页和未知路由；
- 调查与反馈持久化；
- 32 个并发分诊请求；
- PPTX/XLSX 压缩包完整性与 PPT 版面越界；
- 身份、角色、CORS 和历史调查访问等生产控制。

### 1.2 结果总览

| 测试层 | 通过 | 失败 | 结论 |
| --- | ---: | ---: | --- |
| 修复后单元测试 | 18 | 0 | 检索、拒答、签名身份、ABAC、证据闭环和业务泛化稳定 |
| 合成评测集 | 24 | 0 | 在预置数据与预置问题上全部命中 |
| 首次端到端审计 | 24 | 11 | 修复前基线 |
| 修复后原审计集 | 35 | 0 | 11 个已验证缺口不再复现 |
| 追加绕过与开发身份检查后 | 42 | 0 | 签名篡改、匿名访问、跨用户、跨站点、恶意 Origin、生产禁用开发令牌和本地开发正向路径均通过 |
| 基础 HTTP 功能 | 15 | 0 | 页面、API、持久化和错误处理主路径可运行 |
| 生产控制 | 8 | 0 | 当前 PoC 边界通过；企业系统对接仍待完成 |
| 并发烟雾测试 | 32 请求 | 0 请求失败 | 最近一次本机 P95 约 94.72ms；多次观测 12.45–94.72ms，仅代表小型合成数据 |

机器可读结果见 [`full-audit-test-summary.json`](./full-audit-test-summary.json)。可重复测试入口为 `python3 scripts/full_system_test.py`。

### 1.3 必须修复的失败项

下表记录首次审计发现；当前状态均为“本地已修复并进入回归测试”。

| 优先级 | 缺口 | 测试证据 | 业务影响 |
| --- | --- | --- | --- |
| P0 | 角色由客户端自行声明 | 声明 `QUALITY_ENGINEER` 即可读取受限案例 | 受限 FA/质量知识可能越权泄露 |
| P0 | 调查历史无需认证 | `/api/investigations` 匿名返回调查列表 | 用户问题、上下文和证据可能泄露 |
| P1 | 高风险语义只靠关键词 | 5 种改写仅 1 种被拒绝 | 对放行、跳测、报废、联锁等表达覆盖不足 |
| P1 | 缺少输入 Schema | 非对象 `context` 返回 HTTP 500 | 易被异常输入击穿，且泄露内部错误细节 |
| P1 | 字段类型未校验 | `station_ids` 字符串被接受 | 检索集合与业务判断可能失真 |
| P1 | 业务逻辑硬编码 F127 | F219 查询仍显示 F127 检查步骤 | 工程师可能执行与问题不匹配的检查 |
| P1 | 建议与引用未闭环 | 步骤引用 `DOC-QA-ESC-001-V1_0`，回答引用列表没有该文档 | 无法证明每项建议的依据 |
| P2 | 未知角色被接受 | `NOT_A_REAL_ROLE` 返回 201 | 权限模型不完整 |
| P2 | 负分页参数未拒绝 | `limit=-1` 返回未限制结果 | 资源滥用与数据暴露范围扩大 |
| P2 | 未知 API 路由返回 HTML 200 | `/api/does-not-exist` 不是 JSON 404 | 监控、客户端和安全网关行为不准确 |
| P2 | CORS 为通配符 | `Access-Control-Allow-Origin: *` | 真实部署中扩大跨站调用面 |

## 2. 是否采用了 2026 年最新技术

### 2.1 结论

**没有。当前实现刻意选择了零外部依赖、离线可运行的演示架构，因此技术理念有一部分是现代的，但核心模型和生产工程能力尚未实现。**

已符合现代方向的部分：

- 词法、向量近似和制造上下文组成混合排序；
- 检索前按知识密级过滤；
- 文档版本和有效状态控制；
- 回答提供案例与文档引用；
- 证据不足时升级人工，禁止自动控制生产；
- 有合成评测集、反馈和审计记录。

仍属于演示替身的部分：

- 哈希桶余弦只是 token 相似度，不是真实语义嵌入；
- 没有向量数据库、BM25/稀疏索引或学习型重排序器；
- 没有 LLM，答案由确定性模板合成；
- 没有 SeaTrack/MES、PLM、JIRA、ERP、FA 或文档平台连接；
- 已有 PoC 签名身份、服务端 RBAC/ABAC 和调查所有者隔离，但没有希捷企业 SSO、真实 ACL 同步或数据域策略平台；
- 没有真实工程师标注的检索与回答评测；
- 没有模型调用追踪、成本、Token、漂移或提示注入监测。

### 2.2 2026 目标技术基线

| 层 | 当前实现 | 建议目标 |
| --- | --- | --- |
| 业务上下文 | 用户填写 JSON | SeaTrack/MES 只读 API 或消息事件自动补齐产品、工站、批次、版本和时间窗 |
| 知识入库 | 12 个 Markdown/JSON 文档 | 可解析 PDF/Office/网页；分块、版本、血缘、密级和适用条件进入统一索引 |
| 检索 | 词法＋哈希桶＋规则分 | 稀疏关键词＋真实多语嵌入＋结构化过滤＋重排序；对中英混合术语单独评测 |
| 生成 | 固定模板 | 企业批准模型输出严格 JSON Schema；每个事实、建议和风险都绑定引用 |
| 模型接口 | 无 | 模型网关＋可替换供应商；OpenAI 路线使用 Responses API，按评测选择 GPT-5.6 Sol/Terra/Luna，而非盲目固定最贵模型 |
| 权限 | 服务端签名 PoC 身份；角色、站点和产线前置过滤 | 希捷 SSO/OIDC；真实文档 ACL 与 SeaTrack 数据域同步；企业策略与密钥管理 |
| 安全 | 关键词拒绝 | 策略引擎＋语义分类＋输出校验＋人工审批；检索内容按不可信输入处理 |
| 评测 | 24 个同源合成题 | 100–300 个真实脱敏问题；Recall@K、MRR/nDCG、引用精确率、忠实度、拒答召回率和首轮分诊时间 |
| 可观测性 | 单次延迟 | 全链路 trace、检索结果、重排序、引用、模型版本、成本、反馈与回归门槛 |
| 部署 | 本机标准库服务 | 工厂批准的容器/微服务环境；内网或私有连接；知识与实时控制网络隔离 |

OpenAI 当前官方文档建议用向量存储进行语义检索，并支持文档属性过滤、稀疏与语义结果的混合权重、排名阈值及返回检索结果用于引用审计；代表性数据和 trace grading 应用于持续评测。参考：[Retrieval](https://developers.openai.com/api/docs/guides/retrieval)、[File search](https://developers.openai.com/api/docs/guides/tools-file-search)、[Evals](https://developers.openai.com/api/docs/guides/evals)、[Trace grading](https://developers.openai.com/api/docs/guides/trace-grading)。截至审计日，官方模型指南将 GPT-5.6 系列列为当前生产模型，并建议通过 Responses API 使用，但模型选择仍必须基于希捷数据治理、延迟、成本和真实评测。[Model guidance](https://developers.openai.com/api/docs/guides/latest-model)

## 3. 与希捷无锡的吻合程度

### 3.1 已被公开资料验证的吻合点

1. **制造对象吻合。** 2026–2029 ISO 9001 证书列出的无锡范围为 Drive、HSA 和 Systems Manufacturing。[ISO 9001 certificate](https://www.seagate.com/content/dam/seagate/assets/global-citizenship/_shared/files/certificate-9001-seagate-final-20260121.pdf)
2. **性能与良率任务吻合。** 2026 年无锡 Product Performance 岗位明确负责 HDD performance、yield monitoring and analysis、Incoming/Assembly 问题早期检测、Outgoing quality prediction，以及将分析部署到实时工厂 IT 系统。[Product Performance Engineer](https://seagatecareers.com/job/Wuxi-Product-Performance-Engineer-Intern/1403909300/)
3. **SeaTrack RAG 方向高度吻合。** 2026 年无锡 GFIT 岗位明确提出用 LLM 为 SeaTrack MES 建设 GenAI knowledge management，并列出向量数据库、RAG、Agentic AI、消息平台、微服务和容器技术。[GenAI & Software Engineering](https://seagatecareers.com/job/Wuxi-GenAI-%26-Software-Engineering-Intern/1405184300/)
4. **跨系统工程知识吻合。** 无锡 AI Engineering Hub 岗位列出 RAG、MCP、AI assistants，以及 PLM、JIRA、ERP、制造系统和数据仓库集成。[AI Engineering Hub](https://seagatecareers.com/job/Wuxi-AI-Engineering-Hub-Intern/1406632700/)
5. **业务价值方向吻合。** 希捷公开的全球工厂实践将 change management、engineering burden、data throughput、legacy integration 和实时推理列为制造 AI 挑战；公开案例显示 drive factory 的 GenAI RCA 可把处理时间从天/周缩短到小时/分钟。[Smart Manufacturing AI](https://www.seagate.com/innovation/smart-manufacturing-ai/)

### 3.2 尚未验证、不能当作事实的部分

- 无锡现场是否把相关测试阶段称为 `SelfTest`；
- 是否存在 `F127`、`F219` 等代码及其含义；
- SeaTrack 中真实表名、字段名、消息主题、API 和权限模型；
- 当前异常分诊耗时、发生频率、误判成本和案例复用率；
- 无锡是否已经接入希捷全球 drive factory GenAI RCA；
- 哪些 SOP、FA、8D、OCAP 和变更记录允许进入 PoC；
- 工厂批准使用的云、模型供应商、数据驻留和零保留策略。

因此，当前项目可评价为：**概念和业务对象高度相关，现场流程和系统层面仍是假设，生产可用性尚未达到门槛。**

## 4. 最有利于希捷的痛点优先级

评分满分 10：业务价值 35%、公开证据强度 25%、RAG 适配度 25%、六周可行性 15%。该评分用于选择访谈和 PoC 顺序，不代替希捷内部 ROI 数据。

| 排名 | 痛点 | 分数 | 为什么值得做 | RAG 应承担的角色 |
| ---: | --- | ---: | --- | --- |
| 1 | SeaTrack 良率异常与 RCA 证据分诊 | 9.7 | 同时吻合无锡 SeaTrack GenAI 岗位、产品性能岗位和希捷全球 drive factory RCA 方向 | 自动补齐异常上下文，召回相似案例、有效 SOP、FA 和变更；给出引用完整的首轮检查顺序 |
| 2 | Incoming/Assembly/HSA 批次与材料偏差分诊 | 8.7 | 无锡制造 HSA，岗位明确关注 Incoming/Assembly 早期检测；希捷公开 material deviation AI 已产生年度价值 | 跨工站比较批次、料号、偏差审批与历史处置；解释相关性并防止把相关性当根因 |
| 3 | 新产品、测试程序和工程变更的资格/爬坡证据 | 8.4 | 希捷最新监管文件把新产品资格、制造良率、可靠性和 HAMR volume ramp 列为关键执行风险 | 把 PLM/JIRA/变更单、测试结果、SOP 版本和客户资格证据串成可审计证据链 |
| 4 | SeaTrack/GFIT 遗留应用与工程知识传承 | 8.1 | 无锡岗位直接提出 modernize legacy applications、accelerate troubleshooting 和 AI coding assistants | 面向 IT/工程人员检索接口、作业、故障、设计和运行手册；缩短系统故障定位和新人上手时间 |
| 5 | Outgoing quality/DPPM 风险解释与升级 | 8.0 | 无锡产品性能岗位明确提出 Outgoing quality prediction；质量逃逸影响客户资格和成本 | 预测仍由 ML 完成，RAG 负责解释驱动因素、关联证据、适用规则和升级路径 |

### 推荐的第一 PoC 定义

> 在 SeaTrack 中选择一个高频或高损失的良率异常族，自动读取产品、工站、批次、测试程序和时间窗，检索历史案例、有效 SOP、FA 与变更记录，向产品工程师返回 Top-3 相似案例、差异点、低风险检查顺序、证据引用和升级条件。

这个定义保留了当前项目已经做好的“异常上下文＋证据分诊”，同时删除了尚未证实的 `F127` 绑定，并明确它是希捷现有监测/预测模型与工程决策之间的解释层，而不是另建一个检测模型。

## 5. 推荐实施顺序

1. **把已修复的 P0/P1 控制固化为不可回退门槛。** 下一步接入希捷身份、ACL 和密钥系统；当前签名 PoC 身份不能替代企业 SSO。
2. **用真实问题冻结场景。** 从无锡产品性能团队抽样 50–100 个脱敏异常，记录现状首轮分诊时间、查询系统数和最终使用证据。
3. **建立两条数据通道。** SeaTrack/MES 结构化只读查询负责“现在发生了什么”；版本化知识检索负责“历史上如何处理、当前规则是什么”。不要把实时数值全部转成文档向量。
4. **升级检索并做离线选型。** 比较正式多语嵌入、BM25/稀疏检索、混合 RRF 和重排序；中英混合缩写必须单独分层评测。BGE-M3 等多语、多检索模式模型可作为自托管候选，但只能通过无锡真实数据评测决定。[BGE-M3 paper](https://arxiv.org/abs/2402.03216)
5. **最后接生成模型。** 采用企业批准模型和结构化输出；所有高风险动作继续由确定性策略和人工审批控制。OWASP 2025 明确指出 RAG 不能消除提示注入，权限必须最小化并在代码层执行。[OWASP Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)、[OWASP Vector and Embedding Weaknesses](https://genai.owasp.org/llmrisk/llm082025-vector-and-embedding-weaknesses/)
6. **用影子模式验收。** 至少同时达到 Top-3 有用案例命中率、引用正确率、高风险拒答召回率和分诊时间改善门槛，才允许扩大数据范围。治理流程可参考 NIST GenAI Profile。[NIST AI 600-1](https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-generative-artificial-intelligence)

## 6. 仍需希捷现场回答的问题

1. 希捷全球 drive factory 的现有 GenAI RCA 是否已经覆盖无锡？本项目是接入、补充还是替换？
2. SeaTrack 哪个异常族每月消耗最多工程小时或造成最多报废、返工、Hold 和 DPPM 风险？
3. 当前真实的产品、工站、设备、HSA/材料、程序和固件主键分别是什么？
4. 受限 FA、客户资格和供应商材料数据的授权规则由哪个系统提供？
5. 工厂允许的模型部署、数据驻留、日志保留和网络边界是什么？
6. 六周 PoC 的业务成功门槛应是减少多少分钟、多少工程小时或多少重复调查？

## Caveats and Assumptions

- 本报告不包含希捷内部资料，全部无锡流程判断基于公开招聘、证书、监管文件和希捷公开案例。
- 合成评测 24/24 不能外推到真实现场准确率；题目、数据和算法来自同一项目，存在明显同源偏差。
- 12.45–94.72ms P95 是单机、30 案例和 12 文档条件下的多次烟雾结果，调度波动明显，不代表真实向量库、模型或 SeaTrack 集成延迟。
- 希捷全球公开 AI 成果不等于这些能力已部署在无锡；涉及地点迁移的结论均属于待验证推断。
- 2026 年模型与 API 会继续变化，正式选型必须锁定版本并通过希捷自有评测集回归，不能以“最新”代替“适用”。
