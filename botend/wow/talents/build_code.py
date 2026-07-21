# -*- coding: utf-8 -*-

BASE64_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
BASE64_LOOKUP = {char: index for index, char in enumerate(BASE64_ALPHABET)}


class TalentBuildCodeDecoder:
    HEADER_VERSION_BITS = 8
    SPEC_ID_BITS = 16
    TREE_HASH_BITS = 128
    RANKS_PURCHASED_BITS = 6

    @classmethod
    def extract_spec_id(cls, build_code):
        build_code = str(build_code or '').strip()
        stream = _ImportBitStream(build_code)
        if stream.total_bits < cls.HEADER_VERSION_BITS + cls.SPEC_ID_BITS:
            return 0
        stream.extract(cls.HEADER_VERSION_BITS)
        return stream.extract(cls.SPEC_ID_BITS)

    @classmethod
    def decode_node_states(cls, build_code, full_nodes):
        build_code = str(build_code or '').strip()
        if not build_code or not full_nodes:
            return {}

        stream = _ImportBitStream(build_code)
        if stream.total_bits < (cls.HEADER_VERSION_BITS + cls.SPEC_ID_BITS + cls.TREE_HASH_BITS):
            return {}

        stream.extract(cls.HEADER_VERSION_BITS)
        stream.extract(cls.SPEC_ID_BITS)
        for _ in range(16):
            stream.extract(8)

        states = {}
        ordered_nodes = cls._ordered_nodes(full_nodes)
        for node in ordered_nodes:
            node_key = _build_node_key(node)
            if not node_key:
                continue

            is_selected = stream.extract(1) == 1
            is_purchased = False
            is_partially_ranked = False
            partial_ranks = 0
            is_choice_node = False
            choice_selection = 0

            if is_selected:
                is_purchased = stream.extract(1) == 1
                if is_purchased:
                    is_partially_ranked = stream.extract(1) == 1
                    if is_partially_ranked:
                        partial_ranks = stream.extract(cls.RANKS_PURCHASED_BITS)
                    is_choice_node = stream.extract(1) == 1
                    if is_choice_node:
                        choice_selection = stream.extract(2)

            if not is_selected:
                continue

            max_points = cls._effective_max_points(node)
            if not is_purchased:
                # Blizzard import strings mark granted/default nodes as selected
                # but not purchased. Render the granted rank as active while
                # preserving purchased=False so counters can exclude its cost.
                points = 1
            elif is_partially_ranked:
                points = partial_ranks
            else:
                points = max_points

            states[node_key] = {
                'selected': True,
                'purchased': is_purchased,
                'points': max(0, points),
                'is_choice_node': is_choice_node,
                'choice_selection': choice_selection,
            }
        return states

    @staticmethod
    def _effective_max_points(node):
        try:
            max_points = int(node.get('max_points') or 1)
        except (TypeError, ValueError):
            max_points = 1
        # Multi-entry rank pools must be identified explicitly. A hero subtree
        # selector can also be represented as a hero_anchor with choice_options,
        # but its entries are alternatives and must not be summed as ranks.
        if not (node.get('is_apex_talent') or node.get('apex_entries')):
            return max_points
        option_points = 0
        for option in node.get('apex_entries') or node.get('choice_options') or []:
            try:
                option_points += int(option.get('max_points') or 0)
            except (TypeError, ValueError):
                continue
        if option_points > max_points:
            return option_points
        return max_points

    @staticmethod
    def _ordered_nodes(full_nodes):
        """对节点列表去重并按 Blizzard canonical ordering 排序。

        Build code 按 talent_id（DB2 TraitNode ID）顺序编码每个节点。
        对于 choice 节点，整个 TraitNode 只占一个 decode 位（不是每个 entry 一个位）。
        """

        def _sort_key(node):
            return (
                int(node.get('talent_id') or 0),
                int(node.get('node_id') or 0),
            )

        # 按 talent_id 去重 — choice 节点的多个 entry 应合并为一个
        dedup = {}
        for node in full_nodes:
            tid = node.get('talent_id')
            if not tid:
                continue
            key = str(tid)
            if key in dedup:
                continue
            dedup[key] = dict(node)
        return sorted(dedup.values(), key=_sort_key)


class TalentBuildCodeEncoder:
    """Encode selected talent nodes into a Blizzard import string.

    The import header contains Blizzard version/spec/hash data. LMonitor does not
    currently persist the tree hash, so encoding reuses the header from a known
    valid build code for the same class/spec and rewrites only the per-node state
    payload. The result is validated by decoding it back before it is used.
    """

    @classmethod
    def encode_node_states(cls, reference_build_code, full_nodes, selected_nodes):
        reference_build_code = str(reference_build_code or '').strip()
        if not reference_build_code or not full_nodes:
            return ''

        ordered_nodes = TalentBuildCodeDecoder._ordered_nodes(full_nodes)
        if not ordered_nodes:
            return ''

        header_bits = (
            TalentBuildCodeDecoder.HEADER_VERSION_BITS
            + TalentBuildCodeDecoder.SPEC_ID_BITS
            + TalentBuildCodeDecoder.TREE_HASH_BITS
        )
        reference_stream = _ImportBitStream(reference_build_code)
        if reference_stream.total_bits < header_bits:
            return ''

        writer = _ImportBitWriter()
        writer.write(reference_stream.extract(TalentBuildCodeDecoder.HEADER_VERSION_BITS), TalentBuildCodeDecoder.HEADER_VERSION_BITS)
        writer.write(reference_stream.extract(TalentBuildCodeDecoder.SPEC_ID_BITS), TalentBuildCodeDecoder.SPEC_ID_BITS)
        for _ in range(16):
            writer.write(reference_stream.extract(8), 8)

        selected_nodes = list(selected_nodes or [])
        selected_lookup = cls._build_selected_lookup(selected_nodes, ordered_nodes)
        hidden_selector_keys = {
            _build_node_key(node)
            for node in ordered_nodes
            if cls._is_hidden_hero_selector(node)
        }
        if hidden_selector_keys and not (hidden_selector_keys & selected_lookup.keys()):
            reference_states = TalentBuildCodeDecoder.decode_node_states(reference_build_code, ordered_nodes)
            for key in hidden_selector_keys:
                state = reference_states.get(key)
                if state:
                    selected_lookup[key] = dict(state)
        for node in ordered_nodes:
            key = _build_node_key(node)
            state = selected_lookup.get(key)
            if not state:
                writer.write(0, 1)
                continue

            points = max(1, int(state.get('points') or 1))
            max_points = max(1, TalentBuildCodeDecoder._effective_max_points(node))
            points = min(points, max_points)
            is_purchased = state.get('purchased') is not False
            is_partially_ranked = points < max_points
            choice_selection = state.get('choice_selection')
            is_choice_node = choice_selection is not None

            writer.write(1, 1)  # selected
            writer.write(1 if is_purchased else 0, 1)
            if not is_purchased:
                continue
            writer.write(1 if is_partially_ranked else 0, 1)
            if is_partially_ranked:
                writer.write(points, TalentBuildCodeDecoder.RANKS_PURCHASED_BITS)
            writer.write(1 if is_choice_node else 0, 1)
            if is_choice_node:
                writer.write(max(0, min(3, int(choice_selection or 0))), 2)

        build_code = writer.to_string()
        decoded = TalentBuildCodeDecoder.decode_node_states(build_code, full_nodes)
        if not cls._states_match(selected_lookup, decoded):
            return ''
        return build_code

    @staticmethod
    def _is_hidden_hero_selector(node):
        return (
            (node.get('tree_type') or 'spec') == 'hero_anchor'
            and node.get('is_choice_node') is True
            and not node.get('is_apex_talent')
            and not node.get('apex_entries')
        )

    @staticmethod
    def _build_selected_lookup(selected_nodes, ordered_nodes):
        ordered_lookup = {_build_node_key(node): node for node in ordered_nodes}
        alias_lookup = {}
        for key, node in ordered_lookup.items():
            for alias in _node_alias_keys(node):
                alias_lookup.setdefault(alias, key)
            for option in node.get('choice_options') or []:
                for alias in _node_alias_keys(dict(option, tree_type=node.get('tree_type') or 'spec')):
                    alias_lookup.setdefault(alias, key)
        lookup = {}
        for selected in selected_nodes or []:
            if not isinstance(selected, dict):
                continue
            points = int(selected.get('points') or selected.get('rank') or 0)
            if points <= 0:
                continue
            key = _build_node_key(selected)
            if not key or key not in ordered_lookup:
                for alias in _node_alias_keys(selected):
                    mapped_key = alias_lookup.get(alias)
                    if mapped_key:
                        key = mapped_key
                        break
            if not key or key not in ordered_lookup:
                continue
            state = {'points': points}
            if selected.get('purchased') is False:
                state['purchased'] = False
            choice_selection = _resolve_choice_selection(ordered_lookup[key], selected)
            if choice_selection is not None:
                state['choice_selection'] = choice_selection
            lookup[key] = state
        return lookup

    @staticmethod
    def _states_match(expected, decoded):
        expected_keys = {key for key, state in expected.items() if int(state.get('points') or 0) > 0}
        decoded_keys = set(decoded.keys())
        if expected_keys != decoded_keys:
            return False
        for key in expected_keys:
            expected_state = expected[key]
            decoded_state = decoded.get(key) or {}
            if int(expected_state.get('points') or 0) != int(decoded_state.get('points') or 0):
                return False
            if expected_state.get('purchased') is False and decoded_state.get('purchased') is not False:
                return False
            expected_choice = expected_state.get('choice_selection')
            if expected_choice is not None and int(expected_choice) != int(decoded_state.get('choice_selection') or 0):
                return False
        return True


class _ImportBitStream:
    def __init__(self, build_code):
        self.values = [BASE64_LOOKUP.get(char, 0) for char in build_code]
        self.index = 0
        self.extracted_bits = 0
        self.remaining_value = self.values[0] if self.values else 0
        self.total_bits = len(self.values) * 6

    def extract(self, bit_width):
        if self.index >= len(self.values):
            return 0
        value = 0
        bits_needed = bit_width
        extracted_bits = 0
        while bits_needed > 0 and self.index < len(self.values):
            remaining_bits = 6 - self.extracted_bits
            bits_to_extract = min(remaining_bits, bits_needed)
            self.extracted_bits += bits_to_extract
            max_value = 1 << bits_to_extract
            remainder = self.remaining_value % max_value
            self.remaining_value >>= bits_to_extract
            value += (remainder << extracted_bits)
            extracted_bits += bits_to_extract
            bits_needed -= bits_to_extract

            if bits_to_extract >= remaining_bits:
                self.index += 1
                self.extracted_bits = 0
                self.remaining_value = self.values[self.index] if self.index < len(self.values) else 0
        return value


class _ImportBitWriter:
    def __init__(self):
        self.values = []
        self.current_value = 0
        self.written_bits = 0

    def write(self, value, bit_width):
        value = int(value or 0)
        bits_remaining = bit_width
        while bits_remaining > 0:
            remaining_slot_bits = 6 - self.written_bits
            bits_to_write = min(remaining_slot_bits, bits_remaining)
            mask = (1 << bits_to_write) - 1
            chunk = value & mask
            self.current_value |= chunk << self.written_bits
            self.written_bits += bits_to_write
            value >>= bits_to_write
            bits_remaining -= bits_to_write
            if self.written_bits == 6:
                self.values.append(self.current_value)
                self.current_value = 0
                self.written_bits = 0

    def to_string(self):
        values = list(self.values)
        if self.written_bits:
            values.append(self.current_value)
        return ''.join(BASE64_ALPHABET[value] for value in values)


def _resolve_choice_selection(base_node, selected_node):
    # ``choice_options`` may also carry the DB2 entries of a non-choice apex
    # rank pool. Canonical node semantics, not the mere presence of options,
    # decide whether choice bits belong in the import string.
    if base_node.get('is_choice_node') is False or base_node.get('is_apex_talent') or base_node.get('apex_entries'):
        return None
    options = base_node.get('choice_options') or []
    if not options:
        return None
    explicit_selection = selected_node.get('choice_selection')
    if explicit_selection is not None:
        try:
            index = int(explicit_selection)
        except (TypeError, ValueError):
            index = 0
        return max(0, min(len(options) - 1, index))
    selected_ids = {
        _to_int(selected_node.get('display_spell_id')),
        _to_int(selected_node.get('spell_id')),
        _to_int(selected_node.get('talent_id')),
        _to_int(selected_node.get('talentID')),
        _to_int(selected_node.get('spellID')),
    }
    selected_ids.discard(None)
    for index, option in enumerate(options):
        option_ids = {
            _to_int(option.get('display_spell_id')),
            _to_int(option.get('spell_id')),
            _to_int(option.get('talent_id')),
            _to_int(option.get('talentID')),
            _to_int(option.get('spellID')),
        }
        option_ids.discard(None)
        if selected_ids & option_ids:
            return index
    return 0


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _node_alias_keys(node):
    if not isinstance(node, dict):
        return []
    tree_type = node.get('tree_type') or 'spec'
    keys = []
    for field in ('node_id', 'nodeID', 'talent_id', 'talentID', 'spell_id', 'spellID', 'display_spell_id', 'displaySpellID'):
        value = _to_int(node.get(field))
        if value is not None:
            keys.append(f'{tree_type}:{value}')
    return keys


def _build_node_key(node):
    tree_type = node.get('tree_type') or 'spec'
    node_identity = node.get('node_id') or node.get('talent_id') or node.get('spell_id') or node.get('display_spell_id')
    return f'{tree_type}:{node_identity}' if node_identity else ''
