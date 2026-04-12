import requests
import time
import os
import sys

# --- KONFIGURACJA ---
HOST = "localhost"  # Zmień na localhost jeśli odpalasz to na tej samej maszynie co Docker
API_URL = f"http://{HOST}/api"
IMG_PATH = "test_image.jpg"

# Używamy konta demo zdefiniowanego w Twoim main.py
USERNAME = "testuser"
PASSWORD = "testpass"

def run_test():
    print(f"🚀 ROZPOCZYNAM TEST OCR NA ADRESIE: {HOST}")

    if not os.path.exists(IMG_PATH):
        print(f"❌ BŁĄD: Nie znaleziono pliku '{IMG_PATH}'!")
        print("   Wklej prawdziwe zdjęcie etykiety .jpg do tego folderu i nazwij je 'test_image.jpg'")
        sys.exit(1)

    # 0. Logowanie (Pobranie tokenu JWT)
    print("\n🔑 0. Pobieram token JWT dla konta testowego...")
    resp = requests.post(f"{API_URL}/token", data={"username": USERNAME, "password": PASSWORD})
    if resp.status_code != 200:
        print("❌ Błąd logowania. Upewnij się, że backend działa.", resp.text)
        return
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    print("✅ Zalogowano pomyślnie.")

    # 1. Presign (żądanie URL z folderem 'labels')
    print("\n1️⃣  Wysyłam żądanie o URL (POST /photos/presign)...")
    resp = requests.post(f"{API_URL}/photos/presign", json={"extension": "jpg", "folder": "labels"}, headers=headers)
    if resp.status_code != 200:
        print("❌ Błąd presign:", resp.text)
        return
    
    data = resp.json()
    photo_id = data["photo_id"]
    upload_url = data["upload_url"]
    upload_url = upload_url.replace("192.168.20.20", "localhost")
    print(f"✅ Otrzymano ID zdjęcia: {photo_id}")

    # 2. Upload (przesłanie pliku do MinIO)
    print("\n2️⃣  Wysyłam plik na serwer (PUT)...")
    with open(IMG_PATH, "rb") as f:
        upload_resp = requests.put(upload_url, data=f, headers={'Content-Type': 'image/jpeg'})
    if upload_resp.status_code != 200:
        print("❌ Błąd uploadu:", upload_resp.text)
        return
    print("✅ Plik wysłany pomyślnie!")

    # 3. Confirm (wysłanie do odpowiedniego endpointu dla etykiet)
    print("\n3️⃣  Potwierdzam etykietę w bazie (POST /labels)...")
    confirm_data = {
        "photo": {
            "photo_id": photo_id,
            "width": 1920,
            "height": 1080,
            "extension": "jpg"
        }
    }
    conf_resp = requests.post(f"{API_URL}/labels", json=confirm_data, headers=headers)
    if conf_resp.status_code != 200:
        print("❌ Błąd potwierdzenia:", conf_resp.text)
        return
    print("✅ Etykieta potwierdzona! Backend rozpoczął zadanie OCR w tle.")

    # 4. Polling (oczekiwanie na wynik analizy OCR)
    print("\n4️⃣  Czekam na wynik analizy OCR...")
    for i in range(45):
        time.sleep(2) # Czekamy 2 sekundy przed każdym pytaniem
        check_resp = requests.get(f"{API_URL}/photos/{photo_id}/classification", headers=headers)
        
        if check_resp.status_code == 200:
            result = check_resp.json()
            status = result.get("classification_status")
            print(f"   Próba {i+1}/15... Status: {status}")

            if status == "completed":
                print("\n🎉 SUKCES! Serwer przetworzył obraz. Oto co przeczytał OCR:")
                print("=" * 50)
                texts = result.get("extracted_text", [])
                if not texts:
                    print("   [ Pusta lista - OCR nic nie odnalazł na tym zdjęciu ]")
                    print("   (To sugeruje, że OCR działa, ale nie potrafi rozpoznać liter z tego pliku)")
                else:
                    for line in texts:
                        print(f"   > {line}")
                print("=" * 50)
                return
            elif status == "classification_failed":
                print(f"\n❌ BŁĄD SERWERA: {result.get('error_message')}")
                return
        else:
            print("❌ Błąd przy pobieraniu wyników:", check_resp.text)
            return

    print("\n⚠️ Przekroczono czas oczekiwania. Jeśli serwer działa, sprawdź logi w Dockerze (docker logs backend).")

if __name__ == "__main__":
    run_test()