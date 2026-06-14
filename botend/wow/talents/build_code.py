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


def _build_node_key(node):
    tree_type = node.get('tree_type') or 'spec'
    node_identity = node.get('node_id') or node.get('talent_id') or node.get('spell_id') or node.get('display_spell_id')
    return f'{tree_type}:{node_identity}' if node_identity else ''
