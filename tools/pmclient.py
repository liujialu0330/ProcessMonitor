"""最小 IPC 客户端示例（只用标准库）：演示协议本身，便于在其他语言里照着实现。
用法：python tools/pmclient.py [--data-dir 目录] 命令 ['{"参数":"JSON"}']
例如：python tools/pmclient.py marker "{\"task_id\":\"all\"}"（不拉起程序、不核验服务端
身份；正式脚本请用 pmctl）
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.paths import AppPaths  # noqa: E402
from utils.instance_name import pipe_path, single_instance_name  # noqa: E402


def call(path: str, cmd: str, args: dict) -> dict:
    """发一行请求，跳过心跳事件，返回最终回复。"""
    with open(path, 'r+b', buffering=0) as pipe:
        line = json.dumps({'v': 1, 'id': 1, 'cmd': cmd, 'args': args}, ensure_ascii=True)
        pipe.write(line.encode('utf-8') + b'\n')
        while True:
            reply = json.loads(pipe.readline().decode('utf-8'))
            if 'event' not in reply:
                return reply


def main() -> int:
    parser = argparse.ArgumentParser(description="进程监控助手 IPC 协议示例客户端")
    parser.add_argument('--data-dir', help="数据目录（决定连接哪个实例）")
    parser.add_argument('cmd', help="命令名，如 ping、status、marker")
    parser.add_argument('args', nargs='?', default='{}', help="命令参数（JSON 对象）")
    ns = parser.parse_args()
    argv = [sys.argv[0]] + (['--data-dir', ns.data_dir] if ns.data_dir else [])
    name = single_instance_name(AppPaths.resolve(argv=argv).data_dir)
    try:
        reply = call(pipe_path(name), ns.cmd, json.loads(ns.args))
    except ValueError as exc:
        print(f"参数不是合法的 JSON：{exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"连接失败（程序没在运行？）：{exc}", file=sys.stderr)
        return 3
    print(json.dumps(reply, ensure_ascii=False, indent=2))
    return 0 if reply.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
