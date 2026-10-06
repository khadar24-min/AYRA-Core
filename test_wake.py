from pathlib import Path
import time
import numpy as np
from openwakeword.model import Model
from src.audio import AudioSession

model_path = str(Path("models/hey_ayra.onnx").resolve())

model = Model(
    wakeword_models=[model_path],
    inference_framework="onnx",
)

session = AudioSession(device=1)
session.__enter__()

print()
print("================================")
print(" SAY: HEY AYRA")
print(" Listening for 15 seconds...")
print("================================")
print()

start = time.time()
peak_score = 0.0
peak_amp = 0

try:
    while time.time() - start < 15:
        chunk = session.read()

        scores = model.predict(chunk)
        score = float(scores["hey_ayra"])

        amp = int(np.max(np.abs(chunk))) if chunk.size else 0

        peak_score = max(peak_score, score)
        peak_amp = max(peak_amp, amp)

        print(f"score={score:.4f}  amp={amp}")

finally:
    session.__exit__(None, None, None)

print()
print("------------------------------")
print(f"PEAK SCORE: {peak_score:.4f}")
print(f"PEAK AMP:   {peak_amp}")
print("------------------------------")