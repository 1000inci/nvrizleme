# nvrizleme

Dahua NVR'lar için Windows izleme programı. Öncelik: **P2P (seri numarası) ile bağlantı**.

> Durum: geliştirme aşamasında. Şu an yalnızca P2P bağlantı testi aracı var;
> izleme arayüzü sonraki adım.

## Nasıl çalışır

```
NVR (seri no) ──P2P / Easy4IP──► nvrizleme/p2p.py ──► 127.0.0.1:<yerel port>
                                                          │
                                         VLC / izleme ekranı (normal RTSP)
```

P2P katmanı, NVR'ın RTSP (554) ve HTTP (80) portlarını bilgisayarda yerel
bir porta taşır. Sinyalleşme ve paket biçimi için açık kaynaklı
[OpenDahua](https://github.com/Tijn0/OpenDahua) (MIT) kullanılır.
Bu resmi bir Dahua arayüzü değildir; protokol belgesizdir ve Dahua tarafındaki
bir değişiklikle çalışmaz hale gelebilir. Yalnızca kendi cihazlarınızda kullanın.

## Kurulum (Windows)

1. [Python 3.10+](https://www.python.org/downloads/) (kurulumda "Add to PATH" işaretli)
2. [VLC](https://www.videolan.org/) (görüntüyü izlemek için)
3. Komut satırında, proje klasöründe:

```
pip install -r requirements.txt
```

## P2P bağlantı testi

```
python p2p_test.py SERINO --sadece-durum     şifre sormaz; cihaz P2P'de çevrimiçi mi
python p2p_test.py SERINO                    tam test (şifreyi gizli sorar)
python p2p_test.py SERINO --izle 1           kanal 1'i VLC'de açar
python p2p_test.py SERINO --ayrinti          hata ayıklama için paket dökümü
```

Adımlar: (1) cihaz P2P sunucusunda mı → (2) tünel → (3) model ve kanal listesi → (4) RTSP akışı.

**Dikkat:** art arda hatalı şifre NVR hesabını kilitleyebilir. Bir adım
"şifre reddedildi" derse tekrar denemeden önce şifreyi kontrol edin.

## Bilinen sınırlar

- Simetrik NAT / CGNAT (ör. mobil internet) arkasında doğrudan bağlantı
  kurulamaz; aktarma (relay) modu henüz yok.
- Gerçek cihazla henüz doğrulanmadı; `tests/` yalnızca sahte cihazla
  tünel çoklama mantığını sınar.

## Dosyalar

| Dosya | İş |
|---|---|
| `p2p_test.py` | Adım adım P2P bağlantı testi |
| `nvrizleme/p2p.py` | P2P tüneli, tünel HTTP'si, yerel port yönlendirme |
| `nvrizleme/rtsp.py` | RTSP adresleri ve görüntüsüz RTSP testi |
| `nvrizleme/dahua.py` | NVR HTTP API (yerel ağ / IP bağlantısı için) |
