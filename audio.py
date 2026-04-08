
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
        print(f"\n[系统] 播放音频失败: {e}")

# ================= 实时语音连麦功能模块 ================

# ==== 实时语音状态控制 ====
udp_session_active = False
audio_stream_active = False
udp_voice_socket = None
VOICE_RATE = 16000  # 实时语音优化采样率

audio_state_lock = threading.Lock()
p2p_thread_obj = None
audio_send_thread_obj = None
audio_recv_thread_obj = None

# 通话控制状态
udp_voice_mute = False   # 麦克风静音状态 (自己说话不发送)
udp_voice_pause = False  # 会话暂停状态 (不发声音，不播放声音)
pending_mute = False
pending_pause = False
room_members = {}        # {用户名: (ip, port)}
p2p_status = {}          # {用户名: {"active": bool, "last_seen": float, "addr": (ip, port)}}
last_server_ip = ""
last_server_port = 0

def set_mute(state):
    global udp_voice_mute, pending_mute, audio_stream_active
    with audio_state_lock:
        if not audio_stream_active:
            pending_mute = state
            return
        if udp_voice_mute != state:
            udp_voice_mute = state
            print(f"\r[音频系统] 麦克风状态 -> {'静音' if state else '开启'}")
            print("你> ", end="", flush=True)

def set_pause(state):
    global udp_voice_pause, pending_pause, audio_stream_active
    with audio_state_lock:
        if not audio_stream_active:
            pending_pause = state
            return
        if udp_voice_pause != state:
            udp_voice_pause = state
            print(f"\r[音频系统] 语音状态 -> {'暂停' if state else '开启'}")
            print("你> ", end="", flush=True)


def test_p2p_or_relay(username, target, use_p2p, message):
    """
    测试功能：发送纯文本 UDP 消息来验证 P2P 或中继
    """
    global udp_session_active, udp_voice_socket, p2p_status, last_server_ip, last_server_port
    if not udp_session_active or not udp_voice_socket:
        print("\n[系统] 未加入会议室或 UDP 通道未打通，无法测试。")
        return
        
    msg_bytes = message.encode("utf-8")
    
    if use_p2p:
        status = p2p_status.get(target)
        if status and status.get("active") and status.get("addr"):
            packet = f"P2P_TEXT {username} ".encode("utf-8") + msg_bytes
            try:
                udp_voice_socket.sendto(packet, status["addr"])
                print(f"\n[测试] 已尝试通过 P2P 隧道向 {target} 发送文本：{message}")
            except Exception as e:
                print(f"\n[测试] P2P 发送失败：{e}")
        else:
            print(f"\n[测试] 无法向 {target} 发送 P2P 测试，通道尚未打通 (或已超时)。")
    else:
        # 中继测试
        if not last_server_ip or not last_server_port:
            print("\n[测试] 缺少服务器 NAT 信息，无法使用中继发送。")
            return
            
        header = f"RELAY {target} RELAY_TEXT {username} ".encode("utf-8")
        packet = header + msg_bytes
        try:
            udp_voice_socket.sendto(packet, (last_server_ip, last_server_port))
            print(f"\n[测试] 已尝试通过服务器 RELAY 中转向 {target} 发送文本：{message}")
        except Exception as e:
            print(f"\n[测试] RELAY 发送失败：{e}")

# 全局 Pyaudio 对象，避免在多个线程中同时初始化导致 C 语言层面发生 Segfault 卡退
_pyaudio_instance = None
_pyaudio_lock = threading.Lock()

def get_pyaudio():
    global _pyaudio_instance
    with _pyaudio_lock:
        if _pyaudio_instance is None:
            _pyaudio_instance = pyaudio.PyAudio()
        return _pyaudio_instance

def p2p_maintenance_thread(udp_sock, username):
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
    global audio_stream_active, room_members, p2p_status
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
                # 可解除注释用于调试麦克风采样：
                print(f"\n[音频采样] 麦克风RMS值: {rms_val} ", end='')
                
                if rms_val > SILENCE_THRESHOLD:
                    now = time.time()
                    if now - last_send_print_time > 1.5:
                        print(f"\r[监控] 您正在说话... (麦克风音量/RMS: {rms_val:.0f})")
                        print("你> ", end="", flush=True)
                        last_send_print_time = now

                    if room_id:
                        # 房间模式：统一通过服务器 RELAY 中转
                        for target in list(room_members):
                            if target != username:
                                header = f"RELAY {target} ".encode("utf-8")
                                packet = header + data
                                udp_sock.sendto(packet, (server_ip, server_port))
                    else:
                        # 点对点原逻辑（如果有）直接发
                        udp_sock.sendto(data, (server_ip, server_port))
    except Exception as e:
        print(f"\\n[发送线程异常] {e}")
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
    global udp_session_active, audio_stream_active, p2p_status, udp_voice_pause
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
                # print(f"\\r[接收线程] 收到 {len(data)} bytes 数据，来自 {addr}", end='')
                if data == b"HOLE_PUNCH" or len(data) == 0:
                    pass

                elif data.startswith(b"P2P_HELLO "):
                    peer_name = data.split(b" ")[1].decode("utf-8")
                    ack_packet = f"P2P_HELLO_ACK {username}".encode("utf-8")
                    udp_sock.sendto(ack_packet, addr)
                    if peer_name not in p2p_status:
                        p2p_status[peer_name] = {'active': True, 'last_seen': time.time(), 'addr': addr}
                    else:
                        p2p_status[peer_name]['active'] = True
                        p2p_status[peer_name]['last_seen'] = time.time()
                        p2p_status[peer_name]['addr'] = addr

                elif data.startswith(b"P2P_HELLO_ACK "):
                    peer_name = data.split(b" ")[1].decode("utf-8")
                    if peer_name not in p2p_status:
                        p2p_status[peer_name] = {'active': True, 'last_seen': time.time(), 'addr': addr}
                    else:
                        p2p_status[peer_name]['active'] = True
                        p2p_status[peer_name]['last_seen'] = time.time()
                        p2p_status[peer_name]['addr'] = addr

                elif data.startswith(b"P2P_TEXT "):
                    parts = data.split(b" ", 2)
                    if len(parts) >= 3:
                        sender = parts[1].decode("utf-8")
                        msg = parts[2].decode("utf-8")
                        out = (
                            f"\n\n============= [UDP 测试通道: P2P 直连] ============="
                            f"\n[{time.strftime('%H:%M:%S')}] 目标 {sender} 发来的原始穿透数据:"
                            f"\n内容 -> {msg}"
                            f"\n====================================================\n你> "
                        )
                        print(out, end="", flush=True)

                elif data.startswith(b"RELAY_DATA "):
                    payload = data[11:]
                    if payload.startswith(b"RELAY_TEXT "):
                        parts = payload.split(b" ", 2)
                        if len(parts) >= 3:
                            sender = parts[1].decode("utf-8")
                            msg = parts[2].decode("utf-8")
                            out = (
                                f"\n\n============= [UDP 测试通道: RELAY 中继] ============="
                                f"\n[{time.strftime('%H:%M:%S')}] 经由服务器转发接收自 {sender} 的数据:"
                                f"\n内容 -> {msg}"
                                f"\n======================================================\n你> "
                            )
                            print(out, end="", flush=True)
                    else:
                        audio_data = payload
                        source_key = ("relay", addr)

                elif data.startswith(b"P2P_AUDIO "):
                    audio_data = data[10:]
                    source_key = ("p2p", addr)

                else:
                    audio_data = data
                    source_key = ("raw", addr)

                # 将音频数据放入混音缓冲区
                if audio_data and source_key and audio_stream_active and not udp_voice_pause:
                    if source_key not in mix_sources:
                        mix_sources[source_key] = []
                    mix_sources[source_key].append(audio_data)

                    # 间隔 1.5 秒打印一次谁正在说话，避免控制台被刷屏
                    now = time.time()
                    if source_key not in last_recv_print_time or (now - last_recv_print_time[source_key] > 1.5):
                        speaker_label = "对方/某人"
                        if isinstance(source_key, tuple) and len(source_key) == 2:
                            speaker_label = f"地址 {source_key[1]}"
                        print(f"\r[监控] 正在接收语音数据... ({speaker_label} 正在说话)")
                        print("你> ", end="", flush=True)
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
                        if stream is None:
                            stream = p.open(format=FORMAT, channels=CHANNELS,
                                            rate=VOICE_RATE, output=True,
                                            frames_per_buffer=CHUNK)
                        stream.write(mixed)
                    mix_sources.clear()

    except Exception:
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
    global last_username, last_room_id, p2p_thread_obj, audio_recv_thread_obj

    with audio_state_lock:
        if udp_session_active: return
        
        last_server_ip = server_ip
        last_server_port = server_port
        last_username = username
        last_room_id = room_id

        udp_session_active = True
        udp_voice_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        
        if username:
            # 多次发送 STUN_HELLO 确保 NAT 地址注册成功（UDP 不可靠）
            for _ in range(3):
                udp_voice_socket.sendto(f"STUN_HELLO {username}".encode("utf-8"), (server_ip, server_port))

        for _ in range(5):
            udp_voice_socket.sendto(b"HOLE_PUNCH", (server_ip, server_port))
            time.sleep(0.1)

        if room_id:
            p2p_thread_obj = threading.Thread(target=p2p_maintenance_thread, args=(udp_voice_socket, username), daemon=True)
            p2p_thread_obj.start()

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
    global udp_session_active, udp_voice_socket, p2p_thread_obj, audio_recv_thread_obj
    with audio_state_lock:
        if not udp_session_active: return
        
    stop_audio_stream()

    with audio_state_lock:
        udp_session_active = False
        if udp_voice_socket:
            try:
                # 唤醒P2P心跳线程
                udp_voice_socket.sendto(b"", ("127.0.0.1", udp_voice_socket.getsockname()[1]))
                udp_voice_socket.close()
            except: pass
            udp_voice_socket = None

        if p2p_thread_obj:
            p2p_thread_obj.join(timeout=1.0)
            p2p_thread_obj = None

        if audio_recv_thread_obj:
            audio_recv_thread_obj.join(timeout=1.0)
            audio_recv_thread_obj = None

    # 清理全局状态，防止残留影响下次会话
    room_members.clear()
    p2p_status.clear()

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



