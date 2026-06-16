from __future__ import annotations

from dataclasses import asdict, dataclass, field

from botend.wow.talents.models import (
    TalentBuildStateModel,
    TalentNodeModel,
    TalentTreeModel,
    TalentTreeSetModel,
)


LAYOUT_PANEL_COLUMNS = {
    'three-column': 3,
}


@dataclass
class TalentTreeLayoutConfigModel:
    header_height: int = 56
    panel_padding_x: int = 24
    panel_padding_y: int = 24
    panel_gap_x: int = 32
    panel_gap_y: int = 24
    cell_width: int = 96
    cell_height: int = 96
    node_width: int = 72
    node_height: int = 72
    panel_columns: int | None = None

    def to_dict(self):
        return asdict(self)


@dataclass
class TalentNodeLayoutModel:
    node_key: str = ''
    tree_type: str = 'spec'
    node_id: int | None = None
    talent_id: int | None = None
    spell_id: int | None = None
    name: str = ''
    layout_row: int = 1
    layout_column: int = 1
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    center_x: int = 0
    center_y: int = 0
    anchor_top_x: int = 0
    anchor_top_y: int = 0
    anchor_bottom_x: int = 0
    anchor_bottom_y: int = 0
    selected: bool = False
    parents: list[int] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


@dataclass
class TalentPathLayoutModel:
    tree_type: str = 'spec'
    parent_key: str = ''
    child_key: str = ''
    svg_path: str = ''

    def to_dict(self):
        return asdict(self)


@dataclass
class TalentTreePanelModel:
    tree_type: str = 'spec'
    title: str = ''
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    grid_columns: int = 1
    grid_rows: int = 1
    nodes: list[TalentNodeLayoutModel] = field(default_factory=list)
    paths: list[TalentPathLayoutModel] = field(default_factory=list)

    def to_dict(self):
        return {
            'tree_type': self.tree_type,
            'title': self.title,
            'x': self.x,
            'y': self.y,
            'width': self.width,
            'height': self.height,
            'grid_columns': self.grid_columns,
            'grid_rows': self.grid_rows,
            'nodes': [node.to_dict() for node in self.nodes],
            'paths': [path.to_dict() for path in self.paths],
        }


@dataclass
class TalentTreeLayoutModel:
    set_key: str = ''
    class_name: str = ''
    spec_name: str = ''
    layout_mode: str = 'three-column'
    width: int = 0
    height: int = 0
    panels: list[TalentTreePanelModel] = field(default_factory=list)

    def to_dict(self):
        return {
            'set_key': self.set_key,
            'class_name': self.class_name,
            'spec_name': self.spec_name,
            'layout_mode': self.layout_mode,
            'width': self.width,
            'height': self.height,
            'panels': [panel.to_dict() for panel in self.panels],
        }


def build_talent_tree_layout(
    tree_set: TalentTreeSetModel | dict,
    build_state: TalentBuildStateModel | dict | None = None,
    config: TalentTreeLayoutConfigModel | dict | None = None,
) -> TalentTreeLayoutModel:
    tree_set_model = tree_set if isinstance(tree_set, TalentTreeSetModel) else TalentTreeSetModel(**tree_set)
    if build_state is None:
        build_state_model = TalentBuildStateModel()
    else:
        build_state_model = (
            build_state if isinstance(build_state, TalentBuildStateModel) else TalentBuildStateModel(**build_state)
        )
    config_model = config if isinstance(config, TalentTreeLayoutConfigModel) else TalentTreeLayoutConfigModel(**(config or {}))

    trees = [tree if isinstance(tree, TalentTreeModel) else TalentTreeModel(**tree) for tree in tree_set_model.trees]
    panel_columns = max(1, config_model.panel_columns or LAYOUT_PANEL_COLUMNS.get(tree_set_model.layout_mode, 1))

    # 当缺少 class 面板时，插入一个占位空白面板，模拟左中右三栏布局
    tree_types = {t.tree_type for t in trees}
    has_class = 'class' in tree_types
    has_hero = 'hero' in tree_types
    if has_hero and not has_class:
        from botend.wow.talents.models import TREE_COLUMNS
        class_cols = TREE_COLUMNS.get('class', 8)
        class_rows = max(1, max((t.grid_rows for t in trees), default=1))
        # 插入一个空的 class 占位面板
        placeholder = TalentTreeModel(
            tree_type='class', title='', grid_columns=class_cols, grid_rows=class_rows, nodes=[],
        )
        trees.insert(0, placeholder)

    panel_blueprints = []
    for tree in trees:
        grid_columns = max(1, tree.grid_columns)
        grid_rows = max(1, tree.grid_rows)
        panel_blueprints.append({
            'tree': tree,
            'width': (config_model.panel_padding_x * 2) + (grid_columns * config_model.cell_width),
            'height': (
                config_model.header_height
                + (config_model.panel_padding_y * 2)
                + (grid_rows * config_model.cell_height)
            ),
            'grid_columns': grid_columns,
            'grid_rows': grid_rows,
        })

    row_heights = []
    for offset in range(0, len(panel_blueprints), panel_columns):
        row_blueprints = panel_blueprints[offset:offset + panel_columns]
        row_heights.append(max((item['height'] for item in row_blueprints), default=0))

    panels = []
    max_right = 0
    max_bottom = 0
    row_y_positions = _build_row_y_positions(row_heights, config_model.panel_gap_y)
    selected_nodes = set(build_state_model.selected_nodes or set())

    for index, blueprint in enumerate(panel_blueprints):
        row_index = index // panel_columns
        column_index = index % panel_columns
        row_offset = row_index * panel_columns
        row_items = panel_blueprints[row_offset:row_offset + panel_columns]
        x = sum(item['width'] for item in row_items[:column_index]) + (column_index * config_model.panel_gap_x)
        y = row_y_positions[row_index]

        panel = _build_tree_panel_layout(
            tree=blueprint['tree'],
            panel_x=x,
            panel_y=y,
            width=blueprint['width'],
            height=blueprint['height'],
            grid_columns=blueprint['grid_columns'],
            grid_rows=blueprint['grid_rows'],
            selected_nodes=selected_nodes,
            config=config_model,
        )
        panels.append(panel)
        max_right = max(max_right, panel.x + panel.width)
        max_bottom = max(max_bottom, panel.y + panel.height)

    return TalentTreeLayoutModel(
        set_key=tree_set_model.set_key,
        class_name=tree_set_model.class_name,
        spec_name=tree_set_model.spec_name,
        layout_mode=tree_set_model.layout_mode,
        width=max_right,
        height=max_bottom,
        panels=panels,
    )


def _build_row_y_positions(row_heights, panel_gap_y):
    positions = []
    current_y = 0
    for index, height in enumerate(row_heights):
        positions.append(current_y)
        current_y += height
        if index < len(row_heights) - 1:
            current_y += panel_gap_y
    return positions


def _build_tree_panel_layout(tree, panel_x, panel_y, width, height, grid_columns, grid_rows, selected_nodes, config):
    node_layouts = []
    identity_lookup = {}

    for index, raw_node in enumerate(tree.nodes):
        node = raw_node if isinstance(raw_node, TalentNodeModel) else TalentNodeModel.from_raw(raw_node)
        layout_row = _coerce_positive_int(node.layout_row) or _coerce_positive_int(node.row)
        layout_column = _coerce_positive_int(node.layout_column) or _coerce_positive_int(node.column)
        if layout_row is None:
            layout_row = (index // grid_columns) + 1
        if layout_column is None:
            layout_column = (index % grid_columns) + 1

        node_x = (
            panel_x
            + config.panel_padding_x
            + ((layout_column - 1) * config.cell_width)
            + max(0, (config.cell_width - config.node_width) // 2)
        )
        node_y = (
            panel_y
            + config.header_height
            + config.panel_padding_y
            + ((layout_row - 1) * config.cell_height)
            + max(0, (config.cell_height - config.node_height) // 2)
        )
        center_x = node_x + (config.node_width // 2)
        center_y = node_y + (config.node_height // 2)
        node_key = _build_node_key(node)
        node_layout = TalentNodeLayoutModel(
            node_key=node_key,
            tree_type=node.tree_type or tree.tree_type,
            node_id=node.node_id,
            talent_id=node.talent_id,
            spell_id=node.spell_id,
            name=node.name,
            layout_row=layout_row,
            layout_column=layout_column,
            x=node_x,
            y=node_y,
            width=config.node_width,
            height=config.node_height,
            center_x=center_x,
            center_y=center_y,
            anchor_top_x=center_x,
            anchor_top_y=node_y,
            anchor_bottom_x=center_x,
            anchor_bottom_y=node_y + config.node_height,
            selected=bool(node_key and node_key in selected_nodes) or node.selected or node.points > 0,
            parents=list(node.parents or []),
        )
        node_layouts.append(node_layout)
        _register_node_identity(identity_lookup, node, node_layout)

    paths = []
    emitted_edges = set()
    for node_layout in node_layouts:
        for raw_parent in node_layout.parents:
            parent_id = _coerce_positive_int(raw_parent)
            parent_layout = identity_lookup.get(parent_id) if parent_id is not None else None
            if parent_layout is None:
                continue

            edge_key = (parent_layout.node_key, node_layout.node_key)
            if edge_key in emitted_edges:
                continue

            paths.append(
                TalentPathLayoutModel(
                    tree_type=tree.tree_type,
                    parent_key=parent_layout.node_key,
                    child_key=node_layout.node_key,
                    svg_path=_build_minimal_svg_path(
                        parent_layout.anchor_bottom_x,
                        parent_layout.anchor_bottom_y,
                        node_layout.anchor_top_x,
                        node_layout.anchor_top_y,
                    ),
                )
            )
            emitted_edges.add(edge_key)

    return TalentTreePanelModel(
        tree_type=tree.tree_type,
        title=tree.title,
        x=panel_x,
        y=panel_y,
        width=width,
        height=height,
        grid_columns=grid_columns,
        grid_rows=grid_rows,
        nodes=node_layouts,
        paths=paths,
    )


def _build_node_key(node: TalentNodeModel):
    node_identity = node.key
    if node_identity is None:
        return ''
    return f'{node.tree_type or "spec"}:{node_identity}'


def _register_node_identity(identity_lookup, node, node_layout):
    for value in {node.node_id, node.talent_id, node.spell_id, node.key}:
        parsed = _coerce_positive_int(value)
        if parsed is not None:
            identity_lookup[parsed] = node_layout


def _build_minimal_svg_path(start_x, start_y, end_x, end_y):
    if start_x == end_x or start_y == end_y:
        return f'M {_fmt(start_x)} {_fmt(start_y)} L {_fmt(end_x)} {_fmt(end_y)}'

    mid_y = (start_y + end_y) / 2
    return (
        f'M {_fmt(start_x)} {_fmt(start_y)} '
        f'L {_fmt(start_x)} {_fmt(mid_y)} '
        f'L {_fmt(end_x)} {_fmt(mid_y)} '
        f'L {_fmt(end_x)} {_fmt(end_y)}'
    )


def _shift_panel(panel, offset_x=0, offset_y=0):
    """平移面板及其所有节点/连线的坐标"""
    panel.x += offset_x
    panel.y += offset_y
    for node in panel.nodes:
        node.x += offset_x
        node.y += offset_y
        node.center_x += offset_x
        node.center_y += offset_y
        node.anchor_top_x += offset_x
        node.anchor_top_y += offset_y
        node.anchor_bottom_x += offset_x
        node.anchor_bottom_y += offset_y
    for path in panel.paths:
        # SVG path 坐标也需要平移
        path.svg_path = _translate_svg_path(path.svg_path, offset_x, offset_y)


def _translate_svg_path(svg_path, dx, dy):
    """给 SVG path 的所有坐标加偏移"""
    import re
    def _shift_coord(match):
        cmd = match.group(1)
        coords = match.group(2)
        parts = coords.strip().split()
        shifted = []
        for i, p in enumerate(parts):
            val = float(p) + (dx if i % 2 == 0 else dy)
            shifted.append(f'{val:g}')
        return f'{cmd} {" ".join(shifted)}'
    return re.sub(r'([ML])\s+([\d.\s-]+)', _shift_coord, svg_path)


def _coerce_positive_int(value):
    try:
        parsed = int(str(value).strip())
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _fmt(value):
    return f'{value:g}'
