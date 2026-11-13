"""
应用程序入口

启动顺序与依赖组装都在 app.bootstrap.run()；这里不导入 ui/core/data，
保证日志在这些模块导入之前就已初始化。
"""
import sys

from utils.runtime_env import configure_math_threads

configure_math_threads()

if "--self-test" in sys.argv[1:]:
    # 打包自检：窗口程序里导入失败（缺运行库）时，PyInstaller 引导程序会弹模态错误框并一直
    # 等人点击，构建脚本就会无限等下去。这里改为把原因写出来并以失败码退出
    try:
        from app.bootstrap import run
    except BaseException:
        import os
        import traceback
        _text = "[失败] 自检失败：启动阶段导入出错\n" + traceback.format_exc()
        for _stream in (sys.stdout, sys.stderr):
            if _stream is not None:
                try:
                    _stream.write(_text)
                    _stream.flush()
                except (OSError, ValueError, UnicodeError):
                    pass
        os._exit(5)
else:
    from app.bootstrap import run

if __name__ == "__main__":
    sys.exit(run(sys.argv))
