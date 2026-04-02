"""
LinkVoice 服务器管理面板 (Tkinter GUI)
======================================
通过图形界面启动/停止服务器，实时监控：
  - 在线用户列表（IP + 端口）
  - 联系人关系图
  - 活跃通话 / 待接听呼叫
  - 会议室状态
  - 服务器运行日志
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import threading
import socket
import json
import sys
import os
import time
from datetime import datetime

# 复用 server.py 中的核心模块而非复制代码
# 但 server.py 是脚本式结构，这里用 import + 猴子补丁方式接入
import server

# ==================== 颜色主题 ====================
C = {
    "bg":           "#F0F2F5",
    "header":       "#1A1A2E",
    "accent":       "#0984E3",
    "accent_dark":  "#0770C2",
    "online":       "#00B894",
    "offline":      "#B2BEC3",
    "danger":       "#D63031",
    "warn":         "#FDCB6E",
    "card":         "#FFFFFF",
    "text":         "#2D3436",
    "text_light":   "#636E72",
    "text_white":   "#FFFFFF",
    "border":       "#DFE6E9",
    "sidebar":      "#2D3436",
    "log_bg":       "#1E272E",
    "log_fg":       "#DFE6E9",
}


class ServerGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("LinkVoice 服务器管理面板")
        self.root.geometry("960x640")
        self.root.minsize(860, 540)
        self.root.configure(bg=C["bg"])

        self.server_thread = None
        self.server_running = False
        self.refresh_interval = 2000  # 刷新间隔 ms

        # 构建界面
        self._build_header()
        self._build_body()

        # 劫持 server 模块的 print 输出到 GUI 日志
        self._original_print = print
        import builtins
        builtins.print = self._gui_print

        # 启动后自动加载持久化数据
        server.load_contacts()
        self._log("[系统] 已加载联系人数据 (contacts.json)")

        # 窗口关闭
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

    # ==================== 顶部栏 ====================
    def _build_header(self):
        header = tk.Frame(self.root, bg=C["header"], height=56)
        header.pack(fill="x")
        header.pack_propagate(False)

        tk.Label(header, text="LinkVoice Server", fg=C["accent"],
                 bg=C["header"], font=("Helvetica", 15, "bold")).pack(side="left", padx=16)

        # 状态指示
        self.status_frame = tk.Frame(header, bg=C["header"])
        self.status_frame.pack(side="left", padx=20)

        self.status_dot = tk.Canvas(self.status_frame, width=12, height=12,
                                     bg=C["header"], highlightthickness=0)
        self.status_dot.pack(side="left", padx=(0, 6))
        self.status_dot.create_oval(2, 2, 10, 10, fill=C["offline"], outline=C["offline"],
                                     tags="dot")

        self.status_label = tk.Label(self.status_frame, text="已停止",
                                      fg=C["text_light"], bg=C["header"],
                                      font=("微软雅黑", 9))
        self.status_label.pack(side="left")

        # 按钮区
        btn_frame = tk.Frame(header, bg=C["header"])
        btn_frame.pack(side="right", padx=16)

        self.btn_start = tk.Button(btn_frame, text="▶ 启动服务器",
                                    bg=C["online"], fg="white",
                                    font=("微软雅黑", 9, "bold"), relief="flat",
                                    padx=14, cursor="hand2",
                                    command=self._start_server)
        self.btn_start.pack(side="left", padx=4)

        self.btn_stop = tk.Button(btn_frame, text="■ 停止服务器",
                                   bg=C["danger"], fg="white",
                                   font=("微软雅黑", 9, "bold"), relief="flat",
                                   padx=14, cursor="hand2", state="disabled",
                                   command=self._stop_server)
        self.btn_stop.pack(side="left", padx=4)

    # ==================== 主体 ====================
    def _build_body(self):
        body = tk.Frame(self.root, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=8, pady=8)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        # ===== 左栏：信息面板 =====
        left = tk.Frame(body, bg=C["bg"])
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        left.rowconfigure(0, weight=2)
        left.rowconfigure(1, weight=1)
        left.rowconfigure(2, weight=1)
        left.columnconfigure(0, weight=1)

        # -- 在线用户卡片 --
        self._build_users_card(left)

        # -- 通话状态卡片 --
        self._build_calls_card(left)

        # -- 会议室卡片 --
        self._build_rooms_card(left)

        # ===== 右栏：日志 + 联系人 =====
        right = tk.Frame(body, bg=C["bg"])
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        right.rowconfigure(0, weight=3)
        right.rowconfigure(1, weight=2)
        right.columnconfigure(0, weight=1)

        # -- 日志 --
        self._build_log_card(right)

        # -- 联系人总览 --
        self._build_contacts_card(right)

    # -------------------- 在线用户卡片 --------------------
    def _build_users_card(self, parent):
        card = tk.LabelFrame(parent, text="  在线用户  ", bg=C["card"],
                              fg=C["text"], font=("微软雅黑", 10, "bold"),
                              padx=8, pady=6)
        card.grid(row=0, column=0, sticky="nsew", pady=(0, 4))

        # 统计栏
        self.users_count_label = tk.Label(card, text="在线: 0", bg=C["card"],
                                           fg=C["accent"], font=("微软雅黑", 9, "bold"))
        self.users_count_label.pack(anchor="w")

        # Treeview
        columns = ("user", "ip", "port")
        self.users_tree = ttk.Treeview(card, columns=columns, show="headings",
                                        height=5, selectmode="browse")
        self.users_tree.heading("user", text="用户名")
        self.users_tree.heading("ip", text="IP 地址")
        self.users_tree.heading("port", text="端口")
        self.users_tree.column("user", width=90, minwidth=60)
        self.users_tree.column("ip", width=110, minwidth=80)
        self.users_tree.column("port", width=60, minwidth=40)
        self.users_tree.pack(fill="both", expand=True, pady=(4, 0))

        # 右键菜单
        self.user_menu = tk.Menu(self.root, tearoff=0)
        self.user_menu.add_command(label="踢出用户", command=self._kick_user)
        self.users_tree.bind("<Button-3>", self._show_user_menu)

    # -------------------- 通话状态卡片 --------------------
    def _build_calls_card(self, parent):
        card = tk.LabelFrame(parent, text="  通话状态  ", bg=C["card"],
                              fg=C["text"], font=("微软雅黑", 10, "bold"),
                              padx=8, pady=6)
        card.grid(row=1, column=0, sticky="nsew", pady=4)

        self.calls_text = tk.Text(card, font=("Consolas", 9), bg="#FAFAFA",
                                   fg=C["text"], height=4, wrap="word",
                                   state="disabled", bd=0, relief="flat")
        self.calls_text.pack(fill="both", expand=True)

    # -------------------- 会议室卡片 --------------------
    def _build_rooms_card(self, parent):
        card = tk.LabelFrame(parent, text="  会议室  ", bg=C["card"],
                              fg=C["text"], font=("微软雅黑", 10, "bold"),
                              padx=8, pady=6)
        card.grid(row=2, column=0, sticky="nsew", pady=(4, 0))

        self.rooms_text = tk.Text(card, font=("Consolas", 9), bg="#FAFAFA",
                                   fg=C["text"], height=4, wrap="word",
                                   state="disabled", bd=0, relief="flat")
        self.rooms_text.pack(fill="both", expand=True)

    # -------------------- 日志卡片 --------------------
    def _build_log_card(self, parent):
        card = tk.LabelFrame(parent, text="  运行日志  ", bg=C["card"],
                              fg=C["text"], font=("微软雅黑", 10, "bold"),
                              padx=4, pady=4)
        card.grid(row=0, column=0, sticky="nsew", pady=(0, 4))

        self.log_text = scrolledtext.ScrolledText(
            card, font=("Consolas", 9), bg=C["log_bg"], fg=C["log_fg"],
            insertbackground=C["log_fg"], wrap="word", bd=0, relief="flat"
        )
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

        # 日志颜色标签
        self.log_text.tag_configure("info", foreground="#74B9FF")
        self.log_text.tag_configure("warn", foreground="#FDCB6E")
        self.log_text.tag_configure("error", foreground="#FF7675")
        self.log_text.tag_configure("success", foreground="#55EFC4")
        self.log_text.tag_configure("normal", foreground=C["log_fg"])

        # 底部按钮
        log_btns = tk.Frame(card, bg=C["card"])
        log_btns.pack(fill="x", pady=(4, 0))
        tk.Button(log_btns, text="清空日志", font=("微软雅黑", 8), relief="flat",
                  fg=C["text_light"], bg=C["card"], cursor="hand2",
                  command=self._clear_log).pack(side="right")

    # -------------------- 联系人总览卡片 --------------------
    def _build_contacts_card(self, parent):
        card = tk.LabelFrame(parent, text="  联系人关系  ", bg=C["card"],
                              fg=C["text"], font=("微软雅黑", 10, "bold"),
                              padx=8, pady=6)
        card.grid(row=1, column=0, sticky="nsew", pady=(4, 0))

        columns = ("user", "contacts", "online")
        self.contacts_tree = ttk.Treeview(card, columns=columns, show="headings",
                                           height=5, selectmode="browse")
        self.contacts_tree.heading("user", text="用户")
        self.contacts_tree.heading("contacts", text="联系人")
        self.contacts_tree.heading("online", text="状态")
        self.contacts_tree.column("user", width=80, minwidth=60)
        self.contacts_tree.column("contacts", width=200, minwidth=100)
        self.contacts_tree.column("online", width=60, minwidth=40)
        self.contacts_tree.pack(fill="both", expand=True)

    # ==================== 服务器控制 ====================
    def _start_server(self):
        if self.server_running:
            return

        self.server_running = True
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.status_dot.itemconfig("dot", fill=C["online"], outline=C["online"])
        self.status_label.configure(text="运行中", fg=C["online"])

        # 在后台线程启动服务器
        self.server_thread = threading.Thread(target=self._run_server, daemon=True)
        self.server_thread.start()

        # 启动定时刷新
        self._schedule_refresh()

        self._log("[系统] 服务器启动中...", "success")

    def _run_server(self):
        """在子线程中运行 server.start_server() 的核心逻辑"""
        try:
            server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_sock.bind((server.HOST, server.PORT))
            server_sock.listen(5)

            # 获取本机 IP
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
                s.close()
            except Exception:
                local_ip = "127.0.0.1"

            self.server_sock = server_sock
            self._log(f"[系统] 服务器已启动  监听 {server.HOST}:{server.PORT}", "success")
            self._log(f"[系统] 局域网 IP: {local_ip}", "info")
            self._log(f"[系统] 客户端请连接 → {local_ip}:{server.PORT}", "info")

            server_sock.settimeout(1.0)

            while self.server_running:
                try:
                    client_sock, addr = server_sock.accept()
                    self._log(f"[连接] 新连接: {addr[0]}:{addr[1]}", "info")
                    t = threading.Thread(target=self._handle_client_wrapper,
                                         args=(client_sock, addr), daemon=True)
                    t.start()
                except socket.timeout:
                    continue
                except OSError:
                    break

        except OSError as e:
            self._log(f"[错误] 服务器启动失败: {e}", "error")
            self.root.after(0, self._reset_ui)
        finally:
            self._cleanup_server()

    def _handle_client_wrapper(self, client_sock, addr):
        """包装 handle_client，在日志中显示上下线"""
        username = None
        try:
            server.handle_client(client_sock, addr)
        except Exception as e:
            self._log(f"[错误] 处理客户端 {addr} 时出错: {e}", "error")
        finally:
            # handle_client 退出时 server 内部已调用 remove_client
            # 这里只做日志记录，不重复清理
            with server.clients_lock:
                username = server.clients.get(client_sock)
            if username:
                self._log(f"[下线] '{username}' 断开连接 ({addr[0]}:{addr[1]})", "warn")
            elif not username:
                # 已被 handle_client 内部清理，从 addr 推断
                self._log(f"[下线] 客户端 {addr[0]}:{addr[1]} 已断开", "warn")

    def _stop_server(self):
        if not self.server_running:
            return

        self._log("[系统] 正在停止服务器...", "warn")
        self.server_running = False

        # 关闭所有客户端
        with server.clients_lock:
            for sock in list(server.clients.keys()):
                try:
                    sock.close()
                except Exception:
                    pass
            server.clients.clear()

        # 关闭服务器 socket
        if hasattr(self, "server_sock"):
            try:
                self.server_sock.close()
            except Exception:
                pass

        # 关闭所有会议室
        with server.rooms_lock:
            for rid, info in server.rooms.items():
                try:
                    info["relay_sock"].close()
                except Exception:
                    pass
            server.rooms.clear()

        # 保存持久化数据
        server.save_contacts()
        server.save_rooms()

        self._log("[系统] 服务器已停止", "warn")
        self._reset_ui()

    def _cleanup_server(self):
        """服务器线程退出时的清理"""
        server.save_contacts()
        server.save_rooms()

    def _reset_ui(self):
        self.server_running = False
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.status_dot.itemconfig("dot", fill=C["offline"], outline=C["offline"])
        self.status_label.configure(text="已停止", fg=C["text_light"])

    # ==================== 定时刷新仪表盘 ====================
    def _schedule_refresh(self):
        if not self.server_running:
            return
        self._refresh_users()
        self._refresh_calls()
        self._refresh_rooms()
        self._refresh_contacts()
        self.root.after(self.refresh_interval, self._schedule_refresh)

    def _refresh_users(self):
        """刷新在线用户列表"""
        try:
            tree = self.users_tree
            tree.delete(*tree.get_children())

            with server.clients_lock:
                items = []
                for sock, uname in list(server.clients.items()):
                    try:
                        addr = sock.getpeername()
                        items.append((uname, addr[0], str(addr[1])))
                    except Exception:
                        items.append((uname, "?", "?"))

            for uname, ip, port in sorted(items, key=lambda x: x[0]):
                tree.insert("", "end", values=(uname, ip, port))

            self.users_count_label.configure(text=f"在线: {len(items)}")
        except Exception:
            pass

    def _refresh_calls(self):
        """刷新通话状态"""
        try:
            self.calls_text.configure(state="normal")
            self.calls_text.delete("1.0", "end")

            lines = []

            # 活跃通话
            with server.active_calls_lock:
                shown = set()
                for user, val in list(server.active_calls.items()):
                    if not isinstance(val, (tuple, list)) or len(val) < 2:
                        continue
                    peer, mode = val[0], val[1]
                    pair = tuple(sorted([user, peer]))
                    if pair in shown:
                        continue
                    shown.add(pair)
                    mode_str = "TCP 模式" if mode == "TCP_MODE" else "UDP 中继"
                    lines.append(f"📞 {pair[0]} ↔ {pair[1]}  [{mode_str}]")

            # 待接听呼叫
            with server.pending_calls_lock:
                for caller, info in list(server.pending_calls.items()):
                    target = info.get("target", "?") if isinstance(info, dict) else "?"
                    lines.append(f"🔔 {caller} → {target}  [等待接听]")

            if not lines:
                lines.append("暂无通话")

            self.calls_text.insert("end", "\n".join(lines))
            self.calls_text.configure(state="disabled")
        except Exception:
            pass

    def _refresh_rooms(self):
        """刷新会议室信息"""
        try:
            self.rooms_text.configure(state="normal")
            self.rooms_text.delete("1.0", "end")

            lines = []
            with server.rooms_lock:
                for rid, info in list(server.rooms.items()):
                    members = list(info.get("members", {}).keys())
                    port = info.get("port", "?")
                    lines.append(f"🏠 {rid}  端口:{port}  成员({len(members)}): {', '.join(members)}")

            if not lines:
                lines.append("暂无会议室")

            self.rooms_text.insert("end", "\n".join(lines))
            self.rooms_text.configure(state="disabled")
        except Exception:
            pass

    def _refresh_contacts(self):
        """刷新联系人关系表"""
        try:
            tree = self.contacts_tree
            tree.delete(*tree.get_children())

            with server.contacts_lock:
                data = dict(server.contacts)

            for user, clist in sorted(data.items()):
                online = server.is_user_online(user)
                status = "在线" if online else "离线"
                contacts_str = ", ".join(clist) if clist else "(空)"
                tree.insert("", "end", values=(user, contacts_str, status))
        except Exception:
            pass

    # ==================== 用户操作 ====================
    def _show_user_menu(self, event):
        item = self.users_tree.identify_row(event.y)
        if item:
            self.users_tree.selection_set(item)
            self.user_menu.post(event.x_root, event.y_root)

    def _kick_user(self):
        """踢出选中的用户"""
        sel = self.users_tree.selection()
        if not sel:
            return
        values = self.users_tree.item(sel[0], "values")
        username = values[0]

        if not messagebox.askyesno("确认", f"确定踢出用户 '{username}'？"):
            return

        with server.clients_lock:
            target_sock = None
            for sock, uname in server.clients.items():
                if uname == username:
                    target_sock = sock
                    break

        if target_sock:
            try:
                target_sock.sendall(f"[{server.timestamp()}] [系统] 您已被管理员踢出服务器\n".encode(server.ENCODING))
            except Exception:
                pass
            server.remove_client(target_sock)
            try:
                target_sock.close()
            except Exception:
                pass
            self._log(f"[管理] 已踢出用户 '{username}'", "warn")

    # ==================== 日志 ====================
    def _log(self, text, tag="normal"):
        """往日志区追加一行（线程安全）"""
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {text}\n"

        def _do():
            try:
                self.log_text.configure(state="normal")
                self.log_text.insert("end", line, tag)
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
            except Exception:
                pass

        try:
            self.root.after(0, _do)
        except Exception:
            pass

    def _gui_print(self, *args, **kwargs):
        """拦截 print() 调用，同时输出到控制台和 GUI 日志"""
        # 仍然打印到控制台
        self._original_print(*args, **kwargs)
        # 转发到 GUI 日志（窗口可能已销毁）
        try:
            text = " ".join(str(a) for a in args)
            if "错误" in text or "失败" in text:
                tag = "error"
            elif "警告" in text:
                tag = "warn"
            else:
                tag = "normal"
            self._log(text, tag)
        except Exception:
            pass

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # ==================== 关闭 ====================
    def _on_closing(self):
        if self.server_running:
            if not messagebox.askyesno("确认", "服务器正在运行，确定关闭？"):
                return
            self._stop_server()

        # 恢复 print
        import builtins
        builtins.print = self._original_print

        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = ServerGUI(root)
    root.mainloop()
