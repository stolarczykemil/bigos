import io
import logging
from typing import Optional

from paddleocr import PaddleOCR
import numpy as np
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

class LabelReaderError(RuntimeError):
    pass

class LabelReaderService:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._ocr_engine = None

    def _load(self):
        if not self.enabled:
            raise LabelReaderError("Label reader is disabled.")
        if self._ocr_engine is None:
            try:
                self._ocr_engine = PaddleOCR(use_angle_cls=True, lang='pl', enable_mkldnn=False)
            except Exception as exc:
                raise LabelReaderError("Could not load PaddleOCR model.") from exc

    def read_text_from_bytes(self, image_bytes: bytes) -> list[str]:
        """
        Zwraca listę linii tekstu odczytanych z obrazka.
        """
        self._load()
        try:
            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            img_array = np.array(image)
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise LabelReaderError("Could not decode image for OCR.") from exc

        try:
            result = self._ocr_engine.ocr(img_array)
        except Exception as exc:
            raise LabelReaderError("OCR processing failed.") from exc

        extracted_text = []
        if result and result[0] is not None:
            for line in result[0]:
                text = line[1][0]
                extracted_text.append(text)

        return extracted_text