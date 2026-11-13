# Maxroll encoding catalog (schema 1)

`maxroll-encoding.json.gz` is a structural projection of frozen Maxroll game
assets, **not a table of input/output codes**. Runtime conversion lives in
`../maxroll.py`; native output uses the existing `TalentBuildCodeEncoder`.

## Provenance

- Official bundle: https://assets-ng.maxroll.gg/wow-tools/static/js/embed.js
- Source map: the same URL with `.map` appended.
- Game data URL referenced by this frozen bundle:
  https://assets-ng.maxroll.gg/wow-tools/game/data.min.json?20a5e03e
- Game data version: `12.1.5.70391`. Individual tree build/version values are
  preserved; neither the data version nor success guarantees current-patch
  compatibility.
- Offline research inputs:
  `/home/ubuntu/lmonitor-scheduler-readonly/article-talents-research/`:
  `REPORT.md`, `candidate.py`, `verify.py`, `official-4.ts`, `official-13.ts`,
  `official-25.ts`, `encoding-catalog-full_corpus.json.gz`.
- Exact catalog scope: 23 source tree hashes, covering all 312 distinct
  `(spec, code)` pairs in 1,678 talent blocks across all 160 Maxroll articles
  from the 200-article frozen inventory, without latest-version or role filters.
  Each hash supports algorithmic conversion of other builds on that exact tree;
  unknown hashes fail closed. No online fallback is permitted.

SHA-256 (raw file bytes):

| Artifact | SHA-256 |
|---|---|
| Official bundle | `8d895399334c1e49759ff79edb1a828de5cc2b7cb19f4691d71d282e4a7e88cc` |
| Official source map | `bc37ac0ef380890bea3a41a2d04fbdd8256f5c20697a314ab871731e706760cd` |
| Official game data (54,176,109 bytes) | `69854d75a5cea19762f6ffedd526e33572388d05de2b614ecacb04d92537641a` |
| Catalog gzip (112,184 bytes) | `3f7bef1ad277d56a6a1f662d22b44424afab25531a408510bfd18b06512af02c` |
| Uncompressed catalog (1,068,002 bytes) | `5390e060fd7b09aae6889cf0e927440a6876112a0ffb09fafd89a82ccb5c9eca` |
| Original article inventory | `7dbf886cdb1ce726d9b057c0b3093abc28e54675bdd4490ca7338e20cf583e94` |

## Schema and update contract

The package is byte-for-byte the research `full_corpus` catalog, compressed
with `gzip.compress(payload, mtime=0)`. The offline `candidate.project(data,
hashes)` projector retains spec-to-class mappings, SpecSets, tree class/build,
all nodes in ascending numeric ID order, original Entry order/rank limits,
node flags/type/subtree, node/group conditions and costs, group membership,
and parent edges. `PosY` remains as source evidence but runtime does not use it.
It excludes spell text, items, names, art, currency availability and UI data.

Do not blindly refresh this package or change a digest to accept new data.
An update requires reviewing the official import/view/export algorithms and
projected fields, preserving the full historical node universe, validating
all references and semantic flag values, and rerunning independent official
oracles. Unknown schema/fields/flags/condition types are rejected. Conditions
of types 4 and 5, condition flag 1, and non-Apex node flags are known in this
frozen revision: they have no additional effect on nonediting export in the
reviewed official module, rather than being unknown fields silently ignored.
Repeated edge pairs can result from distinct official edge records after
projection; they remain in source order. Duplicate object/Entry identities,
bad references, cyclic graphs and excessive graph depth are rejected.

The loader authenticates gzip bytes before decompression, authenticates the
JSON payload and source-data digest, then validates schema, bounds and
references once per process. Changes to a package require process restart.

## Adapter protocol

```python
from botend.wow.talents.maxroll import convert_maxroll_code
native = convert_maxroll_code(source_code, expected_spec=265)
```

Only version **130** is accepted. `expected_spec` is `None` or an integer
specialization ID (not a numeric string). Failure raises `ValueError` with a
short machine-readable reason. Caller handles native game codes separately;
this module does not expose `native_header` or claim to validate native builds.

The wire format uses six-bit LSB-first characters with standard or URL-safe
alphabet. Header = 8-bit version, 16-bit spec, 16-byte source tree hash.
Nodes follow the complete exact-hash sorted node universe; Apex Entry pools
and choices differ. Three URL-only PvP slots are read and discarded. The
semantic pass reconstructs grants, visibility, condition cache identity,
group self-exclusion and parent connectivity before native encoding. Output
is version **2**, same spec, zero hash, with no PvP tail. No custom native
node writer, runtime network, DB access or JS execution is used.

Official URL exports trim whole zero bytes; bounded EOF zero-extension is
intentional. The protocol has no checksum, so not every truncation is
recognizable. Invalid selections, unproven grants, unknown/nonzero tails and
excess padding are rejected. Full-level points and simulator support (including
healing roles) are **not** format-conversion requirements.

## Verification

`botend/tests/fixtures/maxroll_talent_oracle.json` contains the 312 official
first-export results and two separately labelled semantic probes (original
and omitted gifted-node bit for node 71933). These are test-only expected
outputs, never loaded by the runtime adapter. The gift probe demonstrates
why replacing the header while copying payload is insufficient.

The frozen official bundle was re-executed offline against its full game data
for all 312 inputs and both gift probes during implementation: 312/312 and
2/2 exact production-adapter matches, zero official errors/invalid selections,
zero network requests. Only the webpack entry is replaced to expose the
original unmodified official modules; official JS is used for offline oracle
verification, not the application or routine test runtime.

Run the Python regression suite (no database needed by these SimpleTestCases):

```sh
flock -w 180 /tmp/lmonitor-test-suite.lock .venv/bin/python -B manage.py test \
  botend.tests.test_maxroll_talent_codec \
  --settings=LMonitor.settings_test_sqlite --noinput
```

Tests include both alphabets for all 312 codes; gifted-point restoration;
invalid version/spec/hash/class, rank/choice/Apex encoding, unproven grants,
trailing data; digest tampering; schema/unknown semantics/reference failures;
duplicate identities; cycles/depth bounds; and runtime network/process denial.
