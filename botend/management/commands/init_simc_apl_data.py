#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: init_simc_apl_data.py
@time: 2024/01/15
@desc: 初始化SimC APL关键字对照数据
'''

from django.core.management.base import BaseCommand
from botend.models import SimcAplKeywordPair


class Command(BaseCommand):
    help = '初始化SimC APL关键字对照数据'
    
    def handle(self, *args, **options):
        # 示例关键字对照数据
        keyword_pairs = [
            {
                'simc_keyword': 'actions',
                'apl_keyword': 'actions',
                'description': '动作列表'
            },
            {
                'simc_keyword': 'spell_targets',
                'apl_keyword': 'spell_targets',
                'description': '法术目标数量'
            },
            {
                'simc_keyword': 'target.health.pct',
                'apl_keyword': 'target.health.pct',
                'description': '目标生命值百分比'
            },
            {
                'simc_keyword': 'cooldown',
                'apl_keyword': 'cooldown',
                'description': '冷却时间'
            },
            {
                'simc_keyword': 'buff',
                'apl_keyword': 'buff',
                'description': '增益效果'
            },
            {
                'simc_keyword': 'debuff',
                'apl_keyword': 'debuff',
                'description': '减益效果'
            },
            {
                'simc_keyword': 'cast_time',
                'apl_keyword': 'cast_time',
                'description': '施法时间'
            },
            {
                'simc_keyword': 'gcd',
                'apl_keyword': 'gcd',
                'description': '公共冷却时间'
            },
            {
                'simc_keyword': 'energy',
                'apl_keyword': 'energy',
                'description': '能量'
            },
            {
                'simc_keyword': 'combo_points',
                'apl_keyword': 'combo_points',
                'description': '连击点数'
            },
            # 添加一些示例转换对
            {
                'simc_keyword': 'actions.precombat',
                'apl_keyword': 'actions.precombat',
                'description': '战斗前动作'
            },
            {
                'simc_keyword': 'actions.default',
                'apl_keyword': 'actions.default',
                'description': '默认动作'
            },
            {
                'simc_keyword': 'variable,name=',
                'apl_keyword': 'variable,name=',
                'description': '变量定义'
            },
            {
                'simc_keyword': 'call_action_list',
                'apl_keyword': 'call_action_list',
                'description': '调用动作列表'
            },
            {
                'simc_keyword': 'if=',
                'apl_keyword': 'if=',
                'description': '条件判断'
            }
        ]
        
        # 清空现有数据
        SimcAplKeywordPair.objects.all().delete()
        self.stdout.write(self.style.SUCCESS('已清空现有关键字对照数据'))
        
        # 批量创建数据
        created_count = 0
        for pair_data in keyword_pairs:
            pair, created = SimcAplKeywordPair.objects.get_or_create(
                simc_keyword=pair_data['simc_keyword'],
                apl_keyword=pair_data['apl_keyword'],
                defaults={
                    'description': pair_data['description'],
                    'is_active': True
                }
            )
            if created:
                created_count += 1
                self.stdout.write(f'创建关键字对: {pair.simc_keyword} <-> {pair.apl_keyword}')
        
        self.stdout.write(
            self.style.SUCCESS(f'成功初始化 {created_count} 个关键字对照数据')
        )