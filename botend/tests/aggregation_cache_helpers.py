"""隔离文件缓存，避免数据库用例复用主键后读取其他用例的中间结果。"""
import tempfile
from pathlib import Path


def isolate_work_cache(test):
    base = Path(__file__).resolve().parents[2] / 'tmp'
    base.mkdir(exist_ok=True)
    directory = tempfile.TemporaryDirectory(prefix='聚合文件测试-', dir=base)
    test.addCleanup(directory.cleanup)
    root = Path(directory.name)
    test.enterContext(test.settings(SPEC_AGGREGATION_WORK_ROOT=root))
    return root
