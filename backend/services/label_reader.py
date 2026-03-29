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
                # Inicjalizacja modelu PaddleOCR z językiem polskim.
                # use_angle_cls=True pomaga czytać odwrócone i krzywe etykiety
                self._ocr_engine = PaddleOCR(use_angle_cls=True, lang='pl', show_log=False)
            except Exception as exc:
                raise LabelReaderError("Could not load PaddleOCR model.") from exc

    def read_text_from_bytes(self, image_bytes: bytes) -> list[str]:
        """
        Zwraca listę linii tekstu odczytanych z obrazka.
        """
        self._load()
        try:
            # PaddleOCR najlepiej współpracuje z formatem numpy array (z OpenCV lub PIL)
            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            img_array = np.array(image)
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise LabelReaderError("Could not decode image for OCR.") from exc

        try:
            # Odpalenie OCR
            result = self._ocr_engine.ocr(img_array, cls=True)
        except Exception as exc:
            raise LabelReaderError("OCR processing failed.") from exc

        extracted_text = []
        # PaddleOCR zwraca dane w formacie list list. result[0] zawiera ramki.
        if result and result[0] is not None:
            for line in result[0]:
                # Format line: [[punkty ramki], (tekst, pewność)]
                text = line[1][0]
                extracted_text.append(text)

        return extracted_text