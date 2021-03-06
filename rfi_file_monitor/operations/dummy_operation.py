import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

import time
from threading import current_thread
import logging

#pylint: disable=relative-beyond-top-level
from ..operation import Operation
from ..file import File

class DummyOperation(Operation):
    NAME = "Dummy Operation"

    def __init__(self, *args, **kwargs):
        Operation.__init__(self, *args, **kwargs)
        self._grid = Gtk.Grid(
            row_spacing=5,
            halign=Gtk.Align.FILL, valign=Gtk.Align.CENTER,
            hexpand=True, vexpand=False
        )
        self.add(self._grid)
        self._grid.attach(Gtk.Label(label='This is a dummy operation'), 0, 0, 1, 1)

    def run(self, file: File):
        thread = current_thread()
        for i in range(10):
            if thread.should_exit:
                logging.info(f"Killing thread {thread.name}")
                return False
            time.sleep(1.0)
            file.update_progressbar(self.index, (i + 1) * 10)

        # None indicates success, a string failure, with its contents set to an error message
        return None
    