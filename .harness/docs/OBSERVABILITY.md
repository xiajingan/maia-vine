# Observability

> 线上观测与验证规范。`release` / `prod-deploy` 部署成功后执行。
> 日志/错误处理规范见 [CODING_BACKEND.md](CODING_BACKEND.md)，部署架构见 [../ARCHITECTURE.md](../ARCHITECTURE.md)。
>
> **查询示例**：[`templates/observability/`](../templates/observability/)
> 其中指标名、窗口、目标和依赖均为示例，必须按项目 Architecture 改写后才能作为证据；
> `scaffold` 只创建目录，不会复制示例形成虚假通过资产。
>
> **校验入口**：`uv run --project .harness/runtime harness observability-check validate`
> **适用资产真源**：`config/harness.yml#observability.required_assets`；仅其中声明的
> `dashboards / alerts / queries / runbooks` 会由门禁强制检查，空数组不制造虚假资产。

**产出物**：`docs/observability-reports/sprint-N-observe.md`（须更新 `index.md`）

---

## 观测维度与触发器

Observe 只验证本项目声明且本次部署影响的信号。数值、窗口、目标设备、依赖名称和告警级别必须来自 `ARCHITECTURE.md` 质量属性基线、部署配置或本次技术方案；Harness 不提供跨项目阈值。

| 维度 | 何时适用 | 最小输出 |
|------|----------|----------|
| 健康/就绪 | Architecture 声明对应 endpoint | 实际 endpoint、期望状态与结果 |
| 日志/Trace | 新增关键链路、失败模式或审计要求 | 项目字段契约、关联 ID 与脱敏查询证据 |
| 指标/SLO | Architecture 声明 SLI/SLO | 指标、项目目标、观察窗口与实际值 |
| 告警/Runbook | SLO 或高风险失败需要人员响应 | 项目阈值、持续窗口、通知通道和 Runbook |
| 部署/依赖 | 本次拓扑、版本、实例或依赖发生变化 | 预期拓扑/版本与实际证据 |

不存在的 Redis、数据库、前端、SSL、P50/P99 或资源指标不得为补齐维度而引入。不可观测但被 Architecture 声明为发布门禁的信号必须阻断，不能写“暂不适用”。

---

## 通过标准与严重级别

**通过**：所有适用信号达到项目声明的目标，并能回溯目标来源和实际查询证据。

| 级别 | 定义 | 处理 |
|------|------|------|
| Critical | 项目声明的可用性/安全红线失败 | 保留候选、立即告警并修复；仅人员明确要求时发布回滚 |
| Major | 项目 SLO、审计、关键链路或必要告警不满足 | 当前迭代修复 |
| Minor | 非阻断信号接近项目预警线或证据质量不足 | 记录 `tech-debt-tracker.md` |

---

## 用例生成指引

> AI 不使用预置命令，须从检查项 + 上游产出物动态生成观测用例。

**输入**：ARCHITECTURE.md（部署拓扑/日志系统/监控系统）+ 技术方案（API 列表/性能基线）+ Release 产出（版本号/URL）

每个适用信号生成一个可执行查询；相同查询能同时证明多个信号时复用证据。格式：`OBS-维度-序号` | 目标来源 | 执行命令 | 期望结果 | 实际结果 | 判定。

---

## 完成标准（DoD）

- 适用性矩阵已记录，每个适用信号都有项目目标与实际证据
- 观测报告已生成并保存至 `docs/observability-reports/`
- 项目声明的健康、日志、指标、告警和部署门禁全部通过

---

## AI 执行协议

**允许工具**：bash（curl/日志查询/监控 API）、文件读取 | **禁止**：代码修改

**执行步骤**：
1. 确认部署完成（版本号、环境 URL）
2. 生成观测用例（按上述指引）
3. 执行 `uv run --project .harness/runtime harness verify health logs metrics`
4. 补充平台特定验证（告警配置、依赖连通等）
5. 生成观测报告，判定通过/不通过
