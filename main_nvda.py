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
from PyQt6.QtGui import QFont, QTextCursor, QIcon
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

    def fetch_candles(self, symbol: str, granularity: str, limit: int = 1000, end_time: int = None) -> List[list]:
        params = {
            "symbol": symbol,
            "productType": "usdt-futures",
            "granularity": granularity,
            "limit": limit
        }
        if end_time:
            params["endTime"] = str(end_time)
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
        
        all_candles = []
        last_end_time = None
        target_cycles = 3
        
        print(f"[PYTHON] Rozpoczynam pobieranie ~3000 świec dla {symbol}...")
        for cycle in range(target_cycles):
            candles = self.fetch_candles(symbol, bitget_granularity, limit=1000, end_time=last_end_time)
            if not candles:
                break
            all_candles.extend(candles)
            
            timestamps = [int(c[0]) for c in candles]
            if not timestamps:
                break
                
            oldest_ts = min(timestamps)
            last_end_time = oldest_ts - 1
            
            if len(candles) < 1000:
                break
            time.sleep(0.1)
            
        if all_candles:
            parsed_history = []
            seen_times = set()
            for c in all_candles:
                raw_ts = int(c[0])
                clean_ts = int(raw_ts // 1000) if raw_ts > 10000000000 else int(raw_ts)
                if clean_ts in seen_times:
                    continue
                seen_times.add(clean_ts)
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
            print(f"[PYTHON] Pomyślnie załadowano {len(parsed_history)} świec do wykresu.")
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

    def __init__(self, watch_dir: str):
        super().__init__()
        self.observer = Observer()
        self.handler = FileChangeHandler(self.file_changed.emit)
        self.observer.schedule(self.handler, path=watch_dir, recursive=False)
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
        self.setWindowTitle("NVDA Conditioner")
        self.resize(1600, 950)

        self.base_dir = Path(__file__).resolve().parent
        self.schema_path = self.base_dir / "dane_json_nvda.json"
        print(f"[INFO] Ścieżka do JSON: {self.schema_path}")

        self.current_setup = None
        self.nvda_interval = "1m"
        self.nvda_ready = False
        self.schema_data = None

        self.reload_timer = QTimer()
        self.reload_timer.setSingleShot(True)
        self.reload_timer.timeout.connect(self.reset_and_load_schema)

        self.init_ui()

        self.bitget_client = BitgetFuturesClient()
        self.bitget_client.historical_data_ready.connect(self.on_historical_data)
        self.bitget_client.realtime_update_ready.connect(self.on_realtime_update)
        self.bitget_client.start()

        sciezka_ikony = self.base_dir / "nvidia_icon.svg"
        self.setWindowIcon(QIcon(str(sciezka_ikony)))

        self.file_watcher = SchemaWatcher(str(self.base_dir))
        self.file_watcher.file_changed.connect(self.on_file_changed)

    def init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QHBoxLayout(main_widget)
        main_layout.setContentsMargins(5, 5, 5, 5)

        # === GŁÓWNY SPLITTER POZIOMY (Lewy panel vs Wykresy) ===
        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_splitter.setStyleSheet("QSplitter::handle { background-color: #313244; }")

        # === LEWY PANEL ===
        left_panel = QFrame()
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

        # === WEWNĘTRZNY SPLITTER PIONOWY (Listy vs Opis) ===
        left_inner_splitter = QSplitter(Qt.Orientation.Vertical)
        left_inner_splitter.setStyleSheet("QSplitter::handle { background-color: #313244; }")

        # Górna część: Listy
        lists_widget = QWidget()
        lists_layout = QVBoxLayout(lists_widget)
        lists_layout.setContentsMargins(0, 0, 0, 0)

        lists_layout.addWidget(QLabel("Aktywne Poziomy i Analizy:", styleSheet="color: #bac2de; font-weight: bold;"))
        self.ranges_list = QListWidget()
        self.ranges_list.setStyleSheet("background-color: #181825; color: #cdd6f4; border: 1px solid #313244; border-radius: 4px;")
        self.ranges_list.itemChanged.connect(self.on_range_visibility_changed)
        self.ranges_list.itemClicked.connect(self.on_range_selected)
        lists_layout.addWidget(self.ranges_list)

        lists_layout.addWidget(QLabel("Dostępne Strategie NVDA (Setups):", styleSheet="color: #bac2de; font-weight: bold;"))
        self.setups_list = QListWidget()
        self.setups_list.setStyleSheet("background-color: #181825; color: #cdd6f4; border: 1px solid #313244; border-radius: 4px;")
        self.setups_list.itemChanged.connect(self.on_setup_visibility_changed)
        self.setups_list.itemClicked.connect(self.on_setup_selected)
        lists_layout.addWidget(self.setups_list)

        # Dolna część: Opis
        details_widget = QWidget()
        details_layout = QVBoxLayout(details_widget)
        details_layout.setContentsMargins(0, 0, 0, 0)

        details_layout.addWidget(QLabel("Komentarz i Wytyczne Strategii:", styleSheet="color: #bac2de; font-weight: bold;"))
        self.details_box = QTextEdit()
        self.details_box.setReadOnly(True)
        self.details_box.setStyleSheet("background-color: #11111b; color: #a6adc8; border: 1px solid #313244; border-radius: 4px; font-family: monospace;")
        details_layout.addWidget(self.details_box)

        left_inner_splitter.addWidget(lists_widget)
        left_inner_splitter.addWidget(details_widget)
        left_inner_splitter.setSizes([400, 300])

        left_layout.addWidget(left_inner_splitter, stretch=1)

        # === PRAWA STRONA: WYKRESY ===
        chart_splitter = QSplitter(Qt.Orientation.Vertical)
        chart_splitter.setStyleSheet("background-color: #11111b; QSplitter::handle { background-color: #313244; }")

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

        main_splitter.addWidget(left_panel)
        main_splitter.addWidget(chart_splitter)
        main_splitter.setSizes([420, 1180])

        main_layout.addWidget(main_splitter, stretch=1)

        # Ładowanie wykresów
        nvda_html_path = self.base_dir / "chart.html"
        self.nvda_chart_view.setUrl(QUrl.fromLocalFile(str(nvda_html_path)))
        self.nvda_chart_view.loadFinished.connect(self.on_nvda_chart_load_finished)

        nasdaq_html_content = self.generate_tradingview_html()
        self.nasdaq_chart_view.setHtml(nasdaq_html_content, QUrl("https://s3.tradingview.com"))

    def generate_tradingview_html(self) -> str:
        settings = {
            "symbol": "IG:NASDAQ",
            "interval": "1",
            "theme": "dark",
            "style": "1",
            "timezone": "Europe/Warsaw",
            "locale": "pl",
            "toolbar_bg": "#000000",
            "enable_publishing": False,
            "hide_side_toolbar": False,
            "allow_symbol_change": False,
            "save_image": False,
            "width": "100%",
            "height": "100%",
            "studies": [
                "VWAP@tv-basicstudies",
                "PivotPointsStandard@tv-basicstudies"
            ]
        }
        studies_overrides = {}
        settings_json = json.dumps(settings)
        overrides_json = json.dumps(studies_overrides)

        return f"""
        <!DOCTYPE html>
        <html style="margin: 0; padding: 0; width: 100%; height: 100%; overflow: hidden;">
        <head>
            <meta charset="utf-8">
            <style>
                html, body {{ margin: 0 !important; padding: 0 !important; width: 100% !important; height: 100% !important; background-color: #000000; overflow: hidden; }}
                #tv_chart_nasdaq {{ width: 100% !important; height: 100% !important; position: absolute; top: 0; left: 0; }}
            </style>
        </head>
        <body>
            <div id="tv_chart_nasdaq"></div>
            <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
            <script type="text/javascript">
                try {{
                    var config = {settings_json};
                    config.studies_overrides = {overrides_json};
                    config.container_id = "tv_chart_nasdaq";
                    new TradingView.widget(config);
                }} catch (e) {{
                    console.error("Błąd inicjalizacji wykresu TradingView:", e);
                }}
            </script>
        </body>
        </html>
        """

    def handle_interval_change(self, asset_id: str, new_interval: str):
        if asset_id == "NVDA":
            self.nvda_interval = new_interval
            self.nvda_label.setText(f"NVDAUSDT (Bitget Futures) - Interwał: {new_interval}")
            self.bitget_client.load_historical("NVDA", new_interval, "NVDA")

    def on_historical_data(self, asset_id: str, data_json: str, ticker_name: str):
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
        if asset_id == "NVDA" and self.nvda_ready:
            js_code = f"""
            if (typeof window.updateRealTimeBar === 'function') {{
                window.updateRealTimeBar(`{bar_json}`);
            }} else {{
                console.log('DEBUG_JS: updateRealTimeBar jeszcze nie jest gotowe.');
            }}
            """
            self.nvda_chart_view.page().runJavaScript(js_code)

    def reset_and_load_schema(self):
        print("[WATCHDOG] Wykryto zmianę pliku JSON. Resetowanie stanu do zera...")
        self.current_setup = None
        self.schema_data = None

        self.macro_label.setText("Sentyment: N/A | F&G: N/A")
        self.market_context_box.clear()
        self.details_box.clear()

        if self.nvda_ready:
            self.nvda_chart_view.page().runJavaScript("if(window.hideRangeLines){window.hideRangeLines();}")
            self.nvda_chart_view.page().runJavaScript("if(window.hideSetupLines){window.hideSetupLines();}")

        self.ranges_list.blockSignals(True)
        self.ranges_list.clear()
        self.ranges_list.blockSignals(False)

        self.setups_list.blockSignals(True)
        self.setups_list.clear()
        self.setups_list.blockSignals(False)

        self.load_schema()

    def load_schema(self):
        if not self.schema_path.exists():
            print(f"⚠️ [BŁĄD] Nie znaleziono pliku JSON pod ścieżką: {self.schema_path}")
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
        self.reload_timer.start(500)

    def update_market_context(self):
        nasdaq_data = self.schema_data.get("assets", {}).get("NASDAQ", {})
        if not nasdaq_data:
            self.market_context_box.setPlainText("Brak danych o NASDAQ w JSON.")
            self.market_context_box.moveCursor(QTextCursor.MoveOperation.Start)
            return

        text = f"Sentiment: {nasdaq_data.get('sentiment', 'N/A')}\n"
        text += f"Kluczowy poziom (Gatekeeper): {nasdaq_data.get('key_gatekeeper_level', 'N/A')}\n"
        
        analyses = nasdaq_data.get("analyses", [])
        if analyses:
            text += f"\n📌 {analyses[0]['name']}:\n{analyses[0]['description'][:120]}...\n"
            
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

        # Dodaj analizę NASDAQ jako pierwszy element
        nasdaq_data = self.schema_data.get("assets", {}).get("NASDAQ", {})
        nasdaq_analyses = nasdaq_data.get("analyses", [])
        if nasdaq_analyses:
            for a in nasdaq_analyses:
                item = QListWidgetItem(f"[NASDAQ] {a['name']}")
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked)
                item.setData(Qt.ItemDataRole.UserRole, {"type": "nasdaq_analysis", "data": a})
                self.ranges_list.addItem(item)

        # Dodaj poziomy NVDA
        nvda_ranges = self.schema_data.get("assets", {}).get("NVDA", {}).get("price_ranges", [])
        for r in nvda_ranges:
            item = QListWidgetItem(r['name'])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            if not checked_ids or r["id"] in checked_ids:
                item.setCheckState(Qt.CheckState.Checked)
            else:
                item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, {"type": "nvda_range", "data": r})
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

    def on_range_selected(self, item):
        role_data = item.data(Qt.ItemDataRole.UserRole)
        if not role_data:
            return
        
        item_type = role_data.get("type")
        data = role_data.get("data")
        
        if item_type == "nasdaq_analysis":
            self.display_nasdaq_analysis_details(data)
        elif item_type == "nvda_range":
            self.display_range_details(data)

    def on_setup_visibility_changed(self, item):
        self.redraw_setups()

    def on_setup_selected(self, item):
        setup = item.data(Qt.ItemDataRole.UserRole)
        self.current_setup = setup
        self.display_setup_details(setup)

    def redraw_ranges(self):
        if not self.nvda_ready:
            return
        self.nvda_chart_view.page().runJavaScript("if(window.hideRangeLines){window.hideRangeLines();}")
        for i in range(self.ranges_list.count()):
            item = self.ranges_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                role_data = item.data(Qt.ItemDataRole.UserRole)
                if role_data and role_data.get("type") == "nvda_range":
                    r = role_data.get("data")
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
                    "borderColor": "#f9e2af",
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

    def display_nasdaq_analysis_details(self, a: dict):
        text = f"=== ANALIZA NASDAQ: {a['name']} ===\n\n"
        text += f"{a.get('description', 'Brak opisu.')}\n"
        self.details_box.setPlainText(text)
        self.details_box.moveCursor(QTextCursor.MoveOperation.Start)

    def display_range_details(self, r: dict):
        text = f"=== POZIOM: {r['name']} ===\n"
        text += f"Ram czasowy (Timeframe): {r.get('timeframe', 'N/A')}\n"
        text += f"Strefa Wsparcia (Support): {r['support_zone']}\n"
        text += f"Strefa Oporu (Resistance): {r['resistance_zone']}\n\n"
        text += f"Opis i Kontekst:\n{r.get('description', 'Brak opisu.')}\n"
        self.details_box.setPlainText(text)
        self.details_box.moveCursor(QTextCursor.MoveOperation.Start)

    def display_setup_details(self, s: dict):
        cond = s.get("conditions", {})
        text = f"=== STRATEGIA: {s['name']} ===\n"
        text += f"Kierunek (Bias): {s['bias']}\n"
        text += f"NVDA Trigger Zone: {cond.get('trigger_zone')}\n"
        text += f"Trigger: {cond.get('trigger_condition')}\n"
        text += f"📊 Warunek konfirmacji NASDAQ:\n"
        text += f"  Cena: {cond.get('nasdaq_confirmation_price')}\n"
        text += f"  Warunek: {cond.get('nasdaq_confirmation_condition')}\n"
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
    window.showMaximized()
    sys.exit(app.exec())