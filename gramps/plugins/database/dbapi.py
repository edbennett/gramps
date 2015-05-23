#------------------------------------------------------------------------
#
# Python Modules
#
#------------------------------------------------------------------------
import pickle
import base64
import time
import re
import os
import logging
import shutil

#------------------------------------------------------------------------
#
# Gramps Modules
#
#------------------------------------------------------------------------
import gramps
from gramps.gen.const import GRAMPS_LOCALE as glocale
_ = glocale.translation.gettext
from gramps.gen.db import (DbReadBase, DbWriteBase, DbTxn, 
                           KEY_TO_NAME_MAP, KEY_TO_CLASS_MAP)
from gramps.gen.utils.callback import Callback
from gramps.gen.updatecallback import UpdateCallback
from gramps.gen.db.undoredo import DbUndo
from gramps.gen.db.dbconst import *
from gramps.gen.db import (PERSON_KEY,
                           FAMILY_KEY,
                           CITATION_KEY,
                           SOURCE_KEY,
                           EVENT_KEY,
                           MEDIA_KEY,
                           PLACE_KEY,
                           REPOSITORY_KEY,
                           NOTE_KEY,
                           TAG_KEY)

from gramps.gen.utils.id import create_id
from gramps.gen.lib.researcher import Researcher
from gramps.gen.lib import (Tag, MediaObject, Person, Family, Source, Citation, Event,
                            Place, Repository, Note, NameOriginType)
from gramps.gen.lib.genderstats import GenderStats

_LOG = logging.getLogger(DBLOGNAME)

def touch(fname, mode=0o666, dir_fd=None, **kwargs):
    ## After http://stackoverflow.com/questions/1158076/implement-touch-using-python
    flags = os.O_CREAT | os.O_APPEND
    with os.fdopen(os.open(fname, flags=flags, mode=mode, dir_fd=dir_fd)) as f:
        os.utime(f.fileno() if os.utime in os.supports_fd else fname,
                 dir_fd=None if os.supports_fd else dir_fd, **kwargs)

class Environment(object):
    """
    Implements the Environment API.
    """
    def __init__(self, db):
        self.db = db

    def txn_begin(self):
        return DBAPITxn("DBAPIDb Transaction", self.db)

class Table(object):
    """
    Implements Table interface.
    """
    def __init__(self, funcs):
        self.funcs = funcs

    def cursor(self):
        """
        Returns a Cursor for this Table.
        """
        return self.funcs["cursor_func"]()

    def put(self, key, data, txn=None):
        self.funcs["add_func"](data, txn)

class Map(object):
    """
    Implements the map API for person_map, etc.
    
    Takes a Table() as argument.
    """
    def __init__(self, table, 
                 keys_func="handles_func", 
                 contains_func="has_handle_func",
                 *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.table = table
        self.keys_func = keys_func
        self.contains_func = contains_func

    def keys(self):
        return self.table.funcs[self.keys_func]()

    def values(self):
        return self.table.funcs["cursor_func"]()

    def __contains__(self, key):
        return self.table.funcs[self.contains_func](key)

    def __getitem__(self, key):
        if self.table.funcs[self.contains_func](key):
            return self.table.funcs["raw_func"](key)

    def __len__(self):
        return self.table.funcs["count_func"]()

class MetaCursor(object):
    def __init__(self):
        pass
    def __enter__(self):
        return self
    def __iter__(self):
        return self.__next__()
    def __next__(self):
        yield None
    def __exit__(self, *args, **kwargs):
        pass
    def iter(self):
        yield None
    def first(self):
        self._iter = self.__iter__()
        return self.next()
    def next(self):
        try:
            return next(self._iter)
        except:
            return None
    def close(self):
        pass

class Cursor(object):
    def __init__(self, map):
        self.map = map
        self._iter = self.__iter__()
    def __enter__(self):
        return self
    def __iter__(self):
        for item in self.map.keys():
            yield (bytes(item, "utf-8"), self.map[item])
    def __next__(self):
        try:
            return self._iter.__next__()
        except StopIteration:
            return None
    def __exit__(self, *args, **kwargs):
        pass
    def iter(self):
        for item in self.map.keys():
            yield (bytes(item, "utf-8"), self.map[item])
    def first(self):
        self._iter = self.__iter__()
        try:
            return next(self._iter)
        except:
            return
    def next(self):
        try:
            return next(self._iter)
        except:
            return
    def close(self):
        pass

class Bookmarks(object):
    def __init__(self):
        self.handles = []
    def get(self):
        return self.handles
    def append(self, handle):
        self.handles.append(handle)

class DBAPITxn(DbTxn):
    def __init__(self, message, db, batch=False):
        DbTxn.__init__(self, message, db, batch)

    def get(self, key, default=None, txn=None, **kwargs):
        """
        Returns the data object associated with key
        """
        if txn and key in txn:
            return txn[key]
        else:
            return None

    def put(self, handle, new_data, txn):
        """
        """
        txn[handle] = new_data

class DBAPI(DbWriteBase, DbReadBase, UpdateCallback, Callback):
    """
    A Gramps Database Backend. This replicates the grampsdb functions.
    """
    __signals__ = dict((obj+'-'+op, signal)
                       for obj in
                       ['person', 'family', 'event', 'place',
                        'source', 'citation', 'media', 'note', 'repository', 'tag']
                       for op, signal in zip(
                               ['add',   'update', 'delete', 'rebuild'],
                               [(list,), (list,),  (list,),   None]
                       )
                   )
    
    # 2. Signals for long operations
    __signals__.update(('long-op-'+op, signal) for op, signal in zip(
        ['start',  'heartbeat', 'end'],
        [(object,), None,       None]
        ))

    # 3. Special signal for change in home person
    __signals__['home-person-changed'] = None

    # 4. Signal for change in person group name, parameters are
    __signals__['person-groupname-rebuild'] = (str, str)

    __callback_map = {}

    def __init__(self, directory=None):
        DbReadBase.__init__(self)
        DbWriteBase.__init__(self)
        Callback.__init__(self)
        self._tables['Person'].update(
            {
                "handle_func": self.get_person_from_handle, 
                "gramps_id_func": self.get_person_from_gramps_id,
                "class_func": Person,
                "cursor_func": self.get_person_cursor,
                "handles_func": self.get_person_handles,
                "add_func": self.add_person,
                "commit_func": self.commit_person,
                "iter_func": self.iter_people,
                "ids_func": self.get_person_gramps_ids,
                "has_handle_func": self.has_handle_for_person,
                "has_gramps_id_func": self.has_gramps_id_for_person,
                "count": self.get_number_of_people,
                "raw_func": self._get_raw_person_data,
            })
        self._tables['Family'].update(
            {
                "handle_func": self.get_family_from_handle, 
                "gramps_id_func": self.get_family_from_gramps_id,
                "class_func": Family,
                "cursor_func": self.get_family_cursor,
                "handles_func": self.get_family_handles,
                "add_func": self.add_family,
                "commit_func": self.commit_family,
                "iter_func": self.iter_families,
                "ids_func": self.get_family_gramps_ids,
                "has_handle_func": self.has_handle_for_family,
                "has_gramps_id_func": self.has_gramps_id_for_family,
                "count": self.get_number_of_families,
                "raw_func": self._get_raw_family_data,
            })
        self._tables['Source'].update(
            {
                "handle_func": self.get_source_from_handle, 
                "gramps_id_func": self.get_source_from_gramps_id,
                "class_func": Source,
                "cursor_func": self.get_source_cursor,
                "handles_func": self.get_source_handles,
                "add_func": self.add_source,
                "commit_func": self.commit_source,
                "iter_func": self.iter_sources,
                "ids_func": self.get_source_gramps_ids,
                "has_handle_func": self.has_handle_for_source,
                "has_gramps_id_func": self.has_gramps_id_for_source,
                "count": self.get_number_of_sources,
                "raw_func": self._get_raw_source_data,
                })
        self._tables['Citation'].update(
            {
                "handle_func": self.get_citation_from_handle, 
                "gramps_id_func": self.get_citation_from_gramps_id,
                "class_func": Citation,
                "cursor_func": self.get_citation_cursor,
                "handles_func": self.get_citation_handles,
                "add_func": self.add_citation,
                "commit_func": self.commit_citation,
                "iter_func": self.iter_citations,
                "ids_func": self.get_citation_gramps_ids,
                "has_handle_func": self.has_handle_for_citation,
                "has_gramps_id_func": self.has_gramps_id_for_citation,
                "count": self.get_number_of_citations,
                "raw_func": self._get_raw_citation_data,
            })
        self._tables['Event'].update(
            {
                "handle_func": self.get_event_from_handle, 
                "gramps_id_func": self.get_event_from_gramps_id,
                "class_func": Event,
                "cursor_func": self.get_event_cursor,
                "handles_func": self.get_event_handles,
                "add_func": self.add_event,
                "commit_func": self.commit_event,
                "iter_func": self.iter_events,
                "ids_func": self.get_event_gramps_ids,
                "has_handle_func": self.has_handle_for_event,
                "has_gramps_id_func": self.has_gramps_id_for_event,
                "count": self.get_number_of_events,
                "raw_func": self._get_raw_event_data,
            })
        self._tables['Media'].update(
            {
                "handle_func": self.get_object_from_handle, 
                "gramps_id_func": self.get_object_from_gramps_id,
                "class_func": MediaObject,
                "cursor_func": self.get_media_cursor,
                "handles_func": self.get_media_object_handles,
                "add_func": self.add_object,
                "commit_func": self.commit_media_object,
                "iter_func": self.iter_media_objects,
                "ids_func": self.get_media_gramps_ids,
                "has_handle_func": self.has_handle_for_media,
                "has_gramps_id_func": self.has_gramps_id_for_media,
                "count": self.get_number_of_media_objects,
                "raw_func": self._get_raw_media_data,
            })
        self._tables['Place'].update(
            {
                "handle_func": self.get_place_from_handle, 
                "gramps_id_func": self.get_place_from_gramps_id,
                "class_func": Place,
                "cursor_func": self.get_place_cursor,
                "handles_func": self.get_place_handles,
                "add_func": self.add_place,
                "commit_func": self.commit_place,
                "iter_func": self.iter_places,
                "ids_func": self.get_place_gramps_ids,
                "has_handle_func": self.has_handle_for_place,
                "has_gramps_id_func": self.has_gramps_id_for_place,
                "count": self.get_number_of_places,
                "raw_func": self._get_raw_place_data,
            })
        self._tables['Repository'].update(
            {
                "handle_func": self.get_repository_from_handle, 
                "gramps_id_func": self.get_repository_from_gramps_id,
                "class_func": Repository,
                "cursor_func": self.get_repository_cursor,
                "handles_func": self.get_repository_handles,
                "add_func": self.add_repository,
                "commit_func": self.commit_repository,
                "iter_func": self.iter_repositories,
                "ids_func": self.get_repository_gramps_ids,
                "has_handle_func": self.has_handle_for_repository,
                "has_gramps_id_func": self.has_gramps_id_for_repository,
                "count": self.get_number_of_repositories,
                "raw_func": self._get_raw_repository_data,
            })
        self._tables['Note'].update(
            {
                "handle_func": self.get_note_from_handle, 
                "gramps_id_func": self.get_note_from_gramps_id,
                "class_func": Note,
                "cursor_func": self.get_note_cursor,
                "handles_func": self.get_note_handles,
                "add_func": self.add_note,
                "commit_func": self.commit_note,
                "iter_func": self.iter_notes,
                "ids_func": self.get_note_gramps_ids,
                "has_handle_func": self.has_handle_for_note,
                "has_gramps_id_func": self.has_gramps_id_for_note,
                "count": self.get_number_of_notes,
                "raw_func": self._get_raw_note_data,
            })
        self._tables['Tag'].update(
            {
                "handle_func": self.get_tag_from_handle, 
                "gramps_id_func": None,
                "class_func": Tag,
                "cursor_func": self.get_tag_cursor,
                "handles_func": self.get_tag_handles,
                "add_func": self.add_tag,
                "commit_func": self.commit_tag,
                "iter_func": self.iter_tags,
                "count": self.get_number_of_tags,
            })
        # skip GEDCOM cross-ref check for now:
        self.set_feature("skip-check-xref", True)
        self.set_feature("skip-import-additions", True)
        self.readonly = False
        self.db_is_open = True
        self.name_formats = []
        self.bookmarks = Bookmarks()
        self.family_bookmarks = Bookmarks()
        self.event_bookmarks = Bookmarks()
        self.place_bookmarks = Bookmarks()
        self.citation_bookmarks = Bookmarks()
        self.source_bookmarks = Bookmarks()
        self.repo_bookmarks = Bookmarks()
        self.media_bookmarks = Bookmarks()
        self.note_bookmarks = Bookmarks()
        self.set_person_id_prefix('I%04d')
        self.set_object_id_prefix('O%04d')
        self.set_family_id_prefix('F%04d')
        self.set_citation_id_prefix('C%04d')
        self.set_source_id_prefix('S%04d')
        self.set_place_id_prefix('P%04d')
        self.set_event_id_prefix('E%04d')
        self.set_repository_id_prefix('R%04d')
        self.set_note_id_prefix('N%04d')
        # ----------------------------------
        self.id_trans  = DBAPITxn("ID Transaction", self)
        self.fid_trans = DBAPITxn("FID Transaction", self)
        self.pid_trans = DBAPITxn("PID Transaction", self)
        self.cid_trans = DBAPITxn("CID Transaction", self)
        self.sid_trans = DBAPITxn("SID Transaction", self)
        self.oid_trans = DBAPITxn("OID Transaction", self)
        self.rid_trans = DBAPITxn("RID Transaction", self)
        self.nid_trans = DBAPITxn("NID Transaction", self)
        self.eid_trans = DBAPITxn("EID Transaction", self)
        self.cmap_index = 0
        self.smap_index = 0
        self.emap_index = 0
        self.pmap_index = 0
        self.fmap_index = 0
        self.lmap_index = 0
        self.omap_index = 0
        self.rmap_index = 0
        self.nmap_index = 0
        self.env = Environment(self)
        self.person_map = Map(Table(self._tables["Person"]))
        self.person_id_map = Map(Table(self._tables["Person"]),
                                 keys_func="ids_func",
                                 contains_func="has_gramps_id_func")
        self.family_map = Map(Table(self._tables["Family"]))
        self.family_id_map = Map(Table(self._tables["Family"]),
                                 keys_func="ids_func",
                                 contains_func="has_gramps_id_func")
        self.place_map  = Map(Table(self._tables["Place"]))
        self.place_id_map = Map(Table(self._tables["Place"]),
                                 keys_func="ids_func",
                                 contains_func="has_gramps_id_func")
        self.citation_map = Map(Table(self._tables["Citation"]))
        self.citation_id_map = Map(Table(self._tables["Citation"]),
                                 keys_func="ids_func",
                                 contains_func="has_gramps_id_func")
        self.source_map = Map(Table(self._tables["Source"]))
        self.source_id_map = Map(Table(self._tables["Source"]),
                                 keys_func="ids_func",
                                 contains_func="has_gramps_id_func")
        self.repository_map  = Map(Table(self._tables["Repository"]))
        self.repository_id_map = Map(Table(self._tables["Repository"]),
                                 keys_func="ids_func",
                                 contains_func="has_gramps_id_func")
        self.note_map = Map(Table(self._tables["Note"]))
        self.note_id_map = Map(Table(self._tables["Note"]),
                                 keys_func="ids_func",
                                 contains_func="has_gramps_id_func")
        self.media_map  = Map(Table(self._tables["Media"]))
        self.media_id_map = Map(Table(self._tables["Media"]),
                                 keys_func="ids_func",
                                 contains_func="has_gramps_id_func")
        self.event_map  = Map(Table(self._tables["Event"]))
        self.event_id_map = Map(Table(self._tables["Event"]), 
                                 keys_func="ids_func",
                                 contains_func="has_gramps_id_func")
        self.tag_map  = Map(Table(self._tables["Tag"]))
        self.metadata   = Map(Table({"cursor_func": lambda: MetaCursor()}))
        self.name_group = {}
        self.undo_callback = None
        self.redo_callback = None
        self.undo_history_callback = None
        self.modified   = 0
        self.txn = DBAPITxn("DBAPI Transaction", self)
        self.transaction = None
        self.undodb = DbUndo(self)
        self.abort_possible = False
        self._bm_changes = 0
        self._directory = directory
        self.full_name = None
        self.path = None
        self.brief_name = None
        self.genderStats = GenderStats() # can pass in loaded stats as dict
        self.owner = Researcher()
        if directory:
            self.load(directory)

    def version_supported(self):
        """Return True when the file has a supported version."""
        return True

    def get_table_names(self):
        """Return a list of valid table names."""
        return list(self._tables.keys())

    def get_table_metadata(self, table_name):
        """Return the metadata for a valid table name."""
        if table_name in self._tables:
            return self._tables[table_name]
        return None

    def transaction_commit(self, txn):
        self.dbapi.commit()

    def get_undodb(self):
        ## FIXME
        return None

    def transaction_abort(self, txn):
        self.dbapi.rollback()

    @staticmethod
    def _validated_id_prefix(val, default):
        if isinstance(val, str) and val:
            try:
                str_ = val % 1
            except TypeError:           # missing conversion specifier
                prefix_var = val + "%d"
            except ValueError:          # incomplete format
                prefix_var = default+"%04d"
            else:
                prefix_var = val        # OK as given
        else:
            prefix_var = default+"%04d" # not a string or empty string
        return prefix_var

    @staticmethod
    def __id2user_format(id_pattern):
        """
        Return a method that accepts a Gramps ID and adjusts it to the users
        format.
        """
        pattern_match = re.match(r"(.*)%[0 ](\d+)[diu]$", id_pattern)
        if pattern_match:
            str_prefix = pattern_match.group(1)
            nr_width = int(pattern_match.group(2))
            def closure_func(gramps_id):
                if gramps_id and gramps_id.startswith(str_prefix):
                    id_number = gramps_id[len(str_prefix):]
                    if id_number.isdigit():
                        id_value = int(id_number, 10)
                        #if len(str(id_value)) > nr_width:
                        #    # The ID to be imported is too large to fit in the
                        #    # users format. For now just create a new ID,
                        #    # because that is also what happens with IDs that
                        #    # are identical to IDs already in the database. If
                        #    # the problem of colliding import and already
                        #    # present IDs is solved the code here also needs
                        #    # some solution.
                        #    gramps_id = id_pattern % 1
                        #else:
                        gramps_id = id_pattern % id_value
                return gramps_id
        else:
            def closure_func(gramps_id):
                return gramps_id
        return closure_func

    def set_person_id_prefix(self, val):
        """
        Set the naming template for GRAMPS Person ID values. 
        
        The string is expected to be in the form of a simple text string, or 
        in a format that contains a C/Python style format string using %d, 
        such as I%d or I%04d.
        """
        self.person_prefix = self._validated_id_prefix(val, "I")
        self.id2user_format = self.__id2user_format(self.person_prefix)

    def set_citation_id_prefix(self, val):
        """
        Set the naming template for GRAMPS Citation ID values. 
        
        The string is expected to be in the form of a simple text string, or 
        in a format that contains a C/Python style format string using %d, 
        such as C%d or C%04d.
        """
        self.citation_prefix = self._validated_id_prefix(val, "C")
        self.cid2user_format = self.__id2user_format(self.citation_prefix)
            
    def set_source_id_prefix(self, val):
        """
        Set the naming template for GRAMPS Source ID values. 
        
        The string is expected to be in the form of a simple text string, or 
        in a format that contains a C/Python style format string using %d, 
        such as S%d or S%04d.
        """
        self.source_prefix = self._validated_id_prefix(val, "S")
        self.sid2user_format = self.__id2user_format(self.source_prefix)
            
    def set_object_id_prefix(self, val):
        """
        Set the naming template for GRAMPS MediaObject ID values. 
        
        The string is expected to be in the form of a simple text string, or 
        in a format that contains a C/Python style format string using %d, 
        such as O%d or O%04d.
        """
        self.mediaobject_prefix = self._validated_id_prefix(val, "O")
        self.oid2user_format = self.__id2user_format(self.mediaobject_prefix)

    def set_place_id_prefix(self, val):
        """
        Set the naming template for GRAMPS Place ID values. 
        
        The string is expected to be in the form of a simple text string, or 
        in a format that contains a C/Python style format string using %d, 
        such as P%d or P%04d.
        """
        self.place_prefix = self._validated_id_prefix(val, "P")
        self.pid2user_format = self.__id2user_format(self.place_prefix)

    def set_family_id_prefix(self, val):
        """
        Set the naming template for GRAMPS Family ID values. The string is
        expected to be in the form of a simple text string, or in a format
        that contains a C/Python style format string using %d, such as F%d
        or F%04d.
        """
        self.family_prefix = self._validated_id_prefix(val, "F")
        self.fid2user_format = self.__id2user_format(self.family_prefix)

    def set_event_id_prefix(self, val):
        """
        Set the naming template for GRAMPS Event ID values. 
        
        The string is expected to be in the form of a simple text string, or 
        in a format that contains a C/Python style format string using %d, 
        such as E%d or E%04d.
        """
        self.event_prefix = self._validated_id_prefix(val, "E")
        self.eid2user_format = self.__id2user_format(self.event_prefix)

    def set_repository_id_prefix(self, val):
        """
        Set the naming template for GRAMPS Repository ID values. 
        
        The string is expected to be in the form of a simple text string, or 
        in a format that contains a C/Python style format string using %d, 
        such as R%d or R%04d.
        """
        self.repository_prefix = self._validated_id_prefix(val, "R")
        self.rid2user_format = self.__id2user_format(self.repository_prefix)

    def set_note_id_prefix(self, val):
        """
        Set the naming template for GRAMPS Note ID values. 
        
        The string is expected to be in the form of a simple text string, or 
        in a format that contains a C/Python style format string using %d, 
        such as N%d or N%04d.
        """
        self.note_prefix = self._validated_id_prefix(val, "N")
        self.nid2user_format = self.__id2user_format(self.note_prefix)

    def __find_next_gramps_id(self, prefix, map_index, trans):
        """
        Helper function for find_next_<object>_gramps_id methods
        """
        index = prefix % map_index
        while trans.get(str(index), txn=self.txn) is not None:
            map_index += 1
            index = prefix % map_index
        map_index += 1
        return (map_index, index)
        
    def find_next_person_gramps_id(self):
        """
        Return the next available GRAMPS' ID for a Person object based off the 
        person ID prefix.
        """
        self.pmap_index, gid = self.__find_next_gramps_id(self.person_prefix,
                                          self.pmap_index, self.id_trans)
        return gid

    def find_next_place_gramps_id(self):
        """
        Return the next available GRAMPS' ID for a Place object based off the 
        place ID prefix.
        """
        self.lmap_index, gid = self.__find_next_gramps_id(self.place_prefix,
                                          self.lmap_index, self.pid_trans)
        return gid

    def find_next_event_gramps_id(self):
        """
        Return the next available GRAMPS' ID for a Event object based off the 
        event ID prefix.
        """
        self.emap_index, gid = self.__find_next_gramps_id(self.event_prefix,
                                          self.emap_index, self.eid_trans)
        return gid

    def find_next_object_gramps_id(self):
        """
        Return the next available GRAMPS' ID for a MediaObject object based
        off the media object ID prefix.
        """
        self.omap_index, gid = self.__find_next_gramps_id(self.mediaobject_prefix,
                                          self.omap_index, self.oid_trans)
        return gid

    def find_next_citation_gramps_id(self):
        """
        Return the next available GRAMPS' ID for a Citation object based off the 
        citation ID prefix.
        """
        self.cmap_index, gid = self.__find_next_gramps_id(self.citation_prefix,
                                          self.cmap_index, self.cid_trans)
        return gid

    def find_next_source_gramps_id(self):
        """
        Return the next available GRAMPS' ID for a Source object based off the 
        source ID prefix.
        """
        self.smap_index, gid = self.__find_next_gramps_id(self.source_prefix,
                                          self.smap_index, self.sid_trans)
        return gid

    def find_next_family_gramps_id(self):
        """
        Return the next available GRAMPS' ID for a Family object based off the 
        family ID prefix.
        """
        self.fmap_index, gid = self.__find_next_gramps_id(self.family_prefix,
                                          self.fmap_index, self.fid_trans)
        return gid

    def find_next_repository_gramps_id(self):
        """
        Return the next available GRAMPS' ID for a Respository object based 
        off the repository ID prefix.
        """
        self.rmap_index, gid = self.__find_next_gramps_id(self.repository_prefix,
                                          self.rmap_index, self.rid_trans)
        return gid

    def find_next_note_gramps_id(self):
        """
        Return the next available GRAMPS' ID for a Note object based off the 
        note ID prefix.
        """
        self.nmap_index, gid = self.__find_next_gramps_id(self.note_prefix,
                                          self.nmap_index, self.nid_trans)
        return gid

    def get_mediapath(self):
        return None

    def get_name_group_keys(self):
        return []

    def get_name_group_mapping(self, key):
        return None

    def get_person_handles(self, sort_handles=False):
        if sort_handles:
            cur = self.dbapi.execute("SELECT handle FROM person ORDER BY order_by;")
        else:
            cur = self.dbapi.execute("SELECT handle FROM person;")
        rows = cur.fetchall()
        return [row[0] for row in rows]

    def get_family_handles(self):
        cur = self.dbapi.execute("select handle from family;")
        rows = cur.fetchall()
        return [row[0] for row in rows]

    def get_event_handles(self):
        cur = self.dbapi.execute("select handle from event;")
        rows = cur.fetchall()
        return [row[0] for row in rows]

    def get_citation_handles(self, sort_handles=False):
        if sort_handles:
            cur = self.dbapi.execute("select handle from citation ORDER BY order_by;")
        else:
            cur = self.dbapi.execute("select handle from citation;")
        rows = cur.fetchall()
        return [row[0] for row in rows]

    def get_source_handles(self, sort_handles=False):
        if sort_handles:
            cur = self.dbapi.execute("select handle from source ORDER BY order_by;")
        else:
            cur = self.dbapi.execute("select handle from source;")
        rows = cur.fetchall()
        return [row[0] for row in rows]

    def get_place_handles(self, sort_handles=False):
        if sort_handles:
            cur = self.dbapi.execute("select handle from place ORDER BY order_by;")
        else:
            cur = self.dbapi.execute("select handle from place;")
        rows = cur.fetchall()
        return [row[0] for row in rows]

    def get_repository_handles(self):
        cur = self.dbapi.execute("select handle from repository;")
        rows = cur.fetchall()
        return [row[0] for row in rows]

    def get_media_object_handles(self, sort_handles=False):
        if sort_handles:
            cur = self.dbapi.execute("select handle from media ORDER BY order_by;")
        else:
            cur = self.dbapi.execute("select handle from media;")
        rows = cur.fetchall()
        return [row[0] for row in rows]

    def get_note_handles(self):
        cur = self.dbapi.execute("select handle from note;")
        rows = cur.fetchall()
        return [row[0] for row in rows]

    def get_tag_handles(self, sort_handles=False):
        if sort_handles:
            cur = self.dbapi.execute("select handle from tag ORDER BY order_by;")
        else:
            cur = self.dbapi.execute("select handle from tag;")
        rows = cur.fetchall()
        return [row[0] for row in rows]

    def get_event_from_handle(self, handle):
        if isinstance(handle, bytes):
            handle = str(handle, "utf-8")
        event = None
        if handle in self.event_map:
            event = Event.create(self._get_raw_event_data(handle))
        return event

    def get_family_from_handle(self, handle): 
        if isinstance(handle, bytes):
            handle = str(handle, "utf-8")
        family = None
        if handle in self.family_map:
            family = Family.create(self._get_raw_family_data(handle))
        return family

    def get_repository_from_handle(self, handle):
        if isinstance(handle, bytes):
            handle = str(handle, "utf-8")
        repository = None
        if handle in self.repository_map:
            repository = Repository.create(self._get_raw_repository_data(handle))
        return repository

    def get_person_from_handle(self, handle):
        if isinstance(handle, bytes):
            handle = str(handle, "utf-8")
        person = None
        if handle in self.person_map:
            person = Person.create(self._get_raw_person_data(handle))
        return person

    def get_place_from_handle(self, handle):
        if isinstance(handle, bytes):
            handle = str(handle, "utf-8")
        place = None
        if handle in self.place_map:
            place = Place.create(self._get_raw_place_data(handle))
        return place

    def get_citation_from_handle(self, handle):
        if isinstance(handle, bytes):
            handle = str(handle, "utf-8")
        citation = None
        if handle in self.citation_map:
            citation = Citation.create(self._get_raw_citation_data(handle))
        return citation

    def get_source_from_handle(self, handle):
        if isinstance(handle, bytes):
            handle = str(handle, "utf-8")
        source = None
        if handle in self.source_map:
            source = Source.create(self._get_raw_source_data(handle))
        return source

    def get_note_from_handle(self, handle):
        if isinstance(handle, bytes):
            handle = str(handle, "utf-8")
        note = None
        if handle in self.note_map:
            note = Note.create(self._get_raw_note_data(handle))
        return note

    def get_object_from_handle(self, handle):
        if isinstance(handle, bytes):
            handle = str(handle, "utf-8")
        media = None
        if handle in self.media_map:
            media = MediaObject.create(self._get_raw_media_data(handle))
        return media

    def get_tag_from_handle(self, handle):
        if isinstance(handle, bytes):
            handle = str(handle, "utf-8")
        tag = None
        if handle in self.tag_map:
            tag = Tag.create(self._get_raw_tag_data(handle))
        return tag

    def get_default_person(self):
        handle = self.get_default_handle()
        if handle:
            return self.get_person_from_handle(handle)
        else:
            return None

    def iter_people(self):
        return (Person.create(data[1]) for data in self.get_person_cursor())

    def iter_person_handles(self):
        return (data[0] for data in self.get_person_cursor())

    def iter_families(self):
        return (Family.create(data[1]) for data in self.get_family_cursor())

    def iter_family_handles(self):
        return (handle for handle in self.family_map.keys())

    def get_tag_from_name(self, name):
        ## Slow, but typically not too many tags:
        for data in self.tag_map.values():
            tag = Tag.create(data)
            if tag.name == name:
                return tag
        return None

    def get_person_from_gramps_id(self, gramps_id):
        if gramps_id in self.person_id_map:
            return Person.create(self.person_id_map[gramps_id])
        return None

    def get_family_from_gramps_id(self, gramps_id):
        if gramps_id in self.family_id_map:
            return Family.create(self.family_id_map[gramps_id])
        return None

    def get_citation_from_gramps_id(self, gramps_id):
        if gramps_id in self.citation_id_map:
            return Citation.create(self.citation_id_map[gramps_id])
        return None

    def get_source_from_gramps_id(self, gramps_id):
        if gramps_id in self.source_id_map:
            return Source.create(self.source_id_map[gramps_id])
        return None

    def get_event_from_gramps_id(self, gramps_id):
        if gramps_id in self.event_id_map:
            return Event.create(self.event_id_map[gramps_id])
        return None

    def get_media_from_gramps_id(self, gramps_id):
        if gramps_id in self.media_id_map:
            return MediaObject.create(self.media_id_map[gramps_id])
        return None

    def get_place_from_gramps_id(self, gramps_id):
        if gramps_id in self.place_id_map:
            return Place.create(self.place_id_map[gramps_id])
        return None

    def get_repository_from_gramps_id(self, gramps_id):
        if gramps_id in self.repository_id_map:
            return Repository.create(self.repository_id_map[gramps_id])
        return None

    def get_note_from_gramps_id(self, gramps_id):
        if gramps_id in self.note_id_map:
            return Note.create(self.note_id_map[gramps_id])
        return None

    def get_number_of_people(self):
        cur = self.dbapi.execute("select count(handle) from person;")
        row = cur.fetchone()
        return row[0]

    def get_number_of_events(self):
        cur = self.dbapi.execute("select count(handle) from event;")
        row = cur.fetchone()
        return row[0]

    def get_number_of_places(self):
        cur = self.dbapi.execute("select count(handle) from place;")
        row = cur.fetchone()
        return row[0]

    def get_number_of_tags(self):
        cur = self.dbapi.execute("select count(handle) from tag;")
        row = cur.fetchone()
        return row[0]

    def get_number_of_families(self):
        cur = self.dbapi.execute("select count(handle) from family;")
        row = cur.fetchone()
        return row[0]

    def get_number_of_notes(self):
        cur = self.dbapi.execute("select count(handle) from note;")
        row = cur.fetchone()
        return row[0]

    def get_number_of_citations(self):
        cur = self.dbapi.execute("select count(handle) from citation;")
        row = cur.fetchone()
        return row[0]

    def get_number_of_sources(self):
        cur = self.dbapi.execute("select count(handle) from source;")
        row = cur.fetchone()
        return row[0]

    def get_number_of_media_objects(self):
        cur = self.dbapi.execute("select count(handle) from media;")
        row = cur.fetchone()
        return row[0]

    def get_number_of_repositories(self):
        cur = self.dbapi.execute("select count(handle) from repository;")
        row = cur.fetchone()
        return row[0]

    def get_place_cursor(self):
        return Cursor(self.place_map)

    def get_person_cursor(self):
        return Cursor(self.person_map)

    def get_family_cursor(self):
        return Cursor(self.family_map)

    def get_event_cursor(self):
        return Cursor(self.event_map)

    def get_note_cursor(self):
        return Cursor(self.note_map)

    def get_tag_cursor(self):
        return Cursor(self.tag_map)

    def get_repository_cursor(self):
        return Cursor(self.repository_map)

    def get_media_cursor(self):
        return Cursor(self.media_map)

    def get_citation_cursor(self):
        return Cursor(self.citation_map)

    def get_source_cursor(self):
        return Cursor(self.source_map)

    def has_gramps_id(self, obj_key, gramps_id):
        key2table = {
            PERSON_KEY:     self.person_id_map, 
            FAMILY_KEY:     self.family_id_map, 
            SOURCE_KEY:     self.source_id_map, 
            CITATION_KEY:   self.citation_id_map, 
            EVENT_KEY:      self.event_id_map, 
            MEDIA_KEY:      self.media_id_map, 
            PLACE_KEY:      self.place_id_map, 
            REPOSITORY_KEY: self.repository_id_map, 
            NOTE_KEY:       self.note_id_map, 
            }
        return gramps_id in key2table[obj_key]

    def has_person_handle(self, handle):
        return handle in self.person_map

    def has_family_handle(self, handle):
        return handle in self.family_map

    def has_citation_handle(self, handle):
        return handle in self.citation_map

    def has_source_handle(self, handle):
        return handle in self.source_map

    def has_repository_handle(self, handle):
        return handle in self.repository_map

    def has_note_handle(self, handle):
        return handle in self.note_map

    def has_place_handle(self, handle):
        return handle in self.place_map

    def has_event_handle(self, handle):
        return handle in self.event_map

    def has_tag_handle(self, handle):
        return handle in self.tag_map

    def has_object_handle(self, handle):
        return handle in self.media_map

    def has_name_group_key(self, key):
        # FIXME:
        return False

    def set_name_group_mapping(self, key, value):
        # FIXME:
        pass

    def set_default_person_handle(self, handle):
        ## FIXME
        pass

    def set_mediapath(self, mediapath):
        ## FIXME
        pass

    def get_raw_person_data(self, handle):
        if handle in self.person_map:
            return self.person_map[handle]
        return None

    def get_raw_family_data(self, handle):
        if handle in self.family_map:
            return self.family_map[handle]
        return None

    def get_raw_citation_data(self, handle):
        if handle in self.citation_map:
            return self.citation_map[handle]
        return None

    def get_raw_source_data(self, handle):
        if handle in self.source_map:
            return self.source_map[handle]
        return None

    def get_raw_repository_data(self, handle):
        if handle in self.repository_map:
            return self.repository_map[handle]
        return None

    def get_raw_note_data(self, handle):
        if handle in self.note_map:
            return self.note_map[handle]
        return None

    def get_raw_place_data(self, handle):
        if handle in self.place_map:
            return self.place_map[handle]
        return None

    def get_raw_object_data(self, handle):
        if handle in self.media_map:
            return self.media_map[handle]
        return None

    def get_raw_tag_data(self, handle):
        if handle in self.tag_map:
            return self.tag_map[handle]
        return None

    def get_raw_event_data(self, handle):
        if handle in self.event_map:
            return self.event_map[handle]
        return None

    def add_person(self, person, trans, set_gid=True):
        if not person.handle:
            person.handle = create_id()
        if not person.gramps_id and set_gid:
            person.gramps_id = self.find_next_person_gramps_id()
        self.commit_person(person, trans)
        return person.handle

    def add_family(self, family, trans, set_gid=True):
        if not family.handle:
            family.handle = create_id()
        if not family.gramps_id and set_gid:
            family.gramps_id = self.find_next_family_gramps_id()
        self.commit_family(family, trans)
        return family.handle

    def add_citation(self, citation, trans, set_gid=True):
        if not citation.handle:
            citation.handle = create_id()
        if not citation.gramps_id and set_gid:
            citation.gramps_id = self.find_next_citation_gramps_id()
        self.commit_citation(citation, trans)
        return citation.handle

    def add_source(self, source, trans, set_gid=True):
        if not source.handle:
            source.handle = create_id()
        if not source.gramps_id and set_gid:
            source.gramps_id = self.find_next_source_gramps_id()
        self.commit_source(source, trans)
        return source.handle

    def add_repository(self, repository, trans, set_gid=True):
        if not repository.handle:
            repository.handle = create_id()
        if not repository.gramps_id and set_gid:
            repository.gramps_id = self.find_next_repository_gramps_id()
        self.commit_repository(repository, trans)
        return repository.handle

    def add_note(self, note, trans, set_gid=True):
        if not note.handle:
            note.handle = create_id()
        if not note.gramps_id and set_gid:
            note.gramps_id = self.find_next_note_gramps_id()
        self.commit_note(note, trans)
        return note.handle

    def add_place(self, place, trans, set_gid=True):
        if not place.handle:
            place.handle = create_id()
        if not place.gramps_id and set_gid:
            place.gramps_id = self.find_next_place_gramps_id()
        self.commit_place(place, trans)
        return place.handle

    def add_event(self, event, trans, set_gid=True):
        if not event.handle:
            event.handle = create_id()
        if not event.gramps_id and set_gid:
            event.gramps_id = self.find_next_event_gramps_id()
        self.commit_event(event, trans)
        return event.handle

    def add_tag(self, tag, trans):
        if not tag.handle:
            tag.handle = create_id()
        self.commit_tag(tag, trans)
        return tag.handle

    def add_object(self, obj, transaction, set_gid=True):
        """
        Add a MediaObject to the database, assigning internal IDs if they have
        not already been defined.
        
        If not set_gid, then gramps_id is not set.
        """
        if not obj.handle:
            obj.handle = create_id()
        if not obj.gramps_id and set_gid:
            obj.gramps_id = self.find_next_object_gramps_id()
        self.commit_media_object(obj, transaction)
        return obj.handle

    def commit_person(self, person, trans, change_time=None):
        emit = None
        if person.handle in self.person_map:
            emit = "person-update"
            self.dbapi.execute("""UPDATE person SET gramps_id = ?, 
                                                    order_by = ?,
                                                    blob = ? 
                                                WHERE handle = ?;""",
                               [person.gramps_id, 
                                self._order_by_person_key(person),
                                pickle.dumps(person.serialize()),
                                person.handle])
        else:
            emit = "person-add"
            self.dbapi.execute("""insert into person(handle, order_by, gramps_id, blob) 
                                              values(?, ?, ?, ?);""", 
                               [person.handle, 
                                self._order_by_person_key(person),
                                person.gramps_id, 
                                pickle.dumps(person.serialize())])
        if not trans.batch:
            self.dbapi.commit()
        # Emit after added:
        if emit:
            self.emit(emit, ([person.handle],))

    def commit_family(self, family, trans, change_time=None):
        emit = None
        if family.handle in self.family_map:
            emit = "family-update"
            self.dbapi.execute("""UPDATE family SET gramps_id = ?, 
                                                    blob = ? 
                                                WHERE handle = ?;""",
                               [family.gramps_id, 
                                pickle.dumps(family.serialize()),
                                family.handle])
        else:
            emit = "family-add"
            self.dbapi.execute("insert into family values(?, ?, ?);", 
                               [family.handle, family.gramps_id, 
                                pickle.dumps(family.serialize())])
        if not trans.batch:
            self.dbapi.commit()
        # Emit after added:
        if emit:
            self.emit(emit, ([family.handle],))

    def commit_citation(self, citation, trans, change_time=None):
        emit = None
        if citation.handle in self.citation_map:
            emit = "citation-update"
            self.dbapi.execute("""UPDATE citation SET gramps_id = ?, 
                                                      order_by = ?,
                                                      blob = ? 
                                                WHERE handle = ?;""",
                               [citation.gramps_id, 
                                self._order_by_citation_key(citation),
                                pickle.dumps(citation.serialize()),
                                citation.handle])
        else:
            emit = "citation-add"
            self.dbapi.execute("insert into citation values(?, ?, ?, ?);", 
                       [citation.handle, 
                        self._order_by_citation_key(citation),
                        citation.gramps_id, 
                        pickle.dumps(citation.serialize())])
        if not trans.batch:
            self.dbapi.commit()
        # Emit after added:
        if emit:
            self.emit(emit, ([citation.handle],))

    def commit_source(self, source, trans, change_time=None):
        emit = None
        if source.handle in self.source_map:
            emit = "source-update"
            self.dbapi.execute("""UPDATE source SET gramps_id = ?, 
                                                    order_by = ?,
                                                    blob = ? 
                                                WHERE handle = ?;""",
                               [source.gramps_id, 
                                self._order_by_source_key(source),
                                pickle.dumps(source.serialize()),
                                source.handle])
        else:
            emit = "source-add"
            self.dbapi.execute("insert into source values(?, ?, ?, ?);", 
                       [source.handle, 
                        self._order_by_source_key(source),
                        source.gramps_id, 
                        pickle.dumps(source.serialize())])
        if not trans.batch:
            self.dbapi.commit()
        # Emit after added:
        if emit:
            self.emit(emit, ([source.handle],))

    def commit_repository(self, repository, trans, change_time=None):
        emit = None
        if repository.handle in self.repository_map:
            emit = "repository-update"
            self.dbapi.execute("""UPDATE repository SET gramps_id = ?, 
                                                    blob = ? 
                                                WHERE handle = ?;""",
                               [repository.gramps_id, 
                                pickle.dumps(repository.serialize()),
                                repository.handle])
        else:
            emit = "repository-add"
            self.dbapi.execute("insert into repository values(?, ?, ?);", 
                       [repository.handle, repository.gramps_id, pickle.dumps(repository.serialize())])
        if not trans.batch:
            self.dbapi.commit()
        # Emit after added:
        if emit:
            self.emit(emit, ([repository.handle],))

    def commit_note(self, note, trans, change_time=None):
        emit = None
        if note.handle in self.note_map:
            emit = "note-update"
            self.dbapi.execute("""UPDATE note SET gramps_id = ?, 
                                                    blob = ? 
                                                WHERE handle = ?;""",
                               [note.gramps_id, 
                                pickle.dumps(note.serialize()),
                                note.handle])
        else:
            emit = "note-add"
            self.dbapi.execute("insert into note values(?, ?, ?);", 
                       [note.handle, note.gramps_id, pickle.dumps(note.serialize())])
        if not trans.batch:
            self.dbapi.commit()
        # Emit after added:
        if emit:
            self.emit(emit, ([note.handle],))

    def commit_place(self, place, trans, change_time=None):
        emit = None
        if place.handle in self.place_map:
            emit = "place-update"
            self.dbapi.execute("""UPDATE place SET gramps_id = ?, 
                                                   order_by = ?,
                                                   blob = ? 
                                                WHERE handle = ?;""",
                               [place.gramps_id, 
                                self._order_by_place_key(place),
                                pickle.dumps(place.serialize()),
                                place.handle])
        else:
            emit = "place-add"
            self.dbapi.execute("insert into place values(?, ?, ?, ?);", 
                       [place.handle, 
                        self._order_by_place_key(place),
                        place.gramps_id, 
                        pickle.dumps(place.serialize())])
        if not trans.batch:
            self.dbapi.commit()
        # Emit after added:
        if emit:
            self.emit(emit, ([place.handle],))

    def commit_event(self, event, trans, change_time=None):
        emit = None
        if event.handle in self.event_map:
            emit = "event-update"
            self.dbapi.execute("""UPDATE event SET gramps_id = ?, 
                                                    blob = ? 
                                                WHERE handle = ?;""",
                               [event.gramps_id, 
                                pickle.dumps(event.serialize()),
                                event.handle])
        else:
            emit = "event-add"
            self.dbapi.execute("insert into event values(?, ?, ?);", 
                       [event.handle, 
                        event.gramps_id, 
                        pickle.dumps(event.serialize())])
        if not trans.batch:
            self.dbapi.commit()
        # Emit after added:
        if emit:
            self.emit(emit, ([event.handle],))

    def commit_tag(self, tag, trans, change_time=None):
        emit = None
        if tag.handle in self.tag_map:
            emit = "tag-update"
            self.dbapi.execute("""UPDATE tag SET blob = ?,
                                                 order_by = ?
                                         WHERE handle = ?;""",
                               [pickle.dumps(tag.serialize()),
                                self._order_by_tag_key(tag),
                                tag.handle])
        else:
            emit = "tag-add"
            self.dbapi.execute("insert into tag values(?, ?, ?);", 
                       [tag.handle, 
                        self._order_by_tag_key(tag),
                        pickle.dumps(tag.serialize())])
        if not trans.batch:
            self.dbapi.commit()
        # Emit after added:
        if emit:
            self.emit(emit, ([tag.handle],))

    def commit_media_object(self, media, trans, change_time=None):
        emit = None
        if media.handle in self.media_map:
            emit = "media-update"
            self.dbapi.execute("""UPDATE media SET gramps_id = ?, 
                                                   order_by = ?,
                                                   blob = ? 
                                                WHERE handle = ?;""",
                               [media.gramps_id, 
                                self._order_by_media_key(media),
                                pickle.dumps(media.serialize()),
                                media.handle])
        else:
            emit = "media-add"
            self.dbapi.execute("insert into media values(?, ?, ?, ?);", 
                       [media.handle, 
                        self._order_by_media_key(media),
                        media.gramps_id, 
                        pickle.dumps(media.serialize())])
        if not trans.batch:
            self.dbapi.commit()
        # Emit after added:
        if emit:
            self.emit(emit, ([media.handle],))

    def get_gramps_ids(self, obj_key):
        key2table = {
            PERSON_KEY:     self.person_id_map, 
            FAMILY_KEY:     self.family_id_map, 
            CITATION_KEY:   self.citation_id_map, 
            SOURCE_KEY:     self.source_id_map, 
            EVENT_KEY:      self.event_id_map, 
            MEDIA_KEY:      self.media_id_map, 
            PLACE_KEY:      self.place_id_map, 
            REPOSITORY_KEY: self.repository_id_map, 
            NOTE_KEY:       self.note_id_map, 
            }
        return list(key2table[obj_key].keys())

    def transaction_begin(self, transaction):
        ## FIXME
        return 

    def set_researcher(self, owner):
        self.owner.set_from(owner)

    def get_researcher(self):
        return self.owner

    def request_rebuild(self):
        self.emit('person-rebuild')
        self.emit('family-rebuild')
        self.emit('place-rebuild')
        self.emit('source-rebuild')
        self.emit('citation-rebuild')
        self.emit('media-rebuild')
        self.emit('event-rebuild')
        self.emit('repository-rebuild')
        self.emit('note-rebuild')
        self.emit('tag-rebuild')

    def copy_from_db(self, db):
        """
        A (possibily) implementation-specific method to get data from
        db into this database.
        """
        for key in db._tables.keys():
            cursor = db._tables[key]["cursor_func"]
            class_ = db._tables[key]["class_func"]
            for (handle, data) in cursor():
                map = getattr(self, "%s_map" % key.lower())
                map[handle] = class_.create(data)

    def get_transaction_class(self):
        """
        Get the transaction class associated with this database backend.
        """
        return DBAPITxn

    def get_from_name_and_handle(self, table_name, handle):
        """
        Returns a gen.lib object (or None) given table_name and
        handle.

        Examples:

        >>> self.get_from_name_and_handle("Person", "a7ad62365bc652387008")
        >>> self.get_from_name_and_handle("Media", "c3434653675bcd736f23")
        """
        if table_name in self._tables:
            return self._tables[table_name]["handle_func"](handle)
        return None

    def get_from_name_and_gramps_id(self, table_name, gramps_id):
        """
        Returns a gen.lib object (or None) given table_name and
        Gramps ID.

        Examples:

        >>> self.get_from_name_and_gramps_id("Person", "I00002")
        >>> self.get_from_name_and_gramps_id("Family", "F056")
        >>> self.get_from_name_and_gramps_id("Media", "M00012")
        """
        if table_name in self._tables:
            return self._tables[table_name]["gramps_id_func"](gramps_id)
        return None

    def remove_person(self, handle, transaction):
        """
        Remove the Person specified by the database handle from the database, 
        preserving the change in the passed transaction. 
        """

        if self.readonly or not handle:
            return
        if handle in self.person_map:
            person = Person.create(self.person_map[handle])
            #del self.person_map[handle]
            #del self.person_id_map[person.gramps_id]
            self.dbapi.execute("DELETE from person WHERE handle = ?;", [handle])
            self.emit("person-delete", ([handle],))

    def remove_source(self, handle, transaction):
        """
        Remove the Source specified by the database handle from the
        database, preserving the change in the passed transaction. 
        """
        self.__do_remove(handle, transaction, self.source_map, 
                         self.source_id_map, SOURCE_KEY)

    def remove_citation(self, handle, transaction):
        """
        Remove the Citation specified by the database handle from the
        database, preserving the change in the passed transaction. 
        """
        self.__do_remove(handle, transaction, self.citation_map, 
                         self.citation_id_map, CITATION_KEY)

    def remove_event(self, handle, transaction):
        """
        Remove the Event specified by the database handle from the
        database, preserving the change in the passed transaction. 
        """
        self.__do_remove(handle, transaction, self.event_map, 
                         self.event_id_map, EVENT_KEY)

    def remove_object(self, handle, transaction):
        """
        Remove the MediaObjectPerson specified by the database handle from the
        database, preserving the change in the passed transaction. 
        """
        self.__do_remove(handle, transaction, self.media_map, 
                         self.media_id_map, MEDIA_KEY)

    def remove_place(self, handle, transaction):
        """
        Remove the Place specified by the database handle from the
        database, preserving the change in the passed transaction. 
        """
        self.__do_remove(handle, transaction, self.place_map, 
                         self.place_id_map, PLACE_KEY)

    def remove_family(self, handle, transaction):
        """
        Remove the Family specified by the database handle from the
        database, preserving the change in the passed transaction. 
        """
        self.__do_remove(handle, transaction, self.family_map, 
                         self.family_id_map, FAMILY_KEY)

    def remove_repository(self, handle, transaction):
        """
        Remove the Repository specified by the database handle from the
        database, preserving the change in the passed transaction. 
        """
        self.__do_remove(handle, transaction, self.repository_map, 
                         self.repository_id_map, REPOSITORY_KEY)

    def remove_note(self, handle, transaction):
        """
        Remove the Note specified by the database handle from the
        database, preserving the change in the passed transaction. 
        """
        self.__do_remove(handle, transaction, self.note_map, 
                         self.note_id_map, NOTE_KEY)

    def remove_tag(self, handle, transaction):
        """
        Remove the Tag specified by the database handle from the
        database, preserving the change in the passed transaction. 
        """
        self.__do_remove(handle, transaction, self.tag_map, 
                         None, TAG_KEY)

    def is_empty(self):
        """
        Return true if there are no [primary] records in the database
        """
        for table in self._tables:
            if len(self._tables[table]["handles_func"]()) > 0:
                return False
        return True

    def __do_remove(self, handle, transaction, data_map, data_id_map, key):
        key2table = {
            PERSON_KEY:     "person", 
            FAMILY_KEY:     "family", 
            SOURCE_KEY:     "source", 
            CITATION_KEY:   "citation", 
            EVENT_KEY:      "event", 
            MEDIA_KEY:      "media", 
            PLACE_KEY:      "place", 
            REPOSITORY_KEY: "repository", 
            NOTE_KEY:       "note", 
            }
        if self.readonly or not handle:
            return
        if handle in data_map:
            self.dbapi.execute("DELETE from %s WHERE handle = ?;" % key2table[key], 
                               [handle])
            self.emit(KEY_TO_NAME_MAP[key] + "-delete", ([handle],))

    def delete_primary_from_reference_map(self, handle, transaction, txn=None):
        """
        Remove all references to the primary object from the reference_map.
        handle should be utf-8
        """
        primary_cur = self.get_reference_map_primary_cursor()

        try:
            ret = primary_cur.set(handle)
        except:
            ret = None
        
        remove_list = set()
        while (ret is not None):
            (key, data) = ret
            
            # data values are of the form:
            #   ((primary_object_class_name, primary_object_handle),
            #    (referenced_object_class_name, referenced_object_handle))
            
            # so we need the second tuple give us a reference that we can
            # combine with the primary_handle to get the main key.
            main_key = (handle.decode('utf-8'), pickle.loads(data)[1][1])
            
            # The trick is not to remove while inside the cursor,
            # but collect them all and remove after the cursor is closed
            remove_list.add(main_key)

            ret = primary_cur.next_dup()

        primary_cur.close()

        # Now that the cursor is closed, we can remove things
        for main_key in remove_list:
            self.__remove_reference(main_key, transaction, txn)

    def __remove_reference(self, key, transaction, txn):
        """
        Remove the reference specified by the key, preserving the change in 
        the passed transaction.
        """
        if isinstance(key, tuple):
            #create a byte string key, first validity check in python 3!
            for val in key:
                if isinstance(val, bytes):
                    raise DbError(_('An attempt is made to save a reference key '
                        'which is partly bytecode, this is not allowed.\n'
                        'Key is %s') % str(key))
            key = str(key)
        if isinstance(key, str):
            key = key.encode('utf-8')
        if not self.readonly:
            if not transaction.batch:
                old_data = self.reference_map.get(key, txn=txn)
                transaction.add(REFERENCE_KEY, TXNDEL, key, old_data, None)
                #transaction.reference_del.append(str(key))
            self.reference_map.delete(key, txn=txn)

    ## Missing:

    def backup(self):
        ## FIXME
        pass

    def close(self):
        if self._directory:
            from gramps.plugins.export.exportxml import XmlWriter
            from gramps.cli.user import User 
            writer = XmlWriter(self, User(), strip_photos=0, compress=1)
            filename = os.path.join(self._directory, "data.gramps")
            writer.write(filename)
            filename = os.path.join(self._directory, "meta_data.db")
            touch(filename)
            self.dbapi.close()

    def find_backlink_handles(self, handle, include_classes=None):
        ## FIXME
        return []

    def find_initial_person(self):
        items = self.person_map.keys()
        if len(items) > 0:
            return self.get_person_from_handle(list(items)[0])
        return None

    def find_place_child_handles(self, handle):
        ## FIXME
        return []

    def get_bookmarks(self):
        return self.bookmarks

    def get_child_reference_types(self):
        ## FIXME
        return []

    def get_citation_bookmarks(self):
        return self.citation_bookmarks

    def get_cursor(self, table, txn=None, update=False, commit=False):
        ## FIXME
        ## called from a complete find_back_ref
        pass

    # cursors for lookups in the reference_map for back reference
    # lookups. The reference_map has three indexes:
    # the main index: a tuple of (primary_handle, referenced_handle)
    # the primary_handle index: the primary_handle
    # the referenced_handle index: the referenced_handle
    # the main index is unique, the others allow duplicate entries.

    def get_default_handle(self):
        items = self.person_map.keys()
        if len(items) > 0:
            return list(items)[0]
        return None

    def get_event_attribute_types(self):
        ## FIXME
        return []

    def get_event_bookmarks(self):
        return self.event_bookmarks

    def get_event_roles(self):
        ## FIXME
        return []

    def get_event_types(self):
        ## FIXME
        return []

    def get_family_attribute_types(self):
        ## FIXME
        return []

    def get_family_bookmarks(self):
        return self.family_bookmarks

    def get_family_event_types(self):
        ## FIXME
        return []

    def get_family_relation_types(self):
        ## FIXME
        return []

    def get_media_attribute_types(self):
        ## FIXME
        return []

    def get_media_bookmarks(self):
        return self.media_bookmarks

    def get_name_types(self):
        ## FIXME
        return []

    def get_note_bookmarks(self):
        return self.note_bookmarks

    def get_note_types(self):
        ## FIXME
        return []

    def get_origin_types(self):
        ## FIXME
        return []

    def get_person_attribute_types(self):
        ## FIXME
        return []

    def get_person_event_types(self):
        ## FIXME
        return []

    def get_place_bookmarks(self):
        return self.place_bookmarks

    def get_place_tree_cursor(self):
        ## FIXME
        return []

    def get_place_types(self):
        ## FIXME
        return []

    def get_repo_bookmarks(self):
        return self.repo_bookmarks

    def get_repository_types(self):
        ## FIXME
        return []

    def get_save_path(self):
        return self._directory

    def get_source_attribute_types(self):
        ## FIXME
        return []

    def get_source_bookmarks(self):
        return self.source_bookmarks

    def get_source_media_types(self):
        ## FIXME
        return []

    def get_surname_list(self):
        ## FIXME
        return []

    def get_url_types(self):
        ## FIXME
        return []

    def has_changed(self):
        ## FIXME
        return True

    def is_open(self):
        return self._directory is not None

    def iter_citation_handles(self):
        return (data[0] for data in self.get_citation_cursor())

    def iter_citations(self):
        return (Citation.create(data[1]) for data in self.get_citation_cursor())

    def iter_event_handles(self):
        return (data[0] for data in self.get_event_cursor())

    def iter_events(self):
        return (Event.create(data[1]) for data in self.get_event_cursor())

    def iter_media_objects(self):
        return (MediaObject.create(data[1]) for data in self.get_media_cursor())

    def iter_note_handles(self):
        return (data[0] for data in self.get_note_cursor())

    def iter_notes(self):
        return (Note.create(data[1]) for data in self.get_note_cursor())

    def iter_place_handles(self):
        return (data[0] for data in self.get_place_cursor())

    def iter_places(self):
        return (Place.create(data[1]) for data in self.get_place_cursor())

    def iter_repositories(self):
        return (Repository.create(data[1]) for data in self.get_repository_cursor())

    def iter_repository_handles(self):
        return (data[0] for data in self.get_repository_cursor())

    def iter_source_handles(self):
        return (data[0] for data in self.get_source_cursor())

    def iter_sources(self):
        return (Source.create(data[1]) for data in self.get_source_cursor())

    def iter_tag_handles(self):
        return (data[0] for data in self.get_tag_cursor())

    def iter_tags(self):
        return (Tag.create(data[1]) for data in self.get_tag_cursor())

    def load(self, directory, pulse_progress=None, mode=None, 
             force_schema_upgrade=False, 
             force_bsddb_upgrade=False, 
             force_bsddb_downgrade=False, 
             force_python_upgrade=False):
        # Run code from directory
        default_settings = {"__file__": 
                            os.path.join(directory, "default_settings.py")}
        settings_file = os.path.join(directory, "default_settings.py")
        with open(settings_file) as f:
            code = compile(f.read(), settings_file, 'exec')
            exec(code, globals(), default_settings)

        self.dbapi = default_settings["dbapi"]
            
        # make sure schema is up to date:
        self.dbapi.execute("""CREATE TABLE IF NOT EXISTS person (
                                    handle    TEXT PRIMARY KEY NOT NULL,
                                    order_by  TEXT             ,
                                    gramps_id TEXT             ,
                                    blob      TEXT
        );""")
        self.dbapi.execute("""CREATE TABLE IF NOT EXISTS family (
                                    handle    TEXT PRIMARY KEY NOT NULL,
                                    gramps_id TEXT             ,
                                    blob      TEXT
        );""")
        self.dbapi.execute("""CREATE TABLE IF NOT EXISTS source (
                                    handle    TEXT PRIMARY KEY NOT NULL,
                                    order_by  TEXT             ,
                                    gramps_id TEXT             ,
                                    blob      TEXT
        );""")
        self.dbapi.execute("""CREATE TABLE IF NOT EXISTS citation (
                                    handle    TEXT PRIMARY KEY NOT NULL,
                                    order_by  TEXT             ,
                                    gramps_id TEXT             ,
                                    blob      TEXT
        );""")
        self.dbapi.execute("""CREATE TABLE IF NOT EXISTS event (
                                    handle    TEXT PRIMARY KEY NOT NULL,
                                    gramps_id TEXT             ,
                                    blob      TEXT
        );""")
        self.dbapi.execute("""CREATE TABLE IF NOT EXISTS media (
                                    handle    TEXT PRIMARY KEY NOT NULL,
                                    order_by  TEXT             ,
                                    gramps_id TEXT             ,
                                    blob      TEXT
        );""")
        self.dbapi.execute("""CREATE TABLE IF NOT EXISTS place (
                                    handle    TEXT PRIMARY KEY NOT NULL,
                                    order_by  TEXT             ,
                                    gramps_id TEXT             ,
                                    blob      TEXT
        );""")
        self.dbapi.execute("""CREATE TABLE IF NOT EXISTS repository (
                                    handle    TEXT PRIMARY KEY NOT NULL,
                                    gramps_id TEXT             ,
                                    blob      TEXT
        );""")
        self.dbapi.execute("""CREATE TABLE IF NOT EXISTS note (
                                    handle    TEXT PRIMARY KEY NOT NULL,
                                    gramps_id TEXT             ,
                                    blob      TEXT
        );""")
        self.dbapi.execute("""CREATE TABLE IF NOT EXISTS tag (
                                    handle    TEXT PRIMARY KEY NOT NULL,
                                    order_by  TEXT             ,
                                    blob      TEXT
        );""")

    def redo(self, update_history=True):
        ## FIXME
        pass

    def restore(self):
        ## FIXME
        pass

    def set_prefixes(self, person, media, family, source, citation, 
                     place, event, repository, note):
        ## FIXME
        pass

    def set_save_path(self, directory):
        self._directory = directory
        self.full_name = os.path.abspath(self._directory)
        self.path = self.full_name
        self.brief_name = os.path.basename(self._directory)

    def undo(self, update_history=True):
        ## FIXME
        pass

    def write_version(self, directory):
        """Write files for a newly created DB."""
        versionpath = os.path.join(directory, str(DBBACKEND))
        _LOG.debug("Write database backend file to 'dbapi'")
        with open(versionpath, "w") as version_file:
            version_file.write("dbapi")
        # Write default_settings, sqlite.db
        defaults = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "dbapi_support", "defaults")
        _LOG.debug("Copy defaults from: " + defaults)
        for filename in os.listdir(defaults):
            fullpath = os.path.abspath(os.path.join(defaults, filename))
            shutil.copy2(fullpath, directory)

    def report_bm_change(self):
        """
        Add 1 to the number of bookmark changes during this session.
        """
        self._bm_changes += 1

    def db_has_bm_changes(self):
        """
        Return whethere there were bookmark changes during the session.
        """
        return self._bm_changes > 0

    def get_summary(self):
        """
        Returns dictionary of summary item.
        Should include, if possible:

        _("Number of people")
        _("Version")
        _("Schema version")
        """
        return {
            _("Number of people"): self.get_number_of_people(),
        }

    def get_dbname(self):
        """
        In DBAPI, the database is in a text file at the path
        """
        filepath = os.path.join(self._directory, "name.txt")
        try:
            name_file = open(filepath, "r")
            name = name_file.readline().strip()
            name_file.close()
        except (OSError, IOError) as msg:
            _LOG.error(str(msg))
            name = None
        return name

    def reindex_reference_map(self):
        ## FIXME
        pass

    def rebuild_secondary(self, update):
        ## FIXME
        pass

    def prepare_import(self):
        """
        DBAPI does not commit data on gedcom import, but saves them
        for later commit.
        """
        pass

    def commit_import(self):
        """
        Commits the items that were queued up during the last gedcom
        import for two step adding.
        """
        pass

    def has_handle_for_person(self, key):
        cur = self.dbapi.execute("select * from person where handle = ?", [key])
        return cur.fetchone() != None

    def has_handle_for_family(self, key):
        cur = self.dbapi.execute("select * from family where handle = ?", [key])
        return cur.fetchone() != None

    def has_handle_for_source(self, key):
        cur = self.dbapi.execute("select * from source where handle = ?", [key])
        return cur.fetchone() != None

    def has_handle_for_citation(self, key):
        cur = self.dbapi.execute("select * from citation where handle = ?", [key])
        return cur.fetchone() != None

    def has_handle_for_event(self, key):
        cur = self.dbapi.execute("select * from event where handle = ?", [key])
        return cur.fetchone() != None

    def has_handle_for_media(self, key):
        cur = self.dbapi.execute("select * from media where handle = ?", [key])
        return cur.fetchone() != None

    def has_handle_for_place(self, key):
        cur = self.dbapi.execute("select * from place where handle = ?", [key])
        return cur.fetchone() != None

    def has_handle_for_repository(self, key):
        cur = self.dbapi.execute("select * from repository where handle = ?", [key])
        return cur.fetchone() != None

    def has_handle_for_note(self, key):
        cur = self.dbapi.execute("select * from note where handle = ?", [key])
        return cur.fetchone() != None

    def has_gramps_id_for_person(self, key):
        cur = self.dbapi.execute("select * from person where gramps_id = ?", [key])
        return cur.fetchone() != None

    def has_gramps_id_for_family(self, key):
        cur = self.dbapi.execute("select * from family where gramps_id = ?", [key])
        return cur.fetchone() != None

    def has_gramps_id_for_source(self, key):
        cur = self.dbapi.execute("select * from source where gramps_id = ?", [key])
        return cur.fetchone() != None

    def has_gramps_id_for_citation(self, key):
        cur = self.dbapi.execute("select * from citation where gramps_id = ?", [key])
        return cur.fetchone() != None

    def has_gramps_id_for_event(self, key):
        cur = self.dbapi.execute("select * from event where gramps_id = ?", [key])
        return cur.fetchone() != None

    def has_gramps_id_for_media(self, key):
        cur = self.dbapi.execute("select * from media where gramps_id = ?", [key])
        return cur.fetchone() != None

    def has_gramps_id_for_place(self, key):
        cur = self.dbapi.execute("select * from place where gramps_id = ?", [key])
        return cur.fetchone() != None

    def has_gramps_id_for_repository(self, key):
        cur = self.dbapi.execute("select * from repository where gramps_id = ?", [key])
        return cur.fetchone() != None

    def has_gramps_id_for_note(self, key):
        cur = self.dbapi.execute("select * from note where gramps_id = ?", [key])
        return cur.fetchone() != None

    def get_person_gramps_ids(self):
        cur = self.dbapi.execute("select gramps_id from person;")
        rows = cur.fetchall()
        return [row[0] for row in rows]

    def get_family_gramps_ids(self):
        cur = self.dbapi.execute("select gramps_id from family;")
        rows = cur.fetchall()
        return [row[0] for row in rows]

    def get_source_gramps_ids(self):
        cur = self.dbapi.execute("select gramps_id from source;")
        rows = cur.fetchall()
        return [row[0] for row in rows]

    def get_citation_gramps_ids(self):
        cur = self.dbapi.execute("select gramps_id from citation;")
        rows = cur.fetchall()
        return [row[0] for row in rows]

    def get_event_gramps_ids(self):
        cur = self.dbapi.execute("select gramps_id from event;")
        rows = cur.fetchall()
        return [row[0] for row in rows]

    def get_media_gramps_ids(self):
        cur = self.dbapi.execute("select gramps_id from media;")
        rows = cur.fetchall()
        return [row[0] for row in rows]

    def get_place_gramps_ids(self):
        cur = self.dbapi.execute("select gramps from place;")
        rows = cur.fetchall()
        return [row[0] for row in rows]

    def get_repository_gramps_ids(self):
        cur = self.dbapi.execute("select gramps_id from repository;")
        rows = cur.fetchall()
        return [row[0] for row in rows]

    def get_note_gramps_ids(self):
        cur = self.dbapi.execute("select gramps_id from note;")
        rows = cur.fetchall()
        return [row[0] for row in rows]

    def _get_raw_person_data(self, key):
        cur = self.dbapi.execute("select blob from person where handle = ?", [key])
        row = cur.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_family_data(self, key):
        cur = self.dbapi.execute("select blob from family where handle = ?", [key])
        row = cur.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_source_data(self, key):
        cur = self.dbapi.execute("select blob from source where handle = ?", [key])
        row = cur.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_citation_data(self, key):
        cur = self.dbapi.execute("select blob from citation where handle = ?", [key])
        row = cur.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_event_data(self, key):
        cur = self.dbapi.execute("select blob from event where handle = ?", [key])
        row = cur.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_media_data(self, key):
        cur = self.dbapi.execute("select blob from media where handle = ?", [key])
        row = cur.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_place_data(self, key):
        cur = self.dbapi.execute("select blob from place where handle = ?", [key])
        row = cur.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_repository_data(self, key):
        cur = self.dbapi.execute("select blob from repository where handle = ?", [key])
        row = cur.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_note_data(self, key):
        cur = self.dbapi.execute("select blob from note where handle = ?", [key])
        row = cur.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_tag_data(self, key):
        cur = self.dbapi.execute("select blob from tag where handle = ?", [key])
        row = cur.fetchone()
        if row:
            return pickle.loads(row[0])

    def _order_by_person_key(self, person):
        """
        All non pa/matronymic surnames are used in indexing.
        pa/matronymic not as they change for every generation!
        returns a byte string
        """
        if person.primary_name and person.primary_name.surname_list:
            order_by = " ".join([x.surname for x in person.primary_name.surname_list if not 
                                 (int(x.origintype) in [NameOriginType.PATRONYMIC, 
                                                        NameOriginType.MATRONYMIC]) ])
        else:
            order_by = ""
        return glocale.sort_key(order_by)

    def _order_by_place_key(self, place):
        return glocale.sort_key(place.title)

    def _order_by_source_key(self, source):
        return glocale.sort_key(source.title)

    def _order_by_citation_key(self, citation):
        return glocale.sort_key(citation.page)

    def _order_by_media_key(self, media):
        return glocale.sort_key(media.desc)

    def _order_by_tag_key(self, tag):
        return glocale.sort_key(tag.get_name())

