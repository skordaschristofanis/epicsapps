#!/usr/bin/env python
"""
SQLAlchemy wrapping of Instrument database

Main Class for full Database:  InstrumentDB

classes for Tables:
  Instrument
  Position
"""

import os
import json
import epics
import time
import socket

from datetime import datetime
from sqlalchemy import Row

from .utils import backup_versions, save_backup, normalize_pvname
from .creator import make_newdb

from .simpledb import (SimpleDB, isSimpleDB, get_credentials, json_encode,
                       isotime, isotime2datetime)


from . import upgrades

def isInstrumentDB(dbname):
    """test if a file is a valid Instrument Library file:
       must be a sqlite db file, with tables named
          'info', 'instrument', 'position', 'pv',
       'info' table must have an entries named 'version' and 'create_date'
    """
    return isSimpleDB(dbname,
                      tables=['info', 'instrument', 'position', 'pv'])

def valid_score(score, smin=0, smax=5):
    """ensure that the input score is an integr
    in the range [smin, smax]  (inclusive)"""
    return max(smin, min(smax, int(score)))


def None_or_one(val, msg='Expected 1 or None result'):
    """expect result (as from query.all() to return
    either None or exactly one result
    """
    if len(val) == 1:
        return val[0]
    elif len(val) == 0:
        return None
    else:
        raise InstrumentDBException(msg)


class InstrumentDBException(Exception):
    """DB Access Exception: General Errors"""
    def __init__(self, msg):
        Exception.__init__(self)
        self.msg = msg
    def __str__(self):
        return self.msg

class InstrumentDB(SimpleDB):
    "interface to Instrument Database"
    def __init__(self, dbname=None, **kws):
        if dbname is None:
            self.conndict = get_credentials(envvar='ESCAN_CREDENTIALS')
            dbname = self.conndict.get('dbname', dbname)
        else:
            self.conndict = {'dbname': dbname}
        self.conndict.update(kws)
        self.tables = None
        self.engine = None
        self.session = None
        self.conn    = None
        self.metadata = None
        self.pvs = {}
        self.pvtypes = {}
        SimpleDB.__init__(self, **self.conndict)
        self.connect_pvs()


    def connect_pvs(self):
        for row in self.get_rows('pv'):
            self.pvs[row.name] = epics.get_pv(row.name, form='time')
        time.sleep(0.01)

    def create_newdb(self, dbname, connect=False):
        "create a new, empty database"
        backup_versions(dbname)
        make_newdb(dbname)
        self.conndict['dbname'] = dbname
        if connect:
            time.sleep(0.5)
            self.connect(**self.conndict)

    def check_version(self):
        version_string = self.get_info('version')
        if version_string < '1.2':
            print('Upgrading Database to Version 1.2')
            for statement in upgrades.sqlcode['1.2']:
                self.session.execute(statement)
            self.set_info('version', '1.2')

        if version_string < '1.3':
            print('Upgrading Database to Version 1.3')
            for statement in upgrades.sqlcode['1.3']:
                self.session.execute(statement)
            self.set_info('version', '1.3')


    def commit(self):
        "commit session state"
        self.set_info('modify_date', datetime.isoformat(datetime.now()))
        return self.session.commit()

    def query(self, *args, **kws):
        "generic query"
        return self.session.query(*args, **kws)

    def set_hostpid(self, clear=False):
        """set hostname and process ID, as on intial set up"""
        name, pid = '', '0'
        if not clear:
            name, pid = socket.gethostname(), str(os.getpid())
        self.set_info('host_name', name)
        self.set_info('process_id', pid)

    def check_hostpid(self):
        """check whether hostname and process ID match current config"""
        db_host_name = self.get_info('host_name', default='')
        db_process_id  = self.get_info('process_id', default='0')
        return ((db_host_name == '' and db_process_id == '0') or
                (db_host_name == socket.gethostname() and
                 db_process_id == str(os.getpid())))

    def get_all_instruments(self):
        """return instrument list
        """
        return self.get_rows('instrument')

    def get_instrument(self, name):
        """return instrument by name
        """
        return self.get_rows('instrument', where={'name': name},
                             limit_one=True, none_if_empty=True)

    # def get_ordered_instpvs(self, inst):
    def get_instrument_pvs(self, inst):
        """get dict of {pvname: pv_id} ordered by 'display_order' for an instrument"""
        inst = self.get_instrument(inst)
        instpvs = {}
        allpvs = self.get_allpvs()
        for row in self.get_rows('instrument_pv',
                                 where={'instrument_id':inst.id},
                                 order_by='display_order'):
            instpvs[allpvs[row.pv_id]] = row.pv_id
        return instpvs

    def get_pvtypes_dict(self):
        self.pvtypes = {t.name: t.id for t in self.get_rows('pvtype')}

    def get_pvtypes(self, pvobj):
        """create tuple of choices for PV Type for database,
        which sets how to display PV entry.

        if pvobj is an epics.PV, the epics record type and
        pv.type are used to select the choices.

        if pvobj is an instrument.PV (ie, a db entry), the
        pvobj.pvtype.name field is used.
        """
        if isinstance(pvobj, epics.PV):
            prefix = pvobj.pvname
            suffix = None
            typename = pvobj.type
            if '.' in prefix:
                prefix, suffix = prefix.split('.')
            rectype = epics.caget(f"{prefix}.RTYP")
            if rectype == 'motor' and suffix in (None, 'VAL'):
                typename = 'motor'
            if pvobj.type == 'char' and pvobj.count > 1:
                typename = 'string'

        elif isinstance(pvobj, Row) and 'pvtype_id' in pvobj._fields:
            if len(self.pvtypes) < 1:
                self.get_pvtypes_dict()
            for key, val in self.pvtypes.items():
                if val == pvobj.pvtype_id:
                    typename = key

        choices = ('numeric', 'string')
        if typename == 'motor':
            choices = ('motor', 'numeric', 'string')
        elif typename in ('enum', 'time_enum'):
            choices = ('enum', 'numeric', 'string')
        elif typename in ('string', 'time_string'):
            choices = ('string', 'numeric')
        return choices

    def set_pvtype(self, name, pvtype):
        """ set a pv type"""
        pv = self.get_pv(name)
        if pv is None:
            print("PV not found ", name)
            return
        if len(self.pvtypes) < 1:
            self.get_pvtypes_dict()
        if pvtype not in self.pvtypes:
            self.add_row('pvtype', name=pvtype)
            self.get_pvtypes_dict()
        if pvtype in self.pvtypes:
            self.update('pv', where={name: pv.name},
                        pvtype_id=_types[pvtype])

    def get_allpvs(self):
        return {row.id: row.name for row in self.get_rows('pv')}

    def get_pv(self, name, form='time'):
        """return pv by name
        """
        norm_name = normalize_pvname(name)
        row = self.get_rows('pv', where={'name': norm_name},
                             limit_one=True, none_if_empty=True)
        if row is None and norm_name != name:
            # ensure that "normalized name" is used, fix if needed
            row = self.get_rows('pv', where={'name': name},
                                limit_one=True, none_if_empty=True)
            if row is not None:
                self.update('pv', where={'name': name}, name=norm_name)
        return row

    def rename_position(self, oldname, newname, instrument=None):
        """rename a position"""
        pos = self.get_position(oldname, instrument=instrument)
        if pos is not None:
            self.update('position', where={name: pos.name},
                        name=newname)

    def get_positions(self, instrument):
        """return list of positions for an instrument
        """
        inst = self.get_instrument(instrument)
        return self.get_rows('position', where={'instrument_id': inst.id})

    def get_position(self, name, instrument=None):
        """return position from name and instrument
        """
        where = {'name': name}
        if instrument is not None:
            where['instrument_id'] = self.get_instrument(instrument).id
        return self.get_rows('position', where=where,
                             limit_one=True, none_if_empty=True)

    def add_instrument(self, name, pvs=None, **kws):
        """add instrument  notes and attributes optional
        returns Instruments instance
        """
        kws['name'] = name.strip()
        inst = self.add_row('instrument', **kws)
        if pvs is not None:
            pvlist = []
            for pvname in pvs:
                thispv = self.get_pv(pvname)
                if thispv is None:
                    thispv = self.add_pv(pvname)
                pvlist.append(thispv.id)
            for i, pvid in enumerate(pvlist):
                self.add_row('instrument_pv', instrument_id=inst.id,
                             pv_id=pvid, display_order=i)

        self.commit()
        return inst

    def add_pv(self, name, pvtype=None, **kws):
        """add pv
        notes and attributes optional
        returns PV instance"""
        name = normalize_pvname(name)
        out = self.get_pv(name)
        if out is not None:
            return

        kws['notes'] = notes
        kws['attributes'] = attributes
        if pvtype is None:
            self.pvs[name] = epics.get_pv(name, form='time')
            self.pvs[name].get(timeout=1.0)
            pvtype = self.get_pvtypes(self.pvs[name])[0]
        row = self.add_row('pv', name=name, pvtype_id=pvtype_id)
        return row

    def remove_position(self, posname, instname):
        inst = self.get_instrument(instname)
        if inst is None:
            raise InstrumentDBException('Save Postion needs valid instrument')

        posname = posname.strip()
        pos  = self.get_position(posname, instname)
        if pos is None:
            raise InstrumentDBException("Postion '%s' not found for '%s'" %
                                        (posname, instname))

        self.delete_rows('position_pv', {'position_id': pos.id})
        self.delete_rows('position_pv', {'position_id': None})
        self.delete_rows('position', {'id': pos.id})

    def remove_instrument(self, instname):
        inst = self.get_instrument(instname)
        if inst is None:
            raise InstrumentDBException('Save Postion needs valid instrument')

        for pos in self.get_positions(instname):
            self.remove_position(pos.name, instname)

        self.delete_rows('position',      {'instrument_id': inst.id})
        self.delete_rows('instrument_pv', {'instrument_id': inst.id})
        self.delete_rows('instrument_pv', {'id': inst.id})

    def save_position(self, posname, instname, values, notes=None, **kws):
        """save position for instrument
        """
        inst = self.get_instrument(instname)
        if inst is None:
            raise InstrumentDBException('Save Postion needs valid instrument')

        posname = posname.strip()
        pos  = self.get_position(posname, instname)
        if pos is None:
            pos = self.add_row('position', name=posname,
                               instrument_id=inst.id, notes=notes,
                               modify_time=datetime.now())

        else:
            where = {'name': posname, 'instrument_id': inst.id}
            kwargs = {'modify_time': datetime.now}
            if notes is not None:
                kwargs['notes'] = notes
            pos = self.update('position', where=where, **kwargs)

        instpvs = self.get_instrument_pvs(instname)
        # check for missing pvs in values
        missing_pvs = []
        for pvname in instpvs:
            if pvname not in values:
                missing_pvs.append(pvname)

        if len(missing_pvs) > 0:
            raise InstrumentDBException(f'Save Postion: missing pvs:\n {missing_pvs}')

        for name, pvid in instpvs.items():
            self.insert('position_pv', position_id=pos.id,
                        pv_id=pvid, value=values['name'],
                        notes=f"'{inst.name}' / '{posname}'")


    def get_ordered_position(self, posname, instname, exclude_pvs=None):
        """
        return dict of {pvname: value} for position, ordered by display order
        """
        inst = self.get_instrument(instname)
        if inst is None:
            raise InstrumentDBException(
                'restore_postion needs valid instrument')

        posname = posname.strip()
        pos  = self.get_position(posname, instname)
        if pos is None:
            raise InstrumentDBException(
                f"restore_postion  position '{posname}' not found")

        if exclude_pvs is None:
            exclude_pvs = []

        allpvs = self.get_allpvs()
        pvvals = {}
        for row in self.get_rows('position_pv', where={'position_id': pos.id}):
            pvvals[row.pv_id] = row.value

        # ordered_pvs will hold ordered list of pv, vals in "move order"
        ordered_pvs = {}
        for row in self.get_rows('instrument_pv',
                                 where={'instrument_id': inst.id},
                                 order_by='display_order'):
            ordered_pvs[allpvs[row.pv_id]] = pvvals.get(row.pv_id, None)
        return ordered_pvs


    def restore_position(self, posname, instname, wait=True, timeout=5.0,
                         exclude_pvs=None):
        """
        restore named position for instrument
        """
        t0 = time.time()
        ordered_pvs = self.get_ordered_position(posname, instname,
                                                exclude_pvs=exclude_pvs)

        work_pvs = []
        for pvname, value in ordered_pvs.items():
            # put values without waiting
            thispv = self.pvs[pvname]
            if not thispv.connected():
                thispv.wait_for_connection(timeout=1.0)
            if thispv.connected():
                try:
                    thispv.put(val, wait=False, use_complete=True)
                except:
                    pass
                if wait:
                    work_pvs.append(thispv)

        complete = True
        if wait:
            complete = False
            while not all([p.put_complete for p in work_pvs]):
                time.sleep(0.025)
                if time.time() > (t0+timeout):
                    complete = True
                    break
        return complete
