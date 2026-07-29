# 开发指南

这份仓库的目标不是演示“会聊天”，而是演示一个制造知识助手如何在身份、权限、版本、证据和人工决策边界内工作。改代码时，优先保护下面五个不变量：

1. 身份只来自服务端验证后的令牌，不能信任请求体里的角色或范围；
2. 角色、产线、站点、密级和文档状态必须在检索前过滤；
3. 返回的每个建议步骤都必须指向用户能打开的有效证据；
4. 无证据和高风险生产动作必须升级或拒绝，不能编造根因；
5. 来源数据只有通过签名、schema 和主数据对账后才能进入版本账本。

## 本地开发

要求 Python 3.11+ 和 Node.js（仅用于检查无构建依赖的前端 JavaScript）。项目运行时只依赖 Python 标准库。

```bash
make check
make run
```

`make check` 依次执行语法检查、可重复数据生成、数据完整性校验、单元测试、24 个业务评测和真实 HTTP 全系统测试。提交前还应运行：

```bash
git diff --check
```

## 代码地图

| 文件 | 单一职责 | 修改时优先验证 |
| --- | --- | --- |
| `server.py` | HTTP、认证、CORS、请求 ID、错误映射 | `scripts/full_system_test.py` |
| `rag_app/auth.py` / `oidc.py` | 可信身份归一化和令牌验证 | `tests/test_auth.py`、`tests/test_oidc.py` |
| `rag_app/validation.py` | 外部请求 schema 与身份范围约束 | `tests/test_security_regressions.py` |
| `rag_app/repository.py` | 合成目录、来源覆盖层和读取 ACL | `tests/test_retrieval.py`、`tests/test_ingestion.py` |
| `rag_app/retrieval.py` | 权限过滤后的混合候选排序 | `tests/test_retrieval.py`、`scripts/evaluate.py` |
| `rag_app/service.py` | 分诊编排、证据编译和拒答/升级 | `tests/test_service.py`、`scripts/evaluate.py` |
| `rag_app/storage.py` | 调查状态机、审计和来源版本账本 | `tests/test_workflow.py`、`tests/test_source_operations.py` |
| `rag_app/ingestion.py` | 离线导出 schema、边界和内容校验 | `tests/test_ingestion.py` |
| `rag_app/source_manifest.py` | 交付清单、摘要和 RS256 信任边界 | `tests/test_source_manifest.py` |
| `rag_app/reconciliation.py` | 来源记录与批准主数据对账 | `tests/test_source_operations.py` |
| `rag_app/source_operations.py` | 作业锁、分阶段同步、检疫与健康 | `tests/test_source_operations.py` |

更完整的调用顺序和设计理由见 `docs/architecture-walkthrough.md`。

## 常见变更方法

### 增加一个调查上下文字段

同时修改请求校验、检索结构化评分、测试数据生成和评测样例。不要让新字段绕过 `Identity` 的 line/station 范围，也不要只在前端校验。

### 增加一个 API

先确定权限，再实现路由。所有业务 API 必须通过统一认证边界，使用结构化 JSON 错误，并保留 `X-Request-ID`。详情接口对无权资源返回 404，避免暴露资源是否存在。

### 增加一个来源实体

需要同步更新导出契约、严格字段白名单、主数据关系、内容摘要、来源账本、回滚、目录覆盖层和 ACL 测试。来源文件只读取一次；检疫只保存脱敏元数据，不复制原文。

### 调整检索权重或阈值

权重和拒答阈值集中为命名常量。调整必须给出业务评测结果，尤其检查“相同 Failure Code、不同根因”和“未知代码升级”，不能只看平均分。

## 注释与错误处理

- 注释解释“为什么”和必须保持的安全/业务不变量，不复述语法；
- 边界层错误使用稳定、面向调用方的类型和状态码；详细原因进入带请求 ID 的日志；
- 不使用裸 `except`，不把异常静默转换成空结果；
- 测试夹具显式提交并关闭数据库连接；
- 新增外部网络调用时必须设置协议、主机、大小和超时边界。

## 合成数据与品牌边界

仓库中的产品、Failure Code、案例、文档和指标都是虚构样本。贡献者不得加入真实希捷数据、凭据、内部 URL、组织映射或未获授权的文档。任何简历、演示和说明都必须称其为“面向 SeaTrack 类 MES 场景的合成数据 PoC”，不能称为希捷内部系统或生产部署。
