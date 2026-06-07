"""
项目初始化。

兼容：在本地环境未安装 mysqlclient (MySQLdb) 的情况下，使用 PyMySQL 作为替代驱动，
避免 Django 连接 MySQL 时直接 ImportError。
"""

try:
    import MySQLdb  # noqa: F401
except Exception:
    try:
        import pymysql

        pymysql.install_as_MySQLdb()
    except Exception:
        # 仍然允许以 SQLite 模式启动（LMONITOR_USE_SQLITE=1）
        pass
