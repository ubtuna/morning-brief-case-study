# Morning Brief — Google & Meta Ads

E-Trink Global case study. Her sabah 08:00'de iki platformun günlük verisini normalize eden, anomali tespiti yapan, bir LLM ile yönetici brifingine dönüştüren ve Slack + e-posta ile ileten uçtan uca akış.

```
data/                 ham CSV'ler (google_ads_daily.csv, meta_ads_daily.csv)
src/
  config.py           tüm varsayımlar ve eşikler tek yerde
  normalize.py        veri katmanı → tek şema + veri kalitesi raporu
  anomaly.py          anomali katmanı → anomalies.json
  brief.py            LLM katmanı + grounding denetimi + şablon fallback
  deliver.py          Slack webhook + SMTP
  run.py              uçtan uca çalıştırıcı
prompts/              system + user prompt (koddan bağımsız)
automation/README.md  GitHub Actions adımlarının açıklaması
.github/workflows/    morning-brief.yml (cron 08:00 İstanbul)
output/               örnek çıktılar (anomalies.json, brief_sample_llm.md, brief_audit.json ...)
tests/                14 birim testi
EVALUATION.md         5. bölüm: reklam operasyonu değerlendirmesi
```

## Çalıştırma

```bash
pip install -r requirements.txt
python -m pytest -q tests                      # 14 test

python src/run.py --no-llm --no-deliver        # LLM'siz, sadece dosya üret
export ANTHROPIC_API_KEY=sk-ant-...
python src/run.py --no-deliver                 # LLM brifing üret, gönderme
export SLACK_WEBHOOK_URL=https://hooks.slack.com/...
python src/run.py                              # tam akış
python src/run.py --date 2026-08-31            # geçmiş bir gün için yeniden üret
```

Çıktılar `output/` altına yazılır: `normalized.csv`, `data_quality.json`, `anomalies.json`, `brief.md`, `brief_audit.json`. Ortam değişkenleri: `ANTHROPIC_API_KEY`, `SLACK_WEBHOOK_URL`, `SMTP_*`, `EMAIL_FROM/TO`, `BRIEF_LANGUAGE` (varsayılan Turkish), `FX_USD_EUR` (varsayılan 0.92), `LLM_MODEL`.

## 1. Veri katmanı — kararlar

**Ham veride bulunanlar ve yapılanlar**

| Bulgu | Karar |
|---|---|
| Meta'da her kampanya-gün-ülke için `website_pixel` ve `conversions_api` olmak üzere iki satır var; spend/impression/click birebir aynı. Naif toplama harcamayı 2x sayar. | İki satır tek satıra pivot edilir. **İki dönüşüm sayısı da ayrı kolon olarak saklanır** (`conversions_pixel`, `conversions_capi`); raporlama CAPI üzerinden yapılır (tarayıcı engellerinden etkilenmez, ~%19 daha eksiksiz). CAPI/pixel oranı [1,05–1,40] bandı dışına çıkarsa tracking anomalisi olarak işaretlenir — bu, "sorun reklamda mı veride mi" ayrımı için ek bir sinyal. |
| Google EUR, Meta USD. | Raporlama EUR. Kur `config.FX_TO_EUR`'da; uygulanan kur her satıra yazılır (`fx_rate_to_eur`) ki denetlenebilsin. Üretimde günlük ECB kuru ile değiştirilir. |
| `cost_micros` vs `spend`, `conversions` vs `actions_purchase`, `country_code` vs `country`, `date` vs `date_start`. | Tek şema (`normalize.py` başındaki docstring). |
| `NK \| Search \| Brand` aynı isimle iki campaign_id (DE / NL). | Anahtar her zaman `platform + campaign_id + country`; isim yalnızca gösterim için. |
| `VC \| Retargeting \| US` 8–19 Ağustos arası 12 gün yok. | Gap dedektörü raporlar; duraklatma mı veri kaybı mı bilinmediği için brifingde "veri notu" olarak geçer, doldurulmaz. |
| Şema hataları, sayısal olmayan değerler, negatifler, çift satırlar. | `SchemaError`, coerce + uyarı, clip + uyarı, aggregate + uyarı. Her müdahale `data_quality.json`'a yazılır. |

**API entegrasyon notu (Meta Marketing API + Google Ads API)**

1. Kimlik doğrulama: Meta'da System User token (Business Manager, süresiz, `ads_read`), Google'da OAuth2 refresh token + developer token + MCC `login-customer-id`; tümü secret store'da, kodda yok.
2. Çekim granülaritesi: Meta `insights` endpoint'i `level=campaign`, `breakdowns=country`, `action_breakdowns=action_type` ile; Google GAQL `campaign` + `geographic_view` segmentleri ile. Her iki tarafta da `date` kırılımı zorunlu.
3. Incremental sync: her koşuda son 3 günü (`date_preset=last_3d` / `segments.date DURING`) yeniden çekip upsert; çünkü dönüşümler geriye dönük güncellenir. İlk yüklemede 60 gün, sonra sadece pencere.
4. Idempotency: hedef tabloda `(platform, campaign_id, country, date)` unique key; upsert ile tekrar koşmak güvenli.
5. Rate limit: Meta `x-business-use-case-usage` header'ı izlenir, %75 üzerinde exponential backoff; Google'da günlük operasyon kotası ve `RESOURCE_EXHAUSTED` için aynı backoff. Hesap başına sıralı, hesaplar arası paralel.
6. Hata yönetimi: geçici hatalar (5xx, 429, timeout) 3 deneme; kalıcı hatalar (401, geçersiz alan) akışı durdurup Slack'e "veri çekilemedi" mesajı atar — eksik veriyle brifing üretilmez.
7. Şema drift: API'den gelen alan listesi beklenen listeyle karşılaştırılır; yeni/eksik alan uyarı olarak loglanır.
8. Kur: aynı gün ECB referans kuru çekilip `fx_rate_to_eur`'a yazılır.
9. Ham yanıtlar (JSON) tarih damgasıyla object storage'a atılır; normalize hatası olursa yeniden çekmeden düzeltilebilir.
10. Meta async report (büyük hesaplar için `insights` job + polling) ilk sürümde kapsam dışı; senkron endpoint 5–10 kampanyada yeterli.

## 2. Anomali katmanı — eşikler ve gerekçe

Birim: `platform × campaign_id × country`. Her metrik için son gün; 7 günlük ortalama (güncel run-rate), 28 günlük ortalama (haftalık mevsimsellik), 28 günlük z-skoru ve `days_persisting` (kaç gündür 28 günlük tabanın eşik dışında olduğu) hesaplanır.

**Eşiklerin gerekçesi.** Uyarı/kritik yüzdeleri metrik başına farklı: harcama ve CPC için %30/%60, CTR ve ROAS için %25/%50, dönüşüm ve CPA için %30/%50–60. Harcama ve CPC platform tarafından kontrol edilir ve gün içinde doğal olarak daha az oynar; buna karşılık dönüşüm sayıları küçük birimlerde (günde 5–10) Poisson gürültüsü nedeniyle %30 sapmayı sık sık üretir, bu yüzden dönüşüm türevli metriklerde eşik biraz daha esnek ve ayrıca `MIN_DAILY_CONVERSIONS=3` tabanı var. Yüzde tek başına yeterli değil: %40 hareket çok gürültülü bir seride normal, çok sakin bir seride alarmdır; bu yüzden ciddiyet için **hem yüzde hem |z| ≥ 2** gerekir (yalnızca %60+ hareket z olmadan da uyarı verir). Bir sapma 3+ gündür sürüyorsa 7 günlük taban zaten kirlenmiştir; o durumda 28 günlük tabana göre yargılanır — NK Generic DE'nin 5. gününde hâlâ kritik görünmesinin sebebi bu. Günlük harcaması 20 €'nun altındaki birimler oran metriklerinde değerlendirilmez.

**Üç koruma.** (1) *Dönüşüm gecikmesi:* son 2 günün dönüşüm bazlı metrikleri `confidence: low`; bir platformda kampanyaların ≥%70'inde dönüşüm ≥%25 düşerken tıklama ve gösterim ±%25 içinde sabitse bu `systemic_flag: conversion_reporting_lag` olur ve o platformun tüm dönüşüm anomalileri `data_quality / info`'ya iner. Harcama bilinçli olarak bu kapıya dahil değil; tek bir kampanyanın CPC sıçraması platform harcamasını oynatır ama bu gecikme sinyalini bozmamalı. (2) *Kronik:* 28 günlük ROAS < 1,5 ve harcama ≥ 500 € olan birimler "bugün olan bir şey değil ama bilinmeli" diye ayrı listelenir. (3) *Veri kalitesi:* normalize aşamasındaki uyarılar JSON'a eklenir.

JSON'da her anomali: kampanya, ülke, platform, metrik, son değer, 7g/28g taban, değişim oranı, z, yön, etki, ciddiyet, sürme günü, kategori, güven, not.

## 3. LLM katmanı — kısıt ve denetim

Sağlayıcı Anthropic (`claude-sonnet-4-6`). SDK 1.x `temperature` parametresini kaldırdı ve güncel modeller bunu yok sayıyor; tekrarlanabilirlik örneklemeyle değil aşağıdaki denetimle sağlanıyor.

**"Model veri dışına çıkmasın" kısıtı üç katmanda sağlanır:**

1. *Girdi kontrolü.* Model yalnızca `build_payload()` ile sıkıştırılmış JSON'u görür: toplamlar, `info` olmayan anomaliler, sistemik/kronik/veri kalitesi maddeleri. Gecikme nedeniyle düşürülen 42 sinyal tek tek değil sayı olarak verilir ki model içlerinden "seçip" dramatize edemesin. Web araması, araç, önceki konuşma yok.
2. *Prompt sözleşmesi.* Kapalı dünya; her sayı JSON'dan (yuvarlama ve iki JSON sayısının oranı "2,4x" serbest); kampanya adları birebir; veride olmayan nedensel iddia yasak; `data_quality` maddeleri ayrı bölümde ve asla performans olarak sayılmaz; `confidence: low` cümle içinde belirtilir.
3. *Çıktı denetimi (`validate_brief`).* Deterministik, birim testli. Üretilen metindeki **her sayı** çıkarılır (Türkçe ve İngilizce formatlar, yüzde, kat) ve payload'daki sayıların yuvarlanmış/yüzde/kat türevleri kümesine karşı kontrol edilir; **her kampanya benzeri ad** (`XX | ... | ...`) payload'da aranır; **her kritik ve kronik madde** metinde geçmek zorundadır. Başarısız olursa ihlal listesiyle bir kez yeniden istenir; yine olmazsa `template_brief()` (LLM'siz, tamamen şablon) devreye girer — sabah gönderimi hiçbir zaman modele bağımlı değil. Denetim sonucu `brief_audit.json`'a yazılır; testlerde uydurma sayı, uydurma kampanya ve atlanmış kritik bulgu senaryoları kanıtlanır.

Kalite denetimi, brifingi okuyup beğenmek değil, ölçmek: "denetimden geçen brifing oranı" ve "fallback'e düşme oranı" Actions artefaktlarından takip edilebilir. Kapsam dışı: ikinci bir LLM ile "yargıç" değerlendirmesi (deterministik denetim yeterli görüldü).

`output/brief_sample_llm.md`: prompt ve payload ile üretilmiş, denetimden geçmiş örnek çıktı

## 4. Otomasyon

GitHub Actions, `0 5 * * *` UTC = 08:00 İstanbul. Adımlar, secret'lar ve hata yönetimi `automation/README.md`'de. Test adımı gönderimden önce; hiçbir kanal gönderemezse exit 2.
Slack teslimi Actions üzerinden canlı doğrulandı (bkz. [automation/README.md](automation/README.md), "Çalıştırma kanıtı" bölümü)

## 5. Reklam operasyonu değerlendirmesi

`EVALUATION.md` — üç bulgu, her biri için "gerçek mi veri mi" ve bütçe/teklif/kreatif aksiyonu.

## Kapsam dışı bırakılanlar

- Canlı API çekimi (yalnızca tasarım notu). Gerekçe: case CSV ile geliyor; API entegrasyonu kimlik bilgisi olmadan test edilemez.
- Günlük ECB kuru; sabit kur env ile.
- Slack Block Kit / HTML e-posta; düz mrkdwn ve text.
- Gün-of-week mevsimsellik modeli; 28 günlük taban bunu kabaca karşılıyor, tam DOW ayrıştırması için 60 gün kısa.
- LLM-as-judge ikinci denetim.
- Meta CAPI/pixel `event_id` bazlı gerçek dedupe (ham veride yok).
- E-posta gönderimi kodlandı (SMTP) ancak bu teslimde canlı test edilmedi; SMTP_* secret'ları eklendiğinde ek değişiklik gerektirmeden çalışır.

## Yapay zeka kullanımı

Çalışma boyunca Claude (Anthropic) ile birlikte çalıştım. Kullanım alanları:

- Veri keşfi: iki CSV'nin şema farkları, Meta'daki pixel/CAPI çift satırları, para birimi farkı ve 12 günlük veri boşluğu bu aşamada ortaya çıktı.
- Kod: normalize, anomali, brifing ve dağıtım modüllerinin ilk sürümleri, birim testleri ve GitHub Actions workflow'u.
- Dokümantasyon taslağı: README ve EVALUATION.md'nin ilk hâli.

Benim tarafımda kalan kararlar ve kontroller:

- Meta'da hangi attribution kaynağının esas alınacağı (ikisini saklayıp CAPI'yi raporlama kararı) ve eşik değerleri.
- Üç kritik bulgunun seçimi ve "gerçek mi, veri mi" yorumu; aksiyon önerileri.
- Pipeline'ın kendi ortamımda uçtan uca çalıştırılması. İlk canlı çalıştırmada SDK 1.x uyumsuzluğu nedeniyle LLM çağrısı hata verdi; fallback devreye girip şablon brifingi üretti, hata `brief_audit.json`'a düştü. Sorunu tespit edip kodu ve dokümantasyonu güncelledim.
- İlk model çıktısını okuyup prompt'u üç noktada düzelttim: iki dilli başlıklar, gereksiz ayırıcılar ve veri boşluğuna neden atfedilmesi.
- Slack gönderiminde HTTP 200 yanıtının yanlış pozitif olabildiğini (yönlendirme) test sırasında fark edip doğrulamayı Slack'in `ok` yanıtına bağladım.

Brifingin kendisi de üretimde bir LLM tarafından yazıldığı için, çıktı denetimini modele değil deterministik bir doğrulayıcıya bıraktım.
