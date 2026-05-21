import re


PEAK_CLASS_CN = {
    "death-knight": "死亡骑士",
    "demon-hunter": "恶魔猎手",
    "druid": "德鲁伊",
    "evoker": "唤魔师",
    "hunter": "猎人",
    "mage": "法师",
    "monk": "武僧",
    "paladin": "圣骑士",
    "priest": "牧师",
    "rogue": "潜行者",
    "shaman": "萨满祭司",
    "warlock": "术士",
    "warrior": "战士",
}


PEAK_SPEC_CN = {
    "blood": "鲜血",
    "frost": "冰霜",
    "unholy": "邪恶",
    "havoc": "浩劫",
    "vengeance": "复仇",
    "devourer": "噬灭",
    "balance": "平衡",
    "feral": "野性",
    "guardian": "守护",
    "restoration": "恢复",
    "devastation": "湮灭",
    "preservation": "恩护",
    "augmentation": "增辉",
    "beast-mastery": "兽王",
    "marksmanship": "射击",
    "survival": "生存",
    "arcane": "奥术",
    "fire": "火焰",
    "brewmaster": "酒仙",
    "mistweaver": "织雾",
    "windwalker": "踏风",
    "holy": "神圣",
    "protection": "防护",
    "retribution": "惩戒",
    "discipline": "戒律",
    "shadow": "暗影",
    "assassination": "奇袭",
    "outlaw": "狂徒",
    "subtlety": "敏锐",
    "elemental": "元素",
    "enhancement": "增强",
    "affliction": "痛苦",
    "demonology": "恶魔学识",
    "destruction": "毁灭",
    "arms": "武器",
    "fury": "狂怒",
}


MYTHICSTATS_SPEC_CN = {
    "unholy-death-knight": "邪恶",
    "frost-death-knight": "冰霜",
    "blood-death-knight": "鲜血",
    "demonology-warlock": "恶魔",
    "affliction-warlock": "痛苦",
    "destruction-warlock": "毁灭",
    "devourer-demon-hunter": "噬灭",
    "havoc-demon-hunter": "浩劫",
    "vengeance-demon-hunter": "复仇",
    "retribution-paladin": "惩戒",
    "protection-paladin": "防护",
    "holy-paladin": "神圣",
    "arms-warrior": "武器",
    "fury-warrior": "狂怒",
    "protection-warrior": "防护",
    "outlaw-rogue": "狂徒",
    "subtlety-rogue": "敏锐",
    "assassination-rogue": "奇袭",
    "feral-druid": "野性",
    "balance-druid": "平衡",
    "guardian-druid": "守护",
    "restoration-druid": "恢复",
    "survival-hunter": "生存",
    "beast-mastery-hunter": "兽王",
    "marksmanship-hunter": "射击",
    "enhancement-shaman": "增强",
    "elemental-shaman": "元素",
    "restoration-shaman": "恢复",
    "augmentation-evoker": "增辉",
    "devastation-evoker": "湮灭",
    "preservation-evoker": "恩护",
    "windwalker-monk": "踏风",
    "brewmaster-monk": "酒仙",
    "mistweaver-monk": "织雾",
    "shadow-priest": "暗影",
    "discipline-priest": "戒律",
    "holy-priest": "神圣",
    "arcane-mage": "奥术",
    "fire-mage": "火焰",
    "frost-mage": "冰霜",
}


DUNGEON_CN = {
    "algethar-academy": "艾杰斯亚学院",
    "magisters-terrace": "魔导师平台",
    "maisara-caverns": "迈萨拉洞窟",
    "nexuspoint-xenas": "节点希纳斯",
    "pit-of-saron": "萨隆矿坑",
    "seat-of-the-triumvirate": "执政团之座",
    "skyreach": "通天峰",
    "windrunner-spire": "风行者之塔",
}


def cn_class_from_slug(slug, fallback=""):
    k = str(slug or "").strip().lower()
    return PEAK_CLASS_CN.get(k) or (fallback or slug or "")


def cn_spec_from_slug(slug, fallback=""):
    k = str(slug or "").strip().lower()
    return PEAK_SPEC_CN.get(k) or (fallback or slug or "")


def cn_class_spec(*, class_slug, spec_slug, class_name="", spec_name=""):
    cls = cn_class_from_slug(class_slug, class_name)
    sp = cn_spec_from_slug(spec_slug, spec_name)
    if cls and sp:
        return f"{cls}-{sp}"
    return cls or sp or ""


def cn_dungeon_from_slug(slug, fallback=""):
    k = str(slug or "").strip().lower()
    return DUNGEON_CN.get(k) or (fallback or slug or "")


def _mythicstats_class_from_spec_slug(spec_slug):
    s = str(spec_slug or "").strip().lower()
    if not s:
        return ""
    if s.endswith("death-knight"):
        return "death-knight"
    if s.endswith("demon-hunter"):
        return "demon-hunter"
    parts = [p for p in re.split(r"[-_]+", s) if p]
    return parts[-1] if parts else ""


def cn_mythicstats_spec_display(spec_slug, spec_name=""):
    slug = str(spec_slug or "").strip().lower()
    spec_cn = MYTHICSTATS_SPEC_CN.get(slug)
    cls_cn = cn_class_from_slug(_mythicstats_class_from_spec_slug(slug))
    if spec_cn and cls_cn:
        return f"{spec_cn}（{cls_cn}）"
    if spec_cn:
        return spec_cn
    return spec_name or spec_slug or ""

