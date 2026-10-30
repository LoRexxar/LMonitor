# 30c4b061f2103132a34952d80c963303b82bf368 replay compatibility

Pinned game build: `12.1.5.70575`. `manifest.json` schema_version 1 substitutes only 0065, 0067 and 0068, with exact original and replacement SHA256 values. No fallback is authorized for other revisions or changed historical patch bytes. Historical patches remain unchanged. The manifest deliberately contains no extra preimage fields; exact pre/post Git tree identities are retained in the isolated full-replay evidence for the updater owner to evaluate independently.

## Reviewed adaptation

- 0065: re-anchor tracing return/local conversions in Mage, Evoker and Demon Hunter. Preserve current Blistering Scales 0.6 coefficient and added Ebon Might branch. Preserve Collapsing Star secondary-target reduction, not its obsolete primary-target bonus. Convert the newly introduced Blade Dance override as well as existing First Blood override.
- 0067: re-anchor Death Knight target multiplier and Rogue player multiplier types. Do not restore removed Lingering Darkness logic.
- 0068: Collapsing Star provenance now points to effect 2 for its unchanged `chain_target != 0` scalar; do not add the historical `1.0 +`. Preserve both Eternal Hunt double-dip branches and annotate their effect 1; preserve First Blood effect 1. Preserve both added Ebon Might branches and annotate their actual buff effect 1. Re-anchor Mighty Inferno, Reactive Hide and Regenerative Chitin tracing without changing operands. Omit the removed Rogue Lingering Darkness provenance hunk.
- Unmodified original 0069 and 0070 apply successfully after these substitutions.

## Revision-scoped appended tail

`0071-coiled-fangstone-tracing-types.patch` repairs two new upstream `envenomed_bite_t` overrides, plus its accumulator, in `engine/player/unique_gear_midnight.cpp`. It preserves target-count scaling, integer target count, the bite-index branch and ramp arithmetic; handwritten factors retain real source locations rather than guessing effect identities. The manifest's `append_patches` explicitly selects this tail after 0070 for this revision/build only. It is not a global root-level patch and cannot be applied automatically to older upstream revisions. The updater includes the selected tail bytes in candidate identity and the append-only replay ledger; manifest/hash failures stop before source mutation.

The pre-0072 updater filesystem preparation was independently exercised against the pinned upstream. Its candidate matched all 1,164 files in the frozen compilation source, with no byte differences. This verifies that replay stage, not scope approval or publication.

`0072-raw-scope-audit-catalog.patch` adds an independent unreviewed DBC evidence mode. Identity is read from compiled Git/DBC metadata, caller revision/build overrides and mixed export modes are rejected, and output is explicitly draft, unnormalized and not compilable as a contract. It exports no historical global/local classifications. The formal scope and damage exporters retain their existing validation. This tail is explicitly selected by the same revision-bound manifest; it does not approve this revision's scope contract.

## Reviewed scope tail and final candidate

`0073-reviewed-scope-30c4b06.patch` replaces only the generated scope include. It was generated from the exact-build native review, the separately reviewed specialization/mastery collections, and the shared aura fact; it is not a revision-string substitution. The two retired exact Dark Brew effect identities are removed rather than aliased, and four reviewed Rogue category components move into the existing category-exclusion contract. Whole-spell borrowed selectors retain conditional constructor overrides as separate semantic evidence, not fabricated native registration.

The label audit covers all 732 specialization and 146 mastery components. It verifies 85 newly emitted local bindings, including complete selector sets; 67 have exact observed native registration unions, one has partial registration, and 17 have no observed registration in the frozen inputs. These are explicit coverage boundaries, not absent effects. All local/public decisions remain unchanged in the supplementary layers.

The final local candidate compiled successfully. Its 76 successful frozen native inputs also produced normalized exports that passed the unchanged production Service validator, including actual global base-value/mastery-coefficient zero readback. Four known upstream-unsupported inputs remain in the original denominator of 80. The first harness receipt used an incorrect fixed actor name for upstream-named profiles; revalidation used names from the hash-verified inputs and the identical exporter bytes, without weakening any business validation. This does not claim full per-talent production snapshot completion.

Final binary SHA256: `81de193aae26f444e482f8a4e0351d51503b2a146ec87aaaa0f4a09b650084e5`. Final generated include SHA256: `7a35dee29b57c264e70e7775c40d14a5bd41bcabf644761ac1a35307ccc56347`. The actual updater replay matches all 1,164 frozen source files; patchset digest: `b1125942a96a68afb215a67b68dcbebb4b6338a49308d1e79565d1d22d4cf53a`.

Production promotion, same-candidate resource imports, snapshot generation and UI acceptance remain separate release gates. The clean upstream source configuration preserves the legacy patched checkout and the stable active binary path; it never resets unknown legacy edits.

## Verified / not verified

Fresh-index replay from pinned upstream: all 71 enabled historical positions apply with three explicit substitutions, followed by the separate tail. No reverse-skip was needed. Worktree bytes exactly match the final replay index. All 71 historical hashes match the prior audit. Nine file-level comparisons across conflict-affected class files and the tail preserve arithmetic/control-flow tokens after erasing tracing wrappers, tracing types, and explicit double casts. These are adaptation checks, not a claim that every historical patch is semantically neutral. Engine-wide name-based scanning against 18 traced base multiplier interface names reports no remaining double-return conflicts; this is not a C++ compiler or complete inheritance analysis.

The pre-0072 candidate was compiled and exercised by the parent: three frozen normal-combat inputs matched pure-upstream DPS exactly; the actor evidence matrix recorded 76 successful inputs and four exact, known unsupported inputs. Those receipts do not attest the subsequent 0072 binary. Its compilation and raw/formal-mode probes are separately recorded in `raw-audit-build-status.json`. No scope identity was changed and no formal review bypass was enabled.

Evidence directory: `/home/ubuntu/lmonitor-simc-latest-20261028/patch-audit/`
- `full-replay-manifest.json`: full entry identities, pre/post trees, semantic checks and all source file hashes.
- `full-replay.log`: all 71 replay positions and explicit tail.
- `full-candidate-with-tail.diff`: final source diff.
- `source/`: detached candidate, full chain plus tail, ready for parent compilation (not compile-proven).

Pre-0072 candidate Git tree: `1ed3bc1b98ba5284a3c51cb111635be3d4721ad2`.
Source SHA256 (algorithm in evidence): `25e77c3cbd266d6419f07237607aef707bc615fb1ee2584b67a5dae76b492d6c`.

## 技能归一化结构修复追加说明

通用补丁 `0071-base-semantic-scope-and-state-lattice.patch` 增加效果语义规则、专精触发归属及状态组合规划；名称必须排在本目录的首个版本尾补丁之前，保证新建候选能通过严格追加顺序检查。已有候选的补丁账本仍保持原有顺序，新补丁只追加到已有前缀之后。

`0074-semantic-selectors-30c4b06.patch` 将生成的公共选择器规则绑定到本目录已经复核的版本。公共选择器位集合与原版一致；新的物理分量、专精和精通事实由前面的 `0073` 提供，不改写其审查结论，不放开其他版本的校验。正式伤害导出协议升级到 27，产品数据版本升级到 51。

上线时需要对这条完整候选链重新编译，并重新生成技能快照。旧的二进制和旧数据不能作为新版表验收结果。
