import ctypes
import time

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QApplication, QWidget

from cowmata_tailring.workspace.threaded_engine import ThreadedWorkspaceEngine


class Stats(ctypes.Structure):
    _fields_ = [('displayed_pictures',ctypes.c_int),('decoded_video',ctypes.c_int)]


class FakeDecoder(QObject):
    error_occurred=Signal(str)
    calls=[]

    def __init__(self,widget,**kwargs):
        super().__init__(kwargs['parent'])
        self.owner_thread=QThread.currentThread()
        self.current_status='ready'
        self.last_error=''
        self.position=0
        self.closed=False
        self._dahua_duration_index=None
        time.sleep(.12)  # Simulates native allocation outside the GUI thread.

    def open(self,path):
        self.calls.append(('open',str(path)))
        return True

    def clear_pending_seek(self):
        pass

    def set_time_ms(self,target):
        time.sleep(.02)
        self.position=target
        self.calls.append(('seek',target))
        return True

    def pause(self,value):
        self.current_status='paused' if value else 'playing'

    def play(self):
        self.current_status='playing'
        return True

    def set_rate(self,rate):
        pass

    def set_volume(self,volume):
        pass

    def stats(self):
        return Stats(2,3)

    def get_time_ms(self):
        return self.position

    def duration_ms(self):
        return 10000

    def is_seekable(self):
        return True

    def video_output_count(self):
        return 1

    def close(self):
        self.closed=True


def test_native_calls_are_off_gui_and_commands_coalesce(tmp_path):
    app=QApplication.instance() or QApplication([])
    widget=QWidget()
    FakeDecoder.calls=[]
    proxy=ThreadedWorkspaceEngine(widget,metadata=lambda _: {},cache=tmp_path,backend_factory=FakeDecoder)
    try:
        begin=time.perf_counter()
        proxy.open(tmp_path/'001.mp4')
        for value in range(100):
            proxy.set_time_ms(value)
        proxy.play()
        assert time.perf_counter()-begin < .10  # Not a hardware performance SLA.
        assert proxy.stats() is None  # Never expose old-source/old-seek counters.
        deadline=time.monotonic()+4
        while proxy.get_time_ms()!=99 and time.monotonic()<deadline:
            app.processEvents()
            time.sleep(.01)
        assert proxy.get_time_ms()==99
        assert proxy.decoder.backend.owner_thread!=app.thread()
        assert len([c for c in FakeDecoder.calls if c[0]=='seek'])<5
        assert proxy.stats().displayed_pictures==2
        proxy.set_time_ms(500)
        assert proxy.stats() is None and proxy.get_time_ms()==0
    finally:
        proxy.close()
        deadline=time.monotonic()+4
        while proxy.thread_owner.isRunning() and time.monotonic()<deadline:
            app.processEvents()
            time.sleep(.01)
        assert not proxy.thread_owner.isRunning()
        widget.close()
