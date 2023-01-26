"""
Base Camera for all detectors.

Highlights: The CameraBase class wraps detector operations in a uniform API.

** Main methods:
    *  snap(self, exp_time=None, exp_num=None)
    *  roll(self, switch)
    *  live_on(self)
    *  live_off(self)

** Properties:
    *  file_format (g/s)
    *  file_prefix (g/s)
    *  save_path (g/s)
    *  exposure_time (g/s)
    *  operation_mode (g/s)
    *  exposure_number (g/s)
    *  binning (g/s)
    *  psize (g)
    *  shape (g)
    *  magnification (g/s)
    *  epsize (g/s)
    *  live_fps (g/s)
    *  acquiring (g)
    *  storing (g)
    *  is_live (g)
    *  save (g/s)

The following methods have to be implemented

 * grab_frame(self, *args, **kwargs):
  The actual operation of grabbing one or multiple frames with currently stored parameters.

 * roll(self, switch=None):
  If available: start/stop endless continuous acquisition, e.g. for live view.
  ! NOT READY

 * getters and setters
    *  _get_exposure_time(self):
    *  _set_exposure_time(self, value)
    *  _get_exposure_number(self)
    *  _set_exposure_number(self, value)
    *  _get_operation_mode(self)
    *  _set_operation_mode(self, value)
    *  _get_binning(self)
    *  _set_binning(self, value)
    *  _get_psize(self)
    *  _get_shape(self) -> tuple

** File saving

File saving is enabled/disabled with CaneraBase.save = True/False
File naming uses the following recipe:

 filename = CameraBase.BASE_PATH + CameraBase.save_path + file_prefix + [extension]

 where:
  file_prefix is either CameraBase.file_prefix or CameraBase.file_prefix.format(self.counter)
  extension depends on CameraBase.file_format

** Within a SCAN (see experiment.Scan object)



"""
import os
import json
import threading
from queue import SimpleQueue, Empty

from optimatools.io.h5rw import h5write

from . import workflow, aggregate
from .base import DriverBase
from .util import now, FramePublisher
from .util.proxydevice import proxydevice, proxycall
from .util.future import Future
from .util import filewriter
from .util import filestreamer

DEFAULT_FILE_FORMAT = 'hdf5'
DEFAULT_BROADCAST_PORT = 5555


# No @proxydriver because this class is not meant to be instantiated
class CameraBase(DriverBase):
    """
    Base class for camera drivers, giving a uniform interface between detectors.
    """

    DEFAULT_BROADCAST_PORT = DEFAULT_BROADCAST_PORT  # Default port for frame broadcasting
    BASE_PATH = ""
    PIXEL_SIZE = (0, 0)            # Pixel size in um
    SHAPE = (0, 0)            # Native array dimensions (before binning)
    DATATYPE = 'uint16'            # Expected datatype
    DEFAULT_FPS = 5.
    MAX_FPS = 5.

    def __init__(self, broadcast_port=None):
        super().__init__()
        if broadcast_port is None:
            self.broadcast_port = self.DEFAULT_BROADCAST_PORT
        else:
            self.broadcast_port = broadcast_port

        # Set defaults if they are not set
        if 'do_save' not in self.config:
            self.save = True
        if 'file_format' not in self.config:
            self.file_format = DEFAULT_FILE_FORMAT
        if 'do_broadcast' not in self.config:
            self.config['do_broadcast'] = True
        if 'magnification' not in self.config:
            self.magnification = 1.
        if 'counter' not in self.config:
            self.counter = 0

        self.acq_future = None        # Will be replaced with a future when starting to acquire.
        self.store_future = None      # Will be replaced with a future when starting to store.
        self._stop_roll = False       # To interrupt rolling

        # File writing process
        self.file_writer = filewriter.H5FileWriter.start_process()

        # Prepare metadata collection
        aggregate.connect()
        self.metadata = {}
        self.localmeta = {}
        self.grab_metadata = threading.Event()
        self.meta_future = Future(self.metadata_loop)

        self.do_acquire = threading.Event()
        self.acquire_done = threading.Event()
        self.end_acquisition = False

        # Broadcasting
        self.file_streamer = filestreamer.FileStreamer.start_process(self.broadcast_port)
        if self.config['do_broadcast']:
            self.file_streamer.on()

        # Scan managemnt
        self._experiment = workflow.getExperiment()
        self._scan_path = None
        self.in_scan = False

        self.filename = None

        # Other flags
        self.loop_future = None
        self.armed = False

        self.closing = False

        self.auto_armed = False

        self.rolling = False

    def _trigger(self, *args, **kwargs):
        """
        The device-specific triggering and acquisition procedure.

        * Blocks until acquisition is done.
        * Does not return anything.
        """
        raise NotImplementedError

    def _readout(self, *args, **kwargs):
        """
        The device-specific readout (and possible reset) procedure.

        * Executed after self.trigger returns
        * returns frame, meta
        """
        raise NotImplementedError

    def _arm(self):
        """
        The device-specific arming procedure. Sets up everything so that self._trigger()
        starts an acquisition without delay.
        """
        pass

    def _disarm(self):
        """
        The device-specific disarming procedure.
        """
        pass

    def _rearm(self):
        """
        The device-specific rearming procedure (optional)
        """
        pass

    #
    # INTERNAL METHODS
    #

    @proxycall(admin=True, block=False)
    def snap(self, exp_time=None, exp_num=None):
        """
        Capture one or multiple images

        exp_time and exp_num are optional values
        that change self.exposure_time and self.exposure_number
        before proceeding with the acquisition. NOTE: the previous
        values of these parameters are not reset aftwerwards.
        """
        # If the camera is not armed, we arm it and remember that it was done automatically in snap
        if not self.armed:
            self.logger.debug('Camera was not armed when calling snap. Arming first.')
            self.auto_armed = True
            self.arm(exp_time=exp_time, exp_num=exp_num)

        # Camera is armed
        # Build filename
        if self.in_scan:
            self.filename = self._build_filename(prefix=self._experiment.next_prefix(), path=self._scan_path)
        else:
            self.counter += 1
            self.filename = self._build_filename(prefix=self.file_prefix, path=self.save_path)

        self.logger.info(f'Save path: {self.filename}')

        # Trigger next acquisition now
        self.do_acquire.set()

        # Wipe previous metadata and start collecting new one immediately
        self.metadata = {}
        self.grab_metadata.set()

        # Wait for the end of the acquisition
        self.acquire_done.wait()
        self.acquire_done.clear()

        return

    def acquisition_loop(self):
        """
        Main acquisition loop. Started at the end of the arming procedure
        and running on a thread.
        """
        self.logger.debug('Acquisition loop started')
        while True:

            # Wait for the next trigger
            if not self.do_acquire.wait(1):
                if self.end_acquisition:
                    break
                continue
            self.do_acquire.clear()

            # trigger acquisition with subclassed method and wait until it is done
            self._trigger()

            # Flip flag immediately to allow snap to return.
            self.acquire_done.set()

            # Read out data
            frame, meta = self._readout()

            # Combine metadata
            self.localmeta['acquisition_end'] = now()
            self.localmeta.update(meta)
            self.metadata[self.name] = self.localmeta

            # Broadcast and store
            self.file_streamer.store('', self.metadata, frame)

            if self.rolling:
                # Ask immediately for another frame
                self.do_acquire.set()
                continue

            # Not rolling, so saving
            self.file_writer.store(filename=self.filename, meta=self.metadata, data=frame)

            # Automatically armed - this is a single shot
            if self.auto_armed:
                self.auto_armed = False
                break

            # Get ready for next acquisition
            self._rearm()

        # The loop is closed, so we disarm
        self.disarm()
        self.logger.debug('Acquisition loop completed')

    def metadata_loop(self):
        """
        Running on a thread. Waiting for the "grab_metadata flag to be flipped, then
        attach most recent metadata to self.metadata
        """
        self.logger.debug('Metadata loop started')
        while True:
            if not self.grab_metadata.wait(1):
                if self.closing:
                    return
                continue
            self.grab_metadata.clear()

            # Global metadata
            self.metadata.update(aggregate.get_all_meta())

            # Local metadata
            self.localmeta = self.get_local_meta()
            self.localmeta['acquisition_start'] = now()
        self.logger.debug('Metadata loop completed')

    def get_local_meta(self):
        """
        Return camera-specific metadata
        """
        meta = {'detector': self.name,
                'scan_name': workflow.getExperiment().scan_name,
                'psize': self.psize,
                'epsize': self.epsize,
                'exposure_time': self.exposure_time,
                'operation_mode': self.operation_mode}
        return meta

    def _build_filename(self, prefix, path) -> str:
        """
        Build the full file name to save to.
        """

        # Try to replace counter of prefix is a format string.
        try:
            prefix = prefix.format(self.counter)
        except NameError:
            pass

        full_file_prefix = os.path.join(self.BASE_PATH, path, prefix)

        # Add extension based on file format
        if self.file_format == 'hdf5':
            filename = full_file_prefix + '.h5'
        elif self.file_format == 'tiff':
            filename = full_file_prefix + '.tif'
        else:
            raise RuntimeError(f'Unknown file format: {self.file_format}.')
        return filename

    @proxycall(admin=True)
    def arm(self, exp_time=None, exp_num=None):
        """
        Prepare the camera for acquisition.
        """
        if exp_time is not None:
            if exp_time != self.exposure_time:
                self.logger.info(f'Exposure time: {self.exposure_time} -> {exp_time}')
                self.exposure_time = exp_time
        if exp_num is not None:
            if exp_num != self.exposure_number:
                self.logger.info(f'Exposure number: {self.exposure_number} -> {exp_num}')
                self.exposure_number = exp_num

        # Check if this is part of a scan
        if not self._experiment:
            self._experiment = workflow.getExperiment()
        scan_path = self._experiment.scan_path
        self.in_scan = scan_path is not None
        self._scan_path = scan_path

        # Finish arming with subclassed method
        self._arm()

        # Start the main acquisition loop
        self.loop_future = Future(self.acquisition_loop)

        self.armed = True

    @proxycall(admin=True)
    def disarm(self):
        """
        Terminate acquisition.
        """
        # Terminate acquisition loop
        self.end_acquisition = True

        # Disarm with subclassed method
        self._disarm()

        # Reset flags
        self.in_scan = False
        self.armed = False


    @proxycall(admin=True, block=False)
    def roll(self, switch=None, fps=None):
        """
        Start endless sequence acquisition for live mode.

        If switch is None: toggle rolling state, otherwise turn on (True) or off (False)
        """
        # If currently rolling
        if self.rolling:
            if switch:
                return
            # Stop rolling and disarm the camera
            self.rolling = False
            self.disarm()
            return

        if switch is False:
            return

        # Start rolling
        if not self.is_live:
            self.live_on()

        self.rolling = True

        # Adjust exposure time
        fps = fps or self.DEFAULT_FPS

        if fps > self.MAX_FPS:
            self.logger.warning(f'Requested FPS ({fps}) is higher than the maximum allowed value ({self.MAX_FPS}).')
            fps = self.MAX_FPS
        self.exposure_time = 1./fps

        # Trigger the first acquisition immediately
        self.do_acquire.set()
        # Arm the camera
        if not self.armed:
            self.arm()

    @proxycall(admin=True)
    def reset_counter(self, value=0):
        """
        Reset internal counter to 0 (or to specified value)
        """
        self.counter = value

    @proxycall()
    def settings_json(self) -> str:
        """
        Return all current settings jsoned.
        """
        settings = {'exposure_time': self.exposure_time,
                    'exposure_number': self.exposure_number,
                    'operation_mode': self.operation_mode,
                    'file_format': self.file_format,
                    'file_prefix': self.file_prefix,
                    'save_path': self.save_path,
                    'magnification': self.magnification}
        return json.dumps(settings)

    @proxycall()
    def set_log_level(self, level):
        """
        Set logging level - also for the filewriter process.
        """
        super().set_log_level(level)
        self.file_writer.exec(f'self.logger.setLevel({level})')

    def shutdown(self):
        # Stop rolling
        self.roll(switch=False)
        # Stop file_writer process
        self.file_writer.stop()
        # Stop metadata loop
        self.closing = True

    #
    # GETTERS / SETTERS TO IMPLEMENT IN SUBCLASSES
    #

    def _get_exposure_time(self):
        """
        Return exposure time in seconds
        """
        raise NotImplementedError

    def _set_exposure_time(self, value):
        """
        Set exposure time
        """
        raise NotImplementedError

    def _get_exposure_number(self):
        """
        Return exposure number
        """
        raise NotImplementedError

    def _set_exposure_number(self, value):
        """
        Return exposure number
        """
        raise NotImplementedError

    def _get_operation_mode(self):
        """
        Return operation mode
        """
        raise NotImplementedError

    # Operation mode is a special case: 'value' is a dictionary
    # So it's convenient to have a real setter

    def set_operation_mode(self, **kwargs):
        """
        Set operation mode based on key pair arguments.
        """
        raise NotImplementedError

    def _set_operation_mode(self, value):
        """
        Set operation mode
        """
        value = value or {}
        self.set_operation_mode(**value)

    def _get_binning(self):
        """
        Return binning
        """
        raise NotImplementedError

    def _set_binning(self, value):
        """
        Set binning
        """
        raise NotImplementedError

    def _get_psize(self):
        """
        Return pixel size in mm, taking into account binning.
        """
        raise NotImplementedError

    def _get_shape(self) -> tuple:
        """
        Return array shape, taking into account ROI, binning etc.
        """
        raise NotImplementedError

    #
    # PROPERTIES
    #

    @proxycall(admin=True)
    @property
    def file_format(self):
        """
        File format
        """
        return self.config['file_format']

    @file_format.setter
    def file_format(self, value):
        if value.lower() in ['h5', 'hdf', 'hdf5']:
            self.config['file_format'] = 'hdf5'
        elif value.lower() in ['tif', 'tiff']:
            self.config['file_format'] = 'tiff'
        else:
            raise RuntimeError(f'Unknown file format: {value}')

    @proxycall(admin=True)
    @property
    def file_prefix(self):
        """
        File prefix
        """
        return self.config['file_prefix']

    @file_prefix.setter
    def file_prefix(self, value):
        self.config['file_prefix'] = value

    @proxycall(admin=True)
    @property
    def save_path(self):
        """
        Return save path
        """
        return self.config['save_path']

    @save_path.setter
    def save_path(self, value):
        """
        Set save path
        """
        self.config['save_path'] = value

    @proxycall(admin=True)
    @property
    def exposure_time(self):
        """
        Exposure time in seconds.
        """
        return self._get_exposure_time()

    @exposure_time.setter
    def exposure_time(self, value):
        self._set_exposure_time(value)
        self.config['settings']['exposure_time'] = value

    @proxycall(admin=True)
    @property
    def operation_mode(self):
        """
        Set exposure mode.
        """
        return self._get_operation_mode()

    @operation_mode.setter
    def operation_mode(self, value):
        self._set_operation_mode(value)

    @proxycall(admin=True)
    @property
    def exposure_number(self):
        """
        Number of exposures.
        """
        return self._get_exposure_number()

    @exposure_number.setter
    def exposure_number(self, value):
        self._set_exposure_number(value)
        self.config['settings']['exposure_number'] = value

    @proxycall(admin=True)
    @property
    def binning(self):
        """
        Binning type.
        """
        return self._get_binning()

    @binning.setter
    def binning(self, value):
        self._set_binning(value)
        self.config['settings']['binning'] = value

    @proxycall()
    @property
    def psize(self):
        """
        Pixel size in um (taking into account binning)
        """
        return self._get_psize()

    @proxycall()
    @property
    def shape(self):
        """
        Array shape (taking into account binning)
        """
        return self._get_shape()

    @proxycall(admin=True)
    @property
    def magnification(self):
        """
        Geometric magnification
        """
        return self.config['magnification']

    @magnification.setter
    def magnification(self, value):
        self.config['magnification'] = float(value)

    @proxycall(admin=True)
    @property
    def epsize(self):
        """
        *Effective* pixel size (taking into account both binning and geometric magnification)
        """
        return self.psize / self.magnification

    @epsize.setter
    def epsize(self, new_eps):
        """
        Set the *effective* pixel size. This effectively changes the magnification
        """
        self.magnification = self.psize / new_eps

    @proxycall(admin=True)
    @property
    def live_fps(self):
        """
        Set FPS for live mode.
        """
        return self.config['live_fps']

    @live_fps.setter
    def live_fps(self, value):
        self.config['live_fps'] = int(value)

    @proxycall(admin=True)
    def live_on(self):
        """
        Start broadcaster.
        """
        self.file_streamer.on()
        self.config['do_broadcast'] = True

    @proxycall(admin=True)
    def live_off(self):
        """
        Start broadcaster.
        """
        self.file_streamer.off()
        self.config['do_broadcast'] = False

    @proxycall()
    @property
    def is_live(self):
        """
        Check if camera is live.
        """
        return self.config['do_broadcast']

    @proxycall(admin=True)
    @property
    def save(self):
        """
        If False, frames are not saved on file.
        """
        return self.config['do_save']

    @save.setter
    def save(self, value: bool):
        self.config['do_save'] = bool(value)

    @property
    def counter(self):
        """
        Internal counter for file naming outside of scans
        """
        return self.config['counter']

    @counter.setter
    def counter(self, value: int):
        self.config['counter'] = value
