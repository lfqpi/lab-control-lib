"""
Driver for the Lambda 350 by xpectrum, built on top of their python interface.
"""

import time
import os
import importlib.util
import logging
import numpy as np

from . import register_proxy_client
from .camera import CameraBase
from .network_conf import XSPECTRUM as NET_INFO
from .util.proxydevice import proxycall, proxydevice
from .util.future import Future

logger = logging.getLogger(__name__)

BASE_PATH = os.path.abspath(os.path.expanduser("~/data/"))

# Try to import pyxsp
if importlib.util.find_spec('pyxsp') is not None:
    import pyxsp
else:
    logger.debug("Module pyxsp unavailable on this host")
    class fake_pyxsp:
        def __getattr__(self, item):
            raise RuntimeError('Attempting to access "pyxsp" on a system where it is not present!')
    globals().update({'pyxsp': fake_pyxsp()})

__all__ = ['XSpectrum']


@register_proxy_client
@proxydevice(address=NET_INFO['control'])
class XSpectrum(CameraBase):
    """
    X-Spectrum lambda 350 Driver
    """

    BASE_PATH = BASE_PATH  # All data is saved in subfolders of this one
    PIXEL_SIZE = 55     # Physical pixel pitch in micrometers
    SHAPE = (1536, 1944)   # Native array shape (vertical, horizontal)
    DEFAULT_BROADCAST_PORT = NET_INFO['broadcast_port']
    DEFAULT_LOGGING_ADDRESS = NET_INFO['logging']
    SYSTEM_FILE = '/etc/opt/xsp/system.yml'

    def __init__(self, broadcast_port=None):
        """
        Initialization.

        TODO: implement gap time.
        TODO: implement multiple exposure mode (if needed)
        """
        super().__init__(broadcast_port=broadcast_port)

        self.system = None
        self.det = None
        self.rec = None
        self.init_device()

    def init_device(self):
        """
        Initialize camera.
        """

        s = pyxsp.System(self.SYSTEM_FILE)
        if not s:
            raise RuntimeError('Loading pyxsp system file failed.')

        # Identify detector and receiver
        det_ID = s.list_detectors()[0]
        self.logger.debug(f'Detector ID: {det_ID}')

        rec_ID = s.list_receivers()[0]
        self.logger.debug(f'Receiver ID: {rec_ID}')

        # Open detector and receiver
        det = s.open_detector(det_ID)
        rec = s.open_receiver(rec_ID)

        det.connect()
        det.initialize()

        self.logger.info('Lambda detector is online')

        self.system = s
        self.det = det
        self.rec = rec

        self.operation_mode = self.config.get('operation_mode')
        self.exposure_time = self.config.get('exposure_time', .2)
        self.exposure_number = self.config.get('exposure_number', 1)

        # self.initialized will be True only at completion of this Future
        self.future_init = Future(target=self._init)

    def _init(self):
        """
        Check if detector is ready
        """
        while not self.rec.ram_allocated:
            time.sleep(0.1)
        while not self.det.voltage_settled(1):
            time.sleep(0.1)
        self.logger.debug('Ram allocated and voltage settled.')
        self.initialized = True

    def _arm(self):
        """
        Arming X Spectrum detector: nothing to do apparently.
        """
        pass

    def _trigger(self):
        """
        Trigger the acquisition and manage frames.
        """
        num_frames = self.exposure_number
        exp_time = self.exposure_time
        rec = self.rec

        # Start acquiring
        self.logger.debug('Starting acquisition.')
        self.det.start_acquisition()

        # Trigger metadata collection
        self.grab_metadata.set()

        # Manage dual mode
        dual = (self.counter_mode == 'dual')
        if dual:
            frames = [[], []]
        else:
            frames = []
            fsub = frames

        pair = []

        n = 0
        while True:

            # Wait for frame
            frame = rec.get_frame(2000*exp_time)
            if not frame:
                self.det.stop_acquisition()
                raise RuntimeError('Timout during acquisition!')

            # Release RAM
            rec.release_frame(frame)

            # Check status
            if frame.status_code != pyxsp.FrameStatusCode.FRAME_OK:
                raise RuntimeError(f'Error reading frame: {frame.status_code.name}')

            if dual:
                if frame.subframe == 0:
                    self.logger.debug(f'Acquired frame {n}[0].')
                    pair = [np.array(frame.data)]
                    continue
                else:
                    self.logger.debug(f'Acquired frame {n}[1].')
                    pair.append(np.array(frame.data))
                    f = np.array(pair)
            else:
                self.logger.debug(f'Acquired frame {n}.')
                f = np.array(frame.data)

            # Get metadata
            self.metadata = self._manager.return_meta()

            # Already trigger next metadata collection if needed
            if self.metadata_every_exposure:
                self.grab_metadata.set()

            # Create metadata
            m = {'shape': (rec.frame_height, rec.frame_width),
                 'dtype': str(frame.dtype)}

            # Add frame to the queue
            self.enqueue_frame(f, m)

            # increment count
            n += 1

            if n == num_frames:
                break

    def _disarm(self):
        """
        Nothing to do on Lambda.
        """
        pass

    def _get_exposure_time(self):
        # Convert to seconds
        return self.det.shutter_time / 1000

    def _set_exposure_time(self, value):
        # Convert to milliseconds
        self.det.shutter_time = 1000 * value

    def _get_exposure_number(self):
        return self.det.number_of_frames

    def _set_exposure_number(self, value):
        self.det.number_of_frames = value

    def _get_operation_mode(self):
        opmode = {'beam_energy': self.det.beam_energy,
                  'bit_depth': self.bit_depth,
                  'charge_summing': self.charge_summing,
                  'counter_mode': self.counter_mode,
                  'thresholds': self.thresholds}

        return opmode

    def set_operation_mode(self, **kwargs):
        if beam_energy:=kwargs.get('beam_energy'):
            self.det.beam_energy = beam_energy
        if bit_depth:=kwargs.get('bit_depth'):
            self.bit_depth = bit_depth
        if charge_summing:=kwargs.get('charge_summing'):
            self.charge_summing = charge_summing
        if counter_mode:=kwargs.get('counter_mode'):
            self.counter_mode = counter_mode
        if thresholds:=kwargs.get('thresholds'):
            self.thresholds = thresholds

    def _get_binning(self):
        raise RuntimeError('Binning not available on this detector')

    def _set_binning(self, value):
        raise RuntimeError('Binning not available on this detector')

    def _get_psize(self):
        return self.PIXEL_SIZE

    def _get_shape(self) -> tuple:
        return self.SHAPE

    @proxycall(admin=True)
    @property
    def beam_energy(self):
        """
        Beam energy
        """
        return self.det.beam_energy

    @beam_energy.setter
    def beam_energy(self, value):
        self.det.beam_energy = value

    @proxycall(admin=True)
    @property
    def bit_depth(self):
        """
        Bit depth: 1, 6, 12, 24
        """
        return self.det.bit_depth.value

    @bit_depth.setter
    def bit_depth(self, value):
        if value == 1:
            self.det.bit_depth = pyxsp.BitDepth.DEPTH_1
        elif value == 6:
            self.det.bit_depth = pyxsp.BitDepth.DEPTH_6
        elif value == 12:
            self.det.bit_depth = pyxsp.BitDepth.DEPTH_12
        elif value == 24:
            self.det.bit_depth = pyxsp.BitDepth.DEPTH_24
        else:
            raise RuntimeError(f'Unknown or unsupported bit depth: {value}.')

    @proxycall(admin=True)
    @property
    def charge_summing(self):
        """
        Charge summing ('on', 'off')
        """
        return self.det.charge_summing.name.lower()

    @charge_summing.setter
    def charge_summing(self, value):
        if (value is True) or (value == 'on') or (value == 'ON'):
            self.det.charge_summing = pyxsp.ChargeSumming.ON
        elif (value is False) or (value == 'off') or (value == 'OFF'):
            self.det.charge_summing = pyxsp.ChargeSumming.OFF
        else:
            raise RuntimeError(f'charge_summing cannot be set to {value}.')

    @proxycall(admin=True)
    @property
    def counter_mode(self):
        """
        Counter mode ('single', 'dual')
        """
        return self.det.counter_mode.name.lower()

    @counter_mode.setter
    def counter_mode(self, value):
        if (value == 1) or (value == 'single') or (value == 'SINGLE'):
            self.det.counter_mode = pyxsp.CounterMode.SINGLE
        elif (value == 2) or (value == 'dual') or (value == 'DUAL'):
            self.det.ccounter_mode = pyxsp.CounterMode.DUAL
        else:
            raise RuntimeError(f'counter_mode cannot be set to {value}.')

    @proxycall(admin=True)
    @property
    def thresholds(self):
        """
        Energy thresholds in keV
        """
        return self.det.thresholds

    @thresholds.setter
    def thresholds(self, value):
        self.det.thresholds = value

    @proxycall()
    @property
    def temperature(self):
        """
        Sensor temperature (in degree C)
        """
        return self.det.temperature(1)
