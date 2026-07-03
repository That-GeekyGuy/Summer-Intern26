"""Load MOMENT head.pt and score windows without requiring the MOMENT base model."""
import json
import logging
from pathlib import Path
from typing import Optional

import numpy as np
import torch
import torch.nn as nn

log = logging.getLogger(__name__)

_threshold: float = 0.5
_embedding_dim: int = 7168


def load_moment_head(
    head_path: Path,
    threshold_path: Path,
    device: str,
) -> tuple[Optional[nn.Module], bool]:
    global _threshold, _embedding_dim
    try:
        meta = json.loads(threshold_path.read_text())
        _threshold = meta["threshold"]
        _embedding_dim = meta["embedding_dim"]

        head = nn.Linear(_embedding_dim, 1)
        state = torch.load(head_path, map_location=device, weights_only=True)
        head.load_state_dict(state)
        head.eval()
        head.to(device)
        log.info("MOMENT head loaded (proxy mode, embedding_dim=%d)", _embedding_dim)
        return head, True
    except Exception as exc:
        log.warning("MOMENT head not loaded: %s", exc)
        return None, False


def _make_proxy_embedding(
    channels: list[list[float]],
    embedding_dim: int,
) -> np.ndarray:
    # ponytail: proxy embedding via channel mean+std tiled to embedding_dim;
    # replace with real MOMENT base model inference when momentfm is installed
    stats: list[float] = []
    for series in channels:
        arr = np.array(series, dtype=np.float64)
        stats += [float(np.mean(arr)), float(np.std(arr))]

    base = np.array(stats if stats else [0.0], dtype=np.float32)
    reps = (embedding_dim + len(base) - 1) // len(base)
    emb = np.tile(base, reps)[:embedding_dim]
    norm = np.linalg.norm(emb)
    if norm > 0:
        emb = emb / norm
    return emb


def score_moment(
    head: nn.Module,
    channels: list[list[float]],
    channel_names: list[str],
    device: str,
) -> tuple[float, bool]:
    emb = _make_proxy_embedding(channels, _embedding_dim)
    with torch.no_grad():
        x = torch.tensor(emb, dtype=torch.float32, device=device).unsqueeze(0)
        logit = head(x).item()
        score = float(torch.sigmoid(torch.tensor(logit)).item())
    return score, score >= _threshold
