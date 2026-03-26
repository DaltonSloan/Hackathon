"""Helpers for optional image-based mass estimation."""

from __future__ import annotations

import logging
import os
import shutil
from typing import List, Optional, Tuple

try:  # Optional heavy dependencies – validated at call sites.
    import cv2  # type: ignore
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover - handled gracefully at runtime
    cv2 = None  # type: ignore
    np = None  # type: ignore

try:
    import torch  # type: ignore
    import torchvision  # type: ignore
except ImportError:  # pragma: no cover - handled gracefully at runtime
    torch = None  # type: ignore
    torchvision = None  # type: ignore

try:
    from matplotlib import cm  # type: ignore
except ImportError:  # pragma: no cover - handled gracefully at runtime
    cm = None  # type: ignore

try:
    from segment_anything import SamAutomaticMaskGenerator, sam_model_registry  # type: ignore
except ImportError:  # pragma: no cover - handled gracefully at runtime
    SamAutomaticMaskGenerator = None  # type: ignore
    sam_model_registry = None  # type: ignore

logger = logging.getLogger(__name__)

if torchvision is not None and not hasattr(torchvision, "_is_tracing"):
    torchvision._is_tracing = lambda: False  # type: ignore

_SAM_MASK_GENERATOR: Optional[SamAutomaticMaskGenerator] = None
_MIDAS_MODEL = None
_MIDAS_TRANSFORM = None


class ImageModelDependencyError(RuntimeError):
    """Raised when optional computer-vision dependencies are unavailable."""


def _torch_hub_root() -> str:
    torch_home = os.environ.get("TORCH_HOME")
    base_dir = torch_home or os.path.join(os.path.expanduser("~"), ".cache", "torch")
    return os.path.join(base_dir, "hub")


def _clear_midas_cache() -> None:
    """Remove cached MiDaS artifacts so a corrupted partial download can be retried."""
    hub_root = _torch_hub_root()
    for name in (
        "checkpoints",
        "intel-isl_MiDaS_master",
        "rwightman_gen-efficientnet-pytorch_master",
    ):
        shutil.rmtree(os.path.join(hub_root, name), ignore_errors=True)

    if not os.path.isdir(hub_root):
        return

    for entry in os.listdir(hub_root):
        if entry.endswith(".zip"):
            zip_path = os.path.join(hub_root, entry)
            try:
                os.remove(zip_path)
            except FileNotFoundError:
                continue


def _load_sam() -> Optional[SamAutomaticMaskGenerator]:
    """Return a SAM mask generator if the checkpoint is available."""
    global _SAM_MASK_GENERATOR

    if _SAM_MASK_GENERATOR is not None:
        return _SAM_MASK_GENERATOR
    if SamAutomaticMaskGenerator is None or sam_model_registry is None or torch is None:
        logger.debug("Segment Anything dependencies not installed; skipping SAM init")
        return None

    checkpoint = os.environ.get("SAM_CHECKPOINT_PATH", "sam_vit_b_01ec64.pth")
    if not os.path.exists(checkpoint):
        logger.info("SAM checkpoint not found at %s", checkpoint)
        return None

    try:
        sam_builder = sam_model_registry["vit_b"]
    except KeyError:
        logger.warning("SAM registry missing expected 'vit_b' entry")
        return None

    try:
        sam = sam_builder(checkpoint=checkpoint)
        sam.eval()
    except Exception as exc:  # pragma: no cover - hard to simulate without corrupt weights
        logger.warning("Failed to load SAM checkpoint %s: %s", checkpoint, exc)
        return None

    _SAM_MASK_GENERATOR = SamAutomaticMaskGenerator(sam)
    return _SAM_MASK_GENERATOR


def _ensure_np_cv2() -> None:
    if cv2 is None or np is None:
        raise ImageModelDependencyError(
            "OpenCV and NumPy are required for image-based weight estimation."
        )


def color_filter_custom(mask: "np.ndarray") -> "np.ndarray":
    """Clean up segmentation masks using morphological operations."""
    _ensure_np_cv2()
    mask_uint8 = (mask > 0).astype(np.uint8) * 255
    kernel = np.ones((5, 5), np.uint8)
    clean_mask = cv2.morphologyEx(mask_uint8, cv2.MORPH_OPEN, kernel)
    clean_mask = cv2.morphologyEx(clean_mask, cv2.MORPH_CLOSE, kernel)
    return clean_mask


def segment_grain_pile(image: "np.ndarray") -> Tuple["np.ndarray", List["np.ndarray"], List["np.ndarray"]]:
    """Segment the primary material pile in *image* using SAM."""
    _ensure_np_cv2()
    mask_generator = _load_sam()
    if mask_generator is None:
        raise ImageModelDependencyError(
            "Segment Anything checkpoint not configured. "
            "Set SAM_CHECKPOINT_PATH to the 'sam_vit_b_01ec64.pth' weights file."
        )

    sam_masks = mask_generator.generate(image)
    if not sam_masks:
        return image, [], []

    filtered_masks: List["np.ndarray"] = []
    original_masks: List["np.ndarray"] = []

    for mask_dict in sam_masks:
        sam_mask = mask_dict["segmentation"]
        original_masks.append(sam_mask)
        filtered_masks.append(color_filter_custom(sam_mask))

    masks_with_area = [
        (filtered, original, int(np.sum(filtered > 0)))
        for filtered, original in zip(filtered_masks, original_masks)
    ]
    masks_sorted = sorted(masks_with_area, key=lambda item: item[2], reverse=True)[:5]

    h, w = image.shape[:2]
    center_x, center_y = w // 2, h // 2

    masks_with_distance: List[Tuple["np.ndarray", "np.ndarray", int, float]] = []
    for filtered, original, area in masks_sorted:
        ys, xs = np.nonzero(filtered)
        if len(xs) == 0:
            continue
        cx, cy = float(np.mean(xs)), float(np.mean(ys))
        distance = (cx - center_x) ** 2 + (cy - center_y) ** 2
        masks_with_distance.append((filtered, original, area, distance))

    if not masks_with_distance:
        return image, [], []

    best_filtered, best_original, _, _ = min(masks_with_distance, key=lambda item: item[3])
    return image, [best_filtered], [best_original]


def get_image_mask(image: "np.ndarray") -> Tuple["np.ndarray", "np.ndarray"]:
    """Return a heat-map overlay and binary mask of the segmented material pile."""
    _ensure_np_cv2()
    if cm is None:
        raise ImageModelDependencyError("matplotlib is required for heat-map rendering.")

    image, filtered_masks, _ = segment_grain_pile(image)
    if not filtered_masks:
        raise ValueError("Unable to locate material pile in supplied image.")

    if image.ndim == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)

    if image.shape[2] == 3:
        alpha = np.full((image.shape[0], image.shape[1], 1), 255, dtype=image.dtype)
        image = np.concatenate([image, alpha], axis=2)

    heatmap = (cm.jet(filtered_masks[0])[:, :, :3] * 255).astype(np.uint8)
    overlay = cv2.addWeighted(image[:, :, :3], 0.5, heatmap, 0.5, 0)
    return overlay, filtered_masks[0]


def _load_midas():
    """Load (and cache) the MiDaS depth model + transform."""
    global _MIDAS_MODEL, _MIDAS_TRANSFORM

    if torch is None:
        raise ImageModelDependencyError("PyTorch is required for depth estimation.")

    if _MIDAS_MODEL is not None and _MIDAS_TRANSFORM is not None:
        return _MIDAS_MODEL, _MIDAS_TRANSFORM

    def _load_midas_assets(force_reload: bool = False):
        model = torch.hub.load(
            "intel-isl/MiDaS",
            "MiDaS_small",
            trust_repo=True,
            force_reload=force_reload,
        )
        model.eval()
        transforms = torch.hub.load(
            "intel-isl/MiDaS",
            "transforms",
            trust_repo=True,
            force_reload=force_reload,
        )
        return model, transforms.dpt_transform

    try:
        _MIDAS_MODEL, _MIDAS_TRANSFORM = _load_midas_assets()
    except Exception as exc:  # pragma: no cover - depends on local cache availability
        logger.warning("MiDaS load failed; clearing torch hub cache and retrying once: %s", exc)
        _clear_midas_cache()
        try:
            _MIDAS_MODEL, _MIDAS_TRANSFORM = _load_midas_assets(force_reload=True)
        except Exception as retry_exc:
            raise ImageModelDependencyError(
                "Unable to load MiDaS depth model. Pre-download the weights by running "
                "'torch.hub.load(\"intel-isl/MiDaS\", \"MiDaS_small\")' in an environment "
                "with internet access, then mount the cache via TORCH_HOME."
            ) from retry_exc

    return _MIDAS_MODEL, _MIDAS_TRANSFORM


def calc_volume(image: "np.ndarray") -> Tuple["np.ndarray", float]:
    """Estimate the volume of a material pile captured in *image*."""
    _ensure_np_cv2()

    h, w = image.shape[:2]
    if h == 0 or w == 0:
        raise ValueError("Supplied image has zero dimension.")

    scale = min(1280 / w, 1280 / h)
    if scale < 1.0:
        new_w = int(w * scale)
        new_h = int(h * scale)
        image = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)

    overlay, mask = get_image_mask(image=image)

    midas, transform = _load_midas()
    input_batch = transform(image).to("cpu")

    mask_resized = cv2.resize(mask, (image.shape[1], image.shape[0]), interpolation=cv2.INTER_NEAREST)
    if torch is None or np is None:
        raise ImageModelDependencyError("PyTorch and NumPy are required for depth inference.")

    with torch.no_grad():
        depth = midas(input_batch)
        depth = torch.nn.functional.interpolate(
            depth.unsqueeze(1),
            size=(image.shape[0], image.shape[1]),
            mode="bicubic",
            align_corners=False,
        ).squeeze().cpu().numpy()

    masked_depth = depth * mask_resized
    masked_values = masked_depth[mask_resized > 0]

    if masked_values.size > 0:
        floor_value = masked_values.min()
        filled_depth = masked_depth.copy()
        filled_depth[mask_resized == 0] = floor_value
    else:
        filled_depth = masked_depth

    object_real_length_ft = 19
    object_real_length_m = object_real_length_ft * 0.3048
    pixel_size_m = (object_real_length_m / 30) / image.shape[1]
    pile_volume_m3 = float(np.sum(filled_depth[mask_resized > 0]) * (pixel_size_m**2))

    return overlay, pile_volume_m3


def calc_weight(image: "np.ndarray", density_lbs_per_gal: float) -> Tuple["np.ndarray", float]:
    """Estimate material mass (in tons) for a pile image + material density."""
    overlay, vol_m3 = calc_volume(image=image)

    density_lbs_per_m3 = density_lbs_per_gal / 0.00378541
    mass_lbs = vol_m3 * density_lbs_per_m3
    mass_tons = mass_lbs / 2000.0
    return overlay, mass_tons


__all__ = ["calc_weight", "ImageModelDependencyError"]
