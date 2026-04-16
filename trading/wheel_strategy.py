"""
Core wheel strategy logic.

Stage 1 — Sell a cash-secured put ~10% below market price (2-4 week expiry).
  • Put expires worthless  → keep premium, sell another put (repeat Stage 1).
  • Put is assigned        → receive shares, move to Stage 2.

Stage 2 — Sell a covered call ~10% above cost basis (2-4 week expiry).
  • Call expires worthless → keep premium, sell another call (repeat Stage 2).
  • Call is exercised      → shares sold, full cycle complete, restart Stage 1.

Extra rules (from strategy spec):
  • Never sell a put without enough cash to cover full assignment.
  • Never sell a call with a strike below cost basis.
  • Close any option early once it reaches 50 % of maximum possible profit.
  • Track all premiums across cycles.
  • Generate a daily summary at market close.
"""

import logging
from datetime import date, timedelta
from typing import Optional

from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest, StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    AssetClass,
    OptionType,
    OrderSide,
    QueryOrderStatus,
    TimeInForce,
)
from alpaca.trading.requests import (
    GetOptionContractsRequest,
    GetOrdersRequest,
    LimitOrderRequest,
)

from state_manager import StateManager, WheelStage

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Strategy constants
# ------------------------------------------------------------------
PUT_STRIKE_FACTOR = 0.90    # sell put ~10 % below current price
CALL_STRIKE_FACTOR = 1.10   # sell call ~10 % above cost basis
MIN_DTE = 14                # minimum days to expiration
MAX_DTE = 28                # maximum days to expiration
EARLY_CLOSE_THRESHOLD = 0.50  # close option when 50 % of premium is locked in
CONTRACTS = 1               # number of option contracts per trade
SHARES_PER_CONTRACT = 100
STRIKE_SEARCH_BAND = 0.05   # ±5 % around target strike when searching contracts


class WheelStrategy:
    def __init__(self, api_key: str, api_secret: str, base_url: str, ticker: str):
        self.ticker = ticker
        self.state_mgr = StateManager()

        paper = "paper" in base_url.lower()
        self.trading = TradingClient(api_key, api_secret, paper=paper)
        self.stock_data = StockHistoricalDataClient(api_key, api_secret)
        self.option_data = OptionHistoricalDataClient(api_key, api_secret)

        # Initialise ticker on state if this is a fresh start
        if self.state_mgr.state.ticker != ticker:
            self.state_mgr.update(ticker=ticker)

        logger.info("WheelStrategy ready: ticker=%s paper=%s stage=%s",
                    ticker, paper, self.state_mgr.stage.value)

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_stock_price(self) -> float:
        req = StockLatestQuoteRequest(symbol_or_symbols=[self.ticker])
        quote = self.stock_data.get_stock_latest_quote(req)[self.ticker]
        mid = (quote.ask_price + quote.bid_price) / 2
        logger.info("%s mid price: $%.2f", self.ticker, mid)
        return mid

    def get_option_mid_price(self, symbol: str) -> Optional[float]:
        try:
            req = OptionLatestQuoteRequest(symbol_or_symbols=[symbol])
            quote = self.option_data.get_option_latest_quote(req)[symbol]
            if quote.ask_price and quote.bid_price:
                return (quote.ask_price + quote.bid_price) / 2
        except Exception as exc:
            logger.error("Could not fetch quote for %s: %s", symbol, exc)
        return None

    # ------------------------------------------------------------------
    # Option contract selection
    # ------------------------------------------------------------------

    def find_contract(self, option_type: OptionType, target_strike: float) -> Optional[object]:
        today = date.today()
        low = target_strike * (1 - STRIKE_SEARCH_BAND)
        high = target_strike * (1 + STRIKE_SEARCH_BAND)

        req = GetOptionContractsRequest(
            underlying_symbols=[self.ticker],
            expiration_date_gte=today + timedelta(days=MIN_DTE),
            expiration_date_lte=today + timedelta(days=MAX_DTE),
            strike_price_gte=round(low, 2),
            strike_price_lte=round(high, 2),
            type=option_type,
            status="active",
        )
        try:
            result = self.trading.get_option_contracts(req)
            contracts = result.option_contracts
            if not contracts:
                logger.warning("No %s contracts near $%.2f (±5%%)", option_type.value, target_strike)
                return None
            # Prefer the strike closest to target; break ties by earliest expiry
            best = min(contracts,
                       key=lambda c: (abs(float(c.strike_price) - target_strike), c.expiration_date))
            logger.info("Selected %s contract: %s  strike=$%s  exp=%s",
                        option_type.value, best.symbol, best.strike_price, best.expiration_date)
            return best
        except Exception as exc:
            logger.error("Error fetching option contracts: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Account / position helpers
    # ------------------------------------------------------------------

    def get_cash(self) -> float:
        return float(self.trading.get_account().cash)

    def get_stock_position(self) -> Optional[object]:
        try:
            return self.trading.get_open_position(self.ticker)
        except Exception:
            return None

    def get_open_option_positions(self) -> list:
        positions = self.trading.get_all_positions()
        return [p for p in positions if p.asset_class == AssetClass.US_OPTION]

    def get_open_option_orders(self) -> list:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        orders = self.trading.get_orders(req)
        # Filter to option orders (symbol length > 10 is a reliable heuristic for OCC symbols)
        return [o for o in orders if len(o.symbol) > 10]

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    def _sell_to_open(self, symbol: str, limit_price: float) -> Optional[object]:
        order = LimitOrderRequest(
            symbol=symbol,
            qty=CONTRACTS,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            limit_price=round(limit_price, 2),
        )
        try:
            result = self.trading.submit_order(order)
            logger.info("Sell-to-open submitted: %s @ $%.2f  id=%s", symbol, limit_price, result.id)
            return result
        except Exception as exc:
            logger.error("Failed to submit sell order for %s: %s", symbol, exc)
            return None

    def _buy_to_close(self, symbol: str, limit_price: float) -> Optional[object]:
        order = LimitOrderRequest(
            symbol=symbol,
            qty=CONTRACTS,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=round(limit_price * 1.05, 2),  # 5 % above mid to improve fill probability
        )
        try:
            result = self.trading.submit_order(order)
            logger.info("Buy-to-close submitted: %s @ $%.2f  id=%s", symbol, limit_price, result.id)
            return result
        except Exception as exc:
            logger.error("Failed to submit buy-to-close for %s: %s", symbol, exc)
            return None

    # ------------------------------------------------------------------
    # Stage 1: sell cash-secured put
    # ------------------------------------------------------------------

    def execute_sell_put(self):
        logger.info("=== Stage 1: Sell cash-secured put ===")
        stock_price = self.get_stock_price()
        target_strike = round(stock_price * PUT_STRIKE_FACTOR)

        required_cash = target_strike * SHARES_PER_CONTRACT
        available_cash = self.get_cash()
        if available_cash < required_cash:
            logger.warning(
                "Insufficient cash to cover put assignment. "
                "Need $%.2f, have $%.2f. Skipping.", required_cash, available_cash
            )
            return

        contract = self.find_contract(OptionType.PUT, target_strike)
        if not contract:
            return

        mid = self.get_option_mid_price(contract.symbol)
        if not mid or mid < 0.05:
            logger.warning("Put mid price too low ($%s). Skipping.", mid)
            return

        order = self._sell_to_open(contract.symbol, mid)
        if order:
            premium = mid * SHARES_PER_CONTRACT
            self.state_mgr.update(
                stage=WheelStage.SELL_PUT,
                current_option_symbol=contract.symbol,
                current_option_sell_price=mid,
                put_premium_this_cycle=premium,
            )
            logger.info(
                "Put sold: %s  strike=$%s  premium=$%.2f/share  ($%.2f total)",
                contract.symbol, contract.strike_price, mid, premium,
            )

    # ------------------------------------------------------------------
    # Stage 2: sell covered call
    # ------------------------------------------------------------------

    def execute_sell_call(self):
        logger.info("=== Stage 2: Sell covered call ===")
        cost_basis = self.state_mgr.state.cost_basis_per_share

        if cost_basis <= 0:
            pos = self.get_stock_position()
            if pos:
                cost_basis = float(pos.avg_entry_price)
                self.state_mgr.update(cost_basis_per_share=cost_basis)
            else:
                logger.error("No stock position found; cannot sell covered call.")
                return

        target_strike = round(cost_basis * CALL_STRIKE_FACTOR)

        contract = self.find_contract(OptionType.CALL, target_strike)
        if not contract:
            return

        # Hard safety: never sell a call below cost basis
        if float(contract.strike_price) < cost_basis:
            logger.error(
                "Contract strike $%s is below cost basis $%.2f. "
                "Refusing to sell call — would realise a loss.",
                contract.strike_price, cost_basis,
            )
            return

        mid = self.get_option_mid_price(contract.symbol)
        if not mid or mid < 0.05:
            logger.warning("Call mid price too low ($%s). Skipping.", mid)
            return

        order = self._sell_to_open(contract.symbol, mid)
        if order:
            premium = mid * SHARES_PER_CONTRACT
            state = self.state_mgr.state
            self.state_mgr.update(
                stage=WheelStage.SELL_CALL,
                current_option_symbol=contract.symbol,
                current_option_sell_price=mid,
                call_premium_this_cycle=state.call_premium_this_cycle + premium,
                total_premiums_collected=state.total_premiums_collected + premium,
            )
            logger.info(
                "Call sold: %s  strike=$%s  premium=$%.2f/share  ($%.2f total)",
                contract.symbol, contract.strike_price, mid, premium,
            )

    # ------------------------------------------------------------------
    # 15-minute monitor: early close check + state reconciliation
    # ------------------------------------------------------------------

    def monitor_positions(self):
        logger.info("--- monitor tick (stage=%s) ---", self.state_mgr.stage.value)
        state = self.state_mgr.state

        # 1. Check whether the open option has hit the 50 % profit target
        if state.current_option_symbol and state.current_option_sell_price > 0:
            self._check_early_close(state.current_option_symbol, state.current_option_sell_price)

        # 2. Reconcile real broker state → advance the state machine if needed
        self._reconcile_state()

    def _check_early_close(self, symbol: str, sell_price: float):
        current = self.get_option_mid_price(symbol)
        if current is None:
            return

        profit_pct = 1.0 - (current / sell_price)
        logger.info(
            "Option %s: sold @ $%.2f  now @ $%.2f  (%.1f%% profit captured)",
            symbol, sell_price, current, profit_pct * 100,
        )

        if profit_pct >= EARLY_CLOSE_THRESHOLD:
            logger.info("50%% profit target reached on %s — closing early.", symbol)
            order = self._buy_to_close(symbol, current)
            if order:
                realized = (sell_price - current) * SHARES_PER_CONTRACT
                logger.info("Early close executed. Locked in ~$%.2f profit.", realized)
                self.state_mgr.update(
                    current_option_symbol="",
                    current_option_sell_price=0.0,
                )

    def _reconcile_state(self):
        """
        Compare expected state with the real broker state and trigger the
        next action when a transition is detected (assignment, expiry, exercise).
        """
        state = self.state_mgr.state
        stock_pos = self.get_stock_position()
        option_positions = self.get_open_option_positions()
        option_orders = self.get_open_option_orders()

        has_shares = stock_pos is not None and int(float(stock_pos.qty)) >= SHARES_PER_CONTRACT
        has_open_option = len(option_positions) > 0 or len(option_orders) > 0

        if state.stage == WheelStage.IDLE:
            if not has_shares and not has_open_option:
                logger.info("IDLE — starting wheel. Selling first put.")
                self.execute_sell_put()

        elif state.stage == WheelStage.SELL_PUT:
            if has_shares and not has_open_option:
                # Put was assigned: received shares
                cost_basis = float(stock_pos.avg_entry_price)
                put_prem_per_share = state.put_premium_this_cycle / SHARES_PER_CONTRACT
                effective_cost = cost_basis - put_prem_per_share
                logger.info(
                    "Put assigned! Received %d shares @ $%.2f  "
                    "(effective $%.2f after premium)",
                    SHARES_PER_CONTRACT, cost_basis, effective_cost,
                )
                self.state_mgr.update(
                    stage=WheelStage.HAVE_SHARES,
                    shares_owned=SHARES_PER_CONTRACT,
                    cost_basis_per_share=effective_cost,
                    current_option_symbol="",
                    current_option_sell_price=0.0,
                    total_premiums_collected=state.total_premiums_collected + state.put_premium_this_cycle,
                )
                self.execute_sell_call()

            elif not has_open_option:
                # Put expired worthless — no assignment
                logger.info(
                    "Put expired worthless. Collected $%.2f premium. Restarting Stage 1.",
                    state.put_premium_this_cycle,
                )
                self.state_mgr.update(
                    stage=WheelStage.IDLE,
                    current_option_symbol="",
                    current_option_sell_price=0.0,
                    put_premium_this_cycle=0.0,
                    call_premium_this_cycle=0.0,
                    total_premiums_collected=state.total_premiums_collected + state.put_premium_this_cycle,
                )
                self.execute_sell_put()

        elif state.stage == WheelStage.HAVE_SHARES:
            if has_shares and not has_open_option:
                logger.info("HAVE_SHARES — selling covered call.")
                self.execute_sell_call()

        elif state.stage == WheelStage.SELL_CALL:
            if not has_shares and not has_open_option:
                # Shares were called away: full cycle complete
                cycle_profit = state.put_premium_this_cycle + state.call_premium_this_cycle
                logger.info(
                    "Shares called away! Cycle %d complete. "
                    "Cycle profit: $%.2f  Total premiums: $%.2f",
                    state.cycle_count + 1, cycle_profit, state.total_premiums_collected,
                )
                self.state_mgr.update(
                    stage=WheelStage.IDLE,
                    shares_owned=0,
                    cost_basis_per_share=0.0,
                    current_option_symbol="",
                    current_option_sell_price=0.0,
                    put_premium_this_cycle=0.0,
                    call_premium_this_cycle=0.0,
                    cycle_count=state.cycle_count + 1,
                )
                self.execute_sell_put()

            elif has_shares and not has_open_option:
                # Call expired worthless — keep shares, sell another call
                logger.info(
                    "Call expired worthless. Collected $%.2f call premium. Selling another call.",
                    state.call_premium_this_cycle,
                )
                self.state_mgr.update(
                    current_option_symbol="",
                    current_option_sell_price=0.0,
                )
                self.execute_sell_call()

    # ------------------------------------------------------------------
    # Daily summary (logged at market close)
    # ------------------------------------------------------------------

    def generate_daily_summary(self):
        state = self.state_mgr.state
        account = self.trading.get_account()
        stock_pos = self.get_stock_position()

        lines = [
            "=" * 55,
            f"  WHEEL STRATEGY DAILY SUMMARY — {date.today()}",
            "=" * 55,
            f"  Ticker            : {self.ticker}",
            f"  Stage             : {state.stage.value}",
            f"  Cycles completed  : {state.cycle_count}",
            f"  Total premiums    : ${state.total_premiums_collected:,.2f}",
            f"  Portfolio value   : ${float(account.portfolio_value):,.2f}",
            f"  Cash available    : ${float(account.cash):,.2f}",
        ]

        if stock_pos:
            lines.append(
                f"  Stock position    : {int(float(stock_pos.qty))} shares "
                f"@ ${float(stock_pos.avg_entry_price):.2f}  "
                f"(P&L: ${float(stock_pos.unrealized_pl):+,.2f})"
            )

        if state.current_option_symbol:
            current_opt_price = self.get_option_mid_price(state.current_option_symbol)
            lines.append(f"  Open option       : {state.current_option_symbol}")
            lines.append(f"    Sold @ ${state.current_option_sell_price:.2f}/share")
            if current_opt_price:
                profit_pct = (1 - current_opt_price / state.current_option_sell_price) * 100
                lines.append(f"    Now  @ ${current_opt_price:.2f}/share  ({profit_pct:.1f}% profit captured)")

        lines.append("=" * 55)
        summary = "\n".join(lines)
        logger.info("\n%s", summary)
        self.state_mgr.update(last_summary_date=str(date.today()))
        return summary
