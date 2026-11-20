"""展示上下文：区分当前动作、文档变量与全局技能身份，不执行 APL。"""

from collections.abc import Mapping


class ConditionContext(Mapping):
    def __init__(self, symbols, *, action='', variables=None):
        self.symbols = symbols
        self.action = action
        self.variables = variables if variables is not None else {}

    def __getitem__(self, key):
        return self.symbols[key]

    def __iter__(self):
        return iter(self.symbols)

    def __len__(self):
        return len(self.symbols)

    def for_action(self, action):
        return ConditionContext(self.symbols, action=action, variables=self.variables)
