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
    panel_padding_x: int = 16
    panel_padding_y: int = 16
    panel_gap_x: int = 16
    panel_gap_y: int = 16
    cell_width: int = 96
    cell_height: int = 96
    node_width: int = 72
    node_height: int = 72
    panel_columns: int | None = None
    coord_scale: float = 0.006  # DB2 原始坐标 → 像素的缩放因子

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

    panel_blueprints = []
    for tree in trees:
        # 计算坐标范围
        cols = [n.column for n in tree.nodes if n.column is not None]
        rows = [n.row for n in tree.nodes if n.row is not None]
        min_col = min(cols) if cols else 0
        max_col = max(cols) if cols else 0
        min_row = min(rows) if rows else 0
        max_row = max(rows) if rows else 0
        if cols and rows:
            coord_w = max_col - min_col
            coord_h = max_row - min_row
            # 动态计算 scale：目标面板宽度 280px，高度 550px
            target_w = 280
            target_h = 550
            scale_x = target_w / max(coord_w, 1)
            scale_y = target_h / max(coord_h, 1)
            scale = min(scale_x, scale_y)  # 取较小值保持比例
            node_size = max(32, min(56, int(scale * 600)))  # 节点尺寸随 scale 调整
            panel_w = int(coord_w * scale) + node_size + config_model.panel_padding_x * 2
            panel_h = int(coord_h * scale) + node_size + config_model.panel_padding_y * 2 + config_model.header_height
            grid_columns = max_col
            grid_rows = max_row
        else:
            grid_columns = max(1, tree.grid_columns)
            grid_rows = max(1, tree.grid_rows)
            scale = config_model.coord_scale
            node_size = config_model.node_width
            panel_w = (config_model.panel_padding_x * 2) + (grid_columns * config_model.cell_width)
            panel_h = config_model.header_height + (config_model.panel_padding_y * 2) + (grid_rows * config_model.cell_height)

        panel_blueprints.append({
            'tree': tree,
            'width': panel_w,
            'height': panel_h,
            'grid_columns': grid_columns,
            'grid_rows': grid_rows,
            'min_col': min_col,
            'min_row': min_row,
            'scale': scale,
            'node_size': node_size,
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
            min_col=blueprint['min_col'],
            min_row=blueprint['min_row'],
            scale=blueprint['scale'],
            node_size=blueprint['node_size'],
        )
        panels.append(panel)
        max_right = max(max_right, panel.x + panel.width)
        max_bottom = max(max_bottom, panel.y + panel.height)

    # 无 class 面板时，将所有面板整体右移，空出左侧 class 位置（左中右三栏）
    hero_panel = next((p for p in panels if p.tree_type == 'hero'), None)
    class_panel = next((p for p in panels if p.tree_type == 'class'), None)
    if hero_panel and not class_panel:
        from botend.wow.talents.models import TREE_COLUMNS
        class_cols = TREE_COLUMNS.get('class', 8)
        class_width = (config_model.panel_padding_x * 2) + (class_cols * config_model.cell_width)
        offset_x = class_width + config_model.panel_gap_x
        for panel in panels:
            if panel.tree_type != 'class':
                _shift_panel(panel, offset_x=offset_x)
        max_right = max(p.x + p.width for p in panels)

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


def _build_tree_panel_layout(tree, panel_x, panel_y, width, height, grid_columns, grid_rows, selected_nodes, config, min_col=0, min_row=0, scale=None, node_size=None):
    node_layouts = []
    identity_lookup = {}
    if scale is None:
        scale = config.coord_scale
    if node_size is None:
        node_size = config.node_width
    use_scale = min_col > 0 or min_row > 0

    for index, raw_node in enumerate(tree.nodes):
        node = raw_node if isinstance(raw_node, TalentNodeModel) else TalentNodeModel.from_raw(raw_node)
        layout_row = _coerce_positive_int(node.layout_row) or _coerce_positive_int(node.row)
        layout_column = _coerce_positive_int(node.layout_column) or _coerce_positive_int(node.column)
        if layout_row is None:
            layout_row = (index // grid_columns) + 1
        if layout_column is None:
            layout_column = (index % grid_columns) + 1

        if use_scale:
            node_x = panel_x + config.panel_padding_x + int((layout_column - min_col) * scale)
            node_y = panel_y + config.header_height + config.panel_padding_y + int((layout_row - min_row) * scale)
        else:
            node_x = panel_x + config.panel_padding_x + ((layout_column - 1) * config.cell_width) + max(0, (config.cell_width - node_size) // 2)
            node_y = panel_y + config.header_height + config.panel_padding_y + ((layout_row - 1) * config.cell_height) + max(0, (config.cell_height - node_size) // 2)
        center_x = node_x + (node_size // 2)
        center_y = node_y + (node_size // 2)
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
            width=node_size,
            height=node_size,
            center_x=center_x,
            center_y=center_y,
            anchor_top_x=center_x,
            anchor_top_y=node_y,
            anchor_bottom_x=center_x,
            anchor_bottom_y=node_y + node_size,
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
