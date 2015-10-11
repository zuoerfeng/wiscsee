#!/usr/bin/env python

import argparse
import sys

import bmftl
import config
import dftl2
import dmftl
import flash
import hmftl
import nkftl
import pmftl
import recorder
import tpftl


def event_line_to_dic(line):
    keys = ['operation', 'offset', 'size']
    items = line.strip('\n').split()
    items[1] = eval(items[1]) # offset
    items[2] = eval(items[2]) # size
    event = dict(zip(keys, items))
    return event

class Simulator(object):
    def __init__(self, conf):
        "conf is class Config"
        if not isinstance(conf, config.Config):
            raise TypeError("conf is not config.Config, it is {}".
                format(type(conf).__name__))

        self.conf = conf

        # initialize recorder
        self.rec = recorder.Recorder(output_target = self.conf['output_target'],
            path = self.conf.get_output_file_path(),
            verbose_level = self.conf['verbose_level'],
            print_when_finished = self.conf['print_when_finished']
            )

        if self.conf['ftl_type'] == 'directmap':
            ftl_class = dmftl.DirectMapFtl
        elif self.conf['ftl_type'] == 'blockmap':
            ftl_class = bmftl.BlockMapFtl
        elif self.conf['ftl_type'] == 'pagemap':
            ftl_class = pmftl.PageMapFtl
        elif self.conf['ftl_type'] == 'hybridmap':
            ftl_class = hmftl.HybridMapFtl
        elif self.conf['ftl_type'] == 'dftl2':
            ftl_class = dftl2.Dftl
        elif self.conf['ftl_type'] == 'tpftl':
            ftl_class = tpftl.Tpftl
        elif self.conf['ftl_type'] == 'nkftl':
            ftl_class = nkftl.Nkftl
        else:
            raise ValueError("ftl_type {} is not defined"\
                .format(self.conf['ftl_type']))

        self.ftl = ftl_class(self.conf, self.rec,
            flash.Flash(recorder = self.rec))

        if self.conf['ftl_type'] == 'tpftl':
            self.interface_level = 'range'
        else:
            self.interface_level = 'page'

    def process_event(self, event):
        if event['operation'] == 'read':
            if self.interface_level == 'page':
                pages = self.conf.off_size_to_page_list(event['offset'],
                    event['size'], force_alignment = False)
                for page in pages:
                    self.ftl.lba_read(page)
            elif self.interface_level == 'range':
                start_page, npages = self.conf.off_size_to_page_range(
                    event['offset'], event['size'], force_alignment = False)
                self.ftl.read_range(start_page, npages)
            else:
                raise RuntimeError("interface_level {} not supported".format(
                    self.interface_level))

        elif event['operation'] == 'write':
            if self.interface_level == 'page':
                pages = self.conf.off_size_to_page_list(event['offset'],
                    event['size'])
                for page in pages:
                    self.ftl.lba_write(page)
            elif self.interface_level == 'range':
                start_page, npages = self.conf.off_size_to_page_range(
                    event['offset'], event['size'])
                self.ftl.write_range(start_page, npages)
            else:
                raise RuntimeError("interface_level {} not supported".format(
                    self.interface_level))

        elif event['operation'] == 'discard':
            if self.interface_level == 'page':
                pages = self.conf.off_size_to_page_list(event['offset'],
                    event['size'])
                for page in pages:
                    self.ftl.lba_discard(page)
            elif self.interface_level == 'range':
                start_page, npages = self.conf.off_size_to_page_range(
                    event['offset'], event['size'])
                self.ftl.discard_range(start_page, npages)
            else:
                raise RuntimeError("interface_level {} not supported".format(
                    self.interface_level))

        elif event['operation'] == 'enable_recorder':
            self.ftl.enable_recording()
        elif event['operation'] == 'disable_recorder':
            self.ftl.disable_recording()
        else:
            raise RuntimeError("operation '{}' is not supported".format(
                event['operation']))

    def run(self, event_line_iter):
        cnt = 0
        for event_line in event_line_iter:
            event = event_line_to_dic(event_line)
            self.process_event(event)
            cnt += 1
            if cnt % 100 == 0:
                print '|',
                sys.stdout.flush()

