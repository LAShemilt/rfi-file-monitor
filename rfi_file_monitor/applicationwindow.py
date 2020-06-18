import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gio, Gtk

import logging
from collections import OrderedDict
from threading import RLock
from time import time, ctime
from pathlib import PurePath
from typing import OrderedDict as OrderedDictType
from typing import ClassVar, Final, List
import pkg_resources
import os

from .utils import add_action_entries
from .file import FileStatus, File
from .operation import Operation
from .job import Job

class ApplicationWindow(Gtk.ApplicationWindow):

    #MAX_JOBS = len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else os.cpu_count()
    MAX_JOBS = 2
    # max of cpus usable by this process

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._monitor: Final[Gio.FileMonitor] = None
        self._monitored_directory: str = None
        self._files_dict_lock = RLock()
        self._files_dict: OrderedDictType[str, File] = OrderedDict()
        self._jobs_list: Final[List[Job]] = list()

        self.set_default_size(1000, 1000)

        self._njobs_running: Final[int] = 0

        # get operations from entry points
        self._known_operations = {
            e.name: e.load() for e in pkg_resources.iter_entry_points("rfi_file_monitor.operations")
        }

        for _name, _class in self._known_operations.items():
            logging.debug(f"{_name} -> {_class().valid}")

        action_entries = (
            ("close", self.on_close),
            ("minimize", self.on_minimize),
        )

        # This doesn't work, which is kind of uncool
        # self.add_action_entries(action_entries)
        for action_entry in action_entries:
            add_action_entries(self, *action_entry)

        self.set_border_width(10)
        main_grid = Gtk.Grid(
            row_spacing=10,
            halign=Gtk.Align.FILL, valign=Gtk.Align.FILL,
            hexpand=True, vexpand=True)
        self.add(main_grid)

        controls_frame = Gtk.Frame(label="File Monitor Controls",
            halign=Gtk.Align.FILL, valign=Gtk.Align.FILL,
            hexpand=True, vexpand=False)
        main_grid.attach(controls_frame, 0, 0, 1, 1)
        
        controls_grid = Gtk.Grid(
            halign=Gtk.Align.FILL, valign=Gtk.Align.FILL,
            hexpand=True, vexpand=False,
            border_width=10, column_spacing=5, row_spacing=5)
        controls_frame.add(controls_grid)
        self._monitor_switch = Gtk.Switch(active=False,
            halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
            hexpand=False, vexpand=False)
        controls_grid.attach(self._monitor_switch, 0, 0, 1, 1)
        self._monitor_switch.connect("notify::active", self.monitor_switch_cb)
        self._monitor_switch.set_sensitive(False)
        self._directory_chooser_button = Gtk.FileChooserButton(
            title="Select a directory for monitoring",
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            create_folders=True,
            halign=Gtk.Align.FILL, valign=Gtk.Align.FILL,
            hexpand=True, vexpand=False)
        controls_grid.attach(self._directory_chooser_button, 1, 0, 5, 1)
        self._directory_chooser_button.connect("selection-changed", self.directory_chooser_button_cb)
        
        controls_grid.attach(
            Gtk.Label(
                label="Add operation: ",
                halign=Gtk.Align.END, valign=Gtk.Align.CENTER,
                hexpand=False, vexpand=False),
            0, 1, 2, 1)
        
        controls_operations_model = Gtk.ListStore(str, object)
        for _class in self._known_operations.values():
            controls_operations_model.append([_class.NAME, _class])
        self._controls_operations_combo = Gtk.ComboBox(
            model=controls_operations_model,
            halign=Gtk.Align.FILL, valign=Gtk.Align.CENTER,
            hexpand=False, vexpand=False,
            )
        

        renderer= Gtk.CellRendererText()
        self._controls_operations_combo.pack_start(renderer, True)
        self._controls_operations_combo.add_attribute(renderer, "text", 0)
        controls_grid.attach(
            self._controls_operations_combo,
            2, 1, 2, 1)

        controls_operations_button = Gtk.Button(
            label="Add",
            halign=Gtk.Align.START, valign=Gtk.Align.CENTER,
            hexpand=False, vexpand=False,
        )
        controls_operations_button.connect("clicked", self.operations_button_cb)
        controls_grid.attach(
            controls_operations_button,
            4, 1, 2, 1)

        if len(controls_operations_model) > 0:
            self._controls_operations_combo.set_active(0)
        else:
            controls_operations_button.set_sensitive(False)
            self._controls_operations_combo.set_sensitive(False)
        

        paned = Gtk.Paned(wide_handle=True,
            orientation=Gtk.Orientation.VERTICAL,
            halign=Gtk.Align.FILL, valign=Gtk.Align.FILL,
            hexpand=True, vexpand=True)
        main_grid.attach(paned, 0, 1, 1, 1)

        operations_frame = Gtk.Frame(
            label='List of Operations',
            halign=Gtk.Align.FILL, valign=Gtk.Align.FILL,
            hexpand=True, vexpand=True)
        paned.pack1(operations_frame, resize=True, shrink=False)
        operations_scrolled_window = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            halign=Gtk.Align.FILL, valign=Gtk.Align.FILL,
            hexpand=True, vexpand=True
        )
        operations_frame.add(operations_scrolled_window)
        self._operations_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=5,
            halign=Gtk.Align.FILL, valign=Gtk.Align.FILL,
            hexpand=True, vexpand=True
        )
        operations_scrolled_window.add(self._operations_box)

        output_frame = Gtk.Frame(
            label='Output',
            halign=Gtk.Align.FILL, valign=Gtk.Align.FILL,
            hexpand=True, vexpand=True)
        paned.pack2(output_frame, resize=True, shrink=False)
        
        self._files_tree_model = Gtk.TreeStore(
            str, # filename, relative to monitored directory
            int, # epoch time
            int, # status as code
            #str, # status as string
            str, # operation name
            float, # operation progress
            str, # operation progress as string
        )

        files_scrolled_window = Gtk.ScrolledWindow(
            halign=Gtk.Align.FILL, valign=Gtk.Align.FILL,
            hexpand=True, vexpand=True)
        output_frame.add(files_scrolled_window)

        files_tree_view = Gtk.TreeView(self._files_tree_model)
        files_scrolled_window.add(files_tree_view)

        renderer = Gtk.CellRendererText()
        column = Gtk.TreeViewColumn("Filename", renderer, text=0)
        files_tree_view.append_column(column)

        renderer = Gtk.CellRendererText()
        column = Gtk.TreeViewColumn("Created", renderer)
        column.set_cell_data_func(renderer, self.time_cell_data_func, func_data=dict(column=1))
        files_tree_view.append_column(column)

        renderer = Gtk.CellRendererText()
        column = Gtk.TreeViewColumn("Status", renderer)
        column.set_cell_data_func(renderer, self.status_cell_data_func, func_data=dict(column=2))
        files_tree_view.append_column(column)

        renderer = Gtk.CellRendererText()
        column = Gtk.TreeViewColumn("Operation", renderer, text=3)
        files_tree_view.append_column(column)

        renderer = Gtk.CellRendererProgress()
        column = Gtk.TreeViewColumn("Progress", renderer, value=4, text=5)
        files_tree_view.append_column(column)

    def time_cell_data_func(self, tree_column, cell, tree_model: Gtk.TreeStore, iter, func_data):
        # we currently dont write a timestamp for the individual operations
        if tree_model.iter_parent(iter) is not None:
            cell.set_property('text', "")
            return
        epoch = tree_model.get_value(iter, func_data['column'])
        date_string = ctime(epoch)
        cell.set_property('text', date_string)

    def status_cell_data_func(self, tree_column, cell, tree_model, iter, func_data):
        status = tree_model.get_value(iter, func_data['column'])
        status_string = str(FileStatus(status))
        cell.set_property('text', status_string)

    def monitor_switch_cb(self, button, active):
        self._directory_chooser_button.set_sensitive(not button.get_active())

        if button.get_active():
            # cleanup, launch the monitor
            self._files_tree_model.clear()
            self._timeout_id = GLib.timeout_add_seconds(1, self.files_dict_timeout_cb, priority=GLib.PRIORITY_DEFAULT)

            monitor_file = Gio.File.new_for_path(self._monitored_directory)
            self._monitor = monitor_file.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES)
            self._monitor_changed_id = self._monitor.connect("changed", self.monitor_cb)
        else:
            # disable the monitor
            self._monitor.disconnect(self._monitor_changed_id)
            self._monitor = None
            GLib.source_remove(self._timeout_id)
            with self._files_dict_lock:
                self._files_dict.clear()
                self._njobs_running = 0
            for job in self._jobs_list:
                job.should_exit = True
            self._jobs_list.clear()

    def file_created_cb(self, *user_data):
        file_path = user_data[0]
        with self._files_dict_lock:
            if file_path in self._files_dict:
                logging.warning("f{file_path} has been recreated! Ignoring...")
            else:
                logging.debug(f"New file {file_path} created")
                # add new entry to model
                _creation_timestamp = time()
                _relative_file_path = str(PurePath(file_path).relative_to(self._monitored_directory))
                iter = self._files_tree_model.append(parent=None, row=[
                    _relative_file_path,
                    _creation_timestamp,
                    int(FileStatus.CREATED),
                    "All",
                    0.0,
                    "0.0 %",
                    ])
                _row_reference = Gtk.TreeRowReference.new(self._files_tree_model, self._files_tree_model.get_path(iter))
                # create its children, one for each operation
                for _operation in self._operations_box:
                    self._files_tree_model.append(parent=iter, row=[
                        "",
                        0,
                        int(FileStatus.QUEUED),
                        _operation.NAME,
                        0.0,
                        "0.0 %",
                    ])
                _file = File(filename=file_path, relative_filename=_relative_file_path, created=_creation_timestamp, status=FileStatus.CREATED, row_reference=_row_reference)
                self._files_dict[file_path] = _file
        return GLib.SOURCE_REMOVE

    def monitor_cb(self, monitor, file, other_file, event_type):
        """
        This method is called whenever our monitored directory changed
        """
        file_path = file.get_path()
        logging.debug(f"Monitor found {file_path} for event type {event_type}")
        # file has been created -> add a new object to dict
        if event_type == Gio.FileMonitorEvent.CREATED:
            if (file_type := file.query_file_type(Gio.FileQueryInfoFlags.NONE)) == Gio.FileType.REGULAR:
                # new regular file -> add to dict and treemodel
                # give it very high priority!
                GLib.idle_add(self.file_created_cb, file_path, priority=GLib.PRIORITY_HIGH)
            elif file_type == Gio.FileType.DIRECTORY:
                # directory -> add a new file monitor since this is not working recursively
                pass
        # file has been saved -> kick off pipeline
        # need to check that this hasnt happened before!
        elif event_type == Gio.FileMonitorEvent.CHANGES_DONE_HINT:
            GLib.idle_add(self.file_changes_done_cb, file_path, priority=GLib.PRIORITY_DEFAULT_IDLE)

    def file_changes_done_cb(self, file_path):
        with self._files_dict_lock:
            if file_path not in self._files_dict:
                logging.warning("f{file_path} has not been created yet! Ignoring...")
            elif self._files_dict[file_path].status != FileStatus.CREATED:
                # looks like this file has been saved again!
                logging.warning(f"{file_path} has been saved again?? Ignoring!")
            else:
                logging.debug(f"File {file_path} has been saved")
                file = self._files_dict[file_path]
                file.status = FileStatus.SAVED
                path = file.row_reference.get_path()
                self._files_tree_model[path][2] = int(FileStatus.SAVED) 


    def update_monitor_switch_sensitivity(self):
        if self._monitored_directory and \
            self._monitor is None and \
            len(self._operations_box) > 0:
            self._monitor_switch.set_sensitive(True)
        else:
            self._monitor_switch.set_sensitive(False)

    def operations_button_cb(self, button):
        logging.debug("Clicked operations_button_cb")
        _class = self._controls_operations_combo.get_model()[self._controls_operations_combo.get_active_iter()][1]
        new_operation = _class()
        logging.debug(f"{type(new_operation)=}")
        new_operation.index = len(self._operations_box)
        self._operations_box.pack_start(new_operation, False, False, 0)
        new_operation.show_all()
        self.update_monitor_switch_sensitivity()

    def directory_chooser_button_cb(self, button):
        if monitored_directory := button.get_filename():
            self._monitored_directory = monitored_directory
            self.update_monitor_switch_sensitivity()
            self.set_title(f"Monitoring: {self._monitored_directory}")

    def on_minimize(self, action, param):
        self.iconify()

    def on_close(self, action, param):
        self.destroy()

    def files_dict_timeout_cb(self, *user_data):
        """
        This function runs every second, and will take action based on the status of all files in the dict
        It runs in the GUI thread, so GUI updates are allowed here.
        """
        with self._files_dict_lock:
            for _filename, _file in self._files_dict.items():
                #logging.debug(f"timeout_cb: {_filename} found as {str(_file.status)}")
                if _file.status == FileStatus.CREATED:
                    logging.debug(f"files_dict_timeout_cb: {_filename} was created")
                elif _file.status == FileStatus.SAVED:
                    if self._njobs_running < self.MAX_JOBS:
                        # launch a new job
                        logging.debug(f"files_dict_timeout_cb: launching new job for {_filename}")
                        job = Job(self, _file)
                        self._jobs_list.append(job)
                        job.start()
                        self._njobs_running += 1
                    else:
                        # queue the job
                        logging.debug(f"files_dict_timeout_cb: adding {_filename} to queue for future processing")
                        _file.status = FileStatus.QUEUED
                        path = _file.row_reference.get_path()
                        self._files_tree_model[path][2] = int(_file.status)
                elif _file.status == FileStatus.QUEUED:
                    if self._njobs_running < self.MAX_JOBS:
                        # try and launch a new job
                        logging.debug(f"files_dict_timeout_cb: launching queued job for {_filename}")
                        job = Job(self, _file)
                        self._jobs_list.append(job)
                        job.start()
                        self._njobs_running += 1
        return GLib.SOURCE_CONTINUE


