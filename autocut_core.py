# autocut_core.py v2.4.4
import os, subprocess, wave, srt, numpy as np, shutil, tempfile, atexit
import ctypes, time, psutil, platform
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor

BATCH_SIZE = 500
MAX_WORKERS = min(4, os.cpu_count() or 2)
TEMP_DIR = tempfile.mkdtemp(prefix="autocut_")
FFMPEG_TIMEOUT = 600

atexit.register(lambda: [clean_temp_files(), kill_ffmpeg_processes()])

def get_system_info():
    mem = psutil.virtual_memory()
    return {
        'system': platform.system(),
        'cpu_cores': os.cpu_count(),
        'memory': f"{mem.available/1024**3:.1f}GB/{mem.total/1024**3:.1f}GB",
        'ffmpeg': subprocess.getoutput("ffmpeg -version | head -n1")
    }

def get_short_path(path):
    if os.name != 'nt' or not os.path.exists(path): return path
    try:
        buf = ctypes.create_unicode_buffer(512)
        if ctypes.windll.kernel32.GetShortPathNameW(path, buf, 512): return buf.value
    except: pass
    return path

def clean_temp_files():
    for _ in range(3):
        try:
            if os.path.exists(TEMP_DIR):
                shutil.rmtree(TEMP_DIR, ignore_errors=True)
                time.sleep(1)
            os.makedirs(TEMP_DIR, exist_ok=True)
            break
        except Exception as e:
            print(f"⚠️ 清理临时文件失败 (重试 {_+1}/3): {str(e)}")

def kill_ffmpeg_processes():
    try:
        os.system('taskkill /f /im ffmpeg.exe >nul 2>&1' if os.name == 'nt' else 'pkill -9 ffmpeg >/dev/null 2>&1')
        time.sleep(1)
    except: pass

def check_aac_encoder():
    encoders = {
        'libfdk_aac': ['-c:a', 'libfdk_aac', '-vbr', '4', '-afterburner', '1'],
        'aac': ['-c:a', 'aac', '-b:a', '192k', '-aac_coder', 'twoloop'],
        'default': ['-c:a', 'aac', '-b:a', '192k']
    }
    
    for cmd in [['ffmpeg', '-hide_banner', '-encoders'], ['ffmpeg', '-codecs']]:
        try:
            output = subprocess.run(cmd, capture_output=True, text=True).stdout
            for enc in encoders:
                if enc != 'default' and f'{enc}' in output:
                    print(f"✅ 检测到可用编码器: {enc}")
                    return encoders[enc]
        except: continue
    
    print("⚠️ 使用默认AAC编码器")
    return encoders['default']

def safe_ffmpeg_run(cmd, timeout=FFMPEG_TIMEOUT):
    def pre_exec():
        if os.name != 'nt': os.setpgrp()

    try:
        return subprocess.run(
            [get_short_path(cmd[0]), *cmd[1:]],
            timeout=timeout, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            preexec_fn=pre_exec if os.name != 'nt' else None
        )
    except subprocess.TimeoutExpired:
        kill_ffmpeg_processes()
        raise RuntimeError(f"FFmpeg处理超时 (超过{timeout}秒)")
    except subprocess.CalledProcessError as e:
        error_msg = (e.stderr or b'').decode().strip() or (e.stdout or b'').decode().strip()
        raise RuntimeError(f"FFmpeg错误: {error_msg[:500]}")

def parse_srt(file_path):
    with open(file_path, 'r', encoding='utf-8-sig') as f:
        return [(s.index, s.start.total_seconds(), s.end.total_seconds(), s.content)
                for s in srt.parse(f)]

def read_filter_file(path):
    if not os.path.exists(path): return set()
    with open(path, 'r', encoding='utf-8') as f:
        return set(line.strip() for line in f if line.strip())

def extract_clip_mp3(input_mp3, start_time, duration, output_clip_mp3):
    cmd = ["ffmpeg", "-y", "-ss", str(round(max(0, start_time), 6)),
           "-t", str(round(duration, 6)), "-i", input_mp3,
           "-acodec", "copy", "-max_muxing_queue_size", "9999", output_clip_mp3]
    safe_ffmpeg_run(cmd)

def convert_mp3_to_wav(input_mp3, output_wav_path):
    cmd = ["ffmpeg", "-y", "-i", input_mp3, "-acodec", "pcm_s16le",
           "-ar", "44100", "-ac", "2", "-threads", str(MAX_WORKERS), output_wav_path]
    safe_ffmpeg_run(cmd)

def cut_audio_segments_with_numpy_parallel(wav_path, subtitles, output_path, clip_start_time):
    mem = psutil.virtual_memory()
    if mem.available < 1 * 1024**3:
        raise MemoryError("系统可用内存不足，请关闭其他程序")

    with wave.open(wav_path, 'rb') as wf:
        params = wf.getparams()
        dtype = np.int16 if params.sampwidth == 2 else np.int8
        total_frames = wf.getnframes()
    
    audio_np = np.memmap(wav_path, dtype=dtype, mode='r',
                        offset=44, shape=(total_frames * params.nchannels,))
    
    framerate = params.framerate
    frame_size = params.nchannels

    def extract_segment(start, end):
        start_rel = max(0, round(start - clip_start_time, 6))
        end_rel = round(end - clip_start_time, 6)
        start_frame = int(start_rel * framerate) * frame_size
        end_frame = int(end_rel * framerate) * frame_size
        return audio_np[start_frame:end_frame].copy()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(extract_segment, start, end) for _, start, end, _ in subtitles]
        segments = [f.result() for f in tqdm(futures, desc="⏱️ 切割中", unit="segment")]

    combined = np.concatenate(segments)

    with wave.open(output_path, 'wb') as wf:
        wf.setnchannels(params.nchannels)
        wf.setsampwidth(params.sampwidth)
        wf.setframerate(framerate)
        wf.writeframes(combined.tobytes())

def compress_audio_to_mp3(input_path, output_path, quality="high"):
    qscale = "2" if quality == "high" else "4"
    cmd = ["ffmpeg", "-y", "-i", input_path,
           "-c:a", "libmp3lame", "-q:a", qscale,
           "-threads", str(MAX_WORKERS), "-write_xing", "0", output_path]
    safe_ffmpeg_run(cmd)

def compress_audio_to_aac(input_path, output_path):
    aac_params = check_aac_encoder()
    
    cmd = ["ffmpeg", "-y", "-i", input_path, *aac_params,
           "-movflags", "+faststart", "-threads", str(min(2, MAX_WORKERS)),
           "-max_muxing_queue_size", "9999", output_path]
    
    try:
        safe_ffmpeg_run(cmd)
    except RuntimeError as e:
        print(f"⚠️ 直接压缩失败: {str(e)}, 尝试回退方案...")
        temp_wav = os.path.join(TEMP_DIR, "fallback.wav")
        convert_mp3_to_wav(input_path, temp_wav)
        safe_ffmpeg_run(["ffmpeg", "-y", "-i", temp_wav, *aac_params, output_path])

def parallel_compress_segments(wav_files, output_path, output_format, quality):
    if output_format == "m4a":
        concat_file = os.path.join(TEMP_DIR, "concat.txt")
        with open(concat_file, 'w') as f:
            f.write("\n".join(f"file '{w}'" for w in wav_files))
        
        merged_wav = os.path.join(TEMP_DIR, "merged.wav")
        safe_ffmpeg_run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                         "-i", concat_file, "-c", "copy", merged_wav])
        
        compress_audio_to_aac(merged_wav, output_path)
        return

    temp_dir = os.path.join(TEMP_DIR, "compress_mp3")
    os.makedirs(temp_dir, exist_ok=True)

    def process_file(i, input_wav):
        output_file = os.path.join(temp_dir, f"{i}.mp3")
        compress_audio_to_mp3(input_wav, output_file, quality)
        return output_file

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_file, i, w) for i, w in enumerate(wav_files)]
        mp3_files = [f.result() for f in tqdm(futures, desc="🔧 压缩进度")]

    concat_file = os.path.join(TEMP_DIR, "mp3_list.txt")
    with open(concat_file, 'w') as f:
        f.write("\n".join(f"file '{mp3}'" for mp3 in mp3_files))
    
    safe_ffmpeg_run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                     "-i", concat_file, "-c", "copy", output_path])

def generate_new_srt(subtitles, output_path, filter_texts, start_index, end_index):
    current_time = 0.0
    new_subs = []

    for _, start, end, content in subtitles[start_index-1:end_index]:
        if content.strip() in filter_texts:
            continue
        duration = end - start
        new_subs.append(srt.Subtitle(
            index=len(new_subs)+1,
            start=srt.timedelta(seconds=current_time),
            end=srt.timedelta(seconds=current_time + duration),
            content=content
        ))
        current_time += duration

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(srt.compose(new_subs))

def extract_audio_from_mp4(input_mp4, output_mp3):
    cmd = ["ffmpeg", "-y", "-i", input_mp4, "-vn", "-acodec", "libmp3lame", output_mp3]
    safe_ffmpeg_run(cmd)

def extract_clip_mp4(input_mp4, start_time, duration, output_clip_mp4):
    cmd = ["ffmpeg", "-y", "-ss", str(round(max(0, start_time), 6)),
           "-t", str(round(duration, 6)), "-i", input_mp4,
           "-c:v", "copy", "-c:a", "copy", "-max_muxing_queue_size", "9999", output_clip_mp4]
    safe_ffmpeg_run(cmd)

def generate_mp4(input_audio, input_video, output_mp4):
    cmd = ["ffmpeg", "-y", "-i", input_video, "-i", input_audio, "-c:v", "copy", "-c:a", "aac", output_mp4]
    safe_ffmpeg_run(cmd)

def main(input_audio_path, input_srt_path, output_audio_path, output_srt_path,
         filter_file_path, start_index, end_index, output_format="mp3", quality="high"):
    print("🚀 AutoCut Core v2.4.4 启动")
    print("🖥️ 系统信息:", get_system_info())

    try:
        kill_ffmpeg_processes()
        clean_temp_files()
        os.makedirs(TEMP_DIR, exist_ok=True)

        input_video_path = None
        if input_audio_path.lower().endswith('.mp4'):
            input_video_path = input_audio_path
            input_audio_path = os.path.join(TEMP_DIR, "extracted_audio.mp3")
            extract_audio_from_mp4(input_video_path, input_audio_path)

        if not all(os.path.exists(f) for f in [input_audio_path, input_srt_path]):
            missing = [f for f in [input_audio_path, input_srt_path] if not os.path.exists(f)]
            raise FileNotFoundError(f"文件不存在: {missing}")

        input_audio_path = get_short_path(input_audio_path)
        input_srt_path = get_short_path(input_srt_path)
        output_audio_path = get_short_path(output_audio_path)
        output_srt_path = get_short_path(output_srt_path)

        subtitles = parse_srt(input_srt_path)
        filter_texts = read_filter_file(filter_file_path)

        if not (1 <= start_index <= end_index <= len(subtitles)):
            raise ValueError(f"无效范围 (总字幕: {len(subtitles)}, 请求: {start_index}-{end_index})")

        clip_start_time = subtitles[start_index - 1][1]
        clip_end_time = subtitles[end_index - 1][2]
        clip_duration = clip_end_time - clip_start_time
        print(f"⏱️ 处理区间: {clip_start_time:.2f}s → {clip_end_time:.2f}s (时长: {clip_duration:.2f}s)")

        temp_files = {
            'clip_mp3': os.path.join(TEMP_DIR, "clip.mp3"),
            'clip_wav': os.path.join(TEMP_DIR, "clip.wav"),
            'final_wav': os.path.join(TEMP_DIR, "final.wav")
        }

        print("\n🔪 步骤1/4: 提取原始音频...")
        extract_clip_mp3(input_audio_path, clip_start_time, clip_duration, temp_files['clip_mp3'])
        convert_mp3_to_wav(temp_files['clip_mp3'], temp_files['clip_wav'])

        adjusted_subtitles = [
            (i, start, end, content)
            for i, start, end, content in subtitles[start_index - 1:end_index]
            if content.strip() not in filter_texts
        ]
        print(f"📋 有效字幕: {len(adjusted_subtitles)} (过滤 {len(subtitles[start_index-1:end_index]) - len(adjusted_subtitles)} 条)")

        print("\n✂️ 步骤2/4: 切割音频...")
        batch_wavs = []
        for i in range(0, len(adjusted_subtitles), BATCH_SIZE):
            batch = adjusted_subtitles[i:i + BATCH_SIZE]
            batch_wav = os.path.join(TEMP_DIR, f"batch_{i//BATCH_SIZE}.wav")
            cut_audio_segments_with_numpy_parallel(temp_files['clip_wav'], batch, batch_wav, clip_start_time)
            batch_wavs.append(batch_wav)

        print("\n🧩 步骤3/4: 合并输出...")
        if output_format == "mp4":
            temp_audio = output_audio_path
            output_audio_path = os.path.join(TEMP_DIR, "temp_audio.mp3")
            parallel_compress_segments(batch_wavs, output_audio_path, "mp3", quality)

            # 裁剪视频
            clipped_video_path = os.path.join(TEMP_DIR, "clipped_video.mp4")
            extract_clip_mp4(input_video_path, clip_start_time, clip_duration, clipped_video_path)

            # 合并裁剪后的视频和音频
            generate_mp4(output_audio_path, clipped_video_path, temp_audio)
            output_audio_path = temp_audio
        else:
            if output_format == "wav":
                with wave.open(temp_files['final_wav'], 'wb') as out_wav:
                    with wave.open(batch_wavs[0], 'rb') as in_wav:
                        out_wav.setparams(in_wav.getparams())
                    for bw in batch_wavs:
                        with wave.open(bw, 'rb') as in_wav:
                            out_wav.writeframes(in_wav.readframes(in_wav.getnframes()))
                shutil.move(temp_files['final_wav'], output_audio_path)
            else:
                parallel_compress_segments(batch_wavs, output_audio_path, output_format, quality)

        print("\n📝 步骤4/4: 生成字幕...")
        generate_new_srt(subtitles, output_srt_path, filter_texts, start_index, end_index)

        orig_size = os.path.getsize(temp_files['clip_mp3']) / 1024**2
        final_size = os.path.getsize(output_audio_path) / 1024**2
        print(f"\n✅ 处理完成!\n"
              f"  输出文件: {output_audio_path} ({final_size:.2f}MB)\n"
              f"  字幕文件: {output_srt_path}\n"
              f"  压缩比: {final_size/orig_size*100:.1f}% (原始: {orig_size:.2f}MB)")

    except MemoryError as e:
        print(f"\n❌ 内存不足: {str(e)}")
        print("💡 建议: 1. 减少处理区间 2. 关闭其他程序 3. 使用更小的BATCH_SIZE")
        raise
    except RuntimeError as e:
        print(f"\n❌ FFmpeg处理失败: {str(e)}")
        print("💡 建议: 1. 检查输入文件 2. 更新FFmpeg 3. 尝试其他输出格式")
        raise
    except Exception as e:
        print(f"\n❌ 未知错误: {str(e)}")
        raise
    finally:
        clean_temp_files()
        print("🧹 临时文件已清理")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description='AutoCut 音频处理工具 (精简版)',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('--input', required=True, help='输入音频路径(MP3/WAV)')
    parser.add_argument('--srt', required=True, help='字幕文件路径(SRT格式)')
    parser.add_argument('--output', required=True, help='输出音频路径')
    parser.add_argument('--output-srt', required=True, help='输出字幕路径')
    parser.add_argument('--filter', default="", help='过滤文本文件路径')
    parser.add_argument('--start', type=int, required=True, help='起始字幕序号(从1开始)')
    parser.add_argument('--end', type=int, required=True, help='结束字幕序号')
    parser.add_argument('--format', choices=['mp3', 'm4a', 'wav'], 
                       default='mp3', help='输出音频格式')
    parser.add_argument('--quality', choices=['high', 'medium', 'low'], 
                       default='high', help='输出音质(仅MP3有效)')
    parser.add_argument('--batch-size', type=int, default=500,
                       help='处理批次大小(内存不足时减小此值)')
    
    args = parser.parse_args()
    BATCH_SIZE = max(100, min(args.batch_size, 1000))
    
    try:
        main(
            input_audio_path=args.input,
            input_srt_path=args.srt,
            output_audio_path=args.output,
            output_srt_path=args.output_srt,
            filter_file_path=args.filter,
            start_index=args.start,
            end_index=args.end,
            output_format=args.format,
            quality=args.quality
        )
    except KeyboardInterrupt:
        print("\n🛑 用户中断操作")
        kill_ffmpeg_processes()
        clean_temp_files()
        exit(1)