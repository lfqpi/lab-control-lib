
import threading

import napari
from napari.qt.threading import create_worker
from qtpy.QtWidgets import QPushButton
import numpy as np
import time
import logging

from .imstream import FrameSubscriber


class ViewerBase:

    DEFAULT_ADDRESS = ('localhost', 5555)

    def __init__(self, address=None, compress=False, max_fps=25, yield_timeout=15):
        """
        Base class for frame viewers. This class contains a FrameSubscriber that connects to a FramePublisher.
        The method yeild_new_frame is a generator that can be iterated over.

        address: tuple (ip, port) of the FramePublisher
        compress: whether to use JPG compressed images (not a good idea for now)
        max_fps: maximum FPS: Skip frames if they are incoming at a higher rate.
        yield_timeout: time in seconds after which the generator will stop yielding and return.
        """
        self.compress = compress
        self.max_fps = max_fps
        self.yield_timeout = yield_timeout

        self.logger = logging.getLogger(self.__class__.__name__)

        # Start with no frame source
        self.frame_subscriber = None

        if address is None:
            self.address = self.DEFAULT_ADDRESS
        else:
            self.address = address

        self.prepare_viewer()

    def prepare_viewer(self):
        """
        Backend-dependent viewer initialization
        """
        pass

    def update_viewer(self, frame_and_meta):
        """
        Show the frame. metadata is any metadata sent along with the frame.
        """
        pass

    def start_viewer(self):
        pass

    def stop_viewer(self):
        pass

    def yield_new_frame(self):
        """
        Generator that yields a new frame at a maximum rate of self.max_fps
        """
        twait = 1. / self.max_fps
        t0 = time.time()
        while True:
            try:
                frame, metadata = self.frame_subscriber.receive(1)
            except TimeoutError:
                self.logger.info('no data')
                if time.time() > t0 + self.yield_timeout:
                    return 'No frame sent within given timeout'
                else:
                    continue
            except AttributeError:
                return
            if self.compress:
                frame = self.uncompress(frame)
            yield frame, metadata
            time.sleep(twait)
            t0 = time.time()

    def start(self):
        """
        Initialize a subscriber to the frame source and start the viewer.
        """
        self.frame_subscriber = FrameSubscriber(address=self.address, frames=not self.compress)
        self.start_viewer()

    def stop(self):
        """
        Stop the subscriber and stop the viewer
        """
        self.stop_viewer()
        self.frame_subscriber.close()
        self.frame_subscriber = None

    def once(self, timeout=15):
        """
        Grab a single frame, waiting for maximum time timeout.
        """
        if self.frame_subscriber is not None:
            self.update_viewer(next(self.yield_new_frame()))
        else:
            with FrameSubscriber(address=self.address, frames=not self.compress) as f:
                frame, metadata = f.receive(timeout=timeout)
            self.update_viewer((frame, metadata))

    @staticmethod
    def uncompress(buffer):
        import cv2
        return cv2.imdecode(np.frombuffer(buffer, dtype='uint8'), -1)


class NapariViewer(ViewerBase):

    def __init__(self, address=None, compress=False, max_fps=25, yield_timeout=15):
        self.v = None
        self.worker = None
        self.epsize = None
        super().__init__(address=address, compress=compress, max_fps=max_fps, yield_timeout=yield_timeout)

    def prepare_viewer(self):
        pass

    def start_viewer(self):
        # find viewer from some list of instances (TODO), create new one if inexistent.
        self.v = napari.viewer.Viewer()

        # Napari thread worker
        self.worker = create_worker(self.yield_new_frame)

        # This will update the GUI each time the function yields
        self.worker.yielded.connect(self.update_viewer)

        # Create toggle start/pause button TODO: change all this.
        button = QPushButton("Pause")
        button.clicked.connect(self.worker.toggle_pause)
        self.worker.finished.connect(button.clicked.disconnect)

        # Add to napari viewer
        self.v.window.add_dock_widget(button, area='top')
        self.worker.start()

    def stop_viewer(self):
        self.worker.quit()

    def update_viewer(self, frame_and_meta):
        """
        Show the frame.
        """
        frame, metadata = frame_and_meta
        epsize = metadata.get('epsize')
        try:
            self.v.layers['Live View'].data = frame
            if epsize != self.epsize:
                self.epsize = epsize
                self.update_scalebar()
        except KeyError:
            # First time.
            self.v.add_image(frame, name='Live View')
            if epsize:
                self.epsize = epsize
                self.update_scalebar()

    def update_scalebar(self):
        """
        Update or add scalebar.
        """
        self.v.layers['Live View'].scale = [self.epsize, self.epsize]
        self.v.scale_bar.visible = True
        self.v.scale_bar.unit = 'um'
        self.v.reset_view()


class CvViewer(ViewerBase):

    def __init__(self, address=None, compress=False, max_fps=25, yield_timeout=15):
        import cv2
        self.cv2 = cv2
        self.thread = None
        self._stop = False
        super().__init__(address=address, compress=compress, max_fps=max_fps, yield_timeout=yield_timeout)

    def prepare_viewer(self):
        self.thread = threading.Thread(target=self._imshow)

    def start_viewer(self):
        self.thread.start()

    def stop_viewer(self):
        self._stop = True

    def _imshow(self):
        for frame_and_meta in self.yield_new_frame():
            self.update_viewer(frame_and_meta)
            if self._stop:
                break

    def update_viewer(self, frame_and_meta):
        """
        Show the frame.
        """
        frame, metadata = frame_and_meta
        self.cv2.imshow('Live View', frame)
        self.cv2.waitKey(1)
