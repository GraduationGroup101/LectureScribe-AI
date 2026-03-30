from pathlib import Path
import requests
import re

# =========================
# SETTINGS
# =========================
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "llama3.1:8b-instruct-q4_K_M"

#Input Whisper transcript file
INPUT_TXT = Path("OpenVuas_English.txt")

# Output cleaned transcript file
output = INPUT_TXT.stem + "_cleanedv5.txt"
OUTPUT_TXT = Path(output)

# split text into chunks of roughly this many characters
CHUNK_CHARS = 3500

# =========================
# PROMPT
# =========================
SYSTEM_RULES = """You are a transcript cleaner for university lectures.
Your job: clean Arabic transcript produced by ASR (Whisper).
Rules:
- convert the text into Engloish words 
- dont write any arabic word alone in the text, just convert it to its english term
- Keep English technical terms in correct English spelling (do NOT translate them).
- Fix obvious Arabic spelling and punctuation.
- Remove repeated words, filler sounds, and stuttering (e.g., "يعني", "اا", "مم", "تمام؟" when excessive).
- Do NOT invent new information. Do NOT add explanations. Just clean.
- Preserve the original meaning and order.
- every word should be in its correct form thats if the word in arabic but its english term keep it in english
- when u convert arabic words to english terms write them in their correct english form and put them between quotes like this "term" next to the arabic word  not on the end of the line
- when you write an english sentence , write the arabic sentence that after it in new line
"""

def make_user_prompt(text: str) -> str:
    return f"""Clean this transcript:
{text}
"""

# =========================
# HELPERS
# =========================
def split_text(text: str, max_chars: int):
    """Split text into chunks roughly by paragraphs/sentences."""
    # split by double newlines (paragraphs)
    parts = re.split(r"\n{2,}", text.strip())
    chunks = []
    buf = ""

    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(buf) + len(p) + 2 <= max_chars:
            buf = (buf + "\n\n" + p).strip()
        else:
            if buf:
                chunks.append(buf)
            # if single part is too long, split by sentences
            if len(p) > max_chars:
                for i in range(0, len(p), max_chars):
                    chunks.append(p[i:i+max_chars])
                buf = ""
            else:
                buf = p

    if buf:
        chunks.append(buf)

    return chunks


def ollama_generate(prompt: str) -> str:
    """Call Ollama local API."""
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "system": SYSTEM_RULES,
        "stream": False,
        "options": {
            "temperature": 0.2,
            "top_p": 0.9,
            "num_predict": 1200
        }
    }
    r = requests.post(OLLAMA_URL, json=payload, timeout=600)
    r.raise_for_status()
    data = r.json()
    return data.get("response", "").strip()


def main():
    if not INPUT_TXT.exists():
        print(f"Input file not found: {INPUT_TXT.resolve()}")
        print("ضع ملف ناتج whisper باسم whisper_output.txt بجانب هذا السكربت.")
        return

    raw = INPUT_TXT.read_text(encoding="utf-8", errors="ignore").strip()
    if not raw:
        print("Input file is empty.")
        return

    chunks = split_text(raw, CHUNK_CHARS)
    print(f"Loaded text. Chunks: {len(chunks)}")

    cleaned_chunks = []
    for i, ch in enumerate(chunks, start=1):
        print(f" Cleaning chunk {i}/{len(chunks)} ...")
        out = ollama_generate(make_user_prompt(ch))
        cleaned_chunks.append(out)

    cleaned = "\n\n".join(cleaned_chunks).strip()
    OUTPUT_TXT.write_text(cleaned, encoding="utf-8")

    print("\n DONE")
    print(f"Saved cleaned transcript to: {OUTPUT_TXT.resolve()}")


if __name__ == "__main__":
    main()
