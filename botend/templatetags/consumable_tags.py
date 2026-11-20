"""为聚合快照中的消耗品名称匹配展示图标。"""
from django import template
from django.templatetags.static import static

register = template.Library()

# 快照按展示名称归并物品品质和同效果法术。共享饱餐效果使用日志中的增益图标。
CATEGORY_ICONS = {
    'food': 'spell_misc_food',
    'flask': 'inv_12_profession_alchemy_flask_sindoreipotion_yellow',
    'potion': 'inv_12_profession_alchemy_voidpotion_red',
    'healing': 'inv_potion_49',
    'weapon': 'inv_stone_sharpeningstone_03',
}

ICONS_BY_NAME = {
    '远行者口粮': 'inv_cooking_81_wildberrybread',
    '采集者什锦餐': 'inv_cooking_90_smuggledproduce',
    '便携小食': 'inv_cooking_80_tweetsflavorfulflan',
    '速食三明治': 'inv_misc_starflowersandwich',
    '银月城标准餐': 'inv_cooking_90_phantasmalsoufflefries',
    '香料饼干': 'inv_cooking_90_biscuitscaviar',
    '血蓟肉排': 'inv_misc_food_168_ricecake01',
    '永歌布丁': 'inv_cooking_90_bananabeefpudding',
    '油炸花尾鱼': 'inv_misc_food_legion_driedmackerelstrips',
    '炉火晚餐': 'inv_misc_food_dimsum',
    '太阳之井佳肴': 'inv_misc_food_cooked_sauteedcarrots',
    '智者之尾': 'inv_misc_food_158_fish_redish',
    '萨拉斯抗性合剂': 'inv_12_profession_alchemy_flask_sindoreipotion_yellow',
    '魔导师合剂': 'inv_12_profession_alchemy_flask_sindoreipotion_black',
    '血骑士合剂': 'inv_12_profession_alchemy_flask_sindoreipotion_white-',
    '破碎残阳合剂': 'inv_12_profession_alchemy_flask_sindoreipotion_red--',
    '银月城治疗药水': 'inv_potion_49',
    '浓缩银月城治疗药水': 'inv_potion_49',
    '强效治疗药水': 'inv_alchemy_80_potion01red',
    '鲁莽药水': 'inv_12_profession_alchemy_voidpotion_red',
    '狂放恣意饮剂': 'inv_12_profession_alchemy_voidpotion_purple',
    '狂热药水': 'inv_12_profession_alchemy_lightpotion_green',
    '噬梦药水': 'inv_12_profession_alchemy_voidpotion_blue',
    '诱惑秘药': 'inv_121_professions_alchemy_potion_amani_purple',
    '光融法力药水': 'inv_12_profession_alchemy_lightpotion_blue',
    '圣光护持': 'inv_alchemy_80_potion02yellow',
    '焕新血清': 'inv_potion_49',
    '圣光潜力': 'inv_12_profession_alchemy_lightpotion_yellow',
    '阿曼尼提取物': 'inv_alchemy_elixir_04',
    '液态光泽': 'inv_121_professions_alchemy_potion_amani_blue',
    '耀辉磨刀石': 'inv_stone_sharpeningstone_03',
    '耀辉平衡石': 'inv_stone_sharpeningstone_03',
    '萨拉斯凤凰之油': 'inv_12_profession_enchanting_manaoil_red',
    '黎明之油': 'inv_12_profession_enchanting_manaoil_orange',
    '走私者的附魔锋刃': 'inv_12_profession_enchanting_manaoil_purple',
}


@register.simple_tag
def consumable_icon(name, category):
    icon = ICONS_BY_NAME.get(name) or CATEGORY_ICONS.get(category, 'inv_potion_49')
    return static(f'portal/img/consumables/{icon}.jpg')
