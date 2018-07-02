#!/usr/bin/env python
# -*- coding: utf-8; py-indent-offset:4 -*-
###############################################################################
#
# Copyright (C) 2018 Ed Bartosh
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
###############################################################################

from __future__ import (absolute_import, division, print_function)

from collections import defaultdict
from datetime import datetime
from time import sleep
import threading

import quickfix as fix

from backtrader import BrokerBase, OrderBase, Order
from backtrader.comminfo import CommInfoBase
from backtrader.position import Position
from backtrader.utils.py3 import queue

class FIXCommInfo(CommInfoBase):
    def getvaluesize(self, size, price):
        # In real life the margin approaches the price
        return abs(size) * price

    def getoperationcost(self, size, price):
        '''Returns the needed amount of cash an operation would cost'''
        # Same reasoning as above
        return abs(size) * price

class FIXOrder(OrderBase):
    # Map backtrader order types to the FIX ones
    _OrderTypes = {
        None: fix.OrdType_MARKET,  # default
        Order.Market: fix.OrdType_MARKET,
        Order.Limit: fix.OrdType_LIMIT,
        Order.Close: fix.OrdType_ON_CLOSE,
        Order.Stop: fix.OrdType_STOP,
        Order.StopLimit: fix.OrdType_STOP_LIMIT,
        #Order.StopTrail: ???,
        #Order.StopTrailLimit: ???,
    }

    def __init__(self, action, **kwargs):
        self.ordtype = self.Buy if action == 'BUY' else self.Sell

        OrderBase.__init__(self)

        # pass any custom arguments to the order
        for kwarg in kwargs:
            if not hasattr(self, kwarg):
                setattr(self, kwarg, kwargs[kwarg])

        now = datetime.utcnow()
        self.order_id = now.strftime("%Y-%m-%d_%H:%M:%S_%f")

        msg = fix.Message()
        msg.getHeader().setField(fix.BeginString(fix.BeginString_FIX42))
        msg.getHeader().setField(fix.MsgType(fix.MsgType_NewOrderSingle)) #39=D
        msg.setField(fix.ClOrdID(self.order_id)) #11=Unique order
        msg.setField(fix.OrderID(self.order_id)) # 37
        msg.setField(fix.HandlInst(fix.HandlInst_MANUAL_ORDER_BEST_EXECUTION)) #2
        msg.setField(fix.Symbol(self.data._name)) #55
        msg.setField(fix.Side(fix.Side_BUY if action == 'BUY' else fix.Side_SELL)) #43
        msg.setField(fix.OrdType(self._OrderTypes[self.exectype])) #40
        msg.setField(fix.OrderQty(abs(self.size))) #38
        msg.setField(fix.OrderQty2(abs(self.size)))
        msg.setField(fix.Price(self.price))
        msg.setField(fix.TransactTime())

        sdict = self.settings.get()
        msg.setField(fix.ExDestination(sdict.getString("Destination")))
        msg.setField(fix.Account(sdict.getString("Account")))
        msg.setField(fix.TargetSubID(sdict.getString("TargetSubID")))

        self.msg = msg
        print("DEBUG: order msg:", msg)

    def submit_fix(self, app):
        fix.Session.sendToTarget(self.msg, app.session_id)

    def cancel_fix(self, app):
        msg = fix.Message()
        header = msg.getHeader()

        header.setField(fix.BeginString(fix.BeginString_FIX42))
        header.setField(fix.MsgType(fix.MsgType_OrderCancelRequest))

        msg.setField(fix.OrigClOrdID(self.order_id))
        msg.setField(fix.ClOrdID(self.order_id))
        msg.setField(fix.OrderID(self.order_id))
        msg.setField(fix.Symbol(self.data._name)) #55

        sdict = self.settings.get()
        msg.setField(fix.ExDestination(sdict.getString("Destination")))
        msg.setField(fix.Account(sdict.getString("Account")))
        msg.setField(fix.TargetSubID(sdict.getString("TargetSubID")))

        fix.Session.sendToTarget(msg, app.sessionID)

def get_value(message, tag):
    """Get tag value from the message."""
    message.getField(tag)
    return tag.getValue()

class FIXApplication(fix.Application):
    def __init__(self, broker):
        fix.Application.__init__(self)
        self.broker = broker
        self.session_id = None

    def onCreate(self, arg0):
        print("DEBUG: onCreate:", arg0)

    def onLogon(self, arg0):
        self.session_id = arg0
        print("DEBUG: onLogon:", arg0)

    def onLogout(self, arg0):
        print("DEBUG: onLogout:", arg0)

    def onMessage(self, message, sessionID):
        print("DEBUG: onMessage: ", sessionID, message.toString().replace('\x01', '|'))

    def toAdmin(self, message, sessionID):
        msgType = fix.MsgType()
        message.getHeader().getField(msgType)
        if msgType.getValue() == fix.MsgType_Logon:
            target_subid = self.broker.settings.get().getString("TargetSubID")
            message.getHeader().setField(fix.TargetSubID(target_subid))
        elif msgType.getValue() == fix.MsgType_Heartbeat:
            print("DEBUG: Heartbeat reply")
        else:
            print("DEBUG: toAdmin: ", sessionID, message.toString().replace('\x01', '|'))

    def fromAdmin(self, message, sessionID): #, message):
        msgType = fix.MsgType()
        message.getHeader().getField(msgType)
        if msgType.getValue() == fix.MsgType_Heartbeat:
            print("DEBUG: Heartbeat")
        else:
            print("DEBUG: fromAdmin: ", sessionID, message.toString().replace('\x01', '|'))

    def toApp(self, sessionID, message):
        print("DEBUG: toApp: ", sessionID, message.toString().replace('\x01', '|'))

    def update_position(self, symbol, price, size):
        if symbol in self.broker.positions:
            pos = self.broker.positions[symbol]
            if pos.size + size:
                pos.price = (pos.price * pos.size + price * size) / (pos.size + size)
                pos.size += size
            return pos
        else:
            return Position(size, price)

    def fromApp(self, message, sessionID):
        msgType = fix.MsgType()
        message.getHeader().getField(msgType)
        tag = msgType.getValue()

        if tag == fix.MsgType_News:
            result = []
            for item in message.toString().split('\x01'):
                if not item or not '=' in item:
                    continue
                code, val = item.split("=")
                if code == '10008':
                    result.append([val, None])
                if code == '58':
                    for valtype in int, float, str:
                        try:
                            val = valtype(val)
                        except ValueError:
                            continue
                        break
                    result[-1][1] = val

            for key, value in result:
                if hasattr(self.broker, key):
                    setattr(self.broker, key, value)

            print("DEBUG: account status:", result)

        elif tag == fix.MsgType_ExecutionReport:
            print("DEBUG: execution report: ", sessionID, message.toString().replace('\x01', '|'))
            etype = get_value(message, fix.ExecType())

            if etype in (fix.ExecType_PENDING_NEW, fix.ExecType_NEW):
                order_id = get_value(message, fix.ClOrdID())
                print("DEBUG: Pending [new]", order_id)

                if order_id in self.broker.orders:
                    self.broker.orders[order_id].status = Order.Accepted
                    self.broker.notify(self.broker.orders[order_id])

            elif etype == fix.ExecType_FILL:
                order_id = get_value(message, fix.ClOrdID())
                symbol = get_value(message, fix.Symbol())
                side = get_value(message, fix.Side())
                price = get_value(message, fix.Price())
                size = get_value(message, fix.OrderQty())
                if side == fix.Side_SELL:
                    size = -size

                print("DEBUG: filled", order_id, symbol, price, size)

                pos = self.update_position(symbol, price, size)
                self.broker.positions[symbol] = pos

                if order_id in self.broker.orders:
                    order = self.broker.orders[order_id]
                    order.status = Order.Completed

                    order.execute(0, size, price, 0, size*price, 0.0,
                                  size, 0.0, 0.0, 0.0, 0.0, pos.size, pos.price)

            elif etype == fix.ExecType_REJECTED:
                order_id = get_value(message, fix.ClOrdID())
                print("DEBUG: Rejected", order_id)

                if order_id in self.broker.orders:
                    self.broker.orders[order_id].status = Order.Rejected
                    self.broker.notify(self.broker.orders[order_id])

            elif etype == fix.ExecType_CANCELED:
                order_id = get_value(message, fix.ClOrdID())
                print("DEBUG: Canceled", order_id)

                if order_id in self.broker.orders:
                    self.broker.orders[order_id].status = Order.Canceled
                    self.broker.notify(self.broker.orders[order_id])

            elif etype == 'P':
                symbol = get_value(message, fix.Symbol())
                side = get_value(message, fix.Side())
                price = get_value(message, fix.Price())
                size = get_value(message, fix.CumQty())
                if side == fix.Side_SELL:
                    size = -size

                self.broker.positions[symbol] = self.update_position(symbol, price, size)

        else:
            print("DEBUG: fromApp: ", sessionID, message.toString().replace('\x01', '|'))

def get_value(message, tag):
    message.getField(tag)
    return tag.getValue()

class FIXBroker(BrokerBase):
    '''Broker implementation for FIX protocol using quickfix library.'''

    def __init__(self, config, debug=False):
        BrokerBase.__init__(self)

        self.config = config
        self.debug = debug

        self.queue = queue.Queue()  # holds orders which are notified

        self.startingcash = self.cash = 0.0
        self.done = False

        self.app = None

        self.orders = {}
        self.positions = defaultdict(Position)
        self.executions = {}

        self.settings = None

        # attributes set by fix.Application
        self.HardBuyingPowerLimit = 0

        # start quickfix main loop in a separate thread
        thread = threading.Thread(target=self.fix)
        thread.start()

    def fix(self):
        self.settings = fix.SessionSettings(self.config)
        storeFactory = fix.FileStoreFactory(self.settings)
        logFactory = fix.ScreenLogFactory(self.settings)
        self.app = FIXApplication(self)
        initiator = fix.SocketInitiator(self.app, storeFactory, self.settings, logFactory)
        initiator.start()
        while not self.done:
            sleep(1)
        initiator.stop()

    def stop(self):
        self.done = True

    def getcommissioninfo(self, data):
        return FIXCommInfo()

    def getcash(self):
        return self.HardBuyingPowerLimit

    def getvalue(self, datas=None):
        return self.HardBuyingPowerLimit

    def getposition(self, data):
        return self.positions[data._dataname]

    def get_notification(self):
        try:
            return self.queue.get(False)
        except queue.Empty:
            pass

    def notify(self, order):
        self.queue.put(order)

    def _submit(self, action, owner, data, size, price=None, plimit=None,
                exectype=None, valid=None, tradeid=0, **kwargs):

        order = FIXOrder(action, owner=owner, data=data,
                         size=size, price=price, pricelimit=plimit,
                         exectype=exectype, valid=valid, tradeid=tradeid,
                         settings=self.settings, **kwargs)

        order.addcomminfo(self.getcommissioninfo(data))
        order.submit_fix(self.app)
        self.orders[order.order_id] = order
        self.notify(order)

        return order

    def buy(self, owner, data, size, price=None, plimit=None,
            exectype=None, valid=None, tradeid=0, **kwargs):

        return self._submit('BUY', owner, data, size, price, plimit,
                            exectype, valid, tradeid, **kwargs)

    def sell(self, owner, data, size, price=None, plimit=None,
             exectype=None, valid=None, tradeid=0, **kwargs):

        return self._submit('SELL', owner, data, size, price, plimit,
                            exectype, valid, tradeid, **kwargs)

    def cancel(self, order):
        print("DEBUG: canceling order", order)
        _order = self.orders.get(order.order_id)
        if not ord:
            print("DEBUG: order not found", order)
            return # not found ... not cancellable

        if _order.status == Order.Cancelled:  # already cancelled
            return

        _order.cancel_fix(self.app)
