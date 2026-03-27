import tkinter as tk
from tkinter import messagebox, ttk, simpledialog
import threading
import socket
import base64
import os
import time

from audio import record_audio, play_audio, start_realtime_audio, stop_realtime_audio, TEMP_WAV_FILE

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
    "chat_self":    "#DCF8C6",
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
        self.server_ip = "10.192.53.115"
        self.port = 9999
        self.stop_event = threading.Event()
        self.current_pending_port = None

        # 联系人状态缓存 {name: "online"/"offline"}
        self.contact_status: dict[str, str] = {}
        # 聊天消息缓存
        self.chat_messages: list[str] = []

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
        tk.Label(top_bar, text="通讯录", bg=COLORS["card"], fg=COLORS["text"],
                 font=("微软雅黑", 12, "bold")).pack(side="left")
        btn_add = tk.Button(top_bar, text="＋添加", bg=COLORS["accent"], fg="white",
                            font=("微软雅黑", 9), relief="flat", cursor="hand2",
                            padx=8, command=self.add_contact_dialog)
        btn_add.pack(side="right")

        sep = tk.Frame(sidebar, bg=COLORS["border"], height=1)
        sep.pack(fill="x")

        # 联系人列表区域 (用 Canvas + Frame 自绘实现带状态圆点)
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
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        # 聊天记录
        chat_frame = tk.Frame(right, bg=COLORS["bg"], padx=12, pady=8)
        chat_frame.grid(row=0, column=0, sticky="nsew")
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

        # 输入区域
        input_frame = tk.Frame(right, bg=COLORS["card"], padx=12, pady=8)
        input_frame.grid(row=1, column=0, sticky="ew")
        input_frame.columnconfigure(0, weight=1)

        self.entry_msg = tk.Entry(input_frame, font=("微软雅黑", 10), bd=1, relief="solid")
        self.entry_msg.grid(row=0, column=0, sticky="ew", ipady=4, padx=(0, 6))
        self.entry_msg.bind("<Return>", lambda e: self.send_message())

        tk.Button(input_frame, text="发送", bg=COLORS["accent"], fg="white",
                  font=("微软雅黑", 9, "bold"), relief="flat", padx=14,
                  command=self.send_message).grid(row=0, column=1)

        # 功能按钮行
        action_frame = tk.Frame(right, bg=COLORS["bg"], padx=12)
        action_frame.grid(row=2, column=0, sticky="ew")
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
        """根据 self.contact_status 重新渲染联系人列表"""
        for w in self.contacts_inner.winfo_children():
            w.destroy()

        if not self.contact_status:
            tk.Label(self.contacts_inner, text="暂无联系人\n使用上方「＋添加」按钮", bg=COLORS["card"],
                     fg=COLORS["text_light"], font=("微软雅黑", 9), pady=30).pack()
            return

        # 按在线状态排序：在线在前
        sorted_contacts = sorted(self.contact_status.items(),
                                  key=lambda x: (0 if x[1] == "online" else 1, x[0]))

        for name, status in sorted_contacts:
            is_online = (status == "online")
            row = tk.Frame(self.contacts_inner, bg=COLORS["card"], cursor="hand2", pady=1)
            row.pack(fill="x", padx=6, pady=1)

            # 背景高亮为绿色条
            inner = tk.Frame(row, bg="#E8F8F0" if is_online else COLORS["card"], padx=10, pady=8)
            inner.pack(fill="x")

            # 状态圆点
            dot_canvas = tk.Canvas(inner, width=10, height=10, bg=inner["bg"], highlightthickness=0)
            dot_canvas.pack(side="left", padx=(0, 8))
            color = COLORS["online"] if is_online else COLORS["offline"]
            dot_canvas.create_oval(1, 1, 9, 9, fill=color, outline=color)

            # 名字
            tk.Label(inner, text=name, bg=inner["bg"], fg=COLORS["text"],
                     font=("微软雅黑", 10, "bold" if is_online else "normal")).pack(side="left")

            # 状态文字
            status_text = "在线" if is_online else "离线"
            tk.Label(inner, text=status_text, bg=inner["bg"],
                     fg=COLORS["online"] if is_online else COLORS["text_light"],
                     font=("微软雅黑", 8)).pack(side="left", padx=(6, 0))

            # 删除按钮
            btn_del = tk.Button(inner, text="✕", bg=inner["bg"], fg=COLORS["text_light"],
                                font=("Arial", 9), relief="flat", cursor="hand2", bd=0,
                                command=lambda n=name: self.delete_contact(n))
            btn_del.pack(side="right")

            # 绑定点击选中
            for widget in [inner, row]:
                widget.bind("<Button-1>", lambda e, n=name: self.select_contact(n))

        # 刷新 UI 后恢复高亮
        self._highlight_selected()

    def select_contact(self, name):
        """选中一个联系人（UI 高亮 + 记住选择）"""
        self.selected_contact = name
        # 简单方案：刷新 UI 后用标记高亮
        self._highlight_selected()

    def _highlight_selected(self):
        """遍历联系人行，给选中的加边框，未选中的取消边框"""
        for row in self.contacts_inner.winfo_children():
            for inner in row.winfo_children():
                if isinstance(inner, tk.Frame):
                    # 检查 inner 里的 Label 文字
                    is_selected = False
                    for child in inner.winfo_children():
                        if isinstance(child, tk.Label):
                            if child.cget("text") == getattr(self, "selected_contact", None):
                                is_selected = True
                                break
                    
                    if is_selected:
                        inner.configure(highlightbackground=COLORS["accent"],
                                        highlightthickness=2)
                    else:
                        inner.configure(highlightbackground=inner["bg"], 
                                        highlightthickness=0)

    # ==================== 通讯录管理 ====================
    def add_contact_dialog(self):
        """弹窗添加联系人"""
        name = simpledialog.askstring("添加联系人", "请输入要添加的用户名:", parent=self.root)
        if name and name.strip():
            self.client_sock.sendall(f"/contacts add {name.strip()}".encode(ENCODING))

    def delete_contact(self, name):
        """删除联系人"""
        if messagebox.askyesno("确认", f"确定要删除联系人 '{name}' 吗？"):
            self.client_sock.sendall(f"/contacts del {name}".encode(ENCODING))

    def request_contacts_list(self):
        """向服务器请求通讯录列表"""
        if self.client_sock:
            self.client_sock.sendall("/contacts".encode(ENCODING))

    def request_online_users(self):
        """向服务器请求在线用户列表"""
        if self.client_sock:
            self.client_sock.sendall("/online".encode(ENCODING))

    # ==================== 聊天消息 ====================
    def append_chat(self, text, tag="normal"):
        """往聊天区域追加一条消息（线程安全，通过 root.after 调度）"""
        def _do():
            self.chat_text.configure(state="normal")
            self.chat_text.insert("end", text + "\n", tag)
            self.chat_text.see("end")
            self.chat_text.configure(state="disabled")
        self.root.after(0, _do)

    def send_message(self):
        """发送聊天消息"""
        msg = self.entry_msg.get().strip()
        if not msg:
            return
        self.entry_msg.delete(0, "end")
        try:
            self.client_sock.sendall(msg.encode(ENCODING))
        except Exception as e:
            self.append_chat(f"[发送失败] {e}", "system")

    # ==================== 网络接收线程 (核心修复: 按行分割) ====================
    def receive_messages_thread(self):
        """后台线程：接收并解析服务器消息，处理 TCP 粘包"""
        while not self.stop_event.is_set():
            try:
                data = self.client_sock.recv(BUFFER_SIZE)
                if not data:
                    self.append_chat("[系统] 与服务器的连接已断开", "system")
                    self.stop_event.set()
                    break

                message = data.decode(ENCODING)
                lines = message.split("\n")

                for line in lines:
                    if not line.strip():
                        continue

                    # --- 联系人状态推送 ---
                    if line.startswith("\\CONTACT_STATUS "):
                        parts = line.strip().split(" ")
                        if len(parts) >= 3:
                            contact_name = parts[1]
                            status = parts[2]
                            if status == "removed":
                                old = self.contact_status.pop(contact_name, None)
                                if old:
                                    self.append_chat(
                                        f"[通讯录] 联系人 '{contact_name}' 已被移除", "contact_notify")
                            else:
                                old_status = self.contact_status.get(contact_name)
                                self.contact_status[contact_name] = status
                                if old_status and old_status != status:
                                    hint = "上线了" if status == "online" else "离线了"
                                    self.append_chat(
                                        f"[通讯录] 联系人 '{contact_name}' {hint}", "contact_notify")
                            self.root.after(0, self.refresh_contacts_ui)
                        continue

                    # --- 呼叫请求 ---
                    if line.startswith("\\CALL_REQUEST "):
                        parts = line.split(" ")
                        caller = parts[1]
                        r_port = int(parts[2].strip())
                        # 用局部变量捕获，避免 lambda 闭包问题
                        self.root.after(0, lambda c=caller, p=r_port: self.handle_incoming_call(c, p))
                        continue

                    # --- 呼叫失败 ---
                    if line.startswith("\\CALL_REPLY_FAIL "):
                        parts = line.split(" ")
                        target = parts[1]
                        reason = parts[2].strip()
                        reasons = {"1": "不在线", "2": "正在通话中", "3": "拒绝了您的请求"}
                        self.append_chat(
                            f"[系统] 呼叫 '{target}' 失败：{reasons.get(reason, '未知错误')}", "system")
                        continue

                    # --- 呼叫成功 ---
                    if line.startswith("\\CALL_REPLY_OK "):
                        parts = line.split(" ")
                        target = parts[1]
                        udp_port = int(parts[2].strip())
                        self.root.after(0, lambda t=target, p=udp_port: self.on_call_accepted(t, p))
                        continue

                    # --- 语音留言 ---
                    if "AUDIO:" in line:
                        prefix, b64_audio = line.split("AUDIO:", 1)
                        self.append_chat(f"{prefix}发送了一段语音消息，正在播放...", "system")
                        try:
                            wav_bytes = base64.b64decode(b64_audio)
                            with open("recv_voice.wav", "wb") as f:
                                f.write(wav_bytes)
                            threading.Thread(target=lambda: play_audio("recv_voice.wav"),
                                             daemon=True).start()
                        except Exception:
                            self.append_chat("[系统] 语音播放失败", "system")
                        continue

                    # --- 通话结束 ---
                    if "通话已被" in line and "终止" in line:
                        stop_realtime_audio()
                        self.append_chat(line, "system")
                        if hasattr(self, "call_win") and self.call_win.winfo_exists():
                            self.root.after(0, self.call_win.destroy)
                        continue

                    # --- 普通文本消息 ---
                    # 判断类别用于高亮
                    if "[通讯录]" in line:
                        self.append_chat(line, "contact_notify")
                    elif ">>>" in line or "[系统]" in line or "欢迎" in line:
                        self.append_chat(line, "system")
                    else:
                        self.append_chat(line, "normal")

            except ConnectionResetError:
                self.append_chat("[系统] 连接被服务器重置", "system")
                self.stop_event.set()
                break
            except OSError:
                break
            except Exception:
                pass

    # ==================== 语音 & 通话 ====================
    def open_voice_msg_dialog(self):
        """录音并发送给选中联系人"""
        target = self.get_selected_user()
        if not target:
            return

        self.append_chat(f"[系统] 正在录制语音留言 (3秒)...", "system")

        def do_record():
            record_audio()
            with open(TEMP_WAV_FILE, "rb") as f:
                b64_data = base64.b64encode(f.read()).decode(ENCODING)
            msg = f"@{target} AUDIO:{b64_data}"
            self.client_sock.sendall(msg.encode(ENCODING))
            self.append_chat(f"[系统] 已向 {target} 发送语音留言", "system")

        threading.Thread(target=do_record, daemon=True).start()

    def initiate_call(self):
        """主动发起呼叫"""
        target = self.get_selected_user()
        if not target:
            return
        self.client_sock.sendall(f"/call @{target}".encode(ENCODING))
        self.append_chat(f"[系统] 正在呼叫 {target}，等待对方接听...", "system")

    def handle_incoming_call(self, caller, port):
        """被动接收呼叫弹窗"""
        if messagebox.askyesno("来电", f"用户 '{caller}' 邀请你进行实时通话，是否接听？"):
            self.client_sock.sendall(f"\\CALL_ACCEPT {caller}".encode(ENCODING))
            start_realtime_audio(self.server_ip, port)
            self.show_call_overlay(caller)
        else:
            self.client_sock.sendall(f"\\CALL_REJECT {caller}".encode(ENCODING))

    def on_call_accepted(self, target, udp_port):
        """呼叫被接受"""
        self.append_chat(f"[系统] '{target}' 已接受呼叫，通话建立中...", "system")
        start_realtime_audio(self.server_ip, udp_port)
        self.show_call_overlay(target)

    def show_call_overlay(self, target):
        """通话中悬浮窗"""
        self.call_win = tk.Toplevel(self.root)
        self.call_win.title("通话中")
        self.call_win.geometry("280x160")
        self.call_win.resizable(False, False)
        self.call_win.configure(bg=COLORS["header"])

        tk.Label(self.call_win, text=f"📞 正在与 {target} 通话",
                 fg="white", bg=COLORS["header"], font=("微软雅黑", 11), pady=25).pack()
        tk.Button(self.call_win, text="挂断", bg=COLORS["danger"], fg="white",
                  font=("微软雅黑", 10, "bold"), relief="flat", cursor="hand2",
                  padx=30, pady=4, command=self.end_call).pack()

    def end_call(self):
        """结束通话"""
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
        """获取左侧通讯录当前选中的联系人"""
        name = getattr(self, "selected_contact", None)
        if not name:
            messagebox.showwarning("提示", "请先在左侧通讯录中点击一个联系人")
            return None
        return name

    def disconnect(self):
        """断开连接"""
        if messagebox.askyesno("确认", "确定断开与服务器的连接？"):
            try:
                self.client_sock.sendall("/quit".encode(ENCODING))
            except Exception:
                pass
            self.stop_event.set()
            if self.client_sock:
                self.client_sock.close()
            self.contact_status.clear()
            self.show_login_page()

    def on_closing(self):
        """窗口关闭事件"""
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