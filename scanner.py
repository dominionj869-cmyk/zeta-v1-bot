import os
import re
import csv
import json
import time
import math
import logging
import threading
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import iqoptionapi.constants as OP_code
from iqoptionapi.stable_api import IQ_Option

# PRACTICE ONLY. Strategy: ALMA 7/12 + Choppiness Index on closed 1-minute candles.
# No OTC, crypto, indices, or commodities. No candlestick-pattern or momentum filters.
IQ_EMAIL = os.getenv('IQ_EMAIL', '').strip()
IQ_PASSWORD = os.getenv('IQ_PASSWORD', '').strip()
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '').strip()
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '').strip()
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN', '').strip()
GITHUB_REPOSITORY = os.getenv('GITHUB_REPOSITORY', '').strip()
GITHUB_REF_NAME = os.getenv('GITHUB_REF_NAME', 'main').strip()
GITHUB_WORKFLOW_FILE = 'scanner.yml'

BALANCE_MODE = 'PRACTICE'
AUTO_TRADE = True
STAKE = 1.00
EXPIRY_MINUTES = 1
CANDLE_SECONDS = 60

ALMA_FAST_PERIOD = 7
ALMA_SLOW_PERIOD = 12
ALMA_OFFSET = 0.85
ALMA_SIGMA = 6.0
CHOP_PERIOD = 25
CHOP_UPPER_LEVEL = 59.8
CHOP_LOWER_LEVEL = 36.2
CHOP_RECENT_CANDLES = 3

SESSION_PROFIT_TARGET = 5.0
SESSION_LOSS_LIMIT = 3.0
MAX_RESOLVED_TRADES = 20
MAX_CONSECUTIVE_LOSSES = 5
SCAN_INTERVAL = 5
ASSET_REFRESH_SECONDS = 900
HEARTBEAT_SECONDS = 300
RECONNECT_SECONDS = 20
CANDLE_REQUEST_TIMEOUT = 10
ORDER_RESULT_RETRY_SECONDS = 5
DIAGNOSTIC_LOG_SECONDS = 300
REJECTED_ASSET_COOLDOWN_SECONDS = 600
CANDLE_REQUEST_COUNT = max(100, CHOP_PERIOD + ALMA_SLOW_PERIOD + 15)
LOG_FILE = 'momentum_signal_log.csv'
STATE_FILE = 'momentum_state.json'
LAGOS = ZoneInfo('Africa/Lagos')

CURRENCIES = {
    'USD', 'EUR', 'GBP', 'JPY', 'CHF', 'CAD', 'AUD', 'NZD', 'CNY',
    'HKD', 'SGD', 'SEK', 'NOK', 'DKK', 'PLN', 'CZK', 'HUF', 'TRY',
    'ZAR', 'MXN', 'BRL', 'INR', 'THB', 'AED', 'SAR', 'ILS', 'KRW',
    'TWD', 'RUB', 'RON', 'BGN', 'ISK', 'PHP', 'IDR', 'MYR'
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
SESSION_ID = '{}-{}'.format(os.getenv('GITHUB_RUN_ID', str(int(time.time()))), os.getenv('GITHUB_RUN_ATTEMPT', '1'))
IQ = None
CONNECTION_LOCK = threading.RLock()
BROKER_API_LOCK = threading.RLock()
LAST_RECONNECT_ATTEMPT = 0.0
ASSETS = []
ASSET_REFRESH_AT = 0
REJECTED_ASSETS = {}
STOP_EVENT = threading.Event()
TRADE_LOCK = threading.Lock()
STATE_LOCK = threading.RLock()
RESULT_MONITOR_LOCK = threading.Lock()
RESULT_MONITORS = {}
ASYNC_API_MISSING_LOGGED = False
NEXT_WORKFLOW_REQUESTED = False

DIAG_KEYS = [
    'assets_scanned', 'candle_data_ok', 'empty_candles', 'insufficient_candles',
    'signals_checked', 'alma_call', 'alma_put', 'chop_rejected',
    'setups_found', 'duplicate_signals', 'order_rejected'
]
DIAG = {key: 0 for key in DIAG_KEYS}
DIAG_LOCK = threading.Lock()
DIAG_LAST_ASSET_LOG = {}


def now_lagos():
    return datetime.now(LAGOS)


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        response = requests.post(
            'https://api.telegram.org/bot{}/sendMessage'.format(TELEGRAM_TOKEN),
            data={'chat_id': TELEGRAM_CHAT_ID, 'text': message,
                  'parse_mode': 'HTML', 'disable_web_page_preview': True},
            timeout=10,
        )
        response.raise_for_status()
    except Exception as exc:
        logging.warning('Telegram send failed: %s', exc)


def fresh_state():
    return {
        'date': now_lagos().date().isoformat(), 'session_id': SESSION_ID,
        'profit': 0.0, 'wins': 0, 'losses': 0, 'resolved': 0,
        'consecutive_losses': 0, 'pending_order_id': None,
        'pending_asset': None, 'pending_direction': None, 'pending_started': None,
        'last_signals': {}, 'stop_reason': '', 'settled_order_ids': []
    }


def load_state():
    state = fresh_state()
    try:
        with open(STATE_FILE, encoding='utf-8') as handle:
            old = json.load(handle)
        if not isinstance(old, dict):
            return state
    except (OSError, ValueError, TypeError):
        return state
    if old.get('date') == state['date']:
        for key in ('profit', 'wins', 'losses', 'resolved', 'consecutive_losses',
                    'last_signals', 'stop_reason', 'settled_order_ids'):
            if key in old:
                state[key] = old[key]
    for key in ('pending_order_id', 'pending_asset', 'pending_direction', 'pending_started'):
        state[key] = old.get(key)
    if not isinstance(state.get('last_signals'), dict):
        state['last_signals'] = {}
    if not isinstance(state.get('settled_order_ids'), list):
        state['settled_order_ids'] = []
    return state


STATE = load_state()


def persist_state_to_github():
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        logging.warning('State not committed: GITHUB_TOKEN/repository unavailable')
        return
    try:
        subprocess.run(['git', 'config', 'user.name', 'github-actions[bot]'],
                       check=True, capture_output=True, text=True)
        subprocess.run(['git', 'config', 'user.email',
                        '41898282+github-actions[bot]@users.noreply.github.com'],
                       check=True, capture_output=True, text=True)
        files = [STATE_FILE] + ([LOG_FILE] if os.path.exists(LOG_FILE) else [])
        subprocess.run(['git', 'add'] + files, check=True, capture_output=True, text=True)
        if subprocess.run(['git', 'diff', '--cached', '--quiet'], capture_output=True).returncode == 0:
            return
        subprocess.run(['git', 'commit', '-m', 'Persist scanner state and trade log'],
                       check=True, capture_output=True, text=True)
        result = subprocess.run(['git', 'push', 'origin', 'HEAD'], capture_output=True, text=True)
        if result.returncode:
            logging.warning('State push failed: %s', result.stderr.strip())
    except Exception as exc:
        logging.warning('Git state persistence failed: %s', exc)


def save_state(persist=False):
    try:
        with STATE_LOCK:
            STATE['date'] = now_lagos().date().isoformat()
            STATE['session_id'] = SESSION_ID
            with open(STATE_FILE + '.tmp', 'w', encoding='utf-8') as handle:
                json.dump(STATE, handle, indent=2)
            os.replace(STATE_FILE + '.tmp', STATE_FILE)
        if persist:
            persist_state_to_github()
    except Exception as exc:
        logging.warning('State save failed: %s', exc)


def log_trade(asset, direction, signal_value, status, profit='', order_id=''):
    # The legacy momentum10 CSV column is retained so existing log readers keep working.
    fields = ['timestamp_lagos', 'session_id', 'asset', 'direction', 'momentum10',
              'status', 'profit', 'order_id', 'session_profit']
    try:
        exists = os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 0
        with open(LOG_FILE, 'a', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            if not exists:
                writer.writeheader()
            writer.writerow({
                'timestamp_lagos': now_lagos().isoformat(timespec='seconds'),
                'session_id': SESSION_ID, 'asset': asset, 'direction': direction,
                'momentum10': '' if signal_value is None else round(float(signal_value), 8),
                'status': status, 'profit': profit, 'order_id': order_id,
                'session_profit': round(float(STATE.get('profit', 0)), 2)
            })
    except Exception as exc:
        logging.warning('CSV logging error: %s', exc)


def inc(key, amount=1):
    with DIAG_LOCK:
        if key in DIAG:
            DIAG[key] += amount


def diag_notice(asset, message):
    if time.time() - DIAG_LAST_ASSET_LOG.get(asset, 0) >= DIAGNOSTIC_LOG_SECONDS:
        DIAG_LAST_ASSET_LOG[asset] = time.time()
        logging.warning('CANDLE DIAGNOSTIC %s: %s', asset, message)


def format_stats():
    return 'Daily P/L: ${:+.2f}\nWins: {}\nLosses: {}\nResolved trades: {}\nConsecutive losses: {}'.format(
        STATE['profit'], STATE['wins'], STATE['losses'], STATE['resolved'], STATE['consecutive_losses']
    )


def risk_stop_reason():
    profit = round(float(STATE.get('profit', 0)), 2)
    if profit >= SESSION_PROFIT_TARGET:
        return 'Daily take-profit reached (+${:.2f})'.format(SESSION_PROFIT_TARGET)
    if profit <= -SESSION_LOSS_LIMIT:
        return 'Daily loss limit reached (-${:.2f})'.format(SESSION_LOSS_LIMIT)
    if int(STATE.get('resolved', 0)) >= MAX_RESOLVED_TRADES:
        return 'Daily resolved-trade limit reached ({})'.format(MAX_RESOLVED_TRADES)
    if int(STATE.get('consecutive_losses', 0)) >= MAX_CONSECUTIVE_LOSSES:
        return 'Consecutive-loss limit reached ({})'.format(MAX_CONSECUTIVE_LOSSES)
    return None


def stop_if_limit_reached():
    reason = risk_stop_reason()
    if reason and not STOP_EVENT.is_set():
        STOP_EVENT.set()
        STATE['stop_reason'] = reason
        save_state(True)
        logging.warning('%s | %s', reason, format_stats())
        send_telegram('<b>SCANNER STOPPED</b>\n{}\n{}'.format(reason, format_stats()))
    return bool(reason)


def request_next_workflow():
    global NEXT_WORKFLOW_REQUESTED
    if NEXT_WORKFLOW_REQUESTED:
        return
    NEXT_WORKFLOW_REQUESTED = True
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        logging.error('Cannot dispatch next workflow: missing token or repository')
        send_telegram('<b>Next workflow not triggered.</b> Check GITHUB_TOKEN permissions.')
        return
    url = 'https://api.github.com/repos/{}/actions/workflows/{}/dispatches'.format(
        GITHUB_REPOSITORY, GITHUB_WORKFLOW_FILE
    )
    headers = {'Authorization': 'Bearer ' + GITHUB_TOKEN,
               'Accept': 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28'}
    for attempt in range(3):
        try:
            response = requests.post(url, headers=headers, json={'ref': GITHUB_REF_NAME}, timeout=15)
            if response.status_code in (201, 204):
                send_telegram('<b>Next workflow queued.</b> Daily risk totals preserved.')
                return
            logging.warning('Workflow dispatch failed: %s %s', response.status_code, response.text[:200])
        except Exception as exc:
            logging.warning('Workflow dispatch error: %s', exc)
        time.sleep(3)
    send_telegram('<b>WARNING:</b> automatic workflow dispatch failed.')


def classify_real_forex(value):
    """Return a canonical regular forex pair. All OTC symbols are rejected."""
    if not isinstance(value, str):
        return None
    raw = value.strip().upper().split('.')[-1].strip()
    if not raw or 'OTC' in raw:
        return None
    cleaned = re.sub(r'[^A-Z]', '', raw)
    for suffix in ('BINARY', 'TURBO', 'DIGITAL'):
        if cleaned.endswith(suffix):
            cleaned = cleaned[:-len(suffix)]
    if len(cleaned) != 6:
        return None
    base, quote = cleaned[:3], cleaned[3:]
    if base in CURRENCIES and quote in CURRENCIES and base != quote:
        return cleaned
    return None


def add_asset(found, name, active_id=None):
    pair = classify_real_forex(name)
    if not pair:
        return
    active_map = getattr(OP_code, 'ACTIVES', {})
    resolved = None
    for candidate in (active_map.get(name), active_map.get(pair), active_id):
        try:
            if candidate is not None:
                resolved = int(candidate)
                break
        except (TypeError, ValueError, OverflowError):
            continue
    found[pair] = {'name': pair, 'pair': pair, 'otc': False,
                   'market_type': 'normal_forex', 'id': resolved}
    if resolved is not None:
        try:
            OP_code.ACTIVES[pair] = resolved
        except Exception:
            pass


def walk_asset_tree(node, found, parent=None):
    if isinstance(node, dict):
        name = node.get('name') or node.get('active_name') or node.get('symbol')
        active_id = node.get('id') or node.get('active_id') or node.get('activeId')
        if name:
            add_asset(found, str(name), active_id if active_id is not None else parent)
        elif isinstance(parent, str):
            add_asset(found, parent, active_id)
        for key, value in node.items():
            walk_asset_tree(value, found, key)
    elif isinstance(node, list):
        for item in node:
            walk_asset_tree(item, found, parent)


def refresh_assets(force=False):
    global ASSETS, ASSET_REFRESH_AT
    if ASSETS and not force and time.time() - ASSET_REFRESH_AT < ASSET_REFRESH_SECONDS:
        return ASSETS
    found = {}
    try:
        active_map = IQ.get_all_ACTIVES_OPCODE()
        if isinstance(active_map, dict):
            for name, active_id in active_map.items():
                add_asset(found, str(name), active_id)
    except Exception as exc:
        logging.warning('Active-code discovery failed: %s', exc)
    for method_name in ('get_all_init_v2', 'get_all_init'):
        try:
            method = getattr(IQ, method_name, None)
            if callable(method):
                walk_asset_tree(method(), found)
        except Exception as exc:
            logging.warning('%s discovery failed: %s', method_name, exc)
    ASSETS = sorted(found.values(), key=lambda item: item['name'])
    ASSET_REFRESH_AT = time.time()
    logging.info('REAL FOREX MARKETS LOADED | count=%s | OTC excluded', len(ASSETS))
    return ASSETS


def connect_iq():
    global IQ
    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError('Missing IQ_EMAIL or IQ_PASSWORD secret')
    last_error = 'unknown error'
    for attempt in range(1, 6):
        try:
            client = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
            result = client.connect()
            connected = result[0] if isinstance(result, tuple) else bool(result)
            if not connected:
                raise ConnectionError(str(result))
            client.change_balance(BALANCE_MODE)
            time.sleep(1)
            if not client.check_connect():
                raise ConnectionError('Connection dropped after login')
            IQ = client
            logging.info('Connected to IQ Option in PRACTICE mode')
            send_telegram('<b>Scanner connected</b>\nPRACTICE only\nReal forex only; OTC excluded\n'
                          'ALMA 7/12 + Choppiness Index\nStake: ${:.2f}\n{}'.format(STAKE, format_stats()))
            return
        except Exception as exc:
            last_error = str(exc)
            logging.warning('Connect attempt %s/5 failed: %s', attempt, last_error)
            if attempt < 5:
                time.sleep(RECONNECT_SECONDS)
    raise RuntimeError('Could not connect: ' + last_error)


def ensure_connection():
    global IQ, LAST_RECONNECT_ATTEMPT
    with CONNECTION_LOCK:
        try:
            if IQ is not None and IQ.check_connect():
                return True
        except Exception:
            pass
        now = time.time()
        if now - LAST_RECONNECT_ATTEMPT < RECONNECT_SECONDS:
            return False
        LAST_RECONNECT_ATTEMPT = now
        logging.warning('IQ connection unavailable; starting one controlled reconnect')
        try:
            connect_iq()
            refresh_assets(True)
            return True
        except Exception as exc:
            logging.warning('Reconnect failed: %s', exc)
            return False


def safe_get_candles(self, active, interval, count, endtime):
    with BROKER_API_LOCK:
        try:
            active_id = getattr(OP_code, 'ACTIVES', {}).get(active)
            if active_id is None:
                return None
            self.api.candles.candles_data = None
            self.api.getcandles(active_id, interval, count, endtime)
            deadline = time.time() + CANDLE_REQUEST_TIMEOUT
            while time.time() < deadline:
                data = getattr(self.api.candles, 'candles_data', None)
                if isinstance(data, list):
                    return data
                try:
                    if not self.check_connect():
                        return None
                except Exception:
                    return None
                time.sleep(0.1)
        except Exception as exc:
            logging.warning('Candle request error %s: %s', active, exc)
    return None


def disable_digital_open_worker(self):
    logging.info('Disabled unused digital watcher')


try:
    if hasattr(IQ_Option, '_IQ_Option__get_digital_open'):
        IQ_Option._IQ_Option__get_digital_open = disable_digital_open_worker
    IQ_Option.get_candles = safe_get_candles
except Exception as exc:
    logging.warning('Candle patch failed: %s', exc)


def get_closed_candles(asset):
    try:
        raw = IQ.get_candles(asset, CANDLE_SECONDS, CANDLE_REQUEST_COUNT, int(time.time()))
    except Exception as exc:
        inc('empty_candles')
        diag_notice(asset, 'request failed: ' + str(exc))
        return []
    if not isinstance(raw, list):
        inc('empty_candles')
        diag_notice(asset, 'API returned non-list; reconnect will be checked')
        return []
    try:
        raw.sort(key=lambda candle: int(candle.get('from', 0)))
        now = int(time.time())
        closed = [candle for candle in raw
                  if int(candle.get('from', 0)) > 0
                  and int(candle.get('from', 0)) + CANDLE_SECONDS <= now]
    except Exception as exc:
        inc('empty_candles')
        diag_notice(asset, 'bad candle data: ' + str(exc))
        return []
    if not closed:
        inc('empty_candles')
        diag_notice(asset, 'zero fully closed candles')
        return []
    inc('candle_data_ok')
    if len(closed) < CHOP_PERIOD + ALMA_SLOW_PERIOD + 5:
        inc('insufficient_candles')
        diag_notice(asset, '{} closed candles; need at least {}'.format(
            len(closed), CHOP_PERIOD + ALMA_SLOW_PERIOD + 5))
    return closed


def candle_values(candle):
    try:
        return (float(candle['open']),
                float(candle.get('max', candle.get('high'))),
                float(candle.get('min', candle.get('low'))),
                float(candle['close']))
    except (KeyError, TypeError, ValueError):
        return None


def alma_series(values, period, offset=ALMA_OFFSET, sigma=ALMA_SIGMA):
    """Arnaud Legoux Moving Average with standard offset 0.85 and sigma 6."""
    result = [None] * len(values)
    if period < 1 or len(values) < period:
        return result
    center = offset * (period - 1)
    spread = period / sigma
    weights = [math.exp(-((index - center) ** 2) / (2 * spread * spread))
               for index in range(period)]
    weight_sum = sum(weights)
    weights = [weight / weight_sum for weight in weights]
    for index in range(period - 1, len(values)):
        window = values[index - period + 1:index + 1]
        result[index] = sum(value * weight for value, weight in zip(window, weights))
    return result


def choppiness_series(candles, period=CHOP_PERIOD):
    """Standard Choppiness Index; levels 59.8 and 36.2 are applied in the signal rules."""
    result = [None] * len(candles)
    parsed = [candle_values(candle) for candle in candles]
    true_ranges = []
    for index, values in enumerate(parsed):
        if not values:
            true_ranges.append(None)
            continue
        open_price, high, low, close = values
        if index == 0 or not parsed[index - 1]:
            tr = high - low
        else:
            previous_close = parsed[index - 1][3]
            tr = max(high - low, abs(high - previous_close), abs(low - previous_close))
        true_ranges.append(tr)
    if period < 2:
        return result
    for index in range(period - 1, len(candles)):
        window_values = parsed[index - period + 1:index + 1]
        window_tr = true_ranges[index - period + 1:index + 1]
        if any(value is None for value in window_values) or any(value is None for value in window_tr):
            continue
        highest = max(value[1] for value in window_values)
        lowest = min(value[2] for value in window_values)
        price_range = highest - lowest
        tr_sum = sum(window_tr)
        if price_range <= 0 or tr_sum <= 0:
            continue
        ratio = max(tr_sum / price_range, 1.0)
        result[index] = 100.0 * math.log10(ratio) / math.log10(period)
    return result


def evaluate_signal(candles):
    minimum = CHOP_PERIOD + ALMA_SLOW_PERIOD + 5
    if len(candles) < minimum:
        return None
    try:
        closes = [float(candle['close']) for candle in candles]
        candle_time = int(candles[-1].get('from', 0))
    except (KeyError, TypeError, ValueError):
        return None
    fast = alma_series(closes, ALMA_FAST_PERIOD)
    slow = alma_series(closes, ALMA_SLOW_PERIOD)
    chop = choppiness_series(candles)
    index = len(candles) - 1
    previous = index - 1
    if fast[index] is None or slow[index] is None or fast[previous] is None or slow[previous] is None:
        return None
    if chop[index] is None:
        return None

    # ALMA 7 over ALMA 12 = CALL; ALMA 7 under ALMA 12 = PUT.
    # Keeping the line alignment valid allows repeat entries while the signal remains unchanged.
    if fast[index] > slow[index]:
        direction = 'call'
        inc('alma_call')
        # The supplied method names 59.8 as the upper Choppiness level.
        chop_ok = chop[index] < CHOP_UPPER_LEVEL
        chop_rule = 'Choppiness below upper level 59.8'
    elif fast[index] < slow[index]:
        direction = 'put'
        inc('alma_put')
        # The supplied method explicitly says the blue line must be above the green/lower level for PUT.
        chop_ok = chop[index] > CHOP_LOWER_LEVEL
        chop_rule = 'Choppiness above lower level 36.2'
    else:
        return None

    if not chop_ok:
        inc('chop_rejected')
        return None

    # A prior matching Choppiness condition is reported as stronger, but is not required.
    recent = chop[max(0, index - CHOP_RECENT_CANDLES):index]
    if direction == 'put':
        recent_match = any(value is not None and value > CHOP_LOWER_LEVEL for value in recent)
    else:
        recent_match = any(value is not None and value < CHOP_UPPER_LEVEL for value in recent)
    spread = fast[index] - slow[index]
    return {
        'direction': direction,
        'signal_value': spread,
        'candle_time': candle_time,
        'alma_fast': fast[index],
        'alma_slow': slow[index],
        'choppiness': chop[index],
        'chop_rule': chop_rule,
        'recent_chop_confirmation': recent_match,
        'confirmation': 'ALMA 7/12 alignment + {}{}'.format(
            chop_rule, ' (also present in recent candles)' if recent_match else '')
    }


def to_float(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def async_order_net_profit(async_data):
    if not isinstance(async_data, dict):
        return None
    closed = async_data.get('option-closed') or async_data.get('option_closed')
    if not isinstance(closed, dict) or not closed:
        return None
    message = closed.get('msg', closed)
    if not isinstance(message, dict):
        return None
    gross_return = to_float(message.get('profit_amount'))
    stake_amount = to_float(message.get('amount'))
    if gross_return is None or stake_amount is None:
        return None
    return round(gross_return - stake_amount, 8)


def query_order_profit(order_id):
    global ASYNC_API_MISSING_LOGGED
    try:
        with BROKER_API_LOCK:
            client = IQ
            async_method = getattr(client, 'get_async_order', None) if client is not None else None
            if not callable(async_method):
                if not ASYNC_API_MISSING_LOGGED:
                    logging.error('get_async_order is unavailable in the installed iqoptionapi package')
                    ASYNC_API_MISSING_LOGGED = True
                return None
            data = async_method(order_id)
        value = async_order_net_profit(data)
        if value is not None:
            logging.info('CLOSED ORDER PROFIT RECEIVED | order_id=%s | net=%+.2f', order_id, value)
        return value
    except Exception as exc:
        logging.debug('get_async_order poll failed for order %s: %s', order_id, exc)
        return None


def settle_result(asset, direction, broker_profit, signal_value=None, order_id=''):
    if broker_profit is None:
        raise ValueError('Cannot settle without numeric broker result')
    order_key = str(order_id)
    with STATE_LOCK:
        if order_key and order_key in [str(value) for value in STATE.get('settled_order_ids', [])]:
            logging.warning('DUPLICATE SETTLEMENT BLOCKED | order_id=%s', order_key)
            return
        # Preserve the existing accounting convention: broker-reported zero counts as a $1 loss.
        net = -STAKE if float(broker_profit) == 0 else float(broker_profit)
        win = net > 0
        STATE['profit'] = round(float(STATE.get('profit', 0)) + net, 2)
        STATE['resolved'] = int(STATE.get('resolved', 0)) + 1
        if win:
            STATE['wins'] = int(STATE.get('wins', 0)) + 1
            STATE['consecutive_losses'] = 0
            label = 'WIN'
        else:
            STATE['losses'] = int(STATE.get('losses', 0)) + 1
            STATE['consecutive_losses'] = int(STATE.get('consecutive_losses', 0)) + 1
            label = 'LOSS'
        if order_key:
            STATE.setdefault('settled_order_ids', []).append(order_key)
        if str(STATE.get('pending_order_id')) == order_key:
            for key in ('pending_order_id', 'pending_asset', 'pending_direction', 'pending_started'):
                STATE[key] = None
    log_trade(asset, direction, None, label, round(net, 2), order_id)
    save_state(True)
    logging.info('%s %s %s %+.2f | %s', label, asset, direction, net, format_stats())
    send_telegram('<b>Trade result: {}</b>\n{} {}\nBroker: ${:+.2f}\nAccounted: ${:+.2f}\n{}'.format(
        label, asset, direction.upper(), broker_profit, net, format_stats()))
    stop_if_limit_reached()


def wait_result(order_id, asset, direction, signal_value):
    logging.info('RESULT MONITOR ACTIVE | order_id=%s | asset=%s | expiry=%sm | get_async_order only',
                 order_id, asset, EXPIRY_MINUTES)
    last_notice = 0
    while True:
        if not ensure_connection():
            time.sleep(ORDER_RESULT_RETRY_SECONDS)
            continue
        value = query_order_profit(order_id)
        if value is not None:
            settle_result(asset, direction, value, signal_value, order_id)
            return
        if time.time() - last_notice >= 60:
            logging.warning('ASYNC RESULT NOT READY | order_id=%s | keeping pending; new trades blocked', order_id)
            last_notice = time.time()
        time.sleep(ORDER_RESULT_RETRY_SECONDS)


def start_result_monitor(order_id, asset, direction, signal_value=None):
    key = str(order_id)
    with RESULT_MONITOR_LOCK:
        current = RESULT_MONITORS.get(key)
        if current is not None and current.is_alive():
            return
        thread = threading.Thread(target=wait_result,
                                  args=(order_id, asset, direction, signal_value),
                                  name='result-monitor-' + key, daemon=True)
        RESULT_MONITORS[key] = thread
        thread.start()
    logging.info('RESULT MONITOR STARTED | order_id=%s', order_id)


def recover_pending_order():
    order_id = STATE.get('pending_order_id')
    if order_id:
        asset = STATE.get('pending_asset') or 'UNKNOWN'
        direction = STATE.get('pending_direction') or 'unknown'
        logging.warning('Recovering pending order | order_id=%s | asset=%s', order_id, asset)
        start_result_monitor(order_id, asset, direction, None)


def place_trade(item, signal):
    asset = item['name']
    direction = signal['direction']
    with TRADE_LOCK:
        if STOP_EVENT.is_set() or stop_if_limit_reached() or STATE.get('pending_order_id'):
            logging.warning('ORDER SKIPPED | %s %s | stopped or another order is pending', asset, direction)
            return
        active_id = item.get('id')
        if active_id is not None:
            try:
                OP_code.ACTIVES[asset] = int(active_id)
            except Exception:
                pass
        if asset not in getattr(OP_code, 'ACTIVES', {}):
            inc('order_rejected')
            log_trade(asset, direction, None, 'ORDER_REJECTED_NO_ACTIVE_ID')
            logging.error('ORDER REJECTED | missing active ID for %s', asset)
            REJECTED_ASSETS[asset] = time.time() + REJECTED_ASSET_COOLDOWN_SECONDS
            return
        if not ensure_connection():
            logging.warning('ORDER NOT SENT | connection unavailable for %s', asset)
            return
        logging.info('ORDER SUBMIT BEGIN | PRACTICE | asset=%s | direction=%s | stake=$%.2f | expiry=%sm',
                     asset, direction.upper(), STAKE, EXPIRY_MINUTES)
        try:
            result = IQ.buy(STAKE, asset, direction, EXPIRY_MINUTES)
        except Exception as exc:
            log_trade(asset, direction, None, 'ORDER_ERROR')
            logging.exception('ORDER SUBMIT EXCEPTION | %s | %s', asset, exc)
            send_telegram('<b>Order submission error</b>\n{} {}\nNo automatic retry was made.'.format(
                asset, direction.upper()))
            return
        logging.info('ORDER SUBMIT RESPONSE | asset=%s | response=%r', asset, result)
        success, order_id, reason = False, None, None
        if isinstance(result, (tuple, list)):
            success = bool(result[0]) if result else False
            order_id = result[1] if len(result) > 1 else None
            if not success:
                reason = order_id
        else:
            success = bool(result)
            order_id = result if success else None
        if not success or order_id is None or order_id is False:
            REJECTED_ASSETS[asset] = time.time() + REJECTED_ASSET_COOLDOWN_SECONDS
            inc('order_rejected')
            log_trade(asset, direction, None, 'ORDER_REJECTED')
            logging.error('ORDER REJECTED | asset=%s | response=%r | reason=%r', asset, result, reason)
            return
        with STATE_LOCK:
            STATE['pending_order_id'] = order_id
            STATE['pending_asset'] = asset
            STATE['pending_direction'] = direction
            STATE['pending_started'] = now_lagos().isoformat(timespec='seconds')
        save_state(True)
        log_trade(asset, direction, None, 'OPENED', '', order_id)
        logging.info('ORDER OPENED | asset=%s | direction=%s | order_id=%s', asset, direction.upper(), order_id)
        send_telegram('<b>Practice trade opened</b>\n{} {}\nOrder ID: {}\nStake: ${:.2f}\n'
                      'ALMA 7: {:.8f}\nALMA 12: {:.8f}\nChoppiness: {:.2f}\n{}'.format(
                          asset, direction.upper(), order_id, STAKE, signal['alma_fast'],
                          signal['alma_slow'], signal['choppiness'], signal['confirmation']))
    start_result_monitor(order_id, asset, direction, signal['signal_value'])


def report_diagnostics():
    with DIAG_LOCK:
        values = dict(DIAG)
        for key in DIAG:
            DIAG[key] = 0
    logging.info('SCAN DIAGNOSTICS | scanned=%s candle_ok=%s empty=%s insufficient=%s evaluated=%s '
                 'alma_call=%s alma_put=%s chop_rejected=%s setups=%s duplicates=%s order_rejected=%s',
                 *[values[key] for key in DIAG_KEYS])


def main():
    connect_iq()
    refresh_assets(True)
    if not ASSETS:
        raise RuntimeError('No supported regular forex assets discovered; OTC is excluded')
    if stop_if_limit_reached():
        return
    if STATE.get('pending_order_id'):
        recover_pending_order()
        logging.warning('Previous order unresolved; new orders blocked until its result is confirmed.')
    send_telegram('<b>Scanner started</b>\nPRACTICE only\nReal forex only; OTC excluded\n'
                  'ALMA 7/12 + Choppiness period 25, levels 59.8/36.2\n{}'.format(format_stats()))
    heartbeat = time.time()
    diagnostic = time.time()
    while not STOP_EVENT.is_set():
        if stop_if_limit_reached():
            break
        if not ensure_connection():
            time.sleep(1)
            continue
        if time.time() - ASSET_REFRESH_AT >= ASSET_REFRESH_SECONDS:
            refresh_assets(True)
        if time.time() - heartbeat >= HEARTBEAT_SECONDS:
            logging.info('HEARTBEAT | %s | assets=%s | %s',
                         now_lagos().strftime('%Y-%m-%d %H:%M:%S WAT'), len(ASSETS), format_stats())
            send_telegram('<b>Scanner heartbeat</b>\n{}\n{}'.format(
                now_lagos().strftime('%Y-%m-%d %H:%M:%S WAT'), format_stats()))
            heartbeat = time.time()
        if time.time() - diagnostic >= DIAGNOSTIC_LOG_SECONDS:
            report_diagnostics()
            diagnostic = time.time()
        if STATE.get('pending_order_id'):
            time.sleep(ORDER_RESULT_RETRY_SECONDS)
            continue
        for item in list(ASSETS):
            if STOP_EVENT.is_set() or stop_if_limit_reached():
                break
            asset = item['name']
            try:
                inc('assets_scanned')
                cooldown = REJECTED_ASSETS.get(asset, 0)
                if time.time() < cooldown:
                    continue
                if cooldown:
                    REJECTED_ASSETS.pop(asset, None)
                candles = get_closed_candles(asset)
                if len(candles) < CHOP_PERIOD + ALMA_SLOW_PERIOD + 5:
                    if not ensure_connection():
                        break
                    continue
                inc('signals_checked')
                signal = evaluate_signal(candles)
                if not signal:
                    continue
                inc('setups_found')
                signal_key = str(signal['candle_time'])
                last_signals = STATE.setdefault('last_signals', {})
                if last_signals.get(asset) == signal_key:
                    inc('duplicate_signals')
                    continue
                last_signals[asset] = signal_key
                save_state()
                log_trade(asset, signal['direction'], None, 'SIGNAL')
                logging.info('CONFIRMED %s [%s] %s | ALMA7=%.8f ALMA12=%.8f CHOP=%.2f | %s',
                             asset, item.get('market_type', 'normal_forex'), signal['direction'],
                             signal['alma_fast'], signal['alma_slow'], signal['choppiness'], signal['confirmation'])
                send_telegram('<b>Confirmed setup</b>\n{} {}\nMarket: real forex\n'
                              'ALMA 7: {:.8f}\nALMA 12: {:.8f}\nChoppiness: {:.2f}\n{}'.format(
                                  asset, signal['direction'].upper(), signal['alma_fast'],
                                  signal['alma_slow'], signal['choppiness'], signal['confirmation']))
                if AUTO_TRADE:
                    place_trade(item, signal)
            except Exception as exc:
                logging.warning('Scan error for %s: %s', asset, exc)
            time.sleep(0.15)
        time.sleep(SCAN_INTERVAL)
    reason = STATE.get('stop_reason') or risk_stop_reason() or 'Scanner stopped'
    logging.info('Finished: %s | %s', reason, format_stats())
    send_telegram('<b>Scanner finished</b>\n{}\n{}'.format(reason, format_stats()))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        STOP_EVENT.set()
        save_state(True)
        logging.info('Stopped by keyboard interrupt')
    except Exception as exc:
        logging.exception('Fatal scanner error')
        send_telegram('<b>Scanner error</b>\n{}'.format(exc))
        save_state(True)
        raise
