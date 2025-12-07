import os
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel
from minio import Minio
# Biblioteki do bazy danych
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.orm import sessionmaker, declarative_base, Session

# ---------------------------------------------------------
# 1. KONFIGURACJA ZMIENNYCH ŚRODOWISKOWYCH
# ---------------------------------------------------------
# Dane te przychodzą z docker-compose.yml
DATABASE_URL = os.getenv("DATABASE_URL")

S3_ENDPOINT = os.getenv("S3_ENDPOINT", "minio:9000")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "minioadmin")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "minioadmin")
S3_BUCKET = os.getenv("S3_BUCKET", "photos")

# Konfiguracja zewnętrzna (dla Nginx/Telefonu)
EXTERNAL_HOST = os.getenv("EXTERNAL_HOST", "localhost")
EXTERNAL_PORT = os.getenv("EXTERNAL_PORT", "80")

# ---------------------------------------------------------
# 2. KONFIGURACJA BAZY DANYCH (PostgreSQL)
# ---------------------------------------------------------
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# Model Tabeli w bazie
class PhotoMetadata(Base):
    __tablename__ = "photos"

    id = Column(String(36), primary_key=True, index=True)  # UUID
    object_key = Column(String(255), nullable=False)  # Nazwa pliku w MinIO
    width = Column(Integer, nullable=False)
    height = Column(Integer, nullable=False)
    device_model = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# Automatyczne tworzenie tabel przy starcie (jeśli nie istnieją)
# W produkcji używa się do tego narzędzi migracji jak 'Alembic', ale tu wystarczy to:
try:
    Base.metadata.create_all(bind=engine)
except Exception as e:
    print(f"Oczekiwanie na bazę danych... {e}")
    # Docker zrestartuje kontener, jeśli baza nie jest jeszcze gotowa


# Dependency: Funkcja pomagająca bezpiecznie otwierać i zamykać sesję DB
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------
# 3. KONFIGURACJA MINIO
# ---------------------------------------------------------
minio_client = Minio(
    S3_ENDPOINT,
    access_key=S3_ACCESS_KEY,
    secret_key=S3_SECRET_KEY,
    secure=False  # Wewnątrz sieci Dockera nie używamy HTTPS
)

# Upewniamy się, że bucket istnieje
if not minio_client.bucket_exists(S3_BUCKET):
    try:
        minio_client.make_bucket(S3_BUCKET)
        print(f"Utworzono bucket: {S3_BUCKET}")
    except Exception as e:
        print(f"Błąd tworzenia bucketa: {e}")

# ---------------------------------------------------------
# 4. APLIKACJA FASTAPI
# ---------------------------------------------------------
# root_path="/api" informuje Swaggera, że aplikacja jest schowana za Nginxem pod ścieżką /api
app = FastAPI(title="Photo Upload API", root_path="/api")


# Modele danych (to co przychodzi z Androida)
class PresignRequest(BaseModel):
    extension: str = "jpg"  # Domyślnie jpg, ale może być png


class ConfirmUploadRequest(BaseModel):
    photo_id: str
    width: int
    height: int
    device_model: str = "Unknown"


# ---------------------------------------------------------
# 5. ENDPOINTY
# ---------------------------------------------------------

@app.get("/")
def read_root():
    return {"message": "Photo API is running!"}


@app.post("/photos/presign")
def generate_presigned_url(req: PresignRequest):
    """
    KROK 1: Generuje URL, pod który telefon ma wysłać zdjęcie (PUT).
    """
    # Generujemy unikalne ID zdjęcia
    photo_id = str(uuid.uuid4())
    object_key = f"{photo_id}.{req.extension}"

    try:
        # Generujemy URL ważny przez 10 minut
        # Zwrócony URL wygląda np. tak: http://minio:9000/photos/abc.jpg?token=...
        url = minio_client.presigned_put_object(
            S3_BUCKET,
            object_key,
            expires=timedelta(minutes=10)
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"MinIO Error: {str(e)}")

    # --- NGINX FIX ---
    # MinIO zwróciło adres wewnętrzny (minio:9000). Telefon go nie zobaczy.
    # Musimy go podmienić na adres zewnętrzny Nginxa (np. 192.168.1.15:80)

    # 1. Budujemy publiczną bazę URL: http://192.168.X.X:80/photos
    port_str = f":{EXTERNAL_PORT}" if EXTERNAL_PORT != "80" else ""
    public_base_url = f"http://{EXTERNAL_HOST}{port_str}/{S3_BUCKET}"

    # 2. Wyciągamy z oryginalnego URL wszystko co jest PO nazwie bucketa (czyli /plik.jpg?token=...)
    # url.split(S3_BUCKET) dzieli stringa w miejscu wystąpienia nazwy "photos"
    url_parts = url.split(S3_BUCKET)
    if len(url_parts) < 2:
        raise HTTPException(status_code=500, detail="Błąd generowania URL")

    query_params_part = url_parts[1]

    # 3. Sklejamy nowy URL
    final_upload_url = public_base_url + query_params_part

    return {
        "photo_id": photo_id,
        "object_key": object_key,
        "upload_url": final_upload_url
    }


@app.post("/photos/confirm")
def confirm_upload(data: ConfirmUploadRequest, db: Session = Depends(get_db)):
    """
    KROK 2: Telefon potwierdza, że wysłał plik. Zapisujemy metadane w PostgreSQL.
    """

    # Opcjonalnie: Możemy sprawdzić w MinIO czy plik faktycznie tam jest
    # try:
    #     minio_client.stat_object(S3_BUCKET, f"{data.photo_id}.jpg")
    # except:
    #     raise HTTPException(status_code=404, detail="Plik nie znaleziony w MinIO")

    try:
        new_photo = PhotoMetadata(
            id=data.photo_id,
            object_key=f"{data.photo_id}.jpg",  # Zakładamy jpg, w lepszej wersji można przekazywać rozszerzenie
            width=data.width,
            height=data.height,
            device_model=data.device_model
        )

        db.add(new_photo)
        db.commit()  # Zatwierdź transakcję
        db.refresh(new_photo)  # Odśwież obiekt (opcjonalne)

        return {
            "status": "success",
            "saved_id": new_photo.id,
            "created_at": new_photo.created_at
        }

    except Exception as e:
        db.rollback()  # Wycofaj zmiany w razie błędu
        raise HTTPException(status_code=500, detail=f"Database Error: {str(e)}")


# Endpoint pomocniczy do listowania zdjęć (żebyś widział, że działa)
@app.get("/photos/list")
def list_photos(db: Session = Depends(get_db)):
    photos = db.query(PhotoMetadata).all()
    return photos