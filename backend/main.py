import os
import uuid
from datetime import datetime, timedelta
import sys
from typing import Optional

# --- NOWE IMPORTY DO AUTH ---
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
# ----------------------------

from pydantic import BaseModel
from minio import Minio
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.orm import sessionmaker, declarative_base, Session, relationship

# Getting environment variables
def get_env_variable(var_name, default=None):
    value = os.getenv(var_name, default)
    if not value and default is None:
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

# --- ZMIENNE DO AUTH ---
# Pamiętaj, żeby dodać SECRET_KEY do pliku .env!
SECRET_KEY = get_env_variable("SECRET_KEY", "zmien_mnie_na_tajny_klucz_w_produkcji")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
# Demo credentials (can be overridden via .env)
DEMO_USERNAME = get_env_variable("DEMO_USERNAME", "testuser")
DEMO_PASSWORD = get_env_variable("DEMO_PASSWORD", "testpass")
# -----------------------

# Database configuration
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- KONFIGURACJA SECURITY (ARGON2) ---
# To tutaj definiujemy, ze uzywamy Argon2
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")
# --------------------------------------

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False)
    password = Column(String(255), nullable=False) # Tutaj bedzie hash

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

# --- FUNKCJE POMOCNICZE AUTH ---

def verify_password(plain_password, hashed_password):
    """Sprawdza hasło używając Argon2"""
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    """Generuje hash hasła używając Argon2"""
    return pwd_context.hash(password)

def create_access_token(data: dict):
    """Generuje token JWT"""
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

# FUNKCJA DLA WAS (ADMINÓW) - zgodnie z prośbą.
# Nie jest podpięta pod żaden endpoint. Możesz jej użyć w konsoli Pythona albo w skrypcie,
# żeby stworzyć sobie użytkownika z zahaszowanym hasłem.
def create_user_internal(db: Session, username: str, password_plain: str):
    hashed_password = get_password_hash(password_plain)
    db_user = User(username=username, password=hashed_password)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user
# -------------------------------

def ensure_demo_user():
    """
    Tworzy u‘•ytkownika testowego, je‘>li nie istnieje.
    U‘•ywa danych z DEMO_USERNAME / DEMO_PASSWORD (domyœlnie testuser / testpass).
    """
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == DEMO_USERNAME).first()
        if existing:
            return
        user = create_user_internal(db, DEMO_USERNAME, DEMO_PASSWORD)
        print(f"Created demo user '{user.username}' (id={user.id}) for testing login/token.")
    finally:
        db.close()

ensure_demo_user()

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


class ConfirmUploadRequest(BaseModel):
    photo_id: str
    width: int
    height: int
    extension: str = "jpg"

class Token(BaseModel):
    access_token: str
    token_type: str


# Endpoints

# --- ENDPOINT LOGOWANIA ---
@app.post("/token", response_model=Token)
def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    # 1. Pobierz usera z bazy
    user = db.query(User).filter(User.username == form_data.username).first()
    
    # 2. Zweryfikuj hasło (Argon2)
    if not user or not verify_password(form_data.password, user.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # 3. Wygeneruj token
    access_token = create_access_token(data={"sub": user.username})
    return {"access_token": access_token, "token_type": "bearer"}
# --------------------------

@app.get("/")
def read_root():
    return {"message": "Photo API is running!"}


@app.get("/users/me")
def read_users_me(current_user: User = Depends(get_current_user)):
    return {"id": current_user.id, "username": current_user.username}


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
def generate_presigned_url(req: PresignRequest, current_user: User = Depends(get_current_user)):
    # Generating photo id
    photo_id = str(uuid.uuid4())
    object_key = f"user_{current_user.id}/{photo_id}.{req.extension}"

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
def confirm_upload(data: ConfirmUploadRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        new_photo = PhotoMetadata(
            id=data.photo_id,
            object_key=f"user_{current_user.id}/{data.photo_id}.{data.extension}",
            width=data.width,
            height=data.height,
            user_id=current_user.id
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
