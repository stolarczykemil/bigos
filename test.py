import requests
import json
import os
import sys

# --- KONFIGURACJA ---
HOST = "192.168.0.41"  # Twoje IP
API_URL = f"http://{HOST}/api"
IMG_PATH = "test_image.jpg"  # <-- Skrypt szuka pliku o tej nazwie w tym samym folderze


def run_test():
    print(f"🚀 ROZPOCZYNAM TEST NA ADRESIE: {HOST}")
    print(f"📂 Szukam pliku: {IMG_PATH}...")

    # 0. Sprawdzenie czy plik istnieje (żeby nie wywaliło błędu później)
    if not os.path.exists(IMG_PATH):
        print(f"❌ BŁĄD: Nie znaleziono pliku '{IMG_PATH}'!")
        print("   Wklej prawdziwe zdjęcie .jpg do tego folderu i nazwij je 'test_image.jpg'")
        sys.exit(1)

    print("   Plik znaleziony. Jedziemy z koksem.\n")

    # -------------------------------------------------
    # KROK 1: Presign (Daj mi link do uploadu)
    # -------------------------------------------------
    print("1️⃣  Wysyłam żądanie o URL (POST /photos/presign)...")
    try:
        # Zakładamy, że to JPG, bo tak nazwaliśmy plik
        resp = requests.post(f"{API_URL}/photos/presign", json={"extension": "jpg"})
        resp.raise_for_status()
    except Exception as e:
        print(f"❌ Błąd połączenia z API: {e}")
        # Częsty błąd: jeśli dostajesz 502, to znaczy że backend leży, sprawdź 'docker logs backend'
        if hasattr(e, 'response') and e.response is not None:
            print(f"   Treść błędu: {e.response.text}")
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
        # requests.put automatycznie streamuje plik
        headers = {'Content-Type': 'image/jpeg'}  # Dobra praktyka, żeby ustawić typ
        upload_resp = requests.put(upload_url, data=f, headers=headers)

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

    # Dane symulujące telefon. Wymiary wpisane "na sztywno",
    # w prawdziwej apce Android pobierze je z bitmapy.
    confirm_data = {
        "photo_id": photo_id,
        "width": 1920,
        "height": 1080,
        "extension": "jpg"
    }

    conf_resp = requests.post(f"{API_URL}/photos/confirm", json=confirm_data)

    if conf_resp.status_code == 200:
        print("✅ Sukces! Metadane zapisane w bazie.")
        print("📥 Odpowiedź serwera:", conf_resp.json())
    else:
        print(f"❌ Błąd potwierdzenia: {conf_resp.text}")
        return

    # -------------------------------------------------
    # KROK 4: Weryfikacja (Lista zdjęć)
    # -------------------------------------------------
    print("\n4️⃣  Pobieram listę wszystkich zdjęć w bazie...")
    try:
        list_resp = requests.get(f"{API_URL}/photos/list")
        print(json.dumps(list_resp.json(), indent=2))
    except Exception as e:
        print(f"⚠️ Nie udało się pobrać listy (ale upload działał): {e}")

    print("\n🏁 TEST ZAKOŃCZONY.")


if __name__ == "__main__":
    run_test()