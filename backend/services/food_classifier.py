import io
import threading
from dataclasses import dataclass
from typing import Optional

from PIL import Image, UnidentifiedImageError

try:
    import torch
    from transformers import AutoImageProcessor, AutoModelForImageClassification
except ImportError as exc:  # pragma: no cover - exercised only when deps are missing
    torch = None
    AutoImageProcessor = None
    AutoModelForImageClassification = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


class FoodClassifierError(RuntimeError):
    """Raised when the classifier cannot be loaded or used."""


@dataclass
class FoodPrediction:
    label: str
    confidence: float


@dataclass
class FoodClassificationResult:
    predicted_class: str
    confidence: float
    top_predictions: list[FoodPrediction]
    model_name: str
    model_version: Optional[str]

    @property
    def model_identifier(self) -> str:
        if self.model_version:
            return f"{self.model_name}@{self.model_version}"
        return self.model_name


class FoodClassifierService:
    def __init__(self, model_name: str, top_k: int = 5, enabled: bool = True):
        self.model_name = model_name
        self.top_k = max(top_k, 1)
        self.enabled = enabled
        self._processor = None
        self._model = None
        self._lock = threading.Lock()
        self._device = "cpu"
        self._model_version: Optional[str] = None

    def _ensure_dependencies(self) -> None:
        if IMPORT_ERROR is not None:
            raise FoodClassifierError(
                "Food classifier dependencies are missing. Install pillow, torch and transformers."
            ) from IMPORT_ERROR

    def _load(self) -> None:
        if not self.enabled:
            raise FoodClassifierError("Food classifier is disabled.")

        if self._model is not None and self._processor is not None:
            return

        self._ensure_dependencies()

        with self._lock:
            if self._model is not None and self._processor is not None:
                return

            try:
                self._processor = AutoImageProcessor.from_pretrained(self.model_name)
                self._model = AutoModelForImageClassification.from_pretrained(self.model_name)
                if torch.cuda.is_available():
                    self._device = "cuda"
                    self._model = self._model.to(self._device)
                self._model.eval()
                self._model_version = getattr(self._model.config, "_commit_hash", None)
            except Exception as exc:
                raise FoodClassifierError(
                    f"Could not load food classifier model '{self.model_name}'."
                ) from exc

    def classify_image_bytes(self, image_bytes: bytes, top_k: Optional[int] = None) -> FoodClassificationResult:
        self._load()

        effective_top_k = max(top_k or self.top_k, 1)

        try:
            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise FoodClassifierError("Uploaded image could not be decoded for classification.") from exc

        try:
            inputs = self._processor(images=image, return_tensors="pt")
            if self._device != "cpu":
                inputs = {key: value.to(self._device) for key, value in inputs.items()}

            with torch.no_grad():
                outputs = self._model(**inputs)

            probabilities = torch.nn.functional.softmax(outputs.logits[0], dim=0)
            classes_count = probabilities.shape[0]
            top_values, top_indices = torch.topk(probabilities, k=min(effective_top_k, classes_count))
        except Exception as exc:
            raise FoodClassifierError("Food classifier inference failed.") from exc

        top_predictions = [
            FoodPrediction(
                label=self._resolve_label(index.item()),
                confidence=round(float(value.item()), 6),
            )
            for value, index in zip(top_values, top_indices)
        ]

        best_prediction = top_predictions[0]
        return FoodClassificationResult(
            predicted_class=best_prediction.label,
            confidence=best_prediction.confidence,
            top_predictions=top_predictions,
            model_name=self.model_name,
            model_version=self._model_version,
        )

    def _resolve_label(self, class_index: int) -> str:
        if self._model is None:
            return str(class_index)

        id2label = getattr(self._model.config, "id2label", {}) or {}
        return str(id2label.get(class_index, class_index))
