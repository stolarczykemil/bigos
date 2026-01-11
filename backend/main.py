import os
import uuid
from datetime import datetime, timedelta
import sys
from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel
from minio import Minio
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.orm import sessionmaker, declarative_base, Session, ForeignKey, relationship

# Getting environment variables
def get_env_variable(var_name):
    value = os.getenv(var_name)
    if not value:
        print(f"No environmental variable: {var_name}")
        sys.exit(1)
    return value

# Database
DATABASE_URL = get_env_variable("DATABASE_URL")

# MinIO
S3_ENDPOINT = get_env_variable("S3_ENDPOINT")
S3_ACCESS_KEY = get_env_variable("S3_ACCESS_KEY")
S3_SECRET_KEY = get_env_variable("S3_SECRET_KEY")
S3_BUCKET = get_env_variable("S3_BUCKET")

# External host
EXTERNAL_HOST = get_env_variable("EXTERNAL_HOST")
EXTERNAL_PORT = get_env_variable("EXTERNAL_PORT")


# Database configuration
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False)
    password = Column(String(255), nullable=False)

    photos = relationship("PhotoMetadata", back_populates="owner")

# Metadata table
class PhotoMetadata(Base):
    __tablename__ = "photos"

    id = Column(String(36), primary_key=True, index=True)
    object_key = Column(String(255), nullable=False)
    width = Column(Integer, nullable=False)
    height = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    owner = relationship("User", back_populates="photos")

# Creating table (probably temporary solution)
try:
    Base.metadata.create_all(bind=engine)
except Exception as e:
    print(f"Oczekiwanie na bazę danych... {e}")

# Function for safe creating and closing database session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# minIO configuration
minio_client = Minio(
    S3_ENDPOINT,
    access_key=S3_ACCESS_KEY,
    secret_key=S3_SECRET_KEY,
    secure=False
)

# Bucket creation (probably temporary solution)
if not minio_client.bucket_exists(S3_BUCKET):
    try:
        minio_client.make_bucket(S3_BUCKET)
    except Exception as e:
        print(f"Błąd tworzenia bucketa: {e}")

# fastApi classes
app = FastAPI(title="Photo Upload API", root_path="/api")

class PresignRequest(BaseModel):
    extension: str = "jpg"
    user_id: int

class ConfirmUploadRequest(BaseModel):
    photo_id: str
    width: int
    height: int
    user_id: int
    extension: str = "jpg"


# Endpoints
@app.get("/")
def read_root():
    return {"message": "Photo API is running!"}


# Additional function for URL creation
# Needed because External host can't see minIO and needs to see nginx
def fix_minio_url(internal_url: str) -> str:
    """
    Changes domain minio9000 to EXTERNAL_HOST
    """
    port_str = f":{EXTERNAL_PORT}" if EXTERNAL_PORT != "80" else ""
    public_base_url = f"http://{EXTERNAL_HOST}{port_str}/{S3_BUCKET}"
    url_parts = internal_url.split(S3_BUCKET)

    if len(url_parts) < 2:
        return internal_url

    # correct URL for nginx
    return public_base_url + url_parts[1]

@app.post("/photos/presign")
def generate_presigned_url(req: PresignRequest):
    # Generating photo id
    photo_id = str(uuid.uuid4())
    object_key = f"user_{req.user_id}/{photo_id}.{req.extension}"

    # URL generation, example: http://minio:9000/photos/abc.jpg?token=...
    try:
        internal_url = minio_client.presigned_put_object(
            S3_BUCKET,
            object_key,
            expires=timedelta(minutes=5)
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"MinIO Error: {str(e)}")

    final_upload_url = fix_minio_url(internal_url)

    return {
        "photo_id": photo_id,
        "object_key": object_key,
        "upload_url": final_upload_url
    }

@app.post("/photos/confirm")
def confirm_upload(data: ConfirmUploadRequest, db: Session = Depends(get_db)):
    try:
        new_photo = PhotoMetadata(
            id=data.photo_id,
            object_key=f"user_{data.user_id}/{data.photo_id}.{data.extension}",
            width=data.width,
            height=data.height,
            user_id=data.user_id
        )

        db.add(new_photo)
        db.commit()
        db.refresh(new_photo)

        return {
            "status": "success",
            "saved_id": new_photo.id,
            "created_at": new_photo.created_at
        }

    except Exception as e:
        db.rollback() # If something went wrong discard changes
        raise HTTPException(status_code=500, detail=f"Database Error: {str(e)}")

@app.get("/photos/list")
def list_photos(db: Session = Depends(get_db)):
    photos = db.query(PhotoMetadata).all()
    return photos