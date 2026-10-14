from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import av  # pyright: ignore[reportMissingTypeStubs]
import numpy as np
from av.error import FFmpegError  # pyright: ignore[reportMissingTypeStubs]


class AudioTranscriptionError(Exception):
    def __init__(self, message: str, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code


class FasterWhisperTranscriber:
    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or os.getenv("WHISPER_MODEL", "tiny.en")
        self._model: Any = None
        self._model_lock = threading.Lock()

    def transcribe(self, audio_path: Path) -> str:
        model = self._get_model()
        try:
            audio = self._decode_audio(audio_path)
            if audio.size == 0:
                raise AudioTranscriptionError(
                    "No speech was detected. Try a clearer or longer recording.",
                    status_code=422,
                )
            segments, _ = model.transcribe(
                audio,
                language="en",
                vad_filter=True,
            )
            transcript = " ".join(segment.text.strip() for segment in segments).strip()
        except (FFmpegError, OSError, RuntimeError, ValueError) as error:
            raise AudioTranscriptionError(
                "The recording could not be decoded. Try recording again in a supported audio format.",
                status_code=422,
            ) from error
        if not transcript:
            raise AudioTranscriptionError(
                "No speech was detected. Try a clearer or longer recording.",
                status_code=422,
            )
        return transcript

    @staticmethod
    def _decode_audio(audio_path: Path) -> np.ndarray:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        frames: list[Any] = []
        with av.open(str(audio_path), mode="r") as container:
            for frame in container.decode(audio=0):
                frames.extend(resampler.resample(frame))
            frames.extend(resampler.resample(None))
        if not frames:
            return np.array([], dtype=np.float32)
        samples = np.concatenate([frame.to_ndarray().reshape(-1) for frame in frames])
        return samples.astype(np.float32) / 32768.0

    def _get_model(self) -> Any:
        with self._model_lock:
            if self._model is None:
                try:
                    from faster_whisper import WhisperModel  # pyright: ignore[reportMissingTypeStubs]
                except ImportError as error:
                    raise AudioTranscriptionError(
                        "Faster-Whisper is not installed. Install backend requirements and retry."
                    ) from error
                try:
                    self._model = WhisperModel(
                        self.model_name,
                        device="cpu",
                        compute_type="int8",
                    )
                except (OSError, RuntimeError, ValueError) as error:
                    raise AudioTranscriptionError(
                        f"Could not load the local Whisper model '{self.model_name}'. "
                        "Check the internet connection for its first download, then retry."
                    ) from error
            return self._model
