import requests
import json
import os

# --- KONFIGURACJA ---
# Jeśli w docker-compose masz "localhost", wpisz tu "localhost".
# Jeśli masz "192.168.1.X", wpisz tu to IP.
HOST = "localhost"

# Adresy wynikające z Nginxa
API_URL = f"http://{HOST}/api"  # Backend jest pod /api
IMG_PATH = "test_image.jpg"


def create_dummy_image():
    # Tworzymy mały plik, żeby mieć co wysłać
    with open(IMG_PATH, "wb") as f:
        f.write(b'\xFF\xD8\xFF\xE0' * 1024)  # Udajemy JPG
    print(f"📁 Utworzono plik testowy: {IMG_PATH}")


def run_test():
    print(f"🚀 ROZPOCZYNAM TEST NA ADRESIE: {HOST}\n")
    create_dummy_image()

    # -------------------------------------------------
    # KROK 1: Presign (Daj mi URL do uploadu)
    # -------------------------------------------------
    print("1️⃣  Wysyłam żądanie o URL (POST /photos/presign)...")
    try:
        resp = requests.post(f"{API_URL}/photos/presign", json={"extension": "jpg"})
        resp.raise_for_status()  # Rzuć błąd jak coś nie tak
    except Exception as e:
        print(f"❌ Błąd połączenia z API: {e}")
        return

    data = resp.json()
    photo_id = data["photo_id"]
    upload_url = data["upload_url"]

    print(f"✅ Otrzymano ID: {photo_id}")
    print(f"🔗 Otrzymano Upload URL: {upload_url}")

    # -------------------------------------------------
    # KROK 2: Upload (Wyślij plik do Nginx -> MinIO)
    # -------------------------------------------------
    print("\n2️⃣  Wysyłam plik (PUT)...")

    with open(IMG_PATH, "rb") as f:
        # WAŻNE: Tu "telefon" uderza na URL wygenerowany przez backend
        # Ten URL powinien wskazywać na Nginx (port 80)
        upload_resp = requests.put(upload_url, data=f)

    if upload_resp.status_code == 200:
        print("✅ MinIO (przez Nginx) przyjęło plik!")
    else:
        print(f"❌ Błąd uploadu! Kod: {upload_resp.status_code}")
        print(upload_resp.text)
        return

    # -------------------------------------------------
    # KROK 3: Confirm (Potwierdź w bazie)
    # -------------------------------------------------
    print("\n3️⃣  Potwierdzam zapis (POST /photos/confirm)...")
    confirm_data = {
        "photo_id": photo_id,
        "width": 800,
        "height": 600,
        "device_model": "Python Script"
    }

    conf_resp = requests.post(f"{API_URL}/photos/confirm", json=confirm_data)

    if conf_resp.status_code == 200:
        print("✅ Sukces! Metadane w bazie.")
        print("📥 Odpowiedź serwera:", conf_resp.json())
    else:
        print(f"❌ Błąd potwierdzenia: {conf_resp.text}")

    # -------------------------------------------------
    # KROK 4: Weryfikacja (Lista zdjęć)
    # -------------------------------------------------
    print("\n4️⃣  Pobieram listę wszystkich zdjęć w bazie...")
    list_resp = requests.get(f"{API_URL}/photos/list")
    print(json.dumps(list_resp.json(), indent=2))

    # Sprzątanie
    os.remove(IMG_PATH)
    print("\n🏁 TEST ZAKOŃCZONY.")


if __name__ == "__main__":
    run_test()