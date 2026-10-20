"""Local speaker identification for JARVIS (sherpa-onnx embeddings).

The satellite must greet the person who is actually standing in the room. Voice
embeddings are computed on the host from the same PCM the bridge already
receives; nothing is stored except a normalized vector per enrolled speaker in
an owner-only file. Without a model or without an enrolled profile the caller
falls back to the configured default speaker — identification never blocks a
turn and never guesses a name it cannot prove.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

DEFAULT_THRESHOLD = 0.62
MIN_EMBED_SECONDS = 1.0


def _normalize(vector) -> list[float]:
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values))
    if not norm:
        return []
    return [value / norm for value in values]


def cosine(left, right) -> float:
    a, b = _normalize(left), _normalize(right)
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


class SpeakerProfiles:
    """Owner-only store: one normalized voice vector per person."""

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        self.threshold = DEFAULT_THRESHOLD
        self.vectors: dict[str, list[float]] = {}
        self.model = ""
        self._load()

    def _load(self) -> None:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        if self.path.is_symlink():
            raise PermissionError("speaker profiles must not be a symlink")
        if info.st_mode & 0o077:
            raise PermissionError("speaker profiles must be owner-only (0600)")
        if info.st_uid != os.getuid():
            raise PermissionError("speaker profiles have a foreign owner")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        self.model = str(payload.get("model") or "")
        try:
            threshold = float(payload.get("threshold") or DEFAULT_THRESHOLD)
        except (TypeError, ValueError):
            threshold = DEFAULT_THRESHOLD
        self.threshold = max(0.1, min(threshold, 0.99))
        speakers = payload.get("speakers")
        if isinstance(speakers, dict):
            for key, values in speakers.items():
                if isinstance(values, list) and values:
                    try:
                        self.vectors[str(key).lower()] = _normalize(values)
                    except (TypeError, ValueError):
                        continue

    def save(self, *, model: str | None = None) -> None:
        payload = {
            "version": 1,
            "model": model if model is not None else self.model,
            "threshold": self.threshold,
            "speakers": {key: list(values) for key, values in self.vectors.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(payload).encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(temp, self.path)
        os.chmod(self.path, 0o600)

    def match(self, vector) -> tuple[str | None, float]:
        best_key, best_score = None, 0.0
        for key, stored in self.vectors.items():
            score = cosine(vector, stored)
            if score > best_score:
                best_key, best_score = key, score
        if best_key is not None and best_score >= self.threshold:
            return best_key, best_score
        return None, best_score


class SpeakerIdentifier:
    """Identify a speaker from PCM; degrade to None when unavailable."""

    def __init__(self, *, profiles: SpeakerProfiles, model_path: str = "",
                 embedder=None, sample_rate: int = 16000) -> None:
        self._profiles = profiles
        self._sample_rate = int(sample_rate)
        self._model_path = str(model_path or profiles.model or "")
        self._embedder = embedder
        self._extractor = None
        self._load_error = ""

    @property
    def available(self) -> bool:
        if self._embedder is not None:
            return True
        return bool(self._model_path) and bool(self._profiles.vectors)

    def _ensure_extractor(self):
        if self._extractor is not None or self._embedder is not None:
            return self._extractor
        try:
            import sherpa_onnx  # optional dependency

            config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=self._model_path, num_threads=1,
            )
            if not config.validate():
                raise RuntimeError("speaker model invalid")
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        except Exception as exc:
            self._load_error = type(exc).__name__
            self._extractor = None
        return self._extractor

    def embedding(self, pcm: bytes) -> list[float]:
        """L2-normalized embedding, or [] when the model is unusable."""
        data = bytes(pcm or b"")
        if len(data) < int(self._sample_rate * 2 * MIN_EMBED_SECONDS):
            return []
        if self._embedder is not None:
            try:
                return _normalize(self._embedder(data))
            except Exception:
                return []
        extractor = self._ensure_extractor()
        if extractor is None:
            return []
        try:
            import array
            import sys

            samples = array.array("h")
            samples.frombytes(data[: len(data) - (len(data) % 2)])
            if sys.byteorder != "little":
                samples.byteswap()
            stream = extractor.create_stream()
            stream.accept_waveform(self._sample_rate, samples)
            stream.input_finished()
            if not extractor.is_ready(stream):
                return []
            return _normalize(extractor.compute(stream))
        except Exception:
            return []

    def identify(self, pcm: bytes) -> str | None:
        if not self._profiles.vectors:
            return None
        vector = self.embedding(pcm)
        if not vector:
            return None
        key, _score = self._profiles.match(vector)
        return key

    def enroll(self, key: str, pcm: bytes) -> str:
        """Add or replace one speaker's vector; returns the stored key."""
        name = str(key or "").strip().lower()
        if not name:
            raise ValueError("speaker key missing")
        vector = self.embedding(pcm)
        if not vector:
            raise ValueError("recording too short or model unavailable")
        self._profiles.vectors[name] = vector
        self._profiles.model = self._profiles.model or self._model_path
        self._profiles.save()
        return name
