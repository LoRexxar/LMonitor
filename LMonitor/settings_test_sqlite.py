import os

from .settings_dev import *  # noqa

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        # 测试套件默认继续使用内存库；本地页面联调可显式提供持久化路径，
        # 避免管理命令进程退出后导入数据立即消失。
        'NAME': os.environ.get('LMONITOR_TEST_SQLITE_PATH', ':memory:'),
    }
}

EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
