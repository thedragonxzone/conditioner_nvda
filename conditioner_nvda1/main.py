#!/usr/bin/env python3
"""
NVDA & NASDAQ Trading Assistant - Stabilna wersja z poprawnym sortowaniem chronologicznym
"""
import json
import os
import sys
import time
import requests
from pathlib import Path
from typing import Dict, List, Optional, Any
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QLabel, QCheckBox, QListWidget, QListWidgetItem,
    QTextEdit, QFrame
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QObject, QTimer, QUrl
)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEnginePage
from PyQt6.QtGui import QColor, QFont
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

BITGET_INTERVAL_MAP = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1H",
    "1d": "1D"
}

class BitgetFuturesClient(QThread):
    historical_data_ready = pyqtSignal(str, str, str)
    realtime_update_ready = pyqtSignal(str, str, str)

    def __init__(self):
        super().__init__()
        self.running = True
        self.monitored_assets = {
            "NVDA": {"ticker": "NVDAUSDT", "interval": "1m", "last_ts": 0},
            "NASDAQ": {"ticker": "NDX100USDT", "interval": "1m", "last_ts": 0}
        }
        self.base_url = "https://api.bitget.com/api/v2/mix/market/candles"

    def fetch_candles(self, symbol: str, granularity: str, limit: int = 500) -> List[list]:
        params = {
            "symbol": symbol,
            "productType": "usdt-futures",
            "granularity": granularity,
            "limit": limit
        }
        try:
            res = requests.get(self.base_url, params=params, timeout=10)
            if res.status_code == 200:
                data = res.json()
                if data.get("code") == "00000" and "data" in data:
                    return data["data"]
            print(f"[PYTHON ERROR] Bitget API error dla {symbol}: {res.text}")
        except Exception as e:
            print(f"[PYTHON EXCEPTION] Błąd pobierania {symbol}: {e}")
        return []

    def load_historical(self, asset_id: str, ui_interval: str, ticker_name: str):
        bitget_granularity = BITGET_INTERVAL_MAP.get(ui_interval, "1m")
        self.monitored_assets[asset_id]["interval"] = ui_interval
        symbol = self.monitored_assets[asset_id]["ticker"]
        
        candles = self.fetch_candles(symbol, bitget_granularity, limit=500)
        if candles:
            parsed_history = []
            for c in candles:
                parsed_history.append({
                    "time": int(c[0]),
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5])
                })
            
            # KLUCZOWE: Sortowanie chronologiczne od najstarszej do najnowszej świecy (rosnąco po czasie)
            parsed_history.sort(key=lambda x: x["time"])
            
            self.monitored_assets[asset_id]["last_ts"] = parsed_history[-1]["time"]
            json_str = json.dumps(parsed_history)
            self.historical_data_ready.emit(asset_id, json_str, ticker_name)

    def run(self):
        while self.running:
            time.sleep(2)
            for asset_id, info in self.monitored_assets.items():
                bitget_granularity = BITGET_INTERVAL_MAP.get(info["interval"], "1m")
                candles = self.fetch_candles(info["ticker"], bitget_granularity, limit=5)
                if candles:
                    c = candles[0] # Najnowsza świeca z początku listy
                    raw_ts = int(c[0])
                    
                    # Konwersja ms na sekundy bezpośrednio w Pythonie, by uniknąć rozbieżności typów
                    if raw_ts > 10000000000:
                        clean_ts = int(raw_ts // 1000)
                    else:
                        clean_ts = int(raw_ts)

                    bar = {
                        "time": clean_ts,
                        "open": float(c[1]),
                        "high": float(c[2]),
                        "low": float(c[3]),
                        "close": float(c[4]),
                        "volume": float(c[5])
                    }
                    self.realtime_update_ready.emit(asset_id, json.dumps(bar), info["ticker"])

class FileChangeHandler(FileSystemEventHandler):
    def __init__(self, callback):
        self.callback = callback
    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith('.json'):
            self.callback()

class SchemaWatcher(QObject):
    file_changed = pyqtSignal()
    def __init__(self, file_path: str):
        super().__init__()
        self.file_path = file_path
        self.observer = Observer()
        self.handler = FileChangeHandler(self.file_changed.emit)
        self.observer.schedule(self.handler, path=str(Path(file_path).parent), recursive=False)
        self.observer.start()
    def stop(self):
        self.observer.stop()
        self.observer.join()

class WebEnginePageCustom(QWebEnginePage):
    def __init__(self, parent, asset_id):
        super().__init__(parent)
        self.parent_win = parent
        self.asset_id = asset_id

    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        if "DEBUG_JS" in message or "Error" in message or "TypeError" in message:
            print(f"[JS CONSOLE] {self.asset_id}: {message}")
            
        if "INTERVAL_SWITCH:" in message:
            new_interval = message.replace("INTERVAL_SWITCH:", "").strip()
            self.parent_win.handle_interval_change(self.asset_id, new_interval)
        elif "VISIBLE_RANGE_CHANGED:" in message:
            if self.parent_win.sync_checkbox.isChecked():
                range_json = message.replace("VISIBLE_RANGE_CHANGED:", "").strip()
                self.parent_win.handle_range_sync(self.asset_id, range_json)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Bitget Multi-Chart Assistant")
        self.resize(1600, 950)

        self.schema_path = "schemat_json_rezim.json"
        self.current_setup = None
        self.nvda_interval = "1m"
        self.nasdaq_interval = "1m"
        
        # Flagi sprawdzające, czy strony HTML są już w pełni gotowe
        self.nvda_ready = False
        self.nasdaq_ready = False

        self.init_ui()
        
        self.bitget_client = BitgetFuturesClient()
        self.bitget_client.historical_data_ready.connect(self.on_historical_data)
        self.bitget_client.realtime_update_ready.connect(self.on_realtime_update)
        self.bitget_client.start()

        self.file_watcher = SchemaWatcher(self.schema_path)
        self.file_watcher.file_changed.connect(self.on_file_changed)

    def init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QHBoxLayout(main_widget)
        main_layout.setContentsMargins(5, 5, 5, 5)

        chart_splitter = QSplitter(Qt.Orientation.Vertical)
        
        self.nvda_container = QWidget()
        nvda_lay = QVBoxLayout(self.nvda_container)
        nvda_lay.setContentsMargins(0, 0, 0, 0)
        self.nvda_label = QLabel("NVDAUSDT (Bitget Futures) - Interwał: 1m")
        self.nvda_label.setStyleSheet("background-color: #11111b; color: #a6adc8; padding: 4px; font-weight: bold;")
        self.nvda_chart_view = QWebEngineView()
        self.nvda_page = WebEnginePageCustom(self, "NVDA")
        self.nvda_chart_view.setPage(self.nvda_page)
        nvda_lay.addWidget(self.nvda_label)
        nvda_lay.addWidget(self.nvda_chart_view)
        chart_splitter.addWidget(self.nvda_container)

        self.nasdaq_container = QWidget()
        nasdaq_lay = QVBoxLayout(self.nasdaq_container)
        nasdaq_lay.setContentsMargins(0, 0, 0, 0)
        self.nasdaq_label = QLabel("NDX100USDT (Bitget Futures) - Interwał: 1m")
        self.nasdaq_label.setStyleSheet("background-color: #11111b; color: #a6adc8; padding: 4px; font-weight: bold;")
        self.nasdaq_chart_view = QWebEngineView()
        self.nasdaq_page = WebEnginePageCustom(self, "NASDAQ")
        self.nasdaq_chart_view.setPage(self.nasdaq_page)
        nasdaq_lay.addWidget(self.nasdaq_label)
        nasdaq_lay.addWidget(self.nasdaq_chart_view)
        chart_splitter.addWidget(self.nasdaq_container)

        chart_splitter.setSizes([475, 475])

        right_panel = QFrame()
        right_panel.setFixedWidth(420)
        right_panel.setStyleSheet("background-color: #1e1e2e; border-left: 1px solid #313244;")
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(10, 10, 10, 10)

        top_ctrl_layout = QHBoxLayout()
        self.macro_label = QLabel("Sentyment: N/A")
        self.macro_label.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        self.macro_label.setStyleSheet("color: #cdd6f4;")
        self.sync_checkbox = QCheckBox("Synchro Czasu")
        self.sync_checkbox.setChecked(True)
        self.sync_checkbox.setStyleSheet("color: #a6adc8; font-weight: bold;")
        top_ctrl_layout.addWidget(self.macro_label)
        top_ctrl_layout.addWidget(self.sync_checkbox)
        right_layout.addLayout(top_ctrl_layout)

        right_layout.addWidget(QLabel("Aktywne Poziomy / Zakresy (Ranges):", styleSheet="color: #bac2de; font-weight: bold;"))
        self.ranges_list = QListWidget()
        self.ranges_list.setStyleSheet("background-color: #181825; color: #cdd6f4; border: 1px solid #313244; border-radius: 4px;")
        self.ranges_list.itemChanged.connect(self.on_range_visibility_changed)
        right_layout.addWidget(self.ranges_list)

        right_layout.addWidget(QLabel("Dostępne Strategie / Setups:", styleSheet="color: #bac2de; font-weight: bold;"))
        self.setups_list = QListWidget()
        self.setups_list.setStyleSheet("background-color: #181825; color: #cdd6f4; border: 1px solid #313244; border-radius: 4px;")
        self.setups_list.itemChanged.connect(self.on_setup_visibility_changed)
        self.setups_list.itemClicked.connect(self.on_setup_selected)
        right_layout.addWidget(self.setups_list)

        right_layout.addWidget(QLabel("Komentarz i Wytyczne Strategii:", styleSheet="color: #bac2de; font-weight: bold;"))
        self.details_box = QTextEdit()
        self.details_box.setReadOnly(True)
        self.details_box.setStyleSheet("background-color: #11111b; color: #a6adc8; border: 1px solid #313244; border-radius: 4px; font-family: monospace;")
        right_layout.addWidget(self.details_box)

        main_layout.addWidget(chart_splitter, stretch=3)
        main_layout.addWidget(right_panel, stretch=1)

        html_path = os.path.abspath("chart.html")
        self.nvda_chart_view.setUrl(QUrl.fromLocalFile(html_path))
        self.nasdaq_chart_view.setUrl(QUrl.fromLocalFile(html_path))

        self.nvda_chart_view.loadFinished.connect(self.on_nvda_chart_load_finished)
        self.nasdaq_chart_view.loadFinished.connect(self.on_nasdaq_chart_load_finished)

    def handle_interval_change(self, asset_id: str, new_interval: str):
        if asset_id == "NVDA":
            self.nvda_interval = new_interval
            self.nvda_label.setText(f"NVDAUSDT (Bitget Futures) - Interwał: {new_interval}")
            self.bitget_client.load_historical("NVDA", new_interval, "NVDA")
        else:
            self.nasdaq_interval = new_interval
            self.nasdaq_label.setText(f"NDX100USDT (Bitget Futures) - Interwał: {new_interval}")
            self.bitget_client.load_historical("NASDAQ", new_interval, "NASDAQ")

    def handle_range_sync(self, source_asset_id: str, range_json: str):
        try:
            vrange = json.loads(range_json)
            js_code = f"if(window.setVisibleRangeNoEvent) {{ window.setVisibleRangeNoEvent({vrange['from']}, {vrange['to']}); }}"
            if source_asset_id == "NVDA" and self.nasdaq_ready:
                self.nasdaq_chart_view.page().runJavaScript(js_code)
            elif source_asset_id == "NASDAQ" and self.nvda_ready:
                self.nvda_chart_view.page().runJavaScript(js_code)
        except Exception as e:
            print(f"Sync range error: {e}")

    def on_historical_data(self, asset_id: str, data_json: str, ticker_name: str):
        js_code = f"window.loadHistoricalData(`{data_json}`);"
        if asset_id == "NVDA" and self.nvda_ready:
            self.nvda_chart_view.page().runJavaScript(js_code)
        elif asset_id == "NASDAQ" and self.nasdaq_ready:
            self.nasdaq_chart_view.page().runJavaScript(js_code)

    def on_realtime_update(self, asset_id: str, bar_json: str, ticker_name: str):
        js_code = f"window.updateRealTimeBar(`{bar_json}`);"
        if asset_id == "NVDA" and self.nvda_ready:
            self.nvda_chart_view.page().runJavaScript(js_code)
        elif asset_id == "NASDAQ" and self.nasdaq_ready:
            self.nasdaq_chart_view.page().runJavaScript(js_code)

    def load_schema(self):
        if not os.path.exists(self.schema_path):
            return
        try:
            with open(self.schema_path, "r", encoding="utf-8") as f:
                self.schema_data = json.load(f)
            
            env = self.schema_data.get("macro_environment", {})
            self.macro_label.setText(f"Sentyment: {env.get('market_sentiment','N/A').upper()} | F&G: {env.get('fear_and_greed_index','N/A')}")
            
            self.update_ranges_list()
            self.update_setups_list()
        except Exception as e:
            print(f"Error loading schema: {e}")

    def on_file_changed(self):
        QTimer.singleShot(500, self.load_schema)

    def update_ranges_list(self):
        self.ranges_list.blockSignals(True)
        checked_ids = [self.ranges_list.item(i).data(Qt.ItemDataRole.UserRole)["id"] for i in range(self.ranges_list.count()) if self.ranges_list.item(i).checkState() == Qt.CheckState.Checked]
        self.ranges_list.clear()
        
        all_ranges = []
        if "NASDAQ" in self.schema_data["assets"]:
            for r in self.schema_data["assets"]["NASDAQ"].get("price_ranges", []):
                r["_asset"] = "NASDAQ"
                all_ranges.append(r)
        if "NVDA" in self.schema_data["assets"]:
            for r in self.schema_data["assets"]["NVDA"].get("price_ranges", []):
                r["_asset"] = "NVDA"
                all_ranges.append(r)

        for r in all_ranges:
            item = QListWidgetItem(f"[{r['_asset']}] {r['name']}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            if not checked_ids or r["id"] in checked_ids:
                item.setCheckState(Qt.CheckState.Checked)
            else:
                item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, r)
            self.ranges_list.addItem(item)
        self.ranges_list.blockSignals(False)
        self.redraw_ranges()

    def update_setups_list(self):
        self.setups_list.blockSignals(True)
        checked_ids = [self.setups_list.item(i).data(Qt.ItemDataRole.UserRole)["id"] for i in range(self.setups_list.count()) if self.setups_list.item(i).checkState() == Qt.CheckState.Checked]
        current_row = self.setups_list.currentRow()
        self.setups_list.clear()

        setups = self.schema_data["assets"]["NVDA"].get("setups", [])
        for s in setups:
            item = QListWidgetItem(f"Setup: {s['name']} ({s['bias']})")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            if not checked_ids or s["id"] in checked_ids:
                item.setCheckState(Qt.CheckState.Checked)
            else:
                item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, s)
            self.setups_list.addItem(item)
            
        if current_row >= 0 and current_row < self.setups_list.count():
            self.setups_list.setCurrentRow(current_row)
        self.setups_list.blockSignals(False)
        self.redraw_setups()

    def on_range_visibility_changed(self, item):
        self.redraw_ranges()

    def on_setup_visibility_changed(self, item):
        self.redraw_setups()

    def on_setup_selected(self, item):
        setup = item.data(Qt.ItemDataRole.UserRole)
        self.current_setup = setup
        self.display_setup_details(setup)

    def redraw_ranges(self):
        if self.nvda_ready: self.nvda_chart_view.page().runJavaScript("if(window.hideRangeLines){window.hideRangeLines();}")
        if self.nasdaq_ready: self.nasdaq_chart_view.page().runJavaScript("if(window.hideRangeLines){window.hideRangeLines();}")

        for i in range(self.ranges_list.count()):
            item = self.ranges_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                r = item.data(Qt.ItemDataRole.UserRole)
                zone_info = [{
                    "top": r["resistance_zone"][1],
                    "bottom": r["resistance_zone"][0],
                    "borderColor": r.get("color", "#f38ba8"),
                    "label": f"RES: {r['name']}"
                }, {
                    "top": r["support_zone"][1],
                    "bottom": r["support_zone"][0],
                    "borderColor": r.get("color", "#a6e3a1"),
                    "label": f"SUP: {r['name']}"
                }]
                js_command = f"if(window.showRangeLines){{window.showRangeLines('{json.dumps(zone_info)}');}}"
                if r["_asset"] == "NVDA" and self.nvda_ready:
                    self.nvda_chart_view.page().runJavaScript(js_command)
                elif r["_asset"] == "NASDAQ" and self.nasdaq_ready:
                    self.nasdaq_chart_view.page().runJavaScript(js_command)

    def redraw_setups(self):
        if self.nvda_ready: self.nvda_chart_view.page().runJavaScript("if(window.hideSetupLines){window.hideSetupLines();}")
        for i in range(self.setups_list.count()):
            item = self.setups_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                s = item.data(Qt.ItemDataRole.UserRole)
                lines = []
                lines.append({"top": s["execution"]["entry_zone"][1], "bottom": s["execution"]["entry_zone"][0], "borderColor": "#89b4fa", "label": f"ENTRY ({s['id']})"})
                lines.append({"top": s["execution"]["stop_loss_zone"][1], "bottom": s["execution"]["stop_loss_zone"][0], "borderColor": "#f38ba8", "label": "SL"})
                for idx, tp in enumerate(s["execution"]["take_profit_zones"]):
                    lines.append({"top": tp[1], "bottom": tp[0], "borderColor": "#a6e3a1", "label": f"TP{idx+1}"})
                
                js_command = f"if(window.showSetupLines){{window.showSetupLines('{json.dumps(lines)}');}}"
                if self.nvda_ready:
                    self.nvda_chart_view.page().runJavaScript(js_command)

    def display_setup_details(self, s: dict):
        cond = s.get("conditions", {})
        text = f"=== STRATEGIA: {s['name']} ===\n"
        text += f"Kierunek (Bias): {s['bias']}\n"
        text += f"NVDA Trigger Zone: {cond.get('trigger_zone')}\n"
        text += f"NASDAQ Konfirmacja: {cond.get('nasdaq_confirmation_condition')} ({cond.get('nasdaq_confirmation_price')})\n\n"
        text += f"Wytyczne wykonania pozycji:\n"
        text += f" - Entry: {s['execution']['entry_zone']}\n"
        text += f" - Stop Loss: {s['execution']['stop_loss_zone']}\n"
        text += f" - Targety TP: {s['execution']['take_profit_zones']}\n\n"
        text += f"Komentarz teoretyczny:\n{s.get('commentary','')}\n"
        self.details_box.setPlainText(text)

    def on_nvda_chart_load_finished(self, ok):
        if ok:
            self.nvda_ready = True
            inject_hook = "window.pyWebViewHook = function(val) { console.log('INTERVAL_SWITCH:' + val); };"
            self.nvda_chart_view.page().runJavaScript(inject_hook)
            self.bitget_client.load_historical("NVDA", self.nvda_interval, "NVDA")
            self.load_schema()

    def on_nasdaq_chart_load_finished(self, ok):
        if ok:
            self.nasdaq_ready = True
            inject_hook = "window.pyWebViewHook = function(val) { console.log('INTERVAL_SWITCH:' + val); };"
            self.nasdaq_chart_view.page().runJavaScript(inject_hook)
            self.bitget_client.load_historical("NASDAQ", self.nasdaq_interval, "NASDAQ")
            self.load_schema()

    def closeEvent(self, event):
        self.bitget_client.stop()
        self.file_watcher.stop()
        event.accept()

if __name__ == "__main__":
    os.environ["QTWEBENGINE_REMOTE_DEBUGGING"] = "9222"
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())