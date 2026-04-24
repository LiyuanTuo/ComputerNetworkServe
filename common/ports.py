"""共享端口配置。

端口规划目标：
1. 服务器 TCP 控制端口固定，客户端默认直接连接它。
2. 会议室语音 UDP 使用固定端口，便于排查与复现。
3. 客户端本地实时语音 UDP 与服务器端口错开，保证同一台 PC 同时运行 server/client 时不冲突。
4. 一对一通话的服务器 UDP 中继使用固定端口池，避免每次随机端口导致排查困难。
"""

DEFAULT_SERVER_IP = "10.192.49.3"

# TCP 控制面
SERVER_TCP_PORT = 11451

# UDP 语音面
ROOM_RELAY_UDP_PORT = 8888

# 客户端本地 UDP 端口（优先固定，端口占用时再按候选池顺延）
CLIENT_ROOM_LOCAL_UDP_PORTS = (9999, 10001, 10003)
CLIENT_CALL_LOCAL_UDP_PORTS = (10000, 10002, 10004)

# 服务器 1 对 1 通话 UDP 中继端口池
SERVER_CALL_RELAY_UDP_PORTS = tuple(range(11452, 11472))