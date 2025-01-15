"""
Anything with resources goes here
"""
import logging
import re
import time

from core.extractors import Extractor


class PremiumExchange:
    """
    Logic for interaction with the premium exchange
    """

    def __init__(self, wrapper, stock: dict, capacity: dict, tax: dict, constants: dict, duration: int, merchants: int):
        self.wrapper = wrapper
        self.stock = stock
        self.capacity = capacity
        self.tax = tax
        self.constants = constants
        self.duration = duration
        self.merchants = merchants

    # do not call this anihilation (calculate_cost) - i dechipered it from tribalwars js
    def calculate_cost(self, item, a):
        """
        Stock exchange cost calculation
        """
        t = self.stock[item]
        n = self.capacity[item]

        # tax = self.tax["buy"] if a >= 0 else self.tax["sell"]
        tax = self.tax["sell"]  # twb never buys on premium exchange

        return (1 + tax) * (self.calculate_marginal_price(t, n) + self.calculate_marginal_price(t - a, n)) * a / 2

    def calculate_marginal_price(self, e, a):
        """
        Math magic
        """
        c = self.constants
        return c["resource_base_price"] - c["resource_price_elasticity"] * e / (a + c["stock_size_modifier"])

    def calculate_rate_for_one_point(self, item: str):
        """
        Math magic
        """
        a = self.stock[item]
        t = self.capacity[item]
        n = self.calculate_marginal_price(a, t)
        r = int(1 / n)
        c = self.calculate_cost(item, r)
        i = 0

        while c > 1 and i < 50:
            r -= 1
            i += 1
            c = self.calculate_cost(item, r)

        return r

    @staticmethod
    def optimize_n(amount, sell_price, merchants, size=1000):
        """
        Math magic
        """
        def _ratio(a, b, size=1000):
            a = (size * b) - a
            return a / size

        offers = []

        for i in range(1, merchants + 1):
            for j in range(amount // sell_price + 1):
                r = _ratio(j * sell_price, i, size=size)
                if r >= 0:
                    offers.append((i, r, j))

        offers.sort(key=lambda x: (x[1], -x[0]))

        r = {
            "merchants": offers[0][0],
            "ratio": offers[0][1],
            "n_to_sell": offers[0][2]-1
        }

        return r


class ResourceManager:
    """
    Class to calculate, store and reserve resources for actions
    """
    actual = {}

    requested = {}

    storage = 0
    ratio = 2.5
    max_trade_amount = 4000
    logger = None
    # not allowed to bias
    trade_bias = 1
    last_trade = 0
    trade_max_per_hour = 1
    trade_max_duration = 2
    wrapper = None
    village_id = None
    do_premium_trade = False

    def __init__(self, wrapper=None, village_id=None):
        """
        Create the resource manager
        Preferably used by anything that builds/recruits/sends/whatever
        """
        self.wrapper = wrapper
        self.village_id = village_id

    def update(self, game_state):
        """
        Update the current resources based on the game state
        """
        self.actual["wood"] = game_state["village"]["wood"]
        self.actual["stone"] = game_state["village"]["stone"]
        self.actual["iron"] = game_state["village"]["iron"]
        self.actual["pop"] = (
                game_state["village"]["pop_max"] - game_state["village"]["pop"]
        )
        self.storage = game_state["village"]["storage_max"]
        self.check_state()
        store_state = game_state["village"]["name"]
        self.logger = logging.getLogger(f"Resource Manager: {store_state}")

    def do_premium_stuff(self):
        """
        Does premium stuff.

        NEW LOGIC:
        - If do_premium_trade is True, attempt to sell all excess resources.
        - 'Excess' is determined by: (sum of wood+stone+iron) - (merchants_available * 1000)
          divided by 3.
        - For each resource: if its current amount is greater than the baseline, we sell the difference.
        - Trade sequentially: after a successful trade, update market data and then proceed with the next resource.
        """
        if not self.do_premium_trade:
            self.logger.debug("Premium trading not enabled.")
            return

        # Get the current premium market data.
        url = f"game.php?village={self.village_id}&screen=market&mode=exchange"
        res = self.wrapper.get_url(url=url)
        data = Extractor.premium_data(res.text)
        if not data:
            self.logger.warning("Error reading premium data!")
            return

        # Calculate the baseline for each resource:
        #  total_available_in_resources - (total merchants available * 1000) divided by 3.
        # (Assuming resources are wood, stone, iron)
        available_merchants = data["merchants"]
        if available_merchants < 1:
            self.logger.info("No merchants available for premium trade!")
            return

        total_resources = self.actual.get("wood", 0) + self.actual.get("stone", 0) + self.actual.get("iron", 0)
        total_tradable_capacity = available_merchants * 1000
        # If total_resources is less than or equal to trade capacity, no excess exists.


        baseline = max((total_resources - total_tradable_capacity) // 3, 0)
        self.logger.debug("Premium trade baseline per resource: %d", baseline)

        # Choose an order for resources - for example: wood, iron, stone.
        resource_order = ["stone", "wood", "iron"]

        for resource in resource_order:
            # Refresh market data each time
            url = f"game.php?village={self.village_id}&screen=market&mode=exchange"
            res = self.wrapper.get_url(url=url)
            data = Extractor.premium_data(res.text)
            if not data:
                self.logger.warning("Error reading premium data on refresh!")
                return

            available_merchants = data["merchants"]
            if available_merchants < 1:
                self.logger.info("No more merchants available!")
                return

            # Determine how many units of the resource we can sell.
            current = self.actual.get(resource, 0)
            if current <= baseline:
                self.logger.debug("Resource %s is not in excess (current: %d, baseline: %d)", resource, current, baseline)
                continue

            excess = current - baseline

            # But since one merchant can only trade up to 1000 units, we limit sell amount accordingly.
            max_sell_possible = available_merchants * 1000
            if excess > max_sell_possible:
                excess = max_sell_possible

            # Use existing logic for price calculation: determine the cost per premium point.
            cost_per_point = data and PremiumExchange(
                wrapper=self.wrapper,
                stock=data["stock"],
                capacity=data["capacity"],
                tax=data["tax"],
                constants=data["constants"],
                duration=data["duration"],
                merchants=available_merchants
            ).calculate_rate_for_one_point(resource)

            self.logger.debug("For resource %s, calculated cost per point: %s", resource, cost_per_point)
            self.logger.info("Current %s amount: %d; excess to sell: %d", resource, current, excess)

            # Using the existing logic from before:
            # For a trade to be worthwhile we check that (price from premium market * 1.1) is less than our resource count.
            prices = {}
            for p in ["wood", "stone", "iron"]:
                prices[p] = data["stock"][p] * data["rates"][p]
            if resource not in prices or prices[resource] * 1.1 >= current:
                self.logger.info("Not a good moment to trade %s", resource)
                continue

            # Calculate optimal trade amounts using existing function optimize_n.
            opt_trade = PremiumExchange.optimize_n(
                amount=excess,
                sell_price=cost_per_point,
                merchants=available_merchants,
                size=1000
            )
            self.logger.debug("Optimized trade for %s: %s", resource, opt_trade)
            if opt_trade["ratio"] > 0.4:
                self.logger.info("Trade not worth trading for %s (ratio too high: %s)", resource, opt_trade["ratio"])
                continue

            sell_amount = int(opt_trade["n_to_sell"] * cost_per_point)
            # Do not attempt trades of negligible amounts.
            if sell_amount < 1:
                self.logger.info("Calculated sell amount for %s is too low: %d", resource, sell_amount)
                continue

            # Start the trade (similar to original logic)
            self.logger.info("Attempting trade of %d %s for premium point", sell_amount, resource)
            trade_begin_url = f"game.php?village={self.village_id}&screen=market&mode=exchange"
            trade_begin_res = self.wrapper.get_url(url=trade_begin_url)
            # Assuming premium_data still valid
            result = self.wrapper.get_api_action(
                self.village_id,
                action="exchange_begin",
                params={"screen": "market"},
                data={f"sell_{resource}": sell_amount}
            )

            if result:
                _rate_hash = result["response"][0]["rate_hash"]
                trade_data = {
                    f"sell_{resource}": sell_amount,
                    "rate_hash": _rate_hash,
                    "mb": "1"
                }
                confirm_res = self.wrapper.get_api_action(
                    self.village_id,
                    action="exchange_confirm",
                    params={"screen": "market"},
                    data=trade_data,
                )
                if confirm_res:
                    self.logger.info("Trade for %s successful!", resource)
                    # Update our local resource amount to reflect the sale.
                    self.actual[resource] -= sell_amount
                    # Wait a short while before checking next resource (or re-fetch market data).
                    time.sleep(1)
                    # Continue with next resource.
                else:
                    self.logger.info("Trade confirmation for %s failed.", resource)
            else:
                self.logger.debug("Exchange begin for %s failed.", resource)
                self.logger.info("Trade failed for %s.", resource)


    def check_state(self):
        """
        Removes resource requests when the amount is met
        """
        for source in self.requested:
            for res in self.requested[source]:
                if self.requested[source][res] <= self.actual[res]:
                    self.requested[source][res] = 0

    def request(self, source="building", resource="wood", amount=1):
        """
        When called, resources can be taken from other actions

        """
        if source in self.requested:
            self.requested[source][resource] = amount
        else:
            self.requested[source] = {resource: amount}

    def can_recruit(self):
        """
        Checks of population is sufficient for recruitment
        """
        if self.actual["pop"] == 0:
            self.logger.info("Can't recruit, no room for pops!")
            for x in self.requested:
                if "recruitment" in x:
                    del self.requested[x]
            return False

        for x in self.requested:
            if "recruitment" in x:
                continue
            types = self.requested[x]
            for sub in types:
                if types[sub] > 0:
                    return False
        return True

    def get_plenty_off(self):
        """
        Checks of there is overcapacity in a village
        """
        most_of = 0
        most = None
        for sub in self.actual:
            f = 1
            for sr in self.requested:
                if sub in self.requested[sr] and self.requested[sr][sub] > 0:
                    f = 0
            if not f:
                continue
            if sub == "pop":
                continue
            # self.logger.debug(f"We have {self.actual[sub]} {sub}. Enough? {self.actual[sub]} > {int(self.storage / self.ratio)}")
            if self.actual[sub] > int(self.storage / self.ratio):
                if self.actual[sub] > most_of:
                    most = sub
                    most_of = self.actual[sub]
        if most:
            self.logger.debug(f"We have plenty of {most}")

        return most

    def in_need_of(self, obj_type):
        """
        Checks if the village lacks a certain resource
        """
        for x in self.requested:
            types = self.requested[x]
            if obj_type in types and self.requested[x][obj_type] > 0:
                return True
        return False

    def in_need_amount(self, obj_type):
        """
        Checks what would be needed in order to match requirements
        """
        amount = 0
        for x in self.requested:
            types = self.requested[x]
            if obj_type in types and self.requested[x][obj_type] > 0:
                amount += self.requested[x][obj_type]
        return amount

    def get_needs(self):
        """
        All of the above
        """
        needed_the_most = None
        needed_amount = 0
        for x in self.requested:
            types = self.requested[x]
            for obj_type in types:
                if (
                        self.requested[x][obj_type] > 0
                        and self.requested[x][obj_type] > needed_amount
                ):
                    needed_amount = self.requested[x][obj_type]
                    needed_the_most = obj_type
        if needed_the_most:
            return needed_the_most, needed_amount
        return None

    def trade(self, me_item, me_amount, get_item, get_amount):
        """
        Creates a new trading offer
        """
        url = f"game.php?village={self.village_id}&screen=market&mode=own_offer"
        res = self.wrapper.get_url(url=url)
        if 'market_merchant_available_count">0' in res.text:
            self.logger.debug("Not trading because not enough merchants available")
            return False
        payload = {
            "res_sell": me_item,
            "sell": me_amount,
            "res_buy": get_item,
            "buy": get_amount,
            "max_time": self.trade_max_duration,
            "multi": 1,
            "h": self.wrapper.last_h,
        }
        post_url = f"game.php?village={self.village_id}&screen=market&mode=own_offer&action=new_offer"
        self.wrapper.post_url(post_url, data=payload)
        self.last_trade = int(time.time())
        return True

    def drop_existing_trades(self):
        """
        Removes an existing trade if resources are needed elsewhere or it expired
        """
        url = f"game.php?village={self.village_id}&screen=market&mode=all_own_offer"
        data = self.wrapper.get_url(url)
        existing = re.findall(r'data-id="(\d+)".+?data-village="(\d+)"', data.text)
        for entry in existing:
            offer, village = entry
            if village == str(self.village_id):
                post_url = f"game.php?village={self.village_id}&screen=market&mode=all_own_offer&action=delete_offers"
                post = {
                    "id_%s" % offer: "on",
                    "delete": "Verwijderen",
                    "h": self.wrapper.last_h,
                }
                self.wrapper.post_url(url=post_url, data=post)
                self.logger.info(
                    "Removing offer %s from market because it existed too long" % offer
                )

    def readable_ts(self, seconds):
        """
        Human readable timestamp
        """
        seconds -= int(time.time())
        seconds = seconds % (24 * 3600)
        hour = seconds // 3600
        seconds %= 3600
        minutes = seconds // 60
        seconds %= 60

        return "%d:%02d:%02d" % (hour, minutes, seconds)

    def manage_market(self, drop_existing=True):
        """
        Manages the market for you
        """
        last = self.last_trade + int(3600 * self.trade_max_per_hour)
        if last > int(time.time()):
            rts = self.readable_ts(last)
            self.logger.debug(f"Won't trade for {rts}")
            return

        get_h = time.localtime().tm_hour
        if get_h in range(0, 6) or get_h == 23:
            self.logger.debug("Not managing trades between 23h-6h")
            return
        if drop_existing:
            self.drop_existing_trades()

        plenty = self.get_plenty_off()
        if plenty and not self.in_need_of(plenty):
            need = self.get_needs()
            if need:
                # check incoming resources
                url = f"game.php?village={self.village_id}&screen=market&mode=other_offer"
                res = self.wrapper.get_url(url=url)
                p = re.compile(
                    r"Aankomend:\s.+\"icon header (.+?)\".+?<\/span>(.+) ", re.M
                )
                incoming = p.findall(res.text)
                resource_incoming = {}
                if incoming:
                    resource_incoming[incoming[0][0].strip()] = int(
                        "".join([s for s in incoming[0][1] if s.isdigit()])
                    )
                    self.logger.info(
                        f"There are resources incoming! %s", resource_incoming
                    )

                item, how_many = need
                how_many = round(how_many, -1)
                if item in resource_incoming and resource_incoming[item] >= how_many:
                    self.logger.info(
                        f"Needed {item} already incoming! ({resource_incoming[item]} >= {how_many})"
                    )
                    return
                if how_many < 250:
                    return

                self.logger.debug("Checking current market offers")
                if self.check_other_offers(item, how_many, plenty):
                    self.logger.debug("Took market offer!")
                    return

                if how_many > self.max_trade_amount:
                    how_many = self.max_trade_amount
                    self.logger.debug(
                        "Lowering trade amount of %d to %d because of limitation", how_many, self.max_trade_amount
                    )
                biased = int(how_many * self.trade_bias)
                if self.actual[plenty] < biased:
                    self.logger.debug("Cannot trade because insufficient resources")
                    return
                self.logger.info(
                    "Adding market trade of %d %s -> %d %s", how_many, item, biased, plenty
                )
                self.wrapper.reporter.report(
                    self.village_id,
                    "TWB_MARKET",
                    "Adding market trade of %d %s -> %d %s"
                    % (how_many, item, biased, plenty),
                )

                self.trade(plenty, biased, item, how_many)

    def check_other_offers(self, item, how_many, sell):
        """
        Checks if there are offers that match our needs
        """
        url = f"game.php?village={self.village_id}&screen=market&mode=other_offer"
        res = self.wrapper.get_url(url=url)
        p = re.compile(
            r"(?:<!-- insert the offer -->\n+)\s+<tr>(.*?)<\/tr>", re.S | re.M
        )
        cur_off_tds = p.findall(res.text)
        p = re.compile(r"Aankomend:\s.+\"icon header (.+?)\".+?<\/span>(.+) ", re.M)
        incoming = p.findall(res.text)
        resource_incoming = {}
        if incoming:
            resource_incoming[incoming[0][0].strip()] = int(
                "".join([s for s in incoming[0][1] if s.isdigit()])
            )

        if item in resource_incoming:
            how_many = how_many - resource_incoming[item]
            if how_many < 1:
                self.logger.info("Requested resource already incoming!")
                return False

        willing_to_sell = self.actual[sell] - self.in_need_amount(sell)
        self.logger.debug(
            f"Found {len(cur_off_tds)} offers on market, willing to sell {willing_to_sell} {sell}"
        )

        for tds in cur_off_tds:
            res_offer = re.findall(
                r"<span class=\"icon header (.+?)\".+?>(.+?)</td>", tds
            )
            off_id = re.findall(
                r"<input type=\"hidden\" name=\"id\" value=\"(\d+)", tds
            )

            if len(off_id) < 1:
                # Not enough resources to trade
                continue

            offer = self.parse_res_offer(res_offer, off_id[0])
            if (
                    offer["offered"] == item
                    and offer["offer_amount"] >= how_many
                    and offer["wanted"] == sell
                    and offer["wanted_amount"] <= willing_to_sell
            ):
                self.logger.info(
                    f"Good offer: {offer['offer_amount']} {offer['offered']} for {offer['wanted_amount']} {offer['wanted']}"
                )
                # Take the deal!
                payload = {
                    "count": 1,
                    "id": offer["id"],
                    "h": self.wrapper.last_h,
                }
                post_url = f"game.php?village={self.village_id}&screen=market&mode=other_offer&action=accept_multi&start=0&id={offer['id']}&h={self.wrapper.last_h}"
                # print(f"Would post: {post_url} {payload}")
                self.wrapper.post_url(post_url, data=payload)
                self.last_trade = int(time.time())
                self.actual[offer["wanted"]] = (
                        self.actual[offer["wanted"]] - offer["wanted_amount"]
                )
                return True

        # No useful offers found
        return False

    def parse_res_offer(self, res_offer, id):
        """
        Parse an offer
        """
        off, want, ratio = res_offer
        res_offer, res_offer_amount = off
        res_wanted, res_wanted_amount = want

        return {
            "id": id,
            "offered": res_offer,
            "offer_amount": int("".join([s for s in res_offer_amount if s.isdigit()])),
            "wanted": res_wanted,
            "wanted_amount": int(
                "".join([s for s in res_wanted_amount if s.isdigit()])
            ),
        }
