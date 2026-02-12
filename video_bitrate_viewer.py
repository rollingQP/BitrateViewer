#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频比特率分析器
使用 FFmpeg/FFprobe 分析视频比特率并绘制曲线图
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import subprocess
import json
import os
import sys
import platform
import threading
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
import re
import math
import time as time_module
import bisect
import ctypes
import tempfile


# ============ 全局变量（用于多进程） ============

_shared_affinity_mask = None
_last_applied_mask = 0


def _worker_init(shared_mask):
    """Worker 进程初始化函数"""
    global _shared_affinity_mask
    _shared_affinity_mask = shared_mask
    _apply_current_affinity(force=True)


def _apply_current_affinity(force=False):
    """应用当前的亲和性设置"""
    global _shared_affinity_mask, _last_applied_mask
    
    if _shared_affinity_mask is None:
        return False
    
    mask = _shared_affinity_mask.value
    if mask == 0:
        return False
    
    if not force and mask == _last_applied_mask:
        return True
    
    if platform.system() == "Windows":
        try:
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            if ctypes.sizeof(ctypes.c_void_p) == 8:
                DWORD_PTR = ctypes.c_uint64
            else:
                DWORD_PTR = ctypes.c_uint32
            
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.SetProcessAffinityMask.restype = ctypes.c_int
            kernel32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, DWORD_PTR]
            
            process = kernel32.GetCurrentProcess()
            result = kernel32.SetProcessAffinityMask(process, DWORD_PTR(mask))
            
            if result:
                old_mask = _last_applied_mask
                _last_applied_mask = mask
                if old_mask != mask:
                    print(f"[Worker PID {os.getpid()}] 亲和性切换: {hex(old_mask)} -> {hex(mask)}")
                return True
        except Exception as e:
            print(f"[Worker PID {os.getpid()}] 亲和性设置失败: {e}")
            return False
    return False


def _calculate_chunk(frame_data, time_points, window_size):
    """计算比特率（多进程）"""
    _apply_current_affinity()
    
    results = []
    total = len(time_points)
    
    for i, t in enumerate(time_points):
        total_bits = sum(
            size * 8 for pts, size in frame_data
            if t <= pts < t + window_size
        )
        bitrate_kbps = total_bits / window_size / 1000
        results.append((t + window_size / 2, bitrate_kbps))
        
        if i > 0 and i % 200 == 0:
            _apply_current_affinity()
    
    return results


# ============ CPU 亲和性管理器 ============

class CPUAffinityManager:
    """CPU 亲和性管理器"""
    
    def __init__(self):
        self.supported = False
        self.all_cores_mask = 0
        self.e_cores_mask = 0
        self.p_cores_mask = 0
        self.e_core_count = 0
        self.p_core_count = 0
        self.total_cores = 0
        self.has_hybrid_arch = False
        self.detection_method = "none"
        self.efficiency_classes = {}
        
        self.current_target_mask = 0
        self.current_target_type = "all"
        
        self.shared_mask = None
        self._init_shared_mask()
        
        self._setup_windows_types()
        self._get_basic_core_count()
        
        if platform.system() == "Windows":
            self._detect_windows()
    
    def _init_shared_mask(self):
        try:
            self.shared_mask = multiprocessing.Value('Q', 0, lock=True)
        except Exception as e:
            print(f"[CPU] 共享变量创建失败: {e}")
            self.shared_mask = None
    
    def _setup_windows_types(self):
        if platform.system() != "Windows":
            return
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            self.DWORD_PTR = ctypes.c_uint64
        else:
            self.DWORD_PTR = ctypes.c_uint32
    
    def _get_basic_core_count(self):
        try:
            self.total_cores = multiprocessing.cpu_count()
        except:
            try:
                self.total_cores = os.cpu_count() or 1
            except:
                self.total_cores = 1
    
    def _detect_windows(self):
        try:
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.GetCurrentProcess.argtypes = []
            kernel32.GetProcessAffinityMask.restype = ctypes.c_int
            kernel32.GetProcessAffinityMask.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(self.DWORD_PTR), ctypes.POINTER(self.DWORD_PTR)
            ]
            
            process = kernel32.GetCurrentProcess()
            process_mask = self.DWORD_PTR()
            system_mask = self.DWORD_PTR()
            
            result = kernel32.GetProcessAffinityMask(
                process, ctypes.byref(process_mask), ctypes.byref(system_mask)
            )
            
            if result:
                self.all_cores_mask = system_mask.value
                mask_cores = bin(self.all_cores_mask).count('1')
                if mask_cores > 0:
                    self.total_cores = mask_cores
                print(f"[CPU] 系统掩码: {hex(self.all_cores_mask)}, 核心数: {self.total_cores}")
            else:
                self.all_cores_mask = (1 << self.total_cores) - 1
            
            self.current_target_mask = self.all_cores_mask
            self.current_target_type = "all"
            self._update_shared_mask(self.all_cores_mask)
            
            success = self._detect_via_cpuset_info()
            if not success:
                success = self._detect_via_power_info()
            
            self.supported = self.has_hybrid_arch and self.e_core_count > 0 and self.p_core_count > 0
            
            if self.supported:
                print(f"[CPU] 检测到混合架构: {self.p_core_count}P + {self.e_core_count}E")
                print(f"[CPU] P核掩码: {hex(self.p_cores_mask)}, E核掩码: {hex(self.e_cores_mask)}")
            else:
                print(f"[CPU] 标准架构: {self.total_cores} 核心")
            
        except Exception as e:
            print(f"[CPU] 检测错误: {e}")
            self.supported = False
            if self.all_cores_mask == 0:
                self.all_cores_mask = (1 << self.total_cores) - 1
            self.current_target_mask = self.all_cores_mask
            self._update_shared_mask(self.all_cores_mask)
    
    def _detect_via_cpuset_info(self):
        try:
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            try:
                get_cpu_set_info = kernel32.GetSystemCpuSetInformation
            except AttributeError:
                return False
            
            class SYSTEM_CPU_SET_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("Size", ctypes.c_ulong), ("Type", ctypes.c_ulong),
                    ("Id", ctypes.c_ulong), ("Group", ctypes.c_ushort),
                    ("LogicalProcessorIndex", ctypes.c_ubyte), ("CoreIndex", ctypes.c_ubyte),
                    ("LastLevelCacheIndex", ctypes.c_ubyte), ("NumaNodeIndex", ctypes.c_ubyte),
                    ("EfficiencyClass", ctypes.c_ubyte), ("AllFlags", ctypes.c_ubyte),
                    ("Reserved", ctypes.c_ulong), ("AllocationTag", ctypes.c_ulonglong),
                ]
            
            required_size = ctypes.c_ulong(0)
            get_cpu_set_info(None, 0, ctypes.byref(required_size), None, 0)
            
            if required_size.value == 0:
                return False
            
            buffer = (ctypes.c_ubyte * required_size.value)()
            result = get_cpu_set_info(
                ctypes.cast(buffer, ctypes.c_void_p),
                required_size.value, ctypes.byref(required_size), None, 0
            )
            
            if not result:
                return False
            
            self.efficiency_classes = {}
            offset = 0
            struct_size = ctypes.sizeof(SYSTEM_CPU_SET_INFORMATION)
            
            while offset + struct_size <= required_size.value:
                info = SYSTEM_CPU_SET_INFORMATION.from_buffer_copy(buffer, offset)
                if info.Size == 0:
                    break
                
                lp_index = info.LogicalProcessorIndex
                eff_class = info.EfficiencyClass
                
                if lp_index < 64:
                    mask = 1 << lp_index
                    if eff_class not in self.efficiency_classes:
                        self.efficiency_classes[eff_class] = 0
                    self.efficiency_classes[eff_class] |= mask
                
                offset += info.Size
            
            if len(self.efficiency_classes) >= 2:
                sorted_classes = sorted(self.efficiency_classes.keys())
                self.e_cores_mask = self.efficiency_classes[sorted_classes[0]]
                self.p_cores_mask = 0
                for cls in sorted_classes[1:]:
                    self.p_cores_mask |= self.efficiency_classes[cls]
                
                self.e_core_count = bin(self.e_cores_mask).count('1')
                self.p_core_count = bin(self.p_cores_mask).count('1')
                self.has_hybrid_arch = True
                self.detection_method = "cpuset"
                return True
            
            return False
        except Exception as e:
            print(f"[CPU] CpuSet 检测错误: {e}")
            return False
    
    def _detect_via_power_info(self):
        try:
            class PROCESSOR_POWER_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("Number", ctypes.c_ulong), ("MaxMhz", ctypes.c_ulong),
                    ("CurrentMhz", ctypes.c_ulong), ("MhzLimit", ctypes.c_ulong),
                    ("MaxIdleState", ctypes.c_ulong), ("CurrentIdleState", ctypes.c_ulong),
                ]
            
            ntdll = ctypes.WinDLL('ntdll', use_last_error=True)
            num_processors = self.total_cores
            buffer_size = ctypes.sizeof(PROCESSOR_POWER_INFORMATION) * num_processors
            buffer = (PROCESSOR_POWER_INFORMATION * num_processors)()
            
            status = ntdll.CallNtPowerInformation(11, None, 0, ctypes.byref(buffer), buffer_size)
            if status != 0:
                return False
            
            freq_to_cores = {}
            for i in range(num_processors):
                max_mhz = buffer[i].MaxMhz
                if max_mhz not in freq_to_cores:
                    freq_to_cores[max_mhz] = []
                freq_to_cores[max_mhz].append(i)
            
            if len(freq_to_cores) >= 2:
                sorted_freqs = sorted(freq_to_cores.keys())
                freq_ratio = sorted_freqs[0] / sorted_freqs[-1]
                if freq_ratio < 0.85:
                    e_core_freq = sorted_freqs[0]
                    self.e_cores_mask = 0
                    for core_idx in freq_to_cores[e_core_freq]:
                        if core_idx < 64:
                            self.e_cores_mask |= (1 << core_idx)
                    
                    self.p_cores_mask = 0
                    for freq in sorted_freqs[1:]:
                        for core_idx in freq_to_cores[freq]:
                            if core_idx < 64:
                                self.p_cores_mask |= (1 << core_idx)
                    
                    self.e_core_count = bin(self.e_cores_mask).count('1')
                    self.p_core_count = bin(self.p_cores_mask).count('1')
                    self.has_hybrid_arch = True
                    self.detection_method = "power_info"
                    return True
            return False
        except Exception as e:
            print(f"[CPU] PowerInfo 检测错误: {e}")
            return False
    
    def _update_shared_mask(self, mask):
        if self.shared_mask is not None:
            try:
                self.shared_mask.value = mask
            except Exception as e:
                print(f"[CPU] 更新共享变量失败: {e}")
    
    def set_e_cores_only(self):
        if not self.supported or self.e_cores_mask == 0:
            return False
        self.current_target_mask = self.e_cores_mask
        self.current_target_type = "e_cores"
        self._update_shared_mask(self.e_cores_mask)
        return self._set_affinity_mask(self.e_cores_mask, "E核")
    
    def set_all_cores(self):
        if self.all_cores_mask == 0:
            return False
        self.current_target_mask = self.all_cores_mask
        self.current_target_type = "all"
        self._update_shared_mask(self.all_cores_mask)
        return self._set_affinity_mask(self.all_cores_mask, "全核")
    
    def _set_affinity_mask(self, mask, name):
        try:
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.GetCurrentProcess.argtypes = []
            kernel32.SetProcessAffinityMask.restype = ctypes.c_int
            kernel32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, self.DWORD_PTR]
            
            process = kernel32.GetCurrentProcess()
            result = kernel32.SetProcessAffinityMask(process, self.DWORD_PTR(mask))
            
            if result == 0:
                error = ctypes.get_last_error()
                print(f"[CPU] SetProcessAffinityMask({name} {hex(mask)}) 失败，错误码: {error}")
                return False
            
            print(f"[CPU] 主进程亲和性设置: {name} {hex(mask)}")
            return True
        except Exception as e:
            print(f"[CPU] 设置亲和性错误: {e}")
            return False
    
    def set_subprocess_affinity(self, process):
        if platform.system() != "Windows" or self.current_target_mask == 0:
            return False
        try:
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.SetProcessAffinityMask.restype = ctypes.c_int
            kernel32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, self.DWORD_PTR]
            
            handle = ctypes.c_void_p(int(process._handle))
            result = kernel32.SetProcessAffinityMask(handle, self.DWORD_PTR(self.current_target_mask))
            
            if result:
                print(f"[CPU] 子进程 PID {process.pid} 亲和性设置: {self.current_target_type} {hex(self.current_target_mask)}")
                return True
            return False
        except Exception as e:
            print(f"[CPU] 设置子进程亲和性错误: {e}")
            return False
    
    def get_shared_mask(self):
        return self.shared_mask
    
    def get_info_string(self):
        if self.supported:
            return f"CPU: {self.p_core_count}P + {self.e_core_count}E 核心 (混合架构)"
        return f"CPU: {self.total_cores} 核心"


# ============ 主应用程序 ============

class BitrateAnalyzer:
    def __init__(self, root):
        self.root = root
        self.root.title("视频比特率分析器")
        
        self.dpi_scale = self.get_dpi_scale()
        
        base_width, base_height = 1100, 850
        self.root.geometry(f"{int(base_width * self.dpi_scale)}x{int(base_height * self.dpi_scale)}")
        self.root.minsize(int(800 * self.dpi_scale), int(650 * self.dpi_scale))
        
        self.ffmpeg_path = None
        self.ffprobe_path = None
        self.video_path = None
        self.bitrate_data = []
        self.time_index = []
        self.analyzing = False
        self.chart_info = None
        self.crosshair_items = []
        
        self.video_fps = 25.0
        
        self.current_visible_data = []
        self.current_visible_points = []
        
        self.last_mouse_update = 0
        self.mouse_throttle_ms = 25
        self.pending_mouse_update = None
        
        self.view_start = 0.0
        self.view_end = 1.0
        self.min_view_range = 0.005
        
        # 缩略图拖动相关
        self.thumbnail_dragging = False
        self.thumbnail_drag_mode = None
        self.thumbnail_drag_start_x = 0
        self.thumbnail_drag_start_view = (0, 1)
        self.thumbnail_data = []
        self.thumbnail_info = None
        self.selection_items = {}
        
        self.cpu_manager = CPUAffinityManager()
        self.is_minimized = False
        self.use_e_cores_when_minimized = True
        
        self.pending_chart_draw = None
        self.pending_thumbnail_draw = None
        
        # 视频预览相关
        self.show_preview = False
        self.preview_window = None
        self.preview_label = None
        self.preview_image = None
        self.preview_time_label = None
        self.last_preview_time = -999
        self.preview_cache = {}
        self.preview_size = (320, 180)  # 16:9 预览大小
        self.preview_pending = None
        
        self.setup_fonts()
        self.setup_ui()
        self.find_ffmpeg()
        self.setup_window_state_handler()
    
    def get_dpi_scale(self):
        try:
            if platform.system() == "Windows":
                from ctypes import windll
                try:
                    windll.shcore.SetProcessDpiAwareness(2)
                except:
                    try:
                        windll.shcore.SetProcessDpiAwareness(1)
                    except:
                        windll.user32.SetProcessDPIAware()
                
                hdc = windll.user32.GetDC(0)
                dpi = windll.gdi32.GetDeviceCaps(hdc, 88)
                windll.user32.ReleaseDC(0, hdc)
                return dpi / 96.0
        except:
            pass
        return 1.0
    
    def setup_fonts(self):
        base_size = 10
        scaled_size = int(base_size * self.dpi_scale)
        
        self.fonts = {
            "normal": ("Microsoft YaHei UI", scaled_size),
            "bold": ("Microsoft YaHei UI", scaled_size, "bold"),
            "small": ("Microsoft YaHei UI", int(scaled_size * 0.9)),
            "title": ("Microsoft YaHei UI", int(scaled_size * 1.2), "bold"),
            "chart": ("Arial", int(scaled_size * 0.9)),
            "chart_title": ("Microsoft YaHei UI", int(scaled_size * 1.1), "bold"),
        }
    
    def setup_ui(self):
        style = ttk.Style()
        style.configure("TLabel", font=self.fonts["normal"])
        style.configure("TButton", font=self.fonts["normal"])
        style.configure("TCombobox", font=self.fonts["normal"])
        
        pad = int(10 * self.dpi_scale)
        
        main_frame = ttk.Frame(self.root, padding=pad)
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # 控制区
        control_frame = ttk.Frame(main_frame)
        control_frame.pack(fill=tk.X, pady=(0, pad))
        
        self.select_btn = ttk.Button(
            control_frame, text="📁 选择视频文件", 
            command=self.select_video, width=18
        )
        self.select_btn.pack(side=tk.LEFT, padx=(0, pad))
        
        self.file_label = ttk.Label(
            control_frame, text="未选择文件", 
            font=self.fonts["normal"], foreground="#666"
        )
        self.file_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        param_frame = ttk.Frame(control_frame)
        param_frame.pack(side=tk.RIGHT)
        
        ttk.Label(param_frame, text="采样窗口:").pack(side=tk.LEFT, padx=(pad, 5))
        self.window_var = tk.StringVar(value="0.1")
        self.window_combo = ttk.Combobox(
            param_frame, textvariable=self.window_var,
            values=["0.1", "0.25", "0.5", "1.0", "2.0", "5.0"], 
            width=6, state="readonly", font=self.fonts["normal"]
        )
        self.window_combo.pack(side=tk.LEFT)
        self.window_combo.bind("<<ComboboxSelected>>", self.on_window_changed)
        ttk.Label(param_frame, text="秒").pack(side=tk.LEFT, padx=(2, 0))
        
        # FFmpeg 信息
        info_row = ttk.Frame(main_frame)
        info_row.pack(fill=tk.X, anchor=tk.W)
        
        self.ffmpeg_label = ttk.Label(info_row, text="", foreground="gray", font=self.fonts["small"])
        self.ffmpeg_label.pack(side=tk.LEFT)
        
        # CPU 调度控制
        cpu_frame = ttk.Frame(main_frame)
        cpu_frame.pack(fill=tk.X, pady=(5, 0))
        
        self.cpu_info_label = ttk.Label(
            cpu_frame, text=self.cpu_manager.get_info_string(),
            font=self.fonts["small"], foreground="#666"
        )
        self.cpu_info_label.pack(side=tk.LEFT)
        
        if self.cpu_manager.supported:
            self.e_core_var = tk.BooleanVar(value=True)
            self.e_core_check = ttk.Checkbutton(
                cpu_frame, text="最小化时使用 E 核 (省电模式)",
                variable=self.e_core_var, command=self.on_e_core_toggle
            )
            self.e_core_check.pack(side=tk.RIGHT)
            
            self.cpu_status_label = ttk.Label(
                cpu_frame, text="", font=self.fonts["small"], foreground="#2e7d32"
            )
            self.cpu_status_label.pack(side=tk.RIGHT, padx=(0, 10))
        else:
            self.cpu_status_label = None
        
        # 进度区
        progress_frame = ttk.Frame(main_frame)
        progress_frame.pack(fill=tk.X, pady=int(8 * self.dpi_scale))
        
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(
            progress_frame, variable=self.progress_var,
            maximum=100, mode='determinate'
        )
        self.progress_bar.pack(fill=tk.X, side=tk.LEFT, expand=True, padx=(0, pad))
        
        self.status_label = ttk.Label(
            progress_frame, text="就绪", width=45, anchor=tk.W, font=self.fonts["small"]
        )
        self.status_label.pack(side=tk.LEFT)
        
        # 主图表区
        chart_frame = ttk.LabelFrame(main_frame, text=" 比特率曲线 ", padding=5)
        chart_frame.pack(fill=tk.BOTH, expand=True, pady=(5, pad))
        
        self.canvas = tk.Canvas(chart_frame, bg="white", highlightthickness=1, highlightbackground="#ddd")
        self.canvas.pack(fill=tk.BOTH, expand=True)
        
        # 缩略图区域
        thumbnail_frame = ttk.LabelFrame(main_frame, text=" 时间轴导航 (拖动选择区域 / 滚轮缩放) ", padding=5)
        thumbnail_frame.pack(fill=tk.X, pady=(0, pad))
        
        thumb_height = int(80 * self.dpi_scale)
        self.thumbnail_canvas = tk.Canvas(
            thumbnail_frame, bg="white", height=thumb_height,
            highlightthickness=1, highlightbackground="#ddd"
        )
        self.thumbnail_canvas.pack(fill=tk.X, expand=True)
        
        zoom_frame = ttk.Frame(thumbnail_frame)
        zoom_frame.pack(fill=tk.X, pady=(5, 0))
        
        ttk.Button(zoom_frame, text="🔍 重置视图", command=self.reset_view, width=12).pack(side=tk.LEFT)
        ttk.Button(zoom_frame, text="➕ 放大", command=lambda: self.zoom(1.5), width=8).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Button(zoom_frame, text="➖ 缩小", command=lambda: self.zoom(0.67), width=8).pack(side=tk.LEFT, padx=(5, 0))
        
        # 视频预览开关
        self.preview_var = tk.BooleanVar(value=False)
        self.preview_check = ttk.Checkbutton(
            zoom_frame, text="🖼 悬停显示截图",
            variable=self.preview_var, command=self.on_preview_toggle
        )
        self.preview_check.pack(side=tk.LEFT, padx=(20, 0))
        
        self.zoom_label = ttk.Label(zoom_frame, text="显示: 100.0%", font=self.fonts["small"])
        self.zoom_label.pack(side=tk.RIGHT)
        
        # 视频信息区
        bottom_info_frame = ttk.Frame(main_frame)
        bottom_info_frame.pack(fill=tk.X)
        
        self.video_info_label = ttk.Label(
            bottom_info_frame, text="", font=self.fonts["small"], foreground="#555"
        )
        self.video_info_label.pack(side=tk.LEFT)
        
        self.cursor_info_label = ttk.Label(
            bottom_info_frame, text="", font=self.fonts["bold"], foreground="#1976D2"
        )
        self.cursor_info_label.pack(side=tk.RIGHT)
        
        # 绑定事件
        self.canvas.bind("<Configure>", self.on_canvas_resize)
        self.canvas.bind("<Motion>", self.on_mouse_move)
        self.canvas.bind("<Leave>", self.on_mouse_leave)
        self.canvas.bind("<MouseWheel>", self.on_mouse_wheel)
        self.canvas.bind("<Button-4>", self.on_mouse_wheel)
        self.canvas.bind("<Button-5>", self.on_mouse_wheel)
        
        self.thumbnail_canvas.bind("<Configure>", self.on_thumbnail_resize)
        self.thumbnail_canvas.bind("<ButtonPress-1>", self.on_thumbnail_press)
        self.thumbnail_canvas.bind("<B1-Motion>", self.on_thumbnail_drag)
        self.thumbnail_canvas.bind("<ButtonRelease-1>", self.on_thumbnail_release)
        self.thumbnail_canvas.bind("<MouseWheel>", self.on_mouse_wheel)
        self.thumbnail_canvas.bind("<Double-Button-1>", self.on_thumbnail_double_click)
    
    def setup_window_state_handler(self):
        if platform.system() == "Windows":
            self.root.bind("<Unmap>", self._on_window_unmap)
            self.root.bind("<Map>", self._on_window_map)
            self._start_state_polling()
    
    def _on_window_unmap(self, event):
        if event.widget == self.root:
            self.root.after(100, self._check_minimized_state)
    
    def _on_window_map(self, event):
        if event.widget == self.root:
            self.root.after(100, self._check_minimized_state)
    
    def _start_state_polling(self):
        self._poll_window_state()
    
    def _poll_window_state(self):
        self._check_minimized_state()
        self.root.after(500, self._poll_window_state)
    
    def _check_minimized_state(self):
        try:
            is_minimized = False
            try:
                state = self.root.state()
                is_minimized = (state == 'iconic')
            except:
                pass
            
            if not is_minimized and platform.system() == "Windows":
                try:
                    user32 = ctypes.windll.user32
                    hwnd = self._get_window_handle()
                    if hwnd:
                        is_minimized = user32.IsIconic(hwnd) != 0
                except:
                    pass
            
            if is_minimized != self.is_minimized:
                print(f"[窗口状态] {'最小化' if is_minimized else '恢复'}")
                self.is_minimized = is_minimized
                self._handle_window_state_change()
        except Exception as e:
            print(f"[DEBUG] 窗口状态检测错误: {e}")
    
    def _get_window_handle(self):
        try:
            frame_id = self.root.wm_frame()
            if frame_id:
                return int(frame_id, 16) if isinstance(frame_id, str) else frame_id
        except:
            pass
        try:
            user32 = ctypes.windll.user32
            widget_hwnd = self.root.winfo_id()
            hwnd = user32.GetAncestor(widget_hwnd, 2)
            return hwnd if hwnd else widget_hwnd
        except:
            pass
        return None
    
    def _handle_window_state_change(self):
        if not self.cpu_manager.supported or not self.use_e_cores_when_minimized:
            return
        
        if self.is_minimized:
            if self.analyzing:
                success = self.cpu_manager.set_e_cores_only()
                if success:
                    self.update_cpu_status(f"⚡ E核模式 ({self.cpu_manager.e_core_count}核)")
        else:
            success = self.cpu_manager.set_all_cores()
            if success:
                if self.analyzing:
                    self.update_cpu_status(f"🚀 全核模式 ({self.cpu_manager.total_cores}核)")
                else:
                    self.update_cpu_status("")
    
    def on_e_core_toggle(self):
        self.use_e_cores_when_minimized = self.e_core_var.get()
        print(f"[CPU] E核省电模式: {'启用' if self.use_e_cores_when_minimized else '禁用'}")
        
        if not self.use_e_cores_when_minimized:
            self.cpu_manager.set_all_cores()
            self.update_cpu_status("")
        else:
            self._handle_window_state_change()
    
    def update_cpu_status(self, text):
        if self.cpu_status_label:
            self.root.after(0, lambda: self.cpu_status_label.config(text=text))
    
    # ============ 视频预览相关方法 ============
    
    def on_preview_toggle(self):
        """切换视频预览功能"""
        self.show_preview = self.preview_var.get()
        if not self.show_preview:
            self.hide_preview()
    
    def create_preview_window(self):
        """创建预览窗口"""
        if self.preview_window is None:
            self.preview_window = tk.Toplevel(self.root)
            self.preview_window.title("")
            self.preview_window.overrideredirect(True)  # 无边框
            self.preview_window.attributes('-topmost', True)
            self.preview_window.withdraw()
            
            # 添加边框效果
            outer_frame = tk.Frame(self.preview_window, bg="#1976D2", padx=2, pady=2)
            outer_frame.pack(fill=tk.BOTH, expand=True)
            
            inner_frame = tk.Frame(outer_frame, bg="#f5f5f5")
            inner_frame.pack(fill=tk.BOTH, expand=True)
            
            self.preview_label = tk.Label(inner_frame, bg="#000000")
            self.preview_label.pack(padx=2, pady=(2, 0))
            
            self.preview_time_label = tk.Label(
                inner_frame, text="", font=self.fonts["small"],
                bg="#f5f5f5", fg="#333"
            )
            self.preview_time_label.pack(pady=(2, 4))
        
        return self.preview_window
    
    def hide_preview(self):
        """隐藏预览窗口"""
        if self.preview_window:
            self.preview_window.withdraw()
        if self.preview_pending:
            self.root.after_cancel(self.preview_pending)
            self.preview_pending = None
    
    def request_preview(self, time_sec, mouse_x, mouse_y):
        """请求显示预览截图"""
        if not self.show_preview or not self.video_path or not self.ffmpeg_path:
            return
        
        # 四舍五入到 0.5 秒精度，减少请求
        rounded_time = round(time_sec * 2) / 2
        
        # 如果时间点没变化，只更新位置
        if abs(rounded_time - self.last_preview_time) < 0.3:
            self._update_preview_position(mouse_x, mouse_y)
            return
        
        self.last_preview_time = rounded_time
        
        # 取消之前的请求
        if self.preview_pending:
            self.root.after_cancel(self.preview_pending)
        
        # 延迟执行，避免快速移动时频繁请求
        self.preview_pending = self.root.after(
            100,
            lambda: self._fetch_preview_async(rounded_time, mouse_x, mouse_y)
        )
    
    def _fetch_preview_async(self, time_sec, mouse_x, mouse_y):
        """异步获取预览截图"""
        # 检查缓存
        if time_sec in self.preview_cache:
            self._show_preview(self.preview_cache[time_sec], time_sec, mouse_x, mouse_y)
            return
        
        # 异步获取
        thread = threading.Thread(
            target=self._fetch_preview_thread,
            args=(time_sec, mouse_x, mouse_y),
            daemon=True
        )
        thread.start()
    
    def _fetch_preview_thread(self, time_sec, mouse_x, mouse_y):
        """在后台线程中获取预览截图"""
        tmp_path = None
        try:
            # 创建临时文件
            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                tmp_path = tmp.name
            
            # 使用 ffmpeg 截取帧
            cmd = [
                self.ffmpeg_path,
                '-ss', str(time_sec),
                '-i', self.video_path,
                '-vframes', '1',
                '-vf', f'scale={self.preview_size[0]}:{self.preview_size[1]}:force_original_aspect_ratio=decrease,pad={self.preview_size[0]}:{self.preview_size[1]}:(ow-iw)/2:(oh-ih)/2:black',
                '-y',
                tmp_path
            ]
            
            kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
            if platform.system() == "Windows":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            
            result = subprocess.run(cmd, **kwargs, timeout=5)
            
            if result.returncode == 0 and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                # 在主线程中加载图像
                self.root.after(0, lambda: self._load_and_show_preview(tmp_path, time_sec, mouse_x, mouse_y))
            else:
                # 清理临时文件
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except:
                        pass
                    
        except Exception as e:
            print(f"[预览] 截图错误: {e}")
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except:
                    pass
    
    def _load_and_show_preview(self, tmp_path, time_sec, mouse_x, mouse_y):
        """加载并显示预览图"""
        try:
            image = tk.PhotoImage(file=tmp_path)
            self.preview_cache[time_sec] = image
            self._show_preview(image, time_sec, mouse_x, mouse_y)
        except Exception as e:
            print(f"[预览] 加载图片错误: {e}")
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except:
                pass
    
    def _show_preview(self, image, time_sec, mouse_x, mouse_y):
        """显示预览窗口"""
        if not self.show_preview:
            return
        
        self.create_preview_window()
        self.preview_image = image  # 保持引用防止垃圾回收
        self.preview_label.config(image=image)
        self.preview_time_label.config(text=self.format_time_with_frames(time_sec))
        
        self._update_preview_position(mouse_x, mouse_y)
        self.preview_window.deiconify()
    
    def _update_preview_position(self, mouse_x, mouse_y):
        """更新预览窗口位置"""
        if not self.preview_window or not self.preview_window.winfo_viewable():
            if self.preview_window and self.preview_image:
                self.preview_window.deiconify()
            else:
                return
        
        # 获取画布的屏幕坐标
        canvas_x = self.canvas.winfo_rootx()
        canvas_y = self.canvas.winfo_rooty()
        
        # 预览窗口大小（加上边框和标签）
        preview_w = self.preview_size[0] + 10
        preview_h = self.preview_size[1] + 35
        
        # 默认显示在鼠标右上方
        x = canvas_x + mouse_x + 20
        y = canvas_y + mouse_y - preview_h - 10
        
        # 获取屏幕尺寸
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        
        # 确保不超出屏幕右边界
        if x + preview_w > screen_w:
            x = canvas_x + mouse_x - preview_w - 20
        
        # 确保不超出屏幕上边界
        if y < 0:
            y = canvas_y + mouse_y + 20
        
        # 确保不超出屏幕下边界
        if y + preview_h > screen_h:
            y = screen_h - preview_h - 10
        
        self.preview_window.geometry(f"+{x}+{y}")
    
    def clear_preview_cache(self):
        """清除预览缓存"""
        self.preview_cache = {}
        self.last_preview_time = -999
    
    # ============ FFmpeg 相关 ============
    
    def find_ffmpeg(self):
        system = platform.system()
        ffmpeg_name = "ffmpeg.exe" if system == "Windows" else "ffmpeg"
        ffprobe_name = "ffprobe.exe" if system == "Windows" else "ffprobe"
        
        if getattr(sys, 'frozen', False):
            script_dir = os.path.dirname(sys.executable)
        else:
            script_dir = os.path.dirname(os.path.abspath(__file__))
        
        lib_dir = os.path.join(script_dir, "lib")
        lib_ffmpeg = os.path.join(lib_dir, ffmpeg_name)
        lib_ffprobe = os.path.join(lib_dir, ffprobe_name)
        
        if os.path.isfile(lib_ffmpeg) and os.path.isfile(lib_ffprobe):
            self.ffmpeg_path = lib_ffmpeg
            self.ffprobe_path = lib_ffprobe
            self.ffmpeg_label.config(text=f"✓ FFmpeg: {lib_ffprobe}", foreground="#2e7d32")
            return True
        
        try:
            kwargs = self.get_subprocess_kwargs()
            where_cmd = ["where", ffprobe_name] if system == "Windows" else ["which", ffprobe_name]
            result = subprocess.run(where_cmd, **kwargs)
            
            if result.returncode == 0 and result.stdout.strip():
                full_path = result.stdout.strip().split('\n')[0].strip()
                test_result = subprocess.run([ffprobe_name, "-version"], **kwargs)
                
                if test_result.returncode == 0:
                    self.ffmpeg_path = ffmpeg_name
                    self.ffprobe_path = ffprobe_name
                    self.ffmpeg_label.config(text=f"✓ FFmpeg: {full_path}", foreground="#1565c0")
                    return True
        except:
            pass
        
        self.ffmpeg_label.config(
            text="✗ 未找到 FFmpeg - 请将 ffmpeg 放入 lib 文件夹或添加到系统 PATH",
            foreground="#c62828"
        )
        return False
    
    def get_subprocess_kwargs(self):
        kwargs = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"}
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        return kwargs
    
    def format_time_with_frames(self, seconds, fps=None):
        if seconds < 0:
            return "0:00:00"
        if fps is None:
            fps = self.video_fps
        if fps <= 0:
            fps = 25.0
        
        total_frames = seconds * fps
        frame_in_second = int(total_frames % fps)
        total_seconds = int(seconds)
        
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        secs = total_seconds % 60
        
        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}:{frame_in_second:02d}"
        return f"{minutes}:{secs:02d}:{frame_in_second:02d}"
    
    def format_time_short(self, seconds):
        if seconds < 0:
            return "0:00"
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        
        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"
    
    def on_window_changed(self, event=None):
        if self.video_path and not self.analyzing:
            self.start_analysis()
    
    def select_video(self):
        if not self.ffprobe_path:
            messagebox.showerror("错误", "FFmpeg 未找到")
            return
        if self.analyzing:
            messagebox.showwarning("提示", "正在分析中")
            return
        
        filetypes = [
            ("视频文件", "*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v *.ts *.mts *.m2ts *.mpg *.mpeg *.vob *.3gp"),
            ("所有文件", "*.*")
        ]
        filepath = filedialog.askopenfilename(title="选择视频文件", filetypes=filetypes)
        
        if filepath:
            self.video_path = filepath
            filename = os.path.basename(filepath)
            self.file_label.config(text=filename, foreground="#333")
            self.start_analysis()
    
    def start_analysis(self):
        if self.analyzing:
            return
        
        self.analyzing = True
        self.select_btn.config(state='disabled')
        self.window_combo.config(state='disabled')
        self.bitrate_data = []
        self.time_index = []
        self.thumbnail_data = []
        self.current_visible_data = []
        self.current_visible_points = []
        self.view_start = 0.0
        self.view_end = 1.0
        self.selection_items = {}
        self.canvas.delete("all")
        self.thumbnail_canvas.delete("all")
        self.video_info_label.config(text="")
        self.cursor_info_label.config(text="")
        self.update_zoom_label()
        
        # 清除预览缓存
        self.clear_preview_cache()
        self.hide_preview()
        
        if self.cpu_manager.supported and self.use_e_cores_when_minimized:
            if self.is_minimized:
                self.cpu_manager.set_e_cores_only()
                self.update_cpu_status(f"⚡ E核模式 ({self.cpu_manager.e_core_count}核)")
            else:
                self.cpu_manager.set_all_cores()
                self.update_cpu_status(f"🚀 全核模式 ({self.cpu_manager.total_cores}核)")
        
        thread = threading.Thread(target=self._analyze_thread, daemon=True)
        thread.start()
    
    def _analyze_thread(self):
        try:
            self.update_progress(0, "正在获取视频信息...")
            duration, video_info = self.get_video_info()
            
            if duration is None or duration <= 0:
                self.update_progress(0, "错误: 无法获取视频时长")
                return
            
            self.video_duration = duration
            
            try:
                fps_str = video_info.get("fps", "25")
                self.video_fps = float(fps_str)
                if self.video_fps <= 0:
                    self.video_fps = 25.0
            except:
                self.video_fps = 25.0
            
            self.update_progress(5, f"视频时长: {self.format_time_short(duration)}, 帧率: {self.video_fps:.2f} fps")
            self.update_progress(8, "正在读取帧数据...")
            
            packets = self.get_packets_data(duration)
            if not packets:
                self.update_progress(0, "错误: 无法获取帧数据")
                return
            
            self.update_progress(80, f"读取到 {len(packets)} 个数据包，正在计算...")
            
            window_size = float(self.window_var.get())
            
            if self.cpu_manager.supported and self.is_minimized and self.use_e_cores_when_minimized:
                num_workers = max(1, self.cpu_manager.e_core_count)
            else:
                num_workers = self.cpu_manager.total_cores
            
            self.bitrate_data = self.calculate_bitrate_parallel(packets, duration, window_size, num_workers)
            self.time_index = [d[0] for d in self.bitrate_data]
            self.prepare_thumbnail_data()
            
            self.update_progress(100, f"✓ 完成 - {len(self.bitrate_data)} 个采样点")
            
            self.root.after(0, self.draw_chart)
            self.root.after(0, self.draw_thumbnail)
            self.root.after(0, lambda: self.update_video_info(duration, video_info))
            
        except Exception as e:
            self.update_progress(0, f"错误: {str(e)}")
            import traceback
            traceback.print_exc()
        finally:
            self.analyzing = False
            if self.cpu_manager.supported:
                self.cpu_manager.set_all_cores()
            self.update_cpu_status("")
            self.root.after(0, lambda: self.select_btn.config(state='normal'))
            self.root.after(0, lambda: self.window_combo.config(state='readonly'))
    
    def prepare_thumbnail_data(self):
        """准备缩略图数据 - 使用峰值保留采样"""
        max_thumb_points = 400
        
        if len(self.bitrate_data) <= max_thumb_points:
            self.thumbnail_data = self.bitrate_data[:]
        else:
            step = len(self.bitrate_data) / max_thumb_points
            self.thumbnail_data = [self.bitrate_data[0]]
            
            for i in range(1, max_thumb_points - 1):
                bucket_start = int(i * step)
                bucket_end = int((i + 1) * step)
                bucket = self.bitrate_data[bucket_start:bucket_end]
                if bucket:
                    max_point = max(bucket, key=lambda x: x[1])
                    self.thumbnail_data.append(max_point)
            
            self.thumbnail_data.append(self.bitrate_data[-1])
            self.thumbnail_data.sort(key=lambda x: x[0])
    
    def get_visible_data(self, view_start_time, view_end_time, max_points=1500):
        if not self.bitrate_data:
            return []
        
        start_idx = bisect.bisect_left(self.time_index, view_start_time)
        end_idx = bisect.bisect_right(self.time_index, view_end_time)
        
        start_idx = max(0, start_idx - 1)
        end_idx = min(len(self.bitrate_data), end_idx + 1)
        
        visible_data = self.bitrate_data[start_idx:end_idx]
        
        if len(visible_data) <= max_points:
            return visible_data
        
        step = len(visible_data) / max_points
        sampled_data = [visible_data[0]]
        
        for i in range(1, max_points - 1):
            bucket_start = int(i * step)
            bucket_end = int((i + 1) * step)
            bucket = visible_data[bucket_start:bucket_end]
            if bucket:
                max_point = max(bucket, key=lambda x: x[1])
                sampled_data.append(max_point)
        
        sampled_data.append(visible_data[-1])
        sampled_data.sort(key=lambda x: x[0])
        return sampled_data
    
    def get_video_info(self):
        kwargs = self.get_subprocess_kwargs()
        cmd = [
            self.ffprobe_path, "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", self.video_path
        ]
        result = subprocess.run(cmd, **kwargs)
        
        duration = None
        video_info = {"codec": "N/A", "width": 0, "height": 0, "fps": "25", "size": 0, "bitrate": 0}
        
        if result.returncode == 0 and result.stdout:
            try:
                data = json.loads(result.stdout)
                
                if "format" in data:
                    fmt = data["format"]
                    if "duration" in fmt:
                        try:
                            duration = float(fmt["duration"])
                        except:
                            pass
                    video_info["size"] = int(fmt.get("size", 0) or 0)
                    video_info["bitrate"] = int(fmt.get("bit_rate", 0) or 0)
                
                for stream in data.get("streams", []):
                    if stream.get("disposition", {}).get("attached_pic", 0) == 1:
                        continue
                    if stream.get("codec_type") == "video":
                        video_info["codec"] = stream.get("codec_name", "N/A").upper()
                        video_info["width"] = stream.get("width", 0)
                        video_info["height"] = stream.get("height", 0)
                        
                        fps_str = stream.get("r_frame_rate") or stream.get("avg_frame_rate", "25/1")
                        try:
                            if '/' in fps_str:
                                num, den = map(int, fps_str.split('/'))
                                if den > 0:
                                    video_info["fps"] = f"{num/den:.2f}"
                            else:
                                video_info["fps"] = fps_str
                        except:
                            video_info["fps"] = "25"
                        
                        if duration is None or duration <= 0:
                            stream_duration = stream.get("duration")
                            if stream_duration:
                                try:
                                    duration = float(stream_duration)
                                except:
                                    pass
                        break
            except json.JSONDecodeError:
                pass
        
        if duration is None or duration <= 0:
            duration = self.get_duration_fallback()
        
        return duration, video_info
    
    def get_duration_fallback(self):
        kwargs = self.get_subprocess_kwargs()
        cmd = [self.ffmpeg_path, "-i", self.video_path, "-f", "null", "-t", "0", "-"]
        
        try:
            result = subprocess.run(cmd, **kwargs, timeout=30)
            output = result.stderr or ""
            match = re.search(r'Duration:\s*(\d+):(\d+):(\d+\.?\d*)', output)
            if match:
                hours = int(match.group(1))
                minutes = int(match.group(2))
                seconds = float(match.group(3))
                return hours * 3600 + minutes * 60 + seconds
        except:
            pass
        return None
    
    def get_packets_data(self, duration):
        video_stream = self.find_video_stream_index()
        stream_spec = f"v:{video_stream}" if video_stream is not None else "v:0"
        
        cmd = [
            self.ffprobe_path, "-v", "error", "-print_format", "json",
            "-show_entries", "packet=pts_time,dts_time,size,flags",
            "-select_streams", stream_spec, self.video_path
        ]
        
        kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        
        process = subprocess.Popen(cmd, **kwargs)
        
        if self.cpu_manager.supported:
            self.cpu_manager.set_subprocess_affinity(process)
        
        output_chunks = []
        bytes_read = 0
        estimated_size = max(duration * 30 * 50, 10000)
        
        while True:
            chunk = process.stdout.read(131072)
            if not chunk:
                break
            output_chunks.append(chunk)
            bytes_read += len(chunk)
            progress = min(75, 8 + (bytes_read / estimated_size) * 67)
            kb_read = bytes_read / 1024
            self.update_progress(progress, f"读取帧数据... {kb_read:.0f} KB")
        
        process.wait()
        
        if process.returncode != 0:
            return None
        
        output = b"".join(output_chunks).decode('utf-8', errors='ignore')
        
        try:
            data = json.loads(output)
            return data.get("packets", [])
        except json.JSONDecodeError:
            return None
    
    def find_video_stream_index(self):
        kwargs = self.get_subprocess_kwargs()
        cmd = [
            self.ffprobe_path, "-v", "error", "-print_format", "json",
            "-show_streams", "-select_streams", "v", self.video_path
        ]
        
        try:
            result = subprocess.run(cmd, **kwargs)
            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                streams = data.get("streams", [])
                for i, stream in enumerate(streams):
                    disposition = stream.get("disposition", {})
                    if disposition.get("attached_pic", 0) == 1:
                        continue
                    return i
        except:
            pass
        return 0
    
    def calculate_bitrate_parallel(self, packets, duration, window_size, num_workers=None):
        frame_data = []
        for pkt in packets:
            try:
                pts = pkt.get("pts_time") or pkt.get("dts_time")
                if pts is not None and pts != 'N/A':
                    frame_data.append((float(pts), int(pkt.get("size", 0) or 0)))
            except:
                continue
        
        if not frame_data:
            return []
        
        frame_data.sort(key=lambda x: x[0])
        
        max_frame_time = frame_data[-1][0] if frame_data else 0
        actual_duration = max(duration, max_frame_time)
        
        step = window_size / 2
        time_points = []
        t = 0.0
        while t <= actual_duration:
            time_points.append(t)
            t += step
        
        if num_workers is None:
            num_workers = self.cpu_manager.total_cores
        
        if len(time_points) < 50 or num_workers <= 1:
            return self._calculate_bitrate_single(frame_data, time_points, window_size)
        
        chunk_size = max(1, len(time_points) // num_workers)
        chunks = []
        for i in range(0, len(time_points), chunk_size):
            chunks.append(time_points[i:i + chunk_size])
        
        actual_workers = min(len(chunks), num_workers)
        shared_mask = self.cpu_manager.get_shared_mask()
        
        self.update_progress(82, f"使用 {actual_workers} 进程计算...")
        
        results = []
        try:
            with ProcessPoolExecutor(
                max_workers=num_workers,
                initializer=_worker_init,
                initargs=(shared_mask,)
            ) as executor:
                futures = [
                    executor.submit(_calculate_chunk, frame_data, chunk, window_size)
                    for chunk in chunks
                ]
                for i, future in enumerate(futures):
                    results.extend(future.result())
                    progress = 82 + (i + 1) / len(futures) * 15
                    self.update_progress(progress, f"计算中... {i+1}/{len(futures)}")
        except Exception as e:
            print(f"并行计算错误: {e}")
            return self._calculate_bitrate_single(frame_data, time_points, window_size)
        
        results.sort(key=lambda x: x[0])
        return results
    
    def _calculate_bitrate_single(self, frame_data, time_points, window_size):
        results = []
        total = len(time_points)
        
        for i, t in enumerate(time_points):
            total_bits = sum(
                size * 8 for pts, size in frame_data
                if t <= pts < t + window_size
            )
            bitrate_kbps = total_bits / window_size / 1000
            results.append((t + window_size / 2, bitrate_kbps))
            
            if i % 100 == 0:
                progress = 82 + (i / total) * 15
                self.update_progress(progress, f"计算比特率... {i}/{total}")
        
        return results
    
    @staticmethod
    def calculate_nice_scale(data_min, data_max, num_ticks=6):
        if data_max <= data_min:
            data_max = data_min + 1
        
        data_max_padded = data_max * 1.1
        range_val = data_max_padded - data_min
        rough_step = range_val / (num_ticks - 1)
        if rough_step <= 0:
            rough_step = 1
        
        magnitude = 10 ** math.floor(math.log10(rough_step))
        residual = rough_step / magnitude
        
        if residual <= 1:
            nice_step = 1 * magnitude
        elif residual <= 2:
            nice_step = 2 * magnitude
        elif residual <= 2.5:
            nice_step = 2.5 * magnitude
        elif residual <= 5:
            nice_step = 5 * magnitude
        else:
            nice_step = 10 * magnitude
        
        nice_min = math.floor(data_min / nice_step) * nice_step
        nice_max = math.ceil(data_max_padded / nice_step) * nice_step
        if nice_min < 0:
            nice_min = 0
        
        tick_values = []
        current = nice_min
        while current <= nice_max + nice_step * 0.001:
            tick_values.append(current)
            current += nice_step
        
        return nice_min, nice_max, nice_step, tick_values
    
    def draw_chart(self):
        self.canvas.delete("all")
        self.crosshair_items = []
        
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        
        if width < 100 or height < 100:
            return
        
        if not self.bitrate_data:
            self.canvas.create_text(
                width / 2, height / 2,
                text="请选择视频文件进行分析",
                font=self.fonts["title"], fill="#999"
            )
            return
        
        max_time = self.time_index[-1] if self.time_index else 1
        view_start_time = self.view_start * max_time
        view_end_time = self.view_end * max_time
        
        view_range = self.view_end - self.view_start
        if view_range > 0.8:
            max_points = 800
        elif view_range > 0.5:
            max_points = 1200
        else:
            max_points = 1500
        
        visible_data = self.get_visible_data(view_start_time, view_end_time, max_points)
        if not visible_data:
            return
        
        scale = self.dpi_scale
        margin = {
            "left": int(85 * scale), "right": int(30 * scale),
            "top": int(45 * scale), "bottom": int(55 * scale)
        }
        chart_w = width - margin["left"] - margin["right"]
        chart_h = height - margin["top"] - margin["bottom"]
        
        if chart_w <= 0 or chart_h <= 0:
            return
        
        visible_bitrates = [d[1] for d in visible_data]
        data_max = max(visible_bitrates) if visible_bitrates else 1000
        
        nice_min, nice_max, nice_step, tick_values = self.calculate_nice_scale(0, data_max, 6)
        min_bitrate = nice_min
        max_bitrate = nice_max
        
        chart_left = margin["left"]
        chart_right = width - margin["right"]
        chart_top = margin["top"]
        chart_bottom = height - margin["bottom"]
        
        self.chart_info = {
            "margin": margin, "view_start_time": view_start_time, "view_end_time": view_end_time,
            "max_bitrate": max_bitrate, "min_bitrate": min_bitrate,
            "chart_w": chart_w, "chart_h": chart_h, "width": width, "height": height,
            "max_time": max_time, "chart_left": chart_left, "chart_right": chart_right,
            "chart_top": chart_top, "chart_bottom": chart_bottom
        }
        
        time_range = view_end_time - view_start_time
        if time_range <= 0:
            time_range = 1
        bitrate_range = max_bitrate - min_bitrate
        if bitrate_range <= 0:
            bitrate_range = 1
        
        def to_x(t):
            return chart_left + ((t - view_start_time) / time_range) * chart_w
        
        def to_y(br):
            return chart_bottom - ((br - min_bitrate) / bitrate_range) * chart_h
        
        self.canvas.create_rectangle(
            chart_left, chart_top, chart_right, chart_bottom,
            fill="#fafafa", outline="#ccc"
        )
        
        for br_val in tick_values:
            y = to_y(br_val)
            if chart_top <= y <= chart_bottom:
                self.canvas.create_line(chart_left, y, chart_right, y, fill="#e0e0e0")
                if br_val >= 1000:
                    label = f"{br_val/1000:.0f} Mbps" if br_val % 1000 == 0 else f"{br_val/1000:.1f} Mbps"
                else:
                    label = f"{br_val:.0f} Kbps"
                self.canvas.create_text(
                    chart_left - 8, y, text=label, anchor="e", font=self.fonts["chart"], fill="#666"
                )
        
        x_steps = min(10, max(4, int(time_range / 15)))
        for i in range(x_steps + 1):
            t_val = view_start_time + time_range * i / x_steps
            x = to_x(t_val)
            self.canvas.create_line(x, chart_top, x, chart_bottom, fill="#e0e0e0")
            self.canvas.create_text(
                x, chart_bottom + int(12 * scale),
                text=self.format_time_short(t_val), anchor="n", font=self.fonts["chart"], fill="#666"
            )
        
        points = []
        for t, br in visible_data:
            x = to_x(t)
            y = to_y(br)
            points.append((x, y, t, br))
        
        self.current_visible_data = visible_data
        self.current_visible_points = points
        
        if len(points) >= 2:
            fill_coords = [chart_left, chart_bottom]
            for x, y, _, _ in points:
                x = max(chart_left, min(chart_right, x))
                fill_coords.extend([x, y])
            fill_coords.extend([chart_right, chart_bottom])
            
            self.canvas.create_polygon(fill_coords, fill="#bbdefb", outline="")
            
            line_coords = []
            for x, y, _, _ in points:
                x = max(chart_left, min(chart_right, x))
                line_coords.extend([x, y])
            self.canvas.create_line(line_coords, fill="#1976D2", width=2)
        
        if visible_bitrates:
            avg_bitrate = sum(visible_bitrates) / len(visible_bitrates)
            avg_y = to_y(avg_bitrate)
            
            if chart_top <= avg_y <= chart_bottom:
                self.canvas.create_line(
                    chart_left, avg_y, chart_right, avg_y,
                    fill="#ff9800", width=2, dash=(8, 4)
                )
                avg_label = f"{avg_bitrate/1000:.2f} Mbps" if avg_bitrate >= 1000 else f"{avg_bitrate:.0f} Kbps"
                self.canvas.create_text(
                    chart_right - 5, avg_y - int(8 * scale),
                    text=f"平均: {avg_label}", anchor="e", font=self.fonts["small"], fill="#e65100"
                )
            
            max_br = max(visible_bitrates)
            min_br = min(visible_bitrates)
            max_label = f"{max_br/1000:.2f} Mbps" if max_br >= 1000 else f"{max_br:.0f} Kbps"
            min_label = f"{min_br/1000:.2f} Mbps" if min_br >= 1000 else f"{min_br:.0f} Kbps"
            avg_label = f"{avg_bitrate/1000:.2f} Mbps" if avg_bitrate >= 1000 else f"{avg_bitrate:.0f} Kbps"
            
            stats = f"最大: {max_label}  |  最小: {min_label}  |  平均: {avg_label}"
            
            self.canvas.create_text(
                chart_left + 5, chart_top - int(25 * scale),
                text="视频比特率分析", anchor="w", font=self.fonts["chart_title"], fill="#333"
            )
            self.canvas.create_text(
                chart_right, chart_top - int(25 * scale),
                text=stats, anchor="e", font=self.fonts["small"], fill="#666"
            )
        
        self.canvas.create_text(
            chart_left + chart_w / 2, height - int(10 * scale),
            text="时间", font=self.fonts["normal"], fill="#666"
        )
        self.canvas.create_text(
            int(15 * scale), chart_top + chart_h / 2,
            text="比特率", font=self.fonts["normal"], fill="#666", angle=90
        )
    
    def draw_thumbnail(self):
        """绘制缩略图（仅绘制曲线部分）"""
        self.thumbnail_canvas.delete("all")
        self.selection_items = {}
        
        width = self.thumbnail_canvas.winfo_width()
        height = self.thumbnail_canvas.winfo_height()
        
        if width < 50 or height < 30 or not self.thumbnail_data:
            return
        
        scale = self.dpi_scale
        margin = {"left": int(10 * scale), "right": int(10 * scale),
                  "top": int(10 * scale), "bottom": int(10 * scale)}
        chart_w = width - margin["left"] - margin["right"]
        chart_h = height - margin["top"] - margin["bottom"]
        
        if chart_w <= 0 or chart_h <= 0:
            return
        
        self.thumbnail_info = {
            "margin": margin, "chart_w": chart_w, "chart_h": chart_h,
            "width": width, "height": height
        }
        
        times = [d[0] for d in self.thumbnail_data]
        bitrates = [d[1] for d in self.thumbnail_data]
        
        max_time = max(times)
        max_bitrate = max(bitrates) * 1.1
        min_bitrate = 0
        
        def to_x(t):
            return margin["left"] + (t / max_time) * chart_w
        
        def to_y(br):
            return height - margin["bottom"] - ((br - min_bitrate) / (max_bitrate - min_bitrate)) * chart_h
        
        self.thumbnail_canvas.create_rectangle(
            margin["left"], margin["top"],
            width - margin["right"], height - margin["bottom"],
            fill="#f5f5f5", outline="#ccc"
        )
        
        if len(self.thumbnail_data) >= 2:
            fill_coords = []
            for t, br in self.thumbnail_data:
                fill_coords.extend([to_x(t), to_y(br)])
            fill_coords.extend([
                to_x(self.thumbnail_data[-1][0]), height - margin["bottom"],
                to_x(self.thumbnail_data[0][0]), height - margin["bottom"]
            ])
            self.thumbnail_canvas.create_polygon(fill_coords, fill="#e3f2fd", outline="")
            
            line_coords = []
            for t, br in self.thumbnail_data:
                line_coords.extend([to_x(t), to_y(br)])
            self.thumbnail_canvas.create_line(line_coords, fill="#90caf9", width=1)
        
        self._create_selection_items()
    
    def _create_selection_items(self):
        """创建选择框元素（首次）"""
        if not self.thumbnail_info:
            return
        
        info = self.thumbnail_info
        margin = info["margin"]
        chart_w = info["chart_w"]
        height = info["height"]
        
        x1 = margin["left"] + self.view_start * chart_w
        x2 = margin["left"] + self.view_end * chart_w
        y1 = margin["top"]
        y2 = height - margin["bottom"]
        
        handle_w = int(6 * self.dpi_scale)
        
        self.selection_items = {
            'left_mask': self.thumbnail_canvas.create_rectangle(
                margin["left"], y1, x1, y2,
                fill="#000000", stipple="gray50", outline=""
            ),
            'right_mask': self.thumbnail_canvas.create_rectangle(
                x2, y1, margin["left"] + chart_w, y2,
                fill="#000000", stipple="gray50", outline=""
            ),
            'box': self.thumbnail_canvas.create_rectangle(
                x1, y1, x2, y2,
                fill="", outline="#1976D2", width=2
            ),
            'handle_left': self.thumbnail_canvas.create_rectangle(
                x1 - handle_w // 2, y1, x1 + handle_w // 2, y2,
                fill="#1976D2", outline=""
            ),
            'handle_right': self.thumbnail_canvas.create_rectangle(
                x2 - handle_w // 2, y1, x2 + handle_w // 2, y2,
                fill="#1976D2", outline=""
            ),
        }
    
    def _update_selection_coords(self):
        """只更新选择框坐标（不重绘）- 用于拖动时"""
        if not self.thumbnail_info or not self.selection_items:
            return
        
        info = self.thumbnail_info
        margin = info["margin"]
        chart_w = info["chart_w"]
        height = info["height"]
        
        x1 = margin["left"] + self.view_start * chart_w
        x2 = margin["left"] + self.view_end * chart_w
        y1 = margin["top"]
        y2 = height - margin["bottom"]
        
        handle_w = int(6 * self.dpi_scale)
        
        self.thumbnail_canvas.coords(self.selection_items['left_mask'], margin["left"], y1, x1, y2)
        self.thumbnail_canvas.coords(self.selection_items['right_mask'], x2, y1, margin["left"] + chart_w, y2)
        self.thumbnail_canvas.coords(self.selection_items['box'], x1, y1, x2, y2)
        self.thumbnail_canvas.coords(self.selection_items['handle_left'], x1 - handle_w // 2, y1, x1 + handle_w // 2, y2)
        self.thumbnail_canvas.coords(self.selection_items['handle_right'], x2 - handle_w // 2, y1, x2 + handle_w // 2, y2)
    
    def zoom(self, factor):
        if not self.bitrate_data:
            return
        
        center = (self.view_start + self.view_end) / 2
        current_range = self.view_end - self.view_start
        new_range = current_range / factor
        new_range = max(self.min_view_range, min(1.0, new_range))
        
        new_start = center - new_range / 2
        new_end = center + new_range / 2
        
        if new_start < 0:
            new_start = 0
            new_end = new_range
        if new_end > 1:
            new_end = 1
            new_start = 1 - new_range
        
        self.view_start = max(0, new_start)
        self.view_end = min(1, new_end)
        
        self.update_zoom_label()
        self.draw_chart()
        self._update_selection_coords()
    
    def reset_view(self):
        self.view_start = 0.0
        self.view_end = 1.0
        self.update_zoom_label()
        self.draw_chart()
        self._update_selection_coords()
    
    def update_zoom_label(self):
        view_range = self.view_end - self.view_start
        percentage = view_range * 100
        self.zoom_label.config(text=f"显示: {percentage:.1f}%")
    
    def on_mouse_wheel(self, event):
        if not self.bitrate_data or not self.chart_info:
            return
        
        if event.num == 4 or event.delta > 0:
            factor = 1.3
        else:
            factor = 0.77
        
        info = self.chart_info
        margin = info["margin"]
        
        if margin["left"] <= event.x <= info["width"] - margin["right"]:
            rel_x = (event.x - margin["left"]) / info["chart_w"]
            center = self.view_start + rel_x * (self.view_end - self.view_start)
        else:
            center = (self.view_start + self.view_end) / 2
        
        current_range = self.view_end - self.view_start
        new_range = current_range / factor
        new_range = max(self.min_view_range, min(1.0, new_range))
        
        left_ratio = (center - self.view_start) / current_range if current_range > 0 else 0.5
        new_start = center - left_ratio * new_range
        new_end = new_start + new_range
        
        if new_start < 0:
            new_start = 0
            new_end = new_range
        if new_end > 1:
            new_end = 1
            new_start = 1 - new_range
        
        self.view_start = max(0, new_start)
        self.view_end = min(1, new_end)
        
        self.update_zoom_label()
        self.draw_chart()
        self._update_selection_coords()
    
    def on_thumbnail_press(self, event):
        if not self.thumbnail_info or not self.bitrate_data:
            return
        
        info = self.thumbnail_info
        margin = info["margin"]
        chart_w = info["chart_w"]
        
        x1 = margin["left"] + self.view_start * chart_w
        x2 = margin["left"] + self.view_end * chart_w
        
        handle_w = int(10 * self.dpi_scale)
        
        if abs(event.x - x1) < handle_w:
            self.thumbnail_dragging = True
            self.thumbnail_drag_mode = 'left'
        elif abs(event.x - x2) < handle_w:
            self.thumbnail_dragging = True
            self.thumbnail_drag_mode = 'right'
        elif x1 < event.x < x2:
            self.thumbnail_dragging = True
            self.thumbnail_drag_mode = 'move'
        else:
            rel_x = (event.x - margin["left"]) / chart_w
            rel_x = max(0, min(1, rel_x))
            
            view_range = self.view_end - self.view_start
            new_start = rel_x - view_range / 2
            new_end = rel_x + view_range / 2
            
            if new_start < 0:
                new_start = 0
                new_end = view_range
            if new_end > 1:
                new_end = 1
                new_start = 1 - view_range
            
            self.view_start = new_start
            self.view_end = new_end
            
            self.thumbnail_dragging = True
            self.thumbnail_drag_mode = 'move'
            
            self.update_zoom_label()
            self._update_selection_coords()
            self.draw_chart()
        
        self.thumbnail_drag_start_x = event.x
        self.thumbnail_drag_start_view = (self.view_start, self.view_end)
    
    def on_thumbnail_drag(self, event):
        """拖动时只更新选择框，不重绘主图表"""
        if not self.thumbnail_dragging or not self.thumbnail_info:
            return
        
        info = self.thumbnail_info
        margin = info["margin"]
        chart_w = info["chart_w"]
        
        dx = event.x - self.thumbnail_drag_start_x
        d_ratio = dx / chart_w
        
        start_view_start, start_view_end = self.thumbnail_drag_start_view
        
        if self.thumbnail_drag_mode == 'move':
            view_range = start_view_end - start_view_start
            new_start = start_view_start + d_ratio
            new_end = start_view_end + d_ratio
            
            if new_start < 0:
                new_start = 0
                new_end = view_range
            if new_end > 1:
                new_end = 1
                new_start = 1 - view_range
            
            self.view_start = new_start
            self.view_end = new_end
            
        elif self.thumbnail_drag_mode == 'left':
            new_start = start_view_start + d_ratio
            new_start = max(0, min(start_view_end - self.min_view_range, new_start))
            self.view_start = new_start
            
        elif self.thumbnail_drag_mode == 'right':
            new_end = start_view_end + d_ratio
            new_end = max(start_view_start + self.min_view_range, min(1, new_end))
            self.view_end = new_end
        
        self.update_zoom_label()
        self._update_selection_coords()
        
        if self.pending_chart_draw:
            self.root.after_cancel(self.pending_chart_draw)
        self.pending_chart_draw = self.root.after(100, self.draw_chart)
    
    def on_thumbnail_release(self, event):
        """松开时才绑制最终的主图表"""
        if not self.thumbnail_dragging:
            return
        
        self.thumbnail_dragging = False
        self.thumbnail_drag_mode = None
        
        if self.pending_chart_draw:
            self.root.after_cancel(self.pending_chart_draw)
            self.pending_chart_draw = None
        
        self.draw_chart()
    
    def on_thumbnail_double_click(self, event):
        self.reset_view()
    
    def on_thumbnail_resize(self, event):
        if self.bitrate_data:
            if self.pending_thumbnail_draw:
                self.root.after_cancel(self.pending_thumbnail_draw)
            self.pending_thumbnail_draw = self.root.after(150, self.draw_thumbnail)
    
    def on_canvas_resize(self, event):
        if self.bitrate_data:
            if self.pending_chart_draw:
                self.root.after_cancel(self.pending_chart_draw)
            self.pending_chart_draw = self.root.after(150, self.draw_chart)
    
    def on_mouse_move(self, event):
        current_time = time_module.time() * 1000
        
        if current_time - self.last_mouse_update < self.mouse_throttle_ms:
            if self.pending_mouse_update:
                self.root.after_cancel(self.pending_mouse_update)
            self.pending_mouse_update = self.root.after(
                self.mouse_throttle_ms,
                lambda: self._do_mouse_update(event.x, event.y)
            )
            return
        
        self.last_mouse_update = current_time
        self._do_mouse_update(event.x, event.y)
    
    def _do_mouse_update(self, x, y):
        for item in self.crosshair_items:
            self.canvas.delete(item)
        self.crosshair_items = []
        
        if not self.chart_info or not self.current_visible_points:
            return
        
        info = self.chart_info
        chart_left = info["chart_left"]
        chart_right = info["chart_right"]
        chart_top = info["chart_top"]
        chart_bottom = info["chart_bottom"]
        
        if not (chart_left <= x <= chart_right and chart_top <= y <= chart_bottom):
            self.cursor_info_label.config(text="")
            self.hide_preview()  # 鼠标离开图表区域时隐藏预览
            return
        
        min_dist = float('inf')
        closest_point = None
        
        for point_x, point_y, t, br in self.current_visible_points:
            if chart_left <= point_x <= chart_right:
                dist = abs(point_x - x)
                if dist < min_dist:
                    min_dist = dist
                    closest_point = (point_x, point_y, t, br)
        
        if closest_point is None:
            self.cursor_info_label.config(text="")
            self.hide_preview()
            return
        
        point_x, point_y, t, br = closest_point
        point_x = max(chart_left, min(chart_right, point_x))
        point_y = max(chart_top, min(chart_bottom, point_y))
        
        v_line = self.canvas.create_line(
            point_x, chart_top, point_x, chart_bottom,
            fill="#f44336", dash=(3, 3), width=1
        )
        h_line = self.canvas.create_line(
            chart_left, point_y, chart_right, point_y,
            fill="#f44336", dash=(3, 3), width=1
        )
        
        r = int(5 * self.dpi_scale)
        point_marker = self.canvas.create_oval(
            point_x - r, point_y - r, point_x + r, point_y + r,
            fill="#f44336", outline="white", width=2
        )
        
        self.crosshair_items = [v_line, h_line, point_marker]
        
        br_str = f"{br/1000:.2f} Mbps" if br >= 1000 else f"{br:.0f} Kbps"
        time_str = self.format_time_with_frames(t)
        self.cursor_info_label.config(text=f"⏱ {time_str}  |  📊 {br_str}")
        
        # 请求视频预览
        if self.show_preview:
            self.request_preview(t, x, y)
    
    def on_mouse_leave(self, event):
        if self.pending_mouse_update:
            self.root.after_cancel(self.pending_mouse_update)
            self.pending_mouse_update = None
        
        for item in self.crosshair_items:
            self.canvas.delete(item)
        self.crosshair_items = []
        self.cursor_info_label.config(text="")
        
        # 隐藏预览窗口
        self.hide_preview()
    
    def update_video_info(self, duration, video_info):
        if not video_info:
            return
        
        codec = video_info.get("codec", "N/A")
        width = video_info.get("width", 0)
        height = video_info.get("height", 0)
        fps = video_info.get("fps", "N/A")
        size_mb = video_info.get("size", 0) / 1024 / 1024
        
        info_parts = [
            f"编码: {codec}", f"分辨率: {width}×{height}",
            f"帧率: {fps} fps", f"时长: {self.format_time_short(duration)}",
            f"大小: {size_mb:.1f} MB",
        ]
        self.video_info_label.config(text="  |  ".join(info_parts))
    
    def update_progress(self, value, text):
        def _update():
            self.progress_var.set(value)
            self.status_label.config(text=text)
        self.root.after(0, _update)


def main():
    if platform.system() == "Windows":
        multiprocessing.freeze_support()
    
    root = tk.Tk()
    app = BitrateAnalyzer(root)
    root.mainloop()


if __name__ == "__main__":
    main()