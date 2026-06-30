from __future__ import annotations


class WagoDB2Schema:
    """Centralized labels and lightweight table classification for Wago DB2 rows."""

    SPELL_TABLES = {
        'spell', 'spellname', 'spelldescription', 'spelleffect', 'spellmisc', 'spellpower',
        'spellcooldowns', 'spellduration', 'spellrange', 'spellradius', 'spellauraoptions',
        'spellclassoptions', 'specializationspells', 'skilllineability', 'spellscripttext',
    }
    QUEST_TABLES = {'questv2', 'questv2clitask', 'questline', 'questinfo', 'questsort'}
    ITEM_TABLES = {'item', 'itemsparse', 'itemeffect', 'itemxitemeffect', 'itemcurrencycost'}
    TRAIT_TABLES = {'traitnode', 'traitnodeentry', 'traitnodextraitnodeentry', 'traitdefinition', 'traitsubtree', 'traitedge'}
    MOUNT_TABLES = {'mount', 'mountxdisplay', 'mountcapability'}
    BATTLE_PET_TABLES = {'battlepetspecies', 'battlepetability', 'battlepetabilitystate', 'battlepetspeciesstate'}
    VEHICLE_TABLES = {'vehicle', 'vehicleseat'}

    TABLE_LABELS = {
        'spellname': '技能名称',
        'spelldescription': '技能描述',
        'spelleffect': '技能效果',
        'spellmisc': '技能杂项',
        'spellpower': '资源消耗',
        'spellcooldowns': '冷却',
        'spellduration': '持续时间',
        'spellrange': '距离',
        'spellradius': '半径',
        'spellauraoptions': '光环选项',
        'traitnode': '天赋节点',
        'traitnodeentry': '天赋节点条目',
        'traitdefinition': '天赋定义',
        'questv2': '任务',
        'questv2clitask': '任务目标',
        'item': '物品',
        'itemsparse': '物品文本',
        'itemeffect': '物品效果',
        'itemcurrencycost': '物品货币成本',
        'map': '地图',
        'areatable': '区域',
        'creature': '生物/NPC',
        'creaturedifficulty': '生物难度',
        'playercondition': '玩家条件',
        'modifiertree': '条件树',
        'currencytypes': '货币',
        'achievement': '成就',
        'mount': '坐骑',
        'mountxdisplay': '坐骑外观关联',
        'mountcapability': '坐骑能力',
        'battlepetspecies': '战斗宠物品种',
        'battlepetability': '战斗宠物技能',
        'battlepetabilitystate': '战斗宠物技能属性',
        'battlepetspeciesstate': '战斗宠物品种属性',
        'vehicle': '载具',
        'vehicleseat': '载具座位',
    }

    FIELD_LABELS = {
        'ID': '记录 ID',
        'Name_lang': '名称',
        'Name': '名称',
        'Display_lang': '显示名',
        'DisplayName_lang': '显示名',
        'Title_lang': '标题',
        'Description_lang': '描述',
        'AuraDescription_lang': '光环描述',
        'Text_lang': '文本',
        'VerifiedBuild': '数据 build',
        'SpellID': '技能 ID',
        'EffectIndex': '效果序号',
        'Effect': '效果类型',
        'EffectAura': '光环类型',
        'EffectBasePointsF': '基础数值F',
        'EffectBasePoints': '基础数值',
        'EffectBonusCoefficient': '法强系数',
        'BonusCoefficientFromAP': '攻强系数',
        'Coefficient': '系数',
        'PvpMultiplier': 'PvP 系数',
        'QuestID': '任务 ID',
        'ObjectiveText_lang': '任务目标',
        'ParentItemID': '物品 ID',
        'ItemID': '物品 ID',
        'TriggerType': '触发类型',
        'TraitDefinitionID': '天赋定义',
        'SourceSpellID': '来源技能 ID',
        'CreatureDisplayInfoID': '生物外观 ID',
        'CreatureID': '生物 ID',
        'SpeciesID': '宠物品种 ID',
        'SourceTypeEnum': '来源类型',
        'IconFileDataID': '图标文件 ID',
        'VehicleID': '载具 ID',
        'VehicleSeatID': '载具座位 ID',
        'Flags': '标志位',
        'FlagsB': '标志位 B',
        'AttachmentID': '挂点 ID',
        'CameraEnteringDelay': '进入相机延迟',
        'CameraEnteringDuration': '进入相机时长',
    }

    def normalize_table(self, table: str) -> str:
        return str(table or '').strip()

    def table_key(self, table: str) -> str:
        return self.normalize_table(table).lower()

    def table_label(self, table: str) -> str:
        name = self.normalize_table(table)
        label = self.TABLE_LABELS.get(self.table_key(table))
        return f'{label} / {name}' if label else name

    def field_label(self, field: str) -> str:
        return self.FIELD_LABELS.get(str(field or ''), str(field or ''))

    def object_kind_for_table(self, table: str) -> str:
        key = self.table_key(table)
        if key in self.SPELL_TABLES:
            return 'spell'
        if key in self.TRAIT_TABLES:
            return 'trait'
        if key in self.QUEST_TABLES:
            return 'quest'
        if key in self.ITEM_TABLES:
            return 'item'
        if key in self.MOUNT_TABLES:
            return 'mount'
        if key in self.BATTLE_PET_TABLES:
            return 'battle_pet'
        if key in self.VEHICLE_TABLES:
            return 'vehicle'
        return ''

    def table_category(self, table: str) -> str:
        kind = self.object_kind_for_table(table)
        if kind == 'spell':
            return '技能/法术'
        if kind == 'trait':
            return '天赋'
        if kind == 'quest':
            return '任务'
        if kind == 'item':
            return '物品/装备'
        if kind == 'mount':
            return '坐骑'
        if kind == 'battle_pet':
            return '战斗宠物'
        if kind == 'vehicle':
            return '载具/交互'
        key = self.table_key(table)
        if 'creature' in key:
            return '生物/NPC'
        if 'map' in key or 'area' in key:
            return '地图/区域'
        if 'currency' in key:
            return '货币'
        if 'achievement' in key:
            return '成就'
        if 'condition' in key or 'modifier' in key:
            return '条件'
        return '其他 DB2'
