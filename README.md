# Borsa İstanbul (BIST) Yapay Zeka Analist & Bildirim Ajanı (v2.0)

Aracı kurumların hedef fiyat ve model portföy raporlarını toplayan, teknik ve temel verileri **halk dilinde hazır yorumlanmış** özetlere ve **grafiklere** çeviren, **interaktif Telegram botu** üzerinden cebinize bildirim gönderen 7/24 BIST asistanı.

---

## Öne Çıkan Özellikler (v2.0)

- **Aracı Kurum Hedef Fiyat & Konsensüs Odaklı:** Kurum tavsiyelerini (`AL / TUT / SAT`), ortalama hedef fiyatları ve getiri potansiyelini (%) çıkarır.
- **Hazır Yorumlanmış Teknik & Temel Özet:** Karmaşık terimler yerine *"26 göstergeden 16'sı AL veriyor, RSI şişkin değil"* şeklinde sade ve karar odaklı özet sunar.
- **Akıllı Fırsat Radarı (`/firsatlar`) & Altın Hisseler (`/altinhisseler`):**
  - Fırsat radarı, **en az %35 getiri potansiyeli** ve **en az 2 kurumun AL tavsiyesi** bulunan hisseleri öne çıkarır.
  - **Altın Hisseler** taraması, kurum AL sayısı ve getiri potansiyelini birleştiren bileşik skorla öne çıkan BIST hisselerini sıralar.
- **Temettü Şampiyonları (`/temettu`):** BIST evreninde temettü verimi öne çıkan hisseleri güncel getiri ve analist hedefleriyle listeler.
- **Sektör & Rakip Kıyaslama (`/kiyasla` ve `[⚔️ Sektör Rakipleriyle Kıyasla]`):** Aynı sektördeki rakipleri (ör. `THYAO`, `PGSUS`, `TAVHL`) F/K, PD/DD, hedef fiyat, potansiyel % ve temettü verimi açısından yan yana karşılaştırıp AI yorumu üretir.
- **Karanlık Tema Grafik Üretimi (`[📈 Grafik Göster]` ve `[📊 Portföy Grafiği]`):**
  - Hisse bazında 6 aylık fiyat grafiği, 20 günlük hareketli ortalama (SMA20), ortalama/min-maks hedef fiyat bandı ve varsa kendi alış maliyet çizginiz PNG grafik olarak Telegram'a gönderilir.
  - Portföyünüz için varlık dağılımı ile güncel kâr/zarar (%) vs. analist potansiyeli (%) karşılaştırmalı 2 panelli grafik üretilir.
- **Fotoğraftan Portföy Okuma (OCR + Onay):** Midas, banka veya aracı kurum uygulamanızdaki portföy ekran görüntüsünü sohbete fotoğraf olarak attığınızda hisse kodu, lot ve alış maliyetlerini otomatik okur; onay butonuna bastığınızda portföyünüze kaydeder.
- **Serbest Doğal Dil Soru-Cevap:** Sadece hisse kodu değil, *"Portföyümdeki en riskli hisse hangisi?"* veya *"Uzun vade için havacılık mı bankacılık mı daha ucuz?"* gibi serbest Türkçe sorularınıza portföyünüz ve güncel fırsatlar ışığında yanıt verir.
- **Otomatik Bildirimler & Akıllı Uyarılar:**
  - Yeni aracı kurum raporu veya %35+ potansiyelli fırsat çıktığında anlık bildirim.
  - Portföyünüzdeki hisselerde **gün içi ±%4.5 ve üzeri sert hareketler**, **RSI aşırı ucuzluk (`<= 30`) / aşırı ısınma (`>= 75`)** ve **hedef fiyata yaklaşma** durumlarında otomatik uyarı.
  - Hafta içi akşam **18:30**'da günlük kapanış bülteni.
- **Google Gemini 3.8 Flash:** Tüm yorumları, kıyaslamaları, soru-cevapları ve ekran görüntüsü okumalarını sade Türkçe ile yapar.

---

## Kurulum (5 Dakika)

### 1. Telegram Bot Token Alma
1. Telegram'da `@BotFather` kullanıcısını açın ve `/newbot` yazın.
2. Botunuza bir isim ve `_bot` ile biten bir kullanıcı adı verin (ör. `benim_borsa_ajanim_bot`).
3. BotFather size bir **Token** verecektir (`123456789:AAF...`). Bunu saklayın.

### 2. Ücretsiz Google Gemini API Anahtarı Alma
1. [Google AI Studio](https://aistudio.google.com/apikey) adresine gidin.
2. **"Create API Key"** butonuna tıklayıp ücretsiz API anahtarınızı kopyalayın.

### 3. Ayar Dosyasını Oluşturma
Proje klasöründe `.env.example` dosyasının bir kopyasını `.env` adıyla oluşturun ve içine anahtarlarınızı yazın:
```env
GEMINI_API_KEY=AIzaSy...
TELEGRAM_BOT_TOKEN=123456789:AAF...
TELEGRAM_CHAT_ID=
```
*(Chat ID'yi bilmiyorsanız boş bırakın; botu başlatıp Telegram'dan `/start` yazdığınızda bot size Chat ID'nizi söyleyecektir. Sonra `.env` dosyasına ekleyip botu yeniden başlatabilirsiniz.)*

---

## Windows'ta Çalıştırma ve Manuel Arka Plan Kontrolü

> [!NOTE]
> Ajan, bilgisayarınız her açıldığında **otomatik başlatma (Windows Startup) kaydı oluşturmaz**. Kontrol tamamen sizdedir; yalnızca istediğiniz zaman çift tıklayarak başlatıp durdurabilirsiniz.

### Konsol Penceresiyle Çalıştırma (İlk Kurulum İçin Önerilir)
- **`baslat.bat`** dosyasına çift tıklayın. İlk açılışta `.venv` sanal ortamını ve gerekli kütüphaneleri otomatik kurup botu konsol ekranında başlatır.

### Penceresiz Sessiz Arka Plan Kontrolü (Manuel)
1. **Arka Planda Sessiz Başlatma:** **`arkaplanda_baslat.bat`** dosyasına çift tıklayın.
   - `.venv\Scripts\pythonw.exe` ve `.env` dosyalarını kontrol eder, siyah komut penceresi açmadan ajanı arka planda başlatır ve kısa bir onay mesajı gösterdikten sonra kapanır.
2. **Arka Plandaki Ajanı Durdurma:** **`durdur.bat`** dosyasına çift tıklayın.
   - PowerShell üzerinden `borsa-ajani` / `main.py` çalıştıran Python süreçlerini (`python.exe` / `pythonw.exe`) bulur, temizce sonlandırır ve kaç sürecin durdurulduğunu ekrana yazar.

### Komut Satırından Test Etme (Telegram Olmadan)
```bash
# Tek hisse analizi (örnek portföy maliyetiyle)
.venv\Scripts\python main.py --test-stock THYAO --lot 100 --cost 275

# Fırsat ve yeni rapor taraması (ekrana yazdırır)
.venv\Scripts\python main.py --once-scan --dry-run

# Günlük özet bülteni (ekrana yazdırır)
.venv\Scripts\python main.py --once-summary --dry-run
```

---

## Telegram Komutları ve Etkileşimler

### Temel & Analiz Komutları
- **Hisse kodu yazın:** `THYAO` veya `ASELS ne durumda?` → Güncel fiyat, teknik özet, analist hedefleri ve AI yorumu.
- **Doğal dil sorusu sorun:** *"Portföyümdeki hisseler hakkında ne düşünüyorsun?"* → Portföy ve güncel BIST verilerine göre AI yanıtı.
- **Portföy fotoğrafı gönderin:** Aracı kurum ekran görüntüsünü fotoğraf olarak gönderin → Hisseleri OCR ile okur ve onay butonlarıyla portföyünüze ekler.
- `/ekle THYAO 100 285` → Portföye 100 lot, 285 TL maliyetle ekler.
- `/ekle TUPRS` → Maliyet girmeden izleme listesine ekler.
- `/cikar THYAO` → Listeden çıkarır.
- `/portfoy` → Portföy kâr/zarar durumunu ve hedef fiyat potansiyelini gösterir.

### Tarama & Kıyaslama Komutları (v2.0)
- `/firsatlar` → %35+ potansiyel ve çoklu AL alan hisseleri tarar.
- `/altinhisseler` → Kurum AL sayısı ve getiri potansiyeline göre en güçlü BIST hisselerini sıralar.
- `/temettu` → Temettü verimi yüksek BIST hisselerini listeler.
- `/kiyasla THYAO PGSUS` *(veya tek hisseyle `/kiyasla THYAO`)* → İlgili hisseleri veya sektör rakiplerini yan yana kıyaslar ve AI yorumu üretir.
- `/raporlar` → Son çıkan aracı kurum hedef fiyat raporlarını gösterir.
- `/ozet` → Akşam bültenini beklemeden o an günlük bülten üretir.

### Mesaj Altı Hızlı Butonlar
- **`[📈 Grafik Göster]`** → Hisse kartının altında, 6 aylık fiyat + SMA20 + hedef fiyat + maliyet grafiği üretir.
- **`[📊 Portföy Grafiği]`** → Portföy görünümünün altında, dağılım ve kâr/zarar vs. hedef potansiyeli grafiği üretir.
- **`[⚔️ Sektör Rakipleriyle Kıyasla]`** → Seçili hisseyi otomatik olarak kendi sektöründeki rakipleriyle karşılaştırır.

---

## Bilgisayar Kapalıyken Ücretsiz 7/24 Bulutta Çalıştırma

### Seçenek 1: Render.com (Ücretsiz Docker Web Service + `render.yaml`)
Projedeki [`Dockerfile`](Dockerfile), [`render.yaml`](render.yaml) ve yerleşik `/health` sunucusu (`src/server/keep_alive.py`) Render.com'un ücretsiz katmanı için hazırdır.

1. Projeyi kendi **gizli (private)** GitHub deponuza yükleyin (`.env` ve `data/` dosyalarını yüklemeyin; `.gitignore` bunları zaten hariç tutar).
2. [Render.com](https://render.com) üzerinde ücretsiz hesap açın ve GitHub hesabınızı bağlayın.
3. Dashboard'dan **New + → Blueprint** seçin ve deponuzu bağlayın (`render.yaml` otomatik algılanacaktır).
   - Blueprint kullanmak istemezseniz **New + → Web Service** ile **Runtime: Docker**, **Instance Type: Free**, **Health Check Path: `/health`** ayarlarını seçin.
4. Kurulum ekranında gizli ortam değişkenlerini girin:
   - `GEMINI_API_KEY`: Google AI Studio anahtarınız
   - `TELEGRAM_BOT_TOKEN`: BotFather token'ınız
   - `TELEGRAM_CHAT_ID`: Telegram Chat ID'niz
   - *(Hazır gelenler: `PORT=7860`, `ENABLE_HEALTH_SERVER=1`, `TZ=Europe/Istanbul`, `GEMINI_MODEL=gemini-3.8-flash`)*
5. **Deploy** işlemini başlatın. Servis ayağa kalktığında `https://<uygulamaniz>.onrender.com/health` adresi `200 OK` JSON döner.
6. **Ücretsiz Katmanda Uyku Engelleme (Opsiyonel ama Önerilir):** Render Free web servisleri dışarıdan trafik gelmediğinde 15 dakika sonra uykuya geçebilir. Bunu önlemek için [UptimeRobot](https://uptimerobot.com) veya [cron-job.org](https://cron-job.org) üzerinden her **5–10 dakikada bir** `https://<uygulamaniz>.onrender.com/health` adresine ücretsiz HTTP GET kontrolü tanımlayabilirsiniz. *(Ek olarak `KEEP_ALIVE_URL` veya Render'ın otomatik atadığı `RENDER_EXTERNAL_URL` tanımlıysa sunucu her 600 saniyede bir kendi `/health` ucuna da yoklama atar.)*

---

### Seçenek 2: Hugging Face Spaces (Ücretsiz Docker Space)
Hugging Face Spaces, kredi kartı gerektirmeden Docker konteynerlerini **7860** portu üzerinden ücretsiz barındırabilir.

1. [Hugging Face](https://huggingface.co/spaces) üzerinde ücretsiz hesap açıp **Create new Space** butonuna tıklayın.
2. **Space SDK** olarak **Docker → Blank** seçin, Space görünürlüğünü **Private** yapın ve oluşturun.
3. Space'in **Files** bölümünden veya Git ile bu projenin dosyalarını yükleyin (`Dockerfile`, `requirements.txt`, `main.py`, `src/`, vb. — `.env` dosyasını **kesinlikle yüklemeyin**).
   - Hugging Face Docker SDK için Space `README.md` dosyasının en başında YAML metadata bloğu gerekiyorsa, Space deposundaki `README.md` üstüne şu bloğu ekleyebilirsiniz (`app_port: 7860`):
     ```yaml
     ---
     title: Borsa Ajani
     sdk: docker
     app_port: 7860
     ---
     ```
4. Space içinde **Settings → Variables and secrets** sayfasına gidin:
   - **Repository secrets** olarak ekleyin:
     - `GEMINI_API_KEY`
     - `TELEGRAM_BOT_TOKEN`
     - `TELEGRAM_CHAT_ID`
   - **Variables** olarak ekleyin (Dockerfile içinde varsayılan olarak tanımlıdır ama açıkça belirtebilirsiniz):
     - `ENABLE_HEALTH_SERVER=1`
     - `PORT=7860`
5. Space otomatik olarak Docker imajını derleyip `7860` portunda `/health` ucunu açacak ve Telegram botunu 7/24 çalıştıracaktır.

---

### Seçenek 3: Yerel / VPS Docker Compose
Kendi VPS sunucunuzda veya bilgisayarınızda Docker ile çalıştırmak için:
```bash
docker compose up -d --build
```
*(Veritabanı `./data` klasöründe kalıcı olarak saklanır.)*

---

## Bilgisayar Kapalıyken Yalnızca Zamanlanmış Bildirimler (GitHub Actions)
İnteraktif Telegram komutlarına ihtiyaç duymadan yalnızca hafta içi otomatik tarama ve 18:30 günlük bülteni almak için:
1. Projeyi gizli bir GitHub deposuna yükleyin.
2. **Settings → Secrets and variables → Actions** altına `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN` ve `TELEGRAM_CHAT_ID` secret'larını ekleyin.
3. [`.github/workflows/borsa_ajani_cron.yml`](.github/workflows/borsa_ajani_cron.yml) iş akışı hafta içi gün içi taramaları ve 18:30 özet bültenini otomatik olarak Telegram'a gönderir.

---

> **Yasal Uyarı:** Bu yazılım bilgilendirme ve kişisel takip amaçlıdır; burada üretilen analizler, skorlar, grafikler ve yapay zeka yorumları **yatırım tavsiyesi niteliği taşımaz**.
