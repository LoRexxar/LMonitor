# -*- coding: utf-8 -*-
"""WoW 天赋视图模型入口。"""

from __future__ import annotations

from botend.wow.talents.render import build_talent_render_model


def build_talent_view_model(talents, class_name='', spec_name=''):
    render_model_dict = build_talent_render_model(
        talents,
        class_name=class_name,
        spec_name=spec_name,
    ).to_dict()
    return {
        'build_code': render_model_dict['build_code'],
        'nodes': render_model_dict['nodes'],
        'trees': render_model_dict['trees'],
        'render_model': render_model_dict,
    }
