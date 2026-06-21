# -*- coding: utf-8 -*-

BASE64_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
BASE64_LOOKUP = {char: index for index, char in enumerate(BASE64_ALPHABET)}


class TalentBuildCodeDecoder:
    HEADER_VERSION_BITS = 8
    SPEC_ID_BITS = 16
    TREE_HASH_BITS = 128
    RANKS_PURCHASED_BITS = 6

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

            max_points = int(node.get('max_points') or 1)
            if not is_purchased:
                points = 1
            elif is_partially_ranked:
                points = partial_ranks
            else:
                points = max_points

            states[node_key] = {
                'selected': True,
                'points': max(0, points),
                'is_choice_node': is_choice_node,
                'choice_selection': choice_selection,
            }
        return states

    @staticmethod
    def _ordered_nodes(full_nodes):
        def _sort_key(node):
            return (
                int(node.get('node_id') or 0),
                int(node.get('talent_id') or 0),
                int(node.get('spell_id') or 0),
            )

        dedup = {}
        for node in full_nodes:
            key = _build_node_key(node)
            if not key or key in dedup:
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

        selected_lookup = cls._build_selected_lookup(selected_nodes, ordered_nodes)
        for node in ordered_nodes:
            key = _build_node_key(node)
            state = selected_lookup.get(key)
            if not state:
                writer.write(0, 1)
                continue

            points = max(1, int(state.get('points') or 1))
            max_points = max(1, int(node.get('max_points') or 1))
            points = min(points, max_points)
            is_partially_ranked = points < max_points
            choice_selection = state.get('choice_selection')
            is_choice_node = choice_selection is not None

            writer.write(1, 1)  # selected
            writer.write(1, 1)  # purchased
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
    def _build_selected_lookup(selected_nodes, ordered_nodes):
        ordered_lookup = {_build_node_key(node): node for node in ordered_nodes}
        lookup = {}
        for selected in selected_nodes or []:
            if not isinstance(selected, dict):
                continue
            points = int(selected.get('points') or selected.get('rank') or 0)
            if points <= 0:
                continue
            key = _build_node_key(selected)
            if not key or key not in ordered_lookup:
                continue
            state = {'points': points}
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
    options = base_node.get('choice_options') or []
    if not options:
        return None
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


def _build_node_key(node):
    tree_type = node.get('tree_type') or 'spec'
    node_identity = node.get('node_id') or node.get('talent_id') or node.get('spell_id') or node.get('display_spell_id')
    return f'{tree_type}:{node_identity}' if node_identity else ''
