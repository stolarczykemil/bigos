import json
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta
from typing import Generator, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from minio import Minio
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker

from services.food_classifier import FoodClassifierService

from services.label_reader import LabelReaderService

logger = logging.getLogger(__name__)

IMAGE_TYPE_FOOD = "food"
IMAGE_TYPE_LABEL = "label"
IMAGE_TYPE_UNKNOWN = "unknown"

CLASSIFICATION_PENDING = "pending"
CLASSIFICATION_COMPLETED = "completed"
CLASSIFICATION_FAILED = "classification_failed"
CLASSIFICATION_NOT_APPLICABLE = "not_applicable"
CLASSIFICATION_DISABLED = "disabled"


def get_env_variable(var_name: str, default: Optional[str] = None) -> str:
    value = os.getenv(var_name, default)
    if value is None:
        print(f"No environmental variable: {var_name}")
        sys.exit(1)
    return value.strip() if isinstance(value, str) else value


def get_bool_env_variable(var_name: str, default: bool = False) -> bool:
    value = os.getenv(var_name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_int_env_variable(var_name: str, default: int) -> int:
    value = os.getenv(var_name)
    if value is None:
        return default

    try:
        return int(value)
    except ValueError as exc:
        print(f"Invalid integer value for environment variable {var_name}: {value}")
        raise SystemExit(1) from exc


DATABASE_URL = get_env_variable("DATABASE_URL")

S3_ENDPOINT = get_env_variable("S3_ENDPOINT")
S3_ACCESS_KEY = get_env_variable("S3_ACCESS_KEY")
S3_SECRET_KEY = get_env_variable("S3_SECRET_KEY")
S3_BUCKET = get_env_variable("S3_BUCKET")

EXTERNAL_HOST = get_env_variable("EXTERNAL_HOST")
EXTERNAL_PORT = get_env_variable("EXTERNAL_PORT")

SECRET_KEY = get_env_variable("SECRET_KEY", "zmien_mnie_na_tajny_klucz_w_produkcji")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
DEMO_USERNAME = get_env_variable("DEMO_USERNAME", "testuser")
DEMO_PASSWORD = get_env_variable("DEMO_PASSWORD", "testpass")

FOOD_CLASSIFIER_ENABLED = get_bool_env_variable("FOOD_CLASSIFIER_ENABLED", True)
FOOD_CLASSIFIER_MODEL = get_env_variable(
    "FOOD_CLASSIFIER_MODEL",
    "ashaduzzaman/vit-finetuned-food101",
)
FOOD_CLASSIFIER_TOP_K = get_int_env_variable("FOOD_CLASSIFIER_TOP_K", 5)

label_reader = LabelReaderService(enabled=True)

engine_kwargs = {}
if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

minio_client: Optional[Minio] = None
food_classifier = FoodClassifierService(
    model_name=FOOD_CLASSIFIER_MODEL,
    top_k=FOOD_CLASSIFIER_TOP_K,
    enabled=FOOD_CLASSIFIER_ENABLED,
)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False)
    password = Column(String(255), nullable=False)

    meals = relationship("Meal", back_populates="owner")
    labels = relationship("Label", back_populates="owner")


class PhotoMetadata(Base):
    __tablename__ = "photos"

    id = Column(String(36), primary_key=True, index=True)
    object_key = Column(String(255), nullable=False)
    width = Column(Integer, nullable=False)
    height = Column(Integer, nullable=False)
    image_type = Column(String(20), nullable=True)
    classification_status = Column(String(32), nullable=True)
    predicted_food_class = Column(String(255), nullable=True)
    classification_confidence = Column(Float, nullable=True)
    top_predictions_json = Column(Text, nullable=True)
    classifier_model_name = Column(String(255), nullable=True)
    classification_error = Column(Text, nullable=True)
    classified_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    extracted_text_json = Column(Text, nullable=True)


class Meal(Base):
    __tablename__ = "meals"

    id = Column(String(36), primary_key=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    owner = relationship("User", back_populates="meals")

    front_photo_id = Column(String(36), ForeignKey("photos.id"), nullable=False)
    left_photo_id = Column(String(36), ForeignKey("photos.id"), nullable=False)
    right_photo_id = Column(String(36), ForeignKey("photos.id"), nullable=False)

    front_photo = relationship("PhotoMetadata", foreign_keys=[front_photo_id])
    left_photo = relationship("PhotoMetadata", foreign_keys=[left_photo_id])
    right_photo = relationship("PhotoMetadata", foreign_keys=[right_photo_id])


class Label(Base):
    __tablename__ = "labels"

    id = Column(String(36), primary_key=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    owner = relationship("User", back_populates="labels")

    photo_id = Column(String(36), ForeignKey("photos.id"), nullable=False)
    photo = relationship("PhotoMetadata", foreign_keys=[photo_id])


def ensure_runtime_schema() -> None:
    Base.metadata.create_all(bind=engine)

    inspector = inspect(engine)
    if "photos" not in inspector.get_table_names():
        return

    existing_columns = {column["name"] for column in inspector.get_columns("photos")}
    photo_column_definitions = {
        "image_type": "VARCHAR(20)",
        "classification_status": "VARCHAR(32)",
        "predicted_food_class": "VARCHAR(255)",
        "classification_confidence": "FLOAT",
        "top_predictions_json": "TEXT",
        "classifier_model_name": "VARCHAR(255)",
        "classification_error": "TEXT",
        "classified_at": "TIMESTAMP",
        "extracted_text_json": "TEXT",
    }

    with engine.begin() as connection:
        for column_name, ddl in photo_column_definitions.items():
            if column_name not in existing_columns:
                connection.execute(text(f"ALTER TABLE photos ADD COLUMN {column_name} {ddl}"))

        connection.execute(
            text(
                """
                UPDATE photos
                SET image_type = CASE
                    WHEN object_key LIKE 'labels/%' THEN :label_type
                    WHEN object_key LIKE 'meals/%' THEN :food_type
                    ELSE image_type
                END
                WHERE image_type IS NULL
                """
            ),
            {"label_type": IMAGE_TYPE_LABEL, "food_type": IMAGE_TYPE_FOOD},
        )

        default_food_status = CLASSIFICATION_PENDING if FOOD_CLASSIFIER_ENABLED else CLASSIFICATION_DISABLED
        connection.execute(
            text(
                """
                UPDATE photos
                SET classification_status = CASE
                    WHEN image_type = :label_type THEN :label_status
                    WHEN image_type = :food_type THEN :food_status
                    ELSE :food_status
                END
                WHERE classification_status IS NULL
                """
            ),
            {
                "label_type": IMAGE_TYPE_LABEL,
                "food_type": IMAGE_TYPE_FOOD,
                "label_status": CLASSIFICATION_NOT_APPLICABLE,
                "food_status": default_food_status,
            },
        )


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_user_internal(db: Session, username: str, password_plain: str) -> User:
    hashed_password = get_password_hash(password_plain)
    db_user = User(username=username, password=hashed_password)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


def ensure_demo_user() -> None:
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == DEMO_USERNAME).first()
        if existing:
            return

        user = create_user_internal(db, DEMO_USERNAME, DEMO_PASSWORD)
        logger.info("Created demo user '%s' (id=%s) for testing login/token.", user.username, user.id)
    finally:
        db.close()


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError as exc:
        raise credentials_exception from exc

    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise credentials_exception
    return user


def get_minio_client() -> Minio:
    global minio_client
    if minio_client is None:
        minio_client = Minio(
            S3_ENDPOINT,
            access_key=S3_ACCESS_KEY,
            secret_key=S3_SECRET_KEY,
            secure=False,
        )
    return minio_client


def ensure_bucket_exists() -> None:
    try:
        client = get_minio_client()
        if not client.bucket_exists(S3_BUCKET):
            client.make_bucket(S3_BUCKET)
    except Exception as exc:
        logger.warning("Could not verify or create bucket '%s': %s", S3_BUCKET, exc)


def fix_minio_url(internal_url: str) -> str:
    port_str = f":{EXTERNAL_PORT}" if EXTERNAL_PORT != "80" else ""
    public_base_url = f"http://{EXTERNAL_HOST}{port_str}/{S3_BUCKET}"
    url_parts = internal_url.split(S3_BUCKET)

    if len(url_parts) < 2:
        return internal_url

    return public_base_url + url_parts[1]


def infer_photo_image_type(photo: PhotoMetadata) -> str:
    if photo.image_type in {IMAGE_TYPE_FOOD, IMAGE_TYPE_LABEL}:
        return photo.image_type

    if photo.object_key.startswith("labels/"):
        return IMAGE_TYPE_LABEL
    if photo.object_key.startswith("meals/"):
        return IMAGE_TYPE_FOOD
    return IMAGE_TYPE_UNKNOWN


def get_default_classification_status(image_type: str) -> str:
    if image_type == IMAGE_TYPE_LABEL:
        return CLASSIFICATION_NOT_APPLICABLE
    if image_type == IMAGE_TYPE_FOOD:
        return CLASSIFICATION_PENDING if FOOD_CLASSIFIER_ENABLED else CLASSIFICATION_DISABLED
    return CLASSIFICATION_NOT_APPLICABLE


def serialize_top_predictions(top_predictions: list) -> str:
    payload = [
        {"label": prediction.label, "confidence": prediction.confidence}
        for prediction in top_predictions
    ]
    return json.dumps(payload)


def deserialize_top_predictions(raw_value: Optional[str]) -> list[dict]:
    if not raw_value:
        return []

    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        logger.warning("Could not decode stored top predictions JSON.")
        return []

    if not isinstance(payload, list):
        return []

    normalized_predictions = []
    for item in payload:
        if isinstance(item, dict):
            try:
                normalized_predictions.append(
                    {
                        "label": str(item.get("label", "")),
                        "confidence": float(item.get("confidence", 0.0)),
                    }
                )
            except (TypeError, ValueError):
                logger.warning("Skipping malformed classification prediction entry: %s", item)
    return normalized_predictions


def photo_belongs_to_user(photo: PhotoMetadata, user: User) -> bool:
    user_prefixes = (
        f"meals/user_{user.id}/",
        f"labels/user_{user.id}/",
    )
    return photo.object_key.startswith(user_prefixes)


def download_photo_bytes(object_key: str) -> bytes:
    response = get_minio_client().get_object(S3_BUCKET, object_key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def persist_classification_failure(photo_id: str, error_message: str) -> None:
    db = SessionLocal()
    try:
        photo = db.query(PhotoMetadata).filter(PhotoMetadata.id == photo_id).first()
        if photo is None:
            return

        photo.image_type = infer_photo_image_type(photo)
        photo.classification_status = CLASSIFICATION_FAILED
        photo.predicted_food_class = None
        photo.classification_confidence = None
        photo.top_predictions_json = None
        photo.classifier_model_name = FOOD_CLASSIFIER_MODEL
        photo.classification_error = error_message[:1000]
        photo.classified_at = None
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not persist failed classification state for photo %s.", photo_id)
    finally:
        db.close()


def classify_food_photo(photo_id: str) -> None:
    db = SessionLocal()
    error_message: Optional[str] = None

    try:
        photo = db.query(PhotoMetadata).filter(PhotoMetadata.id == photo_id).first()
        if photo is None:
            logger.warning("Skipping classification for missing photo %s.", photo_id)
            return

        photo.image_type = infer_photo_image_type(photo)
        if photo.image_type != IMAGE_TYPE_FOOD:
            photo.classification_status = CLASSIFICATION_NOT_APPLICABLE
            db.commit()
            return

        if not FOOD_CLASSIFIER_ENABLED:
            photo.classification_status = CLASSIFICATION_DISABLED
            db.commit()
            return

        photo.classification_status = CLASSIFICATION_PENDING
        photo.classification_error = None
        db.commit()

        image_bytes = download_photo_bytes(photo.object_key)
        result = food_classifier.classify_image_bytes(image_bytes, FOOD_CLASSIFIER_TOP_K)

        photo.predicted_food_class = result.predicted_class
        photo.classification_confidence = result.confidence
        photo.top_predictions_json = serialize_top_predictions(result.top_predictions)
        photo.classifier_model_name = result.model_identifier
        photo.classification_status = CLASSIFICATION_COMPLETED
        photo.classification_error = None
        photo.classified_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("Food classification failed for photo %s.", photo_id)
        error_message = str(exc)
    finally:
        db.close()

    if error_message:
        persist_classification_failure(photo_id, error_message)

def read_label_photo(photo_id: str) -> None:
    db = SessionLocal()
    error_message: Optional[str] = None

    try:
        photo = db.query(PhotoMetadata).filter(PhotoMetadata.id == photo_id).first()
        if photo is None:
            logger.warning("Skipping OCR for missing photo %s.", photo_id)
            return

        photo.classification_status = CLASSIFICATION_PENDING
        photo.classification_error = None
        db.commit()

        image_bytes = download_photo_bytes(photo.object_key)
        
        extracted_lines = label_reader.read_text_from_bytes(image_bytes)

        photo.extracted_text_json = json.dumps(extracted_lines)
        photo.classification_status = CLASSIFICATION_COMPLETED
        photo.classified_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("OCR failed for photo %s.", photo_id)
        error_message = str(exc)
    finally:
        db.close()

    if error_message:
        persist_classification_failure(photo_id, error_message)

class PresignRequest(BaseModel):
    extension: str = "jpg"
    folder: str


class UploadedPhotoInfo(BaseModel):
    photo_id: str
    width: int
    height: int
    extension: str = "jpg"


class CreateMealRequest(BaseModel):
    front: UploadedPhotoInfo
    left: UploadedPhotoInfo
    right: UploadedPhotoInfo


class CreateLabelRequest(BaseModel):
    photo: UploadedPhotoInfo


class Token(BaseModel):
    access_token: str
    token_type: str


class ClassificationPredictionResponse(BaseModel):
    label: str
    confidence: float


class PhotoClassificationResponse(BaseModel):
    photo_id: str
    image_type: str
    classification_status: str
    predicted_class: Optional[str] = None
    confidence: Optional[float] = None
    top_predictions: list[ClassificationPredictionResponse] = Field(default_factory=list)
    model: Optional[str] = None
    classified_at: Optional[datetime] = None
    error_message: Optional[str] = None
    extracted_text: Optional[list[str]] = Field(default_factory=list)


def build_photo_classification_response(photo: PhotoMetadata) -> PhotoClassificationResponse:
    image_type = infer_photo_image_type(photo)
    classification_status = photo.classification_status or get_default_classification_status(image_type)

    extracted_text_list = []
    if photo.extracted_text_json:
        try:
            extracted_text_list = json.loads(photo.extracted_text_json)
        except Exception:
            logger.warning("Could not decode stored extracted text JSON.")

    return PhotoClassificationResponse(
        photo_id=photo.id,
        image_type=image_type,
        classification_status=classification_status,
        predicted_class=photo.predicted_food_class,
        confidence=photo.classification_confidence,
        top_predictions=deserialize_top_predictions(photo.top_predictions_json),
        model=photo.classifier_model_name
        or (FOOD_CLASSIFIER_MODEL if image_type == IMAGE_TYPE_FOOD else None),
        classified_at=photo.classified_at,
        error_message=photo.classification_error,
        extracted_text=extracted_text_list,
    )



app = FastAPI(title="Photo Upload API", root_path="/api")


@app.on_event("startup")
def on_startup() -> None:
    try:
        ensure_runtime_schema()
        ensure_demo_user()
    except Exception as exc:
        logger.warning("Database initialization deferred: %s", exc)

    ensure_bucket_exists()


@app.post("/token", response_model=Token)
def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == form_data.username).first()

    if not user or not verify_password(form_data.password, user.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = create_access_token(data={"sub": user.username})
    return {"access_token": access_token, "token_type": "bearer"}


@app.get("/")
def read_root():
    return {"message": "Photo API is running!"}


@app.get("/users/me")
def read_users_me(current_user: User = Depends(get_current_user)):
    return {"id": current_user.id, "username": current_user.username}


@app.post("/photos/presign")
def generate_presigned_url(req: PresignRequest, current_user: User = Depends(get_current_user)):
    photo_id = str(uuid.uuid4())
    object_key = f"{req.folder}/user_{current_user.id}/{photo_id}.{req.extension}"

    try:
        internal_url = get_minio_client().presigned_put_object(
            S3_BUCKET,
            object_key,
            expires=timedelta(minutes=5),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"MinIO Error: {str(exc)}") from exc

    return {
        "photo_id": photo_id,
        "object_key": object_key,
        "upload_url": fix_minio_url(internal_url),
    }


@app.post("/meals")
def create_meal(
    data: CreateMealRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    food_status = CLASSIFICATION_PENDING if FOOD_CLASSIFIER_ENABLED else CLASSIFICATION_DISABLED

    try:
        created_photo_ids = []
        for photo_data in [data.front, data.left, data.right]:
            photo = PhotoMetadata(
                id=photo_data.photo_id,
                object_key=f"meals/user_{current_user.id}/{photo_data.photo_id}.{photo_data.extension}",
                width=photo_data.width,
                height=photo_data.height,
                image_type=IMAGE_TYPE_FOOD,
                classification_status=food_status,
                classifier_model_name=FOOD_CLASSIFIER_MODEL if FOOD_CLASSIFIER_ENABLED else None,
            )
            created_photo_ids.append(photo.id)
            db.add(photo)

        new_meal = Meal(
            id=str(uuid.uuid4()),
            user_id=current_user.id,
            front_photo_id=data.front.photo_id,
            left_photo_id=data.left.photo_id,
            right_photo_id=data.right.photo_id,
        )
        db.add(new_meal)

        db.commit()

        if FOOD_CLASSIFIER_ENABLED:
            for photo_id in created_photo_ids:
                background_tasks.add_task(classify_food_photo, photo_id)

        return {
            "status": "success",
            "meal_id": new_meal.id,
            "classification_status": food_status,
        }
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database Error: {str(exc)}") from exc


@app.post("/labels")
def create_label(
    data: CreateLabelRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        photo = PhotoMetadata(
            id=data.photo.photo_id,
            object_key=f"labels/user_{current_user.id}/{data.photo.photo_id}.{data.photo.extension}",
            width=data.photo.width,
            height=data.photo.height,
            image_type=IMAGE_TYPE_LABEL,
            classification_status=CLASSIFICATION_PENDING,
        )
        db.add(photo)

        new_label = Label(
            id=str(uuid.uuid4()),
            user_id=current_user.id,
            photo_id=data.photo.photo_id,
        )
        db.add(new_label)

        db.commit()

        background_tasks.add_task(read_label_photo, photo.id)

        return {
            "status": "success",
            "label_id": new_label.id,
            "classification_status": CLASSIFICATION_PENDING
        }
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database Error: {str(exc)}") from exc


@app.get("/photos/{photo_id}/classification", response_model=PhotoClassificationResponse)
def get_photo_classification(
    photo_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    photo = db.query(PhotoMetadata).filter(PhotoMetadata.id == photo_id).first()
    if photo is None or not photo_belongs_to_user(photo, current_user):
        raise HTTPException(status_code=404, detail="Photo not found")

    return build_photo_classification_response(photo)


@app.get("/photos/list")
def list_photos(db: Session = Depends(get_db)):
    photos = db.query(PhotoMetadata).all()
    return photos
