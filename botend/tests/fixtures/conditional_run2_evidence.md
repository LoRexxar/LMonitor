# Conditional real-report regression fixture

`conditional_run2_evidence.json.gz` is a deterministic gzip (`mtime=0`) of JSON extracted from the saved **real** same-build `conditional-core-run2` normal/control artifacts. It is not a fabricated simulator response. The archive contains:

- Original `prepared.simc`, params and schema-4 native proof for each side, unchanged.
- HTML projected by the existing `_native_evidence_html` helper, preserving the native Profile, damage and buff evidence sections.
- Original JSON `git_revision`, `sim.options`, player identity/consumables/gear, and the consumer event's complete stats object. Unrelated simulation metrics are omitted.
- `provenance.sha256` hashes of all original source artifacts before projection.

The pinned binary/source/DBC relation and external trusted authorization remain in `conditional_samebuild_contract.json`. The test passes authorization separately; it never learns authorization from a marker or proof. No test needs `/tmp`, SimC, network access or a live database.

`test_simc_conditional_binding` starts from this positive pair and deliberately mutates copies to reject mismatched actor/spec/talents, paired actor/APL substitution, detached native actor identity, and fractional/inconsistent sample counts. Such mutations are tests, not new combat evidence. Format-only APL regrouping is tested separately: SimC's `player_t::create_profile` writes sorted named lists with `=` then `+=/`, and `init_action_list` splits the accumulated string on `/`. Action order and expressions remain significant; comments, list ordering and equivalent string assembly do not.

The conditional validator requires explicit frozen identity and APL. It fails closed if a built-in APL has not been frozen/expanded or the saved actor context cannot be bound; it does not infer the expected APL from either report. This fixture does not establish production completion/lease/artifact authentication, which belongs to the existing outer execution boundary.
