import datetime

from . import logs
from .filedict import FileDict
from .datalog import DataLogger
from .fake_device import FakeDevice
from .imstream import FramePublisher, FrameSubscriber
from . import viewers
def now():
    return str(datetime.datetime.today())