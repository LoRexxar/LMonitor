# 进度日志

## 2026-04-22

### Session 1
- 接收 SimC 优化需求，目标包含 5 项：
- 配置专精字段。
- 配置管理筛选。
- 常规模拟 time/目标数内置选项。
- 天赋预览图。
- APL 列表翻译预览。
- 完成现状审计（模型/API/执行器/模板/前端入口）。
- 新建规划文件：`task_plan.md`、`findings.md`、`progress.md`。
- 当前进入 Phase 1（数据模型与迁移）准备阶段。
- 根据新增要求更新计划：
- 除 `SimcProfile` 外，模板也增加专精字段并实现匹配替换能力。
- 属性模拟步长默认 50，新增可配置入口并下沉到任务执行参数。
- 再次补充规则：模板支持多启用，不再有启用冲突；执行时按专精匹配已启用模板。

### Session 2
- 已开始落地代码，完成模型与迁移：
- `SimcProfile` 新增 `spec` 字段。
- `SimcTemplate` 新增 `spec` 字段。
- 新增迁移：`0038_simcprofile_spec_simctemplate_spec.py`。
- 已改 SimC 执行链路：
- 模板允许多启用，执行时按配置专精匹配模板（支持逗号专精匹配、default/all/* 回退）。
- 常规模拟支持任务级 `regular_time/regular_target_count` 覆盖。
- 属性模拟支持任务级 `attribute_step`，默认 50。
- 已改 Dashboard 前端入口：
- 任务新增/编辑增加常规模拟覆盖参数与属性步长输入。
- 配置新增/编辑增加专精输入。
- 模板新增/编辑增加适配专精输入，模板列表展示专精列。
- 快速模拟弹窗增加常规模拟覆盖参数与属性步长。

### Session 3
- 已完成 SimC 配置管理筛选：
- 后端 `get_table_data` 新增 `simc_spec/simc_fight_style` 过滤参数并在 `SimcProfile` 生效。
- 前端新增筛选栏（专精、战斗风格）和“应用/清空”操作。
- 已完成 APL 列表“翻译预览”：
- 列表新增“翻译预览”按钮，可并排展示原文与中文翻译并支持复制。
- 已完成天赋预览入口（尝试版）：
- 在新增/编辑 SimC 配置弹窗增加天赋预览卡片，自动根据专精和天赋串更新图示与跳转链接。

### Session 4
- 补齐模板匹配一致性：
- 前端 `generateSimcCode` 与后端模板路由规则对齐，支持模板 `spec` 使用逗号分隔多专精匹配。
- 前端增加旧模板兼容：无 `{spec}` 占位符且存在 `spec=` 行时自动覆盖为当前配置专精。
- 检测到仓库中存在额外迁移 `0039_alter_gewechatauth_id_alter_gewechatroomlist_id_and_more.py`，已与你确认“保留并继续”。
- 继续补强多专精模板匹配：
- 后端 fallback 逻辑支持 `default/all/*` 出现在逗号专精列表中。
- 前端本地生成逻辑同步同样规则，避免预览和执行结果不一致。
