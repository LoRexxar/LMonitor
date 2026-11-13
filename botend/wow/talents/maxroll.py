"""Offline Maxroll v130 -> Blizzard v2 source adapter.

Only exact, audited source tree hashes are accepted. The source is a stream of
LSB-first six-bit characters, NOT RFC 4648 base64 bytes. URL exports omit zero
bytes at EOF; bounded zero-extension is intentional and cannot detect every
truncation (the protocol has no checksum). PvP slots are consumed, not exported.
No current-tree substitution, per-code mapping, network, database or JS runtime.
See data/README.maxroll.md for provenance and supported semantics.
"""

from collections import Counter, deque
from functools import lru_cache
import gzip
import hashlib
import json
from pathlib import Path
import re

from .build_code import TalentBuildCodeEncoder

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_CATALOG_PATH = Path(__file__).with_name("data") / "maxroll-encoding.json.gz"
_CATALOG_SHA256 = "3f7bef1ad277d56a6a1f662d22b44424afab25531a408510bfd18b06512af02c"
_PAYLOAD_SHA256 = "5390e060fd7b09aae6889cf0e927440a6876112a0ffb09fafd89a82ccb5c9eca"
_SOURCE_SHA256 = "69854d75a5cea19762f6ffedd526e33572388d05de2b614ecacb04d92537641a"
_CONDITION_FIELDS = {
    "CondType",
    "Flags",
    "SpecSetID",
    "SpentAmountRequired",
    "TraitNodeGroupID",
    "TraitNodeID",
    "TraitCurrencyID",
    "GrantedRanks",
}


def _require(test, reason):
    if not test:
        raise ValueError(reason)


def _integer(value, low=0, high=2**31 - 1):
    _require(type(value) is int and low <= value <= high, "catalog_integer")


def _object(value, required, optional=()):
    _require(type(value) is dict, "catalog_object")
    _require(
        set(required) <= value.keys() <= set(required) | set(optional), "catalog_fields"
    )


def _ids(value, maximum):
    _require(type(value) is dict and 0 < len(value) <= maximum, "catalog_id_map")
    for key in value:
        _require(
            type(key) is str and re.fullmatch(r"[1-9][0-9]{0,8}", key), "catalog_id"
        )


def _array(value, maximum):
    _require(type(value) is list and len(value) <= maximum, "catalog_array")


def _validate_catalog(catalog):
    """Fail closed on unknown schema, semantic flags, bad references or graphs.

    Bounds and DAG/depth validation precede recursive semantic evaluation.
    Types 4/5 and flag 1 are known official no-ops in nonediting export mode.
    """
    _object(
        catalog,
        {
            "schema",
            "source_data_sha256",
            "source_version",
            "specs",
            "specSets",
            "trees",
        },
    )
    _require(
        type(catalog["schema"]) is int and catalog["schema"] == 1, "catalog_schema"
    )
    _require(catalog["source_data_sha256"] == _SOURCE_SHA256, "catalog_source")
    _require(catalog["source_version"] == "12.1.5.70391", "catalog_source_version")
    specs, sets, trees = catalog["specs"], catalog["specSets"], catalog["trees"]
    _ids(specs, 100)
    for spec, cls in specs.items():
        _integer(int(spec), 1, 65535)
        _integer(cls, 1, 64)
    _ids(sets, 256)
    for members in sets.values():
        _array(members, 100)
        for member in members:
            _integer(member, 1, 65535)
            _require(str(member) in specs, "catalog_spec_reference")
        _require(len(members) == len(set(members)), "catalog_duplicate_spec")
    _require(type(trees) is dict and 0 < len(trees) <= 64, "catalog_trees")
    for tree_hash, tree in trees.items():
        _require(
            type(tree_hash) is str and re.fullmatch(r"[0-9a-f]{32}", tree_hash),
            "catalog_hash",
        )
        _object(tree, {"class", "version", "nodes", "groups", "edges"})
        _integer(tree["class"], 1, 64)
        _require(tree["class"] in specs.values(), "catalog_class_reference")
        _require(
            type(tree["version"]) is str
            and re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", tree["version"]),
            "catalog_build",
        )
        nodes, groups = tree["nodes"], tree["groups"]
        _ids(nodes, 512)
        _ids(groups, 512)
        entry_ids = set()
        for node in nodes.values():
            _object(
                node,
                {"Type", "Flags", "Entries", "PosY"},
                {"TraitSubTreeID", "Conditions", "Cost"},
            )
            _integer(node["Type"], 0, 3)
            _integer(node["Flags"])
            _require(node["Flags"] in (0, 1, 8, 9, 256, 264), "catalog_node_flags")
            _integer(node["PosY"], -100000, 100000)
            _integer(node.get("TraitSubTreeID", 0))
            _array(node["Entries"], 4)
            _require(bool(node["Entries"]), "catalog_empty_entries")
            for entry in node["Entries"]:
                _object(entry, {"ID", "MaxRanks"}, {"TraitSubTreeID", "Cost"})
                _integer(entry["ID"], 1)
                _integer(entry["MaxRanks"], 0, 63)
                _integer(entry.get("TraitSubTreeID", 0))
                _require(entry["ID"] not in entry_ids, "catalog_duplicate_entry")
                entry_ids.add(entry["ID"])
            _require(
                sum(e["MaxRanks"] for e in node["Entries"]) <= 63, "catalog_rank_pool"
            )
        for group in groups.values():
            _object(group, {"Nodes"}, {"Conditions", "Cost"})
            _array(group["Nodes"], 512)
            for nid in group["Nodes"]:
                _integer(nid, 1)
                _require(str(nid) in nodes, "catalog_node_reference")
            _require(
                len(group["Nodes"]) == len(set(group["Nodes"])),
                "catalog_duplicate_member",
            )
        objects = [
            *nodes.values(),
            *groups.values(),
            *[e for n in nodes.values() for e in n["Entries"]],
        ]
        currencies = set()
        for obj in objects:
            _array(obj.get("Cost", []), 32)
            for cost in obj.get("Cost", []):
                _object(cost, {"TraitCurrencyID", "Amount"})
                _integer(cost["TraitCurrencyID"], 1)
                _integer(cost["Amount"], 0, 1000)
                currencies.add(cost["TraitCurrencyID"])
        for obj in objects:
            _array(obj.get("Conditions", []), 64)
            for cond in obj.get("Conditions", []):
                _object(cond, _CONDITION_FIELDS)
                for value in cond.values():
                    _integer(value)
                _require(cond["CondType"] in (0, 1, 2, 4, 5), "catalog_condition_type")
                _require(cond["Flags"] in (0, 1, 2, 4), "catalog_condition_flags")
                _integer(cond["GrantedRanks"], 0, 63)
                for field, lookup in (
                    ("SpecSetID", sets),
                    ("TraitNodeID", nodes),
                    ("TraitNodeGroupID", groups),
                ):
                    _require(
                        not cond[field] or str(cond[field]) in lookup,
                        "catalog_condition_reference",
                    )
                _require(
                    not cond["TraitCurrencyID"]
                    or cond["TraitCurrencyID"] in currencies,
                    "catalog_currency_reference",
                )
        _array(tree["edges"], 4096)
        children = {int(n): [] for n in nodes}
        degree = dict.fromkeys(children, 0)
        depth = dict.fromkeys(children, 0)
        for edge in tree["edges"]:
            _array(edge, 2)
            _require(len(edge) == 2, "catalog_edge")
            left, right = edge
            _integer(left, 1)
            _integer(right, 1)
            _require(left in children and right in children, "catalog_edge_reference")
            # Official distinct edge records can project to the same pair.
            # Their repeated parent tests are harmless; preserve source order.
            children[left].append(right)
            degree[right] += 1
        queue = deque(n for n in degree if not degree[n])
        visited = 0
        while queue:
            node = queue.popleft()
            visited += 1
            for child in children[node]:
                depth[child] = max(depth[child], depth[node] + 1)
                _require(depth[child] <= 128, "catalog_graph_depth")
                degree[child] -= 1
                if not degree[child]:
                    queue.append(child)
        _require(visited == len(nodes), "catalog_graph_cycle")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "catalog_duplicate_key")
        result[key] = value
    return result


@lru_cache(maxsize=1)
def _load_catalog():
    try:
        packed = _CATALOG_PATH.read_bytes()
        _require(
            hashlib.sha256(packed).hexdigest() == _CATALOG_SHA256, "catalog_digest"
        )
        # Authenticate before decompression: the pinned input is not a zip bomb.
        payload = gzip.decompress(packed)
        _require(
            hashlib.sha256(payload).hexdigest() == _PAYLOAD_SHA256,
            "catalog_payload_digest",
        )
        catalog = json.loads(payload, object_pairs_hook=_unique_object)
        _validate_catalog(catalog)
        return catalog
    except (OSError, EOFError, UnicodeError, RecursionError) as exc:
        raise ValueError("catalog_unavailable") from exc


class _Bits:
    def __init__(self, code):
        _require(isinstance(code, str) and 26 <= len(code) <= 4096, "length")
        safe = code.replace("-", "+").replace("_", "/")
        _require(all(c in _ALPHABET for c in safe), "alphabet")
        self.value = sum(_ALPHABET.index(c) << (6 * i) for i, c in enumerate(safe))
        self.pos = 0

    def read(self, width):
        value = (self.value >> self.pos) & ((1 << width) - 1)
        self.pos += width
        return value


def _numeric_items(obj):
    return sorted(obj.items(), key=lambda item: int(item[0]))


def convert_maxroll_code(code: str, expected_spec: int | None = None) -> str:
    """Convert only Maxroll version 130; return game v2 or raise ValueError.

    expected_spec must be an integer specialization ID, not a name or string.
    Native game codes belong to the caller's native-code handling path. Success
    proves source-format conversion, not current-patch or simulator eligibility.
    """
    if expected_spec is not None:
        _require(
            type(expected_spec) is int and 0 < expected_spec <= 65535, "expected_spec"
        )
    catalog = _load_catalog()
    r = _Bits(code)
    if r.read(8) != 130:
        raise ValueError("source_version")
    spec = r.read(16)
    if expected_spec is not None and spec != expected_spec:
        raise ValueError("spec_mismatch")
    if str(spec) not in catalog["specs"]:
        raise ValueError("unknown_spec")
    h = bytes(r.read(8) for _ in range(16)).hex()
    tree = catalog["trees"].get(h)
    if tree is None:
        raise ValueError("unknown_hash")
    if tree["class"] != catalog["specs"][str(spec)]:
        raise ValueError("hash_class_mismatch")
    # Preserve the complete sorted physical node universe, including invisible nodes.
    nodes = {int(k): dict(v) for k, v in _numeric_items(tree["nodes"])}
    groups = {int(k): v for k, v in _numeric_items(tree["groups"])}
    entries = Counter()
    parsed = []
    source_flags = {}
    for nid, node in nodes.items():
        selected = r.read(1)
        if not selected:
            continue
        purchased = r.read(1)
        source_flags[nid] = (selected, purchased)
        if not purchased:
            continue
        partial = r.read(1)
        points = r.read(6) if partial else -1
        if partial and points == 0:
            raise ValueError("zero_partial")
        es = node["Entries"]
        if node["Flags"] & 256:
            if points > sum(e["MaxRanks"] for e in es):
                raise ValueError("rank_overflow")
            for e in es:
                if points == 0:
                    break
                pts = e["MaxRanks"] if points < 0 else min(points, e["MaxRanks"])
                entries[e["ID"]] += pts
                parsed.append((nid, e["ID"], pts))
                if points >= 0:
                    points -= pts
            if r.read(1):
                raise ValueError("apex_choice_flag")
        else:
            ischoice = r.read(1)
            index = r.read(2) if ischoice else 0
            if index >= len(es):
                raise ValueError("choice_out_of_range")
            e = es[index]
            pts = e["MaxRanks"] if points < 0 else points
            if pts < 1 or pts > e["MaxRanks"]:
                raise ValueError("rank_overflow")
            entries[e["ID"]] += pts
            parsed.append((nid, e["ID"], pts))
    # Maxroll URL-only PvP tail, deliberately absent from native exports.
    for _ in range(3):
        if r.read(1):
            r.read(16)
    if r.value >> r.pos:
        raise ValueError("nonzero_trailing_bits")
    # Official URL writer trims whole trailing zero bytes. Its reader deliberately
    # zero extends EOF; absence of late unselected nodes is NOT a truncation error.
    # Reject noncanonical redundant suffix length, allowing byte/Base64 padding.
    if len(code) * 6 > ((r.pos + 7) // 8) * 8 + 5:
        raise ValueError("excess_trailing_padding")
    points = {
        nid: [entries[e["ID"]] for e in n["Entries"]]
        for nid, n in nodes.items()
        if any(entries[e["ID"]] for e in n["Entries"])
    }
    for n in nodes.values():
        n["Conditions"] = list(n.get("Conditions", []))
        n["Cost"] = list(n.get("Cost", []))
        n["requires"] = []
    for group in groups.values():
        for nid in group["Nodes"]:
            nodes[nid]["Conditions"].extend(group.get("Conditions", []))
            nodes[nid]["Cost"].extend(group.get("Cost", []))
    for left, right in tree["edges"]:
        nodes[right]["requires"].append(left)
    cache = {}

    def missing(c, skip=None):
        key = id(c)
        if key in cache:
            return cache[key]
        result = 0
        if c["Flags"] & 2:
            result = 0
        elif c["SpecSetID"] and spec not in catalog["specSets"].get(
            str(c["SpecSetID"]), []
        ):
            result = 1
        elif c["SpentAmountRequired"]:
            amount = 0
            curr = c["TraitCurrencyID"]
            g = groups.get(c["TraitNodeGroupID"])
            if g and g.get("Cost"):
                cost = sum(
                    x["Amount"] for x in g["Cost"] if x["TraitCurrencyID"] == curr
                )
                for nid in g["Nodes"]:
                    if skip and nid in skip["Nodes"]:
                        continue
                    amount += sum(points.get(nid, [])) * cost
            nid = c["TraitNodeID"]
            n = nodes.get(nid)
            pts = points.get(nid)
            if n and pts:
                cost = sum(
                    x["Amount"] for x in n["Cost"] if x["TraitCurrencyID"] == curr
                )
                for e, val in zip(n["Entries"], pts):
                    if not val:
                        continue
                    amount += val * cost
                    # Preserve official evalCondition's cost mutation order.
                    for costrow in e.get("Cost", []):
                        if costrow["TraitCurrencyID"] == curr:
                            cost += val * costrow["Amount"]
            result = max(0, c["SpentAmountRequired"] - amount)
        cache[key] = result
        return result

    for g in groups.values():
        for c in g.get("Conditions", []):
            missing(c, g)
    subtree = 0
    for nid, n in nodes.items():
        pts = points.get(nid)
        if pts:
            for e, v in zip(n["Entries"], pts):
                if v and e.get("TraitSubTreeID"):
                    subtree = e["TraitSubTreeID"]
        for c in n["Conditions"]:
            if c["CondType"] == 2 and not missing(c):
                # The original JS does not update its local 'pts' when absent.
                if pts is None:
                    points[nid] = [c["GrantedRanks"] for _ in n["Entries"]]
                else:
                    for i in range(len(pts)):
                        pts[i] += c["GrantedRanks"]
    result = {}
    handled = set()

    def handle(nid):
        if nid in handled:
            return result.get(nid)
        handled.add(nid)
        n = nodes[nid]
        pts = points.get(nid)
        status = 2
        if n["Type"] == 3 and not subtree:
            return None
        # forExport=True intentionally does NOT hide other subtree nodes here.
        hidden = 0
        for c in n["Conditions"]:
            if c["CondType"] != 1:
                continue
            m = missing(c)
            if not m and c["Flags"] & 4:
                hidden = 0
                break
            hidden = max(hidden, m)
        if hidden:
            return None
        for c in n["Conditions"]:
            m = missing(c)
            if c["CondType"] == 0 and m:
                status = 0
            if c["CondType"] == 2 and not m:
                status = 4
        connected = not n["requires"] or any(
            (p := handle(parent)) is not None and p[0] in (3, 4)
            for parent in n["requires"]
        )
        if not connected and status == 2:
            status = 1
        if status != 4 and pts and any(pts):
            if status != 2:
                status = 5
            else:
                test = [v >= e["MaxRanks"] for v, e in zip(pts, n["Entries"])]
                if all(test) if n["Flags"] & 256 else any(test):
                    status = 3
        if status == 2 and not (pts and any(pts)):
            status = 1
        result[nid] = (status, pts)
        return result[nid]

    for nid in nodes:
        handle(nid)
    invalid = [nid for nid, (status, _) in result.items() if status == 5]
    if invalid:
        raise ValueError("invalid_nodes:" + str(invalid))
    if any(nid not in result for nid, entry, rank in parsed if rank):
        raise ValueError("hidden_purchased_node")
    if any(
        purchased == 0 and result.get(nid, (None,))[0] != 4
        for nid, (selected, purchased) in source_flags.items()
    ):
        raise ValueError("unproven_granted_node")
    full_nodes = []
    selected_nodes = []
    for nid, node in nodes.items():
        status, ranks = result.get(nid, (0, None))
        index = next((i for i, rank in enumerate(ranks or []) if rank), None)
        apex = bool(node["Flags"] & 256)
        maximum = (
            sum(e["MaxRanks"] for e in node["Entries"])
            if apex
            else node["Entries"][index or 0]["MaxRanks"]
        )
        full_nodes.append(
            {"talent_id": nid, "max_points": maximum, "is_apex_talent": apex}
        )
        if index is not None:
            selected_nodes.append(
                {
                    "talent_id": nid,
                    "points": 1
                    if status == 4
                    else sum(ranks)
                    if apex
                    else ranks[index],
                    "purchased": status != 4,
                    "is_choice_node": not apex and len(node["Entries"]) > 1,
                    "choice_selection": index,
                }
            )
    # A 152-bit v2/spec/zero-hash header ONLY. Never copy the private v130 header.
    header_value = 2 | (spec << 8)
    header = "".join(_ALPHABET[(header_value >> bit) & 63] for bit in range(0, 152, 6))
    output = TalentBuildCodeEncoder.encode_node_states(
        header, full_nodes, selected_nodes
    )
    _require(bool(output), "native_encoding_failed")
    return output
