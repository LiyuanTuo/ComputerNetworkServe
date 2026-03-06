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
BUFFER_SIZE = 4096
ENCODING = "utf-8"

# ============ 全局状态 ============
clients: dict[socket.socket, str] = {}   # {socket对象: 用户名}
clients_lock = threading.Lock()          # 线程锁，保护 clients 字典


def timestamp() -> str:
    """返回当前时间字符串，用于消息前缀"""
    return datetime.now().strftime("%H:%M:%S")


def broadcast(message: str, sender_socket: socket.socket = None):
    """
    广播消息给所有在线客户端（可排除发送者自身）

    参数:
        message:        要广播的消息文本
        sender_socket:  发送者的 socket，如果指定则跳过该客户端
    """
    data = message.encode(ENCODING)
    with clients_lock:
        for client_sock in list(clients.keys()):
            if client_sock == sender_socket:
                continue
            try:
                client_sock.sendall(data)
            except Exception:
                # 发送失败说明连接已断开，移除该客户端
                remove_client(client_sock)


def remove_client(client_sock: socket.socket):
    """安全移除一个客户端连接"""
    username = clients.pop(client_sock, None)
    try:
        client_sock.close()
    except Exception:
        pass
    return username


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
        raw = client_sock.recv(BUFFER_SIZE)
        if not raw:
            client_sock.close()
            return

        username = raw.decode(ENCODING).strip()
        with clients_lock:
            clients[client_sock] = username

        print(f"[{timestamp()}] 用户 '{username}' 已连接 ({addr[0]}:{addr[1]})")

        # 通知所有人有新用户上线
        join_msg = f"[{timestamp()}] >>> '{username}' 加入了聊天室 <<<"
        broadcast(join_msg)

        # 给新用户发送欢迎消息和在线列表
        with clients_lock:
            online = ", ".join(clients.values())
        welcome = f"[{timestamp()}] 欢迎 {username}！当前在线用户: {online}\n"
        client_sock.sendall(welcome.encode(ENCODING))

        # ---- 第二步：循环接收并广播消息 ----
        while True:
            data = client_sock.recv(BUFFER_SIZE)
            if not data:
                break  # 客户端断开连接

            text = data.decode(ENCODING).strip()
            if not text:
                continue

            # 处理特殊命令
            if text.lower() == "/online":
                with clients_lock:
                    online = ", ".join(clients.values())
                client_sock.sendall(
                    f"[{timestamp()}] 在线用户: {online}\n".encode(ENCODING)
                )
                continue

            if text.lower() == "/quit":
                break

            # 普通消息 → 广播给其他客户端
            formatted = f"[{timestamp()}] {username}: {text}"
            print(formatted)  # 服务器控制台也打印
            broadcast(formatted, sender_socket=client_sock)

    except ConnectionResetError:
        pass
    except Exception as e:
        print(f"[错误] 处理客户端 {addr} 时出错: {e}")
    finally:
        # ---- 第三步：清理 ----
        with clients_lock:
            username = remove_client(client_sock)
        if username:
            leave_msg = f"[{timestamp()}] >>> '{username}' 离开了聊天室 <<<"
            print(leave_msg)
            broadcast(leave_msg)


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

    # 绑定到指定地址和端口
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

    try:
        while True:
            # accept() 阻塞等待新的客户端连接
            client_sock, addr = server_sock.accept()

            # 为每个客户端创建守护线程
            thread = threading.Thread(
                target=handle_client,
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


if __name__ == "__main__":
    start_server()
