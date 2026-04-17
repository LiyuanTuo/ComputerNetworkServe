
import socket
import threading
import sys
import pyaudio
import wave
import base64
import os
import time
import time
import math
import struct
from audio_eval import pack_audio_header, unpack_audio_header, make_sender_id, evaluator

try:
    import audioop
    def get_rms(data): return audioop.rms(data, 2)
except ImportError:
    def get_rms(data):
        count = len(data) // 2
        if count == 0: return 0
        shorts = struct.unpack(f"<{count}h", data)
        return math.sqrt(sum(s*s for s in shorts) / count)

# 音频录制配置参数
CHUNK = 1024             # 采样块大小
FORMAT = pyaudio.paInt16 # 量化位深：16位 (2字节)
CHANNELS = 1             # 单声道
RATE = 44100             # 采样率
VOICE_RATE = 16000       # 实时语音专用的相对较小采样率（节省带宽）
SILENCE_THRESHOLD = 500  # 语音活动检测(VAD) RMS 阈值，低于此值视为静音不发包
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
        _log_to_ui(f"播放音频失败: {e}")

# ================= 实时语音连麦功能模块 ================

# ==== 实时语音状态控制 ====
udp_session_active = False
audio_stream_active = False
udp_voice_socket = None
VOICE_RATE = 16000  # 实时语音优化采样率

ui_logger = None  # 用于向 GUI 界面输出中间变量的日志回调函数

def set_ui_logger(logger_func):
    global ui_logger
    ui_logger = logger_func

def _log_to_ui(msg):
    if ui_logger:
        ui_logger(msg)
    # else:
    #     print(msg) # 全局注释掉终端打印

audio_state_lock = threading.Lock()
nat_thread_obj = None
audio_send_thread_obj = None
audio_recv_thread_obj = None

# 通话控制状态
udp_voice_mute = False   # 麦克风静音状态 (自己说话不发送)
udp_voice_pause = False  # 会话暂停状态 (不发声音，不播放声音)
pending_mute = False
pending_pause = False
room_members = {}        # {用户名: (ip, port)}
          # {用户名: {"active": bool, "last_seen": float, "addr": (ip, port)}}
last_server_ip = ""
last_server_port = 0

# 音频评测报头用的序列号和发送者标识
_audio_seq = 0
_audio_sender_id = 0

def set_mute(state):
    global udp_voice_mute, pending_mute, audio_stream_active
    with audio_state_lock:
        if not audio_stream_active:
            pending_mute = state
            return
        if udp_voice_mute != state:
            udp_voice_mute = state
            _log_to_ui(f"[音频系统] 麦克风状态 -> {'静音' if state else '开启'}")
            

def set_pause(state):
    global udp_voice_pause, pending_pause, audio_stream_active
    with audio_state_lock:
        if not audio_stream_active:
            pending_pause = state
            return
        if udp_voice_pause != state:
            udp_voice_pause = state
            _log_to_ui(f"[音频系统] 语音状态 -> {'暂停' if state else '开启'}")
            


# 全局 Pyaudio 对象，避免在多个线程中同时初始化导致 C 语言层面发生 Segfault 卡退
_pyaudio_instance = None
_pyaudio_lock = threading.Lock()

def get_pyaudio():
    global _pyaudio_instance
    with _pyaudio_lock:
        if _pyaudio_instance is None:
            _pyaudio_instance = pyaudio.PyAudio()
        return _pyaudio_instance

def nat_maintenance_thread(udp_sock, username):
    """
    NAT 地址维护线程：定期向服务器重发 STUN_HELLO 以确保 NAT 地址注册不因丢包而失效。
    所有音频数据均通过服务器 RELAY 中转。
    """
    global udp_session_active
    while udp_session_active:
        # 定期向服务器重发 STUN_HELLO，保证 NAT 地址始终在服务器端注册
        if last_server_ip and last_server_port:
            try:
                udp_sock.sendto(f"STUN_HELLO {username}".encode("utf-8"), (last_server_ip, last_server_port))
            except Exception:
                pass
        time.sleep(2)

def udp_audio_send_thread(udp_sock, server_ip, server_port, username, room_id):
    """
    实时语音发送线程：负责采集本地麦克风的声音并实时发送给服务器。
    """
    global audio_stream_active, room_members, _audio_seq
    p = get_pyaudio()
    
    # 开启麦克风输入流，采样率为专门的 VOICE_RATE (16000)
    stream = p.open(format=FORMAT,
                    channels=CHANNELS,
                    rate=VOICE_RATE,
                    input=True,
                    frames_per_buffer=CHUNK)
    # 不要在后台线程随意穿插 print 和 输入提示符，会打乱界面的 "你> "
    last_send_print_time = 0
    try:
        # 当语音通话处于激活状态时，持续采集并发送
        while audio_stream_active:
            # 每次读取 CHUNK(1024) 大小的音频块，禁用溢出异常以防卡顿
            data = stream.read(CHUNK, exception_on_overflow=False)
            
            # 过滤发声：如果不在暂停并且没有静音
            if not udp_voice_pause and not udp_voice_mute:
                # VAD: 计算音量能量（RMS），低于阈值则不发包（静音滤除，节省带宽）
                rms_val = get_rms(data)
                # 简短的调试输出: 麦克风接收
                # print(f"\r[调试] 🎤麦克风已采集 {len(data)}B (RMS:{rms_val:.0f})".ljust(50), end="")
                
                if rms_val > SILENCE_THRESHOLD:
                    # 封装评测报头: [magic 2B][sender_id 4B][seq 4B][timestamp 8B] + PCM
                    _audio_seq += 1
                    audio_hdr = pack_audio_header(_audio_sender_id, _audio_seq, time.time())
                    payload = audio_hdr + data

                    now = time.time()
                    if now - last_send_print_time > 2.0:
                        _log_to_ui(f"[发送] 音量: {rms_val:.0f} 包大小: {len(payload)}B")
                        
                        last_send_print_time = now

                    if True:
                        if room_id == "":
                            # 1-on-1: 直接发送音频数据，无需RELAY封装
                            udp_sock.sendto(payload, (server_ip, server_port))
                        else:
                            # 统一通过服务器 RELAY 中转
                            for target in list(room_members):
                                if target != username:
                                    header = f"RELAY {username} {target} ".encode("utf-8")
                                    packet = header + payload
                                    udp_sock.sendto(packet, (server_ip, server_port))
    except Exception as e:
        _log_to_ui(f"\\n[发送线程异常] {e}")
        pass
    finally:
        # 退出循环后安全释放声卡及流资源
        stream.stop_stream()
        stream.close()
        # 注意：使用全局 _pyaudio_instance 后，就不要终止它了
        # p.terminate()


def _mix_audio_chunks(chunks_list):
    """
    将多路 PCM int16 音频数据混合为单路输出。
    对各路采样值逐点求和，然后裁剪到 int16 范围 [-32768, 32767]，防止溢出失真。
    """
    if not chunks_list:
        return None
    if len(chunks_list) == 1:
        return chunks_list[0]

    max_len = max(len(c) for c in chunks_list)
    n_samples = max_len // 2

    mixed = [0] * n_samples
    for chunk in chunks_list:
        n = len(chunk) // 2
        samples = struct.unpack(f'<{n}h', chunk[:n * 2])
        for i in range(n):
            mixed[i] += samples[i]

    for i in range(n_samples):
        if mixed[i] > 32767:
            mixed[i] = 32767
        elif mixed[i] < -32768:
            mixed[i] = -32768

    return struct.pack(f'<{n_samples}h', *mixed)


def udp_audio_recv_thread(udp_sock, username):
    """
    实时语音接收线程：负责从网络接收对方的音频数据并输出到本地扬声器。
    支持多路音频混音：将来自不同来源的音频在混音缓冲区中按 PCM 采样值叠加后统一播放，
    而非逐包串行写入（后者会导致多人同时说话时声音拉长）。
    同时兼任 UDP 心跳与控制包的接收任务。
    """
    global udp_session_active, audio_stream_active, udp_voice_pause
    p = get_pyaudio()
    stream = None

    # === 混音缓冲区 ===
    mix_sources = {}       # {source_key: [audio_bytes, ...]}
    MIX_INTERVAL = CHUNK / VOICE_RATE  # 一个 chunk 的时长（秒），约 64ms
    last_mix_time = time.time()
    last_recv_print_time = {}

    try:
        udp_sock.settimeout(MIX_INTERVAL)

        while udp_session_active:
            audio_data = None
            source_key = None

            try:
                data, addr = udp_sock.recvfrom(4096)
            except socket.timeout:
                data = None
                addr = None

            if data is not None:
                if data == b"HOLE_PUNCH" or len(data) == 0:
                    pass
                elif data.startswith(b"RELAY_DATA "):
                    payload = data[11:]
                    if payload.startswith(b"RELAY_TEXT "):
                        parts = payload.split(b" ", 2)
                        if len(parts) >= 3:
                            sender = parts[1].decode("utf-8")
                            msg = parts[2].decode("utf-8")
                            _log_to_ui(f"收到文本消息来自 {sender}: {msg}")
                    else:
                        audio_data = payload
                        source_key = ("relay", addr)
                else:
                    audio_data = data
                    source_key = ("raw", addr)

                # 将音频数据放入混音缓冲区
                if audio_data and source_key and audio_stream_active and not udp_voice_pause:
                    # 解析评测报头，提取序列号和时间戳用于质量评测
                    sid, seq, send_ts, pcm_data = unpack_audio_header(audio_data)
                    if sid is not None:
                        evaluator.record_packet(sid, seq, send_ts)
                        audio_data = pcm_data

                    if source_key not in mix_sources:
                        mix_sources[source_key] = []
                    mix_sources[source_key].append(audio_data)

                    now = time.time()
                    if source_key not in last_recv_print_time or (now - last_recv_print_time[source_key] > 2.0):
                        speaker_label = "服务器中继数据"
                        _log_to_ui(f"[接收] 已接收数据流包大小:{len(audio_data)}B")
                        last_recv_print_time[source_key] = now

            # === 定时混音输出 ===
            now = time.time()
            if now - last_mix_time >= MIX_INTERVAL:
                last_mix_time = now
                if mix_sources and audio_stream_active and not udp_voice_pause:
                    source_audios = []
                    for src_chunks in mix_sources.values():
                        source_audios.append(b''.join(src_chunks))
                    mixed = _mix_audio_chunks(source_audios)
                    if mixed:
                        try:
                            if stream is None:
                                stream = p.open(format=FORMAT, channels=CHANNELS,
                                                rate=VOICE_RATE, output=True,
                                                frames_per_buffer=CHUNK)
                                _log_to_ui(f"[音频系统] 成功打开播放设备")
                            stream.write(mixed)
                            # 间隔打印扬声器播放
                            if now - last_recv_print_time.get("speaker_mix", 0) > 4.0:
                                _log_to_ui(f"[播放] 正在播放混音的音频, 大小:{len(mixed)}B")
                                
                                last_recv_print_time["speaker_mix"] = now
                        except Exception as e:
                            _log_to_ui(f"播放异常: {e}")
                    mix_sources.clear()

    except Exception as e:
        _log_to_ui(f"接收/播放异常: {e}")
        pass
    finally:
        if stream:
            stream.stop_stream()
            stream.close()

last_username = ""
last_room_id = ""

def init_udp_session(server_ip, server_port, username="", room_id=""):
    """
    建立UDP会话，向服务器注册NAT地址，启动维护线程，但不启动音频流。
    """
    global udp_session_active, udp_voice_socket, last_server_ip, last_server_port
    global last_username, last_room_id, nat_thread_obj, audio_recv_thread_obj
    global _audio_seq, _audio_sender_id

    with audio_state_lock:
        if udp_session_active: return
        
        # 初始化评测报头用的序列号和发送者标识
        _audio_seq = 0
        _audio_sender_id = make_sender_id(username) if username else 0
        
        last_server_ip = server_ip
        last_server_port = server_port
        last_username = username
        last_room_id = room_id

        udp_session_active = True
        udp_voice_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        
        # 将client语音接收和发送的端口进行固定
        if room_id:
            try:
                # 尝试绑定固定端口 7777（单机测试如果有多个客户端可能会冲突，此时会自动采用随机端口）
                udp_voice_socket.bind(("0.0.0.0", 7777))
                _log_to_ui(f"[系统] 已固定本地UDP语音端口为 {udp_voice_socket.getsockname()[1]}")
            except OSError:
                udp_voice_socket.bind(("0.0.0.0", 0))
                _log_to_ui(f"[系统] 端口7777被占用，使用随机本地UDP语音端口 {udp_voice_socket.getsockname()[1]}")
        else:
            # 非会议室(单聊)时绑定随机端口
            udp_voice_socket.bind(("0.0.0.0", 0))

        if username:
            # 多次发送 STUN_HELLO 确保 NAT 地址注册成功（UDP 不可靠）
            for _ in range(3):
                udp_voice_socket.sendto(f"STUN_HELLO {username}".encode("utf-8"), (server_ip, server_port))

        for _ in range(5):
            udp_voice_socket.sendto(b"HOLE_PUNCH", (server_ip, server_port))
            time.sleep(0.1)

        if room_id:
            nat_thread_obj = threading.Thread(target=nat_maintenance_thread, args=(udp_voice_socket, username), daemon=True)
            nat_thread_obj.start()

        # 挂载接收线程用于心跳探测等控制流
        audio_recv_thread_obj = threading.Thread(target=udp_audio_recv_thread, args=(udp_voice_socket, username), daemon=True)
        audio_recv_thread_obj.start()

def start_audio_stream():
    """
    在UDP会话已存在的情况下，启动音频发送和接收线程。
    """
    global udp_session_active, audio_stream_active, audio_send_thread_obj
    global udp_voice_mute, udp_voice_pause, pending_mute, pending_pause

    with audio_state_lock:
        if not udp_session_active:
            print("\n[系统] UDP会话尚未建立，无法启动音频流。")
            return
        if audio_stream_active: return

        audio_stream_active = True
        udp_voice_mute = pending_mute
        udp_voice_pause = pending_pause

        audio_send_thread_obj = threading.Thread(target=udp_audio_send_thread, args=(udp_voice_socket, last_server_ip, last_server_port, last_username, last_room_id), daemon=True)
        audio_send_thread_obj.start()

def stop_audio_stream():
    """
    停止音频发送和接收线程，但不关闭UDP会话。
    """
    global audio_stream_active, udp_voice_socket, audio_send_thread_obj
    with audio_state_lock:
        if not audio_stream_active: return
        audio_stream_active = False

        if audio_send_thread_obj:
            audio_send_thread_obj.join(timeout=1.0)
            audio_send_thread_obj = None

def close_udp_session():
    """
    完全关闭UDP会话，停止所有相关线程，释放资源。
    """
    global udp_session_active, udp_voice_socket, nat_thread_obj, audio_recv_thread_obj
    with audio_state_lock:
        if not udp_session_active: return
        
    stop_audio_stream()

    with audio_state_lock:
        udp_session_active = False
        if udp_voice_socket:
            try:
                # 唤醒阻塞的 recvfrom
                udp_voice_socket.sendto(b"", ("127.0.0.1", udp_voice_socket.getsockname()[1]))
                udp_voice_socket.close()
            except: pass
            udp_voice_socket = None

        if nat_thread_obj:
            nat_thread_obj.join(timeout=1.0)
            nat_thread_obj = None

        if audio_recv_thread_obj:
            audio_recv_thread_obj.join(timeout=1.0)
            audio_recv_thread_obj = None

    # 清理全局状态，防止残留影响下次会话
    room_members.clear()

def start_realtime_audio(server_ip, server_port, username="", room_id=""):
    """
    为了向后兼容，作为包装函数调用新接口
    """
    init_udp_session(server_ip, server_port, username, room_id)
    start_audio_stream()

def stop_realtime_audio():
    """
    为了向后兼容，作为包装函数调用新接口
    """
    close_udp_session()



