# Reklam Operasyonu Değerlendirmesi — En Kritik Üç Bulgu

Tüm rakamlar EUR (USD → EUR 0,92), Meta dönüşümleri CAPI kaynaklı. Rapor günü 3 Eylül 2026.

## 1. NK | Search | Generic DE (Google, DE): CPC 3 katına çıktı, harcama kontrolden çıkıyor

**Ne görüyoruz.** 30 Ağustos'tan itibaren 5 gündür CPC 0,45 € → 1,37 €, günlük harcama 180 € → 530 €. Tıklama (396 → 389) ve gösterim (9,6k → 10,3k) sabit, CTR %4,1 → %3,8. Dönüşüm 8,5 → 6,8/gün, CPA 21 € → 94 €, ROAS 3,1 → 0,96. Beş günde ~1.750 € fazladan harcandı; dün Nordkraft'ın Google harcamasının %38'i bu tek kampanya-ülkeye gitti.

**Gerçek mi, veri mi?** Gerçek. Aynı trafik için üç kat fiyat ödeniyor; gösterim ve tıklama hacmi değişmediğine göre bu bir raporlama veya kur hatası değil (Google verisi zaten EUR). Tam bir gece içinde başlaması ve seviyenin sabit kalması, kademeli bir rekabet artışından çok tek bir hesap değişikliğine benziyor: teklif stratejisi değişimi (manuel CPC → tCPA/tROAS, hedef gevşetme), Geniş eşleme açılması ya da maks. CPC limiti kaldırılması.

**Aksiyon.** Bugün: Google Ads değişiklik geçmişinde 29–30 Ağustos'a bakılır; teklif/eşleme değişikliği varsa geri alınır. Bulunamazsa geçici olarak kampanya bütçesi eski seviyeye (≈200 €/gün) sabitlenir ve arama terimi raporunda yeni pahalı sorgular negatiflenir. Bu kampanya Nordkraft'ın en yüksek hacimli generic kampanyası; kapatmak yerine maliyeti eski seviyeye çekmek doğru refleks.

## 2. Her iki platformda son iki gün dönüşümler %25–60 düştü — ama bu bir performans sorunu değil

**Ne görüyoruz.** 2 Eylül'de Google −%34, Meta −%25; 3 Eylül'de Google −%58, Meta −%60 (28 günlük ortalamaya göre). Aynı günlerde tıklama Google'da +%17, Meta'da +%10; harcama sabit veya artıda. Düşüş 18 kampanya-ülke biriminin **tamamında** aynı anda, aynı şekilde.

**Gerçek mi, veri mi?** Veri. Farklı platformlardaki, farklı ülke ve funnel aşamalarındaki tüm kampanyaların aynı gün eş zamanlı çökmesi ama trafiğin sabit kalması, tek bir açıklamayı işaret eder: dönüşüm atıf penceresi henüz dolmadı. Google'da tıklama sonrası dönüşümler günlerce, Meta'da 7 günlük tıklama / 1 günlük görüntüleme penceresinde geç işlenir; "son gün" verisi her zaman eksiktir ve gün 2'de daha da eksiktir. Bunu bir sabah brifingi "tüm hesaplarda ROAS çöktü" diye raporlarsa, ekip yanlış yere bütçe keser. Anomali motoru bu yüzden 42 dönüşüm bazlı sinyali otomatik olarak `data_quality / reporting lag` sınıfına indirir ve yalnızca teslim metriklerini (harcama, CPC, CTR) yüksek güvenle raporlar.

**Aksiyon.** Bütçe/teklif/kreatif aksiyonu **yok**; alınırsa zarar verir. Operasyonel aksiyon: dönüşüm bazlı KPI'lar için son 2 günü "geçici" işaretleyip 3 gün gecikmeli karşılaştırmayı standart yapmak; Meta'da CAPI/pixel oranı (1,19) günlük izlenip pixel kopması ayrıca yakalanır.

## 3. VC | Prospecting | US (Meta): 9 haftadır başabaş civarında, portföyün en büyük harcaması

**Ne görüyoruz.** Son 28 günde 10.000 € harcama (tüm portföyün %15'i, en büyük tek birim) ve ROAS 1,10 (CAPI) / 0,92 (pixel). Haftalık ROAS 9 haftadır 1,0–1,3 bandında; bugün olan bir şey değil, hiç düzelmemiş bir şey. Aynı hesabın Retargeting kampanyası ROAS 4,9 ile çalışıyor.

**Gerçek mi, veri mi?** Gerçek, ama tek başına okunmamalı. Prospecting'in görevi funnel'ın üstünü doldurmak; son tıklama ROAS'ı yapısal olarak düşük olur ve değerinin bir kısmı Retargeting'de görünür. Ancak 8–19 Ağustos'ta Retargeting 12 gün tamamen durduğunda Prospecting ROAS'ı değişmedi (1,07); yani retargeting'in prospecting'e sırtını yasladığı yönünde bir kanıt da yok. ~1,1 ROAS, tipik e-ticaret brüt marjıyla net zarar demek.

**Aksiyon.** Kapatma değil, kademeli yeniden yapılandırma: (1) Bütçe %25–30 düşürülür, serbest kalan tutar ROAS 4,9 olan Retargeting'e ve AH hesabında ROAS 3,6 üreten Advantage+ Shopping benzeri bir kurulumun VC'ye taşınmasına gider. (2) Kreatif tarafında 9 haftadır aynı seviyede kalan bir kampanyada büyük ihtimalle kreatif yorgunluğu var: CTR %1 bandında; yeni kreatif seti ve kitle testi açılır. (3) Teklif tarafında purchase optimizasyonu maliyet limiti (cost cap) ile denenir; 2 hafta sonra aynı brifing bu değişimin etkisini otomatik gösterir.

---

**Not:** İkinci ve üçüncü bulgunun bir arada olması bu sistemin neden gerekli olduğunu özetliyor: panelden bakan bir göz 3 Eylül sabahı "her şey çöktü" diye VC Prospecting'i kapatır, oysa gerçek sorun Nordkraft'ın generic kampanyasında sessizce biriken 1.750 €'dur.
