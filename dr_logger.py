#!/usr/bin/python

# Copyright (C) 2014 Peter O'Malley
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
### BEGIN NODE INFO
[info]
name = DR Logger
version = 0.1
description = Log the DR temperatures, pressures, etc. 

[startup]
cmdline = %PYTHON% %FILE%
timeout = 20

[shutdown]
message = 987654321
timeout = 20
### END NODE INFO
"""

import time

from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.internet.task import LoopingCall

from labrad import types as T
from labrad.server import setting
from labrad.gpib import DeviceWrapper, DeviceServer
from labrad.errors import NoSuchDeviceError, Error
from labrad.units import Unit, Value

CONFIG_PATH = ['', 'Servers', 'DR Logger']
DIODE_LIST = ["4Kin", "4Kout", "77K", "Ret", "Mix", "Xchg", "Still", "Pot"]


class NoMKSDataError(Error):
    pass


class ServerNotFoundError(Error):
    pass


class WatchedServer(object):
    """Proxy for another server from which we pull data

    Attributes:
        name (str): Name of proxied server.
        cxn (labrad.client.Client): Connection to LabRAD.
        ctx (tuple of int): Context for requests to the proxied servers.
        device (str or int): Specific hardware device to access through the
            proxied server. If None (the default) we select the first available
            device, which is the pylabrad default.
    """
    server_name = 'none'

    def __init__(self, name, cxn, ctx, options):
        self.name = name
        self.cxn = cxn
        self.ctx = ctx
        self.options = dict(options)
        self.server = None
        self.active = False

    def get_variables(self):
        """ Get the variables (for the data vault) logged by this server.

        This is called on dataset creation to figure out how to define the dataset.
        :return: list of strings, one per variable, of the form "label (legend) [unit]"
        :rtype: list[str]
        """
        raise NotImplementedError()

    def _take_point(self):
        """ Get data from the device.

        This is called once per cycle to record the data.
        The return values must correspond with those of get_variables.
        :return: list of values, one per variable.
        :rtype: list[Value]
        """
        raise NotImplementedError()

    @inlineCallbacks
    def select_device(self):
        deviceName = self.options.get('device', None)
        print("deviceName: {}".format(deviceName))
        devs = yield self.server.list_devices(context=self.ctx)
        print "looking for device %s" % deviceName
        if deviceName is None:
            yield self.server.select_device(context=self.ctx)
        elif deviceName in [x[1] for x in devs]:
            yield self.server.select_device(deviceName, context=self.ctx)
        else:
            for i, n in devs:
                if deviceName in n:
                    print "selecting device: %s" % n
                    yield self.server.select_device(n, context=self.ctx)
                    break
            else:
                raise NoSuchDeviceError("No such device: %s" % self.device)

    @inlineCallbacks
    def take_point(self):
        """ Take a single data point.

        Call self._take_point, and handle server selection and device selection.
        """
        try:
            self.server = self.cxn[self.name]
            r = yield self._take_point()
            self.active = True
            returnValue(r)
        except KeyError as err:
            raise ServerNotFoundError(
                "'{}' server not found".format(self.name), payload=err
            )
        except T.Error as err:
            self.active = False
            if 'DeviceNotSelectedError' in err.msg:
                yield self.select_device()
                r = yield self._take_point()
                self.active = True
                returnValue(r)
            else:
                raise err


# noinspection PyAttributeOutsideInit
class MKS(WatchedServer):
    """ for MKS servers, we have an optional "device" argument in the form of
    (reading_name, multiplier), which tells us which reading to convert into
    a He flow.
    """
    server_name = 'mks_gauge_server'

    @inlineCallbacks
    def _take_point(self):
        r = yield self.server.get_readings(context=self.ctx)
        r = list(r)
        if not hasattr(self, 'channel'):
            yield self.setup_he_flow()
        if not r:
            raise NoMKSDataError("MKS server did not return data.")
        if self.channel is not None:
            r.append(self.multiplier * r[self.channel])
        returnValue(r)

    @inlineCallbacks
    def get_variables(self):
        point = yield self.take_point()
        names = yield self.server.get_gauge_list(context=self.ctx)
        rv = []
        for p, n in zip(point, names):
            rv.append('%s (Pressure) [%s]' % (n, str(p.unit)))
        if self.channel is not None:
            rv.append('He Flow (LHe) [L/h]')
        returnValue(rv)

    @inlineCallbacks
    def setup_he_flow(self):
        if 'he_flow_rate' in self.options and 'channel' in self.options:
            names = yield self.server.get_gauge_list(context=self.ctx)
            for i, name in enumerate(names):
                if name == self.options['channel']:
                    self.channel = i
                    self.multiplier = self.options['he_flow_rate']
                    print "Using gauge reading %s (%s) for He flow with multiplier %s" % (str(i), name, self.multiplier)
                    break
            else:
                self.channel = None
                print "ERROR: could not find gauge reading named '%s'" % self.device[0]
        else:
            self.channel = None


class MKSHack(MKS):
    server_name = 'mks_gauge_server_testhack'


class Diodes(WatchedServer):
    server_name = 'lakeshore_diodes'

    @inlineCallbacks
    def _take_point(self):
        r = yield self.server.temperatures(context=self.ctx)
        returnValue(r)

    def get_variables(self):
        return ['%s (Diode) [K]' % x for x in DIODE_LIST]


class Ruox(WatchedServer):
    server_name = 'lakeshore_ruox'

    @inlineCallbacks
    def _take_point(self):
        p = self.server.packet(context=self.ctx)
        p.temperatures()
        p.resistances()
        result = yield p.send()
        temps = [x[0] for x in result.temperatures]
        res = [x[0] for x in result.resistances]
        returnValue(temps + res)

    @inlineCallbacks
    def get_variables(self):
        yield self.take_point()  # make sure we're connected
        t = yield self.server.named_temperatures(context=self.ctx)
        temp_vars = ['%s (Ruox) [%s]' % (x[0], str(x[1][0].unit)) for x in t]
        r = yield self.server.named_resistances(context=self.ctx)
        res_vars = ['%s (Ruox Res) [%s]' % (x[0], str(x[1][0].unit)) for x in r]
        returnValue(temp_vars + res_vars)


# some tomfoolery to pull together all our watchers
WATCHERS = []
for obj in vars().values():
    try:
        if issubclass(obj, WatchedServer) and obj is not WatchedServer:
            WATCHERS.append(obj)
    except TypeError:
        pass
print "Found these watchers: ", WATCHERS


# noinspection PyAttributeOutsideInit
class DRLogger(DeviceWrapper):
    """

    Attributes:
        name (str): Name of this DR setup. Assigned by pylabrad's device server
            code.
        watchers (list of WatchedServer): Server proxies we watch.
    """
    @inlineCallbacks
    def connect(self, *args, **kwargs):
        """Connect to a DR device

        Args:
            args (tuple of (cxn,)): A tuple with a single element, which is a
                LabRAD connection.
        kwargs (dict):
            Maps hardware types (e.g. 'ruox') to configuration data. See
            DRLoggerServer.findDevices for format.
        """
        print "Creating DR Logger for %s" % self.name
        self.cxn = args[0]
        self.ctx = self.cxn.context()
        self.watchers = []
        self.data_vault = None
        self.errors = []
        self.dvPath = kwargs.pop('dvPath', ['', 'DR', self.name])
        self.datasetName = kwargs.pop('datasetName', '%s log - [t]' % self.name)
        self.timeInterval = kwargs.pop('timeInterval', 1.0)
        self.currentDay = ''
        # now make our watchers
        for k, v in kwargs.iteritems():
            server_name = v[0]
            nodeName = v[1]
            if len(v) > 2:
                options = v[2]
            else:
                options = {}
            for cls in WATCHERS:
                if cls.server_name == server_name:
                    break
            else:
                cls = None
            if cls is not None:
                print "Found watcher for %s" % server_name
                self.watchers.append(cls(server_name, self.cxn, self.ctx, options=options))
            else:
                raise ValueError("ERROR: No watcher class found for:", server_name)

        self.isLogging = False
        yield self.logging(True)  # start logging

    @inlineCallbacks
    def logging(self, start):
        if not self.isLogging and start:
            # start the loop
            self.isLogging = True
            self.loop = LoopingCall(self.take_point)
            self.loopDone = self.loop.start(self.timeInterval, now=True)
            print 'loop started'
        elif self.isLogging and not start:
            # stop the loop
            try:
                self.loop.stop()
                yield self.loopDone
            except AssertionError:
                pass
            print 'loop stopped'
            self.isLogging = False

    @inlineCallbacks
    def shutdown(self):
        yield self.logging(False)

    def new_dataset(self):
        self.data_vault = None

    @inlineCallbacks
    def make_dataset(self):
        self.data_vault = self.cxn['data_vault']
        self.data_vault.cd(self.dvPath, True, context=self.ctx)
        name = self.datasetName.replace('[t]', time.strftime("%Y-%m-%d %H:%M"))
        self.currentDay = time.strftime("%d")
        indeps = ['time [s]']
        deps = []
        for w in self.watchers:
            r = yield w.get_variables()
            deps.extend(r)
        print "Indep vars: %s" % str(indeps)
        print "Dependent vars: %s" % str(deps)

        yield self.data_vault.new(name, indeps, deps, context=self.ctx)

    @inlineCallbacks
    def take_point(self):
        try:
            # gather data
            data = [time.time() * Unit('s')]
            errors = []
            for w in self.watchers:
                try:
                    r = yield w.take_point()
                    data.extend(r)
                except T.Error as err:
                    errors.append((w.server_name, err.msg))
            if errors:
                self.errors = errors
                returnValue(None)
            # strip units
            data = [x[x.unit] for x in data]
            # did the day roll over?
            if self.currentDay != time.strftime("%d"):
                self.new_dataset()
            try:
                # make dataset if first time
                if self.data_vault is None:
                    print("Making new dataset")
                    yield self.make_dataset()
                # add data
                yield self.data_vault.add(data, context=self.ctx)
            except T.Error as err:
                print("Error when writing data to data vault: {}".format(err))
                if 'NoDatasetError' in err.msg:
                    try:
                        yield self.make_dataset()
                        yield self.data_vault.add(data, context=self.ctx)
                    except T.Error as err:
                        errors.append(("Data Vault", str(err)))
                else:
                    errors.append(("General", str(err)))
            self.errors = errors
        except Exception as e:
            import traceback
            traceback.print_exc()


class DRLoggerServer(DeviceServer):
    """Log DR temperatures and pressures

    Registry format:
        >> Servers >> DR Logger >> <DR name> (e.g. 'Ivan')
            <thing to measure> -> (<server>, <node>, <options>):
                <thing to measure> (s): Either 'diodes', 'mks', or 'ruox'.
                <server> (s): pylabrad name of the server servicing the thing
                    being measure. For example, for the diodes <server name> is
                    'lakeshore_diodes'.
                <node> (s): Name of node running <server>.
                <options> ((s, ?),...): Tuple of (key, value) tuples. Provides
                    additional data to configure measurements.
    """
    name = 'DR Logger'
    deviceName = 'DR'
    deviceWrapper = DRLogger

    @inlineCallbacks
    def findDevices(self):
        """Get device configurations from registry

        all configurations in CONFIG_PATH and returns

        Returns list of (drName, (cxn,), serverDict).
            serverDict is a mapping from a device type (i.e. 'mks', 'ruox', or
            'diodes') to a tuple of either
            (server name, node name, options) or
            (server name, node name).
            For example, for the Jules DR, we would have entries mapping
            'ruox' -> ('lakeshore_ruox', 'DR') and
            'mks' -> ('mks_gauge_server', 'DR', (('He Flow', 24.7 L/h/Torr)))
        """
        deviceList = []
        reg = self.client.registry
        yield reg.cd(CONFIG_PATH)
        resp = yield reg.dir()
        drNames = resp[0].aslist
        for drName in drNames:
            # all required nodes must be present to create this device
            yield reg.cd(drName)
            devs = yield reg.dir()
            devs = devs[1].aslist  # e.g. 'diodes', 'mks'
            missingNodes = []
            serverDict = {}
            for dev in devs:
                config = yield reg.get(dev)
                print("config: {}".format(config))
                # config is a tuple of (server name, node name) or
                #                      (server name, node name, options)
                serverDict[dev] = config
                node = config[1]
                if "node_" + node.lower() not in self.client.servers:
                    missingNodes.append(node)
            if not missingNodes:
                deviceList.append((drName, (self.client,), serverDict))
            else:
                print "device %s missing nodes %s" % (drName, str(list(set(missingNodes))))
            yield reg.cd(1)
        returnValue(deviceList)

    @setting(10, "Take Point")
    def take_point(self, c):
        """ Take a single data point. """
        self.selectedDevice(c).take_point()

    @setting(11, "New Dataset")
    def new_dataset(self, c):
        """ Start a new dataset. """
        self.selectedDevice(c).new_dataset()

    @setting(12, 'Logging', start='b', returns='b')
    def logging(self, c, start=None):
        """ Get/set whether we are currently logging. """
        dev = self.selectedDevice(c)
        if start is not None:
            yield dev.logging(start)
        returnValue(dev.isLogging)

    @setting(13, 'Time Interval', ti='v[s]', returns='v[s]')
    def time_interval(self, c, ti=None):
        """ Get/set the logging time interval. """
        dev = self.selectedDevice(c)
        if ti is not None:
            dev.timeInterval = ti['s']
            if dev.isLogging:
                yield dev.logging(False)
                yield dev.logging(True)
        returnValue(Value(dev.timeInterval, 's'))

    @setting(14, 'Errors', returns='*(s, s)')
    def errors(self, c):
        """ Retrieve outstanding errors.

        Each error is given as a pair of strings:
        (source or type, error message)
        """
        dev = self.selectedDevice(c)
        return dev.errors

    @setting(15, 'Current Time', returns='v['']')
    def current_time(self, c):
        """ Return the current time, in seconds (i.e. time.time()).
        """
        return time.time()


__server__ = DRLoggerServer()

if __name__ == '__main__':
    from labrad import util

    util.runServer(__server__)
