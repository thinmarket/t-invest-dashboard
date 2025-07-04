import sys
from PyQt5.QtWidgets import (QApplication, QWidget, QTableWidget, QTableWidgetItem,
                            QVBoxLayout, QLabel, QHeaderView, QShortcut, QLineEdit, QPushButton, QHBoxLayout, QAbstractItemView, QStyledItemDelegate)
from PyQt5.QtCore import Qt, pyqtSignal, QObject, QTimer, pyqtSlot
from PyQt5.QtGui import QColor, QKeySequence, QFont, QBrush, QPainter
import threading
from tinkoff.invest import AsyncClient, MarketDataRequest, SubscribeOrderBookRequest, SubscribeTradesRequest, SubscriptionAction, OrderBookInstrument, TradeInstrument, TradeDirection
import asyncio
from enum import Enum

class TradeDirection(Enum):
    TRADE_DIRECTION_BUY = 1
    TRADE_DIRECTION_SELL = 2

class OrderBookStreamer(QObject):
    data_updated = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, token, figi):
        super().__init__()
        self.token = token
        self.figi = figi
        self.running = False
        self.thread = None

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False

    def _run(self):
        asyncio.run(self._async_stream())

    async def _async_stream(self):
        try:
            async with AsyncClient(self.token) as client:
                async def request_iterator():
                    yield MarketDataRequest(
                        subscribe_order_book_request=SubscribeOrderBookRequest(
                            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
                            instruments=[OrderBookInstrument(instrument_id=self.figi, depth=50)]
                        )
                    )
                    yield MarketDataRequest(
                        subscribe_trades_request=SubscribeTradesRequest(
                            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
                            instruments=[TradeInstrument(instrument_id=self.figi)]
                        )
                    )
                    while self.running:
                        await asyncio.sleep(0.1)
                        yield MarketDataRequest()
                stream = client.market_data_stream.market_data_stream(request_iterator())
                async for response in stream:
                    if not self.running:
                        break
                    data = {}
                    if hasattr(response, 'orderbook') and response.orderbook is not None:
                        order_book = response.orderbook
                        asks = [(float(a.price.units) + a.price.nano / 1e9, a.quantity, 0) for a in order_book.asks]
                        bids = [(float(b.price.units) + b.price.nano / 1e9, b.quantity, 0) for b in order_book.bids]
                        data['bids'] = bids
                        data['asks'] = asks
                    if hasattr(response, 'trade') and response.trade is not None:
                        trade = response.trade
                        if trade.price is not None:
                            trade_data = {
                                'price': float(trade.price.units) + trade.price.nano / 1e9,
                                'quantity': trade.quantity,
                                'direction': trade.direction
                            }
                            data['trade'] = trade_data
                    if data:
                        self.data_updated.emit(data)
        except Exception as e:
            self.error.emit(str(e))

class VolumeBarDelegate(QStyledItemDelegate):
    def __init__(self, min_vol, max_vol, row_colors, is_sum_mode=False, parent=None):
        super().__init__(parent)
        self.min_vol = min_vol
        self.max_vol = max_vol
        self.row_colors = row_colors
        self.is_sum_mode = is_sum_mode

    def paint(self, painter, option, index):
        if self.is_sum_mode and index.column() == 0:
            try:
                value = float(index.data(Qt.UserRole))
            except (TypeError, ValueError):
                value = 0
        else:
            try:
                value = int(index.data())
            except (TypeError, ValueError):
                value = 0
        row = index.row()
        color = self.row_colors[row] if row < len(self.row_colors) else QColor(255, 180, 40)
        if self.max_vol > self.min_vol and value > 0:
            ratio = (value - self.min_vol) / (self.max_vol - self.min_vol)
        else:
            ratio = 0
        if value > 0:
            min_width = 4
            bar_width = max(int(option.rect.width() * ratio), min_width)
            bar_rect = option.rect.adjusted(0, 0, -option.rect.width() + bar_width, 0)
            painter.save()
            painter.setBrush(QColor(color))
            painter.setOpacity(0.9)
            painter.setPen(Qt.NoPen)
            painter.drawRect(bar_rect)
            painter.restore()
        super().paint(painter, option, index)

class OrderBookWindow(QWidget):
    trade_received = pyqtSignal(dict, int)
    visible_prices_changed = pyqtSignal(list, int, int)
    structure_changed = pyqtSignal(int, int, list)
    scroll_changed = pyqtSignal(int)
    price_label_updated = pyqtSignal(str)
    data_from_stream = pyqtSignal(dict)

    def __init__(self, on_data_updated_callback=None):
        super().__init__()
        self.current_price = 0.0
        self.price_step = 0.01
        self.visible_rows = 20
        self.total_rows = 50
        self.streamer = None
        self.token = None
        self.figi = None
        self.lot_size = 1
        self.all_prices = []
        self.on_data_updated_callback = on_data_updated_callback
        self._pending_data = None
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(50)  # 20 раз в секунду
        self._update_timer.timeout.connect(self._update_from_buffer)
        self._update_timer.start()
        self._volumes = []
        self._vol_colors = []
        self._sums = []
        self._sum_colors = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self.price_label = QLabel("Текущая цена:")
        self.price_label.setAlignment(Qt.AlignCenter)
        self.price_label.setStyleSheet("font-size: 16px; font-weight: bold; color: #C0C0C0; background: #232323;")
        self.price_label.hide()
        layout.addWidget(self.price_label)
        
        # --- Кнопка-переключатель ---
        self.volume_mode = True  # True = 'Объём', False = 'Сумма'
        self.toggle_button = QPushButton('Показать сумму')
        self.toggle_button.setCheckable(True)
        self.toggle_button.setStyleSheet('background: #232323; color: #C0C0C0; border: 1px solid #333; border-radius: 3px; padding: 2px 8px;')
        self.toggle_button.clicked.connect(self.toggle_volume_sum)
        layout.addWidget(self.toggle_button)
        
        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Объём", "Цена"])
        self.table.setRowCount(self.total_rows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setVerticalScrollMode(QTableWidget.ScrollPerPixel)
        
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        
        self.init_empty_order_book()
        layout.addWidget(self.table)
        self.table.installEventFilter(self)
        
        self.apply_dark_style()
        
        QShortcut(QKeySequence(Qt.Key_Space), self).activated.connect(self.center_to_current_price)
    
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setVisible(True)

        self.update_chart_timer = QTimer(self)
        self.update_chart_timer.setSingleShot(True)
        self.update_chart_timer.setInterval(50)
        self.update_chart_timer.timeout.connect(self.send_visible_prices_to_chart)

        self.table.verticalScrollBar().valueChanged.connect(self.scroll_changed)

        self.data_from_stream.connect(self.on_data_updated)

    def apply_dark_style(self):
        self.setStyleSheet('''
            QMainWindow, QWidget { background: #181818; color: #C0C0C0; font-family: Consolas, monospace; font-size: 13px; }
            QTableWidget { background: #232323; color: #C0C0C0; border: 1px solid #333; gridline-color: #333; }
            QHeaderView::section { background: #232323; color: #C0C0C0; border: 1px solid #333; font-weight: bold; }
            QLabel { color: #C0C0C0; }
        ''')
        self.table.setStyleSheet('''
            QTableWidget { background: #232323; color: #C0C0C0; }
            QTableWidget::item { padding: 2px; }
        ''')
        self.price_label.setStyleSheet("font-size: 16px; font-weight: bold; color: #C0C0C0; background: #232323;")
    
    def init_empty_order_book(self):
        self.table.setRowCount(self.total_rows)
        for row in range(self.total_rows):
            for col in range(2):
                item = QTableWidgetItem("")
                item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, col, item)
    
    def set_price_range_callback(self, callback):
        self._price_range_callback = callback

    def update_order_book(self, bids, asks):
        self.last_bids = bids
        self.last_asks = asks
        if not bids and not asks:
            self.init_empty_order_book()
            return
        self.price_step = self.price_step if self.price_step and self.price_step > 0 else 0.01

        all_prices_set = set()
        if bids:
            for p, _, _ in bids: all_prices_set.add(p)
        if asks:
            for p, _, _ in asks: all_prices_set.add(p)
        if not all_prices_set: return

        min_price = min(all_prices_set)
        max_price = max(all_prices_set)

        all_prices = []
        current_price = min_price
        while current_price <= max_price + self.price_step / 2:
            all_prices.append(round(current_price, 2))
            current_price += self.price_step
        if not all_prices: all_prices = [round(min_price, 2)]

        # --- Найти лучший ask и bid для спреда ---
        best_ask = min([p for p, _, _ in asks], default=None)
        best_bid = max([p for p, _, _ in bids], default=None)
        # --- Вставить пустую строку для спреда ---
        prices_with_spread = []
        spread_inserted = False
        for price in reversed(all_prices):
            if best_bid is not None and best_ask is not None and not spread_inserted and price < best_ask and price > best_bid:
                prices_with_spread.append(None)  # None = строка-спред
                spread_inserted = True
            prices_with_spread.append(price)
        self.all_prices = prices_with_spread
        self.table.setRowCount(len(self.all_prices))
        row_height = self.table.verticalHeader().minimumSectionSize()
        for i in range(self.table.rowCount()):
            self.table.setRowHeight(i, row_height)
        self.structure_changed.emit(self.table.rowCount(), row_height, self.all_prices)

        # --- Заполнение массивов объёма, суммы и цветов ---
        self._volumes = []
        self._sums = []
        self._vol_colors = []
        self._sum_colors = []
        ask_zone_color_volume = QColor(180, 60, 60)
        bid_zone_color_volume = QColor(60, 180, 60)
        dark_yellow = QColor(180, 140, 20)
        orange = QColor(255, 180, 40)
        # Найти максимальные объёмы и суммы
        max_ask_volume = max([v for _, v, _ in asks], default=0)
        max_bid_volume = max([v for _, v, _ in bids], default=0)
        max_ask_price = None
        max_bid_price = None
        for p, v, _ in asks:
            if v == max_ask_volume:
                max_ask_price = p
                break
        for p, v, _ in bids:
            if v == max_bid_volume:
                max_bid_price = p
                break
        temp_sums = []
        for price in self.all_prices:
            if price is None:
                self._volumes.append(0)
                self._sums.append(0)
                self._vol_colors.append(orange)
                self._sum_colors.append(orange)
                continue
            ask_volume = sum(v for p, v, _ in asks if self.price_equal(p, price, self.price_step))
            bid_volume = sum(v for p, v, _ in bids if self.price_equal(p, price, self.price_step))
            volume = ask_volume if ask_volume > 0 else bid_volume
            self._volumes.append(volume)
            try:
                lot_size = getattr(self, 'lot_size', 1)
            except Exception:
                lot_size = 1
            summa = price * volume * lot_size if volume > 0 else 0
            self._sums.append(summa)
            temp_sums.append(summa)
            # Цвета для объёма
            if ask_volume > 0 and price == max_ask_price:
                self._vol_colors.append(dark_yellow)
            elif bid_volume > 0 and price == max_bid_price:
                self._vol_colors.append(dark_yellow)
            elif ask_volume > 0:
                self._vol_colors.append(ask_zone_color_volume)
            elif bid_volume > 0:
                self._vol_colors.append(bid_zone_color_volume)
            else:
                self._vol_colors.append(orange)
            # Цвета для суммы (пока как для объёма, потом скорректируем максимум)
            if ask_volume > 0:
                self._sum_colors.append(ask_zone_color_volume)
            elif bid_volume > 0:
                self._sum_colors.append(bid_zone_color_volume)
            else:
                self._sum_colors.append(orange)
        # Найдём максимум суммы среди всех (кроме None)
        max_sum = max([s for i, s in enumerate(self._sums) if self.all_prices[i] is not None], default=0)
        for idx, price in enumerate(self.all_prices):
            if price is None:
                continue
            if self._sums[idx] == max_sum and self._sums[idx] > 0:
                self._sum_colors[idx] = dark_yellow

        for row, price in enumerate(self.all_prices):
            # Вторая колонка всегда цена!
            if price is None:
                price_item = QTableWidgetItem("")
            else:
                price_item = QTableWidgetItem(f"{price:,.2f}")
            price_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 1, price_item)
        self.update_first_column()

        # --- Окраска фона ячейки с ценой (вторая колонка) ---
        default_bg_color = QColor(24, 24, 24)
        ask_zone_color = QColor(45, 35, 35)    # Приглушенный красный
        bid_zone_color = QColor(35, 45, 35)    # Приглушенный зелёный
        best_ask_color = QColor(80, 40, 40)    # Яркий красный
        best_bid_color = QColor(40, 80, 40)    # Яркий зелёный
        spread_color = QColor(60, 60, 30)      # Цвет для спреда

        for row, price in enumerate(self.all_prices):
            if price is None:
                bg_color = spread_color
            else:
                ask_volume = sum(v for p, v, _ in asks if self.price_equal(p, price, self.price_step))
                bid_volume = sum(v for p, v, _ in bids if self.price_equal(p, price, self.price_step))
                bg_color = default_bg_color
                if ask_volume > 0:
                    bg_color = ask_zone_color
                if bid_volume > 0:
                    bg_color = bid_zone_color
                if best_ask is not None and self.price_equal(price, best_ask, self.price_step):
                    bg_color = best_ask_color
                if best_bid is not None and self.price_equal(price, best_bid, self.price_step):
                    bg_color = best_bid_color
            item = self.table.item(row, 1)
            if item is not None:
                item.setBackground(bg_color)

    def highlight_current_price(self):
        pass
    
    def center_to_current_price(self):
        for row, price in enumerate(self.all_prices):
            if self.price_equal(price, self.current_price, self.price_step):
                self.table.scrollToItem(self.table.item(row, 0), QAbstractItemView.PositionAtCenter)
                break
    
    def get_visible_prices(self):
        first_row = self.table.rowAt(0)
        last_row = self.table.rowAt(self.table.viewport().height() - 1)
        if first_row == -1: first_row = 0
        if last_row == -1: last_row = self.table.rowCount() - 1
        
        prices = []
        for row in range(first_row, last_row + 1):
            item = self.table.item(row, 1)
            if item:
                try: prices.append(float(item.text()))
                except (ValueError, AttributeError): pass
        return prices
    
    def eventFilter(self, source, event):
        if event.type() == event.Wheel and source is self.table:
            scroll_bar = self.table.verticalScrollBar()
            scroll_bar.setValue(scroll_bar.value() - event.angleDelta().y())
            self.update_chart_timer.start()
            return True
        return super().eventFilter(source, event)

    def start_stream(self):
        if not self.token or not self.figi:
            return
        # Регистрируемся в StreamManager
        from order_book_copy import StreamManager
        self.stream_manager = StreamManager(self.token)
        self.stream_manager.register(self.figi, self)

    def stop_stream(self):
        if hasattr(self, 'stream_manager'):
            self.stream_manager.unregister(self.figi)

    def on_data_updated(self, data):
        self._pending_data = data

    def _update_from_buffer(self):
        if self._pending_data is not None:
            data = self._pending_data
            self._pending_data = None
            bids = data.get('bids', [])
            asks = data.get('asks', [])
            if bids or asks:
                self.update_order_book(bids, asks)
            if 'trade' in data:
                trade = data['trade']
                self.current_price = trade['price']
                direction = "↑" if trade['direction'] == TradeDirection.TRADE_DIRECTION_BUY else "↓"
                label_text = f"Текущая цена: {self.current_price:.2f} {direction} "
                self.price_label_updated.emit(label_text)
                trade_row_index = -1
                for i, price in enumerate(self.all_prices):
                    if self.price_equal(price, trade['price'], self.price_step):
                        trade_row_index = i
                        break
                if trade_row_index != -1:
                    self.trade_received.emit(trade, trade_row_index)
                self.update_chart_timer.start()
            if self.on_data_updated_callback:
                self.on_data_updated_callback(data)

    def on_stream_error(self, msg):
        pass

    def price_equal(self, p1, p2, step):
        if p1 is None or p2 is None:
            return False
        return abs(round(p1 - p2, 6)) < step / 2

    def send_visible_prices_to_chart(self):
        if not self.table.isVisible():
            return
            
        first_row = self.table.rowAt(self.table.viewport().y())
        if first_row == -1:
            first_row = self.table.verticalScrollBar().value()

        visible_prices = self.get_visible_prices()
        row_height = self.table.verticalHeader().minimumSectionSize()
        
        self.visible_prices_changed.emit(visible_prices, row_height, first_row)

    def public_update_order_book(self, bids, asks):
        self.update_order_book(bids, asks)

    @pyqtSlot(int)
    def sync_scroll(self, value):
        self.table.verticalScrollBar().setValue(value)

    def toggle_volume_sum(self):
        self.volume_mode = not self.volume_mode
        if self.volume_mode:
            self.toggle_button.setText('Показать сумму')
            self.table.setHorizontalHeaderLabels(["Объём", "Цена"])
        else:
            self.toggle_button.setText('Показать объём')
            self.table.setHorizontalHeaderLabels(["Сумма", "Цена"])
        # Обновить только первую колонку
        self.update_first_column()

    def update_first_column(self):
        if not hasattr(self, '_volumes') or not hasattr(self, '_sums') or not self._volumes or not self._sums:
            return
        if self.volume_mode:
            min_vol = min([v for i, v in enumerate(self._volumes) if v > 0 and self.all_prices[i] is not None], default=0)
            max_vol = max([v for i, v in enumerate(self._volumes) if self.all_prices[i] is not None], default=0)
            delegate = VolumeBarDelegate(min_vol, max_vol, self._vol_colors, is_sum_mode=False, parent=self.table)
            self.table.setItemDelegateForColumn(0, delegate)
            for row, value in enumerate(self._volumes):
                if self.all_prices[row] is None:
                    item = QTableWidgetItem("")
                else:
                    text = str(int(value)) if value > 0 else ''
                    item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                self.table.setItem(row, 0, item)
        else:
            min_sum = min([s for i, s in enumerate(self._sums) if s > 0 and self.all_prices[i] is not None], default=0)
            max_sum = max([s for i, s in enumerate(self._sums) if self.all_prices[i] is not None], default=0)
            delegate = VolumeBarDelegate(min_sum, max_sum, self._sum_colors, is_sum_mode=True, parent=self.table)
            self.table.setItemDelegateForColumn(0, delegate)
            for row, summa in enumerate(self._sums):
                if self.all_prices[row] is None:
                    item = QTableWidgetItem("")
                else:
                    text = f"{summa:,.2f}" if summa > 0 else ''
                    item = QTableWidgetItem(text)
                    item.setData(Qt.UserRole, summa)
                item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                self.table.setItem(row, 0, item)

# Этот класс больше не используется напрямую в main.py, но мы оставляем его здесь.
class OrderBook(QTableWidget):
    visible_prices_changed = pyqtSignal(list, int, int)
    trade_signal = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.price_column = 1
        
        self._emit_timer = QTimer(self)
        self._emit_timer.setInterval(50)
        self._emit_timer.setSingleShot(True)
        self._emit_timer.timeout.connect(self._emit_visible_prices)
        
        self.verticalScrollBar().valueChanged.connect(self._request_emit_visible_prices)
        self.painted.connect(self._request_emit_visible_prices)
        
        self.setColumnCount(3)
        self.setHorizontalHeaderLabels(["Объем", "Цена", "Объем"])
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.verticalHeader().setVisible(False)
        self.setShowGrid(False)
        self.setFocusPolicy(Qt.NoFocus)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.NoSelection)

    def _request_emit_visible_prices(self):
        if not self._emit_timer.isActive():
            self._emit_timer.start()

    def _emit_visible_prices(self):
        if self.rowCount() == 0:
            self.visible_prices_changed.emit([], 0, 0)
            return

        first_visible_row = self.rowAt(self.viewport().y())
        if first_visible_row == -1: first_visible_row = 0

        last_visible_row = self.rowAt(self.viewport().y() + self.viewport().height() - 1)
        if last_visible_row == -1: last_visible_row = self.rowCount() - 1
        
        visible_prices = []
        if first_visible_row <= last_visible_row:
            for row in range(first_visible_row, last_visible_row + 1):
                item = self.item(row, self.price_column)
                if item: visible_prices.append(item.text())

        row_height = self.rowHeight(0) if self.rowCount() > 0 else 0
        self.visible_prices_changed.emit(visible_prices, row_height, first_visible_row)

    def update_data(self, data):
        self.setRowCount(len(data))
        for i, row_data in enumerate(data):
            for j, cell_data in enumerate(row_data):
                item = QTableWidgetItem(str(cell_data))
                item.setTextAlignment(Qt.AlignCenter)
                if j == 1:
                    font = QFont(); font.setBold(True); item.setFont(font)
                self.setItem(i, j, item)
        self._request_emit_visible_prices()

    def add_trade(self, trade_data):
        self.trade_signal.emit(trade_data)

# --- StreamManager ---
class StreamManager(QObject):
    _instance = None
    def __new__(cls, token):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    def __init__(self, token):
        if self._initialized:
            return
        super().__init__()
        self.token = token
        self.figi_to_orderbook = {}  # figi: OrderBookWindow
        self.running = False
        self.thread = None
        self._initialized = True
    def register(self, figi, orderbook):
        self.figi_to_orderbook[figi] = orderbook
        self.restart()
    def unregister(self, figi):
        if figi in self.figi_to_orderbook:
            del self.figi_to_orderbook[figi]
            self.restart()
    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
    def stop(self):
        self.running = False
    def restart(self):
        self.stop()
        if self.figi_to_orderbook:
            self.start()
    def _run(self):
        asyncio.run(self._async_stream())
    async def _async_stream(self):
        try:
            async with AsyncClient(self.token) as client:
                figis = list(self.figi_to_orderbook.keys())
                async def request_iterator():
                    yield MarketDataRequest(
                        subscribe_order_book_request=SubscribeOrderBookRequest(
                            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
                            instruments=[OrderBookInstrument(instrument_id=figi, depth=50) for figi in figis]
                        )
                    )
                    yield MarketDataRequest(
                        subscribe_trades_request=SubscribeTradesRequest(
                            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
                            instruments=[TradeInstrument(instrument_id=figi) for figi in figis]
                        )
                    )
                    while self.running:
                        await asyncio.sleep(0.1)
                        yield MarketDataRequest()
                stream = client.market_data_stream.market_data_stream(request_iterator())
                async for response in stream:
                    if not self.running:
                        break
                    # Определяем FIGI
                    figi = None
                    if hasattr(response, 'orderbook') and response.orderbook is not None:
                        figi = response.orderbook.figi
                    elif hasattr(response, 'trade') and response.trade is not None:
                        figi = response.trade.figi
                    if figi and figi in self.figi_to_orderbook:
                        data = {}
                        if hasattr(response, 'orderbook') and response.orderbook is not None:
                            order_book = response.orderbook
                            asks = [(float(a.price.units) + a.price.nano / 1e9, a.quantity, 0) for a in order_book.asks]
                            bids = [(float(b.price.units) + b.price.nano / 1e9, b.quantity, 0) for b in order_book.bids]
                            data['bids'] = bids
                            data['asks'] = asks
                        if hasattr(response, 'trade') and response.trade is not None:
                            trade = response.trade
                            if trade.price is not None:
                                trade_data = {
                                    'price': float(trade.price.units) + trade.price.nano / 1e9,
                                    'quantity': trade.quantity,
                                    'direction': trade.direction
                                }
                                data['trade'] = trade_data
                        if data:
                            self.figi_to_orderbook[figi].data_from_stream.emit(data)
        except Exception as e:
            pass

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = OrderBookWindow()
    window.show()
    sys.exit(app.exec_())