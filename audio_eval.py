"""
网络音频质量评测模块
==================
实时评测 UDP 音频传输的丢包率、时延、抖动、乱序，
按标准评分机制输出综合评分（满分 100）。

支持：
    1) 结束时输出汇总报告（txt）
    2) 评测过程中每秒输出一次实时结果（csv）

帧格式（变长）:
  [PRIORITY 1B][MAGIC 4B: 0xAD100200][JSON_LEN 4B big-endian][JSON_UTF8 N字节][PCM M字节]
  PRIORITY: 包重要性（0=低 / 1=普通 / 2=高），位于 UDP 负载第 0 字节。
  JSON 字段: {"v":版本, "sid":发送者ID, "seq":序号, "ts":发送时间戳, ...可扩展}
  ts 字段保存发送方 Unix 时间戳，接收方据此计算单向时延。

评分标准:
  丢包 40 分 | 时延 25 分 | 抖动 20 分 | 乱序 15 分
"""

import csv
import json as _json
import struct
import time
import threading
import zlib
from collections import deque
from datetime import datetime
from pathlib import Path

# ===================== UDP 音频报头 =====================

# 帧格式: [PRIORITY 1B][MAGIC 4B][JSON_LEN 4B big-endian][JSON_UTF8 NB][PCM MB]
# JSON 最小字段: {"v": 1, "sid": <uint32>, "seq": <uint32>, "ts": <float>}
# 可在 JSON 中自由追加字段，不影响解析逻辑

# 包重要性常量
PRIORITY_LOW    = 0   # 低优先级（背景/次要音频）
PRIORITY_NORMAL = 1   # 普通语音
PRIORITY_HIGH   = 2   # 高优先级（强语音活动）

PACKET_MAGIC      = b'\xAD\x10\x02\x00'   # 4 字节 magic，区分旧 2 字节格式
_PRIORITY_SIZE    = 1                      # 重要性字段：UDP 负载第 0 字节
_MAGIC_SIZE       = len(PACKET_MAGIC)      # 4，起始偏移 = _PRIORITY_SIZE
_LEN_FIELD_SIZE   = 4                      # JSON 长度字段：4 字节 big-endian uint32
_MAGIC_OFFSET     = _PRIORITY_SIZE         # 1
_JSON_LEN_OFFSET  = _MAGIC_OFFSET + _MAGIC_SIZE   # 5
_HEADER_PREFIX_SIZE = _PRIORITY_SIZE + _MAGIC_SIZE + _LEN_FIELD_SIZE  # 9
DEFAULT_EVAL_DIR = "eval_net"


def _make_timestamped_output_paths(base_dir=DEFAULT_EVAL_DIR):
    """生成默认输出路径：./eval_net 下带时间戳的报告与 CSV 文件名。"""
    out_dir = Path(base_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = out_dir / f"audio_quality_report_{ts}.txt"
    csv_path = out_dir / f"audio_quality_realtime_{ts}.csv"

    suffix = 1
    while report_path.exists() or csv_path.exists():
        report_path = out_dir / f"audio_quality_report_{ts}_{suffix}.txt"
        csv_path = out_dir / f"audio_quality_realtime_{ts}_{suffix}.csv"
        suffix += 1

    return str(report_path), str(csv_path)


def make_sender_id(username):
    """根据用户名生成 4 字节发送者标识"""
    return zlib.crc32(username.encode()) & 0xFFFFFFFF


def pack_audio_header(sender_id: int, seq: int, timestamp: float,
                      priority: int = PRIORITY_NORMAL, **extra) -> bytes:
    """
    将音频包元数据序列化为带优先级字节的 JSON 帧头，返回可直接拼接 PCM 的字节串。
    帧格式：[PRIORITY 1B][MAGIC 4B][JSON_LEN 4B big-endian][JSON_UTF8 NB]
    priority: 包重要性 (0=低, 1=普通, 2=高)；extra 中的额外字段会合并进 JSON。
    """
    meta = {"v": 1, "sid": sender_id, "seq": seq, "ts": round(timestamp, 6)}
    meta.update(extra)
    json_bytes = _json.dumps(meta, separators=(",", ":")).encode("utf-8")
    prio_byte = bytes([max(0, min(255, int(priority)))])
    prefix = prio_byte + PACKET_MAGIC + struct.pack("!I", len(json_bytes))
    return prefix + json_bytes


def unpack_audio_header(data: bytes):
    """
    解析音频 JSON 帧头（含首字节优先级字段）。
    返回 (sender_id, seq, send_ts, priority, pcm_data, meta)。
    若不含有效帧头（magic 不匹配或数据不完整），返回 (None, None, None, None, 原始data, {})。
    """
    if (len(data) < _HEADER_PREFIX_SIZE or
            data[_MAGIC_OFFSET:_MAGIC_OFFSET + _MAGIC_SIZE] != PACKET_MAGIC):
        return None, None, None, None, data, {}
    priority = data[0]
    json_len = struct.unpack("!I", data[_JSON_LEN_OFFSET:_JSON_LEN_OFFSET + _LEN_FIELD_SIZE])[0]
    total_header = _HEADER_PREFIX_SIZE + json_len
    if len(data) < total_header:
        return None, None, None, None, data, {}
    try:
        meta = _json.loads(data[_HEADER_PREFIX_SIZE:total_header].decode("utf-8"))
        pcm_data = data[total_header:]
        return meta.get("sid"), meta.get("seq"), meta.get("ts"), priority, pcm_data, meta
    except Exception:
        return None, None, None, None, data, {}


# ===================== 单源指标追踪器 =====================

class _SourceTracker:
    """追踪来自同一发送者的包序列，计算丢包、时延、抖动、乱序"""

    def __init__(self):
        self.highest_seq = None
        self.last_seq = None
        self.total_expected = 0
        self.total_received = 0
        self.reorder_count = 0
        self.delay_sum_ms = 0.0
        self.delay_count = 0
        self.last_recv_time = None
        self.last_send_time = None
        self.jitter_sum_ms = 0.0
        self.jitter_count = 0

    def record(self, seq, send_ts, recv_time=None):
        """
        记录一个数据包。
        返回该包对统计量的增量，用于“每秒窗口统计”。
        """
        if recv_time is None:
            recv_time = time.time()

        delta = {
            "expected": 0,
            "received": 1,
            "reorder": 0,
            "delay_sum_ms": 0.0,
            "delay_count": 0,
            "jitter_sum_ms": 0.0,
            "jitter_count": 0,
        }

        self.total_received += 1

        # 使用“最高序号增量”累计期望收包数，可增量统计无需遍历列表
        if self.highest_seq is None:
            expected_inc = 1
            self.highest_seq = seq
        elif seq > self.highest_seq:
            expected_inc = seq - self.highest_seq
            self.highest_seq = seq
        else:
            expected_inc = 0

        self.total_expected += expected_inc
        delta["expected"] = expected_inc

        # 时延
        delay_ms = (recv_time - send_ts) * 1000
        if delay_ms >= 0:
            self.delay_sum_ms += delay_ms
            self.delay_count += 1
            delta["delay_sum_ms"] = delay_ms
            delta["delay_count"] = 1

        # 抖动 (RFC 3550 风格)
        if self.last_recv_time is not None and self.last_send_time is not None:
            d_recv = recv_time - self.last_recv_time
            d_send = send_ts - self.last_send_time
            jitter_ms = abs(d_recv - d_send) * 1000
            self.jitter_sum_ms += jitter_ms
            self.jitter_count += 1
            delta["jitter_sum_ms"] = jitter_ms
            delta["jitter_count"] = 1

        self.last_recv_time = recv_time
        self.last_send_time = send_ts

        # 乱序检测
        if self.last_seq is not None and seq < self.last_seq:
            self.reorder_count += 1
            delta["reorder"] = 1

        self.last_seq = seq

        return delta

    def expected_count(self):
        return self.total_expected


# ===================== 质量评测器 =====================

class AudioQualityEvaluator:
    def __init__(self):
        self._lock = threading.Lock()
        self.active = False
        self._sources = {}          # {sender_id: _SourceTracker}
        self._report_path = ""
        self._csv_path = ""
        self._interval_sec = 1.0
        self._tick_callback = None
        self._start_time = None
        self._monitor_thread = None
        self._stop_event = threading.Event()
        self._ticks_written = 0

        # 每秒窗口统计，静音时该窗口会保持全 0
        self._window_stats = self._blank_stats()

        # 自适应控制使用的滚动窗口统计（无论是否显式开启评测都持续维护）
        self._live_window_sec = 6.0
        self._live_sources = {}          # {sender_id: _SourceTracker}
        self._live_events = deque()      # [(recv_time, sender_id, delta_stats), ...]
        self._live_window_stats = self._blank_stats()
        self._live_sender_stats = {}     # {sender_id: stats_dict}
        self._live_sender_last_recv = {} # {sender_id: recv_time}

    def _reset_live_state_unlocked(self):
        self._live_sources.clear()
        self._live_events.clear()
        self._live_window_stats = self._blank_stats()
        self._live_sender_stats.clear()
        self._live_sender_last_recv.clear()

    @staticmethod
    def _blank_stats():
        return {
            "expected": 0,
            "received": 0,
            "reorder": 0,
            "delay_sum_ms": 0.0,
            "delay_count": 0,
            "jitter_sum_ms": 0.0,
            "jitter_count": 0,
        }

    @staticmethod
    def _copy_stats(stats):
        return {
            "expected": stats["expected"],
            "received": stats["received"],
            "reorder": stats["reorder"],
            "delay_sum_ms": stats["delay_sum_ms"],
            "delay_count": stats["delay_count"],
            "jitter_sum_ms": stats["jitter_sum_ms"],
            "jitter_count": stats["jitter_count"],
        }

    @staticmethod
    def _merge_stats(target, delta, sign=1):
        target["expected"] += sign * delta["expected"]
        target["received"] += sign * delta["received"]
        target["reorder"] += sign * delta["reorder"]
        target["delay_sum_ms"] += sign * delta["delay_sum_ms"]
        target["delay_count"] += sign * delta["delay_count"]
        target["jitter_sum_ms"] += sign * delta["jitter_sum_ms"]
        target["jitter_count"] += sign * delta["jitter_count"]

    @staticmethod
    def _stats_is_zero(stats):
        return (
            stats["expected"] <= 0
            and stats["received"] <= 0
            and stats["reorder"] <= 0
            and stats["delay_count"] <= 0
            and stats["jitter_count"] <= 0
            and abs(stats["delay_sum_ms"]) < 1e-9
            and abs(stats["jitter_sum_ms"]) < 1e-9
        )

    @staticmethod
    def _has_stats_activity(stats):
        return (
            stats["expected"] > 0
            or stats["received"] > 0
            or stats["reorder"] > 0
            or stats["delay_count"] > 0
            or stats["jitter_count"] > 0
        )

    def _trim_live_events(self, now):
        expire_before = now - self._live_window_sec
        while self._live_events and self._live_events[0][0] < expire_before:
            _, sender_id, delta = self._live_events.popleft()
            self._merge_stats(self._live_window_stats, delta, sign=-1)
            sender_stats = self._live_sender_stats.get(sender_id)
            if sender_stats is not None:
                self._merge_stats(sender_stats, delta, sign=-1)
                if self._stats_is_zero(sender_stats):
                    self._live_sender_stats.pop(sender_id, None)
                    last_recv = self._live_sender_last_recv.get(sender_id)
                    if last_recv is not None and last_recv < expire_before:
                        self._live_sender_last_recv.pop(sender_id, None)

    def _compute_recent_score_average(self, now, sender_id=None, window_sec=5.0):
        if window_sec <= 0:
            return None, 0

        bucket_stats = {}
        bucket_cutoff = int(now - window_sec)
        for recv_time, event_sender_id, delta in self._live_events:
            if recv_time < now - window_sec:
                continue
            if sender_id is not None and event_sender_id != sender_id:
                continue
            bucket_key = int(recv_time)
            if bucket_key < bucket_cutoff:
                continue
            stats = bucket_stats.setdefault(bucket_key, self._blank_stats())
            self._merge_stats(stats, delta)

        if not bucket_stats:
            return None, 0

        scores = []
        for stats in bucket_stats.values():
            if not self._has_stats_activity(stats):
                continue
            metrics = self._metrics_from_stats(stats)
            scores.append(self.score_total(
                metrics["loss_rate"],
                metrics["avg_delay_ms"],
                metrics["avg_jitter_ms"],
                metrics["reorder_rate"],
            ))

        if not scores:
            return None, 0
        return sum(scores) / len(scores), len(scores)

    def _build_live_snapshot(self, stats, now, sender_id=None):
        metrics = self._metrics_from_stats(stats)
        metrics["score"] = self.score_total(
            metrics["loss_rate"],
            metrics["avg_delay_ms"],
            metrics["avg_jitter_ms"],
            metrics["reorder_rate"],
        )
        avg_score_5s, score_samples_5s = self._compute_recent_score_average(now, sender_id=sender_id, window_sec=5.0)
        if sender_id is None:
            active_ages = []
            for sid in self._live_sender_stats:
                last_recv = self._live_sender_last_recv.get(sid)
                if last_recv is not None:
                    active_ages.append(max(0.0, now - last_recv))
            last_packet_age = min(active_ages) if active_ages else None
        else:
            last_recv = self._live_sender_last_recv.get(sender_id)
            last_packet_age = None if last_recv is None else max(0.0, now - last_recv)

        metrics["window_sec"] = self._live_window_sec
        metrics["avg_score_5s"] = avg_score_5s
        metrics["score_samples_5s"] = score_samples_5s
        metrics["last_packet_age_sec"] = last_packet_age
        metrics["active"] = (
            last_packet_age is not None
            and last_packet_age <= self._live_window_sec
            and metrics["total_received"] > 0
        )
        return metrics

    def get_live_snapshot(self, sender_id=None):
        with self._lock:
            now = time.time()
            self._trim_live_events(now)
            if sender_id is None:
                stats = self._copy_stats(self._live_window_stats)
            else:
                stats = self._copy_stats(self._live_sender_stats.get(sender_id, self._blank_stats()))
            return self._build_live_snapshot(stats, now, sender_id=sender_id)

    def reset_live_state(self):
        with self._lock:
            self._reset_live_state_unlocked()

    # ---------- 控制 ----------

    def start(
        self,
        report_path=None,
        csv_path=None,
        interval_sec=1.0,
        tick_callback=None,
    ):
        # 重复启动时，先优雅停止旧评测线程，避免线程泄漏
        if self.is_active():
            self.stop()

        interval_sec = max(0.1, float(interval_sec))
        default_report_path, default_csv_path = _make_timestamped_output_paths()
        report_path = str(Path(report_path)) if report_path else default_report_path
        csv_path = str(Path(csv_path)) if csv_path else default_csv_path

        # 自定义路径也确保父目录存在
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(csv_path).parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            self.active = True
            self._sources.clear()
            self._window_stats = self._blank_stats()
            self._report_path = report_path
            self._csv_path = csv_path
            self._interval_sec = interval_sec
            self._tick_callback = tick_callback
            self._start_time = time.time()
            self._stop_event.clear()
            self._ticks_written = 0

            # 覆盖写入：每次新评测创建新的实时 CSV 文件
            try:
                with open(self._csv_path, "w", newline="", encoding="utf-8-sig") as f:
                    writer = csv.writer(f)
                    writer.writerow(["时刻", "丢包率", "时延", "抖动", "乱序", "总分"])
            except Exception:
                pass

            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                name="AudioQualityEvaluatorMonitor",
                daemon=True,
            )
            self._monitor_thread.start()

    def stop(self):
        monitor_thread = None

        with self._lock:
            if not self.active:
                return ""
            self.active = False
            self._stop_event.set()
            monitor_thread = self._monitor_thread

        if monitor_thread and monitor_thread.is_alive():
            monitor_thread.join(timeout=max(1.0, self._interval_sec + 0.5))

        # 结束时补写最后一个窗口：仅在还未写过任何行或该窗口有活动时追加
        final_row = None
        with self._lock:
            need_final_row = self._ticks_written == 0 or self._has_stats_activity(self._window_stats)
            if need_final_row:
                final_row = self._build_realtime_row_from_stats(self._window_stats)
            self._window_stats = self._blank_stats()

        if final_row is not None:
            self._append_csv_row(final_row)

        with self._lock:
            report = self._generate_report()
            try:
                with open(self._report_path, "a", encoding="utf-8") as f:
                    f.write(report)
            except Exception:
                pass
            self._monitor_thread = None
            self._tick_callback = None
            return report

    def get_output_paths(self):
        with self._lock:
            return self._report_path, self._csv_path

    def is_active(self):
        with self._lock:
            return self.active

    # ---------- 记录 ----------

    def record_packet(self, sender_id, seq, send_ts):
        with self._lock:
            now = time.time()

            if sender_id not in self._live_sources:
                self._live_sources[sender_id] = _SourceTracker()
            live_delta = self._live_sources[sender_id].record(seq, send_ts, recv_time=now)
            self._trim_live_events(now)
            self._live_events.append((now, sender_id, live_delta))
            self._merge_stats(self._live_window_stats, live_delta)
            if sender_id not in self._live_sender_stats:
                self._live_sender_stats[sender_id] = self._blank_stats()
            self._merge_stats(self._live_sender_stats[sender_id], live_delta)
            self._live_sender_last_recv[sender_id] = now

            if not self.active:
                return
            if sender_id not in self._sources:
                self._sources[sender_id] = _SourceTracker()
            delta = self._sources[sender_id].record(seq, send_ts, recv_time=now)

            # 同步累计到“每秒窗口统计”
            self._window_stats["expected"] += delta["expected"]
            self._window_stats["received"] += delta["received"]
            self._window_stats["reorder"] += delta["reorder"]
            self._window_stats["delay_sum_ms"] += delta["delay_sum_ms"]
            self._window_stats["delay_count"] += delta["delay_count"]
            self._window_stats["jitter_sum_ms"] += delta["jitter_sum_ms"]
            self._window_stats["jitter_count"] += delta["jitter_count"]

    # ---------- 评分函数 ----------

    @staticmethod
    def score_loss(rate):
        """丢包评分 (满分 40)"""
        if rate <= 0.02: return 40
        if rate <= 0.05: return 30
        if rate <= 0.10: return 20
        if rate <= 0.20: return 10
        return 0

    @staticmethod
    def score_delay(ms):
        """时延评分 (满分 25)"""
        if ms <= 200:  return 25
        if ms <= 500:  return 20
        if ms <= 750:  return 15
        if ms <= 1000: return 10
        if ms <= 2000: return 5
        return 0

    @staticmethod
    def score_jitter(ms):
        """抖动评分 (满分 20)"""
        if ms <= 20:  return 20
        if ms <= 50:  return 15
        if ms <= 100: return 10
        if ms <= 150: return 5
        return 0

    @staticmethod
    def score_reorder(rate):
        """乱序评分 (满分 15)"""
        if rate <= 0.02: return 15
        if rate <= 0.05: return 12
        if rate <= 0.10: return 8
        if rate <= 0.20: return 4
        return 0

    @classmethod
    def score_total(cls, loss_rate, delay_ms, jitter_ms, reorder_rate):
        return (
            cls.score_loss(loss_rate)
            + cls.score_delay(delay_ms)
            + cls.score_jitter(jitter_ms)
            + cls.score_reorder(reorder_rate)
        )

    # ---------- 聚合计算 ----------

    def _metrics_from_stats(self, stats):
        expected = stats["expected"]
        received = stats["received"]
        reorder = stats["reorder"]

        # 静音/无包场景：expected=0, received=0 时按 0 劣化处理，不报错
        loss_rate = max(0.0, 1.0 - received / expected) if expected > 0 else 0.0
        avg_delay = stats["delay_sum_ms"] / stats["delay_count"] if stats["delay_count"] > 0 else 0.0
        avg_jitter = stats["jitter_sum_ms"] / stats["jitter_count"] if stats["jitter_count"] > 0 else 0.0
        reorder_rate = reorder / received if received > 0 else 0.0

        return {
            "total_expected": expected,
            "total_received": received,
            "loss_rate": loss_rate,
            "avg_delay_ms": avg_delay,
            "avg_jitter_ms": avg_jitter,
            "reorder_count": reorder,
            "reorder_rate": reorder_rate,
        }

    def _build_realtime_row_from_stats(self, stats):
        m = self._metrics_from_stats(stats)
        total = self.score_total(
            m["loss_rate"],
            m["avg_delay_ms"],
            m["avg_jitter_ms"],
            m["reorder_rate"],
        )
        return {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "loss_rate": m["loss_rate"],
            "avg_delay_ms": m["avg_delay_ms"],
            "avg_jitter_ms": m["avg_jitter_ms"],
            "reorder_rate": m["reorder_rate"],
            "score": total,
        }

    def _append_csv_row(self, row):
        try:
            with open(self._csv_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    row["timestamp"],
                    f"{row['loss_rate'] * 100:.2f}",
                    f"{row['avg_delay_ms']:.2f}",
                    f"{row['avg_jitter_ms']:.2f}",
                    f"{row['reorder_rate'] * 100:.2f}",
                    row["score"],
                ])
        except Exception:
            pass

    def _monitor_loop(self):
        while not self._stop_event.wait(self._interval_sec):
            callback = None
            row = None
            with self._lock:
                if not self.active:
                    break
                row = self._build_realtime_row_from_stats(self._window_stats)
                self._window_stats = self._blank_stats()
                callback = self._tick_callback
                self._ticks_written += 1

            self._append_csv_row(row)

            if callback is not None:
                try:
                    callback(row)
                except Exception:
                    pass

    def _aggregate_metrics(self):
        total_stats = self._blank_stats()

        for tracker in self._sources.values():
            total_stats["expected"] += tracker.expected_count()
            total_stats["received"] += tracker.total_received
            total_stats["reorder"] += tracker.reorder_count
            total_stats["delay_sum_ms"] += tracker.delay_sum_ms
            total_stats["delay_count"] += tracker.delay_count
            total_stats["jitter_sum_ms"] += tracker.jitter_sum_ms
            total_stats["jitter_count"] += tracker.jitter_count

        return self._metrics_from_stats(total_stats)

    # ---------- 报告生成 ----------

    def _generate_report(self):
        m = self._aggregate_metrics()
        s_loss    = self.score_loss(m["loss_rate"])
        s_delay   = self.score_delay(m["avg_delay_ms"])
        s_jitter  = self.score_jitter(m["avg_jitter_ms"])
        s_reorder = self.score_reorder(m["reorder_rate"])
        total     = s_loss + s_delay + s_jitter + s_reorder

        duration = time.time() - self._start_time if self._start_time else 0

        report = (
            "\n======================================\n"
            "  网络音频质量评测报告\n"
            "======================================\n"
            f"  评测时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"  持续时长: {duration:.1f} 秒\n"
            f"  数据源数: {len(self._sources)}\n"
            f"  实时CSV:  {self._csv_path}\n"
            "\n--- 原始指标 ---\n"
            f"  期望收包数: {m['total_expected']}\n"
            f"  实际收包数: {m['total_received']}\n"
            f"  丢包率:     {m['loss_rate']*100:.2f}%\n"
            f"  平均时延:   {m['avg_delay_ms']:.2f} ms\n"
            f"  平均抖动:   {m['avg_jitter_ms']:.2f} ms\n"
            f"  乱序包数:   {m['reorder_count']}\n"
            f"  乱序率:     {m['reorder_rate']*100:.2f}%\n"
            "\n--- 评分标准 ---\n"
            "  丢包(40分): <=2%:40 | <=5%:30 | <=10%:20 | <=20%:10 | >20%:0\n"
            "  时延(25分): <=200ms:25 | <=500ms:20 | <=750ms:15 | <=1s:10 | <=2s:5 | >2s:0\n"
            "  抖动(20分): <=20ms:20 | <=50ms:15 | <=100ms:10 | <=150ms:5 | >150ms:0\n"
            "  乱序(15分): <=2%:15 | <=5%:12 | <=10%:8 | <=20%:4 | >20%:0\n"
            "\n--- 分项评分 ---\n"
            f"  丢包评分:   {s_loss} / 40\n"
            f"  时延评分:   {s_delay} / 25\n"
            f"  抖动评分:   {s_jitter} / 20\n"
            f"  乱序评分:   {s_reorder} / 15\n"
            "\n--- 综合评分 ---\n"
            f"  ★ 总分: {total} / 100\n"
            "======================================\n\n"
        )
        return report


# ===================== 全局实例与便捷接口 =====================

evaluator = AudioQualityEvaluator()


def start_evaluation(
    report_path=None,
    csv_path=None,
    interval_sec=1.0,
    tick_callback=None,
):
    """
    开启音频质量评测。

    参数：
        report_path: 停止评测时输出汇总报告路径；None 时自动保存到 ./eval_net 并带时间戳
        csv_path:    实时结果 CSV 路径（每 interval_sec 追加一行）；None 时自动保存到 ./eval_net 并带时间戳
      interval_sec:实时反馈间隔秒数，默认 1 秒
      tick_callback: 每次实时采样后的回调，参数为 dict
    """
    evaluator.start(
        report_path=report_path,
        csv_path=csv_path,
        interval_sec=interval_sec,
        tick_callback=tick_callback,
    )
    return evaluator.get_output_paths()


def get_evaluation_output_paths():
    """返回当前评测的输出文件路径：(report_path, csv_path)"""
    return evaluator.get_output_paths()


def get_live_evaluation_snapshot(sender_id=None):
    """返回最近滚动窗口内的实时网络质量快照，可按发送者区分。"""
    return evaluator.get_live_snapshot(sender_id=sender_id)


def reset_live_evaluation_snapshot():
    """清空自适应控制使用的滚动窗口统计。"""
    evaluator.reset_live_state()


def stop_evaluation():
    """结束音频质量评测，返回评测报告文本"""
    return evaluator.stop()
