import pysubs2
import os
import sys
import subprocess
import tempfile
import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import time

# 常量定义
SETTINGS_FILE = "subtitle_tool_settings.json"
DEFAULT_FILTER_WORDS = [
    "OK", "okay", "啊", "那", "嗯", "哦", "呃", "对吧", "对不对", 
    "对不对啊", "懂了吧", "是不是", "明白吗", "明白了吗", "明白了吧", 
    "清楚了吧", "听懂没有", "听懂了吗", "能明白吧", "能明白吗", 
    "能听懂吗", "能听懂了吗", "听明白了吗", "听懂了没有", "听懂了吧", 
    "这样能听明白吗"
]

# 支持的音频格式
AUDIO_FORMATS = {
    "WAV (无损)": {"ext": "wav", "codec": "pcm_s16le"},
    "MP3 (高质量)": {"ext": "mp3", "codec": "libmp3lame", "options": ["-q:a", "0"]},
    "MP3 (中等质量)": {"ext": "mp3", "codec": "libmp3lame", "options": ["-q:a", "4"]},
    "AAC (高质量)": {"ext": "m4a", "codec": "aac", "options": ["-b:a", "256k"]},
    "AAC (中等质量)": {"ext": "m4a", "codec": "aac", "options": ["-b:a", "128k"]},
    "FLAC (无损压缩)": {"ext": "flac", "codec": "flac"},
    "OGG (高质量)": {"ext": "ogg", "codec": "libvorbis", "options": ["-q:a", "8"]},
    "OGG (中等质量)": {"ext": "ogg", "codec": "libvorbis", "options": ["-q:a", "5"]}
}

# 核心处理函数
class SubtitleProcessor:
    @staticmethod
    def process_subtitles(subs, start_line, end_line, filter_words, progress_callback=None):
        """处理字幕，过滤指定词语并调整时间轴"""
        if progress_callback:
            progress_callback("筛选保留字幕...")
        
        # 筛选需要保留的字幕
        filtered_events = []
        total_lines = min(end_line, len(subs.events)) - (start_line - 1)
        
        for i in range(start_line - 1, min(end_line, len(subs.events))):
            event = subs.events[i]
            if event.plaintext.strip() not in filter_words:
                new_event = event.copy()
                filtered_events.append({
                    "event": new_event,
                    "original_start": event.start,
                    "original_end": event.end,
                    "duration": event.end - event.start
                })
            
            if progress_callback and i % 100 == 0:
                progress_callback(f"已筛选: {i-start_line+2}/{total_lines}行")
        
        if progress_callback:
            progress_callback("调整时间轴...")
        
        # 按原始时间排序
        filtered_events.sort(key=lambda x: x["original_start"])
        
        # 调整时间轴，使字幕连续播放
        current_time = 0
        for item in filtered_events:
            item["event"].start = current_time
            item["event"].end = current_time + item["duration"]
            current_time += item["duration"]
        
        # 创建结果
        result = pysubs2.SSAFile()
        result.info = subs.info.copy()
        result.styles = subs.styles.copy()
        result.events = [item["event"] for item in filtered_events]
        
        # 构建用于音频剪辑的片段信息
        segments = []
        for item in filtered_events:
            segments.append({
                "start_ms": item["original_start"],
                "end_ms": item["original_end"],
                "adjusted_start_ms": item["event"].start,
                "keep": True,
                "keep_events": [item]
            })
        
        if progress_callback:
            progress_callback(f"字幕处理完成，保留了 {len(filtered_events)}/{total_lines} 行")
        
        return result, segments
    
    @staticmethod
    def cut_audio_by_segments(audio_path, output_audio_path, segments, audio_format, gap_threshold=0.1, min_duration=0.05, progress_callback=None):
        """
        剪辑音频，匹配字幕时间轴
        
        参数:
            audio_path: 输入音频路径
            output_audio_path: 输出音频路径
            segments: 片段信息
            audio_format: 音频格式信息字典，包含codec和options
            gap_threshold: 合并间隔阈值(秒)
            min_duration: 最小片段时长(秒)
            progress_callback: 进度回调函数
        """
        if progress_callback:
            progress_callback("准备音频片段...")
        
        # 收集需要保留的片段
        keep_segments = []
        for seg in segments:
            if seg.get("keep"):
                for item in seg["keep_events"]:
                    start = item["original_start"] / 1000
                    end = item["original_end"] / 1000
                    
                    # 确保最小时长
                    if end - start < min_duration:
                        end = start + min_duration
                        
                    keep_segments.append({"start": start, "end": end})
        
        if not keep_segments:
            if progress_callback:
                progress_callback("没有找到需要保留的片段")
            raise ValueError("没有需要保留的片段")
        
        # 排序并合并接近的片段
        if progress_callback:
            progress_callback("合并接近片段...")
            
        keep_segments.sort(key=lambda x: x["start"])
        merged_segments = []
        current = keep_segments[0]
        
        for next_seg in keep_segments[1:]:
            if next_seg["start"] - current["end"] <= gap_threshold:
                # 合并片段
                current["end"] = max(current["end"], next_seg["end"])
            else:
                merged_segments.append(current)
                current = next_seg
        
        merged_segments.append(current)
        
        if progress_callback:
            progress_callback(f"音频处理: {len(merged_segments)} 个片段")
        
        # 获取编码器和选项
        codec = audio_format.get("codec", "pcm_s16le")
        extra_options = audio_format.get("options", [])
        
        # 使用临时目录
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                # 先创建一个临时WAV文件
                temp_wav = os.path.join(temp_dir, "temp_output.wav")
                
                # 方法1: 复杂滤镜方法 (适用于片段数量适中的情况)
                if len(merged_segments) <= 200:
                    if progress_callback:
                        progress_callback("使用滤镜处理音频...")
                    
                    filter_parts = []
                    concat_parts = []
                    
                    for i, seg in enumerate(merged_segments):
                        segment_id = f"seg_{i}"
                        filter_parts.append(f"[0:a]atrim=start={seg['start']:.6f}:end={seg['end']:.6f},asetpts=PTS-STARTPTS[{segment_id}]")
                        concat_parts.append(f"[{segment_id}]")
                    
                    filter_complex = ";".join(filter_parts)
                    if len(merged_segments) > 1:
                        filter_complex += ";" + "".join(concat_parts) + f"concat=n={len(merged_segments)}:v=0:a=1[out]"
                        map_param = "[out]"
                    else:
                        map_param = concat_parts[0]
                    
                    command = [
                        "ffmpeg", "-y", 
                        "-i", audio_path,
                        "-filter_complex", filter_complex,
                        "-map", map_param, 
                        "-acodec", "pcm_s16le",  # 先用无损格式
                        temp_wav
                    ]
                    
                    subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
                    
                # 方法2: EDL方法 (适用于片段数量较多的情况)
                else:
                    if progress_callback:
                        progress_callback("使用EDL方式处理音频...")
                    
                    edl_path = os.path.join(temp_dir, "segments.edl")
                    with open(edl_path, "w", encoding="utf-8") as f:
                        for seg in merged_segments:
                            f.write(f"file '{audio_path}'\n")
                            f.write(f"inpoint {seg['start']:.6f}\n")
                            f.write(f"outpoint {seg['end']:.6f}\n")
                    
                    command = [
                        "ffmpeg", "-y", 
                        "-f", "concat", 
                        "-safe", "0",
                        "-i", edl_path, 
                        "-c:a", "pcm_s16le",  # 先用无损格式
                        temp_wav
                    ]
                    
                    subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
                
                # 转换为最终格式
                if progress_callback:
                    progress_callback(f"转换为{codec}格式...")
                
                final_command = ["ffmpeg", "-y", "-i", temp_wav, "-c:a", codec]
                
                # 添加额外编码选项
                if extra_options:
                    final_command.extend(extra_options)
                
                final_command.append(output_audio_path)
                
                subprocess.run(final_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
                
                if progress_callback:
                    progress_callback("音频处理完成")
                
                return True
            
            except Exception as e:
                # 备用方法
                if progress_callback:
                    progress_callback(f"尝试备用方法: {str(e)[:50]}...")
                
                try:
                    # 单文件EDL备用方法
                    concat_file = os.path.join(temp_dir, "concat.txt")
                    with open(concat_file, "w", encoding="utf-8") as f:
                        for i, seg in enumerate(merged_segments):
                            temp_file = os.path.join(temp_dir, f"part_{i:04d}.wav")
                            
                            # 截取单个片段
                            cmd = [
                                "ffmpeg", "-y", 
                                "-i", audio_path,
                                "-ss", f"{seg['start']:.6f}",
                                "-to", f"{seg['end']:.6f}",
                                "-c:a", "pcm_s16le",
                                temp_file
                            ]
                            
                            if i % 10 == 0 and progress_callback:
                                progress_callback(f"处理片段 {i+1}/{len(merged_segments)}...")
                            
                            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            f.write(f"file '{temp_file}'\n")
                    
                    if progress_callback:
                        progress_callback("合并音频片段...")
                    
                    # 先合并为WAV
                    temp_wav = os.path.join(temp_dir, "merged.wav")
                    merge_cmd = [
                        "ffmpeg", "-y", 
                        "-f", "concat",
                        "-safe", "0",
                        "-i", concat_file,
                        "-c:a", "pcm_s16le",
                        temp_wav
                    ]
                    
                    subprocess.run(merge_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    
                    # 转换为最终格式
                    if progress_callback:
                        progress_callback(f"转换为{codec}格式...")
                    
                    final_command = ["ffmpeg", "-y", "-i", temp_wav, "-c:a", codec]
                    
                    # 添加额外编码选项
                    if extra_options:
                        final_command.extend(extra_options)
                    
                    final_command.append(output_audio_path)
                    
                    subprocess.run(final_command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    
                    if progress_callback:
                        progress_callback("音频处理完成")
                    
                    return True
                
                except Exception as e:
                    if progress_callback:
                        progress_callback(f"音频处理失败: {str(e)[:50]}")
                    return False
    
    @staticmethod
    def export_segments_json(segments, output_json_path):
        """导出片段映射信息到JSON文件"""
        data = []
        for seg in segments:
            if seg.get("keep"):
                for item in seg["keep_events"]:
                    data.append({
                        "original_start": round(item["original_start"] / 1000, 3),
                        "original_end": round(item["original_end"] / 1000, 3),
                        "duration": round((item["original_end"] - item["original_start"]) / 1000, 3),
                        "adjusted_start": round(item["event"].start / 1000, 3)
                    })
        
        with open(output_json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

# 工具类
class AppUtils:
    @staticmethod
    def save_settings(input_path, output_path, start_line, end_line, filter_path_value, gap_threshold=0.1, audio_format="WAV (无损)"):
        """保存应用设置"""
        settings = {
            "input_path": input_path,
            "output_path": output_path,
            "start_line": start_line,
            "end_line": end_line,
            "filter_path": filter_path_value,
            "gap_threshold": gap_threshold,
            "audio_format": audio_format
        }
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f)

    @staticmethod
    def load_settings():
        """加载应用设置"""
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except:
                return {}
        return {}

# GUI应用类
class SubtitleEditorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("字幕剪辑工具")
        self.root.geometry("1080x700")
        
        # 设置应用图标和样式
        self.style = ttk.Style()
        self.style.configure("TButton", padding=5)
        self.style.configure("TLabelframe", padding=10)
        
        # 加载保存的设置
        self.saved_settings = AppUtils.load_settings()
        self.filter_words = DEFAULT_FILTER_WORDS.copy()
        self.audio_file = ""
        self.gap_threshold = float(self.saved_settings.get("gap_threshold", 0.1))
        
        # 创建变量
        self.input_path = tk.StringVar(value=self.saved_settings.get("input_path", ""))
        self.output_path = tk.StringVar(value=self.saved_settings.get("output_path", ""))
        self.filter_path = tk.StringVar(value=self.saved_settings.get("filter_path", ""))
        self.total_label = tk.StringVar(value="总行数: 未加载")
        self.progress_var = tk.StringVar(value="就绪")
        self.audio_label = tk.StringVar(value="未选择音频文件")
        self.filter_count_label = tk.StringVar(value=f"默认过滤词: {len(self.filter_words)} 个")
        self.gap_threshold_var = tk.StringVar(value=str(self.gap_threshold))
        self.audio_format_var = tk.StringVar(value=self.saved_settings.get("audio_format", "WAV (无损)"))
        
        # 创建界面
        self.create_widgets()
        
        # 加载已保存的字幕文件(如果有)
        if self.input_path.get():
            try:
                subs = pysubs2.load(self.input_path.get())
                self.total_label.set(f"总行数: {len(subs.events)}")
                if not self.start_entry.get():
                    self.start_entry.insert(0, "1")
                if not self.end_entry.get():
                    self.end_entry.insert(0, str(len(subs.events)))
            except:
                pass
    
    def create_widgets(self):
        """创建GUI界面"""
        # 创建主框架
        main_paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main_paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # 左侧控制面板
        left_frame = ttk.Frame(main_paned)
        main_paned.add(left_frame, weight=40)
        
        # 右侧预览区域
        right_frame = ttk.LabelFrame(main_paned, text="处理后字幕预览")
        main_paned.add(right_frame, weight=60)
        
        # ===== 左侧控制面板 =====
        # 1. 文件选择区域
        file_frame = ttk.LabelFrame(left_frame, text="文件选择")
        file_frame.pack(fill="x", padx=5, pady=5)
        
        # 字幕文件选择
        ttk.Label(file_frame, text="字幕文件:").pack(anchor="w", pady=(5, 0))
        input_frame = ttk.Frame(file_frame)
        input_frame.pack(fill="x", pady=2)
        ttk.Entry(input_frame, textvariable=self.input_path).pack(side="left", fill="x", expand=True)
        ttk.Button(input_frame, text="浏览", command=self.load_subtitle).pack(side="right", padx=(5, 0))
        ttk.Label(file_frame, textvariable=self.total_label).pack(anchor="w")
        
        # 输出文件选择
        ttk.Label(file_frame, text="输出字幕:").pack(anchor="w", pady=(8, 0))
        output_frame = ttk.Frame(file_frame)
        output_frame.pack(fill="x", pady=2)
        ttk.Entry(output_frame, textvariable=self.output_path).pack(side="left", fill="x", expand=True)
        ttk.Button(output_frame, text="浏览", 
                  command=lambda: self.output_path.set(filedialog.asksaveasfilename(defaultextension=".ass"))).pack(side="right", padx=(5, 0))
        
        # 音频文件选择
        ttk.Label(file_frame, text="音频文件 (可选):").pack(anchor="w", pady=(8, 0))
        audio_frame = ttk.Frame(file_frame)
        audio_frame.pack(fill="x", pady=2)
        ttk.Label(audio_frame, textvariable=self.audio_label).pack(side="left", fill="x", expand=True)
        ttk.Button(audio_frame, text="选择", command=self.choose_audio).pack(side="right")
        ttk.Button(audio_frame, text="清除", command=self.clear_audio).pack(side="right", padx=(0, 5))
        
        # 2. 处理选项区域
        option_frame = ttk.LabelFrame(left_frame, text="处理选项")
        option_frame.pack(fill="x", padx=5, pady=(10, 5))
        
        # 行号范围
        range_frame = ttk.Frame(option_frame)
        range_frame.pack(fill="x", pady=5)
        ttk.Label(range_frame, text="起始行:").pack(side="left")
        self.start_entry = ttk.Entry(range_frame, width=8)
        self.start_entry.insert(0, self.saved_settings.get("start_line", "1"))
        self.start_entry.pack(side="left", padx=(5, 15))
        ttk.Label(range_frame, text="结束行:").pack(side="left")
        self.end_entry = ttk.Entry(range_frame, width=8)
        self.end_entry.insert(0, self.saved_settings.get("end_line", ""))
        self.end_entry.pack(side="left", padx=5)
        
        # 间隔阈值设置
        threshold_frame = ttk.Frame(option_frame)
        threshold_frame.pack(fill="x", pady=5)
        ttk.Label(threshold_frame, text="合并间隔阈值(秒):").pack(side="left")
        ttk.Entry(threshold_frame, textvariable=self.gap_threshold_var, width=6).pack(side="left", padx=5)
        ttk.Label(threshold_frame, text="(小于此值的间隔将被视为连续)").pack(side="left")
        
        # 音频格式选择
        audio_format_frame = ttk.Frame(option_frame)
        audio_format_frame.pack(fill="x", pady=5)
        ttk.Label(audio_format_frame, text="音频输出格式:").pack(side="left")
        audio_format_combo = ttk.Combobox(audio_format_frame, textvariable=self.audio_format_var, 
                                         values=list(AUDIO_FORMATS.keys()), width=20)
        audio_format_combo.pack(side="left", padx=5)
        audio_format_combo.current(list(AUDIO_FORMATS.keys()).index(self.audio_format_var.get()) 
                                  if self.audio_format_var.get() in AUDIO_FORMATS else 0)
        
        # 3. 过滤词设置区域
        filter_frame = ttk.LabelFrame(left_frame, text="过滤词设置")
        filter_frame.pack(fill="x", padx=5, pady=(10, 5))
        
        ttk.Label(filter_frame, textvariable=self.filter_count_label).pack(anchor="w", pady=(5, 0))
        filter_file_frame = ttk.Frame(filter_frame)
        filter_file_frame.pack(fill="x", pady=5)
        ttk.Label(filter_file_frame, text="过滤词文件:").pack(side="left")
        ttk.Entry(filter_file_frame, textvariable=self.filter_path).pack(side="left", fill="x", expand=True, padx=(5, 5))
        ttk.Button(filter_file_frame, text="浏览", command=self.choose_filter_file).pack(side="right")
        
        # 4. 操作区域
        action_frame = ttk.LabelFrame(left_frame, text="操作")
        action_frame.pack(fill="x", padx=5, pady=(10, 5))
        
        # 处理按钮
        self.process_btn = ttk.Button(action_frame, text="开始处理", command=self.run_async, style="Accent.TButton")
        self.process_btn.pack(pady=10)
        
        # 进度显示
        progress_frame = ttk.Frame(action_frame)
        progress_frame.pack(fill="x", pady=5)
        ttk.Label(progress_frame, text="状态:").pack(side="left")
        ttk.Label(progress_frame, textvariable=self.progress_var, wraplength=300).pack(side="left", padx=5, fill="x", expand=True)
        
        # 5. 帮助区域
        help_frame = ttk.LabelFrame(left_frame, text="帮助")
        help_frame.pack(fill="x", padx=5, pady=(10, 5))
        
        help_text = (
            "操作步骤:\n"
            "1. 选择ASS格式字幕文件\n"
            "2. 设置处理行号范围\n"
            "3. 选择音频输出格式(可选)\n"
            "4. 可选择音频文件一起处理\n"
            "5. 可自定义过滤词文件\n"
            "6. 点击「开始处理」按钮\n\n"
            "过滤词文件格式:\n每行一个词，将被自动过滤"
        )
        ttk.Label(help_frame, text=help_text, justify="left", wraplength=300).pack(padx=5, pady=5)
        
        # ===== 右侧预览区域 =====
        # 预览文本框
        preview_frame = ttk.Frame(right_frame)
        preview_frame.pack(fill="both", expand=True, padx=5, pady=5)
        
        # 滚动条
        preview_scroll = ttk.Scrollbar(preview_frame)
        preview_scroll.pack(side="right", fill="y")
        
        # 文本框
        self.preview_text = tk.Text(preview_frame, wrap="word", yscrollcommand=preview_scroll.set)
        self.preview_text.pack(side="left", fill="both", expand=True)
        preview_scroll.config(command=self.preview_text.yview)
        
        # 设置初始提示文本
        self.preview_text.insert("1.0", "处理后的字幕将显示在这里...\n\n"
                                       "提示: 处理完成后，可以在此预览处理结果，\n"
                                       "确认无误后再使用输出的字幕文件。")
    
    def load_subtitle(self):
        """加载字幕文件"""
        path = filedialog.askopenfilename(filetypes=[("ASS 文件", "*.ass")])
        if path:
            self.input_path.set(path)
            try:
                subs = pysubs2.load(path)
                self.total_label.set(f"总行数: {len(subs.events)}")
                self.start_entry.delete(0, tk.END)
                self.start_entry.insert(0, "1")
                self.end_entry.delete(0, tk.END)
                self.end_entry.insert(0, str(len(subs.events)))
                self.output_path.set(os.path.splitext(path)[0] + "_cut.ass")
            except Exception as e:
                self.total_label.set("加载失败")
                messagebox.showerror("错误", f"加载字幕失败: {str(e)}")
    
    def choose_audio(self):
        """选择音频文件"""
        path = filedialog.askopenfilename(filetypes=[("音频文件", "*.mp3 *.wav *.aac *.flac *.m4a *.ogg")])
        if path:
            self.audio_file = path
            self.audio_label.set(os.path.basename(path))
    
    def clear_audio(self):
        """清除音频文件选择"""
        self.audio_file = ""
        self.audio_label.set("未选择音频文件")
    
    def choose_filter_file(self):
        """选择过滤词文件"""
        path = filedialog.askopenfilename(filetypes=[("文本文件", "*.txt")])
        if path:
            self.filter_path.set(path)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.filter_words.clear()
                    for line in f:
                        line = line.strip()
                        if line:
                            self.filter_words.append(line)
                    self.filter_count_label.set(f"已加载 {len(self.filter_words)} 个过滤词")
            except Exception as e:
                messagebox.showerror("错误", f"加载过滤词失败: {str(e)}")
    
    def run_processing(self):
        """执行处理逻辑"""
        try:
            # 更新状态
            self.progress_var.set("开始处理...")
            self.process_btn.config(state="disabled")
            self.root.update()
            
            # 验证输入
            if not self.input_path.get():
                raise ValueError("请选择输入字幕文件")
            if not self.output_path.get():
                raise ValueError("请设置输出字幕路径")
            
            # 解析参数
            try:
                start = int(self.start_entry.get() or 1)
                end = int(self.end_entry.get() or 999999)
                gap_threshold = float(self.gap_threshold_var.get() or 0.1)
            except ValueError:
                raise ValueError("行号和间隔阈值必须是有效的数字")
            
            # 获取选择的音频格式
            audio_format_name = self.audio_format_var.get()
            if audio_format_name not in AUDIO_FORMATS:
                audio_format_name = "WAV (无损)"
            audio_format = AUDIO_FORMATS[audio_format_name]
            
            # 加载字幕
            self.progress_var.set("加载字幕文件...")
            self.root.update()
            subs = pysubs2.load(self.input_path.get())
            
            # 处理字幕
            self.progress_var.set(f"处理字幕 ({start} 到 {min(end, len(subs.events))} 行)...")
            self.root.update()
            
            # 创建进度回调
            def update_progress(message):
                self.progress_var.set(message)
                self.root.update()
            
            # 处理字幕
            edited, segments = SubtitleProcessor.process_subtitles(
                subs, start, end, self.filter_words, progress_callback=update_progress
            )
            
            # 保存字幕
            self.progress_var.set("保存字幕文件...")
            self.root.update()
            edited.save(self.output_path.get())
            
            # 更新预览
            self.progress_var.set("更新预览...")
            self.root.update()
            self.preview_text.delete("1.0", tk.END)
            
            for event in edited.events:
                self.preview_text.insert(tk.END, f"[{event.start/1000:.3f}s - {event.end/1000:.3f}s] {event.plaintext.strip()}\n")
            
            # 处理音频(如果有)
            if self.audio_file:
                base = os.path.splitext(self.output_path.get())[0]
                output_audio = f"{base}.{audio_format['ext']}"
                output_json = base + "_map.json"
                
                # 音频处理
                self.progress_var.set(f"处理音频 ({audio_format_name})...")
                self.root.update()
                
                # 使用线程更新进度
                audio_progress = {"status": "准备中..."}
                
                def update_audio_progress():
                    last_status = ""
                    while audio_progress["status"] != "完成":
                        if audio_progress["status"] != last_status:
                            self.progress_var.set(f"音频处理: {audio_progress['status']}")
                            self.root.update()
                            last_status = audio_progress["status"]
                        time.sleep(0.5)
                
                progress_thread = threading.Thread(target=update_audio_progress, daemon=True)
                progress_thread.start()
                
                # 调用音频剪辑函数
                audio_progress["status"] = "分析片段..."
                success = SubtitleProcessor.cut_audio_by_segments(
                    self.audio_file, output_audio, segments, 
                    audio_format=audio_format,
                    gap_threshold=gap_threshold,
                    progress_callback=lambda msg: audio_progress.update({"status": msg})
                )
                
                audio_progress["status"] = "完成"
                progress_thread.join(timeout=1.0)
                
                # 导出片段映射
                self.progress_var.set("导出片段映射...")
                self.root.update()
                SubtitleProcessor.export_segments_json(segments, output_json)
                
                if success:
                    messagebox.showinfo("完成", f"处理完成！已保存：\n\n字幕：{self.output_path.get()}\n音频：{output_audio}")
                else:
                    messagebox.showwarning("部分完成", f"字幕已保存，但音频处理失败。\n\n已保存：{self.output_path.get()}")
            else:
                messagebox.showinfo("完成", f"字幕处理完成！\n\n已保存：{self.output_path.get()}")
            
            # 保存设置
            AppUtils.save_settings(
                self.input_path.get(),
                self.output_path.get(),
                self.start_entry.get(),
                self.end_entry.get(),
                self.filter_path.get(),
                gap_threshold,
                audio_format_name
            )
            
            # 更新状态
            self.progress_var.set("处理完成")
            self.process_btn.config(state="normal")
            
        except Exception as e:
            self.progress_var.set(f"处理失败: {str(e)}")
            self.process_btn.config(state="normal")
            messagebox.showerror("错误", str(e))
    
    def run_async(self):
        """在线程中异步执行处理"""
        threading.Thread(target=self.run_processing, daemon=True).start()

def launch_gui():
    """启动GUI应用"""
    root = tk.Tk()
    app = SubtitleEditorApp(root)
    root.mainloop()

if __name__ == "__main__":
    if "--gui" in sys.argv:
        launch_gui()
    else:
        print("请使用 --gui 启动图形界面")