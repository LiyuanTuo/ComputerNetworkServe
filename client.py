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
import base64
import json
from audio import * 
from audio_eval import start_evaluation, stop_evaluation, get_evaluation_output_paths
from common.ports import DEFAULT_SERVER_IP, SERVER_TCP_PORT
# ============ 配置 ============
# 对于音频传输，普通的 4096 缓冲区不够大，改为 1MB
BUFFER_SIZE = 1024 * 1024 
ENCODING = "utf-8"

# ==== 实时语音状态控制 ====
current_pending_port = None

# ==== 联系人在线状态 ====
contact_status: dict[str, str] = {}  # {联系人用户名: "online"/"offline"}
client_username = ""
current_room_id = ""


def _print_eval_tick(row):
    """每秒输出一次评测结果（由 audio_eval 后台线程回调）。"""
    print(
        f"[评测 {row['timestamp']}] "
        f"丢包率={row['loss_rate'] * 100:.2f}% "
        f"时延={row['avg_delay_ms']:.2f}ms "
        f"抖动={row['avg_jitter_ms']:.2f}ms "
        f"乱序={row['reorder_rate'] * 100:.2f}% "
        f"总分={row['score']}/100"
    )

def receive_messages(sock: socket.socket, stop_event: threading.Event, server_ip: str):
    """
    接收线程：持续从服务器接收消息并处理，包含解析 Base64 音频
    """
    global current_pending_port, current_room_id
    while not stop_event.is_set():
        try:
            data = sock.recv(BUFFER_SIZE)
            if not data:
                print("\n[系统] 与服务器的连接已断开")
                stop_event.set()
                break
            
            message = data.decode(ENCODING)
            
            # TCP 是流协议，一次 recv() 可能收到多条消息粘在一起
            # 按换行符分割后逐行处理，避免 startswith() 只匹配第一条消息
            lines = message.split("\n")
            need_prompt = False  # 是否需要在最后重新显示输入提示符

            for line in lines:
                if not line.strip():
                    continue

                # --- 处理联系人在线状态推送 ---
                if line.startswith("/CONTACT_STATUS "):
                    parts = line.strip().split(" ")
                    if len(parts) >= 3:
                        contact_name = parts[1]
                        status = parts[2]
                        if status == "removed":
                            contact_status.pop(contact_name, None)
                        else:
                            old_status = contact_status.get(contact_name)
                            contact_status[contact_name] = status
                            # 只在状态变化时提示用户
                            if old_status and old_status != status:
                                hint = "上线了" if status == "online" else "离线了"
                                print(f"\r[通讯录] 联系人 '{contact_name}' {hint}")
                                need_prompt = True
                    continue

                # --- 处理实时语音呼叫信令 ---
                if line.startswith("/CALL_REQUEST "):
                    parts = line.split(" ")
                    caller = parts[1]
                    r_port = parts[2].strip()
                    current_pending_port = int(r_port)
                    
                    print(f"\n\n[系统提示] >>> 用户 '{caller}' 向你发起实时语音通话！ <<<")
                    print(f"请输入 /accept {caller} 接受，或输入 /reject {caller} 拒绝。")
                    need_prompt = True
                    continue
                    
                elif line.startswith("/CALL_REPLY_FAIL "):
                    parts = line.split(" ")
                    target = parts[1]
                    reason = parts[2].strip()
                    reasons = {"1": "不在线", "2": "正在通话中", "3": "拒绝了您的请求"}
                    print(f"\n[系统] 呼叫 '{target}' 失败：{reasons.get(reason, '未知错误')}")
                    need_prompt = True
                    continue
                    
                elif line.startswith("/CALL_REPLY_OK "):
                    parts = line.split(" ")
                    target = parts[1]
                    server_udp_port = int(parts[2].strip())
                    # print(f"\n[系统] '{target}' 已接受呼叫！底UDP 语音通道打通中...")
                    need_prompt = True
                    
                    # 启动底层双向UDP音频收发线程与服务器进行打洞并传输音频
                    init_udp_session(server_ip, server_udp_port, client_username, "")
                    start_audio_stream()
                    continue

                # --- 处理会议室相关信令 ---
                elif line.startswith("/ROOM_CREATED "):
                    parts = line.split(" ")
                    room_id = parts[1]
                    server_udp_port = int(parts[2].strip())
                    current_room_id = room_id
                    # print(f"\r[系统] 会议室 {room_id} 内 UDP 中继通道建立中... (默认静音)")
                    need_prompt = True
                    init_udp_session(server_ip, server_udp_port, client_username, room_id)
                    set_mute(True)
                    start_audio_stream()  # 必须启动流才能收到别人的声音
                    continue

                elif line.startswith("/ROOM_JOINED "):
                    parts = line.split(" ")
                    room_id = parts[1]
                    server_udp_port = int(parts[2].strip())
                    current_room_id = room_id
                    # print(f"\r[系统] 成功打通中继信令交互，房间 {room_id} 语音通道建立中... (默认静音)")
                    need_prompt = True
                    init_udp_session(server_ip, server_udp_port, client_username, room_id)
                    set_mute(True)
                    start_audio_stream()  # 同上，确保接收线程能正常运行并播放
                    continue

                elif line.startswith("/ROOM_MEMBERS "):
                    parts = line.split(" ", 2)
                    if len(parts) >= 3:
                        room_id = parts[1]
                        try:
                            members_list = json.loads(parts[2])
                            member_names = [m["name"] for m in members_list]
                            
                            # 原子更新全局 member dict：先加新成员再删旧成员，避免 clear() 导致发送线程读到空字典
                            new_members = {m["name"]: (m["ip"], m["port"]) for m in members_list}
                            room_members.update(new_members)
                            for old_key in list(room_members.keys()):
                                if old_key not in new_members:
                                    room_members.pop(old_key, None)
                            
                            # print(f"\r[会议室 {room_id}] 当前成员: {', '.join(member_names)}")
                        except Exception:
                            pass
                    need_prompt = True
                    continue
                
                # --- 核心：协议解析 ---
                # 判断接收的字符串里有没有我们定义的音频标头 `AUDIO:`
                if "AUDIO:" in line:
                    prefix, b64_audio = line.split("AUDIO:", 1)
                    print(f"\r{prefix} 发送了一段语音消息，正在播放...")
                    
                    wav_bytes = base64.b64decode(b64_audio)
                    
                    recv_file = "recv_voice.wav"
                    with open(recv_file, "wb") as f:
                        f.write(wav_bytes)
                    
                    play_audio(recv_file)
                    need_prompt = True
                    
                else:
                    # 不是语音，那就当做普通文字打印
                    if ("通话已被" in line and "终止" in line) or "您已成功终止实时语音通话" in line:
                        close_udp_session()
                        current_room_id = ""
                    print(f"\r{line}")
                    need_prompt = True
            
            if need_prompt:
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
    print(get_audio_backend_notice())

    global client_username

    # ---- 获取连接信息 ----
    # server_ip = "DESKTOP-4AFQ0JR" # 使用我的计算机名来作为服务器 就不用担心局域网内 IP 地址变化了 你们要改成你们自己的hostname 或者直接输入局域网 IP 地址
    # server_ip = "10.198.51.210" #蒋利伟主机名"desktop_m2mi6se8"
    server_ip = DEFAULT_SERVER_IP
    port = SERVER_TCP_PORT

    # 尝试将主机名解析为 IP 地址（支持 hostname 和 IP 两种输入）
    try:
        server_ip = socket.gethostbyname(server_ip)
    except socket.gaierror:
        print(f"[警告] 无法解析主机名 '{server_ip}'，将直接尝试连接...")

    username = input("请输入你的用户名: ").strip()
    if not username:
        username = "匿名用户"
    client_username = username

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
        args=(client_sock, stop_event, server_ip),
        daemon=True
    )
    recv_thread.start()

    # ---- 主线程：发送消息 ----
    global current_room_id
    print("提示: 输入文字回车发送\n /voice 语音留言 \n /call @用户 实时语音 \n /contacts 管理通讯录 \n /status 查看联系人状态 \n /help 查看完整帮助 \n /quit 退出")
    print(" /ROOM_CREATE 创建会议 \n /ROOM_JOIN <房间号> 加入会议 \n /ROOM_QUIT <房间号> 退出并挂断语音")
    print(" /open_voice 开启语音传输 \n /close_voice 停止语音传输（静音）\n")
    try:
        while not stop_event.is_set():
            print("你> ", end="", flush=True)
            msg = input()
            if not msg: # 如果用户直接按回车，输入为空字符串，就继续下一轮循环，等待有效输入
                continue

            # ---- 本地查看联系人在线状态 ----
            if msg.lower() == "/status":
                if not contact_status:
                    print("[通讯录] 暂无联系人状态信息，请先 /contacts add <用户名>")
                else:
                    print(f"[通讯录] 联系人状态 ({len(contact_status)} 人):")
                    for name, status in contact_status.items():
                        hint = "在线" if status == "online" else "离线"
                        print(f"  - {name} [{hint}]")
                continue

            # ---- 帮助菜单 ----
            elif msg.lower() == "/help":
                print("\n" + "=" * 50)
                print("  LinkVoice 命令帮助")
                print("=" * 50)
                print("\n【聊天】")
                print("  直接输入文字        → 广播给联系人")
                print("  @用户名 消息        → 私聊消息")
                print("\n【语音留言】")
                print("  /voice              → 录制并广播语音")
                print("  @用户名 /voice      → 录制并私发语音")
                print("\n【实时通话】")
                print("  /call @用户名       → 发起实时语音通话")
                print("  /accept 用户名      → 接听来电")
                print("  /reject 用户名      → 拒绝来电")
                print("  /realtime -quit     → 挂断当前通话")
                print("\n【通讯录】")
                print("  /contacts           → 查看通讯录")
                print("  /contacts add 用户  → 添加联系人（双向）")
                print("  /contacts del 用户  → 删除联系人（双向）")
                print("  /contacts search 词 → 搜索联系人")
                print("  /status             → 查看联系人在线状态")
                print("\n【会议室】")
                print("  /ROOM_CREATE        → 创建会议室")
                print("  /ROOM_JOIN 房间号   → 加入会议室")
                print("  /ROOM_QUIT 房间号   → 退出会议室")
                print("  /open_voice         → 会议中开麦")
                print("  /close_voice        → 会议中静音")
                print("\n【其他】")
                print("  /online             → 查看所有在线用户")
                print("  /quit               → 退出客户端")
                print("=" * 50 + "\n")
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

            # ---- 处理呼叫答复 ----
            if msg.startswith("/accept "):
                caller = msg.split(" ")[1]
                # 防止在已有音频会话时接受新呼叫
                if udp_session_active:
                    print(f"[系统] 您当前正在通话或会议中，请先退出后再接听。")
                    client_sock.sendall(f"/CALL_REJECT {caller}".encode(ENCODING))
                    continue
                # 发送同意指令通过 TCP 提给服务器
                client_sock.sendall(f"/CALL_ACCEPT {caller}".encode(ENCODING))
                print(f"[系统] 已同意 {caller} 的接入，正在建立底层 UDP 通讯...")
                # 启动底层双向UDP音频收发线程与服务器进行打洞并传输音频
                if current_pending_port is not None:
                    init_udp_session(server_ip, current_pending_port, client_username, "")
                    start_audio_stream()
                continue
            
            elif msg.startswith("/reject "):
                caller = msg.split(" ")[1]
                client_sock.sendall(f"/CALL_REJECT {caller}".encode(ENCODING))
                print(f"[系统] 已拒绝 {caller} 的呼叫。")
                continue

            # ---- 处理会议室控制指令 ----
            elif msg.lower().startswith("/room_create"):
                if current_room_id:
                    print(f"[系统] 您已在会议室 {current_room_id} 中，一次只能加入一个会议室。请先 /ROOM_QUIT")
                    continue
                if udp_session_active:
                    print("[系统] 您当前正在通话中，请先结束通话再创建会议室。")
                    continue
                client_sock.sendall("/ROOM_CREATE".encode(ENCODING))
                continue
                
            elif msg.lower().startswith("/room_join"):
                if current_room_id:
                    print(f"[系统] 您已在会议室 {current_room_id} 中，一次只能加入一个会议室。请先 /ROOM_QUIT")
                    continue
                if udp_session_active:
                    print("[系统] 您当前正在通话中，请先结束通话再加入会议室。")
                    continue
                parts = msg.split()
                if len(parts) >= 2:
                    client_sock.sendall(f"/ROOM_JOIN {parts[1]}".encode(ENCODING))
                else:
                    print("[系统] 格式错误，请使用：/ROOM_JOIN <房间号>")
                continue
                
            elif msg.lower().startswith("/room_quit"):
                parts = msg.split()
                if len(parts) >= 2:
                    quit_room_id = parts[1]
                else:
                    quit_room_id = current_room_id  # 未指定时使用当前房间
                current_room_id = "" # 清空当前所在房间 ID
                # 先通知服务器再关闭本地资源
                if quit_room_id:
                    client_sock.sendall(f"/ROOM_QUIT {quit_room_id}".encode(ENCODING))
                else:
                    client_sock.sendall("/ROOM_QUIT".encode(ENCODING))
                close_udp_session()  # 关闭本地 UDP 语音（内部包含 stop_audio_stream）
                continue

            elif msg.lower().startswith("/open_voice"):
                if not current_room_id:
                    print("[系统] 您当前不在会议室中，无法开启语音。")
                    continue
                set_mute(False)  # 仅需解除静音，底层发送线程已经运行，只需放行数据
                client_sock.sendall(msg.encode(ENCODING))
                continue

            elif msg.lower().startswith("/close_voice"):
                if not current_room_id:
                    print("[系统] 您当前不在会议室中。")
                    continue
                set_mute(True)  # 仅需开启静音，保留接收线程和扬声器工作
                client_sock.sendall(msg.encode(ENCODING))
                continue

            elif msg.lower() == "/eval_start":
                report_path, csv_path = start_evaluation(
                    interval_sec=1.0,
                    tick_callback=_print_eval_tick,
                )
                print(f"[系统] 网络音频质量测评已开始：每秒反馈一次，实时数据写入 {csv_path}")
                print(f"[系统] 汇总报告将保存到 {report_path}")
                continue

            elif msg.lower() == "/eval_stop":
                report = stop_evaluation()
                if report:
                    report_path, csv_path = get_evaluation_output_paths()
                    print(f"[系统] 测评已结束：汇总报告已保存至 {report_path}")
                    print(f"[系统] 实时明细已保存至 {csv_path}")
                    print(report)
                else:
                    print("[系统] 测评未在进行中")
                continue

            elif msg.lower() == "/realtime -quit":
                # 如果在会议室中，需要通知服务器退出房间
                if current_room_id:
                    client_sock.sendall(f"/ROOM_QUIT {current_room_id}".encode(ENCODING))
                
                # 无论是实时一对一挂断还是退出房间，清理底层连接及音频线程
                stop_audio_stream()   # 停止发送和接收线程 (关闭麦克风、听筒)
                close_udp_session()   # 销毁 UDP 套接字释放端口
                current_room_id = ""
                # 会交给服务器去广播结束消息

            # 普通文本消息，直接发
            client_sock.sendall(msg.encode(ENCODING))

            if msg.lower() == "/quit":
                # 退出前清理所有音频资源（无论是会议室还是1对1通话）
                close_udp_session()
                current_room_id = ""
                print("[系统] 正在退出...")
                break

    except (KeyboardInterrupt, EOFError):
        print("\n[系统] 正在退出...")
    finally:
        # 确保音频资源被释放（处理异常断开的情况）
        close_udp_session()
        current_room_id = ""
        stop_event.set()
        client_sock.close()
        print("[系统] 已断开连接")


if __name__ == "__main__":
    start_client()
