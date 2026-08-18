---
name: harness-managed
description: 在用户主动发起规划后，检查分发需求、提交响应、将已接受工作纳入本地 Story/Sprint，并发布不可变 Delivery Manifest。
---

1. 仅在用户主动启动需求规划或 Sprint 规划后使用；本 Skill 不唤醒或启动 Agent。
2. 用户入口是 Chat 中的规划意图。内部执行模式校验并检查 `docs/assignments/inbox/`，不要求用户手工驱动 CLI。
3. `product`、`architecture`、`dependency` Assignment 使用同一输入协议。必须明确返回 `accepted`、`accepted_with_changes`、`deferred` 或 `rejected`；接受的工作更新或引用本地 `USER_STORIES.md`，并记录本地 Story/Sprint。
4. 本地规划确认纳入后，才允许使用 `harness-task`。
5. 组件质量通过后，生成并登记不可变制品、签名、SBOM、Build Once 证据和契约，再调用内部 Delivery 发布端口；随后调用 Managed 的 `delivery verify`，通过项目配置的 verifier 真实验证签名、SBOM 与 Build Once 并生成受管 receipt。Control 在系统组合时仍独立复核最终 Delivery。
6. Dependency Assignment 的版本是 Delivery 输出，接收时不得猜测；公共依赖以 `dependency-package` 发布精确版本和 SHA-256。
7. 不得部署共享 Test/Production、编辑其他工程业务文件，也不得暗示存在远程或自动触发能力。

规则和 Schema 保持在 `.harness/rules/` 与 `.harness/schemas/`；本 Skill 只引用，不复制规则正文。
