#!/usr/bin/env python3.11
"""XTTS v2 worker — llamado como subprocess por el bot (requiere Python 3.11).

Uso: python3.11 xtts_worker.py <out.wav> "texto a sintetizar"
     python3.11 xtts_worker.py <out.wav> --stdin   (lee texto de stdin)

Voz fija: Claribel Dervla (XTTS v2 multilingual)
Idioma:   es (español)
"""
import os, sys, warnings
warnings.filterwarnings("ignore")
os.environ["COQUI_TOS_AGREED"] = "1"

def main():
    if len(sys.argv) < 2:
        print("Uso: xtts_worker.py <out.wav> <texto>", file=sys.stderr)
        sys.exit(1)

    out_path = sys.argv[1]

    if len(sys.argv) == 3 and sys.argv[2] == "--stdin":
        texto = sys.stdin.read().strip()
    else:
        texto = " ".join(sys.argv[2:]).strip()

    if not texto:
        print("Error: texto vacío", file=sys.stderr)
        sys.exit(1)

    from TTS.api import TTS  # noqa: PLC0415
    tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
    tts.tts_to_file(
        text=texto,
        language="es",
        speaker="Claribel Dervla",
        file_path=out_path,
    )

if __name__ == "__main__":
    main()
