"""Image Panel using direct connection to Fly2 API
   for Point Grey FlyCapture2 cameras
"""

import wx
import time

from .imagepanel_base import ImagePanel_Base, ConfPanel_Base
from epics.wx.utils import  pack, FloatCtrl, Closure, add_button

HAS_FLY2 = False
try:
    import pyfly2
    HAS_FLY2 = True
except ImportError:
    pass

class ImagePanel_Fly2(ImagePanel_Base):
    """Image Panel for FlyCapture2 camera"""
    def __init__(self, parent,  camera_id=0, writer=None,
                 autosave_file=None, **kws):
        if not HAS_FLY2:
            raise ValueError("Fly2 library not available")

        super(ImagePanel_Fly2, self).__init__(parent, -1,
                                              size=(800, 600),
                                              writer=writer,
                                              autosave_file=autosave_file, **kws)

        self.context = pyfly2.Context()
        self.camera = self.context.get_camera(camera_id)
        self.img_w = 800.5
        self.img_h = 600.5
        self.writer = writer
        self.cam_name = '-'

        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.onTimer, self.timer)

    def Start(self):
        "turn camera on"
        self.camera.Connect()
        self.cam_name = self.camera.info['modelName']
        try:
            self.camera.StartCapture()
            width, height = self.camera.GetSize()
            self.img_w = float(width+0.5)
            self.img_h = float(height+0.5)
        except:
            pass

        self.timer.Start(50)
        if self.autosave_thread is not None:
            self.autosave_thread.start()

    def Stop(self):
        "turn camera off"
        self.timer.Stop()
        self.autosave = False
        self.camera.StopCapture()
        if self.autosave_thread is not None:
            self.autosave_thread.join()

    def GrabWxImage(self, scale=1, rgb=True, can_skip=True):
        try:
            return self.camera.GrabWxImage(scale=scale, rgb=rgb)
        except pyfly2.FC2Error:
            raise ValueError("could not grab camera image")

LEFT = wx.ALIGN_LEFT|wx.EXPAND

class ConfPanel_Fly2(ConfPanel_Base):
    def __init__(self, parent, image_panel=None, camera_id=0,
                 center_cb=None, xhair_cb=None, **kws):
        super(ConfPanel_Fly2, self).__init__(parent, center_cb=center_cb,
                                             xhair_cb=xhair_cb)
        self.image_panel = image_panel
        self.camera_id = camera_id
        self.camera = self.image_panel.camera

        wids = self.wids
        sizer = self.sizer

        self.title = self.txt("Fly2Capture: ", size=285)

        sizer.Add(self.title, (0, 0), (1, 3), LEFT)
        next_row = self.show_position_info(row=1)

        self.__initializing = True
        i = next_row + 1
        #('Sharpness', '%', 100), ('Hue', 'deg', 100), ('Saturation', '%', 100),
        for dat in (('shutter', 'ms', 70),
                    ('gain', 'dB', 24),
                    ('brightness', '%', 6),
                    ('gamma', '', 5)):

            key, units, maxval = dat
            wids[key] = FloatCtrl(self, value=0, maxval=maxval,
                                  precision=1,
                                  action=self.onValue,
                                  act_on_losefocus=True,
                                  action_kw={'prop': key}, size=(75, -1))
            label = '%s' % (key.title())
            if len(units)> 0:
                label = '%s (%s)' % (key.title(), units)
            sizer.Add(self.txt(label), (i, 0), (1, 1), LEFT)
            sizer.Add(wids[key],  (i, 1), (1, 1), LEFT)

            akey = '%s_auto' % key
            wids[akey] =  wx.CheckBox(self, -1, label='auto')
            wids[akey].SetValue(0)
            wids[akey].Bind(wx.EVT_CHECKBOX, Closure(self.onAuto, prop=key))
            sizer.Add(wids[akey], (i, 2), (1, 1), LEFT)
            i = i + 1

        for color in ('blue', 'red'):
            key = 'wb_%s' % color
            wids[key] = FloatCtrl(self, value=0, maxval=1024,
                                  precision=0,
                                  action=self.onValue,
                                  act_on_losefocus=True,
                                  action_kw={'prop': key}, size=(75, -1))
            label = 'White Balance (%s)' % (color)
            sizer.Add(self.txt(label), (i, 0), (1, 1), LEFT)
            sizer.Add(wids[key],  (i, 1), (1, 1), LEFT)

            if color == 'blue':
                akey = 'wb_auto'
                wids[akey] =  wx.CheckBox(self, -1, label='auto')
                wids[akey].SetValue(0)
                wids[akey].Bind(wx.EVT_CHECKBOX, Closure(self.onAuto, prop=key))
                sizer.Add(wids[akey], (i, 2), (1, 1), LEFT)
            i += 1

        pack(self, sizer)
        self.__initializing = False
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.onTimer, self.timer)
        wx.CallAfter(self.onConnect)

    def onConnect(self, **kws):
        for key in ('shutter', 'gain', 'brightness', 'gamma'):
            props = self.camera.GetProperty(key)
            self.wids[key].SetValue(props['absValue'])
            akey = '%s_auto' % key
            self.wids[akey].SetValue({False: 0, True: 1}[props['autoManualMode']])

        props = self.camera.GetProperty('white_balance')
        self.wids['wb_red'].SetValue(props['valueA'])
        self.wids['wb_blue'].SetValue(props['valueB'])
        self.wids['wb_auto'].SetValue({False: 0, True: 1}[props['autoManualMode']])
        self.timer.Start(1000)
        self.title.SetLabel("Fly2Capture: %s" % self.image_panel.cam_name)

    def onTimer(self, evt=None, **kws):
        for prop in ('shutter', 'gain', 'brightness', 'gamma', 'white_balance'):
            try:
                pdict = self.camera.GetProperty(prop)
            except pyfly2.FC2Error:
                return
            if pdict['autoManualMode']:
                if  prop == 'white_balance':
                    self.wids['wb_red'].SetValue(pdict['valueA'])
                    self.wids['wb_blue'].SetValue(pdict['valueB'])
                else:
                    self.wids[prop].SetValue(pdict['absValue'])

    def onAuto(self, evt=None, prop=None, **kws):
        if not evt.IsChecked():
            return
        if prop in ('wb_red', 'wb_blue', 'wb_auto'):
            prop = 'white_balance'
        try:
            if prop in ('shutter', 'gain', 'brightness', 'gamma'):
                pdict = self.camera.GetProperty(prop)
                self.camera.SetPropertyValue(prop, pdict['absValue'], auto=True)
                time.sleep(0.5)
                pdict = self.camera.GetProperty(prop)
                self.wids[prop].SetValue(pdict['absValue'])
            elif prop == 'white_balance':
                red =  self.wids['wb_red'].GetValue()
                blue = self.wids['wb_blue'].GetValue()
                self.camera.SetPropertyValue(prop, (red, blue), auto=True)
                time.sleep(0.5)
                pdict = self.camera.GetProperty(prop)
                self.wids['wb_red'].SetValue(pdict['valueA'])
                self.wids['wb_blue'].SetValue(pdict['valueB'])
        except pyfly2.FC2Error:
            return

    def onValue(self, prop=None, value=None,  **kws):
        if self.__initializing:
            return
        if prop in ('wb_red', 'wb_blue', 'wb_auto'):
            prop = 'white_balance'
        try:
            if prop in ('shutter', 'gain', 'brightness', 'gamma'):
                auto = self.wids['%s_auto' % prop].GetValue()
                self.camera.SetPropertyValue(prop, float(value), auto=auto)
            elif prop == 'white_balance':
                red =  self.wids['wb_red'].GetValue()
                blue = self.wids['wb_blue'].GetValue()
                auto = self.wids['wb_auto'].GetValue()
                self.camera.SetPropertyValue(prop, (red, blue), auto=auto)
        except pyfly2.FC2Error:
            return
