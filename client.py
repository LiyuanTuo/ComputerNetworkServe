"""
TCP 聊天客户端
==============
功能：
  1. 连接到局域网内的聊天服务器
  2. 发送用户名进行注册
  3. 在独立线程中接收服务器推送的消息
  4. 主线程负责读取用户输入并发送

原理：
  - 客户端创建 TCP Socket 连接到服务器的 IP:PORT
  - 使用一个接收线程持续监听服务器消息（非阻塞体验）
  - 主线程阻塞在 input() 上等待用户输入
"""

import socket
import threading
import sys

# ============ 配置 ============
BUFFER_SIZE = 4096
ENCODING = "utf-8"


def receive_messages(sock: socket.socket, stop_event: threading.Event):
    """
    接收线程：持续从服务器接收消息并打印到终端

    参数:
        sock:        与服务器连接的 socket
        stop_event:  停止事件，用于通知该线程退出
    """
    while not stop_event.is_set():
        try:
            data = sock.recv(BUFFER_SIZE)
            if not data:
                # 服务器关闭了连接
                print("\n[系统] 与服务器的连接已断开")
                stop_event.set()
                break
            message = data.decode(ENCODING)
            # \r 清除当前行的输入提示，打印消息后重新显示提示
            print(f"\r{message}")
            print("你> ", end="", flush=True)
        except ConnectionResetError:
            print("\n[系统] 连接被服务器重置")
            stop_event.set()
            break
        except OSError:
            # socket 被关闭
            break


def start_client():
    """
    启动 TCP 客户端

    步骤：
      1. 用户输入服务器 IP 和端口
      2. 建立 TCP 连接
      3. 发送用户名
      4. 启动接收线程
      5. 主线程循环等待用户输入并发送
    """
    print("=" * 50)
    print("  局域网聊天客户端")
    print("=" * 50)

    # ---- 获取连接信息 ----
    server_ip = input("请输入服务器 IP 地址 (默认 127.0.0.1): ").strip()
    if not server_ip:
        server_ip = "127.0.0.1"

    port_str = input("请输入服务器端口号 (默认 9999): ").strip()
    port = int(port_str) if port_str else 9999

    username = input("请输入你的用户名: ").strip()
    if not username:
        username = "匿名用户"

    # ---- 创建并连接 Socket ----
    client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    try:
        print(f"\n正在连接 {server_ip}:{port} ...")
        client_sock.connect((server_ip, port))
        print("连接成功！\n")
    except ConnectionRefusedError:
        print(f"[错误] 无法连接到 {server_ip}:{port}，请检查服务器是否已启动")
        sys.exit(1)
    except Exception as e:
        print(f"[错误] 连接失败: {e}")
        sys.exit(1)

    # ---- 发送用户名完成注册 ----
    client_sock.sendall(username.encode(ENCODING))

    # ---- 启动接收线程 ----
    stop_event = threading.Event()
    recv_thread = threading.Thread(
        target=receive_messages,
        args=(client_sock, stop_event),
        daemon=True
    )
    recv_thread.start()

    # ---- 主线程：发送消息 ----
    print("提示: 输入消息后按回车发送 | /online 查看在线用户 | /quit 退出\n")
    try:
        while not stop_event.is_set():
            print("你> ", end="", flush=True)
            msg = input()
            if not msg:
                continue

            client_sock.sendall(msg.encode(ENCODING))

            if msg.lower() == "/quit":
                print("[系统] 正在退出...")
                break

    except (KeyboardInterrupt, EOFError):
        print("\n[系统] 正在退出...")
    finally:
        stop_event.set()
        client_sock.close()
        print("[系统] 已断开连接")


if __name__ == "__main__":
    start_client()
