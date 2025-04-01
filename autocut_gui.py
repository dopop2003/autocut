#autocut_gui.py v2.4.3
import tkinter as tk 
from tkinter import filedialog, messagebox, ttk 
import threading 
import sys 
import json 
import os 
import re 
from autocut_core import main, parse_srt 
 
class TextRedirector:
    def __init__(self, widget, status_callback=None):
        self.widget  = widget 
        self.status_callback  = status_callback 
        self.step_pattern  = re.compile(r' 步骤(\d)/4: (.*?)\.\.\.')
 
    def write(self, text):
        self.widget.insert(tk.END,  text)
        self.widget.see(tk.END) 
        self.widget.update_idletasks() 
        
        if self.status_callback: 
            match = self.step_pattern.search(text) 
            if match:
                step, description = match.groups() 
                self.status_callback(f" 步骤 {step}/4 - {description}", int(step)*25)

    def flush(self):
        pass

class AutoCutGUI:
    def __init__(self, root):
        self.root  = root 
        self.root.title("🎬  AutoCut 音频剪辑工具")
        self.root.geometry("1000x600") 
        self.root.minsize(900, 600)
        self.root.configure(bg="#f5f5f5") 
 
        self.entries  = {}
        self.config_file  = "autocut_config.json" 
        self.is_processing  = False 
 
        self.build_ui() 
        self.update_config_list() 
        self.load_last_config() 
 
    def build_ui(self):
        main_frame = ttk.Frame(self.root,  padding="5")
        main_frame.pack(fill="both",  expand=True)
 
        ttk.Label(main_frame, text="🎬 AutoCut 音频剪辑工具", font=("微软雅黑", 16, "bold")).pack(pady=(0, 5))
 
        notebook = ttk.Notebook(main_frame)
        notebook.pack(fill="both",  expand=True)
 
        # 设置面板 
        settings_frame = ttk.Frame(notebook, padding=5)
        notebook.add(settings_frame,  text="基本设置")
 
        # 文件设置 - 重新设计为两列布局 
        file_frame = ttk.LabelFrame(settings_frame, text="文件设置", padding=5)
        file_frame.pack(fill="x",  pady=5)
 
        # 左侧文件输入 
        left_frame = ttk.Frame(file_frame)
        left_frame.pack(side="left",  fill="both", expand=True, padx=(0, 5))
        
        # 右侧文件输出 
        right_frame = ttk.Frame(file_frame)
        right_frame.pack(side="left",  fill="both", expand=True)
 
        def add_file_row(parent, label, key, save=False):
            frame = ttk.Frame(parent)
            frame.pack(fill="x",  pady=3)
            ttk.Label(frame, text=label, width=12).pack(side="left")
            entry = ttk.Entry(frame, width=40)
            entry.pack(side="left",  expand=True, fill="x", padx=(0, 5))
            self.entries[key]  = entry 
            ttk.Button(frame, text="浏览", command=lambda: self.browse_file(entry,  save), width=8).pack(side="right")
 
        # 左侧输入文件 
        add_file_row(left_frame, "输入音频文件:", "input_audio")
        add_file_row(left_frame, "输入字幕文件:", "input_srt")
        add_file_row(left_frame, "过滤文本文件:", "filter_file")
        
        # 右侧输出文件 
        add_file_row(right_frame, "输出音频文件:", "output_mp3", True)
        add_file_row(right_frame, "输出字幕文件:", "output_srt", True)
 
        # 字幕范围 
        range_frame = ttk.LabelFrame(settings_frame, text="字幕范围", padding=10)
        range_frame.pack(fill="x",  pady=5)
 
        range_row = ttk.Frame(range_frame)
        range_row.pack(fill="x") 
        
        ttk.Label(range_row, text="起始字幕索引:").pack(side="left")
        self.entries["start_index"]  = ttk.Entry(range_row, width=8)
        self.entries["start_index"].pack(side="left",  padx=(0, 15))
        self.entries["start_index"].insert(0,  "1")
        
        ttk.Label(range_row, text="结束字幕索引:").pack(side="left")
        self.entries["end_index"]  = ttk.Entry(range_row, width=8)
        self.entries["end_index"].pack(side="left",  padx=(0, 5))
        ttk.Label(range_row, text="(留空表示处理到最后)").pack(side="left")
 
        # 音频设置 
        audio_frame = ttk.LabelFrame(settings_frame, text="音频输出设置", padding=10)
        audio_frame.pack(fill="x",  pady=5)
        
        ttk.Label(audio_frame, text="输出格式:").pack(side="left", padx=(0, 5))
        self.format_var  = tk.StringVar(value="mp3")
        ttk.Combobox(audio_frame, textvariable=self.format_var,  
                    values=['mp3', 'wav', 'm4a'], state="readonly", width=10).pack(side="left", padx=(0, 20))
        
        ttk.Label(audio_frame, text="音质等级:").pack(side="left", padx=(0, 5))
        self.quality_var  = tk.StringVar(value="high")
        ttk.Combobox(audio_frame, textvariable=self.quality_var,  
                    values=['high', 'medium'], state="readonly", width=10).pack(side="left")
 
        # 配置管理 
        config_frame = ttk.LabelFrame(settings_frame, text="配置管理", padding=10)
        config_frame.pack(fill="x",  pady=5)
        
        ttk.Label(config_frame, text="配置名称:").pack(side="left")
        self.config_name_entry  = ttk.Entry(config_frame, width=20)
        self.config_name_entry.insert(0,  "默认配置")
        self.config_name_entry.pack(side="left",  padx=(0, 10))
        
        for btn in [("保存配置", self.save_config),  ("载入配置", self.load_config)]: 
            ttk.Button(config_frame, text=btn[0], command=btn[1]).pack(side="left", padx=5)
        
        ttk.Label(config_frame, text="已保存配置:").pack(side="left", padx=(10, 0))
        self.config_var  = tk.StringVar()
        self.config_combo  = ttk.Combobox(config_frame, textvariable=self.config_var,  state="readonly", width=30)
        self.config_combo.pack(side="left",  padx=(0, 10))
        self.config_combo.bind("<<ComboboxSelected>>",  self.on_config_selected) 
        ttk.Button(config_frame, text="删除", command=self.delete_config).pack(side="left") 
 
        # 按钮 
        button_frame = ttk.Frame(settings_frame)
        button_frame.pack(fill="x",  pady=10)
        ttk.Button(button_frame, text="清空日志", command=self.clear_log).pack(side="right",  padx=5)
        self.process_button  = ttk.Button(button_frame, text="开始处理", command=self.start_processing) 
        self.process_button.pack(side="right",  padx=5)
 
        # 日志面板 
        log_frame = ttk.Frame(notebook, padding=10)
        notebook.add(log_frame,  text="处理日志")
        
        self.log_text  = tk.Text(log_frame, height=20, wrap="word", bg="#f8f8f8", font=("Consolas", 10))
        self.log_text.pack(side="left",  fill="both", expand=True)
        
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview) 
        scrollbar.pack(side="right",  fill="y")
        self.log_text.config(yscrollcommand=scrollbar.set) 
 
        sys.stdout  = TextRedirector(self.log_text,  self.update_progress_status) 
        sys.stderr  = TextRedirector(self.log_text,  self.update_progress_status) 
 
        # 状态栏 
        self.status_frame  = ttk.Frame(self.root,  relief="sunken", padding=5)
        self.status_frame.pack(side="bottom",  fill="x")
        
        self.status_label  = ttk.Label(self.status_frame,  text="状态：等待中")
        self.status_label.pack(side="left") 
        
        self.progress  = ttk.Progressbar(self.status_frame,  mode="determinate", length=200)
        self.progress.pack(side="right") 
 
    def browse_file(self, entry, save=False):
        path = filedialog.asksaveasfilename(defaultextension=".mp3")  if save else filedialog.askopenfilename() 
        if path:
            entry.delete(0,  tk.END)
            entry.insert(0,  path)
 
    def update_progress_status(self, status_text, progress_value=None):
        self.status_label.config(text=f" 状态：{status_text}")
        if progress_value is not None:
            self.progress.config(value=progress_value) 
        self.root.update_idletasks() 
 
    def start_processing(self):
        if self.is_processing: 
            return 
            
        self.is_processing  = True 
        self.progress["value"]  = 0 
        self.process_button.config(state="disabled") 
        
        threading.Thread(target=self.process,  daemon=True).start()
 
    def process(self):
        try:
            self.update_progress_status("🔄  初始化处理环境...", 0)
            
            required = ["input_audio", "input_srt", "output_mp3", "output_srt", "filter_file"]
            if not all(self.entries[key].get()  for key in required):
                raise ValueError("请填写所有路径字段。")
                
            for key, name in zip(required, ["输入音频", "输入字幕", "过滤文本"]):
                if not os.path.exists(self.entries[key].get()): 
                    raise FileNotFoundError(f"{name}文件不存在: {self.entries[key].get()}") 
 
            start_index = int(self.entries["start_index"].get()  or "1")
            end_index = self.get_end_index(self.entries["input_srt"].get()) 
 
            output_path = self.entries["output_mp3"].get() 
            ext = f".{self.format_var.get()}" 
            if not output_path.lower().endswith(ext): 
                output_path = os.path.splitext(output_path)[0]  + ext 
 
            main(
                input_audio_path=self.entries["input_audio"].get(), 
                input_srt_path=self.entries["input_srt"].get(), 
                output_audio_path=output_path,
                output_srt_path=self.entries["output_srt"].get(), 
                filter_file_path=self.entries["filter_file"].get(), 
                start_index=start_index,
                end_index=end_index,
                output_format=self.format_var.get(), 
                quality=self.quality_var.get() 
            )
 
            self.update_progress_status("✅  处理完成！")
            messagebox.showinfo(" 完成", f"音频剪辑和字幕处理完成！\n输出文件：{output_path}")
 
        except Exception as e:
            self.update_progress_status(f"❌  错误：{str(e)}")
            messagebox.showerror(" 错误", str(e))
        finally:
            self.process_button.config(state="normal") 
            self.is_processing  = False 
 
    def get_end_index(self, srt_path):
        try:
            if self.entries["end_index"].get().strip(): 
                return int(self.entries["end_index"].get().strip()) 
            return len(parse_srt(srt_path))
        except Exception as e:
            print(f"获取字幕总数失败: {e}")
            return 999999 
 
    def clear_log(self):
        self.log_text.delete(1.0,  tk.END)
        print("日志已清空")
 
    # 配置管理 
    def get_current_config(self):
        return {
            "name": self.config_name_entry.get(), 
            **{k: self.entries[k].get()  for k in ["input_audio", "input_srt", "filter_file", "output_mp3", "output_srt", "start_index", "end_index"]}
        }
 
    def apply_config(self, config):
        self.config_name_entry.delete(0,  tk.END)
        self.config_name_entry.insert(0,  config.get("name",  "默认配置"))
        
        for k in ["input_audio", "input_srt", "filter_file", "output_mp3", "output_srt", "start_index", "end_index"]:
            self.entries[k].delete(0,  tk.END)
            self.entries[k].insert(0,  config.get(k,  "1" if k == "start_index" else ""))
 
    def read_all_configs(self):
        if not os.path.exists(self.config_file): 
            return {}
        try:
            with open(self.config_file,  'r', encoding='utf-8') as f:
                return json.load(f) 
        except Exception as e:
            messagebox.showerror(" 错误", f"读取配置失败: {e}")
            return {}
 
    def update_config_list(self):
        self.config_combo['values']  = list(self.read_all_configs().keys()) 
 
    def save_config(self):
        config = self.get_current_config() 
        name = config["name"]
        
        if not name:
            messagebox.showerror(" 错误", "请输入配置名称")
            return 
            
        configs = self.read_all_configs() 
        if name in configs and not messagebox.askyesno(" 确认", f"配置 '{name}' 已存在，是否覆盖？"):
            return 
            
        configs[name] = config 
        self.save_config_file(configs,  f"配置 '{name}' 已保存")
 
    def load_config(self):
        if not (selected := self.config_var.get()): 
            messagebox.showinfo(" 提示", "请先选择一个配置")
            return 
            
        if (config := self.read_all_configs().get(selected)): 
            self.apply_config(config) 
            self.save_last_used_config(selected) 
            print(f"已加载配置: {selected}")
        else:
            messagebox.showerror(" 错误", f"配置 '{selected}' 不存在")
 
    def delete_config(self):
        if not (selected := self.config_var.get()): 
            messagebox.showinfo(" 提示", "请先选择要删除的配置")
            return 
            
        if not messagebox.askyesno(" 确认", f"确定要删除配置 '{selected}' 吗？"):
            return 
            
        configs = self.read_all_configs() 
        if selected in configs:
            del configs[selected]
            self.save_config_file(configs,  f"配置 '{selected}' 已删除")
            self.config_var.set("") 
 
    def save_config_file(self, configs, success_msg):
        try:
            with open(self.config_file,  'w', encoding='utf-8') as f:
                json.dump(configs,  f, ensure_ascii=False, indent=2)
            self.update_config_list() 
            messagebox.showinfo(" 成功", success_msg)
        except Exception as e:
            messagebox.showerror(" 错误", f"操作失败: {e}")
 
    def on_config_selected(self, event):
        self.load_config() 
 
    def load_last_config(self):
        if last_name := self.read_all_configs().get("last_used"): 
            self.apply_config(self.read_all_configs().get(last_name,  {}))
            self.config_var.set(last_name) 
            print(f"已自动加载上次使用的配置: {last_name}")
 
    def save_last_used_config(self, config_name):
        configs = self.read_all_configs() 
        configs["last_used"] = config_name 
        try:
            with open(self.config_file,  'w', encoding='utf-8') as f:
                json.dump(configs,  f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存最近配置失败: {e}")
 
if __name__ == "__main__":
    root = tk.Tk()
    AutoCutGUI(root)
    root.mainloop() 