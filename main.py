import sys
from PyQt5.QtWidgets import QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QComboBox, QLineEdit, QPushButton, QLabel, QTableWidgetItem, QScrollArea
from PyQt5.QtCore import Qt
from order_book_copy import OrderBookWindow
from tinkoff.invest import Client
from portfolio_widget import PortfolioWidget

# Вспомогательная функция для загрузки инструментов
def load_instruments_by_token(token):
    class_codes = []
    ticker_map = {}
    with Client(token) as client:
        all_instruments = []
        shares = client.instruments.shares().instruments
        all_instruments.extend(shares)
        futures = client.instruments.futures().instruments
        all_instruments.extend(futures)
        codes = set()
        for inst in all_instruments:
            if inst.class_code in ("TQBR", "SPBFUT") and inst.api_trade_available_flag:
                codes.add(inst.class_code)
                ticker_map[(inst.ticker, inst.class_code)] = inst
        class_codes = sorted(list(codes))
    return class_codes, ticker_map

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Тиковый график + Стакан")
        self.setGeometry(100, 100, 1800, 900)
        self.class_codes = []
        self.ticker_map = {}
        self.order_books = []  # список всех стаканов

        self.setStyleSheet('''
            QMainWindow, QWidget { background: #181818; color: #C0C0C0; font-family: Consolas, monospace; font-size: 13px; }
            QLineEdit, QComboBox { background: #232323; color: #C0C0C0; border: 1px solid #333; border-radius: 3px; padding: 2px; }
            QPushButton { background: #232323; color: #C0C0C0; border: 1px solid #333; border-radius: 3px; padding: 4px 10px; }
            QPushButton:hover { background: #2a2a2a; }
            QLabel { color: #C0C0C0; }
        ''')

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # --- Панель управления ---
        control_layout = QHBoxLayout()
        self.token_input = QLineEdit()
        self.token_input.setPlaceholderText("API токен")
        self.token_input.setEchoMode(QLineEdit.Password)
        self.auth_button = QPushButton("Авторизоваться")
        self.auth_button.clicked.connect(self.load_instruments)
        self.add_order_book_button = QPushButton("Добавить стакан")
        self.add_order_book_button.clicked.connect(self.add_order_book)
        self.portfolio_button = QPushButton("Портфель")
        self.portfolio_button.clicked.connect(self.show_portfolio)
        control_layout.addWidget(QLabel("Токен:"))
        control_layout.addWidget(self.token_input)
        control_layout.addWidget(self.auth_button)
        control_layout.addWidget(self.add_order_book_button)
        control_layout.addWidget(self.portfolio_button)
        main_layout.addLayout(control_layout)

        # --- Контейнер для стаканов ---
        self.content_layout = QHBoxLayout()
        self.content_layout.addStretch(1)
        main_layout.addLayout(self.content_layout)

    def load_instruments(self):
        token = self.token_input.text().strip()
        if not token:
            return
        class_codes, ticker_map = load_instruments_by_token(token)
        self.class_codes = class_codes
        self.ticker_map = ticker_map
        # Получаем счета пользователя
        try:
            with Client(token) as client:
                accounts = client.users.get_accounts().accounts
                self.accounts = accounts
        except Exception as e:
            self.accounts = []
        for ob in self.order_books:
            ob['class_code_combo'].clear()
            ob['class_code_combo'].addItems(self.class_codes)
            ob['class_code_combo'].setEnabled(True)
            ob['ticker_combo'].setEnabled(False)
            ob['start_button'].setEnabled(False)
            ob['class_code_combo'].setCurrentIndex(0)
            self.on_class_code_changed(ob, 0)

    def add_order_book(self):
        # Панель управления для стакана
        ob_panel = QVBoxLayout()
        # --- Лейбл для тикера ---
        ticker_label = QLabel()
        ticker_label.setText("")
        ticker_label.setAlignment(Qt.AlignCenter)
        ticker_label.setStyleSheet("background: #2a2a2a; color: #b0b0b0; border-radius: 6px; padding: 2px 10px; font-size: 11px; min-height: 18px; max-width: 120px;")
        ticker_label.hide()
        ob_panel.addWidget(ticker_label, alignment=Qt.AlignHCenter)
        control = QHBoxLayout()
        class_code_combo = QComboBox()
        class_code_combo.setPlaceholderText("Площадка")
        ticker_combo = QComboBox()
        ticker_combo.setPlaceholderText("Тикер")
        ticker_combo.setEnabled(False)
        start_button = QPushButton("Старт стрима")
        start_button.setEnabled(False)
        control.addWidget(QLabel("Площадка:"))
        control.addWidget(class_code_combo)
        control.addWidget(QLabel("Тикер:"))
        control.addWidget(ticker_combo)
        control.addWidget(start_button)
        ob_panel.addLayout(control)
        # Сам стакан
        order_book = OrderBookWindow()
        order_book.setMinimumWidth(400)
        order_book.setMaximumWidth(400)
        order_book.table.verticalHeader().setMinimumSectionSize(20)
        ob_panel.addWidget(order_book)
        # Объединяем в виджет
        ob_widget = QWidget()
        ob_widget.setLayout(ob_panel)
        self.content_layout.addWidget(ob_widget)
        # Сохраняем все элементы для управления
        ob_dict = {
            'widget': ob_widget,
            'order_book': order_book,
            'class_code_combo': class_code_combo,
            'ticker_combo': ticker_combo,
            'start_button': start_button,
            'ticker_label': ticker_label
        }
        self.order_books.append(ob_dict)
        # Сигналы
        class_code_combo.currentIndexChanged.connect(lambda idx, ob=ob_dict: self.on_class_code_changed(ob, idx))
        ticker_combo.currentIndexChanged.connect(lambda idx, ob=ob_dict: self.on_ticker_changed(ob, idx))
        start_button.clicked.connect(lambda checked, ob=ob_dict: self.toggle_stream(ob))

    def on_class_code_changed(self, ob, idx):
        if idx < 0 or not self.class_codes:
            ob['ticker_combo'].clear()
            ob['ticker_combo'].setEnabled(False)
            ob['start_button'].setEnabled(False)
            return
        class_code = self.class_codes[idx]
        ob['ticker_combo'].clear()
        tickers = [t for (t, c) in self.ticker_map if c == class_code]
        ob['ticker_combo'].addItems(sorted(tickers))
        ob['ticker_combo'].setEnabled(True)
        self.on_ticker_changed(ob, ob['ticker_combo'].currentIndex())

    def on_ticker_changed(self, ob, idx):
        if idx < 0 or not ob['ticker_combo'].count():
            ob['start_button'].setEnabled(False)
            return
        ob['start_button'].setEnabled(True)

    def toggle_stream(self, ob):
        token = self.token_input.text().strip()
        class_code = ob['class_code_combo'].currentText()
        ticker = ob['ticker_combo'].currentText()
        if not token or not class_code or not ticker:
            return
        instrument = self.ticker_map.get((ticker, class_code))
        if not instrument:
            return
        instrument_id = instrument.figi
        lot_size = getattr(instrument, 'lot', 1)
        # Получаем шаг цены
        price_step = getattr(instrument, 'min_price_increment', None)
        if price_step is None:
            price_step = getattr(instrument, 'price_step', 0.01)
        # min_price_increment может быть объектом типа Quotation (units, nano)
        if hasattr(price_step, 'units') and hasattr(price_step, 'nano'):
            price_step = float(price_step.units) + price_step.nano / 1e9
        try:
            price_step = float(price_step)
        except Exception:
            price_step = 0.01
        print(f"[INFO] Для тикера {ticker} шаг цены: {price_step}")
        ob['order_book'].token = token
        ob['order_book'].figi = instrument_id
        ob['order_book'].lot_size = lot_size
        ob['order_book'].price_step = price_step
        ob['order_book'].start_stream()
        ob['start_button'].setText("Стоп стрима")
        ob['start_button'].clicked.disconnect()
        ob['start_button'].clicked.connect(lambda checked, ob=ob: self.stop_stream(ob))
        # --- Показываем и обновляем лейбл тикера ---
        ob['ticker_label'].setText(f"{ticker} ({class_code})")
        ob['ticker_label'].show()

    def stop_stream(self, ob):
        ob['order_book'].stop_stream()
        ob['start_button'].setText("Старт стрима")
        ob['start_button'].clicked.disconnect()
        ob['start_button'].clicked.connect(lambda checked, ob=ob: self.toggle_stream(ob))

    def show_portfolio(self):
        token = self.token_input.text().strip()
        if not hasattr(self, 'accounts') or not self.accounts:
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Ошибка", "Нет доступных счетов для отображения портфеля!")
            return
        account_id = self.accounts[0].id
        if not hasattr(self, 'portfolio_widget'):
            self.portfolio_widget = PortfolioWidget(token, account_id)
        self.portfolio_widget.show()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())