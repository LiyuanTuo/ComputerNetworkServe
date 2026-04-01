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
import uuid

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
pending_calls: dict = {}                 # 呼叫中状态 {呼叫者: {"target": 被叫者, "sock": udp_sock, "port": udp_port}}
pending_calls_lock = threading.Lock()

# ============ 房间 (多播/中继) 状态 ============
rooms: dict = {}                         # {room_id: {"members": {username: (ip, port)}, "relay_sock": socket, "port": int}}
rooms_lock = threading.Lock()

# ============ 联系人电话本 ============
CONTACTS_FILE = "contacts.json"
contacts: dict[str, list[str]] = {}      # {用户名: [联系人用户名列表]}
contacts_lock = threading.Lock()         # 线程锁，保护 contacts 字典


def load_contacts():
    """从 JSON 文件加载联系人数据"""
    global contacts
    try:
        with open(CONTACTS_FILE, "r", encoding=ENCODING) as f:
            data = json.load(f)
            if isinstance(data, dict):
                contacts = data
    except (FileNotFoundError, json.JSONDecodeError):
        contacts = {}


def save_contacts():
    """将联系人数据持久化到 JSON 文件"""
    with contacts_lock:
        try:
            with open(CONTACTS_FILE, "w", encoding=ENCODING) as f:
                json.dump(contacts, f, ensure_ascii=False, indent=4)
        except Exception as e:
            print(f"[错误] 保存联系人失败: {e}")


def is_mutual_contact(user1: str, user2: str) -> bool:
    """检查两个用户是否互为联系人"""
    with contacts_lock:
        return user2 in contacts.get(user1, []) and user1 in contacts.get(user2, [])


def is_user_online(username: str) -> bool:
    """检查用户是否在线"""
    with clients_lock:
        return username in clients.values()


def get_user_sock(username: str):
    """通过用户名查找 socket"""
    with clients_lock:
        for sock, name in clients.items():
            if name == username:
                return sock
    return None


def notify_contact_status(username: str, online: bool):
    """
    当用户上线/下线时，通知所有将其添加为联系人的在线用户
    消息格式：/CONTACT_STATUS 用户名 online/offline
    """
    status = "online" if online else "offline"
    msg = f"/CONTACT_STATUS {username} {status}\n".encode(ENCODING)
    with contacts_lock:
        # 找出所有将 username 加为联系人的用户
        interested_users = [u for u, cl in contacts.items() if username in cl]
    with clients_lock:
        for sock, name in clients.items():
            if name in interested_users:
                try:
                    sock.sendall(msg)
                except Exception:
                    pass


def send_initial_contact_status(username: str, client_sock: socket.socket):
    """
    用户刚上线时，向其推送所有联系人的当前在线状态
    """
    with contacts_lock:
        user_contacts = contacts.get(username, [])
    if not user_contacts:
        return
    with clients_lock:
        online_users = set(clients.values())
    for contact_name in user_contacts:
        status = "online" if contact_name in online_users else "offline"
        try:
            client_sock.sendall(f"/CONTACT_STATUS {contact_name} {status}\n".encode(ENCODING))
        except Exception:
            break


def handle_contacts_command(username: str, text: str, client_sock: socket.socket):
    """
    处理联系人 CRUD 命令

    命令格式：
      /contacts                  - 查看自己的所有联系人及在线状态
      /contacts add <用户名>     - 添加联系人（双向）
      /contacts del <用户名>     - 删除联系人（双向）
      /contacts search <关键字>  - 按名字搜索联系人
    """
    parts = text.split()

    if len(parts) == 1:
        # /contacts → 查看所有联系人及在线状态
        with contacts_lock:
            user_contacts = contacts.get(username, [])
        if not user_contacts:
            resp = f"[{timestamp()}] [通讯录] 您的通讯录为空，使用 /contacts add <用户名> 添加联系人\n"
        else:
            with clients_lock:
                online_users = set(clients.values())
            lines = [f"[{timestamp()}] [通讯录] 您的通讯录 ({len(user_contacts)} 人):"]
            for i, name in enumerate(user_contacts, 1):
                status = "在线" if name in online_users else "离线"
                lines.append(f"  {i}. {name} [{status}]")
            resp = "\n".join(lines) + "\n"
        client_sock.sendall(resp.encode(ENCODING))
        return True

    action = parts[1].lower()

    if action == "add":
        if len(parts) < 3:
            client_sock.sendall(f"[{timestamp()}] [通讯录] 格式：/contacts add <用户名>\n".encode(ENCODING))
            return True
        target = parts[2]
        if target == username:
            client_sock.sendall(f"[{timestamp()}] [通讯录] 不能添加自己为联系人\n".encode(ENCODING))
            return True
        with contacts_lock:
            # 初始化双方列表
            if username not in contacts:
                contacts[username] = []
            if target not in contacts:
                contacts[target] = []
            # 检查是否已存在
            if target in contacts[username]:
                client_sock.sendall(f"[{timestamp()}] [通讯录] '{target}' 已在您的通讯录中\n".encode(ENCODING))
                return True
            # 双向添加
            contacts[username].append(target)
            if username not in contacts[target]:
                contacts[target].append(username)
        save_contacts()
        client_sock.sendall(f"[{timestamp()}] [通讯录] 已添加联系人：{target}\n".encode(ENCODING))
        # 通知对方（如果在线）
        target_sock = get_user_sock(target)
        if target_sock:
            try:
                target_sock.sendall(f"[{timestamp()}] [通讯录] '{username}' 将您添加为联系人\n".encode(ENCODING))
                # 推送 username 的在线状态给 target
                target_sock.sendall(f"/CONTACT_STATUS {username} online\n".encode(ENCODING))
            except Exception:
                pass
            # 推送 target 的在线状态给 username
            try:
                client_sock.sendall(f"/CONTACT_STATUS {target} online\n".encode(ENCODING))
            except Exception:
                pass
        else:
            try:
                client_sock.sendall(f"/CONTACT_STATUS {target} offline\n".encode(ENCODING))
            except Exception:
                pass
        return True

    elif action == "del":
        if len(parts) < 3:
            client_sock.sendall(f"[{timestamp()}] [通讯录] 格式：/contacts del <用户名>\n".encode(ENCODING))
            return True
        target = parts[2]
        removed = False
        with contacts_lock:
            # 双向删除
            if username in contacts and target in contacts[username]:
                contacts[username].remove(target)
                removed = True
            if target in contacts and username in contacts[target]:
                contacts[target].remove(username)
        if removed:
            save_contacts()
            client_sock.sendall(f"[{timestamp()}] [通讯录] 已删除联系人：{target}\n".encode(ENCODING))
            # 通知自己清除本地状态缓存
            client_sock.sendall(f"/CONTACT_STATUS {target} removed\n".encode(ENCODING))
            # 通知对方（如果在线）
            target_sock = get_user_sock(target)
            if target_sock:
                try:
                    target_sock.sendall(f"[{timestamp()}] [通讯录] '{username}' 将您从联系人中移除\n".encode(ENCODING))
                    target_sock.sendall(f"/CONTACT_STATUS {username} removed\n".encode(ENCODING))
                except Exception:
                    pass
        else:
            client_sock.sendall(f"[{timestamp()}] [通讯录] 未找到联系人：{target}\n".encode(ENCODING))
        return True

    elif action == "search":
        if len(parts) < 3:
            client_sock.sendall(f"[{timestamp()}] [通讯录] 格式：/contacts search <关键字>\n".encode(ENCODING))
            return True
        keyword = parts[2]
        with contacts_lock:
            user_contacts = contacts.get(username, [])
            results = [name for name in user_contacts if keyword in name]
        if not results:
            resp = f"[{timestamp()}] [通讯录] 未找到包含 '{keyword}' 的联系人\n"
        else:
            with clients_lock:
                online_users = set(clients.values())
            lines = [f"[{timestamp()}] [通讯录] 搜索结果 ({len(results)} 人):"]
            for i, name in enumerate(results, 1):
                status = "在线" if name in online_users else "离线"
                lines.append(f"  {i}. {name} [{status}]")
            resp = "\n".join(lines) + "\n"
        client_sock.sendall(resp.encode(ENCODING))
        return True

    else:
        client_sock.sendall(f"[{timestamp()}] [通讯录] 未知操作 '{action}'，可用：add / del / search\n".encode(ENCODING))
        return True


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
        
    # 如果用户离线，将其从所有的语音房间中剔除
    if username:
        impacted_rooms = []
        with rooms_lock:
            empty_rooms = []
            for room_id, room in list(rooms.items()):
                if username in room["members"]:
                    del room["members"][username]
                    impacted_rooms.append(room_id)
                if not room["members"]:
                    empty_rooms.append(room_id)
            for room_id in empty_rooms:
                try: rooms[room_id]["relay_sock"].close()
                except: pass
                del rooms[room_id]
                if room_id in impacted_rooms:
                    impacted_rooms.remove(room_id)
                    
        for room_id in impacted_rooms:
            broadcast_room_members(room_id)
        
        # 如果用户掉线，清理那些还没接通的呼叫状态
        with pending_calls_lock:
            # 他是呼叫者
            if username in pending_calls:
                info = pending_calls.pop(username)
                try: info["sock"].close()
                except: pass
            
            # 他是被叫者
            to_remove_caller = None
            for c_name, info in pending_calls.items():
                if info["target"] == username:
                    to_remove_caller = c_name
                    try: info["sock"].close()
                    except: pass
                    break
            if to_remove_caller:
                del pending_calls[to_remove_caller]
        
        # 并帮他清理还在进行的实时语音
        end_realtime_voice(username)

    return username


def inform_tcp_fallback(user1: str, user2: str):
    fallback_msg = f"[{timestamp()}] [系统] UDP 探测超时/被阻断，正自动降级为 TCP 语音中继模式！\n"
    with clients_lock:
        # 通过用户名找到对应的两个线程
        sock1 = None
        for s, u in clients.items():
            if u == user1:
                sock1 = s
                break
        sock2 = None

        for s, u in clients.items():
            if u == user2:
                sock2 = s
                break
   
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
    能否利用user_log文件中存储的IP信息？
    """
    addr1 = None
    addr2 = None
    fallback_triggered = False
    
    # 增加心跳探测机制：开头 7 秒钟内如果没有收到双方的 UDP 包，则认为被阻断
    udp_sock.settimeout(7.0) 
    
    try:
        while True:
            try:
                data, addr = udp_sock.recvfrom(4096)
            except socket.timeout:
                # 触发了 7 秒超时，且双方/单方连不上，则启动 TCP 降级
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


def handle_call_request(caller_name: str, target_name: str, caller_sock: socket.socket):
    """
    处理实时语音呼叫请求（改进版）
    不直接接通，而是分配 UDP 端口并通过 TCP 向被呼叫者发请求
    """
    if caller_name == target_name:
        caller_sock.sendall(f"[{timestamp()}] [系统] 不能和自己建立语音。\n".encode(ENCODING))
        return

    # 检查是否互为联系人
    if not is_mutual_contact(caller_name, target_name):
        caller_sock.sendall(f"[{timestamp()}] [系统] '{target_name}' 不是您的联系人，请先 /contacts add {target_name}\n".encode(ENCODING))
        return

    with active_calls_lock:
        if caller_name in active_calls:
            caller_sock.sendall(f"[{timestamp()}] [系统] 您当前正在通话中，请先结束当前通话 (/RealTime -quit)。\n".encode(ENCODING))
            return
        if target_name in active_calls: # 您呼叫的用户正忙，请稍后再拨
            caller_sock.sendall(f"/CALL_REPLY_FAIL {target_name} 2\n".encode(ENCODING))
            return
            
    with pending_calls_lock:
        if caller_name in pending_calls:
            caller_sock.sendall(f"[{timestamp()}] [系统] 您已经在呼叫中，请等待上一呼叫结束或被拒绝。\n".encode(ENCODING))
            return

    target_sock = None
    with clients_lock:
        for sock, name in clients.items():
            if name == target_name:
                target_sock = sock
                break
                
    if not target_sock: #您呼叫的用户已关机，请稍后再拨
        caller_sock.sendall(f"/CALL_REPLY_FAIL {target_name} 1\n".encode(ENCODING))
        return

    # 创建一条专用的 UDP 隧道中继
    relay_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    relay_sock.bind((HOST, 0)) # 0表示服务器随机分配一个端口供udp链接
    relay_port = relay_sock.getsockname()[1]

    # 将其登入到“等待接听(pending)”列表中
    with pending_calls_lock:
        pending_calls[caller_name] = {
            "target": target_name,
            "sock": relay_sock, #给target_name和caller_name的中转线程
            "port": relay_port #给target_name和caller_name的中转端口
        }

    # 通过 TCP 告诉目标：有人呼叫，你可以往这个 UDP 端口打洞
    # 消息格式：\CALL_REQUEST caller_name relay_port
    try:
        target_sock.sendall(f"/CALL_REQUEST {caller_name} {relay_port}\n".encode(ENCODING))
    except Exception:
        pass
        
    try:
        caller_sock.sendall(f"[{timestamp()}] [系统] 已经向 '{target_name}' 发起语音呼叫，等待对方接听...\n".encode(ENCODING))
    except Exception: 
        pass


def handle_call_reply(target_name: str, caller_name: str, is_accept: bool, target_sock: socket.socket):
    """
    处理被呼叫者的答复 (同意/拒绝)
    """
    with pending_calls_lock:
        if caller_name not in pending_calls:
            return # 可能呼叫已经超时，或已被撤销
        call_info = pending_calls.pop(caller_name) #取出呼叫信息
    
    relay_sock = call_info["sock"]
    relay_port = call_info["port"]
    
    caller_sock = None
    # 找寻 caller 的TCP 链接，发送文本信息
    with clients_lock:
        for sock, name in clients.items():
            if name == caller_name:
                caller_sock = sock
                break

    if not is_accept:
        # 目标拒绝
        relay_sock.close()
        if caller_sock: #对方拒绝你的语音邀请
            try: caller_sock.sendall(f"/CALL_REPLY_FAIL {target_name} 3\n".encode(ENCODING))
            except: pass
        if target_sock:
             try: target_sock.sendall(f"[{timestamp()}] [系统] 已拒绝 '{caller_name}' 的语音邀请。\n".encode(ENCODING))
             except: pass
        return

    # 目标同意：建立正式的双向映射，并启动中继线程打通
    with active_calls_lock:
        active_calls[caller_name] = (target_name, relay_sock)
        active_calls[target_name] = (caller_name, relay_sock)

    t = threading.Thread(
        target=udp_voice_relay,
        args=(relay_sock, caller_name, target_name),
        daemon=True
    )
    t.start()

    # 告诉主叫方：目标已同意接入请求，并且告诉它 UDP 中继端口是多少
    if caller_sock: 
        try: caller_sock.sendall(f"/CALL_REPLY_OK {target_name} {relay_port}\n".encode(ENCODING))
        except: pass
    if target_sock:
        try: target_sock.sendall(f"[{timestamp()}] [系统] 语音已接通，正在建立后台安全通道...\n".encode(ENCODING))
        except: pass


def broadcast_room_members(room_id: str):
    """
    通过 TCP 广播房间内的所有成员的 NAT 坐标（用户名, IP, 端口）
    给该房间内所有已经成功打洞/已发握手包的成员。
    消息格式: /ROOM_MEMBERS [{"name": "A", "ip": "1.1.1.1", "port": 1234}, ...]
    """
    with rooms_lock:
        if room_id not in rooms:
            return
        
        room = rooms[room_id]
        member_list = [{"name": name, "ip": addr[0], "port": addr[1]} for name, addr in room["members"].items()]
        msg = f"/ROOM_MEMBERS {room_id} {json.dumps(member_list)}\n".encode(ENCODING)
        
        for name in room["members"]:
            sock = get_user_sock(name)
            if sock:
                try: sock.sendall(msg)
                except: pass


def room_udp_worker(room_id: str):
    """
    房间专用的 UDP 端口，用于 STUN (获取外网坐标) 和 处理降级中继
    """
    with rooms_lock:
        if room_id not in rooms: return
        relay_sock = rooms[room_id]["relay_sock"]

    while True:
        try:
            data, addr = relay_sock.recvfrom(BUFFER_SIZE)
            if not data: continue
            
            # STUN 握手包 (纯文本): STUN_HELLO username
            if data.startswith(b"STUN_HELLO "):
                parts = data.decode(ENCODING).strip().split(" ", 1)
                if len(parts) == 2:
                    username = parts[1]
                    with rooms_lock:
                        if room_id in rooms:
                            rooms[room_id]["members"][username] = addr
                    # 广播更新全房间的 NAT 信息
                    broadcast_room_members(room_id)
                continue
                
            # 中断/降级中继包 (带有 target_name 的二进制音频)
            # 格式约定: b"RELAY target_name " + payload
            if data.startswith(b"RELAY "):
                # 分割前两段：b"RELAY", b"target_name"
                parts = data.split(b' ', 2)
                if len(parts) >= 3:
                    target_name = parts[1].decode(ENCODING)
                    payload = parts[2]
                    
                    with rooms_lock:
                        if room_id in rooms and target_name in rooms[room_id]["members"]:
                            target_addr = rooms[room_id]["members"][target_name]
                            try:
                                relay_sock.sendto(b"RELAY_DATA " + payload, target_addr)
                            except: pass

        except Exception:
            break


def handle_room_create(username: str, client_sock: socket.socket):
    """创建新会议室并返回房间号及 UDP 端口"""
    room_id = str(uuid.uuid4())[:6].upper()
    relay_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    relay_sock.bind((HOST, 0))
    relay_port = relay_sock.getsockname()[1]

    with rooms_lock:
        rooms[room_id] = {
            "members": {},     # {username: (ip, port)}
            "relay_sock": relay_sock,
            "port": relay_port
        }

    # 启动该房间专设的 UDP STUN/中继 线程
    threading.Thread(target=room_udp_worker, args=(room_id,), daemon=True).start()

    client_sock.sendall(f"[{timestamp()}] [系统] 您已创建会议室 {room_id}，正在连接信令服务器...\n".encode(ENCODING))
    client_sock.sendall(f"/ROOM_CREATED {room_id} {relay_port}\n".encode(ENCODING))


def handle_room_join(username: str, room_id: str, client_sock: socket.socket):
    """加入指定会议室"""
    with rooms_lock:
        if room_id not in rooms:
            client_sock.sendall(f"[{timestamp()}] [系统] 会议室 {room_id} 不存在或已解散\n".encode(ENCODING))
            return
        relay_port = rooms[room_id]["port"]
        
    client_sock.sendall(f"[{timestamp()}] [系统] 成功进入会议室 {room_id}，正在进行 P2P 穿透握手...\n".encode(ENCODING))
    client_sock.sendall(f"/ROOM_JOINED {room_id} {relay_port}\n".encode(ENCODING))


def handle_room_quit(username: str, room_id: str, client_sock: socket.socket):
    """退出指定会议室"""
    with rooms_lock:
        if room_id in rooms:
            if username in rooms[room_id]["members"]:
                del rooms[room_id]["members"][username]
                
            # 若房间空了，释放 UDP socket 资源并清理
            if not rooms[room_id]["members"]:
                try: rooms[room_id]["relay_sock"].close()
                except: pass
                del rooms[room_id]
                return
                
    broadcast_room_members(room_id)
    client_sock.sendall(f"[{timestamp()}] [系统] 您已离开会议室 {room_id}\n".encode(ENCODING))


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

        # 通知该用户的联系人：他上线了
        notify_contact_status(username, online=True)

        # 给新用户发送欢迎消息和在线列表
        with clients_lock:
            online = ", ".join(clients.values())
        welcome = f"[{timestamp()}] 欢迎 {username}！当前在线用户: {online}\n"
        client_sock.sendall(welcome.encode(ENCODING))

        # 推送该用户所有联系人的当前在线状态
        send_initial_contact_status(username, client_sock)

        # ---- 第二步：循环接收并广播消息 ----
        while True:
            data = client_sock.recv(BUFFER_SIZE) # 这里也是阻塞的，recv() 会等待客户端发送消息或者断开连接。如果客户端断开连接，recv() 会返回空数据，这时服务器线程会跳出循环进行清理。
            if not data:
                break  # 客户端断开连接

            text = data.decode(ENCODING).strip() # decode 是从字节转换成字符串
            if not text:
                continue

            # 处理特殊命令
            if text.lower().startswith("/contacts"):
                handle_contacts_command(username, text, client_sock)
            elif text.lower() == "/online":
                with clients_lock:
                    online = ", ".join(clients.values())
                client_sock.sendall(
                    f"[{timestamp()}] 在线用户: {online}\n".encode(ENCODING)
                )
            elif text.lower() == "/quit":
                break
            elif text.lower() == "/realtime -quit" or text.lower() == "/realttime -quit":
                # 主动结束当前的实时语音
                end_realtime_voice(username, initiator=username)
            elif text.lower().startswith("/call @") or text.lower().startswith("/realttime @") or text.lower().startswith("/realtime @"):
                # 处理实时点对点语音请求
                parts = text.split('@', 1)
                if len(parts) == 2:
                    target_name = parts[1].strip()
                    handle_call_request(username, target_name, client_sock)
            elif text.startswith("/CALL_ACCEPT "): #接收方同意语音请求
                parts = text.split(' ', 1)
                caller_name = parts[1].strip()
                handle_call_reply(username, caller_name, True, client_sock)
            elif text.startswith("/CALL_REJECT "): #接收方拒绝语音请求
                parts = text.split(' ', 1)
                caller_name = parts[1].strip()
                handle_call_reply(username, caller_name, False, client_sock)
            elif text.startswith("/ROOM_CREATE"):
                handle_room_create(username, client_sock)
            elif text.startswith("/ROOM_JOIN "):
                parts = text.split(" ", 1)
                room_id = parts[1].strip()
                handle_room_join(username, room_id, client_sock)
            elif text.startswith("/ROOM_QUIT"):
                parts = text.split(" ", 1)
                if len(parts) == 2:
                    room_id = parts[1].strip()
                    handle_room_quit(username, room_id, client_sock)
            elif text.startswith("/ROOM_RELAY_REQUEST "):
                parts = text.split(" ", 2)
                if len(parts) == 3:
                    room_id = parts[1].strip()
                    target_name = parts[2].strip()
                    client_sock.sendall(f"[{timestamp()}] [系统] 服务器已为您和 '{target_name}' 开启中转桥接\n".encode(ENCODING))
            elif text.lower().startswith("/tcp_voice @"):
                # 接收降级后的音频数据：\TCP_VOICE @对方名字 payload二进制
                parts = text.split(' ', 2)
                if len(parts) >= 3:
                    target_name = parts[1][1:] # 移除@
                    voice_data = parts[2]
                    # 给目标直接做系统级别中继下发
                    privatecast(f"/TCP_VOICE_FROM @{username} {voice_data}", target_name, client_sock)
            elif text[0] == '@':
                # become    @tuoliyuan content
                parts = text.split(' ', 1)
                target_name = parts[0][1:]  # targetname = tuoliyuan
                if len(parts) > 1:
                    content = parts[1]
                else:
                    content = ""
                # 检查是否互为联系人
                if not is_mutual_contact(username, target_name):
                    client_sock.sendall(f"[{timestamp()}] [系统] '{target_name}' 不是您的联系人，请先 /contacts add {target_name}\n".encode(ENCODING))
                elif not is_user_online(target_name):
                    client_sock.sendall(f"[{timestamp()}] [系统] '{target_name}' 当前不在线\n".encode(ENCODING))
                else:
                    formatted = f"[{timestamp()}] [私聊] {username}: {content}"
                    privatecast(formatted, target_name, sender_socket=client_sock)

            else:
                # 普通消息 → 广播给所有在线联系人（而非所有在线用户）
                with contacts_lock:
                    user_contacts = contacts.get(username, [])
                formatted = f"[{timestamp()}] {username}: {text}"
                data_bytes = formatted.encode(ENCODING)
                with clients_lock:
                    for sock, name in list(clients.items()):
                        if name in user_contacts and sock != client_sock:
                            try:
                                sock.sendall(data_bytes)
                            except Exception:
                                remove_client(sock)

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
            broadcast(leave_msg)
            # 通知该用户的联系人：他下线了
            notify_contact_status(username, online=False)


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

        # 保存联系人数据
        save_contacts()


if __name__ == "__main__":
    load_contacts()
    start_server()
