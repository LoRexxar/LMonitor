"""兼容旧命令入口；只输出伤害相关天赋与自身状态的排除审计清单。"""
import sys
from build_simc_scope_review_list import main

if __name__ == '__main__':
    # 旧候选目录使用的汇总报告参数已由实际导出的版本校验替代。
    if '--report' in sys.argv:
        position = sys.argv.index('--report')
        sys.argv[position:position + 2] = []
    while '--initialization' in sys.argv:
        position = sys.argv.index('--initialization')
        sys.argv[position:position + 2] = []
    main()
