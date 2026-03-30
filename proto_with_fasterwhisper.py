from faster_whisper import WhisperModel
from pathlib import Path
import os
import url_to_mp3


os.environ["PATH"] += os.pathsep + r"C:\Users\Mahmoud\Downloads\Compressed\ffmpeg-8.1-essentials_build\ffmpeg-8.1-essentials_build\bin"

# ========= إعدادات =========
# AUDIO_PATH = Path(r"Lab5_ OpenVAS tool_20261523541.mp3")
AUDIO_PATH_as_url = Path(url_to_mp3.download_youtube_mp3(input("Enter YouTube URL: ")))
MODEL_SIZE = "large-v3"                        
DEVICE = "cuda"                              
COMPUTE_TYPE = "int8_float16"                 

# prompt من ملف الترجمة (اختياري)
SUB_TXT_PATH = Path("ch1_b_sub_txt.txt")
PROMPT = ""
if SUB_TXT_PATH.exists():
    PROMPT = SUB_TXT_PATH.read_text(encoding="utf-8", errors="ignore")[:4000]

# إعداد ال prompt الرئيسي   
another_promot = "This is a lecture about openVas program of web securty subject , the lecture contain some english words so please identfiy them and write them in english"

Main_Prompt = (
    "This is a university lecture in Arabic with English technical terms.\n"
    "The topic is indexed images and image processing in Octave/MATLAB.\n"
    "Keep English technical terms correctly when they appear.\n"
    "Here are subtitle hints:\n"
    + PROMPT
)

def main():
    if not AUDIO_PATH_as_url.exists():
        print(f"File not found: {AUDIO_PATH_as_url.resolve()}")
        return

    print(f"Loading faster-whisper model: {MODEL_SIZE} on {DEVICE} ({COMPUTE_TYPE}) ...")
    MODEL_PATH = r"C:\Users\Mahmoud\models\faster-whisper-large-v3"

    print(f"Loading faster-whisper model from: {MODEL_PATH}")
    model = WhisperModel(
        MODEL_PATH,
        device=DEVICE,
        compute_type=COMPUTE_TYPE,
    )

    print("Transcribing ...")

#    some settings for transcription 
    kwargs = dict(
        language='ar',        
        vad_filter=True,
        beam_size=5,
        # word_timestamps=True,  # لو بدك توقيت لكل كلمة (يبطّئ شوي)
    )

#   If you want to use the custom prompt, uncomment the next line
  
    kwargs["initial_prompt"] = another_promot

    segments, info = model.transcribe(AUDIO_PATH_as_url.as_posix(), **kwargs)

    # اجمع النص
    parts = []
    for seg in segments:
        parts.append(seg.text.strip())

    text = " ".join(parts)
    # text = clean_transcript(text)

    print("\n===== TRANSCRIPT =====\n")
    print(text)

    out_path = AUDIO_PATH_as_url.stem + "sapcial_promot" + "_transcript.txt"

    out = Path(out_path)
    out.write_text(text, encoding="utf-8")
    print(f"\nSaved to: {out.resolve()}")

    print("\n===== INFO =====")
    print("Detected language:", info.language)
    print("Language probability:", getattr(info, "language_probability", "N/A"))

if __name__ == "__main__":
    main()
