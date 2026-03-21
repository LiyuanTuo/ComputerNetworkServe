
import socket
import threading
import sys
import pyaudio
import wave
import base64
import os


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
    p = pyaudio.PyAudio()
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
    p.terminate()

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
        p = pyaudio.PyAudio()
        
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
        p.terminate()
        wf.close()
    except Exception as e:
        print(f"\n[系统] 播放音频失败: {e}")

# ================= 实时语音连麦功能模块 =================
import time

# ==== 实时语音状态控制 ====
udp_voice_active = False
udp_voice_socket = None
VOICE_RATE = 16000  # 实时语音优化采样率

def udp_audio_send_thread(udp_sock, server_ip, server_port):
    global udp_voice_active
    p = pyaudio.PyAudio()
    stream = p.open(format=FORMAT,
                    channels=CHANNELS,
                    rate=VOICE_RATE,
                    input=True,
                    frames_per_buffer=CHUNK)
    print("\n[系统] 麦克风已开启，双向语音打通！(输入 /realtime -quit 挂断)")
    print("你> ", end="", flush=True)
    try:
        while udp_voice_active:
            data = stream.read(CHUNK, exception_on_overflow=False)
            udp_sock.sendto(data, (server_ip, server_port))
    except Exception as e:
        pass
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()

def udp_audio_recv_thread(udp_sock):
    global udp_voice_active
    p = pyaudio.PyAudio()
    stream = p.open(format=FORMAT,
                    channels=CHANNELS,
                    rate=VOICE_RATE,
                    output=True,
                    frames_per_buffer=CHUNK)
    try:
        while udp_voice_active:
            data, addr = udp_sock.recvfrom(4096)
            stream.write(data)
    except Exception as e:
        pass
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()

def start_realtime_audio(server_ip, server_port):
    global udp_voice_active, udp_voice_socket
    if udp_voice_active: return
    
    udp_voice_active = True
    udp_voice_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    
    # 【核心：主动打洞】连发5个空包，将本机局域网后的公网端口暴露给服务器
    for _ in range(5):
        udp_voice_socket.sendto(b"HOLE_PUNCH", (server_ip, server_port))
        time.sleep(0.1)

    t1 = threading.Thread(target=udp_audio_send_thread, args=(udp_voice_socket, server_ip, server_port), daemon=True)
    t2 = threading.Thread(target=udp_audio_recv_thread, args=(udp_voice_socket,), daemon=True)
    t1.start()
    t2.start()

def stop_realtime_audio():
    global udp_voice_active, udp_voice_socket
    udp_voice_active = False
    if udp_voice_socket:
        try:
            # 发送一空包打破 recvfrom 阻塞
            udp_voice_socket.sendto(b"", ("127.0.0.1", udp_voice_socket.getsockname()[1]))
            udp_voice_socket.close()
        except: pass
        udp_voice_socket = None



