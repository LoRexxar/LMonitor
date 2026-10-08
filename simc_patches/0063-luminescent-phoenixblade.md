# Luminescent Phoenixblade 原生模型

## 范围与事实

`simc_patches/0063-luminescent-phoenixblade.patch` 为 item `281056` 的 driver `1310252` 注册原生实现。数据源为 SimC 固定上游 `db768b52b425db274e4ebd3ce3d5efc26c8882b5` 的 PTR `12.1.5.70077`。`ptr=1` 仍由原有 Composer/冻结输入链控制；该补丁不替代 PTR 选择。

- 治疗来源触发伤害 action `1310255`，数值读取 driver E1 `average(effect)`。
- 伤害来源触发治疗 action `1310258`，数值读取 driver E2 `average(effect)`。
- 保留数据中的 RPPM、缩放、ICD、proc mask 与标准 callback eligibility；在抽取触发机会前检查来源和目标。
- 不设置 `execute_action=damage`，避免默认 callback 把治疗来源挡在触发机会之前。
- 不修改全局 callbacks，不放宽有效原生特效、控制组或收益有效性校验。

## 模型边界

这是客户端数据和引擎惯例驱动的模型，**不是游戏战斗日志实测结论**。保持 `UNVERIFIED_IMPLEMENTATION` 提示：自疗来源资格、服务器过滤、双向共享机会池、局部两子技能防递归规则尚未游戏实测。当前治疗目标为自身、伤害目标为唯一敌人。

超过一个敌人时明确失败，而非返回看似有效的零收益。该边界既在初始化检查，也在运行时活动目标检查；不允许将未经支持的多目标结果投影为正式收益。后续扩展目标模型必须单独实现并验证，不通过删除检查宣称已支持。

## 交付链

- Backend 继续使用现有 `update_simc_binary` 的有序项目补丁链。
- Agent 仅携带本次所需的受管原生 runtime patch，不引入 Backend exporter 补丁集；在隔离构建副本应用。
- 同一上游 revision 的补丁变化也必须触发重建；只有构建/探针通过并激活后的二进制才能上报对应补丁身份。
- 不向上游源码 checkout 写入实现，不手工复制替换线上 binary，不改冻结历史结果。

## 已执行的本地验证

在固定上游的隔离构建中真实编译，使用失败任务的 normal/control 冻结输入，经原有预处理/原生校验重放：

1. 无实现基线复现 `No constructible buff or action`。
2. 有实现后两侧通过原有原生有效性校验。
3. normal 报告中伤害 `1310255` 与治疗 `1310258` 的执行次数均大于零；control 中两者均不存在。
4. 两侧实际报告均使用 PTR `12.1.5.70077`。
5. `desired_targets=2` 真实执行返回非零（40）并给出 single-enemy model 错误。
6. 在独立临时 index 上，从干净上游按序重放完整 64 个 Backend 补丁成功；原生 patch 单独 `git apply --cached --check --whitespace=error-all` 成功。

本地行为验证不等于正式执行验收，更不等于游戏机制实测。发布验收限于既有凰刃单一坐标、四个装等的 normal/control 加一个 baseline；通过实际终态、完整报告 build、控制效果与面板投影回读确认。
