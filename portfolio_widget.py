from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem
from PyQt5.QtCore import Qt, pyqtSignal, QObject, QTimer
from PyQt5.QtGui import QColor
import threading
import asyncio
from google.protobuf.json_format import MessageToDict
try:
    from tinkoff.invest import AsyncClient
    from tinkoff.invest.services import OperationsStreamService
except ImportError:
    AsyncClient = None
    OperationsStreamService = None

class PortfolioStreamWorker(QObject):
    data_updated = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, token, account_id):
        super().__init__()
        self.token = token
        self.account_id = account_id
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
        if AsyncClient is None:
            self.error.emit("tinkoff.invest не установлен")
            return
        try:
            async with AsyncClient(self.token) as client:
                async for item in client.operations_stream.portfolio_stream(accounts=[self.account_id]):
                    if not self.running:
                        break
                    try:
                        data = item.dict()
                    except AttributeError:
                        data = item.__dict__
                    self.data_updated.emit(data)
        except Exception as e:
            self.error.emit(str(e))

class PortfolioWidget(QWidget):
    rest_data_ready = pyqtSignal(object)

    def __init__(self, token, account_id):
        super().__init__()
        self.setWindowTitle("Портфель")
        self.resize(700, 400)
        self.setStyleSheet('''
            QWidget { background: #181818; color: #C0C0C0; }
        ''')
        self.layout = QVBoxLayout(self)
        self.label = QLabel("Портфель (обновляется в реальном времени)")
        self.layout.addWidget(self.label)
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels([
            "Тикер", "Тип", "Кол-во", "Сред. цена", "Тек. цена", "Доход", "Доход, %"
        ])
        self.layout.addWidget(self.table)
        self.table.verticalHeader().setVisible(False)  # скрыть нумерацию строк
        self.table.setShowGrid(False)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSortingEnabled(False)
        self.token = token
        self.account_id = account_id
        self.worker = PortfolioStreamWorker(token, account_id)
        self.worker.data_updated.connect(self.update_portfolio)
        self.worker.error.connect(self.show_error)
        self.worker.start()
        # --- Таймер для периодического обновления через REST ---
        self.rest_data_ready.connect(self.update_portfolio)
        self.rest_timer = QTimer(self)
        self.rest_timer.setInterval(8000)  # 8 секунд
        self.rest_timer.timeout.connect(self.update_portfolio_rest)
        self.rest_timer.start()
        self._rest_loop = asyncio.new_event_loop()
        threading.Thread(target=self._run_rest_loop, daemon=True).start()

    def _run_rest_loop(self):
        asyncio.set_event_loop(self._rest_loop)
        self._rest_loop.run_forever()

    def update_portfolio_rest(self):
        asyncio.run_coroutine_threadsafe(self._fetch_and_emit(), self._rest_loop)

    async def _fetch_and_emit(self):
        if AsyncClient is None:
            return
        try:
            async with AsyncClient(self.token) as client:
                resp = await client.operations.get_portfolio(account_id=self.account_id)
                try:
                    data = resp.dict()
                except Exception:
                    data = resp.__dict__
                self.rest_data_ready.emit({'portfolio': data})
        except Exception as e:
            self.show_error(f"REST: {e}")

    def update_portfolio(self, data):
        import json
        # Если окно скрыто — не обновлять
        if not self.isVisible():
            return
        # Если была ошибка, а теперь всё ок — убрать сообщение
        if hasattr(self, '_last_error') and self._last_error:
            self.label.setText("Портфель (обновляется в реальном времени)")
            self._last_error = None
        # Преобразуем в dict, если это не dict
        if not isinstance(data, dict):
            try:
                data = data.dict()
            except AttributeError:
                data = data.__dict__
        portfolio = data.get('portfolio') or data.get('result', {}).get('portfolio')
        if not portfolio:
            return
        if not isinstance(portfolio, dict):
            try:
                portfolio = portfolio.dict()
            except AttributeError:
                portfolio = portfolio.__dict__
        positions = portfolio.get('positions', [])
        self.table.setUpdatesEnabled(False)
        # --- Группировка по типу инструмента ---
        groups = {'Валюта и металлы': [], 'Акции': [], 'Облигации': [], 'Фонды': [], 'Фьючерсы': [], 'Другое': []}
        for pos in positions:
            if not isinstance(pos, dict):
                try:
                    pos = pos.dict()
                except AttributeError:
                    pos = pos.__dict__
            instr_type = pos.get('instrument_type', '').lower()
            if instr_type in ('currency', 'metal'):
                groups['Валюта и металлы'].append(pos)
            elif instr_type == 'share':
                groups['Акции'].append(pos)
            elif instr_type == 'bond':
                groups['Облигации'].append(pos)
            elif instr_type == 'etf':
                groups['Фонды'].append(pos)
            elif instr_type == 'futures':
                groups['Фьючерсы'].append(pos)
            else:
                groups['Другое'].append(pos)
        # --- Считаем итоговое количество строк ---
        rows = 0
        for group, items in groups.items():
            if items:
                rows += 1 + len(items)  # строка группы + позиции
        self.table.setRowCount(rows)
        # --- Тёмный стиль ---
        self.table.setStyleSheet('''
            QTableWidget { background: #181818; color: #C0C0C0; border: 1px solid #222; gridline-color: #333; }
            QHeaderView::section { background: #232323; color: #C0C0C0; border: 1px solid #222; font-weight: bold; }
            QTableWidget::item { padding: 2px; }
        ''')
        row = 0
        for group, items in groups.items():
            if not items:
                continue
            # --- Строка группы ---
            group_item = QTableWidgetItem(group)
            group_item.setFlags(Qt.ItemIsEnabled)
            group_item.setBackground(QColor('#232323'))
            group_item.setForeground(QColor('#C0C0C0'))
            self.table.setItem(row, 0, group_item)
            for col in range(1, self.table.columnCount()):
                empty = QTableWidgetItem('')
                empty.setFlags(Qt.ItemIsEnabled)
                empty.setBackground(QColor('#232323'))
                empty.setForeground(QColor('#C0C0C0'))
                self.table.setItem(row, col, empty)
            row += 1
            # --- Позиции ---
            for pos in items:
                if not isinstance(pos, dict):
                    try:
                        pos = pos.dict()
                    except AttributeError:
                        pos = pos.__dict__
                ticker = pos.get('ticker', '') or pos.get('figi', '')
                quantity = self._format_quota(pos.get('quantity')) or '—'
                avg_price = self._format_money(pos.get('average_position_price')) or '—'
                cur_price = self._format_money(pos.get('current_price')) or '—'
                # --- Стоимость ---
                try:
                    q = float(quantity.replace(',', '.'))
                except Exception:
                    q = 0
                try:
                    cp = float(cur_price.split()[0].replace(',', '.'))
                except Exception:
                    cp = 0
                value = q * cp if q and cp else ''
                value_str = f"{value:,.2f}" if value else '—'
                # --- Доход и доходность ---
                try:
                    ap = float(avg_price.split()[0].replace(',', '.'))
                except Exception:
                    ap = 0
                profit = (cp - ap) * q if q and cp and ap else ''
                profit_str = f"{profit:,.2f}" if profit else '—'
                profit_pct = (profit / (ap * q) * 100) if profit and ap and q else ''
                profit_pct_str = f"{profit_pct:.2f}%" if profit_pct != '' else '—'
                # --- Заполнение строки ---
                for j, val in enumerate([ticker, pos.get('instrument_type', '—'), quantity, avg_price, cur_price, profit_str, profit_pct_str]):
                    item = QTableWidgetItem(val)
                    item.setTextAlignment(Qt.AlignCenter)
                    # Цвет для дохода и доходности
                    if j in (5, 6):
                        try:
                            num = float(val.replace('%', '').replace(',', '.'))
                            if num > 0:
                                item.setForeground(QColor('#98c379'))  # зелёный
                            elif num < 0:
                                item.setForeground(QColor('#e06c75'))  # красный
                        except Exception:
                            pass
                    self.table.setItem(row, j, item)
                row += 1
        # --- Очистить лишние строки, если их стало меньше ---
        for r in range(row, self.table.rowCount()):
            for c in range(self.table.columnCount()):
                self.table.setItem(r, c, QTableWidgetItem(''))
        self.table.setUpdatesEnabled(True)
        # Сравниваем с предыдущим состоянием
        new_data_str = json.dumps(data, sort_keys=True, default=str)
        if hasattr(self, '_last_portfolio_data') and self._last_portfolio_data == new_data_str:
            return  # Данные не изменились, не обновляем таблицу
        self._last_portfolio_data = new_data_str

    def show_error(self, msg):
        # Показывать ошибку только если она новая или не INTERNAL
        if hasattr(self, '_last_error') and self._last_error == msg:
            return
        self._last_error = msg
        if 'INTERNAL' in msg or 'Internal error' in msg:
            self.label.setText("Ошибка: Временная проблема соединения с сервером Tinkoff. Повторяем попытку...")
        else:
            self.label.setText(f"Ошибка: {msg}")

    def closeEvent(self, event):
        self.worker.stop()
        self.rest_timer.stop()
        super().closeEvent(event)

    def _format_money(self, money):
        if not money:
            return ''
        if not isinstance(money, dict):
            try:
                money = money.dict()
            except AttributeError:
                money = money.__dict__
        units = money.get('units', '0')
        nano = money.get('nano', 0)
        currency = money.get('currency', '')
        try:
            value = float(units) + int(nano) / 1e9
        except Exception:
            value = 0
        return f"{value:.2f} {currency}" if currency else f"{value:.2f}"

    def _format_quota(self, quota):
        if not quota:
            return ''
        if not isinstance(quota, dict):
            try:
                quota = quota.dict()
            except AttributeError:
                quota = quota.__dict__
        units = quota.get('units', '0')
        nano = quota.get('nano', 0)
        try:
            value = float(units) + int(nano) / 1e9
        except Exception:
            value = 0
        return f"{value:.2f}" 