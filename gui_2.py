import tkinter as tk
from tkinter import messagebox, ttk
import threading
import socket
import base64
import os
import time

from audio import record_audio, play_audio, start_realtime_audio, stop_realtime_audio, TEMP_WAV_FILE
BUFFER_SIZE = 1024 * 1024 
ENCODING = "utf-8"

class VoiceChatApp:
    def __init__(self, root):
        self.root = root
        self.root.title("LinkVoice 实时语音通信系统")
        self.root.geometry("400x650")
        self.root.configure(bg="#F5F6FA")
        
        # 网络相关变量
        self.client_sock = None
        self.current_user = None
        self.server_ip = "10.192.53.115" # 建议实际使用时改为你的局域网IP
        self.port = 9999
        self.stop_event = threading.Event()
        
        # 页面容器
        self.container = tk.Frame(self.root, bg="#F5F6FA")
        self.container.pack(fill="both", expand=True)

        self.show_login_page()

    def clear_container(self):
        for widget in self.container.winfo_children():
            widget.destroy()

    # ==================== 1. 登录逻辑与页面 ====================
    def show_login_page(self):
        self.clear_container()
        
        # 装饰背景
        canvas = tk.Canvas(self.container, width=400, height=200, bg="#0984E3", highlightthickness=0)
        canvas.pack(fill="x")
        canvas.create_text(200, 100, text="LinkVoice", fill="white", font=("Helvetica", 32, "bold"))

        login_card = tk.Frame(self.container, bg="white", padx=30, pady=30)
        login_card.place(relx=0.5, rely=0.6, anchor="center", width=320)

        tk.Label(login_card, text="用户名", bg="white", fg="#636E72").pack(anchor="w")
        self.entry_userid = tk.Entry(login_card, font=("Arial", 12), bd=1, relief="solid")
        self.entry_userid.pack(fill="x", pady=(5, 20), ipady=5)
        self.entry_userid.insert(0, "User_001")

        btn_login = tk.Button(login_card, text="进入系统", bg="#0984E3", fg="white", 
                             font=("微软雅黑", 12, "bold"), relief="flat", cursor="hand2",
                             command=self.handle_login)
        btn_login.pack(fill="x", ipady=5)

    def handle_login(self):
        username = self.entry_userid.get().strip()
        if not username:
            messagebox.showwarning("提示", "用户名不能为空")
            return
        
        # 尝试连接服务器
        try:
            self.client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.client_sock.connect((self.server_ip, self.port))
            self.client_sock.sendall(username.encode(ENCODING)) # 发送用户名注册
            self.current_user = username
            
            # 开启后台接收线程
            self.stop_event.clear()
            threading.Thread(target=self.receive_messages_thread, daemon=True).start()
            
            self.show_main_page()
        except Exception as e:
            messagebox.showerror("连接失败", f"无法连接到服务器: {e}")

    # ==================== 2. 主页面 (通讯录) ====================
    def show_main_page(self):
        self.clear_container()

        # 顶部栏
        header = tk.Frame(self.container, bg="#2D3436", height=60)
        header.pack(fill="x")
        tk.Label(header, text=f"👤 {self.current_user}", fg="white", bg="#2D3436", font=("微软雅黑", 10)).pack(side="left", padx=20, pady=15)
        
        # 好友列表
        content = tk.Frame(self.container, bg="#F5F6FA", padx=20, pady=10)
        content.pack(fill="both", expand=True)
        
        tk.Label(content, text="在线联系人 (双击拨打)", bg="#F5F6FA", fg="#636E72", font=("微软雅黑", 9)).pack(anchor="w")
        
        self.listbox_friends = tk.Listbox(content, font=("微软雅黑", 11), bd=0, highlightthickness=0, 
                                          selectbackground="#DFE6E9", selectforeground="#0984E3")
        self.listbox_friends.pack(fill="both", expand=True, pady=10)
        
        # 功能按钮区
        btn_frame = tk.Frame(content, bg="#F5F6FA")
        btn_frame.pack(fill="x", pady=10)

        # 语音留言按钮逻辑
        self.btn_voice_msg = tk.Button(btn_frame, text="🎙️ 语音留言", command=self.open_voice_msg_dialog)
        self.btn_voice_msg.grid(row=0, column=0, sticky="ew", padx=5)
        
        tk.Button(btn_frame, text="📞 实时通话", command=self.initiate_call).grid(row=0, column=1, sticky="ew", padx=5)
        btn_frame.columnconfigure(0, weight=1)
        btn_frame.columnconfigure(1, weight=1)

        tk.Button(content, text="🌐 进入组播会议室", bg="#2D3436", fg="white", command=self.open_conference_dialog).pack(fill="x", pady=10, ipady=5)

    # ==================== 3. 核心业务逻辑 (对接后端协议) ====================
    
    def receive_messages_thread(self):
        """对接 client.py 中的 receive_messages 逻辑"""
        while not self.stop_event.is_set():
            try:
                data = self.client_sock.recv(BUFFER_SIZE)
                if not data: break
                
                message = data.decode(ENCODING)
                
                # 处理呼叫请求
                if message.startswith("\\CALL_REQUEST "):
                    parts = message.split(" ")
                    caller = parts[1]
                    r_port = int(parts[2].strip())
                    self.root.after(0, lambda: self.handle_incoming_call(caller, r_port))
                
                # 处理呼叫回复
                elif message.startswith("\\CALL_REPLY_OK "):
                    parts = message.split(" ")
                    target = parts[1]
                    udp_port = int(parts[2].strip())
                    self.root.after(0, lambda: self.on_call_accepted(target, udp_port))

                # 处理语音留言音频
                elif "AUDIO:" in message:
                    prefix, b64_audio = message.split("AUDIO:", 1)
                    wav_bytes = base64.b64decode(b64_audio)
                    with open("recv_voice.wav", "wb") as f:
                        f.write(wav_bytes)
                    threading.Thread(target=lambda: play_audio("recv_voice.wav")).start()
                
                # 处理挂断通知
                elif "通话已被" in message:
                    stop_realtime_audio()

            except:
                break

    def open_voice_msg_dialog(self):
        """实现录音并发送给指定用户"""
        target = self.get_selected_user()
        if not target: return

        # 使用线程录音，防止 UI 卡死
        def do_record():
            record_audio() # 录制 3 秒
            with open(TEMP_WAV_FILE, "rb") as f:
                b64_data = base64.b64encode(f.read()).decode(ENCODING)
            # 构造协议格式: @target AUDIO:base64数据
            msg = f"@{target} AUDIO:{b64_data}"
            self.client_sock.sendall(msg.encode(ENCODING))
            self.root.after(0, lambda: messagebox.showinfo("完成", f"已向 {target} 发送语音留言"))

        threading.Thread(target=do_record).start()

    def initiate_call(self):
        """主动发起呼叫"""
        target = self.get_selected_user()
        if not target: return
        # 发送协议: /call @target
        self.client_sock.sendall(f"/call @{target}".encode(ENCODING))
        messagebox.showinfo("呼叫", f"正在等待 {target} 接听...")

    def handle_incoming_call(self, caller, port):
        """被动接收呼叫弹窗"""
        if messagebox.askyesno("语音请求", f"用户 {caller} 邀请你进行实时通话，是否接听？"):
            # 发送接受协议: \CALL_ACCEPT caller
            self.client_sock.sendall(f"\\CALL_ACCEPT {caller}".encode(ENCODING))
            start_realtime_audio(self.server_ip, port)
            self.show_call_overlay(caller)
        else:
            self.client_sock.sendall(f"\\CALL_REJECT {caller}".encode(ENCODING))

    def on_call_accepted(self, target, udp_port):
        """呼叫被对方接受后，启动音频线程"""
        start_realtime_audio(self.server_ip, udp_port)
        self.show_call_overlay(target)

    def show_call_overlay(self, target):
        """显示通话中的悬浮窗"""
        self.call_win = tk.Toplevel(self.root)
        self.call_win.title("通话中")
        self.call_win.geometry("250x150")
        tk.Label(self.call_win, text=f"正在与 {target} 通话", pady=20).pack()
        tk.Button(self.call_win, text="挂断", bg="#D63031", fg="white", 
                  command=self.end_call).pack(fill="x", padx=50)

    def end_call(self):
        """结束通话并通知服务器"""
        stop_realtime_audio()
        self.client_sock.sendall("/realtime -quit".encode(ENCODING))
        if hasattr(self, 'call_win'): self.call_win.destroy()

    def open_conference_dialog(self):
        """组播会议"""
        ip = "224.1.1.1" # 默认组播地址
        # 实际逻辑需在 audio.py 中增加组播支持
        messagebox.showinfo("组播会议", f"正在加入组播组: {ip}\n(此功能需对接系统组播权限)")

    def get_selected_user(self):
        sel = self.listbox_friends.curselection()
        if not sel:
            messagebox.showwarning("提示", "请先选择一个在线联系人")
            return None
        return self.listbox_friends.get(sel[0]).split(" ")[0]

if __name__ == "__main__":
    root = tk.Tk()
    app = VoiceChatApp(root)
    root.mainloop()