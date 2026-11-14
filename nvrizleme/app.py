"""NVR İzleme - ana pencere.

Sol: cihaz ve kanal listesi. Orta: 1/4/9/16/25/36'lı görüntü ızgarası.
Alt: bağlantı günlüğü. Izgarada alt akış, tek kanala büyütünce ana akış.
"""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDockWidget,
    QFormLayout, QFrame, QGridLayout, QInputDialog, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QSpinBox, QToolBar, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import rtsp, store
from .links import Bridge, LinkManager
from .store import Device

try:
    import vlc
except Exception:  # python-vlc veya VLC kurulu değil
    vlc = None

LAYOUTS = [1, 4, 9, 16, 25, 36]
MAX_TILES = max(LAYOUTS)
ROLE_DEVICE = Qt.UserRole
ROLE_CHANNEL = Qt.UserRole + 1


def make_vlc_instance():
    if vlc is None:
        return None
    try:
        return vlc.Instance("--no-video-title-show", "--quiet", "--no-audio")
    except Exception:
        return None


@dataclass
class Link:
    """Bağlı bir cihazın RTSP erişim bilgisi."""
    host: str
    port: int
    password: str
    names: list[str]


@dataclass(frozen=True)
class Entry:
    """Izgarada gösterilecek tek kanal."""
    device_id: str
    channel: int      # 1 tabanlı


class VideoTile(QFrame):
    clicked = Signal(int)
    double_clicked = Signal(int)

    def __init__(self, index: int, instance) -> None:
        super().__init__()
        self.index = index
        self._instance = instance
        self._player = None
        self._key = None            # şu an oynatılan (url, kullanıcı, şifre)
        self._args = None
        self._started = 0.0
        self._retry_at = 0.0
        self._retry_delay = 10
        self._base_title = ""

        self.title = QLabel("")
        self.title.setStyleSheet("color:#ddd;background:#222;padding:2px 6px;")
        self.video = QWidget()
        self.video.setAttribute(Qt.WA_NativeWindow, True)
        self.video.setStyleSheet("background:black;")
        self.video.installEventFilter(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, 1, 1, 1)
        lay.setSpacing(0)
        lay.addWidget(self.title)
        lay.addWidget(self.video, 1)
        self.set_selected(False)

    # --- seçim / fare ---------------------------------------------------
    def set_selected(self, on: bool) -> None:
        self.setStyleSheet("VideoTile{border:2px solid %s;background:black;}"
                           % ("#2d8cf0" if on else "#333"))

    def eventFilter(self, obj, event):
        if obj is self.video:
            if event.type() == QEvent.MouseButtonDblClick:
                self.double_clicked.emit(self.index)
                return True
            if event.type() == QEvent.MouseButtonPress:
                self.clicked.emit(self.index)
        return False

    def mousePressEvent(self, event):
        self.clicked.emit(self.index)

    def mouseDoubleClickEvent(self, event):
        self.double_clicked.emit(self.index)

    # --- oynatma --------------------------------------------------------
    def play(self, url: str, user: str, password: str, title: str,
             delay: float = 0.0) -> bool:
        """Oynatmayı başlatır. Yeni bir akış başlatıldıysa True döner.

        delay: akışın kaç saniye sonra açılacağı. Karolar aynı anda değil
        sırayla açılsın diye kullanılır (cihaza ani yük bindirmemek için).
        """
        key = (url, user, password)
        if key == self._key:
            self._base_title = title
            return False
        self.stop()
        self._base_title = title
        self._key = key
        self._args = (url, user, password)
        self._retry_delay = 10
        if delay > 0:
            self._retry_at = time.monotonic() + delay
            self.title.setText(f"{title} - sırada…")
        else:
            self._start()
        return True

    def _start(self) -> None:
        url, user, password = self._args
        self._started = time.monotonic()
        self._retry_at = 0.0
        if self._instance is None:
            self.title.setText(f"{self._base_title} - VLC bulunamadı")
            return
        self.title.setText(f"{self._base_title} - bağlanıyor…")
        # Kimlik bilgisi hem adreste hem seçenek olarak verilir; gömülü VLC
        # şifre penceresi açamadığı için ikisinden biri mutlaka işlemeli.
        auth_url = url.replace(
            "rtsp://", f"rtsp://{quote(user, safe='')}:{quote(password, safe='')}@", 1)
        media = self._instance.media_new(auth_url)
        media.add_option(":rtsp-tcp")
        media.add_option(":network-caching=400")
        media.add_option(f":rtsp-user={user}")
        media.add_option(f":rtsp-pwd={password}")
        player = self._instance.media_player_new()
        player.set_media(media)
        # Fare/klavye olaylarını VLC yerine Qt alsın (çift tıklama için).
        player.video_set_mouse_input(False)
        player.video_set_key_input(False)
        wid = int(self.video.winId())
        if sys.platform == "win32":
            player.set_hwnd(wid)
        elif sys.platform == "darwin":
            player.set_nsobject(wid)
        else:
            player.set_xwindow(wid)
        player.play()
        self._player = player

    def _release_player(self) -> None:
        player, self._player = self._player, None
        if player is None:
            return

        def work() -> None:      # stop() RTSP kapanışında bekleyebilir
            try:
                player.stop()
                player.release()
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def stop(self) -> None:
        self._release_player()
        self._key = None
        self._args = None
        self._base_title = ""
        self.title.setText("")

    def tick(self) -> None:
        """Durumu günceller; hata / kopmada artan aralıkla yeniden dener."""
        if self._args is None or self._instance is None:
            return
        now = time.monotonic()
        if self._player is None:
            if now >= self._retry_at:
                self._start()
            return
        state = self._player.get_state()
        if state == vlc.State.Playing:
            self.title.setText(self._base_title)
            if now - self._started > 30:
                self._retry_delay = 10
            return
        failed = state in (vlc.State.Error, vlc.State.Ended, vlc.State.Stopped)
        stuck = now - self._started > 25      # hâlâ görüntü yok
        state_name = str(state).split(".")[-1]
        if failed or stuck:
            self._release_player()
            self._retry_at = now + self._retry_delay
            self.title.setText(f"{self._base_title} - görüntü yok ({state_name}), "
                               f"{self._retry_delay} sn sonra yeniden denenecek")
            self._retry_delay = min(self._retry_delay * 2, 120)
        else:
            self.title.setText(f"{self._base_title} - bağlanıyor… ({state_name})")


class DeviceDialog(QDialog):
    def __init__(self, parent, device: Device | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Cihaz")
        self.device = device
        d = device or Device(name="")

        self.name = QLineEdit(d.name)
        self.kind = QComboBox()
        self.kind.addItem("P2P (seri numarası)", "p2p")
        self.kind.addItem("IP adresi / alan adı", "ip")
        self.kind.setCurrentIndex(0 if d.kind == "p2p" else 1)
        self.serial = QLineEdit(d.serial)
        self.host = QLineEdit(d.host)
        self.http_port = QSpinBox(); self.http_port.setRange(1, 65535)
        self.http_port.setValue(d.http_port)
        self.rtsp_port = QSpinBox(); self.rtsp_port.setRange(1, 65535)
        self.rtsp_port.setValue(d.rtsp_port)
        self.user = QLineEdit(d.user)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText(
            "değiştirmemek için boş bırakın" if device else "")
        self.save_pw = QCheckBox("Şifreyi Windows Kimlik Yöneticisi'ne kaydet")
        has_saved = bool(device and store.get_password(device.id))
        self.save_pw.setChecked(has_saved or device is None)
        if not store.keyring_available():
            self.save_pw.setChecked(False)
            self.save_pw.setEnabled(False)
            self.save_pw.setText("Şifre kaydı için 'keyring' paketi gerekli")
        self.channels = QSpinBox(); self.channels.setRange(1, 256)
        self.channels.setValue(d.channel_count)

        form = QFormLayout(self)
        form.addRow("Ad", self.name)
        form.addRow("Bağlantı türü", self.kind)
        form.addRow("Seri numarası", self.serial)
        form.addRow("Adres", self.host)
        form.addRow("HTTP portu", self.http_port)
        form.addRow("RTSP portu", self.rtsp_port)
        form.addRow("Kullanıcı adı", self.user)
        form.addRow("Şifre", self.password)
        form.addRow("", self.save_pw)
        form.addRow("Kanal sayısı", self.channels)
        note = QLabel("Kanal sayısı, cihaz kanal listesini vermediğinde kullanılır.\n"
                      "P2P'de portlar cihazdan otomatik okunur.")
        note.setStyleSheet("color:gray;")
        form.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.kind.currentIndexChanged.connect(self._sync)
        self._sync()

    def _sync(self) -> None:
        p2p = self.kind.currentData() == "p2p"
        self.serial.setEnabled(p2p)
        for w in (self.host, self.http_port, self.rtsp_port):
            w.setEnabled(not p2p)

    def accept(self) -> None:
        p2p = self.kind.currentData() == "p2p"
        target = (self.serial if p2p else self.host).text().strip()
        if not target:
            QMessageBox.warning(self, "Eksik bilgi",
                                "Seri numarası gerekli." if p2p else "Adres gerekli.")
            return
        super().accept()

    def result_device(self) -> Device:
        d = self.device or Device(name="")
        d.kind = self.kind.currentData()
        d.serial = self.serial.text().strip().upper()
        d.host = self.host.text().strip()
        d.http_port = self.http_port.value()
        d.rtsp_port = self.rtsp_port.value()
        d.user = self.user.text().strip() or "admin"
        d.channel_count = self.channels.value()
        d.name = self.name.text().strip() or d.target
        return d


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("NVR İzleme")
        self.resize(1400, 850)

        self.devices: list[Device] = store.load_devices()
        self.links: dict[str, Link] = {}
        self.session_pw: dict[str, str] = {}     # kaydedilmeyen şifreler (oturumluk)
        self.status: dict[str, str] = {}
        self.playlist: list[Entry | None] = []
        self.page = 0
        self.count = 4
        self.selected = 0
        self.zoom_back: tuple[int, int] | None = None   # (karo sayısı, sayfa)
        self.zoom_entry: Entry | None = None
        self.show_after_connect: str | None = None

        self.bridge = Bridge()
        self.manager = LinkManager(self.bridge)
        self.bridge.log.connect(self.log)
        self.bridge.status.connect(self.on_status)
        self.bridge.connected.connect(self.on_connected)
        self.bridge.lost.connect(self.on_lost)
        self.bridge.failed.connect(self.on_failed)

        self.vlc_instance = make_vlc_instance()

        # --- ızgara ---
        self.grid_host = QWidget()
        self.grid_host.setStyleSheet("background:#111;")
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(2, 2, 2, 2)
        self.grid.setSpacing(2)
        self.tiles = [VideoTile(i, self.vlc_instance) for i in range(MAX_TILES)]
        for t in self.tiles:
            t.clicked.connect(self.select_tile)
            t.double_clicked.connect(self.toggle_zoom)
        self.setCentralWidget(self.grid_host)

        # --- cihaz ağacı ---
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemDoubleClicked.connect(self.on_tree_double)
        dock = QDockWidget("Cihazlar", self)
        dock.setWidget(self.tree)
        dock.setFeatures(QDockWidget.DockWidgetMovable)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

        # --- günlük ---
        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setMaximumBlockCount(500)
        logdock = QDockWidget("Bağlantı günlüğü", self)
        logdock.setWidget(self.logbox)
        self.addDockWidget(Qt.BottomDockWidgetArea, logdock)
        self.resizeDocks([logdock], [140], Qt.Vertical)

        # --- araç çubuğu ---
        tb = QToolBar("Araçlar")
        tb.setMovable(False)
        self.addToolBar(tb)
        for text, slot in (("Cihaz Ekle", self.add_device),
                           ("Düzenle", self.edit_device),
                           ("Sil", self.delete_device)):
            act = QAction(text, self); act.triggered.connect(slot); tb.addAction(act)
        tb.addSeparator()
        for text, slot in (("Bağlan ve Göster", self.connect_selected),
                           ("Bağlantıyı Kes", self.disconnect_selected)):
            act = QAction(text, self); act.triggered.connect(slot); tb.addAction(act)
        tb.addSeparator()
        tb.addWidget(QLabel(" Ekran: "))
        self.layout_box = QComboBox()
        for n in LAYOUTS:
            self.layout_box.addItem(str(n), n)
        self.layout_box.setCurrentIndex(LAYOUTS.index(self.count))
        self.layout_box.currentIndexChanged.connect(self.on_layout_changed)
        tb.addWidget(self.layout_box)
        prev = QAction("◀ Önceki", self); prev.triggered.connect(lambda: self.turn(-1))
        nxt = QAction("Sonraki ▶", self); nxt.triggered.connect(lambda: self.turn(1))
        tb.addAction(prev)
        self.page_label = QLabel(" 1/1 ")
        tb.addWidget(self.page_label)
        tb.addAction(nxt)
        tb.addSeparator()
        clear = QAction("Ekranı Temizle", self); clear.triggered.connect(self.clear_view)
        tb.addAction(clear)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(500)

        self.rebuild_tree()
        self.apply_layout()
        if self.vlc_instance is None:
            self.log("UYARI: VLC bulunamadı. Görüntü için 64 bit VLC kurun ve "
                     "'pip install python-vlc' çalıştırın.")
        self.log("Hazır. Cihaz eklemek için 'Cihaz Ekle', izlemek için cihaza "
                 "çift tıklayın.")

    # --- yardımcılar ----------------------------------------------------
    def log(self, text: str) -> None:
        self.logbox.appendPlainText(f"{time.strftime('%H:%M:%S')}  {text}")

    def device(self, device_id: str) -> Device | None:
        return next((d for d in self.devices if d.id == device_id), None)

    def channel_names(self, dev: Device) -> list[str]:
        link = self.links.get(dev.id)
        if link and link.names:
            return link.names
        return [f"Kanal {i}" for i in range(1, dev.channel_count + 1)]

    def current_device_id(self) -> str | None:
        item = self.tree.currentItem()
        return item.data(0, ROLE_DEVICE) if item else None

    # --- cihaz ağacı ----------------------------------------------------
    def rebuild_tree(self) -> None:
        current = self.current_device_id()
        self.tree.clear()
        for dev in self.devices:
            state = self.status.get(dev.id, "bağlı değil")
            top = QTreeWidgetItem([f"{dev.name}  -  {state}"])
            top.setData(0, ROLE_DEVICE, dev.id)
            top.setToolTip(0, f"{'P2P' if dev.kind == 'p2p' else 'IP'}: {dev.target}")
            self.tree.addTopLevelItem(top)
            if dev.id in self.links:
                for i, name in enumerate(self.channel_names(dev), 1):
                    child = QTreeWidgetItem([f"{i}. {name}"])
                    child.setData(0, ROLE_DEVICE, dev.id)
                    child.setData(0, ROLE_CHANNEL, i)
                    top.addChild(child)
                top.setExpanded(True)
            if dev.id == current:
                self.tree.setCurrentItem(top)

    def on_tree_double(self, item: QTreeWidgetItem, _col: int) -> None:
        device_id = item.data(0, ROLE_DEVICE)
        channel = item.data(0, ROLE_CHANNEL)
        if channel is None:
            self.connect_and_show(device_id)
            return
        # Kanalı seçili karoya yerleştir.
        self.leave_zoom()
        pos = self.page * self.count + self.selected
        while len(self.playlist) <= pos:
            self.playlist.append(None)
        self.playlist[pos] = Entry(device_id, channel)
        self.select_tile(min(self.selected + 1, self.count - 1))
        self.refresh()

    # --- cihaz ekle / düzenle / sil -------------------------------------
    def add_device(self) -> None:
        dlg = DeviceDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        dev = dlg.result_device()
        self.devices.append(dev)
        self._store_password(dev, dlg)
        store.save_devices(self.devices)
        self.rebuild_tree()

    def edit_device(self) -> None:
        dev = self.device(self.current_device_id() or "")
        if dev is None:
            return
        dlg = DeviceDialog(self, dev)
        if dlg.exec() != QDialog.Accepted:
            return
        dlg.result_device()
        self._store_password(dev, dlg)
        store.save_devices(self.devices)
        self.rebuild_tree()
        self.log(f"[{dev.name}] Ayarlar kaydedildi. Değişiklikler bir sonraki "
                 "bağlantıda geçerli olur.")

    def _store_password(self, dev: Device, dlg: DeviceDialog) -> None:
        pw = dlg.password.text()
        if not dlg.save_pw.isChecked():
            store.delete_password(dev.id)
            if pw:
                self.session_pw[dev.id] = pw
        elif pw:
            if not store.set_password(dev.id, pw):
                self.session_pw[dev.id] = pw
                self.log(f"[{dev.name}] Şifre kaydedilemedi; bu oturum için "
                         "bellekte tutulacak.")

    def delete_device(self) -> None:
        dev = self.device(self.current_device_id() or "")
        if dev is None:
            return
        if QMessageBox.question(self, "Sil", f"'{dev.name}' silinsin mi?") \
                != QMessageBox.Yes:
            return
        self.drop_link(dev.id)
        store.delete_password(dev.id)
        self.devices.remove(dev)
        self.status.pop(dev.id, None)
        store.save_devices(self.devices)
        self.rebuild_tree()

    # --- bağlantı -------------------------------------------------------
    def connect_selected(self) -> None:
        device_id = self.current_device_id()
        if device_id:
            self.connect_and_show(device_id)

    def connect_and_show(self, device_id: str) -> None:
        dev = self.device(device_id)
        if dev is None:
            return
        if device_id in self.links:
            self.show_device(device_id)
            return
        if "bağlan" in self.status.get(device_id, "") and \
                self.status.get(device_id) != "bağlı":
            self.show_after_connect = device_id
            return                      # zaten bağlanıyor; ikinci kez başlatma
        password = store.get_password(dev.id) or self.session_pw.get(dev.id)
        if not password:
            password, ok = QInputDialog.getText(
                self, "Şifre", f"'{dev.name}' için '{dev.user}' şifresi:",
                QLineEdit.Password)
            if not ok or not password:
                return
        self.session_pw[dev.id] = password
        self.show_after_connect = device_id
        self.on_status(device_id, "bağlanıyor…")
        self.manager.connect(dev, password)

    def disconnect_selected(self) -> None:
        device_id = self.current_device_id()
        if device_id:
            self.drop_link(device_id)
            self.on_status(device_id, "bağlı değil")

    def drop_link(self, device_id: str) -> None:
        self.manager.disconnect(device_id)
        self.links.pop(device_id, None)
        self.playlist = [e if e and e.device_id != device_id else None
                         for e in self.playlist]
        if self.zoom_entry and self.zoom_entry.device_id == device_id:
            self.leave_zoom()
        self.refresh()

    def on_status(self, device_id: str, text: str) -> None:
        self.status[device_id] = text
        self.rebuild_tree()

    def on_connected(self, device_id: str, host: str, port: int, names: list) -> None:
        dev = self.device(device_id)
        if dev is None:
            return
        self.links[device_id] = Link(host, port, self.session_pw.get(device_id, ""),
                                     list(names))
        count = len(names) or dev.channel_count
        self.log(f"[{dev.name}] Bağlandı. {count} kanal"
                 f"{'' if names else ' (ayardaki sayı)'}.")
        self.on_status(device_id, "bağlı")
        if self.show_after_connect == device_id:
            self.show_after_connect = None
            self.show_device(device_id)
        else:
            self.refresh()        # yeniden bağlanmada yeni portla oynat

    def on_lost(self, device_id: str) -> None:
        self.links.pop(device_id, None)
        self.on_status(device_id, "bağlantı koptu, yeniden bağlanılıyor…")
        self.refresh()

    def on_failed(self, device_id: str, message: str) -> None:
        dev = self.device(device_id)
        name = dev.name if dev else device_id
        self.links.pop(device_id, None)
        self.session_pw.pop(device_id, None)
        if self.show_after_connect == device_id:
            self.show_after_connect = None
        self.log(f"[{name}] HATA: {message}")
        self.on_status(device_id, "hata")
        self.refresh()
        QMessageBox.warning(self, f"{name} - bağlanılamadı", message)

    # --- ızgara ---------------------------------------------------------
    def show_device(self, device_id: str) -> None:
        dev = self.device(device_id)
        if dev is None:
            return
        self.leave_zoom()
        total = len(self.channel_names(dev))
        self.playlist = [Entry(device_id, ch) for ch in range(1, total + 1)]
        self.page = 0
        self.select_tile(0)
        self.refresh()

    def clear_view(self) -> None:
        self.leave_zoom()
        self.playlist = []
        self.page = 0
        self.refresh()

    def page_count(self) -> int:
        return max(1, -(-len(self.playlist) // self.count))

    def turn(self, step: int) -> None:
        if self.zoom_entry is not None:
            return
        self.page = (self.page + step) % self.page_count()
        self.refresh()

    def on_layout_changed(self) -> None:
        self.leave_zoom()
        first = self.page * self.count          # ilk görünen kanalı koru
        self.count = self.layout_box.currentData()
        self.page = first // self.count
        self.selected = 0
        self.apply_layout()

    def apply_layout(self) -> None:
        shown = 1 if self.zoom_entry is not None else self.count
        side = int(shown ** 0.5)
        for t in self.tiles:
            self.grid.removeWidget(t)
            t.setVisible(False)
        for r in range(MAX_TILES):
            self.grid.setRowStretch(r, 0)
            self.grid.setColumnStretch(r, 0)
        for i in range(shown):
            self.grid.addWidget(self.tiles[i], i // side, i % side)
            self.tiles[i].setVisible(True)
        for r in range(side):
            self.grid.setRowStretch(r, 1)
            self.grid.setColumnStretch(r, 1)
        self.refresh()

    def select_tile(self, index: int) -> None:
        self.selected = index
        for t in self.tiles:
            t.set_selected(t.index == index and self.zoom_entry is None)

    def toggle_zoom(self, index: int) -> None:
        if self.zoom_entry is not None:
            self.leave_zoom()
            return
        pos = self.page * self.count + index
        entry = self.playlist[pos] if pos < len(self.playlist) else None
        if entry is None or self.count == 1:
            return
        self.zoom_entry = entry
        self.select_tile(index)
        self.apply_layout()

    def leave_zoom(self) -> None:
        if self.zoom_entry is None:
            return
        self.zoom_entry = None
        self.select_tile(self.selected)
        self.apply_layout()

    def visible_entries(self) -> list[Entry | None]:
        if self.zoom_entry is not None:
            return [self.zoom_entry]
        start = self.page * self.count
        chunk = self.playlist[start:start + self.count]
        return chunk + [None] * (self.count - len(chunk))

    def refresh(self) -> None:
        """Görünen karoları oynatma listesiyle eşitler."""
        entries = self.visible_entries()
        main = len(entries) == 1
        started = 0
        for i, tile in enumerate(self.tiles):
            entry = entries[i] if i < len(entries) else None
            dev = self.device(entry.device_id) if entry else None
            link = self.links.get(entry.device_id) if entry else None
            if entry is None or dev is None:
                tile.stop()
                continue
            names = self.channel_names(dev)
            name = names[entry.channel - 1] if entry.channel <= len(names) \
                else f"Kanal {entry.channel}"
            title = f"{dev.name} · {entry.channel}. {name}"
            if link is None:
                tile.stop()
                tile.title.setText(f"{title} - cihaz bağlı değil")
                continue
            url = rtsp.live_url(link.host, entry.channel,
                                rtsp.MAIN if main else rtsp.SUB, link.port)
            if tile.play(url, dev.user, link.password, title, delay=started * 0.5):
                started += 1
        if self.zoom_entry is not None:
            self.page_label.setText(" tek kanal ")
        else:
            self.page_label.setText(f" {self.page + 1}/{self.page_count()} ")

    def tick(self) -> None:
        for tile in self.tiles:
            if tile.isVisible():
                tile.tick()

    def closeEvent(self, event) -> None:
        self.timer.stop()
        for tile in self.tiles:
            tile.stop()
        self.manager.shutdown()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
