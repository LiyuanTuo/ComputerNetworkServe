import tkinter as tk
from tkinter import messagebox, ttk, simpledialog
import threading
import socket
import base64
import os
import time
from datetime import datetime
import re

from audio import record_audio, play_audio, start_realtime_audio, stop_realtime_audio, TEMP_WAV_FILE, set_mute, set_pause

BUFFER_SIZE = 1024 * 1024
ENCODING = "utf-8"

# ==================== 颜色主题 ====================
COLORS = {
    "bg":           "#F0F2F5",
    "header":       "#1A1A2E",
    "accent":       "#0984E3",
    "accent_hover": "#0770C2",
    "online":       "#00B894",
    "offline":      "#B2BEC3",
    "danger":       "#D63031",
    "card":         "#FFFFFF",
    "text":         "#2D3436",
    "text_light":   "#636E72",
    "text_white":   "#FFFFFF",
    "border":       "#DFE6E9",
    "chat_self":    "#00B894",
    "chat_other":   "#FFFFFF",
    "sidebar":      "#2D3436",
}


class VoiceChatApp:
    def __init__(self, root):
        self.root = root
        self.root.title("LinkVoice 实时语音通信系统")
        self.root.geometry("880x620")
        self.root.minsize(780, 520)
        self.root.configure(bg=COLORS["bg"])

        # 网络相关
        self.client_sock = None
        self.current_user = None
        self.server_ip = "127.0.0.1" # 默认改为127.0.0.1或者保留原IP
        self.port = 9999
        self.stop_event = threading.Event()
        self.current_pending_port = None

        # 联系人状态缓存 {name: "online"/"offline"}
        self.contact_status: dict[str, str] = {}
        
        # 聊天消息缓存：{target: [{"text": str, "tag": str}]}
        # "广播" 是默认频道
        self.chat_history = {"广播": []}
        self.current_chat_target = "广播"
        self.selected_contact = "广播"

        # 页面容器
        self.container = tk.Frame(self.root, bg=COLORS["bg"])
        self.container.pack(fill="both", expand=True)

        self.show_login_page()

    def clear_container(self):
        for w in self.container.winfo_children():
            w.destroy()

    # ==================== 1. 登录页 ====================
    def show_login_page(self):
        self.clear_container()

        # 顶部装饰
        banner = tk.Canvas(self.container, height=200, bg=COLORS["accent"], highlightthickness=0)
        banner.pack(fill="x")
        banner.create_text(440, 70, text="LinkVoice", fill="white", font=("Helvetica", 36, "bold"))
        banner.create_text(440, 110, text="局域网实时语音通信系统", fill="#B8E4FF", font=("微软雅黑", 12))

        # 登录卡片
        card = tk.Frame(self.container, bg=COLORS["card"], padx=40, pady=35)
        card.place(relx=0.5, rely=0.62, anchor="center", width=360)

        tk.Label(card, text="服务器 IP", bg=COLORS["card"], fg=COLORS["text_light"],
                 font=("微软雅黑", 9)).pack(anchor="w")
        self.entry_ip = tk.Entry(card, font=("Consolas", 11), bd=1, relief="solid")
        self.entry_ip.pack(fill="x", pady=(3, 12), ipady=4)
        self.entry_ip.insert(0, self.server_ip)

        tk.Label(card, text="用户名", bg=COLORS["card"], fg=COLORS["text_light"],
                 font=("微软雅黑", 9)).pack(anchor="w")
        self.entry_userid = tk.Entry(card, font=("Arial", 11), bd=1, relief="solid")
        self.entry_userid.pack(fill="x", pady=(3, 20), ipady=4)

        btn = tk.Button(card, text="连接并登录", bg=COLORS["accent"], fg="white",
                        font=("微软雅黑", 11, "bold"), relief="flat", cursor="hand2",
                        activebackground=COLORS["accent_hover"], command=self.handle_login)
        btn.pack(fill="x", ipady=6)

    def handle_login(self):
        username = self.entry_userid.get().strip()
        ip = self.entry_ip.get().strip()
        if not username:
            messagebox.showwarning("提示", "用户名不能为空")
            return
        if not ip:
            messagebox.showwarning("提示", "请输入服务器 IP")
            return

        self.server_ip = ip
        try:
            self.client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.client_sock.connect((self.server_ip, self.port))
            self.client_sock.sendall(username.encode(ENCODING))
            self.current_user = username
            self.stop_event.clear()
            threading.Thread(target=self.receive_messages_thread, daemon=True).start()
            self.show_main_page()
        except Exception as e:
            messagebox.showerror("连接失败", f"无法连接到服务器:\n{e}")

    # ==================== 2. 主页面 ====================
    def show_main_page(self):
        self.clear_container()

        # --- 顶部栏 ---
        header = tk.Frame(self.container, bg=COLORS["header"], height=50)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="LinkVoice", fg=COLORS["accent"], bg=COLORS["header"],
                 font=("Helvetica", 14, "bold")).pack(side="left", padx=16)
        tk.Label(header, text=f"已登录: {self.current_user}", fg="#AAB0B7",
                 bg=COLORS["header"], font=("微软雅黑", 9)).pack(side="right", padx=16)

        # --- 主体两栏布局 ---
        body = tk.Frame(self.container, bg=COLORS["bg"])
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # ======= 左侧：通讯录 =======
        sidebar = tk.Frame(body, bg=COLORS["card"], width=260)
        sidebar.grid(row=0, column=0, sticky="ns", padx=(0, 1))
        sidebar.grid_propagate(False)

        # 搜索/添加联系人栏
        top_bar = tk.Frame(sidebar, bg=COLORS["card"], pady=8, padx=10)
        top_bar.pack(fill="x")
        tk.Label(top_bar, text="频道与联系人", bg=COLORS["card"], fg=COLORS["text"],
                 font=("微软雅黑", 12, "bold")).pack(side="left")
        btn_add = tk.Button(top_bar, text="＋添加", bg=COLORS["accent"], fg="white",
                            font=("微软雅黑", 9), relief="flat", cursor="hand2",
                            padx=8, command=self.add_contact_dialog)
        btn_add.pack(side="right")

        sep = tk.Frame(sidebar, bg=COLORS["border"], height=1)
        sep.pack(fill="x")

        # 联系人列表区域
        list_container = tk.Frame(sidebar, bg=COLORS["card"])
        list_container.pack(fill="both", expand=True)

        self.contacts_canvas = tk.Canvas(list_container, bg=COLORS["card"],
                                         highlightthickness=0, bd=0)
        self.contacts_scrollbar = ttk.Scrollbar(list_container, orient="vertical",
                                                 command=self.contacts_canvas.yview)
        self.contacts_inner = tk.Frame(self.contacts_canvas, bg=COLORS["card"])

        self.contacts_inner.bind("<Configure>",
            lambda e: self.contacts_canvas.configure(scrollregion=self.contacts_canvas.bbox("all")))
        self.contacts_canvas.create_window((0, 0), window=self.contacts_inner, anchor="nw", width=258)
        self.contacts_canvas.configure(yscrollcommand=self.contacts_scrollbar.set)

        self.contacts_canvas.pack(side="left", fill="both", expand=True)
        self.contacts_scrollbar.pack(side="right", fill="y")

        # 底部操作栏
        bot_bar = tk.Frame(sidebar, bg=COLORS["card"], pady=6, padx=10)
        bot_bar.pack(fill="x", side="bottom")
        sep2 = tk.Frame(sidebar, bg=COLORS["border"], height=1)
        sep2.pack(fill="x", side="bottom")

        tk.Button(bot_bar, text="刷新列表", font=("微软雅黑", 9), relief="flat",
                  fg=COLORS["accent"], bg=COLORS["card"], cursor="hand2",
                  command=self.request_contacts_list).pack(side="left")
        tk.Button(bot_bar, text="断开连接", font=("微软雅黑", 9), relief="flat",
                  fg=COLORS["danger"], bg=COLORS["card"], cursor="hand2",
                  command=self.disconnect).pack(side="right")

        # ======= 右侧：聊天 + 操作 =======
        right = tk.Frame(body, bg=COLORS["bg"])
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        # 右侧聊天标题栏
        chat_header = tk.Frame(right, bg=COLORS["card"], height=44)
        chat_header.grid(row=0, column=0, sticky="ew")
        chat_header.pack_propagate(False)
        self.lbl_chat_target = tk.Label(chat_header, text="当前频道：广播", bg=COLORS["card"], font=("微软雅黑", 11, "bold"), fg=COLORS["text"])
        self.lbl_chat_target.pack(side="left", padx=12, pady=10)

        # 聊天记录
        chat_frame = tk.Frame(right, bg=COLORS["bg"], padx=12, pady=8)
        chat_frame.grid(row=1, column=0, sticky="nsew")
        chat_frame.rowconfigure(0, weight=1)
        chat_frame.columnconfigure(0, weight=1)

        self.chat_text = tk.Text(chat_frame, font=("微软雅黑", 10), bg=COLORS["card"],
                                 fg=COLORS["text"], wrap="word", bd=0, padx=10, pady=8,
                                 state="disabled", relief="flat")
        chat_scroll = ttk.Scrollbar(chat_frame, command=self.chat_text.yview)
        self.chat_text.configure(yscrollcommand=chat_scroll.set)
        self.chat_text.grid(row=0, column=0, sticky="nsew")
        chat_scroll.grid(row=0, column=1, sticky="ns")

        # 配置聊天文本标签
        self.chat_text.tag_configure("system", foreground="#0984E3", font=("微软雅黑", 9, "italic"))
        self.chat_text.tag_configure("contact_notify", foreground="#00B894", font=("微软雅黑", 9))
        self.chat_text.tag_configure("normal", foreground=COLORS["text"])
        self.chat_text.tag_configure("self", foreground=COLORS["chat_self"], justify="right")

        # 输入区域
        input_frame = tk.Frame(right, bg=COLORS["card"], padx=12, pady=8)
        input_frame.grid(row=2, column=0, sticky="ew")
        input_frame.columnconfigure(0, weight=1)

        self.entry_msg = tk.Entry(input_frame, font=("微软雅黑", 10), bd=1, relief="solid")
        self.entry_msg.grid(row=0, column=0, sticky="ew", ipady=4, padx=(0, 6))
        self.entry_msg.bind("<Return>", lambda e: self.send_message())

        tk.Button(input_frame, text="发送", bg=COLORS["accent"], fg="white",
                  font=("微软雅黑", 9, "bold"), relief="flat", padx=14,
                  command=self.send_message).grid(row=0, column=1)

        # 功能按钮行
        action_frame = tk.Frame(right, bg=COLORS["bg"], padx=12)
        action_frame.grid(row=3, column=0, sticky="ew")
        for i in range(4):
            action_frame.columnconfigure(i, weight=1)

        tk.Button(action_frame, text="🎙 语音留言", font=("微软雅黑", 9), relief="flat",
                  bg=COLORS["card"], cursor="hand2",
                  command=self.open_voice_msg_dialog).grid(row=0, column=0, sticky="ew", padx=2)
        tk.Button(action_frame, text="📞 实时通话", font=("微软雅黑", 9), relief="flat",
                  bg=COLORS["card"], cursor="hand2",
                  command=self.initiate_call).grid(row=0, column=1, sticky="ew", padx=2)
        tk.Button(action_frame, text="🌐 组播会议", font=("微软雅黑", 9), relief="flat",
                  bg=COLORS["card"], cursor="hand2",
                  command=self.open_conference_dialog).grid(row=0, column=2, sticky="ew", padx=2)
        tk.Button(action_frame, text="👥 在线用户", font=("微软雅黑", 9), relief="flat",
                  bg=COLORS["card"], cursor="hand2",
                  command=self.request_online_users).grid(row=0, column=3, sticky="ew", padx=2)

        # 初次请求通讯录列表
        self.root.after(500, self.request_contacts_list)

    # ==================== 通讯录 UI 刷新 ====================
    def refresh_contacts_ui(self):
        """重新渲染联系人列表"""
        for w in self.contacts_inner.winfo_children():
            w.destroy()

        self._add_contact_row("广播", "online", is_broadcast=True)

        if not self.contact_status:
            tk.Label(self.contacts_inner, text="暂无联系人\n使用上方「＋添加」按钮", bg=COLORS["card"],
                     fg=COLORS["text_light"], font=("微软雅黑", 9), pady=20).pack()
        else:
            sorted_contacts = sorted(self.contact_status.items(),
                                      key=lambda x: (0 if x[1] == "online" else 1, x[0]))
            for name, status in sorted_contacts:
                self._add_contact_row(name, status, is_broadcast=False)

        self._highlight_selected()

    def _add_contact_row(self, name, status, is_broadcast):
        is_online = (status == "online")
        row = tk.Frame(self.contacts_inner, bg=COLORS["card"], cursor="hand2", pady=1)
        row.pack(fill="x", padx=6, pady=1)

        # 背景颜色
        bg_color = "#E8F8F0" if (is_online and not is_broadcast) else COLORS["card"]
        inner = tk.Frame(row, bg=bg_color, padx=10, pady=8)
        inner.pack(fill="x")

        # 状态圆点
        dot_canvas = tk.Canvas(inner, width=10, height=10, bg=inner["bg"], highlightthickness=0)
        dot_canvas.pack(side="left", padx=(0, 8))
        color = COLORS["accent"] if is_broadcast else (COLORS["online"] if is_online else COLORS["offline"])
        dot_canvas.create_oval(1, 1, 9, 9, fill=color, outline=color)

        # 名字
        tk.Label(inner, text=name, bg=inner["bg"], fg=COLORS["text"],
                 font=("微软雅黑", 10, "bold" if is_online or is_broadcast else "normal")).pack(side="left")

        if not is_broadcast:
            status_text = "在线" if is_online else "离线"
            tk.Label(inner, text=status_text, bg=inner["bg"],
                     fg=COLORS["online"] if is_online else COLORS["text_light"],
                     font=("微软雅黑", 8)).pack(side="left", padx=(6, 0))

            btn_del = tk.Button(inner, text="✕", bg=inner["bg"], fg=COLORS["text_light"],
                                font=("Arial", 9), relief="flat", cursor="hand2", bd=0,
                                command=lambda n=name: self.delete_contact(n))
            btn_del.pack(side="right")

        for widget in [inner, row]:
            widget.bind("<Button-1>", lambda e, n=name: self.select_contact(n))

    def select_contact(self, name):
        """选中一个联系人或者频道"""
        self.selected_contact = name
        self.current_chat_target = name
        title = "群发广播" if name == "广播" else f"与 {name} 私聊"
        self.lbl_chat_target.config(text=f"当前频道：{title}")
        self._highlight_selected()
        self.update_chat_ui()

    def _highlight_selected(self):
        for row in self.contacts_inner.winfo_children():
            for inner in row.winfo_children():
                if isinstance(inner, tk.Frame):
                    is_selected = False
                    for child in inner.winfo_children():
                        if isinstance(child, tk.Label):
                            if child.cget("text") == self.selected_contact:
                                is_selected = True
                                break
                    
                    if is_selected:
                        inner.configure(highlightbackground=COLORS["accent"], highlightthickness=2)
                    else:
                        inner.configure(highlightbackground=inner["bg"], highlightthickness=0)

    # ==================== 通讯录管理 ====================
    def add_contact_dialog(self):
        name = simpledialog.askstring("添加联系人", "请输入要添加的用户名:", parent=self.root)
        if name and name.strip():
            self.client_sock.sendall(f"/contacts add {name.strip()}".encode(ENCODING))

    def delete_contact(self, name):
        if messagebox.askyesno("确认", f"确定要删除联系人 '{name}' 吗？"):
            self.client_sock.sendall(f"/contacts del {name}".encode(ENCODING))
            if self.current_chat_target == name:
                self.select_contact("广播")

    def request_contacts_list(self):
        if self.client_sock:
            self.client_sock.sendall("/contacts".encode(ENCODING))

    def request_online_users(self):
        if self.client_sock:
            self.client_sock.sendall("/online".encode(ENCODING))

    # ==================== 聊天消息 ====================
    def append_to_history(self, target, text, tag="normal"):
        """保存历史记录并根据当前视角更新 UI"""
        if target not in self.chat_history:
            self.chat_history[target] = []
        self.chat_history[target].append((text, tag))
        
        if self.current_chat_target == target:
            self.append_chat(text, tag)

    def append_chat(self, text, tag="normal"):
        """底层 UI 插入"""
        def _do():
            self.chat_text.configure(state="normal")
            self.chat_text.insert("end", text + "\n", tag)
            self.chat_text.see("end")
            self.chat_text.configure(state="disabled")
        self.root.after(0, _do)

    def update_chat_ui(self):
        """切换频道时刷新聊天框"""
        def _do():
            self.chat_text.configure(state="normal")
            self.chat_text.delete(1.0, "end")
            history = self.chat_history.get(self.current_chat_target, [])
            for text, tag in history:
                self.chat_text.insert("end", text + "\n", tag)
            self.chat_text.see("end")
            self.chat_text.configure(state="disabled")
        self.root.after(0, _do)

    def send_message(self):
        msg = self.entry_msg.get().strip()
        if not msg:
            return
        self.entry_msg.delete(0, "end")
        
        target = self.current_chat_target
        now = datetime.now().strftime("%H:%M:%S")
        try:
            if target == "广播":
                self.client_sock.sendall(msg.encode(ENCODING))
                self.append_to_history("广播", f"[{now}] 我 (广播): {msg}", "self")
            else:
                self.client_sock.sendall(f"@{target} {msg}".encode(ENCODING))
                self.append_to_history(target, f"[{now}] 我: {msg}", "self")
        except Exception as e:
            self.append_to_history(target, f"[发送失败] {e}", "system")

    # ==================== 网络接收线程 ====================
    def receive_messages_thread(self):
        while not self.stop_event.is_set():
            try:
                data = self.client_sock.recv(BUFFER_SIZE)
                if not data:
                    self.append_to_history("广播", "[系统] 与服务器的连接已断开", "system")
                    self.stop_event.set()
                    break

                message = data.decode(ENCODING)
                lines = message.split("\n")

                for line in lines:
                    if not line.strip():
                        continue

                    if line.startswith("/CONTACT_STATUS "):
                        parts = line.strip().split(" ")
                        if len(parts) >= 3:
                            contact_name = parts[1]
                            status = parts[2]
                            if status == "removed":
                                old = self.contact_status.pop(contact_name, None)
                                if old:
                                    self.append_to_history("广播", f"[通讯录] 联系人 '{contact_name}' 已被移除", "contact_notify")
                            else:
                                old_status = self.contact_status.get(contact_name)
                                self.contact_status[contact_name] = status
                                if old_status and old_status != status:
                                    hint = "上线了" if status == "online" else "离线了"
                                    self.append_to_history("广播", f"[通讯录] 联系人 '{contact_name}' {hint}", "contact_notify")
                            self.root.after(0, self.refresh_contacts_ui)
                        continue

                    if line.startswith("/CALL_REQUEST "):
                        parts = line.split(" ")
                        caller = parts[1]
                        r_port = int(parts[2].strip())
                        self.root.after(0, lambda c=caller, p=r_port: self.handle_incoming_call(c, p))
                        continue

                    if line.startswith("/CALL_REPLY_FAIL "):
                        parts = line.split(" ")
                        target = parts[1]
                        reason = parts[2].strip()
                        reasons = {"1": "不在线", "2": "正在通话中", "3": "拒绝了您的请求"}
                        self.append_to_history(target, f"[系统] 呼叫 '{target}' 失败：{reasons.get(reason, '未知错误')}", "system")
                        continue

                    if line.startswith("/CALL_REPLY_OK "):
                        parts = line.split(" ")
                        target = parts[1]
                        udp_port = int(parts[2].strip())
                        self.root.after(0, lambda t=target, p=udp_port: self.on_call_accepted(t, p))
                        continue

                    if "AUDIO:" in line:
                        prefix, b64_audio = line.split("AUDIO:", 1)
                        target = "广播"
                        # [10:11:12] [私聊] username: 
                        match_private = re.search(r'\[私聊\]\s+(.*?):', prefix)
                        match_public = re.search(r'\]\s+(.*?):', prefix)
                        if match_private:
                            target = match_private.group(1).strip()
                        elif match_public:
                            target = "广播"

                        self.append_to_history(target, f"{prefix}发送了一段语音消息，正在播放...", "system")
                        try:
                            wav_bytes = base64.b64decode(b64_audio)
                            with open("recv_voice.wav", "wb") as f:
                                f.write(wav_bytes)
                            threading.Thread(target=lambda: play_audio("recv_voice.wav"), daemon=True).start()
                        except Exception:
                            self.append_to_history(target, "[系统] 语音播放失败", "system")
                        continue

                    if "通话已被" in line and "终止" in line:
                        stop_realtime_audio()
                        self.append_to_history("广播", line, "system")
                        if hasattr(self, "call_win") and self.call_win.winfo_exists():
                            self.root.after(0, self.call_win.destroy)
                        continue

                    # 判断类别用于分流
                    if "[通讯录]" in line:
                        self.append_to_history("广播", line, "contact_notify")
                    elif ">>>" in line or "[系统]" in line or "欢迎" in line:
                        self.append_to_history("广播", line, "system")
                    else:
                        match_private = re.search(r'\[私聊\]\s+(.*?):', line)
                        if match_private:
                            sender = match_private.group(1).strip()
                            self.append_to_history(sender, line, "normal")
                        else:
                            self.append_to_history("广播", line, "normal")

            except ConnectionResetError:
                self.append_to_history("广播", "[系统] 连接被服务器重置", "system")
                self.stop_event.set()
                break
            except OSError:
                break
            except Exception:
                pass

    # ==================== 语音 & 通话 ====================
    def open_voice_msg_dialog(self):
        target = self.get_selected_user()
        if not target:
            return

        self.append_to_history(target, f"[系统] 正在录制语音留言 (3秒)...", "system")

        def do_record():
            record_audio()
            with open(TEMP_WAV_FILE, "rb") as f:
                b64_data = base64.b64encode(f.read()).decode(ENCODING)
            msg = f"@{target} AUDIO:{b64_data}"
            self.client_sock.sendall(msg.encode(ENCODING))
            self.append_to_history(target, f"[系统] 已向 {target} 发送语音留言", "system")

        threading.Thread(target=do_record, daemon=True).start()

    def initiate_call(self):
        target = self.get_selected_user()
        if not target:
            return
        self.client_sock.sendall(f"/call @{target}".encode(ENCODING))
        self.append_to_history(target, f"[系统] 正在呼叫 {target}，等待对方接听...", "system")

    def handle_incoming_call(self, caller, port):
        if messagebox.askyesno("来电", f"用户 '{caller}' 邀请你进行实时通话，是否接听？"):
            self.client_sock.sendall(f"/CALL_ACCEPT {caller}".encode(ENCODING))
            start_realtime_audio(self.server_ip, port)
            self.show_call_overlay(caller)
        else:
            self.client_sock.sendall(f"/CALL_REJECT {caller}".encode(ENCODING))

    def on_call_accepted(self, target, udp_port):
        self.append_to_history(target, f"[系统] '{target}' 已接受呼叫，通话建立中...", "system")
        start_realtime_audio(self.server_ip, udp_port)
        self.show_call_overlay(target)

    def show_call_overlay(self, target):
        self.call_win = tk.Toplevel(self.root)
        self.call_win.title("通话中")
        self.call_win.geometry("280x200")
        self.call_win.resizable(False, False)
        self.call_win.configure(bg=COLORS["header"])

        tk.Label(self.call_win, text=f"📞 正在与 {target} 通话",
                 fg="white", bg=COLORS["header"], font=("微软雅黑", 11), pady=15).pack()

        control_frame = tk.Frame(self.call_win, bg=COLORS["header"])
        control_frame.pack(pady=10)

        self.is_muted = False
        self.is_paused = False

        def toggle_mute():
            self.is_muted = not self.is_muted
            set_mute(self.is_muted)
            btn_mute.config(text="取消静音" if self.is_muted else "静音🎙")
            btn_mute.config(bg="orange" if self.is_muted else "#5A6A80")

        def toggle_pause():
            self.is_paused = not self.is_paused
            set_pause(self.is_paused)
            btn_pause.config(text="恢复通话" if self.is_paused else "暂停通话⏸")
            btn_pause.config(bg="orange" if self.is_paused else "#5A6A80")

        btn_mute = tk.Button(control_frame, text="静音🎙", bg="#5A6A80", fg="white",
                             font=("微软雅黑", 9), relief="flat", cursor="hand2", width=9,
                             command=toggle_mute)
        btn_mute.pack(side=tk.LEFT, padx=10)

        btn_pause = tk.Button(control_frame, text="暂停通话⏸", bg="#5A6A80", fg="white",
                              font=("微软雅黑", 9), relief="flat", cursor="hand2", width=9,
                              command=toggle_pause)
        btn_pause.pack(side=tk.LEFT, padx=10)

        tk.Button(self.call_win, text="挂断", bg=COLORS["danger"], fg="white",
                  font=("微软雅黑", 10, "bold"), relief="flat", cursor="hand2",
                  padx=30, pady=4, command=self.end_call).pack(pady=5)

    def end_call(self):
        stop_realtime_audio()
        try:
            self.client_sock.sendall("/realtime -quit".encode(ENCODING))
        except Exception:
            pass
        if hasattr(self, "call_win") and self.call_win.winfo_exists():
            self.call_win.destroy()

    def open_conference_dialog(self):
        messagebox.showinfo("组播会议", "正在加入组播组: 224.1.1.1\n(此功能需对接系统组播权限)")

    # ==================== 工具方法 ====================
    def get_selected_user(self):
        name = getattr(self, "selected_contact", None)
        if not name or name == "广播":
            messagebox.showwarning("提示", "请选择一个具体的联系人（不能是广播大厅）")
            return None
        return name

    def disconnect(self):
        if messagebox.askyesno("确认", "确定断开与服务器的连接？"):
            try:
                self.client_sock.sendall("/quit".encode(ENCODING))
            except Exception:
                pass
            self.stop_event.set()
            if self.client_sock:
                self.client_sock.close()
            self.contact_status.clear()
            self.chat_history.clear()
            self.chat_history["广播"] = []
            self.show_login_page()

    def on_closing(self):
        self.stop_event.set()
        try:
            if self.client_sock:
                self.client_sock.sendall("/quit".encode(ENCODING))
                self.client_sock.close()
        except Exception:
            pass
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = VoiceChatApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()
