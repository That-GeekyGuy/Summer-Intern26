"""Load MOMENT head.pt and score windows via real MOMENT base-model inference,
falling back to a proxy embedding when momentfm/pretrained weights aren't
available (no network access, package missing, insufficient memory)."""
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
_seq_len: int = 512

# The exact channel order moment_head.pt was trained on — read from the same
# models/moment_channel_names.json file tools/train/train_moment.py writes.
# This is NOT the same set/order as pipeline/window.py's live ML_CHANNELS;
# channels the live stream doesn't have are zero-filled below.
_moment_channels: list[str] = []

_moment_base = None
_moment_base_available = False


class ReconstructionAnomalyHead(nn.Module):
    """Mirrors tools/train/train_moment.py's ReconstructionAnomalyHead exactly
    (same layer names/shapes are required for state_dict to load): an
    autoencoder trained only on normal windows — score is the mean squared
    reconstruction error, compared against a precomputed threshold
    (normal_mean + n_sigma*normal_std, baked into moment_threshold.json)."""

    def __init__(self, input_dim: int):
        super().__init__()
        hidden = min(input_dim, 256)
        self.encoder = nn.Sequential(
            nn.Linear(input_dim, hidden),
            nn.GELU(),
            nn.Linear(hidden, hidden // 2),
        )
        self.decoder = nn.Sequential(
            nn.Linear(hidden // 2, hidden),
            nn.GELU(),
            nn.Linear(hidden, input_dim),
        )

    def forward(self, x):
        return self.decoder(self.encoder(x))

    def reconstruction_error(self, x):
        recon = self.forward(x)
        return ((x - recon) ** 2).mean(dim=-1)


def _load_moment_base(models_dir: Path, device: str) -> None:
    """Best-effort load of the real MOMENT-1-large backbone. Failure here is
    expected/acceptable (no internet, OOM, package missing) — score_moment
    falls back to the proxy embedding whenever this hasn't succeeded."""
    global _moment_base, _moment_base_available, _moment_channels
    try:
        ch_path = models_dir / "moment_channel_names.json"
        _moment_channels = json.loads(ch_path.read_text())

        from momentfm import MOMENTPipeline

        model = MOMENTPipeline.from_pretrained(
            "AutonLab/MOMENT-1-large",
            model_kwargs={"task_name": "reconstruction"},
        )
        model.init()
        model.eval()
        model.to(device)
        _moment_base = model
        _moment_base_available = True
        log.info(
            "MOMENT base model loaded (real inference, device=%s, channels=%d)",
            device, len(_moment_channels),
        )
    except Exception as exc:
        log.warning("MOMENT base model not loaded — using proxy embedding: %s", exc)
        _moment_base_available = False


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

        head = ReconstructionAnomalyHead(input_dim=_embedding_dim)
        state = torch.load(head_path, map_location=device, weights_only=True)
        head.load_state_dict(state)
        head.eval()
        head.to(device)
        _load_moment_base(head_path.parent, device)
        log.info(
            "MOMENT head loaded (%s mode, embedding_dim=%d)",
            "real" if _moment_base_available else "proxy", _embedding_dim,
        )
        return head, True
    except Exception as exc:
        log.warning("MOMENT head not loaded: %s", exc)
        return None, False


def _make_proxy_embedding(
    channels: list[list[float]],
    embedding_dim: int,
) -> np.ndarray:
    # ponytail: proxy embedding via channel mean+std tiled to embedding_dim;
    # used whenever the real MOMENT base model isn't available (see
    # _load_moment_base) — e.g. momentfm not installed, no network access to
    # pull pretrained weights, or the pod lacks memory to load them.
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


def _make_real_embedding(
    channels: list[list[float]],
    channel_names: list[str],
    device: str,
) -> Optional[np.ndarray]:
    """Mirrors tools/train/train_moment.py's get_moment_embeddings exactly
    (task_name="reconstruction", input_mask, reconstruction output flattened
    to n_channels*seq_len) so live scoring matches what moment_head.pt was
    trained on. Builds the (n_channels, seq_len) window in
    models/moment_channel_names.json's order — any channel the live stream
    doesn't provide (this pipeline's ML_CHANNELS is a different, overlapping
    set) is zero-filled rather than dropped, to keep the tensor shape fixed."""
    if not _moment_channels:
        return None
    by_name = dict(zip(channel_names, channels))
    rows = []
    for ch in _moment_channels:
        series = by_name.get(ch)
        if series is None:
            rows.append(np.zeros(_seq_len, dtype=np.float32))
            continue
        arr = np.asarray(series, dtype=np.float32)
        if arr.size == 0:
            arr = np.zeros(_seq_len, dtype=np.float32)
        elif arr.size < _seq_len:
            arr = np.pad(arr, (_seq_len - arr.size, 0), mode="edge")
        else:
            arr = arr[-_seq_len:]
        rows.append(arr)

    window = np.stack(rows)  # (n_channels, seq_len)
    batch = torch.tensor(window, dtype=torch.float32, device=device).unsqueeze(0)  # (1, n_channels, seq_len)
    with torch.no_grad():
        # MOMENTPipeline.forward is keyword-only (x_enc, input_mask, mask, **kwargs)
        out = _moment_base(
            x_enc=batch,
            input_mask=torch.ones(batch.shape[0], _seq_len, dtype=torch.bool, device=device),
        )
        emb = out.reconstruction.reshape(batch.shape[0], -1)  # (1, n_channels*seq_len)
    return emb.squeeze(0).cpu().numpy()


def score_moment(
    head: nn.Module,
    channels: list[list[float]],
    channel_names: list[str],
    device: str,
) -> tuple[float, bool]:
    emb = None
    if _moment_base_available:
        try:
            emb = _make_real_embedding(channels, channel_names, device)
        except Exception as exc:
            log.warning("real MOMENT inference failed, falling back to proxy: %s", exc)
            emb = None
    if emb is None or emb.shape[0] != _embedding_dim:
        emb = _make_proxy_embedding(channels, _embedding_dim)

    with torch.no_grad():
        x = torch.tensor(emb, dtype=torch.float32, device=device).unsqueeze(0)
        error = head.reconstruction_error(x).item()
    return error, error >= _threshold
