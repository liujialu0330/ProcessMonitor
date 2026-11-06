"""
应用程序入口

启动顺序与依赖组装都在 app.bootstrap.run()；这里不导入 ui/core/data，
保证日志在这些模块导入之前就已初始化。
"""
import sys

from app.bootstrap import run

if __name__ == "__main__":
    sys.exit(run(sys.argv))
