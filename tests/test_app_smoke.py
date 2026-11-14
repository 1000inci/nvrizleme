"""Arayüz duman testi (ekransız). Görüntü ve gerçek bağlantı test EDİLMEZ."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_app_smoke(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from PySide6.QtWidgets import QApplication

    from nvrizleme import app as appmod
    from nvrizleme.store import Device, load_devices, save_devices

    monkeypatch.setattr(appmod, "make_vlc_instance", lambda: None)
    qapp = QApplication.instance() or QApplication([])
    save_devices([Device(name="Test", serial="ABC", channel_count=20)])
    assert load_devices()[0].channel_count == 20

    win = appmod.MainWindow()
    dev = win.devices[0]
    win.session_pw[dev.id] = "x"
    win.show_after_connect = dev.id
    win.on_connected(dev.id, "127.0.0.1", 5000, [])
    assert len(win.playlist) == 20 and win.page_count() == 5
    assert win.tiles[0]._args[0].endswith("channel=1&subtype=1")
    assert win.tree.topLevelItem(0).childCount() == 20

    win.turn(1)
    assert win.tiles[0]._args[0].endswith("channel=5&subtype=1")
    win.layout_box.setCurrentIndex(2)             # 9'lu: kanal 5 görünür kalır
    assert win.count == 9 and win.page == 0
    win.toggle_zoom(3)                            # kanal 4, ana akış
    assert win.tiles[0]._args[0].endswith("channel=4&subtype=0")
    assert not win.tiles[1].isVisibleTo(win.grid_host)
    win.toggle_zoom(0)
    assert win.tiles[3]._args[0].endswith("channel=4&subtype=1")
    assert win.tiles[0]._retry_at == 0 and win.tiles[3]._retry_at > 0   # sırayla açılır

    win.on_lost(dev.id)                           # tünel koptu
    assert win.tiles[0]._args is None
    win.on_connected(dev.id, "127.0.0.1", 6000, ["A", "B"])
    assert ":6000/" in win.tiles[0]._args[0]      # yeni portla yeniden oynar
    assert win.tree.topLevelItem(0).childCount() == 2

    win.tick()
    win.close()
    qapp.processEvents()
