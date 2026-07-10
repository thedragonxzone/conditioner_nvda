#!/usr/bin/env python3
"""
NVDA (Bitget) + NASDAQ (TradingView widget) Trading Assistant
- Panel boczny PO LEWEJ, wykresy PO PRAWEJ
- NVDA: lightweight-charts z danymi z Bitget + strefy/setupy z JSON
- NASDAQ: widget TradingView ładowany bezpośrednio przez HTTPS
"""
import json
import os
import sys
import time
import requests
import urllib.parse
from pathlib import Path
from typing import Dict, List
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QLabel, QListWidget, QListWidgetItem,
    QTextEdit, QFrame
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QObject, QTimer, QUrl
)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PyQt6.QtGui import QFont, QTextCursor
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

BITGET_INTERVAL_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H", "1d": "1D"
}


class BitgetFuturesClient(QThread):
    historical_data_ready = pyqtSignal(str, str, str)
    realtime_update_ready = pyqtSignal(str, str, str)

    def __init__(self):
        super().__init__()
        self.running = True
        self.monitored_assets = {
            "NVDA": {"ticker": "NVDAUSDT", "interval": "1m", "last_ts": 0}
        }
        self.base_url = "https://api.bitget.com/api/v2/mix/market/candles"

    def fetch_candles(self, symbol: str, granularity: str, limit: int = 1000) -> List[list]:
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
                raw_ts = int(c[0])
                clean_ts = int(raw_ts // 1000) if raw_ts > 10000000000 else int(raw_ts)
                
                parsed_history.append({
                    "time": clean_ts,
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5])
                })
            parsed_history.sort(key=lambda x: x["time"])
            self.monitored_assets[asset_id]["last_ts"] = parsed_history[-1]["time"]
            json_str = json.dumps(parsed_history)
            self.historical_data_ready.emit(asset_id, json_str, ticker_name)

    def run(self):
        while self.running:
            time.sleep(2)
            for asset_id, info in self.monitored_assets.items():
                bitget_granularity = BITGET_INTERVAL_MAP.get(info["interval"], "1m")
                candles = self.fetch_candles(info["ticker"], bitget_granularity, limit=2)
                if candles:
                    c = candles[0]
                    raw_ts = int(c[0])
                    clean_ts = int(raw_ts // 1000) if raw_ts > 10000000000 else int(raw_ts)
                    
                    last_recorded = self.monitored_assets[asset_id].get("last_ts", 0)
                    if last_recorded > 10000000000:
                        last_recorded = last_recorded // 1000

                    if clean_ts >= last_recorded:
                        bar = {
                            "time": clean_ts,
                            "open": float(c[1]),
                            "high": float(c[2]),
                            "low": float(c[3]),
                            "close": float(c[4]),
                            "volume": float(c[5])
                        }
                        self.monitored_assets[asset_id]["last_ts"] = clean_ts
                        self.realtime_update_ready.emit(asset_id, json.dumps(bar), info["ticker"])

    def stop(self):
        self.running = False


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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NVDA + NASDAQ Trading Assistant")
        self.resize(1600, 950)
        self.schema_path = "schemat_json_rezim.json"
        self.current_setup = None
        self.nvda_interval = "1m"
        self.nvda_ready = False
        self.schema_data = None
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

        # === LEWY PANEL ===
        left_panel = QFrame()
        left_panel.setFixedWidth(420)
        left_panel.setStyleSheet("background-color: #1e1e2e; border-right: 1px solid #313244;")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(10, 10, 10, 10)

        self.macro_label = QLabel("Sentyment: N/A")
        self.macro_label.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        self.macro_label.setStyleSheet("color: #cdd6f4;")
        left_layout.addWidget(self.macro_label)

        left_layout.addWidget(QLabel("Kontekst Rynkowy (NASDAQ):", styleSheet="color: #bac2de; font-weight: bold;"))
        self.market_context_box = QTextEdit()
        self.market_context_box.setReadOnly(True)
        self.market_context_box.setMaximumHeight(120)
        self.market_context_box.setStyleSheet("background-color: #181825; color: #a6adc8; border: 1px solid #313244; border-radius: 4px; font-size: 11px;")
        left_layout.addWidget(self.market_context_box)

        left_layout.addWidget(QLabel("Aktywne Poziomy NVDA (Ranges):", styleSheet="color: #bac2de; font-weight: bold;"))
        self.ranges_list = QListWidget()
        self.ranges_list.setStyleSheet("background-color: #181825; color: #cdd6f4; border: 1px solid #313244; border-radius: 4px;")
        self.ranges_list.itemChanged.connect(self.on_range_visibility_changed)
        left_layout.addWidget(self.ranges_list)

        left_layout.addWidget(QLabel("Dostępne Strategie NVDA (Setups):", styleSheet="color: #bac2de; font-weight: bold;"))
        self.setups_list = QListWidget()
        self.setups_list.setStyleSheet("background-color: #181825; color: #cdd6f4; border: 1px solid #313244; border-radius: 4px;")
        self.setups_list.itemChanged.connect(self.on_setup_visibility_changed)
        self.setups_list.itemClicked.connect(self.on_setup_selected)
        left_layout.addWidget(self.setups_list)

        left_layout.addWidget(QLabel("Komentarz i Wytyczne Strategii:", styleSheet="color: #bac2de; font-weight: bold;"))
        self.details_box = QTextEdit()
        self.details_box.setReadOnly(True)
        self.details_box.setStyleSheet("background-color: #11111b; color: #a6adc8; border: 1px solid #313244; border-radius: 4px; font-family: monospace;")
        left_layout.addWidget(self.details_box)

        # === PRAWA STRONA: WYKRESY ===
        chart_splitter = QSplitter(Qt.Orientation.Vertical)
        chart_splitter.setStyleSheet("background-color: #11111b;")

        # NVDA
        self.nvda_container = QWidget()
        nvda_lay = QVBoxLayout(self.nvda_container)
        nvda_lay.setContentsMargins(0, 0, 0, 0)
        nvda_lay.setSpacing(0)
        
        self.nvda_label = QLabel("NVDAUSDT (Bitget Futures) - Interwał: 1m")
        self.nvda_label.setStyleSheet("background-color: #11111b; color: #a6adc8; padding: 6px; font-weight: bold;")
        self.nvda_label.setFixedHeight(28)
        
        self.nvda_chart_view = QWebEngineView()
        self.nvda_page = WebEnginePageCustom(self, "NVDA")
        self.nvda_chart_view.setPage(self.nvda_page)
        
        nvda_lay.addWidget(self.nvda_label)
        nvda_lay.addWidget(self.nvda_chart_view, stretch=1)

        # NASDAQ
        self.nasdaq_container = QWidget()
        nasdaq_lay = QVBoxLayout(self.nasdaq_container)
        nasdaq_lay.setContentsMargins(0, 0, 0, 0)
        nasdaq_lay.setSpacing(0)
        
        self.nasdaq_label = QLabel("NASDAQ-100 (TradingView CFD) - Podgląd rynku")
        self.nasdaq_label.setStyleSheet("background-color: #11111b; color: #a6adc8; padding: 6px; font-weight: bold;")
        self.nasdaq_label.setFixedHeight(28)
        
        self.nasdaq_chart_view = QWebEngineView()
        
        nasdaq_lay.addWidget(self.nasdaq_label)
        nasdaq_lay.addWidget(self.nasdaq_chart_view, stretch=1)

        chart_splitter.addWidget(self.nvda_container)
        chart_splitter.addWidget(self.nasdaq_container)
        chart_splitter.setCollapsible(0, False)
        chart_splitter.setCollapsible(1, False)
        chart_splitter.setSizes([450, 450])

        main_layout.addWidget(left_panel, stretch=0)
        main_layout.addWidget(chart_splitter, stretch=1)

        # Ładowanie wykresów
        nvda_html_path = os.path.abspath("chart.html")
        self.nvda_chart_view.setUrl(QUrl.fromLocalFile(nvda_html_path))
        self.nvda_chart_view.loadFinished.connect(self.on_nvda_chart_load_finished)

        nasdaq_tv_url = self.generate_tradingview_url()
        self.nasdaq_chart_view.setUrl(QUrl(nasdaq_tv_url))

    def generate_tradingview_url(self) -> str:
        base_url = "https://s.tradingview.com/widgetembed/"
        
        disabled_features = [
            "show_right_widgets_panel_by_default", "right_toolbar", "widget_logo",
            "use_localstorage_for_settings", "symbol_info_price_source",
            "symbol_info_long_description", "symbol_info_fundamentals",
            "show_symbol_info_panel", "symbol_info_dialog"
        ]
        
        query_params = {
            "hideideas": "1",
            "backgroundColor": "#000000",
            "overrides": json.dumps({
                "paneProperties.background": "#000000",
                "paneProperties.backgroundType": "solid",
                "paneProperties.vertGridProperties.color": "rgba(42, 46, 57, 0.2)",
                "paneProperties.horzGridProperties.color": "rgba(42, 46, 57, 0.2)"
            }),
            "enabled_features": "[]",
            "disabled_features": json.dumps(disabled_features),
            "locale": "pl"
        }
        
        settings = {
            "symbol": "IG:NASDAQ",
            "frameElementId": "tv_chart_nasdaq",
            "interval": "5",
            "hide_legend": "0",
            "hide_side_toolbar": "0",
            "allow_symbol_change": "0",
            "save_image": "0",
            "studies": "VWAP@tv-basicstudies",
            "theme": "dark",
            "style": "1",
            "timezone": "Europe/Warsaw",
            "backgroundColor": "#000000"
        }
        
        encoded_params = urllib.parse.urlencode(query_params)
        encoded_settings = urllib.parse.quote(json.dumps(settings))
        return f"{base_url}?{encoded_params}#{encoded_settings}"

    def handle_interval_change(self, asset_id: str, new_interval: str):
        if asset_id == "NVDA":
            self.nvda_interval = new_interval
            self.nvda_label.setText(f"NVDAUSDT (Bitget Futures) - Interwał: {new_interval}")
            self.bitget_client.load_historical("NVDA", new_interval, "NVDA")

    def on_historical_data(self, asset_id: str, data_json: str, ticker_name: str):
        """Bezpieczne wstrzyknięcie gotowego stringa JSON przy użyciu backticków"""
        if asset_id == "NVDA" and self.nvda_ready:
            js_code = f"""
            if (typeof window.loadHistoricalData === 'function') {{
                window.loadHistoricalData(`{data_json}`);
            }} else {{
                console.log('DEBUG_JS: loadHistoricalData jeszcze nie jest gotowe.');
            }}
            """
            self.nvda_chart_view.page().runJavaScript(js_code)

    def on_realtime_update(self, asset_id: str, bar_json: str, ticker_name: str):
        """Bezpieczne wstrzyknięcie gotowej świecy real-time przy użyciu backticków"""
        if asset_id == "NVDA" and self.nvda_ready:
            js_code = f"""
            if (typeof window.updateRealTimeBar === 'function') {{
                window.updateRealTimeBar(`{bar_json}`);
            }} else {{
                console.log('DEBUG_JS: updateRealTimeBar jeszcze nie jest gotowe.');
            }}
            """
            self.nvda_chart_view.page().runJavaScript(js_code)

    def load_schema(self):
        if not os.path.exists(self.schema_path):
            return
        try:
            with open(self.schema_path, "r", encoding="utf-8") as f:
                self.schema_data = json.load(f)
            env = self.schema_data.get("macro_environment", {})
            self.macro_label.setText(f"Sentyment: {env.get('market_sentiment','N/A').upper()} | F&G: {env.get('fear_and_greed_index','N/A')}")
            self.update_market_context()
            self.update_ranges_list()
            self.update_setups_list()
        except Exception as e:
            print(f"Error loading schema: {e}")

    def on_file_changed(self):
        QTimer.singleShot(500, self.load_schema)

    def update_market_context(self):
        nasdaq_data = self.schema_data.get("assets", {}).get("NASDAQ", {})
        if not nasdaq_data:
            self.market_context_box.setPlainText("Brak danych o NASDAQ w JSON.")
            self.market_context_box.moveCursor(QTextCursor.MoveOperation.Start)
            return

        text = f"Aktualna cena: {nasdaq_data.get('current_price', 'N/A')}\n"
        text += f"Sentiment: {nasdaq_data.get('sentiment', 'N/A')}\n"
        text += f"Key Gatekeeper: {nasdaq_data.get('key_gatekeeper_level', 'N/A')}\n\n"

        ranges = nasdaq_data.get("price_ranges", [])
        if ranges:
            r = ranges[0]
            text += f"📌 {r['name']}\n"
            text += f"Support: {r['support_zone']}\n"
            text += f"Resistance: {r['resistance_zone']}\n"
        
        self.market_context_box.setPlainText(text)
        self.market_context_box.moveCursor(QTextCursor.MoveOperation.Start)

    def update_ranges_list(self):
        self.ranges_list.blockSignals(True)
        checked_ids = [
            self.ranges_list.item(i).data(Qt.ItemDataRole.UserRole)["id"]
            for i in range(self.ranges_list.count())
            if self.ranges_list.item(i).checkState() == Qt.CheckState.Checked
        ]
        self.ranges_list.clear()

        nvda_ranges = self.schema_data.get("assets", {}).get("NVDA", {}).get("price_ranges", [])
        for r in nvda_ranges:
            item = QListWidgetItem(r['name'])
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
        checked_ids = [
            self.setups_list.item(i).data(Qt.ItemDataRole.UserRole)["id"]
            for i in range(self.setups_list.count())
            if self.setups_list.item(i).checkState() == Qt.CheckState.Checked
        ]
        current_row = self.setups_list.currentRow()
        self.setups_list.clear()

        setups = self.schema_data.get("assets", {}).get("NVDA", {}).get("setups", [])
        for s in setups:
            item = QListWidgetItem(f"{s['name']} ({s['bias']})")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            if not checked_ids or s["id"] in checked_ids:
                item.setCheckState(Qt.CheckState.Checked)
            else:
                item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, s)
            self.setups_list.addItem(item)

        if 0 <= current_row < self.setups_list.count():
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
        """Bezpieczne przekazanie stref za pomocą jednego zrzutu do JSON"""
        if not self.nvda_ready:
            return
        self.nvda_chart_view.page().runJavaScript("if(window.hideRangeLines){window.hideRangeLines();}")
        for i in range(self.ranges_list.count()):
            item = self.ranges_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                r = item.data(Qt.ItemDataRole.UserRole)
                zone_info = [
                    {
                        "top": r["resistance_zone"][1],
                        "bottom": r["resistance_zone"][0],
                        "borderColor": r.get("color", "#f38ba8"),
                        "label": f"RES: {r['name']}"
                    },
                    {
                        "top": r["support_zone"][1],
                        "bottom": r["support_zone"][0],
                        "borderColor": r.get("color", "#a6e3a1"),
                        "label": f"SUP: {r['name']}"
                    }
                ]
                json_str = json.dumps(zone_info)
                js_command = f"""
                if(window.showRangeLines){{
                    window.showRangeLines(`{json_str}`);
                }}
                """
                self.nvda_chart_view.page().runJavaScript(js_command)

    def redraw_setups(self):
        """Bezpieczne przekazanie linii setupów za pomocą jednego zrzutu do JSON"""
        if not self.nvda_ready:
            return
        self.nvda_chart_view.page().runJavaScript("if(window.hideSetupLines){window.hideSetupLines();}")
        for i in range(self.setups_list.count()):
            item = self.setups_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                s = item.data(Qt.ItemDataRole.UserRole)
                lines = []
                lines.append({
                    "top": s["execution"]["entry_zone"][1],
                    "bottom": s["execution"]["entry_zone"][0],
                    "borderColor": "#89b4fa",
                    "label": f"ENTRY ({s['id']})"
                })
                lines.append({
                    "top": s["execution"]["stop_loss_zone"][1],
                    "bottom": s["execution"]["stop_loss_zone"][0],
                    "borderColor": "#f38ba8",
                    "label": "SL"
                })
                for idx, tp in enumerate(s["execution"]["take_profit_zones"]):
                    lines.append({
                        "top": tp[1],
                        "bottom": tp[0],
                        "borderColor": "#a6e3a1",
                        "label": f"TP{idx+1}"
                    })
                json_str = json.dumps(lines)
                js_command = f"""
                if(window.showSetupLines){{
                    window.showSetupLines(`{json_str}`);
                }}
                """
                self.nvda_chart_view.page().runJavaScript(js_command)

    def display_setup_details(self, s: dict):
        cond = s.get("conditions", {})
        text = f"=== STRATEGIA: {s['name']} ===\n"
        text += f"Kierunek (Bias): {s['bias']}\n"
        text += f"NVDA Trigger Zone: {cond.get('trigger_zone')}\n"
        text += f"Trigger: {cond.get('trigger_condition')}\n\n"
        text += f"📊 Warunek konfirmacji NASDAQ:\n"
        text += f"  Cena: {cond.get('nasdaq_confirmation_price')}\n"
        text += f"  Warunek: {cond.get('nasdaq_confirmation_condition')}\n\n"
        text += f"Wytyczne wykonania pozycji:\n"
        text += f" - Entry: {s['execution']['entry_zone']}\n"
        text += f" - Stop Loss: {s['execution']['stop_loss_zone']}\n"
        text += f" - Targety TP: {s['execution']['take_profit_zones']}\n\n"
        text += f"Komentarz teoretyczny:\n{s.get('commentary','')}\n"
        self.details_box.setPlainText(text)
        self.details_box.moveCursor(QTextCursor.MoveOperation.Start)

    def on_nvda_chart_load_finished(self, ok):
        if ok:
            self.nvda_ready = True
            QTimer.singleShot(500, lambda: self.bitget_client.load_historical("NVDA", self.nvda_interval, "NVDA"))
            self.load_schema()

    def closeEvent(self, event):
        self.bitget_client.stop()
        self.file_watcher.stop()
        event.accept()


if __name__ == "__main__":
    os.environ["QTWEBENGINE_REMOTE_DEBUGGING"] = "9222"
    app = QApplication(sys.argv)
    QWebEngineProfile.defaultProfile().setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())