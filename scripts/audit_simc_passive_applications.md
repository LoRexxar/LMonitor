# 原生被动：初始化前 parser-only 反事实工具

**仅诊断，不是生产生成器接入。不要把本分支的实验补丁自动部署到正式 Backend。**

## 目的与边界

DBC 来源候选、parser 登记、base/state 的数值一致都不能证明来源经过派生 action 覆盖后仍保留。本工具在相同冻结输入、相同二进制中，从初始化前仅排除一个来源 effect 的 parser 注册，再比较实际 action/component/state/金额；不修改 DBC effect，也不执行常规战斗。

- `0064` 只导出 `parser_consistency` 诊断，不代表 applied。
- `0065` 增加 exporter-only 的 `LMONITOR_SIMC_PARSER_EXCLUDE=source_spell_id:zero_based_effect_index,...`。同时跳过该 effect 的注册和撤销，避免未注册却执行逆运算；不改 spell/effect 数据。
- 排除模式的 JSON 明确携带 `parser_counterfactual`。正式 Service 必须拒绝此标记，不能把禁用后的伤害当正常快照发布。
- 普通 SimC 运行不启用此干预；CLI 只为被显式要求的 exporter 子进程设置环境，不修改父进程或主机配置。

## 编译与使用

从指定 upstream revision 的隔离工作树，按项目受管补丁顺序重放，再在隔离构建目录编译。不要修改上游同步 checkout，也不要覆盖线上/活动 binary。本工具不承担下载、部署、资源导入或创建快照。

准备自包含的、由正式 Composer 生成的冻结 `.simc` 输入，以及与构建匹配的 revision/game build，然后执行：

```bash
python3 scripts/audit_simc_passive_applications.py \
  --binary /absolute/path/to/isolated/simc \
  --input /absolute/path/to/frozen-actors.simc \
  --revision <exact-40-character-upstream-sha> \
  --game-build <exact-game-build> \
  --output-dir /absolute/path/to/new-output-directory
```

- 输出目录必须不存在，防止覆盖之前的证据。
- 默认目标血量100；其他冻结场景需明确传 `--target-health`。
- 先进行一次正常导出，按实际 native 候选发现 effect；逐 effect 做独立导出，再只为实际观察到的多来源组件集合做精确联合排除，不枚举幂集，也不用全局候选并集代替组件子集。
- 默认最多16个候选 effect、16个精确联合集合；主动扩大边界才传 `--max-effects` / `--max-joint-groups`，超限不得静默丢弃覆盖。
- 每次子进程前后校验 binary/input SHA256，防止更新过程混入另一套事实。输入的外部 include/可变依赖不在文件哈希覆盖范围内，调用者必须先用自包含冻结输入消除它们。
- 禁止 `python -O`：工具会主动拒绝关闭断言的运行方式。

## 证据校验

1. 正常/禁用输出的 exporter identity、actor metadata、action 身份和非伤害 action facts一致。
2. parser ledger 仅移除请求的来源 effect；其他登记 identity/value 和 flat 不变，pct 比值与请求 effect 的有效 average 值一致。
3. 场景 identity 使用实际 Buff 条件（包括 scope、spell、stacks），不把 `delta_pct` 等浮点派生值当 key；场景其他 metadata 单独比较。先要求 on/off 场景全集和各场景 direct/tick 的存在集合相同，不能只遍历 on 而忽略 off 多出的场景或组件。
4. component base、state da/ta、hit/crit/expected、贡献及全部实际导出的 target maps 都按同一因子变化；其他 component facts不变。零伤害、缺场景、unresolved 和额外变化不授权。
5. `verified` 是**这次单独或联合干预**通过比较的记录，不等于可直接生成顶部全局行。多个 effect 要求每项独立比较和同一 component/state 的联合比较均通过，且联合倍率等于各项乘积。
6. `unregistered` 与 `rejected` 分开保留。缺登记不等于整个游戏实现无此被动；不通过证明的项保持原值，不按职业、spell ID 或倍率特判。

`probe-report.json` 保存正常与每个排除导出的路径、哈希、输入/binary 哈希、实际 `target_health_percentage`、执行参数、耗时，以及每个实际 component/state 的 verified/rejected/unregistered 明细。进程退出成功和 `status=compared` 不等于没有被拒绝的组件，必须检查明细。

## 后端共享验证入口

CLI 与 `botend.services.simc_skill_passive_evidence` 共用同一比较谓词。`verify_passive_applications(ordinary, counterfactuals)` 接收可信执行器创建的 `PassiveProbeExport`，逐份核对冻结输入、binary 哈希与实际目标血量；血量是命令行覆盖项，不在输入文件哈希中。`target_health_percentage` 缺失、非法或 on/off 不同均拒绝，不能默认为100，也不能从一份旧 JSON 猜回该参数。这些执行身份不能从客户端任意 JSON 声称中获得。

单来源用自身独立对照；多来源必须找到**与当前组件待剥离来源集合完全相同**的联合排除。全局候选并集即使结果满足乘积，也可能掩盖子集联动，不能代替精确子集。缺精确对照与已对照但不通过分别记录，不重试后者来伪造成功。

该入口返回证据计划，不修改 native 数据、不接受 `application.applied` 标记，也不自动启用旧 helper 的剥离。读写已冻结的证据计划不能替代对原始成对输出的验证。

## 已核对的接入边界（尚未实现）

- 在 `_run_profile_export()` 的冻结输入/执行上下文尚在时取得可信证据；正常输出与 off 诊断输出保持隔离。canonical actor 经 spool 改为 logical alias 前保留原始身份、血量、action/reporting-root 复合身份和完整场景引用。
- 全场景一致性授权要在原始 action 上完成，不能等 flatten 清空 scenarios 或 activation 裁剪动作后再证明。当前 Service 的 `components` 是逐状态证据，不是“该 action 所有状态均可剥离”的生产授权。某个状态缺失/拒绝时，不能只归一化 baseline 而让 Buff 行保留原值。
- reference/selected、high/low 和 baseline/scenario 的比较始终使用原值。`flatten_single_talent_damage_variants()` 的 amount 拷贝只传证据；`complete_cast_damage_components()` 补出的 child 只能用它自己的证据。不要把旁路证明插入参与比较签名的 `runtime_layers`。
- 数值处理放在 `project_skill_damage_product_payload()` 深拷贝之后、root 聚合之前：同步 hit/crit/expected/贡献、所有目标映射、对应 da/ta 与 base component multiplier；只从 `actor_baseline` 阶段去掉因子，不动 `runtime_scenario` 边际。baseline 因子原为1时也需要明确阶段证据，不能根据缺字段猜。之后调用 `attach_runtime_product_metrics()` 重建缓存，再走原聚合。
- 顶部展示不能只在生成结束 append：读时 `reviewed_global_display_effects()` 会重新生成列表。需新增仅记录“已验证且已实际完成数值剥离”的持久化事实容器，并在生成/读时共用 wrapper 合并；无 reviewed catalog 的生成路径也要覆盖。应用证明不等于全技能 scope，必须携带适用 action/component/条件。零基 passive effect_index 不冒充 reviewed catalog 的物理 effect_id。

## 尚未实现的生产工作

本工具不写回 `application`，不触发 helper 剥离，也不更新数据库、Backend 或快照。其覆盖只等于提供的冻结输入，不冒充全部天赋、前置组合、血量和英雄树。

初始化成本随不同候选数增加。接入生产前必须测量完整生成任务的时间、进程数、内存和取消边界，不能将基线小样本的成功当成可上线性能证明。不能跨不同输入/场景复用来源证明，也不能把测试限制永久变成隐藏的覆盖缺口。
