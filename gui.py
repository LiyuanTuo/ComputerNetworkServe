import tkinter as tk
from tkinter import messagebox


class VoiceChatUI:
    def __init__(self, root):
        self.root = root
        self.root.title("语音通信系统")
        self.root.geometry("350x550")
        self.root.resizable(False, False)

        # 全局状态变量
        self.current_user = None

        # 页面容器（用于切换登录页和主页）
        self.container = tk.Frame(self.root)
        self.container.pack(fill="both", expand=True)

        # 启动时展示登录页
        self.show_login_page()

    def clear_container(self):
        """清空当前页面的所有组件，用于页面切换"""
        for widget in self.container.winfo_children():
            widget.destroy()

    # ==================== 1. 登录页面 ====================
    def show_login_page(self):
        self.clear_container()

        tk.Label(self.container, text="🎤 语音通信系统", font=("Microsoft YaHei", 20, "bold")).pack(pady=50)

        tk.Label(self.container, text="请输入 User ID:", font=("Microsoft YaHei", 12)).pack(pady=10)

        self.entry_userid = tk.Entry(self.container, font=("Arial", 14), justify="center")
        self.entry_userid.pack(pady=10, ipady=5)

        tk.Button(self.container, text="登 录", font=("Microsoft YaHei", 12), bg="#4CAF50", fg="white",
                  width=15, command=self.handle_login).pack(pady=30)

    def handle_login(self):
        user_id = self.entry_userid.get().strip()
        if not user_id:
            messagebox.showwarning("提示", "User ID 不能为空！")
            return

        # [TODO: 接入后端] 这里应调用 client_backend.login(user_id)
        # 假设网络请求成功，执行以下逻辑跳转：
        self.current_user = user_id
        self.show_main_page()

    # ==================== 2. 主页面 (通讯录与功能) ====================
    def show_main_page(self):
        self.clear_container()

        # 顶部用户信息
        header_frame = tk.Frame(self.container, bg="#f0f0f0")
        header_frame.pack(fill="x", pady=10)
        tk.Label(header_frame, text=f"当前用户: {self.current_user}", font=("Microsoft YaHei", 12, "bold"),
                 bg="#f0f0f0", fg="#333").pack(side="left", padx=20)
        tk.Button(header_frame, text="注销", command=self.show_login_page, relief="flat", fg="blue", bg="#f0f0f0").pack(
            side="right", padx=20)

        # 通讯录列表
        tk.Label(self.container, text="👥 我的通讯录", font=("Microsoft YaHei", 12)).pack(anchor="w", padx=20, pady=5)

        self.listbox_friends = tk.Listbox(self.container, font=("Microsoft YaHei", 11), height=10,
                                          selectbackground="#4CAF50")
        self.listbox_friends.pack(fill="x", padx=20, pady=5)

        # [TODO: 接入后端] 这里应调用 client_backend.query_online() 获取真实的在线状态
        # 此处为接口
        mock_friends = ["1,2,3"]
        for friend in mock_friends:
            self.listbox_friends.insert(tk.END, friend)

        # 底部功能按钮区
        btn_frame = tk.Frame(self.container)
        btn_frame.pack(pady=20)

        tk.Button(btn_frame, text="🎙️ 语音留言", width=12, height=2, command=self.open_voice_msg_dialog).grid(row=0,
                                                                                                              column=0,
                                                                                                              padx=10,
                                                                                                              pady=10)
        tk.Button(btn_frame, text="📞 语音通话", width=12, height=2, command=self.open_voice_call_dialog).grid(row=0,
                                                                                                              column=1,
                                                                                                              padx=10,
                                                                                                              pady=10)
        tk.Button(btn_frame, text="🌐 加入会议室", width=28, height=2, command=self.open_conference_dialog).grid(row=1,
                                                                                                                column=0,
                                                                                                                columnspan=2,
                                                                                                                pady=5)

    def get_selected_friend(self):
        """辅助函数：获取当前选中的好友"""
        selection = self.listbox_friends.curselection()
        if not selection:
            messagebox.showwarning("提示", "请先在通讯录中选择一个联系人！")
            return None
        return self.listbox_friends.get(selection[0])

    # ==================== 3. 弹出窗口 (留言/通话/会议室) ====================
    def open_voice_msg_dialog(self):
        target = self.get_selected_friend()
        if not target: return

        dialog = tk.Toplevel(self.root)
        dialog.title(f"给 {target} 留言")
        dialog.geometry("300x200")
        dialog.transient(self.root)  # 保持在主窗口之上

        tk.Label(dialog, text="🔴 准备录音...", font=("Microsoft YaHei", 12)).pack(pady=20)

        # [TODO: 接入后端] 下面三个按钮分别绑定 audio.py 的录音函数和 client_backend 的上传函数
        tk.Button(dialog, text="开始录音", bg="#ffcccc", width=15, command=lambda: print("调用: record_audio()")).pack(
            pady=5)
        tk.Button(dialog, text="停止录音", width=15, command=lambda: print("调用: stop_audio()")).pack(pady=5)
        tk.Button(dialog, text="发送留言 (TCP)", bg="#ccffcc", width=15,
                  command=lambda: [print("调用: upload_offline_msg()"), dialog.destroy()]).pack(pady=5)

    def open_voice_call_dialog(self):
        target = self.get_selected_friend()
        if not target: return

        # [TODO: 接入后端] 这里应该先判断对方是否在线，如果不在线直接 return 并弹窗提示

        dialog = tk.Toplevel(self.root)
        dialog.title("语音通话中")
        dialog.geometry("300x200")
        dialog.transient(self.root)

        tk.Label(dialog, text=f"正在与 {target} 通话...", font=("Microsoft YaHei", 12), fg="green").pack(pady=30)
        tk.Label(dialog, text="UDP 实时传输中 📶", font=("Arial", 10), fg="gray").pack(pady=5)

        # [TODO: 接入后端] 点击挂断时，需通知 UDP 线程停止发送/接收音频
        tk.Button(dialog, text="挂 断", bg="red", fg="white", width=15, height=2,
                  command=lambda: [print("停止 UDP 线程"), dialog.destroy()]).pack(pady=20)

    def open_conference_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("会议室 (IP组播)")
        dialog.geometry("300x200")
        dialog.transient(self.root)

        tk.Label(dialog, text="请输入会议室组播 IP:\n(例如: 224.1.1.1)", font=("Microsoft YaHei", 10)).pack(pady=15)
        ip_entry = tk.Entry(dialog, font=("Arial", 12), justify="center")
        ip_entry.insert(0, "224.1.1.1")
        ip_entry.pack(pady=10)

        # [TODO: 接入后端] 绑定加入组播组的逻辑
        tk.Button(dialog, text="加入组播会议", bg="#4CAF50", fg="white", width=15,
                  command=lambda: [print(f"加入组播IP: {ip_entry.get()}"), messagebox.showinfo("成功", "已加入会议室"),
                                   dialog.destroy()]).pack(pady=20)


if __name__ == "__main__":
    root = tk.Tk()
    app = VoiceChatUI(root)
    root.mainloop()