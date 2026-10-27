# WOWTERM 漏出门禁与后续修复边界

本轮仅本地代码与 SQLite 离线回放；不调用模型、不访问/改写生产、不 push/deploy，不回填既有文章。没有增加整篇候选数量限制。

## 已实现

- `ProtectedText.is_intact` 保持合法 token 的精确多重集合校验，并拒绝合法 token 之外的 `WOWTERM_` 保留命名空间（不依赖括号形状或数字宽度）。
- 整块精确 key、同 source 历史缓存、文本节点缓存均拒绝带残留的旧译文；继续走原有正常缓存复用、逐节点恢复或失败补译路径。
- 新译文、确定性本地还原和节点最终重组均检查还原后的残留，不通过删除 token 或猜词强行修复。
- 坏缓存不删除、不覆盖。现有 `get_or_create` 保留其原始证据；本次新生成且通过校验的结果可供当前翻译返回，但不会自动覆盖同 key 坏行。因此在人工修复缓存前，后续任务可能再次需要补译，不能声称已完成缓存治理。

## 离线证据

复用 `/home/ubuntu/lmonitor-maxroll-refresh/placeholder-review` 的既有诊断，无新增全19篇采集。

`botend/tests/fixtures/maxroll_placeholder_cache_2389.json` 精确摘自 `production-probe.jsonl`：guide 138 / b-3-1-0 / title / cache 2389。测试重建相同 protected/replacements 与原 key，在真实 SQLite ORM 建缓存，调用生产 `translate_blocks`，确认拒绝发布、保留来源并返回失败，坏行内容不变。词表构建以冻结事实注入，非生产数据库重采；模型响应测试使用纯本地确定性 replay。

- 修复前同目标 RED：6 tests，13 个失败子断言，无执行错误，`/tmp/placeholder-red.log`。
- 修复后同目标 GREEN：6 tests 通过，`/tmp/placeholder-green.log`。
- 补充可补译但保留坏行证据正例，并运行既有缓存/引用/节点 fallback/损坏拒绝用例：最终 10 tests 全通过，`/tmp/placeholder-green-final.log`。

复验命令：

```sh
/home/ubuntu/LMonitor/.venv/bin/python manage.py test \
  botend.tests.test_guide_placeholder_integrity \
  botend.tests.test_class_guides.GuideFlowTests.test_translation_is_cached_and_preserves_reference \
  botend.tests.test_class_guides.GuideFlowTests.test_failed_markup_translation_falls_back_to_visible_nodes \
  botend.tests.test_class_guides.GuideFlowTests.test_translation_corruption_is_not_cached \
  --settings=LMonitor.settings_test_sqlite --noinput
```

## 安全补译 / 缓存修复建议（需另行授权）

1. 先部署门禁，再按既有 ledger 冻结精确 cache ID/key/source hash/旧译文 hash 及 guide ID/字段；保留原文、旧缓存、保护映射和审校记录，不扩大成全篇付费重译。
2. 不按数字反向猜 key，不机械正规化括号再全量替换。cache 1143 已有明确语义错位：原文 `One of the strongest ... Similar to ...` 的“一”已在中文表达一次，又漂移到“类似”之后；恢复编号会产生“类似一”。此行必须人工完整审校或授权定向补译。其他行也不能把身份吻合当成语义正确。
3. 获准后仅处理确认坏的字段，优先使用通过新门禁的正常缓存；缺口走原失败补译流程。若选择修复同 key 缓存，先持久化旧证据，再用旧 hash/CAS 核实未并发变化后写入经结构、引用次数、官方术语和语义审查的候选，不批量删行。
4. 最后对精确受影响文章走既有人工保护/并发保护发布，独立回读正文与快照；扫描括号无关的 `WOWTERM_` 并抽验 DOM。代码修复不等于历史文章已修复，不能以零 untranslated 代替此终验。

合并注意：仅修改 `class_guide_service.py` 的占位符 import、缓存分支与还原校验；不触碰该文件的宏候选逻辑或 `wow_localization.py`。并行宏补丁若冲突，保留双方各自独立职责。
