from pathlib import Path
import re

SRT_PATH = Path("CH1 B Part2 [rLc0HeAWFy4].ar.srt")      
TXT_PATH = Path("ch1_b_sub_txt.txt")

def srt_to_text(srt: str) -> str:
    lines = srt.splitlines()
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # ignore sequence numbers
        if re.fullmatch(r"\d+", line):
            continue
        # ignore timestamps
        if "-->" in line:
            continue
        out.append(line)

    # join and clean
    text = " ".join(out)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def main():
    srt = SRT_PATH.read_text(encoding="utf-8", errors="ignore")
    text = srt_to_text(srt)
    TXT_PATH.write_text(text, encoding="utf-8")
    print("Saved:", TXT_PATH.resolve())
    print("\nSample:\n", text[:500])

if __name__ == "__main__":
    main()
