
import socket
import threading
import sys
import pyaudio
import wave
import base64
import os
import time
import time

# 音频录制配置参数
CHUNK = 1024             # 采样块大小
FORMAT = pyaudio.paInt16 # 量化位深：16位 (2字节)
CHANNELS = 1             # 单声道
RATE = 44100             # 采样率
RECORD_SECONDS = 3       # 默认语音留言时长：3 秒
TEMP_WAV_FILE = "temp_voice.wav"  # 用于录音写入的临时文件名

def record_audio():
    """
    采集麦克风音频，并保存为本地的 .wav 临时文件
    """
    p = get_pyaudio()
    print(f"\n[系统] 开始录音，时长 {RECORD_SECONDS} 秒...")
    
    stream = p.open(format=FORMAT,
                    channels=CHANNELS,
                    rate=RATE,
                    input=True,
                    frames_per_buffer=CHUNK)

    frames = []
    # 循环读取音频流
    for i in range(0, int(RATE / CHUNK * RECORD_SECONDS)):
        data = stream.read(CHUNK)
        frames.append(data)

    print("[系统] 录音结束。")
    stream.stop_stream()
    stream.close()
    # p.terminate()

    # 使用 wave 库，将刚刚录制的 raw 字节流打包成标准的 wav 文件
    wf = wave.open(TEMP_WAV_FILE, 'wb')
    wf.setnchannels(CHANNELS)
    wf.setsampwidth(p.get_sample_size(FORMAT))
    wf.setframerate(RATE)
    wf.writeframes(b''.join(frames))
    wf.close()



def play_audio(filename):
    """
    读取指定路径的 wav 文件并通过扬声器播放
    """
    try:
        wf = wave.open(filename, 'rb')
        p = get_pyaudio()
        
        # 根据读取出的文件头信息，设定播音流的参数
        stream = p.open(format=p.get_format_from_width(wf.getsampwidth()),
                        channels=wf.getnchannels(),
                        rate=wf.getframerate(),
                        output=True)

        data = wf.readframes(CHUNK)
        while data:
            stream.write(data)
            data = wf.readframes(CHUNK)

        stream.stop_stream()
        stream.close()
        # p.terminate()
        wf.close()
    except Exception as e:
        print(f"\n[系统] 播放音频失败: {e}")

# ================= 实时语音连麦功能模块 ================

# ==== 实时语音状态控制 ====
udp_voice_active = False
udp_voice_socket = None
VOICE_RATE = 16000  # 实时语音优化采样率


# 全局 Pyaudio 对象，避免在多个线程中同时初始化导致 C 语言层面发生 Segfault 卡退
_pyaudio_instance = None
_pyaudio_lock = threading.Lock()

def get_pyaudio():
    global _pyaudio_instance
    with _pyaudio_lock:
        if _pyaudio_instance is None:
            _pyaudio_instance = pyaudio.PyAudio()
        return _pyaudio_instance

def udp_audio_send_thread(udp_sock, server_ip, server_port):
    """
    实时语音发送线程：负责采集本地麦克风的声音并实时发送给服务器。
    """
    global udp_voice_active
    p = get_pyaudio()
    
    # 开启麦克风输入流，采样率为专门的 VOICE_RATE (16000)
    stream = p.open(format=FORMAT,
                    channels=CHANNELS,
                    rate=VOICE_RATE,
                    input=True,
                    frames_per_buffer=CHUNK)
    print("\n[系统] 麦克风已开启，双向语音打通！(输入 /realtime -quit 挂断)")
    print("你> ", end="", flush=True)
    try:
        # 当语音通话处于激活状态时，持续采集并发送
        while udp_voice_active:
            # 每次读取 CHUNK(1024) 大小的音频块，禁用溢出异常以防卡顿
            data = stream.read(CHUNK, exception_on_overflow=False)
            # 通过 UDP socket 直接扔向服务器，延迟极低
            udp_sock.sendto(data, (server_ip, server_port))
    except Exception as e:
        # print(f"\\n[发送线程异常] {e}")
        pass
    finally:
        # 退出循环后安全释放声卡及流资源
        stream.stop_stream()
        stream.close()
        # 注意：使用全局 _pyaudio_instance 后，就不要终止它了
        # p.terminate()


def udp_audio_recv_thread(udp_sock):
    """
    实时语音接收线程：负责从网络接收对方的音频数据并输出到本地扬声器。
    
    参数：
      - udp_sock: 用于接收数据的 UDP Socket 对象，绑定本地端口
    """
    global udp_voice_active
    p = get_pyaudio()
    
    # 开启扬声器输出流
    stream = p.open(format=FORMAT,
                    channels=CHANNELS,
                    rate=VOICE_RATE,
                    output=True,
                    frames_per_buffer=CHUNK)
    try:
        while udp_voice_active:
            # 阻塞等待网络端传来的音频包 (最大缓冲 4096 字节)
            data, addr = udp_sock.recvfrom(4096)
            # 过滤掉由于打洞产生的 HOLE_PUNCH 包以及非正常大小的包
            if data == b"HOLE_PUNCH" or len(data) == 0:
                continue
            # 接收到音频数据后，直接写入扬声器播放发声
            stream.write(data)
    except Exception as e:
        # 调试排错输出
        # print(f"\\n[接收线程异常] {e}")
        pass
    finally:
        # 退出流与声卡资源
        stream.stop_stream()
        stream.close()
        # p.terminate()

def start_realtime_audio(server_ip, server_port):
    """
    启动实时语音通话逻辑：建立网络数据通道并拉起收发双线程。
    
    参数:
      - server_ip: 服务器 IP
      - server_port: 服务器预先分配的接收端口
    """
    global udp_voice_active, udp_voice_socket
    if udp_voice_active: return
    
    udp_voice_active = True
    udp_voice_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    
    # 【核心：主动打洞 (NAT Traversal)】
    # 在双向通信前连续发送 5 个空包 (诱饵包) 
    # 作用是向路由器上的 NAT 强行打开一个“局域网<->公网”的端口映射窗口
    # 以免后续服务器转发过来的语音包被路由器当成非法包直接拦截。
    for _ in range(5):
        udp_voice_socket.sendto(b"HOLE_PUNCH", (server_ip, server_port))
        time.sleep(0.1)

    # 以守护线程 (Daemon) 的方式启动采集与播放双线程
    t1 = threading.Thread(target=udp_audio_send_thread, args=(udp_voice_socket, server_ip, server_port), daemon=True)
    t2 = threading.Thread(target=udp_audio_recv_thread, args=(udp_voice_socket,), daemon=True)
    t1.start()
    t2.start()

def stop_realtime_audio():
    """
    结束实时语音通话逻辑：重置标示位，打断死锁，并安全关闭底层 UDP 通道。
    """
    global udp_voice_active, udp_voice_socket
    
    # 将标志位置为 False，使得收发线程循环内的判断失效从而自动退出
    udp_voice_active = False
    
    if udp_voice_socket:
        try:
            # 【精妙断开法】向自己所在 socket 端口发送一个空包
            # 因为 recv_thread 此时大概率阻塞在 recvfrom 上等待对方发话
            # 仅修改 udp_voice_active 并不能马上将其唤醒，所以需要喂一个包强行打破阻塞
            udp_voice_socket.sendto(b"", ("127.0.0.1", udp_voice_socket.getsockname()[1]))
            # 唤醒后立刻关闭端口
            udp_voice_socket.close()
        except: pass
        udp_voice_socket = None



