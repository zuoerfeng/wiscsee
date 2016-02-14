import bitarray
from collections import deque, Counter
import csv
import datetime
import random
import os
import Queue
import sys
import simpy

import bidict

import config
import flash
import ftlbuilder
import lrulist
import recorder
import utils


UNINITIATED, MISS = ('UNINIT', 'MISS')
DATA_BLOCK, TRANS_BLOCK = ('data_block', 'trans_block')
random.seed(0)

class GlobalHelper(object):
    """
    In case you need some global variables. We put all global stuff here so
    it is easier to manage. (And you know all the bad things you did :)
    """
    def __init__(self, confobj):
        pass

LOGICAL_READ, LOGICAL_WRITE, LOGICAL_DISCARD = ('LOGICAL_READ', \
        'LOGICAL_WRITE', 'LOGICAL_DISCARD')


def block_to_channel_block(conf, blocknum):
    n_blocks_per_channel = conf.n_blocks_per_channel
    channel = blocknum / n_blocks_per_channel
    block_off = blocknum % n_blocks_per_channel
    return channel, block_off

def channel_block_to_block(conf, channel, block_off):
    n_blocks_per_channel = conf.n_blocks_per_channel
    return channel * n_blocks_per_channel + block_off

def page_to_channel_page(conf, pagenum):
    """
    pagenum is in the context of device
    """
    n_pages_per_channel = conf.n_pages_per_channel
    channel = pagenum / n_pages_per_channel
    page_off = pagenum % n_pages_per_channel
    return channel, page_off

def channel_page_to_page(conf, channel, page_off):
    """
    Translate channel, page_off to pagenum in context of device
    """
    return channel * conf.n_pages_per_channel + page_off

class OutOfBandAreas(object):
    """
    It is used to hold page state and logical page number of a page.
    It is not necessary to implement it as list. But the interface should
    appear to be so.  It consists of page state (bitmap) and logical page
    number (dict).  Let's proivde more intuitive interfaces: OOB should accept
    events, and react accordingly to this event. The action may involve state
    and lpn_of_phy_page.
    """
    def __init__(self, confobj):
        self.conf = confobj

        self.flash_num_blocks = confobj.n_blocks_per_dev
        self.flash_npage_per_block = confobj.n_pages_per_block
        self.total_pages = self.flash_num_blocks * self.flash_npage_per_block

        # Key data structures
        self.states = ftlbuilder.FlashBitmap2(confobj)
        # ppn->lpn mapping stored in OOB, Note that for translation pages, this
        # mapping is ppn -> m_vpn
        self.ppn_to_lpn_mvpn = {}
        # Timestamp table PPN -> timestamp
        # Here are the rules:
        # 1. only programming a PPN updates the timestamp of PPN
        #    if the content is new from FS, timestamp is the timestamp of the
        #    LPN
        #    if the content is copied from other flash block, timestamp is the
        #    same as the previous ppn
        # 2. discarding, and reading a ppn does not change it.
        # 3. erasing a block will remove all the timestamps of the block
        # 4. so cur_timestamp can only be advanced by LBA operations
        self.timestamp_table = {}
        self.cur_timestamp = 0

        # flash block -> last invalidation time
        # int -> timedate.timedate
        self.last_inv_time_of_block = {}

    ############# Time stamp related ############
    def timestamp(self):
        """
        This function will advance timestamp
        """
        t = self.cur_timestamp
        self.cur_timestamp += 1
        return t

    def timestamp_set_ppn(self, ppn):
        self.timestamp_table[ppn] = self.timestamp()

    def timestamp_copy(self, src_ppn, dst_ppn):
        self.timestamp_table[dst_ppn] = self.timestamp_table[src_ppn]

    def translate_ppn_to_lpn(self, ppn):
        return self.ppn_to_lpn_mvpn[ppn]

    def wipe_ppn(self, ppn):
        self.states.invalidate_page(ppn)
        block, _ = self.conf.page_to_block_off(ppn)
        self.last_inv_time_of_block[block] = datetime.datetime.now()

        # It is OK to delay it until we erase the block
        # try:
            # del self.ppn_to_lpn_mvpn[ppn]
        # except KeyError:
            # # it is OK that the key does not exist, for example,
            # # when discarding without writing to it
            # pass

    def erase_block(self, flash_block):
        self.states.erase_block(flash_block)

        start, end = self.conf.block_to_page_range(flash_block)
        for ppn in range(start, end):
            try:
                del self.ppn_to_lpn_mvpn[ppn]
                # if you try to erase translation block here, it may fail,
                # but it is expected.
                del self.timestamp_table[ppn]
            except KeyError:
                pass

        del self.last_inv_time_of_block[flash_block]

    def new_write(self, lpn, old_ppn, new_ppn):
        """
        mark the new_ppn as valid
        update the LPN in new page's OOB to lpn
        invalidate the old_ppn, so cleaner can GC it
        """
        self.states.validate_page(new_ppn)
        self.ppn_to_lpn_mvpn[new_ppn] = lpn

        if old_ppn != UNINITIATED:
            # the lpn has mapping before this write
            self.wipe_ppn(old_ppn)

    def new_lba_write(self, lpn, old_ppn, new_ppn):
        """
        This is exclusively for lba_write(), so far
        """
        self.timestamp_set_ppn(new_ppn)
        self.new_write(lpn, old_ppn, new_ppn)

    def data_page_move(self, lpn, old_ppn, new_ppn):
        # move data page does not change the content's timestamp, so
        # we copy
        self.timestamp_copy(src_ppn = old_ppn, dst_ppn = new_ppn)
        self.new_write(lpn, old_ppn, new_ppn)

    def lpns_of_block(self, flash_block):
        s, e = self.conf.block_to_page_range(flash_block)
        lpns = []
        for ppn in range(s, e):
            lpns.append(self.ppn_to_lpn_mvpn.get(ppn, 'NA'))

        return lpns


class BlockPool(object):
    def __init__(self, confobj):
        self.conf = confobj
        self.n_channels = self.conf['flash_config']['n_channels_per_dev']
        self.channel_pools = [ChannelBlockPool(self.conf, i)
                for i in range(self.n_channels)]

        self.cur_channel = 0

    @property
    def freeblocks(self):
        free = []
        for channel in self.channel_pools:
            free.extend( channel.freeblocks_global )
        return free

    @property
    def data_usedblocks(self):
        used = []
        for channel in self.channel_pools:
            used.extend( channel.data_usedblocks_global )
        return used

    @property
    def trans_usedblocks(self):
        used = []
        for channel in self.channel_pools:
            used.extend( channel.trans_usedblocks_global )
        return used

    def iter_channels(self, funcname, addr_type):
        n = self.n_channels
        while n > 0:
            n -= 1
            try:
                in_channel_offset = eval("self.channel_pools[self.cur_channel].{}()"\
                        .format(funcname))
            except OutOfSpaceError:
                pass
            else:
                if addr_type == 'block':
                    ret = channel_block_to_block(self.conf, self.cur_channel,
                            in_channel_offset)
                elif addr_type == 'page':
                    ret = channel_page_to_page(self.conf, self.cur_channel,
                            in_channel_offset)
                else:
                    raise RuntimeError("addr_type {} is not supported."\
                        .format(addr_type))
                return ret
            finally:
                self.cur_channel = (self.cur_channel + 1) % self.n_channels

        raise OutOfSpaceError("Tried all channels. Out of Space")

    def pop_a_free_block(self):
        return self.iter_channels("pop_a_free_block", addr_type = 'block')

    def pop_a_free_block_to_trans(self):
        return self.iter_channels("pop_a_free_block_to_trans",
            addr_type = 'block')

    def pop_a_free_block_to_data(self):
        return self.iter_channels("pop_a_free_block_to_data",
            addr_type = 'block')

    def move_used_data_block_to_free(self, blocknum):
        channel, block_off = block_to_channel_block(self.conf, blocknum)
        self.channel_pools[channel].move_used_data_block_to_free(block_off)

    def move_used_trans_block_to_free(self, blocknum):
        channel, block_off = block_to_channel_block(self.conf, blocknum)
        self.channel_pools[channel].move_used_trans_block_to_free(block_off)

    def next_data_page_to_program(self):
        return self.iter_channels("next_data_page_to_program",
            addr_type = 'page')

    def next_translation_page_to_program(self):
        return self.iter_channels("next_translation_page_to_program",
            addr_type = 'page')

    def next_gc_data_page_to_program(self):
        return self.iter_channels("next_gc_data_page_to_program",
            addr_type = 'page')

    def next_gc_translation_page_to_program(self):
        return self.iter_channels("next_gc_translation_page_to_program",
            addr_type = 'page')

    def current_blocks(self):
        cur_blocks = []
        for channel in self.channel_pools:
            cur_blocks.extend( channel.current_blocks_global )

        return cur_blocks

    def used_ratio(self):
        n_used = 0
        for channel in self.channel_pools:
            n_used += channel.total_used_blocks()

        return float(n_used) / self.conf.n_blocks_per_dev

    def total_used_blocks(self):
        total = 0
        for channel in self.channel_pools:
            total += channel.total_used_blocks()
        return total

    def num_freeblocks(self):
        total = 0
        for channel in self.channel_pools:
            total += len( channel.freeblocks )
        return total


class OutOfSpaceError(RuntimeError):
    pass


class ChannelBlockPool(object):
    """
    This class maintains the free blocks and used blocks of a
    flash channel.
    The block number of each channel starts from 0.
    """
    def __init__(self, confobj, channel_no):
        self.conf = confobj

        self.freeblocks = deque(
            range(self.conf.n_blocks_per_channel))

        # initialize usedblocks
        self.trans_usedblocks = []
        self.data_usedblocks  = []

        self.channel_no = channel_no

    def shift_to_global(self, blocks):
        """
        calculate the block num in global namespace for blocks
        """
        return [ channel_block_to_block(self.conf, self.channel_no, block_off)
            for block_off in blocks ]

    @property
    def freeblocks_global(self):
        return self.shift_to_global(self.freeblocks)

    @property
    def trans_usedblocks_global(self):
        return self.shift_to_global(self.trans_usedblocks)

    @property
    def data_usedblocks_global(self):
        return self.shift_to_global(self.data_usedblocks)

    @property
    def current_blocks_global(self):
        local_cur_blocks = self.current_blocks()

        global_cur_blocks = []
        for block in local_cur_blocks:
            if block == None:
                global_cur_blocks.append(block)
            else:
                global_cur_blocks.append(
                    channel_block_to_block(self.conf, self.channel_no, block) )

        return global_cur_blocks

    def pop_a_free_block(self):
        if self.freeblocks:
            blocknum = self.freeblocks.popleft()
        else:
            # nobody has free block
            raise OutOfSpaceError('No free blocks in device!!!!')

        return blocknum

    def pop_a_free_block_to_trans(self):
        "take one block from freelist and add it to translation block list"
        blocknum = self.pop_a_free_block()
        self.trans_usedblocks.append(blocknum)
        return blocknum

    def pop_a_free_block_to_data(self):
        "take one block from freelist and add it to data block list"
        blocknum = self.pop_a_free_block()
        self.data_usedblocks.append(blocknum)
        return blocknum

    def move_used_data_block_to_free(self, blocknum):
        self.data_usedblocks.remove(blocknum)
        self.freeblocks.append(blocknum)

    def move_used_trans_block_to_free(self, blocknum):
        try:
            self.trans_usedblocks.remove(blocknum)
        except ValueError:
            sys.stderr.write( 'blocknum:' + str(blocknum) )
            raise
        self.freeblocks.append(blocknum)

    def total_used_blocks(self):
        return len(self.trans_usedblocks) + len(self.data_usedblocks)

    def next_page_to_program(self, log_end_name_str, pop_free_block_func):
        """
        The following comment uses next_data_page_to_program() as a example.

        it finds out the next available page to program
        usually it is the page after log_end_pagenum.

        If next=log_end_pagenum + 1 is in the same block with
        log_end_pagenum, simply return log_end_pagenum + 1
        If next=log_end_pagenum + 1 is out of the block of
        log_end_pagenum, we need to pick a new block from self.freeblocks

        This function is stateful, every time you call it, it will advance by
        one.
        """

        if not hasattr(self, log_end_name_str):
           # This is only executed for the first time
           cur_block = pop_free_block_func()
           # use the first page of this block to be the
           next_page = self.conf.block_off_to_page(cur_block, 0)
           # log_end_name_str is the page that is currently being operated on
           setattr(self, log_end_name_str, next_page)

           return next_page

        cur_page = getattr(self, log_end_name_str)
        cur_block, cur_off = self.conf.page_to_block_off(cur_page)

        next_page = (cur_page + 1) % self.conf.total_num_pages()
        next_block, next_off = self.conf.page_to_block_off(next_page)

        if cur_block == next_block:
            ret = next_page
        else:
            # get a new block
            block = pop_free_block_func()
            start, _ = self.conf.block_to_page_range(block)
            ret = start

        setattr(self, log_end_name_str, ret)
        return ret

    def next_data_page_to_program(self):
        return self.next_page_to_program('data_log_end_ppn',
            self.pop_a_free_block_to_data)

    def next_translation_page_to_program(self):
        return self.next_page_to_program('trans_log_end_ppn',
            self.pop_a_free_block_to_trans)

    def next_gc_data_page_to_program(self):
        return self.next_page_to_program('gc_data_log_end_ppn',
            self.pop_a_free_block_to_data)

    def next_gc_translation_page_to_program(self):
        return self.next_page_to_program('gc_trans_log_end_ppn',
            self.pop_a_free_block_to_trans)

    def current_blocks(self):
        try:
            cur_data_block, _ = self.conf.page_to_block_off(
                self.data_log_end_ppn)
        except AttributeError:
            cur_data_block = None

        try:
            cur_trans_block, _ = self.conf.page_to_block_off(
                self.trans_log_end_ppn)
        except AttributeError:
            cur_trans_block = None

        try:
            cur_gc_data_block, _ = self.conf.page_to_block_off(
                self.gc_data_log_end_ppn)
        except AttributeError:
            cur_gc_data_block = None

        try:
            cur_gc_trans_block, _ = self.conf.page_to_block_off(
                self.gc_trans_log_end_ppn)
        except AttributeError:
            cur_gc_trans_block = None

        return (cur_data_block, cur_trans_block, cur_gc_data_block,
            cur_gc_trans_block)

    def __repr__(self):
        ret = ' '.join(['freeblocks', repr(self.freeblocks)]) + '\n' + \
            ' '.join(['trans_usedblocks', repr(self.trans_usedblocks)]) + \
            '\n' + \
            ' '.join(['data_usedblocks', repr(self.data_usedblocks)])
        return ret

    def visual(self):
        block_states = [ 'O' if block in self.freeblocks else 'X'
            for block in range(self.conf.n_blocks_per_channel)]
        return ''.join(block_states)

    def used_ratio(self):
        return (len(self.trans_usedblocks) + len(self.data_usedblocks))\
            / float(self.conf.n_blocks_per_channel)

class CacheEntryData(object):
    """
    This is a helper class that store entry data for a LPN
    """
    def __init__(self, lpn, ppn, dirty):
        self.lpn = lpn
        self.ppn = ppn
        self.dirty = dirty

    def __repr__(self):
        return "lpn:{}, ppn:{}, dirty:{}".format(self.lpn,
            self.ppn, self.dirty)


class CachedMappingTable(object):
    """
    When do we need batched update?
    - do we need it when cleaning translation pages? NO. cleaning translation
    pages does not change contents of translation page.
    - do we need it when cleaning data page? Yes. When cleaning data page, you
    need to modify some lpn->ppn. For those LPNs in the same translation page,
    you can group them and update together. The process is: put those LPNs to
    the same group, read the translation page, modify entries and write it to
    flash. If you want batch updates here, you will need to buffer a few
    lpn->ppn. Well, since we have limited SRAM, you cannot do this.
    TODO: maybe you need to implement this.

    - do we need it when writing a lpn? To be exact, we need it when evict an
    entry in CMT. In that case, we need to find all the CMT entries in the same
    translation page with the victim entry.
    """
    def __init__(self, confobj):
        self.conf = confobj

        self.entry_bytes = 8 # lpn + ppn
        max_bytes = self.conf['dftl']['max_cmt_bytes']
        self.max_n_entries = (max_bytes + self.entry_bytes - 1) / \
            self.entry_bytes
        print 'cache max entries', self.max_n_entries, \
            self.max_n_entries * 4096 / 2**20, 'MB'

        # self.entries = {}
        # self.entries = lrulist.LruCache()
        self.entries = lrulist.SegmentedLruCache(self.max_n_entries, 0.5)

    def lpn_to_ppn(self, lpn):
        "Try to find ppn of the given lpn in cache"
        entry_data = self.entries.get(lpn, MISS)
        if entry_data == MISS:
            return MISS
        else:
            return entry_data.ppn

    def add_new_entry(self, lpn, ppn, dirty):
        "dirty is a boolean"
        if self.entries.has_key(lpn):
            raise RuntimeError("{}:{} already exists in CMT entries.".format(
                lpn, self.entries[lpn].ppn))
        self.entries[lpn] = CacheEntryData(lpn = lpn, ppn = ppn, dirty = dirty)

    def update_entry(self, lpn, ppn, dirty):
        "You may end up remove the old one"
        self.entries[lpn] = CacheEntryData(lpn = lpn, ppn = ppn, dirty = dirty)

    def overwrite_entry(self, lpn, ppn, dirty):
        "lpn must exist"
        self.entries[lpn].ppn = ppn
        self.entries[lpn].dirty = dirty

    def remove_entry_by_lpn(self, lpn):
        del self.entries[lpn]

    def victim_entry(self):
        # lpn = random.choice(self.entries.keys())
        classname = type(self.entries).__name__
        if classname in ('SegmentedLruCache', 'LruCache'):
            lpn = self.entries.victim_key()
        else:
            raise RuntimeError("You need to specify victim selection")

        # lpn, Cacheentrydata
        return lpn, self.entries.peek(lpn)

    def is_full(self):
        n = len(self.entries)
        assert n <= self.max_n_entries
        return n == self.max_n_entries

    def __repr__(self):
        return repr(self.entries)


class GlobalMappingTable(object):
    """
    This mapping table is for data pages, not for translation pages.
    GMT should have entries as many as the number of pages in flash
    """
    def __init__(self, confobj, flashobj):
        """
        flashobj is the flash device that we may operate on.
        """
        if not isinstance(confobj, config.Config):
            raise TypeError("confobj is not conf.Config. it is {}".
               format(type(confobj).__name__))

        self.conf = confobj

        self.n_entries_per_page = self.conf.dftl_n_mapping_entries_per_page()

        # do the easy thing first, if necessary, we can later use list or
        # other data structure
        self.entries = {}

    def total_entries(self):
        """
        total number of entries stored in global mapping table.  It is the same
        as the number of pages in flash, since we use page-leveling mapping
        """
        return self.conf.total_num_pages()

    def total_translation_pages(self):
        """
        total number of translation pages needed. It is:
        total_entries * entry size / page size
        """
        entries = self.total_entries()
        entry_bytes = self.conf['dftl']['global_mapping_entry_bytes']
        flash_page_size = self.conf.page_size
        # play the ceiling trick
        return (entries * entry_bytes + (flash_page_size -1))/flash_page_size

    def lpn_to_ppn(self, lpn):
        """
        GMT should always be able to answer query. It is perfectly OK to return
        None because at the beginning there is no mapping. No valid data block
        on device.
        """
        return self.entries.get(lpn, UNINITIATED)

    def update(self, lpn, ppn):
        self.entries[lpn] = ppn

    def __repr__(self):
        return "global mapping table: {}".format(repr(self.entries))


class GlobalTranslationDirectory(object):
    """
    This is an in-memory data structure. It is only for book keeping. It used
    to remeber thing so that we don't lose it.
    """
    def __init__(self, confobj):
        self.conf = confobj

        self.flash_npage_per_block = self.conf.n_pages_per_block
        self.flash_num_blocks = self.conf.n_blocks_per_dev
        self.flash_page_size = self.conf.page_size
        self.total_pages = self.conf.total_num_pages()

        self.n_entries_per_page = self.conf.dftl_n_mapping_entries_per_page()

        # M_VPN -> M_PPN
        # Virtual translation page number --> Physical translation page number
        # Dftl should initialize
        self.mapping = {}

    def m_vpn_to_m_ppn(self, m_vpn):
        """
        m_vpn virtual translation page number. It should always be successfull.
        """
        return self.mapping[m_vpn]

    def add_mapping(self, m_vpn, m_ppn):
        if self.mapping.has_key(m_vpn):
            raise RuntimeError("self.mapping already has m_vpn:{}"\
                .format(m_vpn))
        self.mapping[m_vpn] = m_ppn

    def update_mapping(self, m_vpn, m_ppn):
        self.mapping[m_vpn] = m_ppn

    def remove_mapping(self, m_vpn):
        del self.mapping[m_vpn]

    def m_vpn_of_lpn(self, lpn):
        "Find the virtual translation page that holds lpn"
        return lpn / self.n_entries_per_page

    def m_vpn_to_lpns(self, m_vpn):
        start_lpn = m_vpn * self.n_entries_per_page
        return range(start_lpn, start_lpn + self.n_entries_per_page)

    def m_ppn_of_lpn(self, lpn):
        m_vpn = self.m_vpn_of_lpn(lpn)
        m_ppn = self.m_vpn_to_m_ppn(m_vpn)
        return m_ppn

    def __repr__(self):
        return repr(self.mapping)


class MappingManager(object):
    """
    This class is the supervisor of all the mappings. When initializing, it
    register CMT and GMT with it and provides higher level operations on top of
    them.
    This class should act as a coordinator of all the mapping data structures.
    """
    def __init__(self, confobj, block_pool, flashobj, oobobj, recorderobj,
            envobj):
        self.conf = confobj
        self.flash = flashobj
        self.oob = oobobj
        self.block_pool = block_pool
        self.recorder = recorderobj
        self.env = envobj

        # managed and owned by Mappingmanager
        self.global_mapping_table = GlobalMappingTable(confobj, flashobj)
        self.cached_mapping_table = CachedMappingTable(confobj)
        self.directory = GlobalTranslationDirectory(confobj)

    def __del__(self):
        print self.flash.recorder.count_counter

    def ppns_for_writing(self, lpns):
        """
        This function returns ppns that can be written.

        The ppns returned are mapped by lpns, one to one
        """
        ppns = []
        for lpn in lpns:
            old_ppn = yield self.env.process( self.lpn_to_ppn(lpn) )
            new_ppn = self.block_pool.next_data_page_to_program()
            ppns.append(new_ppn)
            # CMT
            # lpn must be in cache thanks to self.mapping_manager.lpn_to_ppn()
            self.cached_mapping_table.overwrite_entry(
                lpn = lpn, ppn = new_ppn, dirty = True)
            # OOB
            self.oob.new_lba_write(lpn = lpn, old_ppn = old_ppn,
                new_ppn = new_ppn)

        self.env.exit(ppns)

    def ppns_for_reading(self, lpns):
        """
        """
        ppns = []
        for lpn in lpns:
            ppn = yield self.env.process( self.lpn_to_ppn(lpn) )
            ppns.append(ppn)

        self.env.exit(ppns)

    def lpn_to_ppn(self, lpn):
        """
        This method does not fail. It will try everything to find the ppn of
        the given lpn.
        return: real PPN or UNINITIATED
        """
        # try cached mapping table first.
        ppn = self.cached_mapping_table.lpn_to_ppn(lpn)
        if ppn == MISS:
            # cache miss
            while self.cached_mapping_table.is_full():
                yield self.process(self.evict_cache_entry())

            # find the physical translation page holding lpn's mapping in GTD
            ppn = yield self.env.process(
                    self.load_mapping_entry_to_cache(lpn))

            self.recorder.count_me("cache", "miss")
        else:
            self.recorder.count_me("cache", "hit")

        self.env.exit(ppn)

    def load_mapping_entry_to_cache(self, lpn):
        """
        When a mapping entry is not in cache, you need to read the entry from
        flash and put it to cache. This function does this.
        Output: it return the ppn of lpn read from entry on flash.
        """
        # find the location of the translation page
        m_ppn = self.directory.m_ppn_of_lpn(lpn)

        # read it up, this operation is just for statistics
        yield self.env.process(
                self.flash.rw_ppn_extent(m_ppn, 1, op = 'read'))

        # Now we have all the entries of m_ppn in memory, we need to put
        # the mapping of lpn->ppn to CMT
        ppn = self.global_mapping_table.lpn_to_ppn(lpn)
        self.cached_mapping_table.add_new_entry(lpn = lpn, ppn = ppn,
            dirty = False)

        self.env.exit(ppn)

    def initialize_mappings(self):
        """
        This function initialize global translation directory. We assume the
        GTD is very small and stored in flash before mounting. We also assume
        that the global mapping table has been prepared by the vendor, so there
        is no other overhead except for reading the GTD from flash. Since the
        overhead is very small, we ignore it.
        """
        total_pages = self.global_mapping_table.total_translation_pages()

        # use some free blocks to be translation blocks
        tmp_blk_mapping = {}
        for m_vpn in range(total_pages):
            m_ppn = self.block_pool.next_translation_page_to_program()
            # Note that we don't actually read or write flash
            self.directory.add_mapping(m_vpn=m_vpn, m_ppn=m_ppn)
            # update oob of the translation page
            self.oob.new_write(lpn = m_vpn, old_ppn = UNINITIATED,
                new_ppn = m_ppn)

    def update_entry(self, lpn, new_ppn, tag = "NA"):
        """
        Update mapping of lpn to be lpn->new_ppn everywhere if necessary.

        if lpn is not in cache, it will NOT be added to it.

        block_pool:
            it may be affect because we need a new page
        CMT:
            if lpn is in cache, we need to update it and mark it as clean
            since after this function the cache will be consistent with GMT
        GMT:
            we need to read the old translation page, update it and write it
            to a new flash page
        OOB:
            we need to wipe out the old_ppn and fill the new_ppn
        GTD:
            we need to update m_vpn to new m_ppn
        """
        cached_ppn = self.cached_mapping_table.lpn_to_ppn(lpn)
        if cached_ppn != MISS:
            # in cache
            self.cached_mapping_table.overwrite_entry(lpn = lpn,
                ppn = new_ppn, dirty = False)

        m_vpn = self.directory.m_vpn_of_lpn(lpn)

        # batch_entries may be empty
        batch_entries = self.dirty_entries_of_translation_page(m_vpn)

        new_mappings = {lpn:new_ppn} # lpn->new_ppn may not be in cache
        for entry in batch_entries:
            new_mappings[entry.lpn] = entry.ppn

        # update translation page
        yield self.env.process(
            self.update_translation_page_on_flash(m_vpn, new_mappings, tag))

        # mark as clean
        for entry in batch_entries:
            entry.dirty = False

    def evict_cache_entry(self):
        """
        Select one entry in cache
        If the entry is dirty, write it back to GMT.
        If it is not dirty, simply remove it.
        """
        self.recorder.count_me('cache', 'evict')

        vic_lpn, vic_entrydata = self.cached_mapping_table.victim_entry()

        if vic_entrydata.dirty == True:
            # If we have to write to flash, we write in batch
            m_vpn = self.directory.m_vpn_of_lpn(vic_lpn)
            yield self.env.process(self.batch_write_back(m_vpn))

        # remove only the victim entry
        self.cached_mapping_table.remove_entry_by_lpn(vic_lpn)

    def batch_write_back(self, m_vpn):
        """
        Write dirty entries in a translation page with a flash read and a flash write.
        """
        self.recorder.count_me('cache', 'batch_write_back')

        batch_entries = self.dirty_entries_of_translation_page(m_vpn)

        new_mappings = {}
        for entry in batch_entries:
            new_mappings[entry.lpn] = entry.ppn

        # update translation page
        self.recorder.count_me('batch.size', len(new_mappings))
        yield self.env.process(
                self.update_translation_page_on_flash(m_vpn, new_mappings,
                    TRANS_CACHE))

        # mark them as clean
        for entry in batch_entries:
            entry.dirty = False

    def dirty_entries_of_translation_page(self, m_vpn):
        """
        Get all dirty entries in translation page m_vpn.
        """
        retlist = []
        for entry_lpn, dataentry in self.cached_mapping_table.entries.items():
            if dataentry.dirty == True:
                tmp_m_vpn = self.directory.m_vpn_of_lpn(entry_lpn)
                if tmp_m_vpn == m_vpn:
                    retlist.append(dataentry)

        return retlist

    def update_translation_page_on_flash(self, m_vpn, new_mappings, tag):
        """
        Use new_mappings to replace their corresponding mappings in m_vpn

        read translationo page
        modify it with new_mappings
        write translation page to new location
        update related data structures

        Notes:
        - Note that it does not modify cached mapping table
        """
        old_m_ppn = self.directory.m_vpn_to_m_ppn(m_vpn)

        # update GMT on flash
        if len(new_mappings) < self.conf.dftl_n_mapping_entries_per_page():
            # need to read some mappings
            yield self.env.process(
                self.flash.rw_ppn_extent(old_m_ppn, 1, op = 'read') )
        else:
            self.recorder.count_me('cache', 'saved.1.read')

        pass # modify in memory. Since we are a simulator, we don't do anything
        new_m_ppn = self.block_pool.next_translation_page_to_program()

        # update flash
        yield self.env.process(
            self.flash.rw_ppn_extent(new_m_ppn, 1, op = 'write'))

        # update our fake 'on-flash' GMT
        for lpn, new_ppn in new_mappings.items():
            self.global_mapping_table.update(lpn = lpn, ppn = new_ppn)

        # OOB, keep m_vpn as lpn
        self.oob.new_write(lpn = m_vpn, old_ppn = old_m_ppn,
            new_ppn = new_m_ppn)

        # update GTD so we can find it
        self.directory.update_mapping(m_vpn = m_vpn, m_ppn = new_m_ppn)


class GcDecider(object):
    """
    It is used to decide wheter we should do garbage collection.

    When need_cleaning() is called the first time, use high water mark
    to decide if we need GC.
    Later, use low water mark and progress to decide. If we haven't make
    progress in 10 times, stop GC
    """
    def __init__(self, confobj, block_pool, recorderobj):
        self.conf = confobj
        self.block_pool = block_pool
        self.recorder = recorderobj

        # Check if the high_watermark is appropriate
        # The high watermark should not be lower than the file system size
        # because if the file system is full you have to constantly GC and
        # cannot get more space
        min_high = 1 / float(self.conf['dftl']['over_provisioning'])
        if self.conf['dftl']['GC_threshold_ratio'] < min_high:
            hi_watermark_ratio = min_high
            print 'High watermark is reset to {}. It was {}'.format(
                hi_watermark_ratio, self.conf['dftl']['GC_threshold_ratio'])
        else:
            hi_watermark_ratio = self.conf['dftl']['GC_threshold_ratio']
            print 'Using user defined high watermark', hi_watermark_ratio

        self.high_watermark = hi_watermark_ratio * \
            self.conf.n_blocks_per_dev

        min_low = 0.8 * 1 / self.conf['dftl']['over_provisioning']
        if self.conf['dftl']['GC_low_threshold_ratio'] < min_low:
            low_watermark_ratio = min_low
            print 'Low watermark is reset to {}. It was {}'.format(
                low_watermark_ratio, self.conf['dftl']['GC_low_threshold_ratio'])
        else:
            low_watermark_ratio = self.conf['dftl']['GC_low_threshold_ratio']
            print 'Using user defined low watermark', low_watermark_ratio

        self.low_watermark = low_watermark_ratio * \
            self.conf.n_blocks_per_dev

        print 'High watermark', self.high_watermark
        print 'Low watermark', self.low_watermark

        self.call_index = -1
        self.last_used_blocks = None
        self.freeze_count = 0

    def refresh(self):
        """
        TODO: this class needs refactoring.
        """
        self.call_index = -1
        self.last_used_blocks = None
        self.freeze_count = 0

    def need_cleaning(self):
        "The logic is a little complicated"
        self.call_index += 1

        n_used_blocks = self.block_pool.total_used_blocks()

        if self.call_index == 0:
            # clean when above high_watermark
            ret = n_used_blocks > self.high_watermark
            # raise the high water mark because we want to avoid frequent GC
            if ret == True:
                self.raise_high_watermark()
        else:
            if self.freezed_too_long(n_used_blocks):
                ret = False
                print 'freezed too long, stop GC'
                self.recorder.count_me("GC", 'freezed_too_long')
            else:
                # Is it higher than low watermark?
                ret = n_used_blocks > self.low_watermark
                if ret == False:
                    self.recorder.count_me("GC", 'below_lowerwatermark')
                    # We were able to bring used block to below lower
                    # watermark. It means we still have a lot free space
                    # We don't need to worry about frequent GC.
                    self.reset_high_watermark()

        return ret

    def reset_high_watermark(self):
        return

        self.high_watermark = self.high_watermark_orig

    def raise_high_watermark(self):
        """
        Raise high watermark.

        95% of the total blocks are the highest possible
        """
        return

        self.high_watermark = min(self.high_watermark * 1.01,
            self.conf.n_blocks_per_dev * 0.95)

    def lower_high_watermark(self):
        """
        THe lowest is the original value
        """
        return

        self.high_watermark = max(self.high_watermark_orig,
            self.high_watermark / 1.01)

    def improved(self, cur_n_used_blocks):
        """
        wether we get some free blocks since last call of this function
        """
        if self.last_used_blocks == None:
            ret = True
        else:
            # common case
            ret = cur_n_used_blocks < self.last_used_blocks

        self.last_used_blocks = cur_n_used_blocks
        return ret

    def freezed_too_long(self, cur_n_used_blocks):
        if self.improved(cur_n_used_blocks):
            self.freeze_count = 0
            ret = False
        else:
            self.freeze_count += 1

            if self.freeze_count > 2 * self.conf.n_pages_per_block:
                ret = True
            else:
                ret = False

        return ret


class BlockInfo(object):
    """
    This is for sorting blocks to clean the victim.
    """
    def __init__(self, block_type, block_num, value):
        self.block_type = block_type
        self.block_num = block_num
        self.value = value

    def __comp__(self, other):
        "You can switch between benefit/cost and greedy"
        return cmp(self.valid_ratio, other.valid_ratio)
        # return cmp(self.value, other.value)


class GarbageCollector(object):
    def __init__(self, confobj, flashobj, oobobj, block_pool, mapping_manager,
        recorderobj, envobj):
        self.conf = confobj
        self.flash = flashobj
        self.oob = oobobj
        self.block_pool = block_pool
        self.recorder = recorderobj
        self.env = envobj

        self.mapping_manager = mapping_manager

        self.decider = GcDecider(self.conf, self.block_pool, self.recorder)

        self.victim_block_seqid = 0

    def try_gc(self):
        triggered = False

        self.decider.refresh()
        while self.decider.need_cleaning():
            if self.decider.call_index == 0:
                triggered = True
                self.recorder.count_me("GC", "invoked")
                print 'GC is triggerred', self.block_pool.used_ratio(), \
                    'freeblocks:', len(self.block_pool.freeblocks)
                block_iter = self.victim_blocks_iter()
                blk_cnt = 0
            try:
                blockinfo = block_iter.next()
            except StopIteration:
                print 'GC stoped from StopIteration exception'
                self.recorder.count_me("GC", "StopIteration")
                # high utilization, raise watermarkt to reduce GC attempts
                self.decider.raise_high_watermark()
                # nothing to be cleaned
                break
            victim_type, victim_block = (blockinfo.block_type,
                blockinfo.block_num)
            if victim_type == DATA_BLOCK:
                yield self.env.process(self.clean_data_block(victim_block))
            elif victim_type == TRANS_BLOCK:
                yield self.env.process(self.clean_trans_block(victim_block))
            blk_cnt += 1

        if triggered:
            print 'GC is finished', self.block_pool.used_ratio(), \
                blk_cnt, 'collected', \
                'freeblocks:', len(self.block_pool.freeblocks)
            # raise RuntimeError("intentional exit")

    def clean_data_block(self, flash_block):
        start, end = self.conf.block_to_page_range(flash_block)

        changes = []
        for ppn in range(start, end):
            if self.oob.states.is_page_valid(ppn):
                change = yield self.env.process(
                        self.move_data_page_to_new_location(ppn))
                changes.append(change)

        # change the mappings
        self.update_mapping_in_batch(changes)

        # mark block as free
        self.block_pool.move_used_data_block_to_free(flash_block)
        # it handles oob and flash
        yield self.env.process(
                self.erase_block(flash_block, DATA_CLEANING))

    def clean_trans_block(self, flash_block):
        yield self.env.process(
                self.move_valid_pages(flash_block,
                self.move_trans_page_to_new_location))
        # mark block as free
        self.block_pool.move_used_trans_block_to_free(flash_block)
        # it handles oob and flash
        yield self.env.process(
                self.erase_block(flash_block, TRANS_CLEAN))

    def move_valid_pages(self, flash_block, mover_func):
        start, end = self.conf.block_to_page_range(flash_block)

        for ppn in range(start, end):
            if self.oob.states.is_page_valid(ppn):
                yield self.env.process( mover_func(ppn) )

    def move_valid_data_pages(self, flash_block, mover_func):
        """
        With batch update:
        1. Move all valid pages to new location.
        2. Aggregate mappings in the same translation page and update together
        """
        start, end = self.conf.block_to_page_range(flash_block)

        for ppn in range(start, end):
            if self.oob.states.is_page_valid(ppn):
                mover_func(ppn)

    def move_data_page_to_new_location(self, ppn):
        """
        This function only moves data pages, but it does not update mappings.
        It will return the mappings changes to so another function can update
        the mapping.
        """
        # for my damaged brain
        old_ppn = ppn

        # read the the data page
        self.env.process(
                self.flash.rw_ppn_extent(old_ppn, 1, op = 'read'))

        # find the mapping
        lpn = self.oob.translate_ppn_to_lpn(old_ppn)

        # write to new page
        new_ppn = self.block_pool.next_gc_data_page_to_program()
        self.env.process(
                self.flash.rw_ppn_extent(new_ppn, 1, op = 'write'))

        # update new page and old page's OOB
        self.oob.data_page_move(lpn, old_ppn, new_ppn)

        ret = {'lpn':lpn, 'old_ppn':old_ppn, 'new_ppn':new_ppn}
        self.env.exit(ret)

    def group_changes(self, changes):
        """
        ret groups:
            { m_vpn_1: [
                      {'lpn':lpn, 'old_ppn':old_ppn, 'new_ppn':new_ppn},
                      {'lpn':lpn, 'old_ppn':old_ppn, 'new_ppn':new_ppn},
                      ...],
              m_vpn_2: [
                      {'lpn':lpn, 'old_ppn':old_ppn, 'new_ppn':new_ppn},
                      {'lpn':lpn, 'old_ppn':old_ppn, 'new_ppn':new_ppn},
                      ...],
        """
        # Put the mapping changes into groups, each group belongs to one mvpn
        groups = {}
        for change in changes:
            m_vpn = self.mapping_manager.directory.m_vpn_of_lpn(change['lpn'])
            group = groups.setdefault(m_vpn, [])
            group.append(change)

        return groups

    def update_flash_mappings(self, m_vpn, changes_list):
        # update translation page on flash
        new_mappings = {change['lpn']:change['new_ppn']
                for change in changes_list}
        yield self.env.process(
                self.mapping_manager.update_translation_page_on_flash(
                m_vpn, new_mappings, TRANS_UPDATE_FOR_DATA_GC))

    def update_cache_mappings(self, changes_in_cache):
        # some mappings are in flash and some in cache
        # we can set mappings in cache as dirty=False since
        # they are consistent with flash
        for change in changes_in_cache:
            lpn = change['lpn']
            old_ppn = change['old_ppn']
            new_ppn = change['new_ppn']
            self.mapping_manager.cached_mapping_table\
                .overwrite_entry(
                lpn = lpn, ppn = new_ppn, dirty = False)

    def apply_mvpn_changes(self, m_vpn, changes_list):
        """
        changes
          [
              {'lpn':lpn, 'old_ppn':old_ppn, 'new_ppn':new_ppn},
              {'lpn':lpn, 'old_ppn':old_ppn, 'new_ppn':new_ppn},
          ...]

        # if some mappings are in cache and some are in flash, you
        # can set dirty=False since both cache and flash will be
        # updated.
        # if all mappings are in cache you need to set dirty=True
        # since flash will not be updated
        # if all mappings are in flash you do nothing with cache
        """
        changes_in_cache = []
        some_in_cache = False
        some_in_flash = False
        for change in changes_list:
            lpn = change['lpn']
            old_ppn = change['old_ppn']
            new_ppn = change['new_ppn']

            cached_ppn = self.mapping_manager\
                .cached_mapping_table.lpn_to_ppn(lpn)
            if cached_ppn != MISS:
                # lpn is in cache
                some_in_cache = True
                self.mapping_manager.cached_mapping_table.overwrite_entry(
                    lpn = lpn, ppn = new_ppn, dirty = True)
                changes_in_cache.append(change)
            else:
                # lpn is not in cache, mark it and update later in batch
                some_in_flash = True

        if some_in_flash == True:
            yield self.env.process(
                    self.update_flash_mappings(m_vpn, changes_list))
            if some_in_cache == True:
                self.update_cache_mappings(changes_in_cache)

    def update_mapping_in_batch(self, changes):
        """
        changes is a table in the form of:
        [
          {'lpn':lpn, 'old_ppn':old_ppn, 'new_ppn':new_ppn},
          {'lpn':lpn, 'old_ppn':old_ppn, 'new_ppn':new_ppn},
          ...
        ]

        This function groups the LPNs that in the same MVPN and updates them
        together.

        If a MVPN has some entries in cache and some not, we need to update
        both cache (for the ones in cache) and the on-flash translation page.
        If a MVPN has only entries in cache, we will only update cache, and
            mark them dirty
        If a MVPN has only entries on flash, we will only update flash.
        """
        # Put the mapping changes into groups, each group belongs to one mvpn
        groups = self.group_changes(changes)

        for m_vpn, changes_list in groups.items():
            self.apply_mvpn_changes(m_vpn, changes_list)
    def move_trans_page_to_new_location(self, m_ppn):
        """
        1. read the trans page
        2. write to new location
        3. update OOB
        4. update GTD
        """
        old_m_ppn = m_ppn

        m_vpn = self.oob.translate_ppn_to_lpn(old_m_ppn)

        yield self.env.process(
            self.flash.rw_ppn_extent(old_m_ppn, 1, op = 'read'))

        # write to new page
        new_m_ppn = self.block_pool.next_gc_translation_page_to_program()
        yield self.env.process(
            self.flash.rw_ppn_extent(new_m_ppn, 1, op = 'write'))

        # update new page and old page's OOB
        self.oob.new_write(m_vpn, old_m_ppn, new_m_ppn)

        # update GTD
        self.mapping_manager.directory.update_mapping(m_vpn = m_vpn,
            m_ppn = new_m_ppn)

    def benefit_cost(self, blocknum, current_time):
        """
        This follows the DFTL paper
        """
        valid_ratio = self.oob.states.block_valid_ratio(blocknum)

        if valid_ratio == 0:
            # empty block is always the best deal
            return float("inf"), valid_ratio

        if valid_ratio == 1:
            # it is possible that none of the pages in the block has been
            # invalidated yet. In that case, all pages in the block is valid.
            # we don't need to clean it.
            return 0, valid_ratio

        last_inv_time = self.oob.last_inv_time_of_block.get(blocknum, None)
        if last_inv_time == None:
            print blocknum
            raise RuntimeError(
                "blocknum {} has never been invalidated."\
                "valid ratio:{}."
                .format(blocknum, valid_ratio))

        age = current_time - self.oob.last_inv_time_of_block[blocknum]
        age = age.total_seconds()
        bene_cost = age * ( 1 - valid_ratio ) / ( 2 * valid_ratio )

        return bene_cost, valid_ratio

    def victim_blocks_iter(self):
        """
        Calculate benefit/cost and put it to a priority queue
        """
        current_blocks = self.block_pool.current_blocks()
        current_time = datetime.datetime.now()
        priority_q = Queue.PriorityQueue()

        for usedblocks, block_type in (
            (self.block_pool.data_usedblocks, DATA_BLOCK),
            (self.block_pool.trans_usedblocks, TRANS_BLOCK)):
            for blocknum in usedblocks:
                if blocknum in current_blocks:
                    continue

                bene_cost, valid_ratio = self.benefit_cost(blocknum,
                    current_time)

                if bene_cost == 0:
                    # valid_ratio must be zero, we definitely don't
                    # want to cleaning it because we cannot get any
                    # free pages from it
                    continue

                blk_info = BlockInfo(block_type = block_type,
                    block_num = blocknum, value = bene_cost)
                blk_info.valid_ratio = valid_ratio

                if blk_info.valid_ratio > 0:
                    lpns = self.oob.lpns_of_block(blocknum)
                    s, e = self.conf.block_to_page_range(blocknum)
                    ppns = range(s, e)

                    ppn_states = [self.oob.states.page_state_human(ppn)
                        for ppn in ppns]
                    blk_info.mappings = zip(ppns, lpns, ppn_states)

                priority_q.put(blk_info)

        while not priority_q.empty():
            b_info =  priority_q.get()

            # record the information of victim block
            self.recorder.count_me('block.info.valid_ratio',
                round(b_info.valid_ratio, 2))
            self.recorder.count_me('block.info.bene_cost',
                round(b_info.value))

            if self.conf['record_bad_victim_block'] == True and \
                b_info.valid_ratio > 0:
                self.recorder.write_file('bad_victim_blocks',
                    block_type = b_info.block_type,
                    block_num = b_info.block_num,
                    bene_cost = b_info.value,
                    valid_ratio = round(b_info.valid_ratio, 2))

                # lpn ppn ppn_states blocknum
                for ppn, lpn, ppn_state in b_info.mappings:
                    if b_info.block_type == DATA_BLOCK:
                        lpn_timestamp = self.oob.timestamp_table[ppn]
                    else:
                        lpn_timestamp = -1

                    self.recorder.write_file('bad.block.mappings',
                        ppn = ppn,
                        lpn = lpn,
                        ppn_state = ppn_state,
                        block_num = b_info.block_num,
                        valid_ratio = b_info.valid_ratio,
                        block_type = b_info.block_type,
                        victim_block_seqid = self.victim_block_seqid,
                        lpn_timestamp = lpn_timestamp
                        )

            self.victim_block_seqid += 1

            yield b_info

    def erase_block(self, blocknum, tag):
        """
        THIS IS NOT A PUBLIC API
        set pages' oob states to ERASED
        electrionically erase the pages
        """
        # set page states to ERASED and in-OOB lpn to nothing
        self.oob.erase_block(blocknum)

        self.env.process(
            self.flash.erase_pbn_extent(blocknum, 1))


def dec_debug(function):
    def wrapper(self, lpn):
        ret = function(self, lpn)
        if lpn == 38356:
            print function.__name__, 'lpn:', lpn, 'ret:', ret
        return ret
    return wrapper

#
# - translation pages
#   - cache miss read (trans.cache.load)
#   - eviction write  (trans.cache.evict)
#   - cleaning read   (trans.clean)
#   - cleaning write  (trans.clean)
# - data pages
#   - user read       (data.user)
#   - user write      (data.user)
#   - cleaning read   (data.cleaning)
#   - cleaning writes (data.cleaning)
# Tag format
# pagetype.
# Example tags:

# trans cache read is due to cache misses, the read fetches translation page
# to cache.
# write is due to eviction. Note that entry eviction may incure both page read
# and write.
TRANS_CACHE = "trans.cache"

# trans clean include:
#  erasing translation block
#  move translation page during gc (including read and write)
TRANS_CLEAN = "trans.clean"  #read/write are for moving pages

#  clean_data_block()
#   update_mapping_in_batch()
#    update_translation_page_on_flash() this is the same as cache eviction
TRANS_UPDATE_FOR_DATA_GC = "trans.update.for.data.gc"

DATA_USER = "data.user"

# erase data block in clean_data_block()
# move data page during gc (including read and write)
DATA_CLEANING = "data.cleaning"

class Dftl(object):
    """
    The implementation literally follows DFtl paper.
    This class is a coordinator of other coordinators and data structures
    """
    def __init__(self, confobj, recorderobj, flashcontrollerobj, env):
        self.conf = confobj
        self.recorder = recorderobj
        self.flash = flashcontrollerobj
        self.env = env

        # bitmap has been created parent class
        # Change: we now don't put the bitmap here
        # self.bitmap.initialize()
        # del self.bitmap

        self.global_helper = GlobalHelper(confobj)

        # Replace the flash object with a new one, which has global helper
        # self.flash = ParallelFlash(self.conf, self.recorder, self.global_helper)

        self.block_pool = BlockPool(confobj)
        self.oob = OutOfBandAreas(confobj)

        ###### the managers ######
        self.mapping_manager = MappingManager(
            confobj = self.conf,
            block_pool = self.block_pool,
            flashobj = self.flash,
            oobobj=self.oob,
            recorderobj = recorderobj,
            envobj = env
            )

        self.garbage_collector = GarbageCollector(
            confobj = self.conf,
            flashobj = self.flash,
            oobobj=self.oob,
            block_pool = self.block_pool,
            mapping_manager = self.mapping_manager,
            recorderobj = recorderobj,
            envobj = env
            )

        # We should initialize Globaltranslationdirectory in Dftl
        self.mapping_manager.initialize_mappings()

        self.n_sec_per_page = self.conf.page_size \
                / self.conf['sector_size']

        # This resource protects all data structures stored in the memory.
        self.resource_ram = simpy.Resource(self.env, capacity = 1)

    def translate(self, io_req):
        """
        io_req is of type simulator.Event()

        Our job here is to find the corresponding physical flat address
        of the address in io_req. The during the translation, we may
        need to synchronizely access flash.

        We do the following here:
        Read:
            1. find out the range of logical pages
            2. for each logical page:
                if mapping is in cache, just translate it
                else bring mapping to cache and possibly evict a mapping entry
        """
        with self.resource_ram.request() as ram_request:
            yield ram_request # serialize all access to data structures

            # for debug
            yield self.env.timeout(3)

            flash_reqs = yield self.env.process(
                    self.handle_io_requests(io_req))
            # flash_reqs = self.handle_io_requests(io_req)
            self.env.exit(flash_reqs)

    def handle_io_requests(self, io_req):
        print 'handling request', str(io_req)
        lpn_start, lpn_count = self.conf.sec_ext_to_page_ext(io_req.sector,
                io_req.sector_count)
        lpns = range(lpn_start, lpn_start + lpn_count)
        print 'lpns', lpns

        if io_req.operation == 'read':
            ppns = yield self.env.process(
                    self.mapping_manager.ppns_for_reading(lpns))
        elif io_req.operation == 'write':
            ppns = yield self.env.process(
                    self.mapping_manager.ppns_for_writing(lpns))
            print 'write ppns', ppns
        else:
            print 'io operation', io_req.operation, 'is not processed'
            ppns = []

        flash_reqs = []
        for ppn in ppns:
            if ppn == 'UNINIT':
                continue

            req = self.flash.get_flash_requests_for_ppns(ppn, 1,
                    op = io_req.operation)
            flash_reqs.extend(req)

        self.env.exit(flash_reqs)

    def lba_discard(self, lpn, pid = None):
        """
        block_pool:
            no need to update
        CMT:
            if lpn->ppn exist, you need to update it to lpn->UNINITIATED
            if not exist, you need to add lpn->UNINITIATED
            the mapping lpn->UNINITIATED will be written back to GMT later
        GMT:
            no need to update
            REMEMBER: all updates to GMT can and only can be maded through CMT
        OOB:
            invalidate the ppn
            remove the lpn
        GTD:
            no updates needed
            updates should be done by GC
        """
        self.recorder.put('logical_discard', lpn, 'user')

        # self.recorder.write_file('lba.trace.txt',
            # timestamp = self.oob.timestamp(),
            # operation = 'discard',
            # lpn =  lpn
        # )

        ppn = yield self.env.process(self.mapping_manager.lpn_to_ppn(lpn))
        if ppn == UNINITIATED:
            return

        # flash page ppn has valid data
        self.mapping_manager.cached_mapping_table.overwrite_entry(lpn = lpn,
            ppn = UNINITIATED, dirty = True)

        # OOB
        self.oob.wipe_ppn(ppn)

        # garbage collection checking and possibly doing
        # self.garbage_collector.try_gc()

    def check_read(self, sector, sector_count, data):
        for sec, sec_data in zip(
                range(sector, sector + sector_count), data):
            if sec_data == None:
                continue
            if not sec_data.startswith(str(sec)):
                msg = "request: sec {} count {}\n".format(sector, sector_count)
                msg += "INFTL: Data is not correct. Got: {read}, "\
                        "sector={sec}".format(
                        read = sec_data,
                        sec = sec)
                # print msg
                raise RuntimeError(msg)

    def page_to_sec_items(self, data):
        ret = []
        for page_data in data:
            if page_data == None:
                page_data = [None] * self.n_sec_per_page
            for item in page_data:
                ret.append(item)

        return ret

    def sec_to_page_items(self, data):
        if data == None:
            return None

        sec_per_page = self.conf.page_size / self.conf['sector_size']
        n_pages = len(data) / sec_per_page

        new_data = []
        for page in range(n_pages):
            page_items = []
            for sec in range(sec_per_page):
                page_items.append(data[page * sec_per_page + sec])
            new_data.append(page_items)

        return new_data

    def pre_workload(self):
        pass

    def post_processing(self):
        """
        This function is called after the simulation.
        """
        pass

    def get_type(self):
        return "dftlext"


class ParallelFlash(object):
    def __init__(self, confobj, recorderobj, globalhelper = None):
        self.conf = confobj
        self.recorder = recorderobj
        self.global_helper = globalhelper
        self.flash_backend = flash.SimpleFlash(recorderobj, confobj)

    def get_max_channel_page_count(self, ppns):
        """
        Find the max count of the channels
        """
        pbns = []
        for ppn in ppns:
            if ppn == 'UNINIT':
                # skip it so unitialized ppn does not involve flash op
                continue
            block, _ = self.conf.page_to_block_off(ppn)
            pbns.append(block)

        return self.get_max_channel_block_count(pbns)

    def get_max_channel_block_count(self, pbns):
        channel_counter = Counter()
        for pbn in pbns:
            channel, _ = block_to_channel_block(self.conf, pbn)
            channel_counter[channel] += 1

        return self.find_max_count(channel_counter)

    def find_max_count(self, channel_counter):
        if len(channel_counter) == 0:
            return 0
        else:
            max_channel, max_count = channel_counter.most_common(1)[0]
            return max_count

    def read_pages(self, ppns, tag):
        """
        Read ppns in batch and calculate time
        lpns are the corresponding lpns of ppns, we pass them in for checking
        """
        max_count = self.get_max_channel_page_count(ppns)

        data = []
        for ppn in ppns:
            data.append( self.flash_backend.page_read(ppn, tag) )
        return data

    def write_pages(self, ppns, ppn_data, tag):
        """
        This function will store ppn_data to flash and calculate the time
        it takes to do it with real flash.

        The access time is determined by the channel with the longest request
        queue.
        """
        max_count = self.get_max_channel_page_count(ppns)

        # save the data to flash
        if ppn_data == None:
            for ppn in ppns:
                self.flash_backend.page_write(ppn, tag)
        else:
            for ppn, item in zip(ppns, ppn_data):
                self.flash_backend.page_write(ppn, tag, data = item)

    def erase_blocks(self, pbns, tag):
        max_count = self.get_max_channel_block_count(pbns)

        for block in pbns:
            self.flash_backend.block_erase(block, cat = tag)


"""
Transforming this ftl to DES-enabled needs these steps
1. treat this realftl as a non-simpy process, have the interface
return flash hierarchy requests
2. make the interface simpy process, add yield timeout() for testing
3. change the realftl to use flash controller, which queues requests.
4. Done
"""


