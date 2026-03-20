"""
TCP 聊天客户端 (带语音留言功能)
==============
功能：
  1. 连接到局域网内的聊天服务器
  2. 发送用户名进行注册
  3. 在独立线程中接收服务器推送的消息
  4. 主线程负责读取用户输入并发送
  5. 支持录制音频并发送为语音留言

原理：
  - 客户端创建 TCP Socket 连接到服务器的 IP:PORT
  - 使用一个接收线程持续监听服务器消息（非阻塞体验）
  - 主线程阻塞在 input() 上等待用户输入
"""

import socket
import threading
import sys
import pyaudio
import wave
import base64
import os
from audio import * 
# ============ 配置 ============
# 对于音频传输，普通的 4096 缓冲区不够大，改为 1MB
BUFFER_SIZE = 1024 * 1024 
ENCODING = "utf-8"



def receive_messages(sock: socket.socket, stop_event: threading.Event):
    """
    接收线程：持续从服务器接收消息并处理，包含解析 Base64 音频
    """
    while not stop_event.is_set():
        try:
            data = sock.recv(BUFFER_SIZE)
            if not data:
                print("\n[系统] 与服务器的连接已断开")
                stop_event.set()
                break
            
            message = data.decode(ENCODING)
            
            # --- 核心：协议解析 ---
            # 判断接收的字符串里有没有我们定义的音频标头 `AUDIO:`
            if "AUDIO:" in message:
                # 字符串长这样： "[14:20:30] 张三: AUDIO:UklGR...="
                # 分割为头部和音频 Base64 数据
                prefix, b64_audio = message.split("AUDIO:", 1)
                print(f"\r{prefix} 发送了一段语音消息，正在播放...")
                
                # 1. 还原：将 Base64 文本解码回原本生成的 Wav 二进制流
                wav_bytes = base64.b64decode(b64_audio)
                
                # 2. 保存磁盘：PyAudio/wave 库读文件播放更稳定
                recv_file = "recv_voice.wav"
                with open(recv_file, "wb") as f:
                    f.write(wav_bytes)
                
                # 3. 播放
                play_audio(recv_file)
                
            else:
                # 不是语音，那就当做普通文字打印
                print(f"\r{message}")
            
            print("你> ", end="", flush=True)

        except ConnectionResetError:
            print("\n[系统] 连接被服务器重置")
            stop_event.set()
            break
        except OSError:
            break
        except Exception as e:
            # 数据量过大一次没接完等异常先忽略
            pass


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
    server_ip = "DESKTOP-4AFQ0JR" # 使用我的计算机名来作为服务器 就不用担心局域网内 IP 地址变化了 你们要改成你们自己的hostname 或者直接输入局域网 IP 地址

    port = 9999

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
    print("提示: 输入文字回车发送 | 输入 /voice 录制发送语音留言 | /quit 退出\n")
    try:
        while not stop_event.is_set():
            print("你> ", end="", flush=True)
            msg = input()
            if not msg: # 如果用户直接按回车，输入为空字符串，就继续下一轮循环，等待有效输入
                continue

            # ---- 处理录音指令 ----
            if "/voice" in msg.lower():
                record_audio()  # 录制音频成 wav 格式临时文件
                
                # 读出生成的 wav 文件的二进制内容
                with open(TEMP_WAV_FILE, "rb") as f:
                    wav_content = f.read()
                    
                # 编码为 base64 字符串。因为我们的协议是文本通讯
                b64_string = base64.b64encode(wav_content).decode(ENCODING) # 先返回 bytes，再解码成字符串，准备发送
                
                # 在前面加上标识符 "AUDIO:"
                # @tuoliyuan /voice 
                msg = f"{msg.split(sep = '/voice')[0]}AUDIO:{b64_string}"  # string has member function encode() but bytes doesn't, could receive para like "utf-8" or "ascii" to specify how to encode the string into bytes
                # become    @tuoliyuan AUDIO:xxxxxx

            # 普通文本消息，直接发
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
