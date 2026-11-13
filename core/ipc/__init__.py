"""
本地 IPC：版本化 JSON 行协议、服务端、命令白名单与标准库客户端

- protocol：版本号、错误码、编解码与分帧（纯标准库）；
- client：ctypes 命名管道客户端（纯标准库，pmctl 与 tools/pmclient.py 复用）；
- server：IpcServer（QLocalServer 封装，每连接独立缓冲、客户端身份核验）；
- commands：命令白名单与处理器（CommandDispatcher）。

本包的 __init__ 不导入任何子模块：pmctl 只能导入 protocol 与 client，
不得连带导入 PyQt5。
"""
