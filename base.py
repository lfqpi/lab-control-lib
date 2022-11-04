"""
Base classes.
"""
import threading
import json
import logging
import os
import errno
import time
import atexit
import socket
import signal
from select import select

import zmq.log.handlers

from . import conf_path, FileDict
from .util.future import Future
from .util.proxydevice import proxycall


class MotorLimitsException(Exception):
    pass


class DeviceException(Exception):
    pass


class DaemonException(Exception):
    pass


def _recv_all(sock, EOL=b'\n'):
    """
    Receive all data from socket (until EOL)
    * all bytes *
    """
    ret = sock.recv(1024)
    if not ret:
        # This happens if the connection was closed at the other end
        return ret
    while not ret.endswith(EOL):
        ret += sock.recv(1024)
    return ret


def _send_all(sock, msg):
    """
    Convert str to byte (if needed) and send on socket.
    """
    if isinstance(msg, str):
        msg = msg.encode()
    sock.sendall(msg)


def nonblock(fin):
    """
    Decorator to make any function or method non-blocking
    """
    def fout(*args, **kwargs):
        block = 'block' not in kwargs or kwargs.pop('block')
        if block:
            return fin(*args, **kwargs)
        else:
            t = threading.Thread(target=fin, args=args, kwargs=kwargs)
            t.start()
            return t
    return fout


class emergency_stop:

    stop_method = None

    def __init__(self, stop_method):
        try:
            # Won't work if not in main thread
            signal.signal(signal.SIGINT, self.signal_handler)
        except ValueError:
            pass
        self.local_stop_method = stop_method

    @classmethod
    def set_stop_method(cls, stop_method):
        cls.stop_method = stop_method

    @classmethod
    def signal_handler(cls, sig, frame):
        if cls.stop_method is not None:
            cls.stop_method()

    def __enter__(self):
        self.set_stop_method(self.local_stop_method)

    def __exit__(self, exc_type, exc_value, traceback):
        self.__class__.stop_method = None


class DriverBase:
    """
    Base for all drivers
    """

    logger = None                       # Place-holder. Gets defined at construction.
    DEFAULT_LOGGING_ADDRESS = None      # The default address for logging broadcast

    def __init__(self):
        """
        Initialization.
        """
        # Get logger if not set in subclass
        if self.logger is None:
            self.logger = logging.getLogger(self.__class__.__name__)

        # Set default name here. Can be overriden by subclass, for instance to allow multiple instances to run
        # concurrently
        if not hasattr(self, 'name'):
            self.name = self.__class__.__name__

        if self.DEFAULT_LOGGING_ADDRESS is not None:
            pub_interface = f'tcp://*:{self.DEFAULT_LOGGING_ADDRESS[1]}'
            pub_handler = zmq.log.handlers.PUBHandler(pub_interface, root_topic=self.name)
            self.logger.addHandler(pub_handler)
            self.logger.info(f'Driver {self.name} publishing logs on {pub_interface}.')

        # Load (or create) config dictionary
        self.config_filename = os.path.join(conf_path, 'drivers', self.name + '.json')
        self.config = FileDict(self.config_filename)

        # Dictionary of metadata calls
        self.metacalls = {}
        self.initialized = False

    def init_device(self):
        """
        Device initalization.
        """
        self.initialized = True
        raise NotImplementedError

    @proxycall()
    def get_meta(self, metakeys=None):
        """
        Return the data described by the list
        of keys in metakeys. If metakeys is None: return all
        available meta.
        """

        if metakeys is None:
            metakeys = self.metacalls.keys()

        meta = {}
        for key in metakeys:
            call = self.metacalls.get(key)
            if call is None:
                meta[key] = 'unknown'
            else:
                meta[key] = call()
        return meta


class SocketDriverBase(DriverBase):
    """
    Base class for all drivers working through a socket.
    """

    EOL = b'\n'                         # End of API sequence (default is \n)
    DEFAULT_DEVICE_ADDRESS = None       # The default address of the device socket.
    DEVICE_TIMEOUT = None               # Device socket timeout
    NUM_CONNECTION_RETRY = 2            # Number of times to try to connect
    KEEPALIVE_INTERVAL = 10.            # Default Polling (keep-alive) interval
    logger = None

    def __init__(self, device_address):
        """
        Initialization.
        """
        super().__init__()

        # register exit functions
        atexit.register(self.shutdown)

        # Store device address
        self.device_address = device_address
        self.device_sock = None
        self.shutdown_requested = False

        self.logger.debug(f'Driver {self.name} will connect to {self.device_address[0]}:{self.device_address[1]}')

        # Attributes initialized (or re-initialized) in self.connect_device
        # Buffer in which incoming data will be stored
        self.recv_buffer = None
        # Flag to inform other threads that data has arrived
        self.recv_flag = None
        # Listening/receiving thread
        self.recv_thread = None
        # Receiver lock
        self._lock = threading.Lock()

        # Connect to device
        self.connected = False
        self.connect_device()

        # Initialize the device
        self.initialized = False
        self.init_device()
        self.logger.info('Device initialized')

        # Start polling
        # number of skipped answers in polling thread (not sure that's useful)
        self.device_N_noreply = 0
        # "keep alive" thread
        self.future_keep_alive = Future(self._keep_alive)

    def connect_device(self):
        """
        Device connection
        """
        # Prepare device socket connection
        self.device_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # TCP socket
        self.device_sock.settimeout(self.DEVICE_TIMEOUT)

        for retry_count in range(self.NUM_CONNECTION_RETRY):
            conn_errno = self.device_sock.connect_ex(self.device_address)
            if conn_errno == 0:
                break

            self.logger.critical(os.strerror(conn_errno))
            time.sleep(.05)

        if conn_errno != 0:
            self.logger.critical("Can't connect to device")
            raise DeviceException("Can't connect to device")

        # Start receiving data
        self.recv_buffer = b''
        self.recv_flag = threading.Event()
        self.recv_flag.clear()
        self.recv_thread = Future(target=self._listen_recv)

        self.connected = True
        self.logger.info(f'Driver {self.name} connected to {self.device_address[0]}:{self.device_address[1]}')

    def _listen_recv(self):
        """
        This threads receives all data in real time and stores it
        in a local buffer. For devices that send data only after
        receiving a command, the buffer is read and emptied immediately.
        """
        while True:
            rlist, _, elist = select([self.device_sock], [], [self.device_sock], .5)
            if elist:
                self.logger.critical('Exceptional event with device socket.')
                break
            if rlist:
                # Incoming data
                with self._lock:
                    d = _recv_all(rlist[0], EOL=self.EOL)
                    self.recv_buffer += d
                    self.recv_flag.set()
            if self.shutdown_requested:
                break

    def _keep_alive(self):
        """
        Infinite loop on a separate thread that pings the device periodically to keep the connection alive.

        TODO: figure out what to do if device dies.
        """
        while True:
            if not (self.connected and self.initialized):
                time.sleep(self.KEEPALIVE_INTERVAL)
                continue
            try:
                self.wait_call()
                self.device_N_noreply = 0
            except socket.timeout:
                self.device_N_noreply += 1
            except DeviceException:
                self.logger.critical('Device disconnected.')
                self.close_device()
            time.sleep(self.KEEPALIVE_INTERVAL)

    def wait_call(self):
        """
        Keep-alive call to the device
        If possible, the implementation should raise a
        DeviceDisconnectException if the device disconnects.
        """
        raise NotImplementedError

    def device_cmd(self, cmd: bytes) -> bytes:
        """
        Send command to the device, NOT adding EOL and return the reply.
        """
        if not self.connected:
            raise RuntimeError('Device not connected.')
        if not self.initialized:
            self.logger.info('Device not (yet?) initialized.')

        with self._lock:
            # Clear the "new data" flag so we can wait on the reply.
            self.recv_flag.clear()

            # Pass command to device
            _send_all(self.device_sock, cmd)

        # Wait for reply
        self.recv_flag.wait()

        # Just to be super safe: take the lock again
        with self._lock:
            # Reply is in the local buffer
            reply = self.recv_buffer

            # Clear the local buffer
            self.recv_buffer = b''

        return reply

    def terminal(self):
        """
        Create a terminal session to send commands directly to the device.
        """
        print('Enter command and hit return. Empty line will exit.')
        prompt = f'[{self.__name__}] >> '
        while True:
            cmd = input(prompt)
            if not cmd:
                break
            try:
                reply = self.device_cmd(cmd.encode() + self.EOL)
                print(reply)
            except Exception as e:
                print(repr(e))

    def close_device(self):
        """
        Driver clean up on shutdown.
        """
        self.device_sock.close()
        self.connected = False
        self.initialized = False

    def driver_status(self):
        """
        Some info about the current state of the driver.
        """
        raise NotImplementedError

    def shutdown(self):
        """
        Clean shutdown of the driver.
        """
        if not self.connected:
            return
        # Tell the polling thread to abort. This will ensure that all the rest is wrapped up
        self.shutdown_requested = True
        self.logger.info('Shutting down connection to driver.')

    def stop(self):
        if not self.connected:
            raise RuntimeError('Not connected.')
        self.close_device()
        return

    def restart(self):
        try:
            self.stop()
        except RuntimeError:
            pass
        self.connect_device()
        self.init_device()


class MotorBase:
    """
    Representation of a motor (any object that has one translation / rotation axis).

    User and dial positions are different and controlled by self.offset and self.scalar
    Dial = (User*scalar)-offset
    """
    def __init__(self, name, driver):
        # Store motor name and driver instance
        self.name = name
        self.driver = driver

        # Attributes
        self.offset = None
        self.scalar = None
        self.limits = None

        # Store logger
        self.logger = logging.getLogger(name)

        # File name for motor configuration
        self.config_file = os.path.join(conf_path, 'motors', name + '.json')

        # Load offset configs
        self._load_config()

    def _get_pos(self):
        """
        Return *dial* position in mm or degrees
        """
        raise NotImplementedError

    def _set_abs_pos(self, x):
        """
        Set absolute *dial* position in mm or degrees
        """
        raise NotImplementedError

    def _set_rel_pos(self, x):
        """
        Change position relative in mm or degrees
        """
        return self._set_abs_pos(self._get_pos() + (self.scalar * x))

    def _user_to_dial(self, user):
        """
        Converts user position to a dial position
        """
        return (user * self.scalar) - self.offset

    def _dial_to_user(self, dial):
        """
        Converts a dial position to a user position
        """
        return (dial + self.offset)/self.scalar

    def mv(self, x, block=True):
        """
        Absolute move to *user* position x

        Returns final USER position if block=True (default). If block=False, returns
        the thread that will terminate when motion is complete.
        """
        if not self._within_limits(x):
            raise MotorLimitsException()
        if not block:
            t = threading.Thread(target=self._set_abs_pos, args=[self._user_to_dial(x)])
            t.start()
            return t
        else:
            return self._dial_to_user(self._set_abs_pos(self._user_to_dial(x)))

    def mvr(self, x, block=True):
        """
        Relative move by position x

        Returns final USER position if block=True (default). If block=False, returns
        the thread that will terminate when motion is complete.
        """
        if not self._within_limits(self.pos + x):
            raise MotorLimitsException()
        if not block:
            t = threading.Thread(target=self._set_rel_pos, args=[self.scalar * x])
            t.start()
            return t
        else:
            return self._dial_to_user(self._set_rel_pos(self.scalar * x))

    def lm(self):
        """
        Return *user* soft limits
        """
        # Limits as stored in dialed values. Here they are offset into user values
        if self.scalar > 0:
            return self._dial_to_user(self.limits[0]), self._dial_to_user(self.limits[1])
        else:
            return self._dial_to_user(self.limits[1]), self._dial_to_user(self.limits[0])

    def set_lm(self, low, high):
        """
        Set *user* soft limits
        """
        if low >= high:
            raise RuntimeError("Low limit (%f) should be lower than high limit (%f)" % (low, high))
        # Limits are stored in dial values
        vals = [self._user_to_dial(low), self._user_to_dial(high)]  # to allow for scalar to be negative (flips limits)
        self.limits = (min(vals), max(vals))
        self._save_config()

    @property
    def pos(self):
        """
        Current *user* position
        """
        return (self._get_pos() + self.offset)/self.scalar

    @pos.setter
    def pos(self, value):
        self.mv(value)

    def where(self):
        """
        Return (dial, user) position
        """
        x = self._get_pos()
        return x, self._dial_to_user(x)

    def set(self, pos):
        """
        Set user position
        """
        self.offset = (self.scalar * pos) - self._get_pos()
        self._save_config()

    def set_scalar(self, scalar):
        """
        Set a scalar value for conversion between user and dial positions
        Dial = scalar*User_pos - offset
        """
        self.scalar = scalar
        self._save_config()
        print ('You have just changed the scalar. The motor limits may need to be manually updated.')

    def get_meta(self, returndict):
        """
        Place metadata in `returndict`.

        Note: returndict is used instead of a normal method return
        so that this method can be run on a different thread.
        """

        dx, ux = self.where()
        returndict['scalar'] = self.scalar
        returndict['offset'] = self.offset
        returndict['pos_dial'] = dx
        returndict['pos_user'] = ux
        returndict['lim_user'] = self.lm()
        returndict['lim_dial'] = self.limits
        returndict['driver'] = self.driver.name

        return

    def _within_limits(self, x):
        """
        Check if *user* position x is within soft limits.
        """
        return self.limits[0] < self._user_to_dial(x) < self.limits[1]

    def _save_config(self):
        """
        Save *dial* limits and offset
        """
        data = {'limits': self.limits, 'offset': self.offset, 'scalar': self.scalar}
        with open(self.config_file, 'w') as f:
            json.dump(data, f)

    def _load_config(self):
        """
        Load limits and offset
        """
        try:
            with open(self.config_file, 'r') as f:
                data = json.load(f)
        except IOError:
            self.logger.warning('Could not find config file "%s". Continuing with default values.' % self.config_file)
            # Create path
            try:
                os.makedirs(os.path.split(self.config_file)[0])
            except OSError as e:
                if e.errno == errno.EEXIST:
                    pass
                else:
                    raise
            self.limits = (-1., 1.)
            self.offset = 0.
            self.scalar = 1.
            # Save file
            self._save_config()
            return False
        self.limits = data["limits"]
        self.offset = data["offset"]
        self.scalar = data["scalar"]
        self.logger.info('Loaded stored limits, scalar and offset.')
        return True