from faster_whisper import WhisperModel
from pathlib import Path
import os
import url_to_mp3
from clean_with_Llama import clean_transcript_file


os.environ["PATH"] += os.pathsep + r"C:\Users\Mahmoud\Downloads\Compressed\ffmpeg-8.1-essentials_build\ffmpeg-8.1-essentials_build\bin"


# Model and device settings
AUDIO_PATH_as_url = Path(url_to_mp3.download_youtube_mp3(input("Enter YouTube URL: ")))
# AUDIO_PATH_as_url = Path("IUG Renewable energy Lab 3 ： solar panel connection.mp3")
MODEL_SIZE = "large-v3"                        
DEVICE = "cuda"                              
COMPUTE_TYPE = "int8_float16"                 

# subtitle Prompt
SUB_TXT_PATH = Path("ch1_b_sub_txt.txt")
PROMPT = ""
if SUB_TXT_PATH.exists():
    PROMPT = SUB_TXT_PATH.read_text(encoding="utf-8", errors="ignore")[:4000]


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
    )

#   If you want to use the custom prompt, uncomment the next line
  
    kwargs["initial_prompt"] = Main_Prompt

    segments, info = model.transcribe(AUDIO_PATH_as_url.as_posix(), **kwargs)

    # combine all segments into one text
    parts = []
    for seg in segments:
        parts.append(seg.text.strip())

    text = " ".join(parts)
    # text = clean_transcript(text)

    print("\n===== TRANSCRIPT =====\n")
    print(text)
#   save the transcript to a text file in folder of name "OutputForOllama" + AUDIO_PATH_as_url.stem + "_transcript.txt"
    output_dir = Path("OutputForWhisper")
    output_dir.mkdir(exist_ok=True)

    out = output_dir / f"{AUDIO_PATH_as_url.stem}_transcript.txt"
    out.write_text(text, encoding="utf-8")

    print(f"\nSaved to: {out.resolve()}")

    # pipeline to clean the transcript using Ollama
    clean_transcript_file(out)

    print("\n===== INFO =====")
    print("Detected language:", info.language)
    print("Language probability:", getattr(info, "language_probability", "N/A"))

if __name__ == "__main__":
    main()