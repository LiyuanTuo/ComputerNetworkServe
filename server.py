"""
TCP 聊天服务器
==============
功能：
  1. 监听指定端口，等待客户端连接
  2. 为每个客户端创建独立线程处理消息
  3. 将某个客户端发来的消息广播给所有其他在线客户端
  4. 维护在线用户列表，支持用户上下线通知

原理：
  - 使用 socket 模块创建 TCP 服务端 Socket
  - 使用 threading 模块为每个客户端连接派生独立线程（并发处理）
  - 服务器充当"消息中转站"，所有客户端的消息先发到服务器，再由服务器转发
"""

import socket
import threading
import json
import sys
from datetime import datetime

# ============ 配置 ============
HOST = "0.0.0.0"  # 监听所有网卡，局域网内其他主机可连接
PORT = 9999       # 服务端口号，客户端需要连接此端口
BUFFER_SIZE = 1024 * 1024 # 扩大到 1MB，否则装不下 Base64 的音频长字符串
ENCODING = "utf-8"

# ============ 全局状态 ============
clients: dict[socket.socket, str] = {}   # {socket对象: 用户名}
clients_lock = threading.Lock()          # 线程锁，保护 clients 字典
active_calls: dict = {}                  # {用户名: (对方用户名, udp_socket)}
active_calls_lock = threading.Lock()


logs = []                                # 存储日志，应该是列表类型
logs_lock = threading.Lock()             # 日志锁，保护 logs 列表


def timestamp() -> str:
    """返回当前时间字符串，用于消息前缀"""
    return datetime.now().strftime("%H:%M:%S")


# 进来时调用， 离开时调用，普通消息发送 时调用
def broadcast(message: str, sender_socket: socket.socket = None):
    """
    广播消息给所有在线客户端（可排除发送者自身）

    参数:
        message:        要广播的消息文本
        sender_socket:  发送者的 socket，如果指定则跳过该客户端
    """
    data = message.encode(ENCODING)
    with clients_lock:
        for client_sock in list(clients.keys()): # 遍历的对象的类型是socket.socket
            if client_sock == sender_socket:
                continue
            try:
                client_sock.sendall(data)
            except Exception:
                # 发送失败说明连接已断开，移除该客户端
                remove_client(client_sock)


def privatecast(message: str, target_name: str, sender_socket: socket.socket):
    """
    私聊消息发送给指定用户

    参数:
        message:        要发送的消息文本
        target_name:    目标用户名
        sender_socket:  发送者的 socket，用于排除自己
    """
    data = message.encode(ENCODING)
    with clients_lock:
        for client_sock, username in clients.items():
            if username == target_name and client_sock != sender_socket:
                try:
                    client_sock.sendall(data)
                except Exception:
                    # 发送失败说明连接已断开，移除该客户端
                    remove_client(client_sock)
                break  # 找到目标用户后就退出循环


# 发送失败说明连接已断开，移除客户端连接 / 在客户离开聊天室时调用，
def remove_client(client_sock: socket.socket):
    """安全移除一个客户端连接"""
    username = clients.pop(client_sock, None)
    try:
        client_sock.close()
    except Exception:
        pass
    return username


def inform_tcp_fallback(user1: str, user2: str):
    fallback_msg = f"[{timestamp()}] [系统] UDP 探测超时/被阻断，正自动降级为 TCP 语音中继模式！\n"
    with clients_lock:
        sock1 = next((s for s, u in clients.items() if u == user1), None)
        sock2 = next((s for s, u in clients.items() if u == user2), None)
        
    if sock1:
        try: sock1.sendall(fallback_msg.encode(ENCODING))
        except: pass
    if sock2:
        try: sock2.sendall(fallback_msg.encode(ENCODING))
        except: pass


def udp_voice_relay(udp_sock: socket.socket, user1: str, user2: str):
    """
    负责在两个客户端之间转发 UDP 语音包的线程
    由于不知道客户端的确切发包端口，采用"记录前两个不同的发送方地址"的策略，
    将收到的包相互转发实现 P2P 代理
    """
    addr1 = None
    addr2 = None
    fallback_triggered = False
    
    # 增加心跳探测机制：开头 5 秒钟内如果没有收到双方的 UDP 包，则认为被阻断
    udp_sock.settimeout(5.0) 
    
    try:
        while True:
            try:
                data, addr = udp_sock.recvfrom(4096)
            except socket.timeout:
                # 触发了 5 秒超时，且双方/单方连不上，则启动 TCP 降级
                if not addr1 or not addr2:
                    fallback_triggered = True
                    break
                else:
                    break   # 长久不说话导致的常规长超时断开
                
            if addr == addr1:
                if addr2:
                    udp_sock.sendto(data, addr2)
            elif addr == addr2:
                if addr1:
                    udp_sock.sendto(data, addr1)
            else:
                if not addr1:
                    addr1 = addr
                elif not addr2 and addr != addr1:
                    addr2 = addr
                    
                # 如果此时两端都已确认，网络打通！恢复300秒保活长超时
                if addr1 and addr2:
                    udp_sock.settimeout(300.0)
                    
                # 立即转发首包给对方
                if addr == addr1 and addr2:
                    udp_sock.sendto(data, addr2)
                elif addr == addr2 and addr1:
                    udp_sock.sendto(data, addr1)
    except OSError:
        pass
    finally:
        udp_sock.close()
        
        # 根据是否激发了防火墙降级，重置当前通讯模式为 TCP 面向对象
        if fallback_triggered:
            inform_tcp_fallback(user1, user2)
            with active_calls_lock:
                if user1 in active_calls and active_calls[user1][1] == udp_sock:
                    active_calls[user1] = (user2, "TCP_MODE")
                if user2 in active_calls and active_calls[user2][1] == udp_sock:
                    active_calls[user2] = (user1, "TCP_MODE")
        else:
            # 正常清场
            with active_calls_lock:
                if user1 in active_calls and active_calls[user1][1] == udp_sock:
                    del active_calls[user1]
                if user2 in active_calls and active_calls[user2][1] == udp_sock:
                    del active_calls[user2]


def end_realtime_voice(username: str, initiator: str = None):
    """
    终止实时语音通话并通知双方
    """
    with active_calls_lock:
        if username not in active_calls:
            return
        
        peer_name, relay_status = active_calls[username]
        # 从记录中移除双方
        del active_calls[username]
        if peer_name in active_calls:
            del active_calls[peer_name]
            
    # 如果还在 UDP 通道状态，则关闭 socket。如果是 TCP 降级通道那就不用关
    if isinstance(relay_status, socket.socket):
        try:
            relay_status.close()
        except Exception:
            pass
            
    # 通过 TCP 消息通知两端
    with clients_lock:
        # 获取最新的 socket 实例，因为可能会有重连或其他情况
        peer_sock = None
        initiator_sock = None
        for sock, name in clients.items():
            if name == peer_name:
                peer_sock = sock
            if name == initiator:
                initiator_sock = sock

    if peer_sock:
        try:
            peer_sock.sendall(f"\n[{timestamp()}] [系统] 实时语音通话已被 {'对方' if initiator else '系统'} 终止。\n".encode(ENCODING))
        except Exception:
            pass
            
    if initiator_sock and initiator == username:
        try:
            initiator_sock.sendall(f"\n[{timestamp()}] [系统] 您已成功终止实时语音通话。\n".encode(ENCODING))
        except Exception:
            pass


def start_realtime_voice(caller_name: str, target_name: str, caller_sock: socket.socket):
    """
    处理实时语音请求：分配UDP端口并通知双方
    """
    if caller_name == target_name:
        caller_sock.sendall(f"[{timestamp()}] [系统] 不能和自己建立语音。\n".encode(ENCODING))
        return

    with active_calls_lock:
        if caller_name in active_calls:
            caller_sock.sendall(f"[{timestamp()}] [系统] 您当前正在通话中，请先结束当前通话 (\RealTime -quit)。\n".encode(ENCODING))
            return
        if target_name in active_calls:
            caller_sock.sendall(f"[{timestamp()}] [系统] 目标用户 '{target_name}' 正在通话中。\n".encode(ENCODING))
            return

    target_sock = None
    # 在clients_lock 表中找到目标client的socket
    with clients_lock:
        for sock, name in clients.items():
            if name == target_name:
                target_sock = sock
                break
                
    if not target_sock:
        caller_sock.sendall(f"[{timestamp()}] [系统] 目标用户 '{target_name}' 不存在或未在线。\n".encode(ENCODING))
        return

    # 创建一个新的 UDP socket 用于此会话的转发
    relay_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    relay_sock.bind((HOST, 0)) # 0 表示让系统自动分配可用端口
    relay_port = relay_sock.getsockname()[1]

    # 将此次通话进行登记以供后续查询或强制结束
    with active_calls_lock:
        active_calls[caller_name] = (target_name, relay_sock)
        active_calls[target_name] = (caller_name, relay_sock)

    # 启动 UDP 转发线程
    t = threading.Thread(
        target=udp_voice_relay,
        args=(relay_sock, caller_name, target_name),
        daemon=True
    )
    t.start()

    # 通知双方 UDP 端口
    msg_to_caller = f"[{timestamp()}] [系统] 正在与 '{target_name}' 建立实时语音。请向服务器 UDP 端口 {relay_port} 发送/接收语音。\n"
    msg_to_target = f"[{timestamp()}] [系统] '{caller_name}' 向您发起实时语音！请向服务器 UDP 端口 {relay_port} 发送/接收语音。\n"
    
    try:
        caller_sock.sendall(msg_to_caller.encode(ENCODING))
    except Exception: 
        pass
    try:
        target_sock.sendall(msg_to_target.encode(ENCODING))
    except Exception: 
        pass


def handle_client(client_sock: socket.socket, addr: tuple):
    """
    处理单个客户端的线程函数

    流程:
      1. 接收客户端发来的用户名
      2. 循环接收消息并广播
      3. 连接断开时清理资源

    参数:
        client_sock: 客户端 socket 对象
        addr:        客户端地址 (ip, port)
    """
    username = None
    try:
        # ---- 第一步：接收用户名 ----
        raw = client_sock.recv(BUFFER_SIZE) # 这里是不是阻塞了？ 是的，recv() 是一个阻塞调用，如果客户端没有发送数据，服务器线程会在这里等待，直到收到数据或者连接断开。有超时机制吗？没有设置超时，所以如果客户端连接后不发送用户名，服务器线程会一直阻塞在这里。可以考虑设置 socket 超时来避免这种情况，但目前代码中没有实现这一点。
        if not raw:
            client_sock.close()
            return  

        username = raw.decode(ENCODING).strip()
        with clients_lock:
            clients[client_sock] = username


        # 通知所有人有新用户上线
        join_msg = f"[{timestamp()}] >>> '{username}' 加入了聊天室 <<<"
        broadcast(join_msg)

        with logs_lock:
            logs.append({
                "timestamp": timestamp(),
                "event": "user join",
                "username": username
            })


        # 给新用户发送欢迎消息和在线列表
        with clients_lock:
            online = ", ".join(clients.values())
        welcome = f"[{timestamp()}] 欢迎 {username}！当前在线用户: {online}\n"
        client_sock.sendall(welcome.encode(ENCODING))

        # ---- 第二步：循环接收并广播消息 ----
        while True:
            data = client_sock.recv(BUFFER_SIZE) # 这里也是阻塞的，recv() 会等待客户端发送消息或者断开连接。如果客户端断开连接，recv() 会返回空数据，这时服务器线程会跳出循环进行清理。
            if not data:
                break  # 客户端断开连接

            text = data.decode(ENCODING).strip() # decode 是从字节转换成字符串
            if not text:
                continue

            # 处理特殊命令
            if text.lower() == "/online":
                with clients_lock:
                    online = ", ".join(clients.values())
                client_sock.sendall(
                    f"[{timestamp()}] 在线用户: {online}\n".encode(ENCODING)
                )
            elif text.lower() == "/quit":
                break
            elif text.lower() == r"\realtime -quit" or text.lower() == r"\realttime -quit":
                # 主动结束当前的实时语音
                end_realtime_voice(username, initiator=username)
            elif text.lower().startswith("\\realttime @") or text.lower().startswith("\\realtime @"):
                # 处理实时点对点语音请求 "\RealTtime @ C2" 或 "\RealTime @ C2"
                parts = text.split('@', 1)
                if len(parts) == 2:
                    target_name = parts[1].strip()
                    start_realtime_voice(username, target_name, client_sock)
            elif text.lower().startswith("\\tcp_voice @"):
                # 接收降级后的音频数据：\TCP_VOICE @对方名字 payload二进制
                parts = text.split(' ', 2)
                if len(parts) >= 3:
                    target_name = parts[1][1:] # 移除@
                    voice_data = parts[2]
                    # 给目标直接做系统级别中继下发
                    privatecast(f"\\TCP_VOICE_FROM @{username} {voice_data}", target_name, client_sock)
            elif text[0] == '@':
                # become    @tuoliyuan AUDIO:xxxxxx
                target_name = text.split(sep=' ')[0][1:]  # targetname = tuoliyuan
                formatted = f"[{timestamp()}] {username}: {text.split(sep=' ')[1]}" # text.split(sep=' ')[1] = AUDIO:xxxxxx
                # 提取目标用户名
                privatecast(formatted, target_name, sender_socket=client_sock)

            else:
                # 普通消息 → 广播给其他客户端
                formatted = f"[{timestamp()}] {username}: {text}"
                broadcast(formatted, sender_socket=client_sock)

    except ConnectionResetError:
        pass
    except Exception as e:
        print(f"[错误] 处理客户端 {addr} 时出错: {e}")
    finally:
        # 确保突发断开时自动关闭语音通话
        end_realtime_voice(username)
        # ---- 第三步：清理 ----
        with clients_lock:
            username = remove_client(client_sock)
        if username:
            leave_msg = f"[{timestamp()}] >>> '{username}' 离开了聊天室 <<<"
            broadcast(leave_msg)
            with logs_lock:
                logs.append({
                    "timestamp": timestamp(),
                    "event": "user leave",
                    "username": username
                })


def start_server():
    """
    启动 TCP 服务器

    步骤：
      1. 创建 TCP Socket
      2. 绑定地址与端口
      3. 开始监听
      4. 循环 accept 新连接，为每个连接创建处理线程
    """
    # 创建 TCP/IPv4 套接字
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # SO_REUSEADDR 允许端口复用，避免服务器重启时 "Address already in use"
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # 绑定到指定地址和端口 bind 中文意思是绑定，服务器需要绑定一个地址和端口来监听客户端连接请求
    server_sock.bind((HOST, PORT))

    # 开始监听，backlog=5 表示最多排队 5 个未处理的连接请求
    server_sock.listen(5)

    # 获取本机局域网 IP（方便客户端连接）
    local_ip = socket.gethostbyname(socket.gethostname())
    print("=" * 50)
    print(f"  聊天服务器已启动")
    print(f"  监听地址: {HOST}:{PORT}")
    print(f"  局域网 IP: {local_ip}")
    print(f"  客户端请连接 → {local_ip}:{PORT}")
    print("=" * 50)

    # 设置超时时间，解决 Windows 下 accept() 阻塞导致无法响应 Ctrl+C 的问题
    server_sock.settimeout(1.0)

    try:
        while True:
            try:
                # accept() 每 1 秒会引发一次 timeout 异常
                client_sock, addr = server_sock.accept()
            except socket.timeout:
                # 如果是超时，说明这一秒内没人连接，继续下一轮循环
                # 在这个时候如果用户按了 Ctrl+C，Python 就能捕获到了
                continue

            # addr 是一个元组 (ip, port)，e.g. ("192.168.1.5", 54321)
            # 为每个客户端创建守护线程
            thread = threading.Thread(
                target=handle_client, # 调用处理客户端的函数
                args=(client_sock, addr),
                daemon=True  # 守护线程：主线程退出时自动终止
            )
            thread.start()

    except KeyboardInterrupt:
        print("\n[服务器] 正在关闭...")
    finally:
        # 关闭所有客户端连接
        with clients_lock:
            for sock in list(clients.keys()):
                try:
                    sock.close()
                except Exception:
                    pass
            clients.clear()
        server_sock.close()
        print("[服务器] 已关闭")

        with logs_lock:
            with open("logs.jsonl", "a", encoding=ENCODING) as f: # 改后缀为 .jsonl 区分
                for entry in logs:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            print(f"[系统] 已追加 {len(logs)} 条新记录")


if __name__ == "__main__":
        
    start_server()
