"""
LMonitor 开发环境配置 — 用于连接服务器数据库做验证
"""
import os

try:
    import pymysql
    pymysql.install_as_MySQLdb()
except ImportError:
    pass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SECRET_KEY = 'dev-key-not-for-production'
DEBUG = True
ALLOWED_HOSTS = ['*']

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'botend.apps.BotendConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
]

ROOT_URLCONF = 'LMonitor.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, 'templates')],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

# 连接服务器数据库（通过 SSH 隧道或直接连接）
# 如果直接连接: HOST=121.4.104.77，需要 MySQL 开放远程访问
# 如果 SSH 隧道: ssh -L 3307:127.0.0.1:3306 lighthouse@121.4.104.77，然后 HOST=127.0.0.1, PORT=3307
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': 'lmonitor',
        'USER': 'LMonitor',
        'PASSWORD': 'VM7pZuI2JVGtsKq0',
        'HOST': '121.4.104.77',
        'PORT': '3306',
        'OPTIONS': {
            'init_command': "SET SESSION sql_mode='STRICT_TRANS_TABLES'",
            'charset': 'utf8mb4',
        },
    }
}

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_L10N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
STATICFILES_DIRS = [os.path.join(BASE_DIR, 'static')]

LOGIN_URL = '/auth/login/'
LOGIN_REDIRECT_URL = '/dashboard/'
LOGOUT_REDIRECT_URL = '/auth/login/'
ALLOW_REGISTRATION = False

MONITOR_TASK_AUTO_SYNC_PLUGINS = True

REQUEST_CONFIG = {
    "timeout": (5, 20),
    "retries": 2,
    "enable_proxy": False,
}

WCL_V2_CONFIG = {
    "client_id": "a1720ed2-2ca1-4363-97cc-6897b18229ed",
    "client_secret": "I0XFgS9X0b8r8aMWZZv4rqIH9NuCtG1U88bJd5Zl",
}
# Battle.net API (WoW Character Statistics)
BATTLENET_CONFIG = {
    "client_id": "c37aaac5d0de4e8d8d9b03c958363222",
    "client_secret": "cqglr1sd7x8FxqEbFQs0ama59pJLQJU8",
    "token_url": "https://oauth.battle.net/token",
    "api_host_us": "https://us.api.blizzard.com",
    "api_host_cn": "https://gateway.battlenet.com.cn",
}

SIMC_CONFIG = {
    "simc_source_dir": "/home/lighthouse/simc",
    "simc_build_dir": "/home/lighthouse/simc/build-cli",
    "simc_path": "/home/lighthouse/simc/build-cli/simc",
    "result_path": "static/simc_results/",
    "simc_template": "LMonitor/simc_template.txt",
    "update_check_interval_seconds": 1800,
    "compile_threads": 2,
}
