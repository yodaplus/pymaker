#!/usr/bin/env python3
#
# This file is part of Maker Keeper Framework.
#
# Copyright (C) 2017 reverendus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import argparse
import copy
import operator
import time
from functools import reduce
from typing import List

import logging

from api import Address, Transfer
from api.numeric import Ray
from api.numeric import Wad
from api.otc import SimpleMarket, OfferInfo, LogTake
from api.sai import Tub, Lpc
from api.token import ERC20Token
from keepers import Keeper
from keepers.arbitrage.conversion import Conversion
from keepers.arbitrage.conversion import LpcTakeAltConversion, LpcTakeRefConversion
from keepers.arbitrage.transfer_formatter import TransferFormatter


class SaiOtcMaker(Keeper):
    def args(self, parser: argparse.ArgumentParser):
        parser.add_argument("--frequency", help="Monitoring frequency in seconds (default: 5)", default=5, type=float)
        parser.add_argument("--sell-token", help="Token to put on sale on OasisDEX", type=str)
        parser.add_argument("--buy-token", help="Token we will be paid with on OasisDEX", type=str)
        parser.add_argument("--min-spread", help="Minimum spread allowed", type=float)
        parser.add_argument("--avg-spread", help="Average spread (used on order creation)", type=float)
        parser.add_argument("--max-spread", help="Maximum spread allowed", type=float)
        parser.add_argument("--max-amount", help="Maximum value of open orders", type=float)
        parser.add_argument("--min-amount", help="Minimum value of open orders", type=float)

    def init(self):
        self.tub_address = Address(self.config.get_contract_address("saiTub"))
        self.tap_address = Address(self.config.get_contract_address("saiTap"))
        self.top_address = Address(self.config.get_contract_address("saiTop"))
        self.tub = Tub(web3=self.web3, address_tub=self.tub_address, address_tap=self.tap_address, address_top=self.top_address)
        self.lpc_address = Address(self.config.get_contract_address("saiLpc"))
        self.lpc = Lpc(web3=self.web3, address=self.lpc_address)
        self.otc_address = Address(self.config.get_contract_address("otc"))
        self.otc = SimpleMarket(web3=self.web3, address=self.otc_address)

        self.skr = ERC20Token(web3=self.web3, address=self.tub.skr())
        self.sai = ERC20Token(web3=self.web3, address=self.tub.sai())
        self.gem = ERC20Token(web3=self.web3, address=self.tub.gem())
        ERC20Token.register_token(self.tub.skr(), 'SKR')
        ERC20Token.register_token(self.tub.sai(), 'SAI')
        ERC20Token.register_token(self.tub.gem(), 'WETH')

        self.sell_token = ERC20Token.token_address_by_name(self.arguments.sell_token)
        self.buy_token = ERC20Token.token_address_by_name(self.arguments.buy_token)
        self.max_amount = Wad.from_number(self.arguments.max_amount)
        self.min_amount = Wad.from_number(self.arguments.min_amount)
        self.min_spread = self.arguments.min_spread
        self.avg_spread = self.arguments.avg_spread
        self.max_spread = self.arguments.max_spread

    def run(self):
        self.setup_allowances()
        self.print_balances()
        self.otc.on_take(self.order_take_handler)
        while True:
            self.update_otc_orders()
            time.sleep(self.arguments.frequency)

    def print_balances(self):
        def balances():
            for token in [self.sai, self.gem]:
                yield f"{token.balance_of(self.our_address)} {token.name()}"
        logging.info(f"Keeper balances are {', '.join(balances())}.")

    def setup_allowances(self):
        """Approve all components that need to access our balances"""
        self.setup_lpc_allowances()
        self.setup_otc_allowances()

    # def setup_tub_allowances(self):
    def setup_lpc_allowances(self):
        """Approve the Lpc so we can exchange WETH and SAI using it"""
        self.setup_allowance(self.gem, self.lpc.address, 'Lpc')
        self.setup_allowance(self.sai, self.lpc.address, 'Lpc')

    def setup_otc_allowances(self):
        """Approve OasisDEX so we can exchange all three tokens (WETH, SAI and SKR)"""
        self.setup_allowance(self.gem, self.otc.address, 'OasisDEX')
        self.setup_allowance(self.sai, self.otc.address, 'OasisDEX')

    def setup_allowance(self, token: ERC20Token, spender_address: Address, spender_name: str):
        if token.allowance_of(self.our_address, spender_address) < Wad(2 ** 128 - 1):
            logging.info(f"Approving {spender_name} ({spender_address}) to access our {token.name()} balance directly")
            if not token.approve(spender_address):
                raise RuntimeError("Token approval failed!")

    def lpc_conversions(self) -> List[Conversion]:
        return [LpcTakeRefConversion(self.lpc),
                LpcTakeAltConversion(self.lpc)]

    def conversion(self):
        return next(filter(lambda conversion: conversion.source_token == self.buy_token and
                                              conversion.target_token == self.sell_token, self.lpc_conversions()))

    def order_take_handler(self, log_take: LogTake):
        if log_take.maker == self.our_address \
                and log_take.have_token == self.sell_token and log_take.want_token == self.buy_token:

            conversion = copy.deepcopy(self.conversion())
            #TODO this should get extracted somewhere
            conversion.source_amount = Wad.min(log_take.give_amount, conversion.max_source_amount)
            conversion.target_amount = Wad(Ray(conversion.source_amount) * conversion.rate)

            logging.info(f"Someone exchanged {log_take.take_amount} {ERC20Token.token_name_by_address(self.sell_token)} to {log_take.give_amount} {ERC20Token.token_name_by_address(self.buy_token)}")
            logging.info(f"We will make an counterexchange of {conversion.source_amount} {ERC20Token.token_name_by_address(conversion.source_token)}"
                  f" to {conversion.target_amount} {ERC20Token.token_name_by_address(conversion.target_token)}")
            logging.info(f"Executing {conversion.name()}")
            result = conversion.execute()
            if result:
                trans = list(result.transfers)
                trans.append(Transfer(log_take.have_token, log_take.maker, log_take.taker, log_take.take_amount))
                trans.append(Transfer(log_take.want_token, log_take.taker, log_take.maker, log_take.give_amount))
                logging.info("OK")
                logging.info(f"The profit we made is {TransferFormatter().format_net(trans, self.our_address)}.")
            else:
                logging.info("FAILED!!!")

    def update_otc_orders(self):

        # cancel offers with prices outside allowed range
        for offer in self.otc_offers():
            rate = self.price(offer)
            rate_min = self.apply_spread(self.conversion().rate, self.min_spread)
            rate_max = self.apply_spread(self.conversion().rate, self.max_spread)
            if (rate < rate_max) or (rate > rate_min):
                self.otc.kill(offer.offer_id)

        # if our engagement is below the minimum amount,
        # create a new offer up to the maximum amount
        #TODO check our balance
        #TODO check max_amount on conversion??
        total_amount = self.total_amount(self.otc_offers())
        if total_amount < self.min_amount:
            have_amount = self.max_amount - total_amount
            want_amount = Wad(Ray(have_amount) / self.apply_spread(self.conversion().rate, self.avg_spread))
            self.otc.make(have_token=self.sell_token, have_amount=have_amount,
                          want_token=self.buy_token, want_amount=want_amount)

    @staticmethod
    def price(offer: OfferInfo) -> Ray:
        return Ray(offer.sell_how_much) / Ray(offer.buy_how_much)

    @staticmethod
    def total_amount(offers: List[OfferInfo]):
        return reduce(operator.add, map(lambda offer: offer.sell_how_much, offers), Wad(0))

    @staticmethod
    def apply_spread(rate: Ray, spread: float) -> Ray:
        return rate * Ray.from_number(1 - spread)

    def otc_offers(self):
        # TODO extract that to a method in otc(...) ?
        offers = [self.otc.get_offer(offer_id + 1) for offer_id in range(self.otc.get_last_offer_id())]
        offers = [offer for offer in offers if offer is not None]
        return list(filter(lambda offer: offer.owner == self.our_address and
                                         offer.sell_which_token == self.sell_token and
                                         offer.buy_which_token == self.buy_token, offers))


if __name__ == '__main__':
    SaiOtcMaker().start()