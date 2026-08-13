from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np
from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REMBG_MODEL_DIR = PROJECT_ROOT / "data" / "models" / "rembg"
MAX_CHROMA_RESIDUE_COMPONENT_RATIO = 0.0003
MAX_WHITE_RESIDUE_COMPONENT_RATIO = 0.001
REMBG_MODEL_MINIMUM_BYTES = {
    "u2netp": 4_000_000,
    "u2net": 160_000_000,
    "u2net_human_seg": 160_000_000,
    "isnet-general-use": 160_000_000,
    "birefnet-portrait": 500_000_000,
}


@dataclass(frozen=True)
class BackgroundRemovalResult:
    output_path: str
    metadata: dict[str, Any]


class CharacterImagePostprocessor(Protocol):
    def process(self, input_path: str, *, model: str = "auto") -> BackgroundRemovalResult: ...


class CharacterBackgroundRemovalTool:
    """Deterministic local postprocessor for character portrait transparency."""

    def __init__(self) -> None:
        self._sessions: dict[str, Any] = {}

    def process(self, input_path: str, *, model: str = "auto") -> BackgroundRemovalResult:
        source = Path(input_path)
        if not source.is_file():
            raise FileNotFoundError(f"character image not found: {source}")
        if source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError(f"unsupported character image format: {source.suffix}")
        with Image.open(source) as existing:
            if existing.mode == "RGBA" and existing.getchannel("A").getextrema()[0] < 255:
                return BackgroundRemovalResult(
                    output_path=str(source),
                    metadata={
                        "background_removed": True,
                        "background_removal_tool": "existing_alpha",
                        "background_removal_model": "already_transparent",
                        **validate_transparent_portrait(source),
                    },
                )

        output_path = _transparent_output_path(source)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        selected_model = _select_rembg_model(model)
        expected_chroma_hsv = _detect_saturated_border_screen_hsv(source)
        candidate_errors: list[str] = []
        normalized_model = str(model or "auto").strip().lower()
        if normalized_model == "auto":
            strategies = [*([("rembg", selected_model)] if selected_model else []), ("chroma", ""), ("contour", "")]
            strategy_name = "rembg_primary_with_local_fallback"
        elif normalized_model == "rembg":
            strategies = [("rembg", selected_model)]
            strategy_name = "rembg_only"
        elif normalized_model in {"chroma", "contour", "local"}:
            strategies = [("contour" if normalized_model == "local" else normalized_model, "")]
            strategy_name = f"{normalized_model}_only"
        else:
            strategies = [("rembg", selected_model)]
            strategy_name = "requested_rembg_model_only"

        for index, (strategy, active_model) in enumerate(strategies, start=1):
            temp_path = output_path.with_name(f".{output_path.stem}.{os.getpid()}.{index}.{strategy}.tmp.png")
            try:
                if strategy == "chroma":
                    chroma_metadata = _remove_with_chroma_key(source, temp_path)
                    processor_metadata = {
                        "background_removal_tool": "opencv_chroma_key",
                        "background_removal_model": "edge_connected_chroma_v1",
                        **chroma_metadata,
                    }
                elif strategy == "contour":
                    _remove_with_local_matting(source, temp_path)
                    processor_metadata = {
                        "background_removal_tool": "opencv_grabcut",
                        "background_removal_model": "border_aware_local_v2",
                    }
                elif strategy == "rembg":
                    if not active_model:
                        raise RuntimeError(f"rembg model is not cached: {model}")
                    temp_path.write_bytes(self._remove_with_rembg(source, active_model))
                    processor_metadata = {
                        "background_removal_tool": "rembg",
                        "background_removal_model": active_model,
                    }
                else:
                    raise ValueError(f"unknown background removal strategy: {strategy}")
                if expected_chroma_hsv is not None:
                    residue_metadata = _chroma_residue_metrics_from_path(temp_path, expected_chroma_hsv)
                    if float(residue_metadata["chroma_residue_largest_component_ratio"]) > MAX_CHROMA_RESIDUE_COMPONENT_RATIO:
                        raise ValueError(
                            "background removal left a large opaque chroma-screen region "
                            f"({residue_metadata['chroma_residue_largest_component_pixels']} px)"
                        )
                    processor_metadata.update(residue_metadata)
                validation = validate_transparent_portrait(temp_path)
                quality_score = _portrait_quality_score(validation, processor_metadata)
                shutil.copyfile(temp_path, output_path)
                return BackgroundRemovalResult(
                    output_path=str(output_path),
                    metadata={
                        "background_removed": True,
                        **processor_metadata,
                        **validation,
                        "background_removal_quality_score": quality_score,
                        "background_removal_strategy": strategy_name,
                        "background_removal_candidate_count": index,
                        "background_removal_candidate_errors": candidate_errors,
                    },
                )
            except Exception as exc:
                candidate_errors.append(f"{strategy}: {type(exc).__name__}: {exc}")
            finally:
                temp_path.unlink(missing_ok=True)
        raise ValueError("all background removal strategies failed: " + "; ".join(candidate_errors))

    def _remove_with_rembg(self, source: Path, model: str) -> bytes:
        model_root = _rembg_model_root()
        model_root.mkdir(parents=True, exist_ok=True)
        # rembg itself reads U2NET_HOME. Keep its network/cache side effects in
        # the project so deployments are portable and never depend on a user's
        # home-directory cache.
        os.environ["U2NET_HOME"] = str(model_root)
        try:
            from rembg import new_session, remove
        except ModuleNotFoundError as exc:
            if exc.name == "onnxruntime":
                raise RuntimeError("automatic character cutout requires the onnxruntime dependency") from exc
            raise RuntimeError("automatic character cutout requires rembg") from exc
        session = self._sessions.get(model)
        if session is None:
            session = new_session(model)
            self._sessions[model] = session
        return remove(source.read_bytes(), session=session, force_return_bytes=True)


def validate_transparent_portrait(path: str | Path) -> dict[str, Any]:
    image_path = Path(path)
    with Image.open(image_path) as image:
        rgba = image.convert("RGBA")
        alpha = rgba.getchannel("A")
        minimum, maximum = alpha.getextrema()
        if minimum == 255:
            raise ValueError("background removal produced an opaque image without transparency")
        if maximum == 0:
            raise ValueError("background removal produced a fully transparent image")
        histogram = alpha.histogram()
        pixel_count = max(1, rgba.width * rgba.height)
        transparent_ratio = sum(histogram[:16]) / pixel_count
        visible_ratio = sum(histogram[240:]) / pixel_count
        alpha_array = np.asarray(alpha, dtype=np.uint8)
        alpha_mass_ratio = float(alpha_array.mean() / 255.0)
        foreground = (alpha_array >= 32).astype(np.uint8)
        foreground_ratio = float(foreground.mean())
        if transparent_ratio < 0.01:
            raise ValueError("background removal left less than 1% transparent pixels")
        if visible_ratio < 0.015 or alpha_mass_ratio < 0.025 or foreground_ratio < 0.025:
            raise ValueError("background removal removed the visible character")
        component_count, component_labels, component_stats, _ = cv2.connectedComponentsWithStats(foreground, connectivity=8)
        if component_count <= 1:
            raise ValueError("background removal did not preserve a character-shaped foreground")
        largest_label = 1 + int(np.argmax(component_stats[1:, cv2.CC_STAT_AREA]))
        largest = component_stats[largest_label]
        largest_ratio = float(largest[cv2.CC_STAT_AREA] / pixel_count)
        bbox_width_ratio = float(largest[cv2.CC_STAT_WIDTH] / rgba.width)
        bbox_height_ratio = float(largest[cv2.CC_STAT_HEIGHT] / rgba.height)
        if largest_ratio < 0.015 or bbox_width_ratio < 0.06 or bbox_height_ratio < 0.22:
            raise ValueError("background removal removed most of the character body")
        bbox_x = int(largest[cv2.CC_STAT_LEFT])
        bbox_y = int(largest[cv2.CC_STAT_TOP])
        bbox_width = int(largest[cv2.CC_STAT_WIDTH])
        bbox_height = int(largest[cv2.CC_STAT_HEIGHT])
        largest_mask = component_labels == largest_label
        bbox_mask = largest_mask[bbox_y : bbox_y + bbox_height, bbox_x : bbox_x + bbox_width]
        bbox_fill_ratio = float(bbox_mask.mean())
        upper_band_height = max(1, int(bbox_height * 0.33))
        upper_band_fill_ratio = float(bbox_mask[:upper_band_height].mean())
        faces = [
            face
            for face in _detect_character_faces(np.asarray(rgba.convert("RGB"), dtype=np.uint8))
            if face[1] + face[3] * 0.5 <= bbox_y + bbox_height * 0.58
        ]
        if len(faces) > 1:
            raise ValueError("character asset contains multiple visible faces instead of one isolated person")
        face_alpha_coverage = None
        face_alpha_mass_ratio = None
        if faces:
            face_x, face_y, face_width, face_height = faces[0]
            inset_x = max(1, int(face_width * 0.12))
            inset_y = max(1, int(face_height * 0.12))
            face_alpha = alpha_array[
                face_y + inset_y : face_y + face_height - inset_y,
                face_x + inset_x : face_x + face_width - inset_x,
            ]
            if face_alpha.size:
                face_alpha_coverage = float((face_alpha >= 64).mean())
                face_alpha_mass_ratio = float(face_alpha.mean() / 255.0)
                if face_alpha_coverage < 0.78 or face_alpha_mass_ratio < 0.72:
                    raise ValueError("background removal cut a transparent hole through the character face")
        # A portrait silhouette normally narrows around the head and shoulders.
        # Large, dense upper regions indicate that a spotlight, moon, wall, or
        # another connected background slab survived GrabCut. Rejecting it is
        # safer than publishing an obviously broken transparent asset.
        if (
            min(rgba.width, rgba.height) >= 256
            and bbox_fill_ratio > 0.58
            and upper_band_fill_ratio > 0.78
        ):
            raise ValueError("background removal left a large solid background region")
        return {
            "alpha_validated": True,
            "transparent_pixel_ratio": round(transparent_ratio, 6),
            "visible_pixel_ratio": round(visible_ratio, 6),
            "alpha_mass_ratio": round(alpha_mass_ratio, 6),
            "foreground_pixel_ratio": round(foreground_ratio, 6),
            "largest_foreground_ratio": round(largest_ratio, 6),
            "foreground_bbox_width_ratio": round(bbox_width_ratio, 6),
            "foreground_bbox_height_ratio": round(bbox_height_ratio, 6),
            "foreground_bbox_fill_ratio": round(bbox_fill_ratio, 6),
            "foreground_upper_band_fill_ratio": round(upper_band_fill_ratio, 6),
            "detected_face_count": len(faces),
            "face_alpha_coverage": round(face_alpha_coverage, 6) if face_alpha_coverage is not None else None,
            "face_alpha_mass_ratio": round(face_alpha_mass_ratio, 6) if face_alpha_mass_ratio is not None else None,
            "image_width": rgba.width,
            "image_height": rgba.height,
        }


def _transparent_output_path(source: Path) -> Path:
    stem = source.stem[:-12] if source.stem.endswith(".transparent") else source.stem
    return source.with_name(f"{stem}.transparent.png")


def _select_rembg_model(model: str) -> str:
    if model == "local":
        return ""
    if model not in {"", "auto", "rembg"}:
        return model if _rembg_model_is_ready(model) else ""
    # Use rembg only when a complete model is already cached. Downloading a
    # model inside a Creator workflow can hang indefinitely on restricted or
    # unstable networks; the deterministic local matting path is immediate and
    # is still protected by validate_transparent_portrait().
    model_root = _rembg_model_root()
    for candidate in ("birefnet-portrait", "u2net_human_seg", "isnet-general-use", "u2net", "u2netp"):
        if _rembg_model_is_ready(candidate, model_root=model_root):
            return candidate
    return ""


def _rembg_model_root() -> Path:
    configured = str(os.getenv("REMBG_MODEL_DIR") or "").strip()
    return Path(configured).expanduser().resolve() if configured else DEFAULT_REMBG_MODEL_DIR


def _rembg_model_is_ready(model: str, *, model_root: Path | None = None) -> bool:
    path = (model_root or _rembg_model_root()) / f"{model}.onnx"
    minimum = REMBG_MODEL_MINIMUM_BYTES.get(model, 1_000_000)
    return path.is_file() and path.stat().st_size >= minimum


def _remove_with_chroma_key(source: Path, output_path: Path) -> dict[str, Any]:
    try:
        image = cv2.imdecode(np.fromfile(source, dtype=np.uint8), cv2.IMREAD_COLOR)
    except OSError:
        image = None
    if image is None:
        raise ValueError(f"unable to decode character image: {source}")
    height, width = image.shape[:2]
    border = max(3, int(min(height, width) * 0.025))
    border_pixels = np.concatenate(
        [image[:border].reshape(-1, 3), image[-border:].reshape(-1, 3), image[:, :border].reshape(-1, 3), image[:, -border:].reshape(-1, 3)],
        axis=0,
    )
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    border_hsv = np.concatenate(
        [hsv[:border].reshape(-1, 3), hsv[-border:].reshape(-1, 3), hsv[:, :border].reshape(-1, 3), hsv[:, -border:].reshape(-1, 3)],
        axis=0,
    )
    # Estimate the screen from the dominant high-saturation border hue.  A
    # median over the entire frame is brittle: a full-body sprite can touch the
    # bottom/side edges and legitimately contaminate 10-30% of border pixels.
    chroma_eligible = (border_hsv[:, 1] >= 90) & (border_hsv[:, 2] >= 28)
    eligible_ratio = float(chroma_eligible.mean())
    background_hue = 0.0
    border_hue_delta = np.full(border_hsv.shape[0], 90.0, dtype=np.float32)
    screen_inliers = np.zeros(border_hsv.shape[0], dtype=bool)
    chroma_coverage = 0.0
    if np.any(chroma_eligible):
        hue_histogram = np.bincount(
            border_hsv[chroma_eligible, 0],
            weights=border_hsv[chroma_eligible, 1].astype(np.float32) / 255.0,
            minlength=180,
        )
        smooth_histogram = sum(np.roll(hue_histogram, offset) for offset in range(-3, 4))
        background_hue = float(int(np.argmax(smooth_histogram)))
        border_hue_delta = _circular_hue_distance(border_hsv[:, 0], background_hue)
        screen_inliers = chroma_eligible & (border_hue_delta <= 12.0)
        chroma_coverage = float(screen_inliers.mean())
    # Keep this intentionally strict so beige/grey illustrated backgrounds do
    # not masquerade as a controlled white screen and bypass GrabCut/rembg.
    white_inliers = (border_hsv[:, 1] <= 32) & (border_hsv[:, 2] >= 218)
    white_coverage = float(white_inliers.mean())
    if chroma_coverage >= 0.32:
        screen_mode = "chroma"
        border_coverage = chroma_coverage
    elif white_coverage >= 0.32:
        screen_mode = "white"
        screen_inliers = white_inliers
        border_coverage = white_coverage
    else:
        raise ValueError("source border is not a dominant white, red, green, or other saturated chroma screen")
    background_bgr = np.median(border_pixels[screen_inliers], axis=0).astype(np.uint8)
    background_hsv = cv2.cvtColor(background_bgr.reshape(1, 1, 3), cv2.COLOR_BGR2HSV).reshape(3)
    hue = hsv[:, :, 0].astype(np.float32)
    saturation = hsv[:, :, 1].astype(np.float32)
    value = hsv[:, :, 2].astype(np.float32)
    if screen_mode == "chroma":
        hue_delta = _circular_hue_distance(hue, background_hue)
        inlier_spread = float(np.percentile(border_hue_delta[screen_inliers], 95))
        core_distance = max(5.0, min(12.0, inlier_spread + 3.0))
        edge_distance = min(30.0, max(18.0, core_distance + 12.0))
        screen_saturation = float(np.median(border_hsv[screen_inliers, 1]))
        minimum_saturation = max(45.0, screen_saturation * 0.28)
        background_candidate = ((hue_delta <= edge_distance) & (saturation >= minimum_saturation)).astype(np.uint8)
        transition = np.clip((hue_delta - core_distance) / max(1.0, edge_distance - core_distance), 0.0, 1.0)
        saturation_transition = np.clip((minimum_saturation + 35.0 - saturation) / 35.0, 0.0, 1.0)
        transition = np.maximum(transition, saturation_transition)
    else:
        lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB).astype(np.float32)
        background_lab = cv2.cvtColor(background_bgr.reshape(1, 1, 3), cv2.COLOR_BGR2LAB).reshape(3).astype(np.float32)
        screen_distance = np.linalg.norm(lab - background_lab, axis=2)
        border_lab = cv2.cvtColor(border_pixels.reshape(-1, 1, 3), cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(np.float32)
        border_screen_distance = np.linalg.norm(border_lab - background_lab, axis=1)
        inlier_spread = float(np.percentile(border_screen_distance[screen_inliers], 95))
        core_distance = max(7.0, inlier_spread + 3.0)
        edge_distance = max(34.0, core_distance + 24.0)
        maximum_saturation = max(72.0, float(background_hsv[1]) + 48.0)
        minimum_value = max(145.0, float(background_hsv[2]) - 82.0)
        background_candidate = (
            (screen_distance <= edge_distance) & (saturation <= maximum_saturation) & (value >= minimum_value)
        ).astype(np.uint8)
        transition = np.clip((screen_distance - core_distance) / max(1.0, edge_distance - core_distance), 0.0, 1.0)
    background_candidate = cv2.morphologyEx(
        background_candidate,
        cv2.MORPH_CLOSE,
        np.ones((3, 3), dtype=np.uint8),
        iterations=1,
    )
    bridge_radius = max(2, int(min(height, width) * 0.008))
    edge_background = _edge_connected_binary(background_candidate, bridge_radius=bridge_radius)
    edge_background_ratio = float(edge_background.mean())
    if edge_background_ratio < 0.08:
        raise ValueError("dominant chroma screen is not connected to enough of the canvas edge")
    alpha = np.full((height, width), 255, dtype=np.float32)
    alpha[edge_background] = transition[edge_background] * 255.0
    alpha = alpha.astype(np.uint8)
    _protect_detected_faces(alpha, image)
    white_cleanup_metadata: dict[str, Any] = {}
    if screen_mode == "white":
        alpha, white_cleanup_metadata = _remove_large_exterior_white_islands(
            image,
            alpha,
            background_candidate,
            transition,
        )
    alpha = cv2.GaussianBlur(alpha, (0, 0), sigmaX=0.55, sigmaY=0.55)
    rgba = cv2.cvtColor(image, cv2.COLOR_BGR2BGRA)
    rgba[:, :, :3] = _decontaminate_chroma_edges(image, alpha, background_bgr)
    rgba[:, :, 3] = alpha
    residue_metadata: dict[str, Any] = {}
    if screen_mode == "chroma":
        residue_metadata = _chroma_residue_metrics(rgba, background_hsv)
        if float(residue_metadata["chroma_residue_largest_component_ratio"]) > MAX_CHROMA_RESIDUE_COMPONENT_RATIO:
            raise ValueError(
                "background removal left a large opaque chroma-screen region "
                f"({residue_metadata['chroma_residue_largest_component_pixels']} px)"
            )
    elif screen_mode == "white":
        residue_metadata = _white_residue_metrics(rgba)
        if float(residue_metadata["white_residue_largest_component_ratio"]) > MAX_WHITE_RESIDUE_COMPONENT_RATIO:
            raise ValueError(
                "background removal left a large opaque white-screen region "
                f"({residue_metadata['white_residue_largest_component_pixels']} px)"
            )
    encoded, buffer = cv2.imencode(".png", rgba)
    if not encoded:
        raise OSError(f"failed to write chroma-key character image: {output_path}")
    buffer.tofile(output_path)
    return {
        "chroma_key_bgr": [int(value) for value in background_bgr],
        "chroma_key_hsv": [int(value) for value in background_hsv],
        "chroma_screen_mode": screen_mode,
        "chroma_dominant_hue": round(background_hue, 6),
        "chroma_eligible_border_ratio": round(eligible_ratio, 6),
        "chroma_border_coverage": round(border_coverage, 6),
        "chroma_edge_background_ratio": round(edge_background_ratio, 6),
        "chroma_bridge_radius": bridge_radius,
        "chroma_border_hue_spread": round(inlier_spread, 6),
        "chroma_core_distance": round(core_distance, 6),
        "chroma_edge_distance": round(edge_distance, 6),
        **white_cleanup_metadata,
        **residue_metadata,
    }


def _circular_hue_distance(hue: np.ndarray, reference: float) -> np.ndarray:
    delta = np.abs(hue.astype(np.float32) - float(reference))
    return np.minimum(delta, 180.0 - delta)


def _decontaminate_chroma_edges(image: np.ndarray, alpha: np.ndarray, background_bgr: np.ndarray) -> np.ndarray:
    """Estimate foreground colors at semitransparent edges to suppress screen spill."""
    result = image.astype(np.float32)
    opacity = alpha.astype(np.float32) / 255.0
    edge = (opacity >= 0.08) & (opacity < 0.985)
    if np.any(edge):
        edge_opacity = opacity[edge, None]
        background = background_bgr.astype(np.float32).reshape(1, 3)
        estimated = (result[edge] - (1.0 - edge_opacity) * background) / np.maximum(edge_opacity, 0.08)
        result[edge] = np.clip(estimated, 0.0, 255.0)
    result[opacity < 0.01] = 0.0
    return result.astype(np.uint8)


def _chroma_residue_metrics(rgba: np.ndarray, background_hsv: np.ndarray) -> dict[str, Any]:
    """Measure opaque screen-coloured islands that survived chroma removal."""
    if rgba.ndim != 3 or rgba.shape[2] != 4:
        raise ValueError("chroma residue inspection requires a BGRA image")
    hsv = cv2.cvtColor(rgba[:, :, :3], cv2.COLOR_BGR2HSV)
    alpha = rgba[:, :, 3]
    hue_delta = _circular_hue_distance(hsv[:, :, 0], float(background_hsv[0]))
    minimum_saturation = max(90.0, float(background_hsv[1]) * 0.45)
    minimum_value = max(35.0, float(background_hsv[2]) * 0.20)
    residue = (
        (alpha >= 128)
        & (hue_delta <= 12.0)
        & (hsv[:, :, 1].astype(np.float32) >= minimum_saturation)
        & (hsv[:, :, 2].astype(np.float32) >= minimum_value)
    ).astype(np.uint8)
    component_count, component_labels, component_stats, _ = cv2.connectedComponentsWithStats(residue, connectivity=8)
    transparent = (alpha < 32).astype(np.uint8)
    exterior_band = (alpha >= 128) & (
        cv2.dilate(transparent, np.ones((3, 3), dtype=np.uint8), iterations=1) != 0
    )
    exterior_component_pixels: list[int] = []
    for label in range(1, component_count):
        if np.any((component_labels == label) & exterior_band):
            exterior_component_pixels.append(int(component_stats[label, cv2.CC_STAT_AREA]))
    largest_pixels = max(exterior_component_pixels, default=0)
    pixel_count = max(1, int(rgba.shape[0] * rgba.shape[1]))
    return {
        "chroma_residue_pixel_ratio": round(float(residue.sum()) / pixel_count, 7),
        "chroma_residue_exterior_component_count": len(exterior_component_pixels),
        "chroma_residue_largest_component_pixels": largest_pixels,
        "chroma_residue_largest_component_ratio": round(largest_pixels / pixel_count, 7),
        "chroma_residue_component_limit_ratio": MAX_CHROMA_RESIDUE_COMPONENT_RATIO,
    }


def _remove_large_exterior_white_islands(
    image: np.ndarray,
    alpha: np.ndarray,
    background_candidate: np.ndarray,
    transition: np.ndarray,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Remove detached white-screen pockets without touching internal white clothing."""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    white = (
        (alpha >= 32)
        & (hsv[:, :, 1].astype(np.float32) <= 40.0)
        & (hsv[:, :, 2].astype(np.float32) >= 210.0)
    ).astype(np.uint8)
    component_count, labels, stats, _ = cv2.connectedComponentsWithStats(white, connectivity=8)
    exterior = cv2.dilate((alpha < 32).astype(np.uint8), np.ones((3, 3), dtype=np.uint8), iterations=1) != 0
    pixel_count = max(1, int(alpha.shape[0] * alpha.shape[1]))
    removed = np.zeros_like(alpha, dtype=bool)
    removed_components = 0
    largest_removed = 0
    for label in range(1, component_count):
        component = labels == label
        area = int(stats[label, cv2.CC_STAT_AREA])
        if area / pixel_count <= MAX_WHITE_RESIDUE_COMPONENT_RATIO or not np.any(component & exterior):
            continue
        removed_components += 1
        largest_removed = max(largest_removed, area)
        # Include the antialiased screen fringe immediately around the solid
        # island, but only where the original white-screen classifier agrees.
        expanded = cv2.dilate(component.astype(np.uint8), np.ones((3, 3), dtype=np.uint8), iterations=1) != 0
        removed |= expanded & (background_candidate != 0)
    if np.any(removed):
        alpha = alpha.copy()
        alpha[removed] = np.minimum(alpha[removed], (transition[removed] * 255.0).astype(np.uint8))
    return alpha, {
        "white_screen_removed_component_count": removed_components,
        "white_screen_largest_removed_component_pixels": largest_removed,
        "white_screen_removed_pixel_ratio": round(float(removed.sum()) / pixel_count, 7),
    }


def _white_residue_metrics(rgba: np.ndarray) -> dict[str, Any]:
    if rgba.ndim != 3 or rgba.shape[2] != 4:
        raise ValueError("white residue inspection requires a BGRA image")
    hsv = cv2.cvtColor(rgba[:, :, :3], cv2.COLOR_BGR2HSV)
    alpha = rgba[:, :, 3]
    residue = (
        (alpha >= 128)
        & (hsv[:, :, 1].astype(np.float32) <= 40.0)
        & (hsv[:, :, 2].astype(np.float32) >= 210.0)
    ).astype(np.uint8)
    component_count, labels, stats, _ = cv2.connectedComponentsWithStats(residue, connectivity=8)
    exterior = (alpha >= 128) & (
        cv2.dilate((alpha < 32).astype(np.uint8), np.ones((3, 3), dtype=np.uint8), iterations=1) != 0
    )
    exterior_areas: list[int] = []
    for label in range(1, component_count):
        if np.any((labels == label) & exterior):
            exterior_areas.append(int(stats[label, cv2.CC_STAT_AREA]))
    largest = max(exterior_areas, default=0)
    pixel_count = max(1, int(rgba.shape[0] * rgba.shape[1]))
    return {
        "white_residue_pixel_ratio": round(float(residue.sum()) / pixel_count, 7),
        "white_residue_exterior_component_count": len(exterior_areas),
        "white_residue_largest_component_pixels": largest,
        "white_residue_largest_component_ratio": round(largest / pixel_count, 7),
        "white_residue_component_limit_ratio": MAX_WHITE_RESIDUE_COMPONENT_RATIO,
    }


def _chroma_residue_metrics_from_path(path: Path, background_hsv: np.ndarray) -> dict[str, Any]:
    try:
        rgba = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    except OSError:
        rgba = None
    if rgba is None or rgba.ndim != 3 or rgba.shape[2] != 4:
        raise ValueError("background removal candidate is not a readable RGBA image")
    return _chroma_residue_metrics(rgba, background_hsv)


def _detect_saturated_border_screen_hsv(source: Path) -> np.ndarray | None:
    """Detect a controlled saturated screen once so every cutout method is gated."""
    try:
        image = cv2.imdecode(np.fromfile(source, dtype=np.uint8), cv2.IMREAD_COLOR)
    except OSError:
        image = None
    if image is None:
        return None
    height, width = image.shape[:2]
    border = max(3, int(min(height, width) * 0.025))
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    border_hsv = np.concatenate(
        [hsv[:border].reshape(-1, 3), hsv[-border:].reshape(-1, 3), hsv[:, :border].reshape(-1, 3), hsv[:, -border:].reshape(-1, 3)],
        axis=0,
    )
    eligible = (border_hsv[:, 1] >= 90) & (border_hsv[:, 2] >= 28)
    if not np.any(eligible):
        return None
    histogram = np.bincount(
        border_hsv[eligible, 0],
        weights=border_hsv[eligible, 1].astype(np.float32) / 255.0,
        minlength=180,
    )
    smooth_histogram = sum(np.roll(histogram, offset) for offset in range(-3, 4))
    hue = float(int(np.argmax(smooth_histogram)))
    inliers = eligible & (_circular_hue_distance(border_hsv[:, 0], hue) <= 12.0)
    if float(inliers.mean()) < 0.32:
        return None
    saturation = int(np.median(border_hsv[inliers, 1]))
    value = int(np.median(border_hsv[inliers, 2]))
    return np.array([int(round(hue)), saturation, value], dtype=np.uint8)


def _edge_connected_binary(candidate: np.ndarray, *, bridge_radius: int = 0) -> np.ndarray:
    count, labels = cv2.connectedComponents(candidate, connectivity=8)
    if count <= 1:
        return np.zeros_like(candidate, dtype=bool)
    edge_labels = np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
    edge_labels = edge_labels[edge_labels != 0]
    connected = np.isin(labels, edge_labels)
    if bridge_radius <= 0 or not np.any(connected):
        return connected
    # Generated sprites often have tiny screen-colored pockets separated from
    # the outer background by a 1-5 px ink outline (behind an ear, between a
    # sleeve and torso, etc.). Include whole chroma components that sit within
    # a narrow distance of the real outer background, while leaving distant
    # internal ornaments untouched.
    diameter = bridge_radius * 2 + 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (diameter, diameter))
    nearby = cv2.dilate(connected.astype(np.uint8), kernel, iterations=1).astype(bool)
    nearby_labels = np.unique(labels[nearby & (candidate != 0)])
    nearby_labels = nearby_labels[nearby_labels != 0]
    return np.isin(labels, np.unique(np.concatenate([edge_labels, nearby_labels])))


def _portrait_quality_score(validation: dict[str, Any], processor: dict[str, Any]) -> float:
    face_coverage = validation.get("face_alpha_coverage")
    face_mass = validation.get("face_alpha_mass_ratio")
    largest = float(validation.get("largest_foreground_ratio") or 0.0)
    fill = float(validation.get("foreground_bbox_fill_ratio") or 0.0)
    method_bonus = 0.45 if processor.get("background_removal_tool") == "opencv_chroma_key" else 0.0
    return round(
        method_bonus
        + (float(face_coverage) if face_coverage is not None else 0.75) * 3.0
        + (float(face_mass) if face_mass is not None else 0.75) * 2.0
        + min(largest, 0.45)
        + min(fill, 0.65) * 0.25,
        6,
    )


def _remove_with_local_matting(source: Path, output_path: Path) -> None:
    try:
        image = cv2.imdecode(np.fromfile(source, dtype=np.uint8), cv2.IMREAD_COLOR)
    except OSError:
        image = None
    if image is None:
        raise ValueError(f"unable to decode character image: {source}")
    height, width = image.shape[:2]
    if min(height, width) < 16:
        raise ValueError("character image is too small for background removal")

    border = max(2, int(min(height, width) * 0.035))
    border_pixels = np.concatenate(
        [
            image[:border].reshape(-1, 3),
            image[-border:].reshape(-1, 3),
            image[:, :border].reshape(-1, 3),
            image[:, -border:].reshape(-1, 3),
        ],
        axis=0,
    )
    background_bgr = np.median(border_pixels, axis=0).astype(np.uint8)
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB).astype(np.float32)
    background_lab = cv2.cvtColor(background_bgr.reshape(1, 1, 3), cv2.COLOR_BGR2LAB).reshape(3).astype(np.float32)
    distance = np.linalg.norm(lab - background_lab, axis=2)
    border_distance = np.concatenate(
        [distance[:border].ravel(), distance[-border:].ravel(), distance[:, :border].ravel(), distance[:, -border:].ravel()]
    )
    background_threshold = max(8.0, float(np.percentile(border_distance, 92)) + 5.0)
    foreground_threshold = max(background_threshold + 12.0, float(np.percentile(distance, 58)))

    mask = np.full((height, width), cv2.GC_PR_BGD, dtype=np.uint8)
    # Similar colors inside robes, hair, or skin must never become definite background.
    # Only the outer frame is trusted as background; color distance supplies foreground seeds.
    mask[distance >= foreground_threshold] = cv2.GC_PR_FGD
    mask[:border] = cv2.GC_BGD
    mask[-border:] = cv2.GC_BGD
    mask[:, :border] = cv2.GC_BGD
    mask[:, -border:] = cv2.GC_BGD
    center_y0, center_y1 = int(height * 0.12), int(height * 0.94)
    center_x0, center_x1 = int(width * 0.22), int(width * 0.78)
    center = mask[center_y0:center_y1, center_x0:center_x1]
    center_distance = distance[center_y0:center_y1, center_x0:center_x1]
    center[center_distance >= foreground_threshold] = cv2.GC_FGD

    background_model = np.zeros((1, 65), np.float64)
    foreground_model = np.zeros((1, 65), np.float64)
    cv2.grabCut(image, mask, None, background_model, foreground_model, 5, cv2.GC_INIT_WITH_MASK)
    foreground = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    edge_background = _edge_connected_background(distance, background_threshold + 14.0)
    foreground[edge_background] = 0
    _protect_detected_faces(foreground, image)
    foreground = _keep_character_components(foreground)
    foreground = cv2.morphologyEx(foreground, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)
    alpha = cv2.GaussianBlur(foreground, (0, 0), sigmaX=0.8, sigmaY=0.8)
    rgba = cv2.cvtColor(image, cv2.COLOR_BGR2BGRA)
    rgba[:, :, 3] = alpha
    encoded, buffer = cv2.imencode(".png", rgba)
    if not encoded:
        raise OSError(f"failed to write transparent character image: {output_path}")
    buffer.tofile(output_path)


def _protect_detected_faces(foreground: np.ndarray, image: np.ndarray) -> None:
    """Keep skin-toned faces from being erased when they resemble the studio background."""
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    for x, y, width, height in _detect_character_faces(rgb):
        center = (x + width // 2, y + height // 2)
        axes = (max(2, int(width * 0.40)), max(2, int(height * 0.44)))
        cv2.ellipse(foreground, center, axes, 0, 0, 360, 255, thickness=-1)


def _detect_character_faces(rgb: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Return de-duplicated, plausibly sized frontal faces for deterministic QA."""
    if rgb.ndim != 3 or min(rgb.shape[:2]) < 64:
        return []
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if cascade.empty():
        return []
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    minimum = max(24, int(min(rgb.shape[:2]) * 0.035))
    detected = cascade.detectMultiScale(gray, scaleFactor=1.08, minNeighbors=4, minSize=(minimum, minimum))
    image_area = rgb.shape[0] * rgb.shape[1]
    candidates = [
        tuple(int(value) for value in face)
        for face in detected
        if int(face[2]) * int(face[3]) >= image_area * 0.0015
    ]
    candidates.sort(key=lambda face: face[2] * face[3], reverse=True)
    kept: list[tuple[int, int, int, int]] = []
    for face in candidates:
        if any(_same_face_cluster(face, existing) for existing in kept):
            continue
        if kept and face[2] * face[3] < kept[0][2] * kept[0][3] * 0.25:
            continue
        kept.append(face)
    return kept


def _same_face_cluster(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> bool:
    if _rectangle_iou(first, second) >= 0.20:
        return True
    first_x, first_y, first_width, first_height = first
    second_x, second_y, second_width, second_height = second
    horizontal_overlap = max(0, min(first_x + first_width, second_x + second_width) - max(first_x, second_x))
    overlap_ratio = horizontal_overlap / max(1, min(first_width, second_width))
    first_center_y = first_y + first_height / 2
    second_center_y = second_y + second_height / 2
    # Frontal-face cascades often detect a xianxia hair bun or forehead as a
    # second face directly above the real face. Treat vertically aligned,
    # nearby boxes as one person; genuine side-by-side people remain separate.
    return overlap_ratio >= 0.45 and abs(first_center_y - second_center_y) <= max(first_height, second_height) * 1.10


def _rectangle_iou(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> float:
    first_x, first_y, first_width, first_height = first
    second_x, second_y, second_width, second_height = second
    left = max(first_x, second_x)
    top = max(first_y, second_y)
    right = min(first_x + first_width, second_x + second_width)
    bottom = min(first_y + first_height, second_y + second_height)
    intersection = max(0, right - left) * max(0, bottom - top)
    union = first_width * first_height + second_width * second_height - intersection
    return float(intersection / union) if union else 0.0


def _keep_character_components(mask: np.ndarray) -> np.ndarray:
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if count <= 1:
        return mask
    height, width = mask.shape
    minimum_area = max(32, int(height * width * 0.0008))
    center = np.array([width / 2, height / 2])
    candidates: list[tuple[float, int]] = []
    for label in range(1, count):
        area = int(stats[label, cv2.CC_STAT_AREA])
        if area < minimum_area:
            continue
        distance = float(np.linalg.norm(centroids[label] - center))
        score = area - distance * 2.0
        candidates.append((score, label))
    if not candidates:
        return mask
    candidates.sort(reverse=True)
    largest_area = int(stats[candidates[0][1], cv2.CC_STAT_AREA])
    keep = [label for _, label in candidates if int(stats[label, cv2.CC_STAT_AREA]) >= largest_area * 0.03]
    return np.where(np.isin(labels, keep), 255, 0).astype(np.uint8)


def _edge_connected_background(distance: np.ndarray, threshold: float) -> np.ndarray:
    candidate = (distance <= threshold).astype(np.uint8)
    count, labels = cv2.connectedComponents(candidate, connectivity=8)
    if count <= 1:
        return np.zeros_like(candidate, dtype=bool)
    edge_labels = np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
    edge_labels = edge_labels[edge_labels != 0]
    return np.isin(labels, edge_labels)
