from gzip import GzipFile
import os
import copy
import logging
import cPickle as pickle
import random
import struct
from contextlib import contextmanager, closing
from collections import defaultdict
import tarfile
from tempfile import NamedTemporaryFile, mkstemp
from eventlet.green import socket
from tempfile import mkdtemp
from shutil import rmtree
from test import get_config
from textwrap import dedent
import sys
from swift.common.ring import RingData, Ring
from swift.common.utils import config_true_value, LogAdapter
from hashlib import md5
from eventlet import sleep, Timeout
import logging.handlers
from httplib import HTTPException
import time


def readuntil2crlfs(fd):
    rv = ''
    lc = ''
    crlfs = 0
    while crlfs < 2:
        c = fd.read(1)
        if not c:
            raise ValueError("didn't get two CRLFs; just got %r" % rv)
        rv = rv + c
        if c == '\r' and lc != '\n':
            crlfs = 0
        if lc == '\r' and c == '\n':
            crlfs += 1
        lc = c
    return rv


def connect_tcp(hostport):
    rv = socket.socket()
    rv.connect(hostport)
    return rv


@contextmanager
def tmpfile(content):
    with NamedTemporaryFile('w', delete=False) as f:
        file_name = f.name
        f.write(str(content))
    try:
        yield file_name
    finally:
        os.unlink(file_name)

xattr_data = {}


def _get_inode(fd):
    if not isinstance(fd, int):
        try:
            fd = fd.fileno()
        except AttributeError:
            return os.stat(fd).st_ino
    return os.fstat(fd).st_ino


def _setxattr(fd, k, v):
    inode = _get_inode(fd)
    data = xattr_data.get(inode, {})
    data[k] = v
    xattr_data[inode] = data


def _getxattr(fd, k):
    inode = _get_inode(fd)
    data = xattr_data.get(inode, {}).get(k)
    if not data:
        raise IOError
    return data

import xattr
xattr.setxattr = _setxattr
xattr.getxattr = _getxattr


@contextmanager
def temptree(files, contents=''):
    # generate enough contents to fill the files
    c = len(files)
    contents = (list(contents) + [''] * c)[:c]
    tempdir = mkdtemp()
    for path, content in zip(files, contents):
        if os.path.isabs(path):
            path = '.' + path
        new_path = os.path.join(tempdir, path)
        subdir = os.path.dirname(new_path)
        if not os.path.exists(subdir):
            os.makedirs(subdir)
        with open(new_path, 'w') as f:
            f.write(str(content))
    try:
        yield tempdir
    finally:
        rmtree(tempdir)


class FakeRing(Ring):

    def __init__(self, replicas=3, max_more_nodes=0, part_power=0):
        """
        :param part_power: make part calculation based on the path

        If you set a part_power when you setup your FakeRing the parts you get
        out of ring methods will actually be based on the path - otherwise we
        exercise the real ring code, but ignore the result and return 1.
        """
        # 9 total nodes (6 more past the initial 3) is the cap, no matter if
        # this is set higher, or R^2 for R replicas
        self.set_replicas(replicas)
        self.max_more_nodes = max_more_nodes
        self._part_shift = 32 - part_power
        self._reload()

    def get_part(self, *args, **kwargs):
        real_part = super(FakeRing, self).get_part(*args, **kwargs)
        if self._part_shift == 32:
            return 1
        return real_part

    def _reload(self):
        self._rtime = time.time()

    def clear_errors(self):
        for dev in self.devs:
            for key in ('errors', 'last_error'):
                try:
                    del dev[key]
                except KeyError:
                    pass

    def set_replicas(self, replicas):
        self.replicas = replicas
        self._devs = []
        for x in range(self.replicas):
            ip = '10.0.0.%s' % x
            port = 1000 + x
            self._devs.append({
                'ip': ip,
                'replication_ip': ip,
                'port': port,
                'replication_port': port,
                'device': 'sd' + (chr(ord('a') + x)),
                'zone': x % 3,
                'region': x % 2,
                'id': x,
            })

    @property
    def replica_count(self):
        return self.replicas

    def _get_part_nodes(self, part):
        return list(self._devs)

    def get_more_nodes(self, part):
        # replicas^2 is the true cap
        for x in xrange(self.replicas, min(self.replicas + self.max_more_nodes,
                                           self.replicas * self.replicas)):
            yield {'ip': '10.0.0.%s' % x,
                   'port': 1000 + x,
                   'device': 'sda',
                   'zone': x % 3,
                   'region': x % 2,
                   'id': x}


def write_fake_ring(path, *devs):
    """
    Pretty much just a two node, two replica, 2 part power ring...
    """
    dev1 = {'id': 0, 'zone': 0, 'device': 'sda1', 'ip': '127.0.0.1',
            'port': 6000}
    dev2 = {'id': 0, 'zone': 0, 'device': 'sdb1', 'ip': '127.0.0.1',
            'port': 6000}

    dev1_updates, dev2_updates = devs or ({}, {})

    dev1.update(dev1_updates)
    dev2.update(dev2_updates)

    replica2part2dev_id = [[0, 1, 0, 1], [1, 0, 1, 0]]
    devs = [dev1, dev2]
    part_shift = 30
    with closing(GzipFile(path, 'wb')) as f:
        pickle.dump(RingData(replica2part2dev_id, devs, part_shift), f)


class FakeMemcache(object):

    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def keys(self):
        return self.store.keys()

    def set(self, key, value, time=0):
        self.store[key] = value
        return True

    def incr(self, key, time=0):
        self.store[key] = self.store.setdefault(key, 0) + 1
        return self.store[key]

    @contextmanager
    def soft_lock(self, key, timeout=0, retries=5):
        yield True

    def delete(self, key):
        try:
            del self.store[key]
        except Exception:
            pass
        return True


class NullLoggingHandler(logging.Handler):

    def emit(self, record):
        pass


class UnmockTimeModule(object):

    """
    Even if a test mocks time.time - you can restore unmolested behavior in a
    another module who imports time directly by monkey patching it's imported
    reference to the module with an instance of this class
    """

    _orig_time = time.time

    def __getattribute__(self, name):
        if name == 'time':
            return UnmockTimeModule._orig_time
        return getattr(time, name)


# logging.LogRecord.__init__ calls time.time
logging.time = UnmockTimeModule()


class FakeLogger(logging.Logger):
    # a thread safe logger

    def __init__(self, *args, **kwargs):
        self._clear()
        self.name = 'swift.unit.fake_logger'
        self.level = logging.NOTSET
        if 'facility' in kwargs:
            self.facility = kwargs['facility']
        self.statsd_client = None
        self.thread_locals = None
        self.parent = None

    def _clear(self):
        self.log_dict = defaultdict(list)
        self.lines_dict = defaultdict(list)

    def _store_in(store_name):
        def stub_fn(self, *args, **kwargs):
            self.log_dict[store_name].append((args, kwargs))
        return stub_fn

    def _store_and_log_in(store_name, level):
        def stub_fn(self, *args, **kwargs):
            self.log_dict[store_name].append((args, kwargs))
            self._log(level, args[0], args[1:], **kwargs)
        return stub_fn

    def get_lines_for_level(self, level):
        return self.lines_dict[level]

    error = _store_and_log_in('error', logging.ERROR)
    info = _store_and_log_in('info', logging.INFO)
    warning = _store_and_log_in('warning', logging.WARNING)
    warn = _store_and_log_in('warning', logging.WARNING)
    debug = _store_and_log_in('debug', logging.DEBUG)

    def exception(self, *args, **kwargs):
        self.log_dict['exception'].append((args, kwargs,
                                           str(sys.exc_info()[1])))
        print 'FakeLogger Exception: %s' % self.log_dict

    # mock out the StatsD logging methods:
    update_stats = _store_in('update_stats')
    increment = _store_in('increment')
    decrement = _store_in('decrement')
    timing = _store_in('timing')
    timing_since = _store_in('timing_since')
    transfer_rate = _store_in('transfer_rate')
    set_statsd_prefix = _store_in('set_statsd_prefix')

    def get_increments(self):
        return [call[0][0] for call in self.log_dict['increment']]

    def get_increment_counts(self):
        counts = {}
        for metric in self.get_increments():
            if metric not in counts:
                counts[metric] = 0
            counts[metric] += 1
        return counts

    def setFormatter(self, obj):
        self.formatter = obj

    def close(self):
        self._clear()

    def set_name(self, name):
        # don't touch _handlers
        self._name = name

    def acquire(self):
        pass

    def release(self):
        pass

    def createLock(self):
        pass

    def emit(self, record):
        pass

    def _handle(self, record):
        try:
            line = record.getMessage()
        except TypeError:
            print 'WARNING: unable to format log message %r %% %r' % (
                record.msg, record.args)
            raise
        self.lines_dict[record.levelname.lower()].append(line)

    def handle(self, record):
        self._handle(record)

    def flush(self):
        pass

    def handleError(self, record):
        pass


class DebugLogger(FakeLogger):

    """A simple stdout logging version of FakeLogger"""

    def __init__(self, *args, **kwargs):
        FakeLogger.__init__(self, *args, **kwargs)
        self.formatter = logging.Formatter(
            "%(server)s %(levelname)s: %(message)s")

    def handle(self, record):
        self._handle(record)
        print self.formatter.format(record)


class DebugLogAdapter(LogAdapter):

    def _send_to_logger(name):
        def stub_fn(self, *args, **kwargs):
            return getattr(self.logger, name)(*args, **kwargs)
        return stub_fn

    # delegate to FakeLogger's mocks
    update_stats = _send_to_logger('update_stats')
    increment = _send_to_logger('increment')
    decrement = _send_to_logger('decrement')
    timing = _send_to_logger('timing')
    timing_since = _send_to_logger('timing_since')
    transfer_rate = _send_to_logger('transfer_rate')
    set_statsd_prefix = _send_to_logger('set_statsd_prefix')

    def __getattribute__(self, name):
        try:
            return object.__getattribute__(self, name)
        except AttributeError:
            return getattr(self.__dict__['logger'], name)


def debug_logger(name='test'):
    """get a named adapted debug logger"""
    return DebugLogAdapter(DebugLogger(), name)


original_syslog_handler = logging.handlers.SysLogHandler


def fake_syslog_handler():
    for attr in dir(original_syslog_handler):
        if attr.startswith('LOG'):
            setattr(FakeLogger, attr,
                    copy.copy(getattr(logging.handlers.SysLogHandler, attr)))
    FakeLogger.priority_map = \
        copy.deepcopy(logging.handlers.SysLogHandler.priority_map)

    logging.handlers.SysLogHandler = FakeLogger


if config_true_value(get_config('unit_test').get('fake_syslog', 'False')):
    fake_syslog_handler()


class MockTrue(object):

    """
    Instances of MockTrue evaluate like True
    Any attr accessed on an instance of MockTrue will return a MockTrue
    instance. Any method called on an instance of MockTrue will return
    a MockTrue instance.

    >>> thing = MockTrue()
    >>> thing
    True
    >>> thing == True # True == True
    True
    >>> thing == False # True == False
    False
    >>> thing != True # True != True
    False
    >>> thing != False # True != False
    True
    >>> thing.attribute
    True
    >>> thing.method()
    True
    >>> thing.attribute.method()
    True
    >>> thing.method().attribute
    True

    """

    def __getattribute__(self, *args, **kwargs):
        return self

    def __call__(self, *args, **kwargs):
        return self

    def __repr__(*args, **kwargs):
        return repr(True)

    def __eq__(self, other):
        return other is True

    def __ne__(self, other):
        return other is not True


@contextmanager
def mock(update):
    returns = []
    deletes = []
    for key, value in update.items():
        imports = key.split('.')
        attr = imports.pop(-1)
        module = __import__(imports[0], fromlist=imports[1:])
        for modname in imports[1:]:
            module = getattr(module, modname)
        if hasattr(module, attr):
            returns.append((module, attr, getattr(module, attr)))
        else:
            deletes.append((module, attr))
        setattr(module, attr, value)
    yield True
    for module, attr, value in returns:
        setattr(module, attr, value)
    for module, attr in deletes:
        delattr(module, attr)


def fake_http_connect(*code_iter, **kwargs):

    class FakeConn(object):

        def __init__(self, status, etag=None, body='', timestamp='1',
                     expect_status=None, headers=None):
            self.status = status
            if expect_status is None:
                self.expect_status = self.status
            else:
                self.expect_status = expect_status
            self.reason = 'Fake'
            self.host = '1.2.3.4'
            self.port = '1234'
            self.sent = 0
            self.received = 0
            self.etag = etag
            self.body = body
            self.headers = headers or {}
            self.timestamp = timestamp

        def getresponse(self):
            if kwargs.get('raise_exc'):
                raise Exception('test')
            if kwargs.get('raise_timeout_exc'):
                raise Timeout()
            return self

        def getexpect(self):
            if self.expect_status == -2:
                raise HTTPException()
            if self.expect_status == -3:
                return FakeConn(507)
            if self.expect_status == -4:
                return FakeConn(201)
            return FakeConn(100)

        def getheaders(self):
            etag = self.etag
            if not etag:
                if isinstance(self.body, str):
                    etag = '"' + md5(self.body).hexdigest() + '"'
                else:
                    etag = '"68b329da9893e34099c7d8ad5cb9c940"'

            headers = {'content-length': len(self.body),
                       'content-type': 'x-application/test',
                       'x-timestamp': self.timestamp,
                       'last-modified': self.timestamp,
                       'x-object-meta-test': 'testing',
                       'x-delete-at': '9876543210',
                       'etag': etag,
                       'x-works': 'yes',
                       'x-account-container-count': kwargs.get('count', 12345)}
            if not self.timestamp:
                del headers['x-timestamp']
            try:
                if container_ts_iter.next() is False:
                    headers['x-container-timestamp'] = '1'
            except StopIteration:
                pass
            if 'slow' in kwargs:
                headers['content-length'] = '4'
            headers.update(self.headers)
            return headers.items()

        def read(self, amt=None):
            if 'slow' in kwargs:
                if self.sent < 4:
                    self.sent += 1
                    sleep(0.1)
                    return ' '
            rv = self.body[:amt]
            self.body = self.body[amt:]
            return rv

        def send(self, amt=None):
            if 'slow' in kwargs:
                if self.received < 4:
                    self.received += 1
                    sleep(0.1)

        def getheader(self, name, default=None):
            return dict(self.getheaders()).get(name.lower(), default)

    timestamps_iter = iter(kwargs.get('timestamps') or ['1'] * len(code_iter))
    etag_iter = iter(kwargs.get('etags') or [None] * len(code_iter))
    if isinstance(kwargs.get('headers'), list):
        headers_iter = iter(kwargs['headers'])
    else:
        headers_iter = iter([kwargs.get('headers', {})] * len(code_iter))

    x = kwargs.get('missing_container', [False] * len(code_iter))
    if not isinstance(x, (tuple, list)):
        x = [x] * len(code_iter)
    container_ts_iter = iter(x)
    code_iter = iter(code_iter)
    static_body = kwargs.get('body', None)
    body_iter = kwargs.get('body_iter', None)
    if body_iter:
        body_iter = iter(body_iter)

    def connect(*args, **ckwargs):
        if 'give_content_type' in kwargs:
            if len(args) >= 7 and 'Content-Type' in args[6]:
                kwargs['give_content_type'](args[6]['Content-Type'])
            else:
                kwargs['give_content_type']('')
        if 'give_connect' in kwargs:
            kwargs['give_connect'](*args, **ckwargs)
        status = code_iter.next()
        if isinstance(status, tuple):
            status, expect_status = status
        else:
            expect_status = status
        etag = etag_iter.next()
        headers = headers_iter.next()
        timestamp = timestamps_iter.next()

        if status <= 0:
            raise HTTPException()
        if body_iter is None:
            body = static_body or ''
        else:
            body = body_iter.next()
        return FakeConn(status, etag, body=body, timestamp=timestamp,
                        expect_status=expect_status, headers=headers)

    return connect


def create_random_numbers(max_num, proto='pickle'):
    numlist = [i for i in range(max_num)]
    for i in range(max_num):
        randindex1 = random.randrange(max_num)
        randindex2 = random.randrange(max_num)
        numlist[randindex1], numlist[randindex2] =\
            numlist[randindex2], numlist[randindex1]
    if proto == 'binary':
        return struct.pack('%sI' % len(numlist), *numlist)
    else:
        return pickle.dumps(numlist, protocol=0)


def get_sorted_numbers(min_num=0, max_num=10, proto='pickle'):
    numlist = [i for i in range(min_num, max_num)]
    if proto == 'binary':
        return struct.pack('%sI' % len(numlist), *numlist)
    else:
        return pickle.dumps(numlist, protocol=0)


@contextmanager
def create_tar(name_and_file):
    tarfd, tarname = mkstemp()
    os.close(tarfd)
    tar = tarfile.open(name=tarname, mode='w')
    for name, f in name_and_file.iteritems():
        info = tarfile.TarInfo(name)
        f.seek(0, 2)
        size = f.tell()
        info.size = size
        f.seek(0, 0)
        tar.addfile(info, f)
    tar.close()
    try:
        yield tarname
    finally:
        try:
            os.unlink(tarname)
        except OSError:
            pass


def trim(script):
    return dedent(script[1:-1])
